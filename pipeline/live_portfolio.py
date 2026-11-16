"""Live portfolio tracking for Lodestar.

Reads execution details from data/live_portfolio.json.
Syncs live valuation from Trading 212 API if credentials exist,
or falls back to yfinance market quotes and live exchange rates.
Compares performance directly against VUAG.L, VWRP.L, VUKG.L, and VALL.L.
"""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import json
import math
import os
import urllib.request
from pathlib import Path
import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE.parent / "data"

ENV_PATHS = [
    Path("/Users/leeslater/projects/portfolio-tracker/.env"),
    Path("/Users/leeslater/.mithril/ping.env"),
    HERE.parent / ".env",
]


def load_t212_credentials() -> tuple[str | None, str | None, str]:
    key = os.environ.get("T212_KEY_ID") or os.environ.get("T212_KEY")
    secret = os.environ.get("T212_SECRET")
    base = os.environ.get("T212_BASE", "https://live.trading212.com")
    for p in ENV_PATHS:
        if (not key or not secret) and p.exists():
            try:
                for line in p.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        val = v.strip().strip('"').strip("'")
                        if k in ("T212_KEY_ID", "T212_KEY") and not key:
                            key = val
                        elif k == "T212_SECRET" and not secret:
                            secret = val
                        elif k == "T212_BASE" and not base:
                            base = val
            except Exception:
                pass
    return key, secret, base


class _QuoteDict(dict):
    """Dictionary of latest quotes carrying the source close frame and asset date."""

    def __init__(self, *args, frame: pd.DataFrame | None = None, asof: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.frame = frame
        self.asof = asof


def _normalize_close_frame(raw, all_symbols: list[str]) -> pd.DataFrame:
    if raw is None:
        return pd.DataFrame()
    close = raw["Close"] if "Close" in raw else raw
    if isinstance(close, pd.Series):
        if not all_symbols:
            return pd.DataFrame()
        close = close.to_frame(name=all_symbols[0])
    if close is None or close.empty:
        return pd.DataFrame()
    df = close.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df = df.groupby(level=0).last().sort_index()
    return df.dropna(how="all")


def _last_valid_quotes(all_symbols: list[str], start: str | None = None) -> dict[str, float]:
    """Take the latest finite positive close per symbol so 24h FX never yields NaN for closed LSE lines."""
    if not all_symbols:
        return _QuoteDict()
    kwargs = {"progress": False, "auto_adjust": False}
    if start:
        kwargs["start"] = start
    else:
        kwargs["period"] = "5d"
    raw = yf.download(all_symbols, **kwargs)
    df = _normalize_close_frame(raw, all_symbols)
    out = _QuoteDict(frame=df)
    if df.empty:
        return out
    latest_dates: list[pd.Timestamp] = []
    for sym in all_symbols:
        if sym in df.columns:
            s = pd.to_numeric(df[sym], errors="coerce").dropna()
            s = s[s.map(lambda v: math.isfinite(float(v)) and float(v) > 0)]
            if not s.empty:
                out[sym] = float(s.iloc[-1])
                if not sym.endswith("=X"):
                    latest_dates.append(pd.Timestamp(s.index[-1]))
    if latest_dates:
        out.asof = max(latest_dates).date().isoformat()
    return out


def _holding_currency(holding: dict) -> str:
    cur = holding.get("currency")
    if cur:
        return str(cur)
    if "fill_price_usd" in holding:
        return "USD"
    if "fill_price_eur" in holding:
        return "EUR"
    if "fill_price_gbp" in holding or "fill_price_gbx" in holding:
        return "GBp"
    return "GBp"


def _fallback_price_gbp(holding: dict, quotes: dict[str, float], fx_gbp_usd: float, fx_eur_gbp: float) -> float:
    """Convert a Yahoo market quote or stored fill price into GBP pounds."""
    ticker = holding["ticker"]
    cur = _holding_currency(holding)
    if ticker in quotes:
        px_raw = float(quotes[ticker])
        if cur in ("GBp", "GBX"):
            return px_raw / 100.0
        if cur == "GBP":
            return px_raw
        if cur == "EUR":
            return px_raw * fx_eur_gbp
        return px_raw / fx_gbp_usd
    if "fill_price_gbp" in holding:
        return float(holding["fill_price_gbp"])
    if "fill_price_gbx" in holding:
        return float(holding["fill_price_gbx"]) / 100.0
    if "fill_price_usd" in holding:
        return float(holding["fill_price_usd"]) / fx_gbp_usd
    if "fill_price_eur" in holding:
        return float(holding["fill_price_eur"]) * fx_eur_gbp
    shares = float(holding["shares"])
    return float(holding["cost_gbp"]) / shares if shares else 0.0


def get_live_portfolio_data(broker: bool = False) -> dict:
    cfg_path = DATA_DIR / "live_portfolio.json"
    if not cfg_path.exists():
        return {}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    initial_cap = float(cfg.get("initial_capital_gbp", 5248.14))

    # Broker data is requested only for private Telegram/email reports.
    t212_positions = {}
    broker_status = "market_quotes"
    if broker:
        from pipeline.broker import positions
        t212_positions = positions()
        broker_status = "trading212"

    if broker:
        missing = [h["ticker"] for h in cfg.get("holdings", [])
                   if h.get("t212_ticker") not in t212_positions]
        if missing:
            raise RuntimeError("Configured holdings were not found in the broker response")

    # 2. Fetch live quotes via yfinance for fallback and benchmarks
    bm_symbols = list(cfg.get("benchmarks", {}).keys())
    holding_symbols = [h["ticker"] for h in cfg.get("holdings", [])]
    asset_symbols = holding_symbols + bm_symbols
    currencies = {_holding_currency(h) for h in cfg.get("holdings", [])}
    fx_symbols = []
    if "USD" in currencies:
        fx_symbols.append("GBPUSD=X")
    if "EUR" in currencies:
        fx_symbols.append("EURGBP=X")
    all_symbols = bm_symbols + holding_symbols + fx_symbols
    entry_date_str = cfg.get("entry_date", "2026-11-12")
    try:
        fetch_start = (
            min(pd.Timestamp(entry_date_str), pd.Timestamp.today()) - pd.Timedelta(days=7)
        ).date().isoformat()
    except Exception:
        fetch_start = None
    try:
        raw_quotes = _last_valid_quotes(all_symbols, start=fetch_start) if fetch_start else _last_valid_quotes(all_symbols)
    except TypeError:
        try:
            raw_quotes = _last_valid_quotes(all_symbols)
        except Exception:
            raw_quotes = {}
    except Exception:
        raw_quotes = {}
    quotes = raw_quotes if hasattr(raw_quotes, "get") else {}
    quote_frame = getattr(raw_quotes, "frame", None)
    quote_asof = getattr(raw_quotes, "asof", None)

    fx_gbp_usd = float(quotes.get("GBPUSD=X", 1.3265))
    fx_eur_gbp = float(quotes.get("EURGBP=X", 0.85))

    def _snapshot_for(day_quotes: dict[str, float]) -> tuple[float, float, dict[str, tuple[float, float]]]:
        day_fx_usd = float(day_quotes.get("GBPUSD=X", fx_gbp_usd))
        day_fx_eur = float(day_quotes.get("EURGBP=X", fx_eur_gbp))
        day_val = 0.0
        for h in cfg.get("holdings", []):
            sh = float(h["shares"])
            px_gbp = _fallback_price_gbp(h, day_quotes, day_fx_usd, day_fx_eur)
            day_val += sh * px_gbp
        day_pnl_pct = ((day_val - initial_cap) / initial_cap * 100.0) if initial_cap else 0.0
        bm_vals: dict[str, tuple[float, float]] = {}
        for sym, b in cfg.get("benchmarks", {}).items():
            init_sh = float(b["initial_shares"])
            px = float(day_quotes.get(sym, b["entry_price_gbp"]))
            b_val = init_sh * px
            b_ret = (b_val / initial_cap - 1) * 100.0 if initial_cap else 0.0
            bm_vals[sym] = (round(b_val, 2), round(b_ret, 2))
        return round(day_val, 2), round(day_pnl_pct, 2), bm_vals

    # 3. Calculate holdings current value and P&L
    holdings_out = []
    total_val = 0.0
    for h in cfg.get("holdings", []):
        t212_key = h.get("t212_ticker")
        ticker = h["ticker"]
        t212_match = t212_positions.get(t212_key)

        shares = float(h["shares"])
        cost_gbp = float(h["cost_gbp"])

        if t212_match and t212_match.get("quantity", 0) > 0:
            # Value the configured purchase quantity using the broker's GBP
            # per-share value. Additional purchases do not become trading gains.
            val_gbp = shares * t212_match["value_gbp"] / t212_match["quantity"]
            pnl_gbp = val_gbp - cost_gbp
        else:
            px_gbp = _fallback_price_gbp(h, quotes, fx_gbp_usd, fx_eur_gbp)
            val_gbp = shares * px_gbp
            pnl_gbp = val_gbp - cost_gbp

        pnl_pct = (pnl_gbp / cost_gbp * 100.0) if cost_gbp else 0.0
        total_val += val_gbp
        holdings_out.append({
            "leg": h["leg"],
            "code": h["code"],
            "ticker": ticker,
            "name": h["name"],
            "shares": shares,
            "cost_gbp": round(cost_gbp, 2),
            "current_value_gbp": round(val_gbp, 2),
            "pnl_gbp": round(pnl_gbp, 2),
            "pnl_pct": round(pnl_pct, 2),
        })

    total_pnl = total_val - initial_cap
    total_pnl_pct = (total_pnl / initial_cap * 100.0) if initial_cap else 0.0

    # 4. Calculate benchmarks
    benchmarks_out = []
    bm_map = {}
    for sym, b in cfg.get("benchmarks", {}).items():
        init_shares = float(b["initial_shares"])
        px = float(quotes.get(sym, b["entry_price_gbp"])) if hasattr(quotes, "get") else b["entry_price_gbp"]
        cur_val = init_shares * px
        ret_pct = (cur_val / initial_cap - 1) * 100.0
        alpha_pct = total_pnl_pct - ret_pct
        row = {
            "ticker": sym,
            "name": b["name"],
            "entry_price_gbp": float(b["entry_price_gbp"]),
            "current_price_gbp": round(px, 3),
            "current_value_gbp": round(cur_val, 2),
            "return_pct": round(ret_pct, 2),
            "alpha_pct": round(alpha_pct, 2),
        }
        benchmarks_out.append(row)
        bm_map[sym] = row

    # 5. Build forward testing benchmark chart time series
    saved_history = cfg.get("history", [])
    if saved_history:
        chart_dates = [h["label"] for h in saved_history] + ["Live"]
        s_lodestar_val = [h["lodestar"] for h in saved_history] + [round(total_val, 2)]
        s_lodestar_pct = [h["lodestar_pct"] for h in saved_history] + [round(total_pnl_pct, 2)]
        s_vuag_val = [h.get("vuag", initial_cap) for h in saved_history] + [bm_map.get("VUAG.L", {}).get("current_value_gbp", initial_cap)]
        s_vuag_pct = [h.get("vuag_pct", 0.0) for h in saved_history] + [bm_map.get("VUAG.L", {}).get("return_pct", 0.0)]
        s_vwrp_val = [h.get("vwrp", initial_cap) for h in saved_history] + [bm_map.get("VWRP.L", {}).get("current_value_gbp", initial_cap)]
        s_vwrp_pct = [h.get("vwrp_pct", 0.0) for h in saved_history] + [bm_map.get("VWRP.L", {}).get("return_pct", 0.0)]
        s_vukg_val = [h.get("vukg", initial_cap) for h in saved_history] + [bm_map.get("VUKG.L", {}).get("current_value_gbp", initial_cap)]
        s_vukg_pct = [h.get("vukg_pct", 0.0) for h in saved_history] + [bm_map.get("VUKG.L", {}).get("return_pct", 0.0)]
        s_vall_val = [h.get("vall", initial_cap) for h in saved_history] + [bm_map.get("VALL.L", {}).get("current_value_gbp", initial_cap)]
        s_vall_pct = [h.get("vall_pct", 0.0) for h in saved_history] + [bm_map.get("VALL.L", {}).get("return_pct", 0.0)]
    else:
        entry_dt = pd.Timestamp(entry_date_str).normalize()
        entry_label = f"{entry_dt.day} {entry_dt.strftime('%b')} (Fill)"
        chart_dates = [entry_label]
        s_lodestar_val = [initial_cap]
        s_lodestar_pct = [0.0]
        s_vuag_val = [initial_cap]
        s_vuag_pct = [0.0]
        s_vwrp_val = [initial_cap]
        s_vwrp_pct = [0.0]
        s_vukg_val = [initial_cap]
        s_vukg_pct = [0.0]
        s_vall_val = [initial_cap]
        s_vall_pct = [0.0]

        asset_dates: list[pd.Timestamp] = []
        if isinstance(quote_frame, pd.DataFrame) and not quote_frame.empty:
            present_assets = [c for c in asset_symbols if c in quote_frame.columns]
            if present_assets:
                for dt in quote_frame.index:
                    dt_norm = pd.Timestamp(dt).normalize()
                    if dt_norm <= entry_dt:
                        continue
                    row_vals = pd.to_numeric(quote_frame.loc[dt, present_assets], errors="coerce")
                    if row_vals.map(lambda v: pd.notna(v) and math.isfinite(float(v)) and float(v) > 0).any():
                        asset_dates.append(dt_norm)

        if asset_dates:
            ffilled = quote_frame.ffill()
            for dt in asset_dates[:-1]:
                row = ffilled.loc[dt]
                day_quotes = {
                    sym: float(row[sym])
                    for sym in all_symbols
                    if sym in row.index and pd.notna(row[sym]) and math.isfinite(float(row[sym])) and float(row[sym]) > 0
                }
                d_val, d_pct, d_bm = _snapshot_for(day_quotes)
                chart_dates.append(f"{dt.day} {dt.strftime('%b')}")
                s_lodestar_val.append(d_val)
                s_lodestar_pct.append(d_pct)
                s_vuag_val.append(d_bm.get("VUAG.L", (initial_cap, 0.0))[0])
                s_vuag_pct.append(d_bm.get("VUAG.L", (initial_cap, 0.0))[1])
                s_vwrp_val.append(d_bm.get("VWRP.L", (initial_cap, 0.0))[0])
                s_vwrp_pct.append(d_bm.get("VWRP.L", (initial_cap, 0.0))[1])
                s_vukg_val.append(d_bm.get("VUKG.L", (initial_cap, 0.0))[0])
                s_vukg_pct.append(d_bm.get("VUKG.L", (initial_cap, 0.0))[1])
                s_vall_val.append(d_bm.get("VALL.L", (initial_cap, 0.0))[0])
                s_vall_pct.append(d_bm.get("VALL.L", (initial_cap, 0.0))[1])
            last_dt = asset_dates[-1]
            live_label = f"{last_dt.day} {last_dt.strftime('%b')} (Live)"
        else:
            live_label = "Live"

        chart_dates.append(live_label)
        s_lodestar_val.append(round(total_val, 2))
        s_lodestar_pct.append(round(total_pnl_pct, 2))
        s_vuag_val.append(bm_map.get("VUAG.L", {}).get("current_value_gbp", initial_cap))
        s_vuag_pct.append(bm_map.get("VUAG.L", {}).get("return_pct", 0.0))
        s_vwrp_val.append(bm_map.get("VWRP.L", {}).get("current_value_gbp", initial_cap))
        s_vwrp_pct.append(bm_map.get("VWRP.L", {}).get("return_pct", 0.0))
        s_vukg_val.append(bm_map.get("VUKG.L", {}).get("current_value_gbp", initial_cap))
        s_vukg_pct.append(bm_map.get("VUKG.L", {}).get("return_pct", 0.0))
        s_vall_val.append(bm_map.get("VALL.L", {}).get("current_value_gbp", initial_cap))
        s_vall_pct.append(bm_map.get("VALL.L", {}).get("return_pct", 0.0))

    benchmark_chart = {
        "dates": chart_dates,
        "series": [
            {"name": "Lodestar (50/50)", "ticker": "Lodestar", "colour": "#f59e0b", "strokeWidth": 2.5, "values": s_lodestar_val, "pcts": s_lodestar_pct},
            {"name": "Vanguard S&P 500", "ticker": "VUAG.L", "colour": "#10b981", "strokeWidth": 1.5, "values": s_vuag_val, "pcts": s_vuag_pct},
            {"name": "Vanguard FTSE All-World", "ticker": "VWRP.L", "colour": "#3b82f6", "strokeWidth": 1.5, "values": s_vwrp_val, "pcts": s_vwrp_pct},
            {"name": "Vanguard FTSE 100", "ticker": "VUKG.L", "colour": "#f43f5e", "strokeWidth": 1.5, "values": s_vukg_val, "pcts": s_vukg_pct},
            {"name": "Vanguard Developed World", "ticker": "VALL.L", "colour": "#a855f7", "strokeWidth": 1.5, "values": s_vall_val, "pcts": s_vall_pct},
        ]
    }

    return {
        "sync_source": broker_status,
        "asof": quote_asof or datetime.now(timezone.utc).date().isoformat(),
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "entry_date": entry_date_str,
        "initial_capital_gbp": initial_cap,
        "current_value_gbp": round(total_val, 2),
        "total_pnl_gbp": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "holdings": holdings_out,
        "benchmarks": benchmarks_out,
        "benchmarkChart": benchmark_chart,
    }


if __name__ == "__main__":
    res = get_live_portfolio_data()
    print(json.dumps(res, indent=2))

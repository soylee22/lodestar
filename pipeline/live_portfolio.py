"""Live portfolio tracking for Lodestar.

Reads execution details from data/live_portfolio.json.
Syncs live valuation from Trading 212 API if credentials exist,
or falls back to yfinance market quotes and live exchange rates.
Compares performance directly against VUAG.L, VWRP.L, VUKG.L, and VALL.L.
"""
from __future__ import annotations
import base64
import json
import os
import urllib.request
from pathlib import Path
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


def get_live_portfolio_data() -> dict:
    cfg_path = DATA_DIR / "live_portfolio.json"
    if not cfg_path.exists():
        return {}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    initial_cap = float(cfg.get("initial_capital_gbp", 5248.14))

    # 1. Fetch Trading 212 API positions if available
    key, secret, base = load_t212_credentials()
    t212_positions = {}
    t212_cash = {}
    if key and secret:
        try:
            auth = "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()
            req = urllib.request.Request(f"{base}/api/v0/equity/portfolio", headers={"Authorization": auth})
            with urllib.request.urlopen(req, timeout=10) as r:
                for p in json.load(r):
                    t212_positions[p.get("ticker")] = p
            req2 = urllib.request.Request(f"{base}/api/v0/equity/account/cash", headers={"Authorization": auth})
            with urllib.request.urlopen(req2, timeout=10) as r:
                t212_cash = json.load(r)
        except Exception:
            pass

    # 2. Fetch live quotes via yfinance for fallback and benchmarks
    bm_symbols = list(cfg.get("benchmarks", {}).keys())
    holding_symbols = [h["ticker"] for h in cfg.get("holdings", [])]
    all_symbols = bm_symbols + holding_symbols + ["GBPUSD=X"]
    try:
        quotes = yf.download(all_symbols, period="2d", progress=False, auto_adjust=False)["Close"].iloc[-1]
    except Exception:
        quotes = {}

    fx_gbp_usd = float(quotes.get("GBPUSD=X", 1.3265)) if hasattr(quotes, "get") else 1.3265

    # 3. Calculate holdings current value and P&L
    holdings_out = []
    total_val = 0.0
    for h in cfg.get("holdings", []):
        t212_key = h.get("t212_ticker")
        ticker = h["ticker"]
        t212_match = t212_positions.get(t212_key)

        shares = float(h["shares"])
        cost_gbp = float(h["cost_gbp"])

        if t212_match and "currentPrice" in t212_match:
            px_usd = float(t212_match["currentPrice"])
            val_gbp = (shares * px_usd) / fx_gbp_usd
            # Use direct T212 PnL if available
            pnl_gbp = float(t212_match.get("ppl", val_gbp - cost_gbp))
            val_gbp = cost_gbp + pnl_gbp
        else:
            px_raw = float(quotes.get(ticker, h["fill_price_usd"])) if hasattr(quotes, "get") else h["fill_price_usd"]
            # IUIT.L and IUVL.L trade in USD on LSE
            val_gbp = (shares * px_raw) / fx_gbp_usd
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
    for sym, b in cfg.get("benchmarks", {}).items():
        init_shares = float(b["initial_shares"])
        px = float(quotes.get(sym, b["entry_price_gbp"])) if hasattr(quotes, "get") else b["entry_price_gbp"]
        cur_val = init_shares * px
        ret_pct = (cur_val / initial_cap - 1) * 100.0
        alpha_pct = total_pnl_pct - ret_pct
        benchmarks_out.append({
            "ticker": sym,
            "name": b["name"],
            "entry_price_gbp": float(b["entry_price_gbp"]),
            "current_price_gbp": round(px, 3),
            "current_value_gbp": round(cur_val, 2),
            "return_pct": round(ret_pct, 2),
            "alpha_pct": round(alpha_pct, 2),
        })

    return {
        "entry_date": cfg.get("entry_date", "2026-11-12"),
        "initial_capital_gbp": initial_cap,
        "current_value_gbp": round(total_val, 2),
        "total_pnl_gbp": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "holdings": holdings_out,
        "benchmarks": benchmarks_out,
    }


if __name__ == "__main__":
    res = get_live_portfolio_data()
    print(json.dumps(res, indent=2))

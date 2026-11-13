"""Lodestar Emerging Next-Month Target Allocation & Shared Signal Engine.

Evaluates 8-month momentum across all 15 indices (8 factors and 7 sectors)
on intra-month prices, checks the 19-month crash floor, and produces one
canonical provisional target snapshot shared by the website, Telegram, and
HTML email alerts.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timezone
import json
from pathlib import Path
import time

import pandas as pd
import yfinance as yf

from universe import (
    FACTOR_NAMES,
    FACTOR_PROXY,
    LSE,
    NAMES,
    SECTOR_NAMES,
    SECTOR_PROXY,
    SECTOR_YF,
    held_now,
)
from pipeline.crash_protection import get_crash_protection_data

HERE = Path(__file__).resolve().parent.parent
LOOKBACK = 8


def _retry(fn, n: int = 4, wait: float = 5.0):
    last = None
    for attempt in range(n):
        try:
            out = fn()
            if out is not None and len(out):
                return out
            last = "empty response"
        except Exception as e:
            last = e
        if attempt + 1 < n:
            time.sleep(wait)
    raise RuntimeError(f"failed after {n} attempts: {last}")


def series(
    sym: str,
    start: str,
    need: pd.Period | None = None,
    adjusted: bool = False,
    retries: int = 4,
) -> pd.Series:
    """One symbol's daily closes, fetched alone and with retries.

    Yahoo throttles runner addresses and answers a throttled request with a
    short series rather than an error, so a response that does not reach back
    to `need` counts as a failure and is retried.
    """
    def go():
        d = yf.Ticker(sym).history(start=start, interval="1d", auto_adjust=adjusted)["Close"]
        if d.empty:
            return None
        d.index = pd.to_datetime(d.index).tz_localize(None)
        if need is not None and d.index.min().to_period("M") > need:
            print(f"  .. {sym} came back short (from {d.index.min():%Y-%m-%d}); retrying")
            return None
        return d

    return _retry(go, n=retries)


def closes(
    mapping: dict[str, str],
    start: str,
    need: pd.Period | None = None,
    adjusted: bool = False,
    retries: int = 4,
    stale_days: int = 5,
) -> tuple[pd.DataFrame, list[str]]:
    """Frame of daily closes keyed by slot, plus slots that would not load."""
    out: dict[str, pd.Series] = {}
    missing: list[str] = []
    for slot, sym in mapping.items():
        try:
            out[slot] = series(sym, start, need, adjusted, retries)
        except Exception as e:
            missing.append(slot)
            print(f"  !! {slot} ({sym}) unavailable: {e}")
    if not out:
        return pd.DataFrame(), missing
    px = pd.DataFrame(out)
    last = {c: px[c].last_valid_index() for c in px.columns}
    asof = px.index.max()
    stale = [c for c in px.columns if (asof - last[c]).days > stale_days]
    for c in stale:
        print(
            f"  !! {c} ({mapping[c]}) stops at {last[c]:%Y-%m-%d}, "
            f"{asof:%Y-%m-%d} elsewhere: dropped as stale"
        )
    return px.drop(columns=stale).ffill(), missing + stale


def month_end_level(px: pd.DataFrame, period: pd.Period) -> pd.Series:
    """Closes at the end of one completed month. Empty if nothing reaches back."""
    if px.empty:
        return pd.Series(dtype=float)
    m = px.resample("ME").last()
    m.index = m.index.to_period("M")
    if period not in m.index:
        return pd.Series(dtype=float)
    return m.loc[period].dropna()


def momentum(
    px: pd.DataFrame,
    base: pd.Period,
    missing: list[str],
) -> tuple[pd.Series, list[str]]:
    """Price return from the close of `base` to the latest close, per slot."""
    lvl = month_end_level(px, base)
    if lvl.empty:
        return pd.Series(dtype=float), missing + list(px.columns)
    mom = (px.iloc[-1][lvl.index] / lvl - 1.0).dropna()
    return mom, missing + [c for c in px.columns if c not in mom.index]


def ranked(mom: pd.Series) -> list[tuple[str, float]]:
    return sorted(((str(k), float(v)) for k, v in mom.items()), key=lambda kv: -kv[1])


def leg_margin_sentence(mom: pd.Series, held: str) -> str | None:
    """Explain the return needed or cushion available against #1 / #2."""
    if held == "CASH" or held not in mom.index or len(mom) < 2:
        return None
    (lead, lv), (second, sv) = ranked(mom)[:2]
    if lead == held:
        give = (1.0 - (1.0 + sv) / (1.0 + lv)) * 100.0
        return (
            f"{NAMES[held]} holds the slot unless it gives back "
            f"{give:.1f}% against {NAMES[second]} this month."
        )
    need = ((1.0 + lv) / (1.0 + float(mom[held])) - 1.0) * 100.0
    return (
        f"{NAMES[held]} keeps the slot only if it beats {NAMES[lead]} by "
        f"{need:.1f}% over the rest of the month."
    )


def _format_leg(
    mom: pd.Series,
    proxy_map: dict[str, str],
    names: dict[str, str],
    held_slot: str,
) -> tuple[list[dict], dict]:
    ordered = ranked(mom)
    if not ordered:
        return [], {}

    rows = []
    for code, ret in ordered:
        rows.append(
            {
                "code": code,
                "name": names.get(code, code),
                "ticker": LSE.get(code, proxy_map.get(code, code)),
                "proxy": proxy_map.get(code, code),
                "momentum_pct": round(ret * 100.0, 2),
                "is_held": code == held_slot,
            }
        )

    top_mom = rows[0]["momentum_pct"]
    sec_mom = rows[1]["momentum_pct"] if len(rows) > 1 else rows[0]["momentum_pct"]
    leader = rows[0]
    runner_up = rows[1] if len(rows) > 1 else rows[0]
    gap_pp = round(top_mom - sec_mom, 2)

    lv_frac = ordered[0][1]
    sv_frac = ordered[1][1] if len(ordered) > 1 else ordered[0][1]
    giveback = (1.0 - (1.0 + sv_frac) / (1.0 + lv_frac)) * 100.0 if (1.0 + lv_frac) > 0 else 0.0

    held_need = None
    if held_slot in mom.index and held_slot != leader["code"] and (1.0 + float(mom[held_slot])) > 0:
        held_need = round(((1.0 + lv_frac) / (1.0 + float(mom[held_slot])) - 1.0) * 100.0, 1)

    for idx, r in enumerate(rows):
        r["rank"] = idx + 1
        if idx == 0:
            r["lead_margin"] = f"+{gap_pp:.1f}pp"
        else:
            diff = round(r["momentum_pct"] - top_mom, 1)
            r["lead_margin"] = f"{diff:+.1f}pp"

    lead_summary = {
        "leader_code": leader["code"],
        "leader_name": leader["name"],
        "leader_ticker": leader["ticker"],
        "momentum_pct": leader["momentum_pct"],
        "is_held": leader["is_held"],
        "runner_up_code": runner_up["code"],
        "runner_up_name": runner_up["name"],
        "runner_up_ticker": runner_up["ticker"],
        "runner_up_momentum_pct": runner_up["momentum_pct"],
        "gap_pp": gap_pp,
        "giveback_pct": round(giveback, 1),
        "held_code": held_slot,
        "held_name": NAMES.get(held_slot, held_slot),
        "held_need_pct": held_need,
        "margin_text": leg_margin_sentence(mom, held_slot),
    }
    return rows, lead_summary


def compute_emerging_data(
    today: date | None = None,
    root: Path | None = None,
    crash_data: dict | None = None,
    use_cache: bool = True,
    factor_px: pd.DataFrame | None = None,
    sector_px: pd.DataFrame | None = None,
    sector_is_proxy: bool | None = None,
) -> dict:
    ref_day = today if today is not None else date.today()
    base_dir = Path(root) if root is not None else HERE
    now = pd.Timestamp(ref_day).to_period("M")
    base = now - LOOKBACK
    start = (pd.Timestamp(ref_day) - pd.DateOffset(months=12)).date().isoformat()
    last_day = monthrange(ref_day.year, ref_day.month)[1]
    signal_date = date(ref_day.year + (ref_day.month == 12), ref_day.month % 12 + 1, 1)
    days_out = (signal_date - ref_day).days
    cache_file = base_dir / "data" / "emerging.json"

    f_missing: list[str] = []
    s_missing: list[str] = []

    if factor_px is None:
        factor_px, f_missing = closes(FACTOR_PROXY, start, need=base, retries=4)
    fmom, f_missing = momentum(factor_px, base, f_missing)

    used_sector_proxy = bool(sector_is_proxy) if sector_is_proxy is not None else False
    s_source_map = SECTOR_PROXY if used_sector_proxy else SECTOR_YF

    if sector_px is None:
        sector_px, s_missing = closes(SECTOR_YF, start, need=base, retries=1)
        smom, s_missing = momentum(sector_px, base, s_missing)
        if s_missing:
            print(
                f"  .. sector index feed incomplete ({', '.join(sorted(set(s_missing)))}); "
                "recomputing the leg from the sector ETFs"
            )
            px2, miss2 = closes(SECTOR_PROXY, start, need=base, retries=4)
            smom2, miss2 = momentum(px2, base, miss2)
            if len(smom2) >= len(smom):
                sector_px, smom, s_missing = px2, smom2, miss2
                used_sector_proxy = True
                s_source_map = SECTOR_PROXY
    else:
        smom, s_missing = momentum(sector_px, base, s_missing)

    hf, hs, hmonth = held_now(base_dir, ref_day)
    held_cash = hf == "CASH" or hs == "CASH"

    if not len(fmom) and not len(smom):
        if use_cache and cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                cached["days_out"] = days_out
                cached["days_run"] = f"{ref_day.day} of {last_day} days"
                cached["stale_cache"] = True
                return cached
            except Exception:
                pass
        return {}

    if crash_data is None:
        crash_data = get_crash_protection_data(today=ref_day)
    cash_triggered = bool(crash_data.get("cash_triggered", False)) if crash_data else False

    factors_ranked, f_lead = _format_leg(fmom, FACTOR_PROXY, FACTOR_NAMES, hf)
    sectors_ranked, s_lead = _format_leg(smom, s_source_map, SECTOR_NAMES, hs)

    f_pick = f_lead.get("leader_code")
    s_pick = s_lead.get("leader_code")

    f_asof = factor_px.index.max().date().isoformat() if not factor_px.empty else None
    s_asof = sector_px.index.max().date().isoformat() if not sector_px.empty else None
    valid_dates = [d for d in (f_asof, s_asof) if d]
    asof_date = max(valid_dates) if valid_dates else ref_day.isoformat()

    if held_cash:
        current_holdings = [
            {"leg": "Cash", "code": "CASH", "name": "Cash (100%)", "ticker": "CASH"},
        ]
    else:
        current_holdings = [
            {"leg": "Factor", "code": hf, "name": FACTOR_NAMES.get(hf, hf), "ticker": LSE.get(hf, hf)},
            {"leg": "Sector", "code": hs, "name": SECTOR_NAMES.get(hs, hs), "ticker": LSE.get(hs, hs)},
        ]

    changes: list[dict] = []
    if cash_triggered:
        action = "CASH"
        if held_cash:
            action_text = "HOLD CASH · CRASH GATE ACTIVE"
            action_summary = (
                "S&P 500 stays more than 25% below its close 19 months earlier. "
                "Both legs remain in 100% cash if month closed today."
            )
        else:
            action_text = "CRASH GATE · MOVE TO CASH"
            action_summary = (
                "S&P 500 is more than 25% below its close 19 months earlier. "
                "Target shifts to 100% cash if month closed today."
            )
            changes = [
                {
                    "leg": "Book",
                    "from_code": f"{hf}+{hs}",
                    "from_name": f"{NAMES.get(hf, hf)} + {NAMES.get(hs, hs)}",
                    "to_code": "CASH",
                    "to_name": "Cash (100%)",
                    "to_ticker": "CASH",
                }
            ]
        action_badge_cls = "fail"
        projected_holdings = [
            {
                "leg": "Cash",
                "code": "CASH",
                "name": "Cash (100%)",
                "ticker": "CASH",
                "weight_pct": 100.0,
                "status": "KEEP" if held_cash else "NEW BUY",
            }
        ]
    else:
        f_change = bool(f_pick and f_pick != hf)
        s_change = bool(s_pick and s_pick != hs)
        needs_trade = f_change or s_change or held_cash

        if f_change and f_pick:
            changes.append(
                {
                    "leg": "factor",
                    "from_code": hf,
                    "from_name": NAMES.get(hf, hf),
                    "to_code": f_pick,
                    "to_name": NAMES.get(f_pick, f_pick),
                    "to_ticker": LSE.get(f_pick, f_pick),
                }
            )
        if s_change and s_pick:
            changes.append(
                {
                    "leg": "sector",
                    "from_code": hs,
                    "from_name": NAMES.get(hs, hs),
                    "to_code": s_pick,
                    "to_name": NAMES.get(s_pick, s_pick),
                    "to_ticker": LSE.get(s_pick, s_pick),
                }
            )

        action = "ROTATE" if needs_trade else "HOLD"
        action_text = "ROTATE · REBALANCE PROJECTED" if needs_trade else "HOLD · ZERO TURNOVER"
        action_badge_cls = "fail" if needs_trade else "pass"
        if needs_trade and changes:
            parts = [f"{c['leg'].capitalize()}: {c['from_name']} to {c['to_name']}" for c in changes]
            action_summary = ", ".join(parts) + " if month closed on latest prices."
        elif needs_trade:
            action_summary = "Rebalance would trade new positions if month closed today."
        else:
            action_summary = "Both provisional leaders match currently held positions. Zero turnover projected if month closed today."

        eff_f = f_pick or hf
        eff_s = s_pick or hs
        projected_holdings = [
            {
                "leg": "Factor",
                "code": eff_f,
                "name": FACTOR_NAMES.get(eff_f, eff_f),
                "ticker": LSE.get(eff_f, eff_f),
                "weight_pct": 50.0,
                "status": "KEEP" if eff_f == hf else "NEW BUY",
            },
            {
                "leg": "Sector",
                "code": eff_s,
                "name": SECTOR_NAMES.get(eff_s, eff_s),
                "ticker": LSE.get(eff_s, eff_s),
                "weight_pct": 50.0,
                "status": "KEEP" if eff_s == hs else "NEW BUY",
            },
        ]

    out = {
        "asof": asof_date,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "factor_asof": f_asof,
        "sector_asof": s_asof,
        "month": str(now),
        "holding_month": hmonth,
        "held_factor": hf,
        "held_sector": hs,
        "held_cash": held_cash,
        "days_out": days_out,
        "next_action_date": signal_date.strftime("%-d %b %Y"),
        "days_run": f"{ref_day.day} of {last_day} days",
        "cash_triggered": cash_triggered,
        "action": action,
        "action_text": action_text,
        "action_badge_cls": action_badge_cls,
        "action_summary": action_summary,
        "changes": changes,
        "current_holdings": current_holdings,
        "projected_holdings": projected_holdings,
        "factor_lead": f_lead,
        "sector_lead": s_lead,
        "factors": factors_ranked,
        "sectors": sectors_ranked,
        "factor_missing": sorted(set(f_missing)),
        "sector_missing": sorted(set(s_missing)),
        "sector_proxy": used_sector_proxy,
        "stale_cache": False,
    }

    if use_cache:
        try:
            cache_file.write_text(json.dumps(out, indent=2))
        except Exception:
            pass

    return out


if __name__ == "__main__":
    data = compute_emerging_data()
    print("Action:", data.get("action_text"))
    print("Asof:", data.get("asof"), "Computed:", data.get("computed_at"))
    print("Factor Leader:", data.get("factor_lead"))
    print("Sector Leader:", data.get("sector_lead"))

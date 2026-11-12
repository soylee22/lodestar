"""Lodestar Emerging Next-Month Target Allocation Monitor.

Evaluates 8-month momentum across all 15 indices (8 factors and 7 sectors)
plus the TIP 1-3-6-12 canary on intra-month prices to project next month's
target allocation if the month closed today.
"""
from __future__ import annotations
import json
import sys
from calendar import monthrange
from datetime import date
from pathlib import Path
import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from pipeline.crash_protection import get_crash_protection_data
from universe import DEFENSIVE_NAMES, DEFENSIVE_TRADABLE, FACTOR_NAMES, SECTOR_NAMES, TRADABLE

LSE = {k: v[2] for k, v in TRADABLE.items()}

FACTOR_PROXY = {
    "USA_VAL": "VLUE",
    "USA_QUAL": "QUAL",
    "USA_MOM": "MTUM",
    "USA_SMALL": "SMLF",
    "EUR_VAL": "IVLU",
    "EUR_QUAL": "IQLT",
    "EUR_MOM": "IMTM",
    "EUR_SMALL": "IEUS",
}

SECTOR_PROXY = {
    "INFOTECH": "XLK",
    "HEALTH": "XLV",
    "CONS_STAP": "XLP",
    "CONS_DISC": "XLY",
    "INDUST": "XLI",
    "ENERGY": "XLE",
    "MATERIALS": "XLB",
}


def _current_holding(now: pd.Period) -> dict:
    """Return the latest active monthly signal driving currently held ETFs."""
    default = {"factor": "USA_VAL", "sector": "INFOTECH", "cash": False,
               "defensive": None, "defensive_ticker": None}
    hist_path = HERE / "data" / "signal_history.json"
    if not hist_path.exists():
        return default
    try:
        history = json.loads(hist_path.read_text())
    except Exception:
        return default
    if not isinstance(history, dict) or not history:
        return default
    settled = sorted(m for m in history if m <= str(now - 1))
    key = settled[-1] if settled else sorted(history)[-1]
    row = history.get(key) or {}
    return {
        "factor": row.get("factor", default["factor"]),
        "sector": row.get("sector", default["sector"]),
        "cash": bool(row.get("cash", False)),
        "defensive": row.get("defensive"),
        "defensive_ticker": row.get("defensive_ticker"),
    }


def compute_emerging_data() -> dict:
    today = date.today()
    now = pd.Timestamp(today).to_period("M")
    base = now - 8
    start = (pd.Timestamp(today) - pd.DateOffset(months=12)).date().isoformat()
    last_day = monthrange(today.year, today.month)[1]
    signal_date = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    days_out = (signal_date - today).days

    cache_file = HERE / "data" / "emerging.json"

    def _calc_leg(mapping: dict, names: dict, held_slot: str) -> tuple[list[dict], dict]:
        syms = list(mapping.values())
        try:
            df = yf.download(syms, start=start, interval="1d", progress=False, auto_adjust=False)["Close"]
            m = df.resample("ME").last()
            m.index = m.index.to_period("M")
            if base not in m.index:
                raise ValueError("base month not in index")
            base_row = m.loc[base]
            latest_row = df.iloc[-1]
            mom = (latest_row / base_row - 1.0).dropna()
        except Exception:
            # If live download fails, return fallback from cache if exists
            return [], {}

        # Sort descending
        res = []
        for slot, sym in mapping.items():
            if sym in mom.index:
                val = float(mom[sym]) * 100.0
                res.append({
                    "code": slot,
                    "name": names.get(slot, slot),
                    "ticker": LSE.get(slot, sym),
                    "proxy": sym,
                    "momentum_pct": round(val, 2),
                    "is_held": (slot == held_slot),
                })
        res.sort(key=lambda x: -x["momentum_pct"])

        # Add ranks and margins
        top_mom = res[0]["momentum_pct"] if res else 0.0
        sec_mom = res[1]["momentum_pct"] if len(res) > 1 else 0.0
        lead_summary = {}

        if res:
            leader = res[0]
            runner_up = res[1] if len(res) > 1 else res[0]
            gap_pp = round(top_mom - sec_mom, 2)
            # Giveback calculation: 1 - (1 + sv) / (1 + lv)
            lv_frac = top_mom / 100.0
            sv_frac = sec_mom / 100.0
            giveback = (1.0 - (1.0 + sv_frac) / (1.0 + lv_frac)) * 100.0 if (1.0 + lv_frac) > 0 else 0.0

            lead_summary = {
                "leader_code": leader["code"],
                "leader_name": leader["name"],
                "leader_ticker": leader["ticker"],
                "momentum_pct": leader["momentum_pct"],
                "is_held": leader["is_held"],
                "runner_up_name": runner_up["name"],
                "runner_up_ticker": runner_up["ticker"],
                "runner_up_momentum_pct": runner_up["momentum_pct"],
                "gap_pp": gap_pp,
                "giveback_pct": round(giveback, 1),
            }

            for idx, r in enumerate(res):
                r["rank"] = idx + 1
                if idx == 0:
                    r["lead_margin"] = f"+{gap_pp:.1f}pp"
                else:
                    diff = round(r["momentum_pct"] - top_mom, 1)
                    r["lead_margin"] = f"{diff:+.1f}pp"

        return res, lead_summary

    held = _current_holding(now)
    held_cash = bool(held.get("cash"))
    held_f = held.get("factor", "USA_VAL")
    held_s = held.get("sector", "INFOTECH")
    held_def = held.get("defensive")

    factors_ranked, f_lead = _calc_leg(FACTOR_PROXY, FACTOR_NAMES, "" if held_cash else held_f)
    sectors_ranked, s_lead = _calc_leg(SECTOR_PROXY, SECTOR_NAMES, "" if held_cash else held_s)

    try:
        crash_data = get_crash_protection_data()
    except Exception:
        crash_data = None

    # If yfinance failed to download, try loading cached file
    if not factors_ranked or not sectors_ranked:
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                cached["days_out"] = days_out
                return cached
            except Exception:
                pass

    canary_triggered = bool(crash_data and crash_data.get("cash_triggered"))
    def_slot = crash_data.get("defensive_slot") if crash_data else None
    def_ticker = crash_data.get("defensive_ticker") if crash_data else None
    def_name = crash_data.get("defensive_name") if crash_data else None
    def_isin = crash_data.get("defensive_isin") if crash_data else None
    tip_pct = crash_data.get("tip_score_pct") if crash_data else None

    if held_cash:
        held_def_slot = held_def or "DEF_BIL"
        held_def_ticker = held.get("defensive_ticker") or DEFENSIVE_TRADABLE.get(held_def_slot, ("", "", "IB01.L", ""))[2]
        held_def_name = DEFENSIVE_NAMES.get(held_def_slot, held_def_slot)
        current_holdings = [
            {"leg": "Defensive", "code": held_def_slot, "name": held_def_name, "ticker": held_def_ticker},
        ]
    else:
        current_holdings = [
            {"leg": "Factor", "code": held_f, "name": FACTOR_NAMES.get(held_f, held_f), "ticker": LSE.get(held_f, held_f)},
            {"leg": "Sector", "code": held_s, "name": SECTOR_NAMES.get(held_s, held_s), "ticker": LSE.get(held_s, held_s)},
        ]

    if canary_triggered and def_slot:
        same_def = held_cash and (held_def == def_slot)
        action = "HOLD" if same_def else "DEFENSIVE"
        action_text = "DEFENSIVE HOLD · TIP <= 0%" if same_def else "DEFENSIVE · TIP CANARY TRIGGERED"
        action_badge_cls = "fail"
        score_str = f"{tip_pct:+.2f}%" if tip_pct is not None else "<= 0%"
        if same_def:
            action_summary = f"TIP 1-3-6-12 sits at {score_str} (<= 0%). Hold 100% in {def_name} ({def_ticker}) if month closed today."
        else:
            action_summary = f"TIP 1-3-6-12 sits at {score_str} (<= 0%). Rotate 100% into {def_name} ({def_ticker}) if month closed today."
        projected_holdings = [
            {
                "leg": "Defensive",
                "code": def_slot,
                "name": def_name,
                "ticker": def_ticker,
                "isin": def_isin,
                "weight_pct": 100.0,
                "status": "KEEP" if same_def else "NEW BUY",
            }
        ]
    else:
        f_code = f_lead.get("leader_code", held_f)
        s_code = s_lead.get("leader_code", held_s)
        f_change = held_cash or (f_code != held_f)
        s_change = held_cash or (s_code != held_s)
        needs_trade = f_change or s_change

        action = "ROTATE" if needs_trade else "HOLD"
        if held_cash:
            action_text = "RE-ENTER EQUITIES · TIP > 0%"
            action_summary = "TIP 1-3-6-12 sits above 0%. Re-enter 50/50 equities if month closed today."
        else:
            action_text = "ROTATE · REBALANCE PROJECTED" if needs_trade else "HOLD · ZERO TURNOVER"
            action_summary = (
                "Rebalance would trade new positions if month closed today."
                if needs_trade
                else "Both provisional leaders match currently held positions. Zero turnover projected if month closed today."
            )
        action_badge_cls = "fail" if needs_trade else "pass"
        projected_holdings = [
            {
                "leg": "Factor",
                "code": f_code,
                "name": f_lead.get("leader_name", FACTOR_NAMES.get(held_f, held_f)),
                "ticker": f_lead.get("leader_ticker", LSE.get(held_f, held_f)),
                "weight_pct": 50.0,
                "status": "KEEP" if not f_change else "NEW BUY",
            },
            {
                "leg": "Sector",
                "code": s_code,
                "name": s_lead.get("leader_name", SECTOR_NAMES.get(held_s, held_s)),
                "ticker": s_lead.get("leader_ticker", LSE.get(held_s, held_s)),
                "weight_pct": 50.0,
                "status": "KEEP" if not s_change else "NEW BUY",
            },
        ]

    canary_info = {
        "triggered": canary_triggered,
        "tip_score_pct": tip_pct,
        "ief_score_pct": crash_data.get("ief_score_pct") if crash_data else None,
        "bil_score_pct": crash_data.get("bil_score_pct") if crash_data else None,
        "defensive_slot": def_slot,
        "defensive_ticker": def_ticker,
        "defensive_name": def_name,
        "defensive_isin": def_isin,
        "asof": crash_data.get("asof") if crash_data else None,
    }

    out = {
        "asof": today.isoformat(),
        "month": str(now),
        "days_out": days_out,
        "next_action_date": signal_date.strftime("%-d %b %Y"),
        "days_run": f"{today.day} of {last_day} days",
        "action": action,
        "action_text": action_text,
        "action_badge_cls": action_badge_cls,
        "action_summary": action_summary,
        "canary": canary_info,
        "current_holdings": current_holdings,
        "projected_holdings": projected_holdings,
        "factor_lead": f_lead,
        "sector_lead": s_lead,
        "factors": factors_ranked,
        "sectors": sectors_ranked,
    }

    # Save cache
    try:
        cache_file.write_text(json.dumps(out, indent=2))
    except Exception:
        pass

    return out


if __name__ == "__main__":
    data = compute_emerging_data()
    print("Action:", data["action_text"])
    print("Factor Leader:", data["factor_lead"])
    print("Sector Leader:", data["sector_lead"])

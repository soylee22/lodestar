"""Lodestar Emerging Next-Month Target Allocation Monitor.

Evaluates 8-month momentum across all 15 indices (8 factors and 7 sectors)
on intra-month prices to project next month's target allocation if the month closed today.
"""
from __future__ import annotations
import json
from calendar import monthrange
from datetime import date
from pathlib import Path
import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent.parent

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

LSE = {
    "USA_MOM": "IUMO.L",
    "USA_QUAL": "IUQA.L",
    "USA_VAL": "IUVL.L",
    "USA_SMALL": "CUSS.L",
    "EUR_MOM": "IEFM.L",
    "EUR_QUAL": "IEQU.L",
    "EUR_VAL": "IEVL.L",
    "EUR_SMALL": "XXSC.L",
    "CONS_DISC": "IUCD.L",
    "CONS_STAP": "IUCS.L",
    "ENERGY": "IUES.L",
    "HEALTH": "IHCU.L",
    "INDUST": "IUIS.L",
    "INFOTECH": "IUIT.L",
    "MATERIALS": "IUMS.L",
}

FACTOR_NAMES = {
    "USA_MOM": "USA Momentum",
    "USA_QUAL": "USA Quality",
    "USA_VAL": "USA Value",
    "USA_SMALL": "USA Small Cap",
    "EUR_MOM": "Europe Momentum",
    "EUR_QUAL": "Europe Quality",
    "EUR_VAL": "Europe Value",
    "EUR_SMALL": "Europe Small Cap",
}

SECTOR_NAMES = {
    "CONS_DISC": "Consumer Discretionary",
    "CONS_STAP": "Consumer Staples",
    "ENERGY": "Energy",
    "HEALTH": "Health Care",
    "INDUST": "Industrials",
    "INFOTECH": "Information Technology",
    "MATERIALS": "Materials",
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

    # Current held positions (November 2026 targets)
    held_f = "USA_VAL"
    held_s = "INFOTECH"

    factors_ranked, f_lead = _calc_leg(FACTOR_PROXY, FACTOR_NAMES, held_f)
    sectors_ranked, s_lead = _calc_leg(SECTOR_PROXY, SECTOR_NAMES, held_s)

    # If yfinance failed to download, try loading cached file
    if not factors_ranked or not sectors_ranked:
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                cached["days_out"] = days_out
                return cached
            except Exception:
                pass

    # Action determination
    f_change = f_lead.get("leader_code") != held_f
    s_change = s_lead.get("leader_code") != held_s
    needs_trade = f_change or s_change

    action = "ROTATE" if needs_trade else "HOLD"
    action_text = "ROTATE · REBALANCE PROJECTED" if needs_trade else "HOLD · ZERO TURNOVER"
    action_badge_cls = "fail" if needs_trade else "pass"
    action_summary = (
        "Rebalance would trade new positions if month closed today."
        if needs_trade
        else "Both provisional leaders match currently held positions. Zero turnover projected if month closed today."
    )

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
        "current_holdings": [
            {"leg": "Factor", "code": held_f, "name": FACTOR_NAMES[held_f], "ticker": LSE[held_f]},
            {"leg": "Sector", "code": held_s, "name": SECTOR_NAMES[held_s], "ticker": LSE[held_s]},
        ],
        "projected_holdings": [
            {
                "leg": "Factor",
                "code": f_lead.get("leader_code", held_f),
                "name": f_lead.get("leader_name", FACTOR_NAMES[held_f]),
                "ticker": f_lead.get("leader_ticker", LSE[held_f]),
                "weight_pct": 50.0,
                "status": "KEEP" if not f_change else "NEW BUY",
            },
            {
                "leg": "Sector",
                "code": s_lead.get("leader_code", held_s),
                "name": s_lead.get("leader_name", SECTOR_NAMES[held_s]),
                "ticker": s_lead.get("leader_ticker", LSE[held_s]),
                "weight_pct": 50.0,
                "status": "KEEP" if not s_change else "NEW BUY",
            },
        ],
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

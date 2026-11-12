"""S&P 500 Crash Protection & Ketchup Rule Monitor for Lodestar.

Calculates the trailing 24 monthly closes of the S&P 500, evaluates the
four risk checks (1-month, 6-month, 12-month momentum and the 19-month -25% floor),
and determines the master cash gate status.
"""
from __future__ import annotations
import pandas as pd
import yfinance as yf


def get_crash_protection_data() -> dict:
    try:
        spx = yf.Ticker("^GSPC").history(period="3y", interval="1mo", auto_adjust=False)["Close"]
        spx.index = pd.to_datetime(spx.index).tz_localize(None).to_period("M")
        spx = spx.dropna()
    except Exception:
        return {}

    if len(spx) < 20:
        return {}

    last_24 = spx.iloc[-24:]
    last_val = float(spx.iloc[-1])
    p_1m = float(spx.iloc[-2])
    p_6m = float(spx.iloc[-7])
    p_12m = float(spx.iloc[-13])
    p_19m = float(spx.iloc[-20])

    m1_pct = round((last_val / p_1m - 1) * 100.0, 2)
    m6_pct = round((last_val / p_6m - 1) * 100.0, 2)
    m12_pct = round((last_val / p_12m - 1) * 100.0, 2)
    m19_pct = round((last_val / p_19m - 1) * 100.0, 2)

    # MarketFighter Ketchup rule requires all 3 momentum checks to fail simultaneously
    checks = [
        {
            "id": "m1",
            "name": "1-Month Momentum",
            "desc": "Latest close vs previous month",
            "reading_pct": m1_pct,
            "pass": m1_pct >= 0,
            "rule": "Research Check 1",
        },
        {
            "id": "m6",
            "name": "6-Month Momentum",
            "desc": "Latest close vs 6 months earlier",
            "reading_pct": m6_pct,
            "pass": m6_pct >= 0,
            "rule": "Research Check 2",
        },
        {
            "id": "m12",
            "name": "12-Month Momentum",
            "desc": "Latest close vs 12 months earlier",
            "reading_pct": m12_pct,
            "pass": m12_pct >= 0,
            "rule": "Research Check 3",
        },
        {
            "id": "floor19",
            "name": "19-Month Crash Floor",
            "desc": "Return vs 19 months ago (threshold: -25%)",
            "reading_pct": m19_pct,
            "pass": m19_pct >= -25.0,
            "rule": "Lodestar Circuit Breaker",
        },
    ]

    # Cash only fires if all 3 MarketFighter checks fail OR Lodestar's floor breaks
    ketchup_fails = sum(1 for c in checks[:3] if not c["pass"])
    cash_triggered = m19_pct < -25.0

    chart_points = [{"month": str(idx), "price": round(float(v), 2)} for idx, v in last_24.items()]
    return {
        "asof": str(spx.index[-1]),
        "latest_price": round(last_val, 2),
        "history": chart_points,
        "chart": chart_points,
        "checks": checks,
        "ketchup_fails": ketchup_fails,
        "research_cash_triggered": ketchup_fails == 3,
        "method_note": "The three research checks do not recover the proprietary MarketFighter rule. The production gate uses the 19-month floor.",
        "cash_triggered": cash_triggered,
        "status": "CASH" if cash_triggered else "INVESTED",
        "status_text": "Circuit Breaker Active · 100% Cash" if cash_triggered else "All Systems Green · 100% Invested",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(get_crash_protection_data(), indent=2))

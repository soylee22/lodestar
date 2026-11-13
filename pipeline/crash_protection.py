"""S&P 500 Crash Protection & Ketchup Rule Monitor for Lodestar.

Calculates the trailing 24 monthly closes of the S&P 500 from daily bars,
evaluates the four risk checks (1-month, 6-month, 12-month momentum and the
19-month -25% floor), and determines the master cash gate status.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
import json
from pathlib import Path
import time
import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent.parent


def _fetch_spx_daily(start: str, need: pd.Period, retries: int = 4, wait: float = 4.0) -> pd.Series | None:
    last_err = None
    for attempt in range(retries):
        try:
            d = yf.Ticker("^GSPC").history(start=start, interval="1d", auto_adjust=False)["Close"].dropna()
            if not d.empty:
                d.index = pd.to_datetime(d.index).tz_localize(None)
                if d.index.min().to_period("M") <= need:
                    return d
                last_err = f"short series from {d.index.min():%Y-%m-%d}"
            else:
                last_err = "empty response"
        except Exception as exc:
            last_err = exc
        if attempt + 1 < retries:
            time.sleep(wait)
    return None


def get_crash_protection_data(
    today: date | None = None,
    spx_daily: pd.Series | None = None,
    root: Path | None = None,
    use_cache: bool = False,
) -> dict:
    ref_day = today if today is not None else date.today()
    now = pd.Timestamp(ref_day).to_period("M")
    need_19 = now - 19
    start = (pd.Timestamp(ref_day) - pd.DateOffset(months=26)).date().isoformat()

    if spx_daily is None:
        spx_daily = _fetch_spx_daily(start=start, need=need_19)
    else:
        spx_daily = spx_daily.dropna().copy()
        spx_daily.index = pd.to_datetime(spx_daily.index).tz_localize(None)

    if spx_daily is None or spx_daily.empty:
        return {}

    month_ends = spx_daily.resample("ME").last().dropna()
    month_ends.index = month_ends.index.to_period("M")

    required_periods = {
        1: now - 1,
        6: now - 6,
        12: now - 12,
        19: now - 19,
    }
    if any(p not in month_ends.index for p in required_periods.values()):
        return {}

    last_24 = month_ends.iloc[-24:]
    last_val = float(spx_daily.iloc[-1])
    asof_date = spx_daily.index.max().date().isoformat()
    p_1m = float(month_ends.loc[required_periods[1]])
    p_6m = float(month_ends.loc[required_periods[6]])
    p_12m = float(month_ends.loc[required_periods[12]])
    p_19m = float(month_ends.loc[required_periods[19]])

    m1_pct = round((last_val / p_1m - 1.0) * 100.0, 2)
    m6_pct = round((last_val / p_6m - 1.0) * 100.0, 2)
    m12_pct = round((last_val / p_12m - 1.0) * 100.0, 2)
    m19_pct = round((last_val / p_19m - 1.0) * 100.0, 2)

    # Research hypotheses only. The proprietary checks remain unresolved.
    checks = [
        {
            "id": "m1",
            "name": "1-Month Momentum",
            "desc": "Latest close vs previous month-end",
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
            "desc": "Return vs 19 months earlier (threshold: -25%)",
            "reading_pct": m19_pct,
            "pass": m19_pct >= -25.0,
            "rule": "Lodestar Circuit Breaker",
        },
    ]

    # Production uses the existing Lodestar floor only.
    ketchup_fails = sum(1 for c in checks[:3] if not c["pass"])
    cash_triggered = m19_pct < -25.0

    if cash_triggered:
        status_text = "Cash Gate Active · 100% Cash"
        status_tone = "fail"
    elif ketchup_fails == 3:
        status_text = "19M Floor Intact · Research Checks Negative"
        status_tone = "warn"
    elif ketchup_fails > 0:
        status_text = "19M Floor Intact · 100% Invested"
        status_tone = "pass"
    else:
        status_text = "All Systems Green · 100% Invested"
        status_tone = "pass"

    chart_points = [{"month": str(idx), "price": round(float(v), 2)} for idx, v in last_24.items()]
    out = {
        "asof": asof_date,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "latest_price": round(last_val, 2),
        "history": chart_points,
        "chart": chart_points,
        "checks": checks,
        "ketchup_fails": ketchup_fails,
        "research_cash_triggered": ketchup_fails == 3,
        "method_note": "The three research checks do not recover the proprietary MarketFighter rule. The production gate uses the 19-month floor.",
        "cash_triggered": cash_triggered,
        "m19_pct": m19_pct,
        "base_month_19m": str(required_periods[19]),
        "status": "CASH" if cash_triggered else "INVESTED",
        "status_text": status_text,
        "status_tone": status_tone,
    }
    if use_cache:
        base_dir = Path(root) if root is not None else HERE
        try:
            (base_dir / "data" / "crash_protection.json").write_text(json.dumps(out, indent=2))
        except Exception:
            pass
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(get_crash_protection_data(), indent=2))

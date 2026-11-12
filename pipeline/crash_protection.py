"""TIP 1-3-6-12 Canary & Defensive Treasury Selector for Lodestar.

Evaluates HAA's canary rule in USD total return:
  * Score TIP, IEF and BIL by the unweighted mean of 1M, 3M, 6M and 12M total returns.
  * Remain invested (50/50 Factor + Sector) when TIP > 0%.
  * Rotate 100% into argmax(IEF, BIL) when TIP <= 0%, executed in London USD UCITS
    equivalents CBU0.L (IEF) or IB01.L (BIL).
  * Retain S&P 500 19-month -25% reading solely as a legacy comparator.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf

from universe import DEFENSIVE_NAMES, DEFENSIVE_TRADABLE

CANARY_WINDOWS = (1, 3, 6, 12)
CANARY_TICKERS = {"TIP": "TIP", "IEF": "IEF", "BIL": "BIL"}


def score_13612(monthly_series: pd.Series) -> pd.Series:
    """Compute unweighted 1-3-6-12 momentum on a monthly PeriodIndex series."""
    if monthly_series.empty:
        return pd.Series(dtype=float)
    s = monthly_series.sort_index().dropna()
    s = s.reindex(pd.period_range(s.index.min(), s.index.max(), freq="M"))
    parts = [s / s.shift(w) - 1.0 for w in CANARY_WINDOWS]
    frame = pd.concat(parts, axis=1).replace([float("inf"), float("-inf")], float("nan"))
    valid = frame.dropna(how="any")
    return valid.mean(axis=1)


def score_13612_at(
    monthly_series: pd.Series,
    target: pd.Period,
    latest_price: float | None = None,
) -> dict[str, float] | None:
    """Return 1M/3M/6M/12M returns and their mean at `target` (or `latest_price`)."""
    if monthly_series.empty:
        return None
    s = monthly_series.sort_index().dropna()
    cur = latest_price if latest_price is not None else s.get(target)
    if cur is None or pd.isna(cur) or float(cur) <= 0:
        return None
    cur_val = float(cur)
    out: dict[str, float] = {}
    for w in CANARY_WINDOWS:
        ref = s.get(target - w)
        if ref is None or pd.isna(ref) or float(ref) <= 0:
            return None
        out[f"m{w}"] = cur_val / float(ref) - 1.0
    out["score"] = sum(out[f"m{w}"] for w in CANARY_WINDOWS) / len(CANARY_WINDOWS)
    return out


def defensive_choice(ief_score: float, bil_score: float) -> str:
    """Return DEF_IEF when intermediate Treasuries beat T-bills, else DEF_BIL."""
    return "DEF_IEF" if float(ief_score) > float(bil_score) else "DEF_BIL"


def evaluate_canary_history(
    tip: pd.Series,
    ief: pd.Series,
    bil: pd.Series,
) -> pd.DataFrame:
    """Evaluate monthly settled TIP <= 0 canary decisions across common history."""
    st = score_13612(tip)
    si = score_13612(ief)
    sb = score_13612(bil)
    df = pd.concat({"tip_score": st, "ief_score": si, "bil_score": sb}, axis=1).dropna(how="any")
    if df.empty:
        return pd.DataFrame(
            columns=["tip_score", "ief_score", "bil_score", "cash", "defensive", "defensive_ticker"]
        )
    df["cash"] = df["tip_score"] <= 0.0
    df["defensive"] = [
        defensive_choice(ief_v, bil_v) if cash_v else None
        for cash_v, ief_v, bil_v in zip(df["cash"], df["ief_score"], df["bil_score"])
    ]
    df["defensive_ticker"] = [
        DEFENSIVE_TRADABLE[d][2] if d else None for d in df["defensive"]
    ]
    return df


def _daily_with_monthly(sym: str, period: str = "4y", adjusted: bool = True) -> tuple[pd.Series, pd.Series]:
    d = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=adjusted)["Close"].dropna()
    if d.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    d.index = pd.to_datetime(d.index).tz_localize(None)
    m = d.resample("ME").last()
    m.index = m.index.to_period("M")
    return d, m.dropna()


def get_crash_protection_data(history_months: int = 24) -> dict:
    """Compute provisional intra-month TIP canary state, history and checks."""
    try:
        tip_d, tip_m = _daily_with_monthly("TIP", period="4y", adjusted=True)
        ief_d, ief_m = _daily_with_monthly("IEF", period="4y", adjusted=True)
        bil_d, bil_m = _daily_with_monthly("BIL", period="4y", adjusted=True)
    except Exception:
        return {}

    if tip_d.empty or ief_d.empty or bil_d.empty:
        return {}

    latest_dt = min(tip_d.index[-1], ief_d.index[-1], bil_d.index[-1])
    target_period = pd.Timestamp(latest_dt).to_period("M")

    tip_comp = score_13612_at(tip_m, target_period, float(tip_d.loc[:latest_dt].iloc[-1]))
    ief_comp = score_13612_at(ief_m, target_period, float(ief_d.loc[:latest_dt].iloc[-1]))
    bil_comp = score_13612_at(bil_m, target_period, float(bil_d.loc[:latest_dt].iloc[-1]))
    if tip_comp is None or ief_comp is None or bil_comp is None:
        return {}

    tip_score = tip_comp["score"]
    ief_score = ief_comp["score"]
    bil_score = bil_comp["score"]
    cash_triggered = tip_score <= 0.0
    winner_slot = defensive_choice(ief_score, bil_score)
    winner_isin, _, winner_ticker, _ = DEFENSIVE_TRADABLE[winner_slot]
    winner_name = DEFENSIVE_NAMES[winner_slot]

    # Build trailing monthly settled TIP 1-3-6-12 scores plus the current provisional point.
    completed_tip = tip_m[tip_m.index < target_period]
    hist_scores = score_13612(completed_tip)
    tail_scores = hist_scores.iloc[-(history_months - 1):] if history_months > 1 else hist_scores.iloc[:0]
    chart_points = [
        {"month": str(idx), "score_pct": round(float(val) * 100.0, 2), "provisional": False}
        for idx, val in tail_scores.items()
    ]
    chart_points.append(
        {"month": str(target_period), "score_pct": round(tip_score * 100.0, 2), "provisional": True}
    )

    legacy_m19_pct = None
    try:
        _, spx_m = _daily_with_monthly("^GSPC", period="3y", adjusted=False)
        if len(spx_m) >= 20 and target_period - 19 in spx_m.index:
            spx_now = float(spx_m.iloc[-1])
            legacy_m19_pct = round((spx_now / float(spx_m.loc[target_period - 19]) - 1.0) * 100.0, 2)
    except Exception:
        legacy_m19_pct = None

    checks = [
        {
            "id": "tip_canary",
            "name": "TIP 1-3-6-12 Canary",
            "desc": "Unweighted 1M, 3M, 6M, 12M USD total return",
            "reading_pct": round(tip_score * 100.0, 2),
            "pass": not cash_triggered,
            "rule": "Master Gate (> 0.00%)",
        },
        {
            "id": "tip_m1",
            "name": "TIP 1-Month Return",
            "desc": "Latest close vs 1 month earlier",
            "reading_pct": round(tip_comp["m1"] * 100.0, 2),
            "pass": tip_comp["m1"] > 0.0,
            "rule": "Canary Component",
        },
        {
            "id": "tip_m3",
            "name": "TIP 3-Month Return",
            "desc": "Latest close vs 3 months earlier",
            "reading_pct": round(tip_comp["m3"] * 100.0, 2),
            "pass": tip_comp["m3"] > 0.0,
            "rule": "Canary Component",
        },
        {
            "id": "tip_m6",
            "name": "TIP 6-Month Return",
            "desc": "Latest close vs 6 months earlier",
            "reading_pct": round(tip_comp["m6"] * 100.0, 2),
            "pass": tip_comp["m6"] > 0.0,
            "rule": "Canary Component",
        },
        {
            "id": "tip_m12",
            "name": "TIP 12-Month Return",
            "desc": "Latest close vs 12 months earlier",
            "reading_pct": round(tip_comp["m12"] * 100.0, 2),
            "pass": tip_comp["m12"] > 0.0,
            "rule": "Canary Component",
        },
        {
            "id": "def_bil",
            "name": "IB01.L (BIL 1-3-6-12)",
            "desc": "US Treasury 0-1yr UCITS defensive sleeve",
            "reading_pct": round(bil_score * 100.0, 2),
            "pass": winner_slot == "DEF_BIL",
            "rule": "Defensive Winner" if winner_slot == "DEF_BIL" else "Defensive Standby",
        },
        {
            "id": "def_ief",
            "name": "CBU0.L (IEF 1-3-6-12)",
            "desc": "US Treasury 7-10yr UCITS defensive sleeve",
            "reading_pct": round(ief_score * 100.0, 2),
            "pass": winner_slot == "DEF_IEF",
            "rule": "Defensive Winner" if winner_slot == "DEF_IEF" else "Defensive Standby",
        },
    ]
    if legacy_m19_pct is not None:
        checks.append(
            {
                "id": "legacy_floor19",
                "name": "S&P 500 19-Month Floor",
                "desc": "Historical -25%/19m comparator reading",
                "reading_pct": legacy_m19_pct,
                "pass": legacy_m19_pct >= -25.0,
                "rule": "Legacy Comparator (>= -25%)",
            }
        )

    return {
        "asof": latest_dt.date().isoformat(),
        "period": str(target_period),
        "tip_score_pct": round(tip_score * 100.0, 2),
        "ief_score_pct": round(ief_score * 100.0, 2),
        "bil_score_pct": round(bil_score * 100.0, 2),
        "defensive_slot": winner_slot,
        "defensive_ticker": winner_ticker,
        "defensive_name": winner_name,
        "defensive_isin": winner_isin,
        "legacy_m19_pct": legacy_m19_pct,
        "history": chart_points,
        "chart": chart_points,
        "checks": checks,
        "method_note": (
            "Evaluates TIP 1-3-6-12 in USD total return at month-end. "
            "When TIP <= 0%, both equity legs exit into argmax(IEF, BIL) via CBU0.L or IB01.L."
        ),
        "cash_triggered": cash_triggered,
        "status": "DEFENSIVE" if cash_triggered else "INVESTED",
        "status_text": (
            f"TIP Canary Triggered · 100% {winner_ticker}"
            if cash_triggered
            else "TIP Canary Positive · 100% Equities (50/50)"
        ),
        "status_desc": (
            f"TIP 1-3-6-12 sits at {tip_score*100:+.2f}% (<= 0%). Provisional defensive sleeve: {winner_name} ({winner_ticker})."
            if cash_triggered
            else f"TIP 1-3-6-12 sits at {tip_score*100:+.2f}% (> 0%). Standby defensive sleeve: {winner_ticker}."
        ),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(get_crash_protection_data(), indent=2))

#!/usr/bin/env python3
"""Lodestar daily read: where the signal stands part-way through the month.

This is a drumbeat, not a trade instruction. The real signal is computed once,
at the month-end close, by run_monthly.py. What this does is take the same rule
and evaluate it on today's prices, so the standing is never a surprise on the
1st:

  * momentum = today's level / the level at the close of the month eight
    months before the month now running. At month-end that expression becomes
    the real signal, unchanged.
  * the cash rule, likewise, on today's S&P 500 against its close nineteen
    month-ends back.
  * what is held now comes from data/last_alert.json, which run_monthly.py
    writes when it announces a signal.

The factor leg uses the US-listed distributing factor ETFs rather than the MSCI
indices, because MSCI publishes month-end levels only and there is no intra-month
index level to read. Their unadjusted closes are price series, like the index,
but they are not the index: momentum runs 1-4pp adrift. The sector leg uses the
S&P sector indices themselves, which are daily and exact.
"""
from __future__ import annotations
import json, sys, time
from calendar import monthrange
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from notify import telegram              # noqa: E402
from mailer import email                 # noqa: E402
from universe import FACTOR_NAMES, SECTOR_NAMES  # noqa: E402

NAMES = {**FACTOR_NAMES, **SECTOR_NAMES}
SITE = "https://soylee22.github.io/lodestar/"

LOOKBACK = 8
CASH_LOOKBACK, CASH_THRESHOLD = 19, -0.25

FACTOR_PROXY = {"USA_MOM": "MTUM", "USA_QUAL": "QUAL", "USA_VAL": "VLUE",
                "USA_SMALL": "SMLF", "EUR_MOM": "IMTM", "EUR_QUAL": "IQLT",
                "EUR_VAL": "IVLU", "EUR_SMALL": "IEUS"}
SECTOR_YF = {"CONS_DISC": "^SP500-25", "CONS_STAP": "^SP500-30", "ENERGY": "^GSPE",
             "HEALTH": "^SP500-35", "INDUST": "^SP500-20", "INFOTECH": "^SP500-45",
             "MATERIALS": "^SP500-15"}
LSE = {"USA_MOM": "IUMO.L", "USA_QUAL": "IUQA.L", "USA_VAL": "IUVL.L",
       "USA_SMALL": "CUSS.L", "EUR_MOM": "IEMO.L", "EUR_QUAL": "IEQU.L",
       "EUR_VAL": "IEVL.L", "EUR_SMALL": "XXSC.L", "CONS_DISC": "IUCD.L",
       "CONS_STAP": "IUCS.L", "ENERGY": "IUES.L", "HEALTH": "IHCU.L",
       "INDUST": "IUIS.L", "INFOTECH": "IUIT.L", "MATERIALS": "IUMS.L"}


def _retry(fn, n=3, wait=4):
    last = None
    for _ in range(n):
        try:
            out = fn()
            if out is not None and len(out):
                return out
        except Exception as e:
            last = e
        time.sleep(wait)
    raise RuntimeError(f"failed after {n} attempts: {last}")


def series(sym, start, adjusted=False):
    """One symbol's daily closes. Fetched one at a time, with retries: Yahoo
    throttles bulk requests from datacentre addresses, which is where this runs."""
    def go():
        d = yf.Ticker(sym).history(start=start, interval="1d", auto_adjust=adjusted)["Close"]
        if d.empty:
            return None
        d.index = pd.to_datetime(d.index).tz_localize(None)
        return d
    return _retry(go)


def closes(mapping, start, adjusted=False):
    """Frame of daily closes keyed by slot, plus the slots that would not load.

    A missing slot is reported rather than raised: this is a read, not a trade,
    and a partial ranking flagged as partial beats no message at all.
    """
    out, missing = {}, []
    for slot, sym in mapping.items():
        try:
            out[slot] = series(sym, start, adjusted)
        except Exception as e:
            missing.append(slot)
            print(f"  !! {slot} ({sym}) unavailable: {type(e).__name__}")
    if not out:
        raise RuntimeError("no price history at all")
    return pd.DataFrame(out).ffill(), missing


def month_end_level(px: pd.DataFrame, period: pd.Period) -> pd.Series:
    """Last close in a completed month, as a row of the frame."""
    m = px.resample("ME").last()
    m.index = m.index.to_period("M")
    if period not in m.index:
        raise RuntimeError(f"no history at {period}")
    return m.loc[period]


def held_now() -> tuple[str, str, str]:
    """(factor slot, sector slot, month the signal was computed at)."""
    state = json.loads((HERE / "data" / "last_alert.json").read_text())
    picks = state.get("picks", "")
    if picks == "CASH":
        return "CASH", "CASH", state.get("month", "?")
    f, _, s = picks.partition("+")
    return f, s, state.get("month", "?")


def ranked(mom: pd.Series) -> list[tuple[str, float]]:
    return sorted(((k, float(v)) for k, v in mom.items()), key=lambda kv: -kv[1])


def main() -> int:
    today = date.today()
    now = pd.Timestamp(today).to_period("M")
    base = now - LOOKBACK                 # the month-end the eight-month window opens at
    cash_base = now - CASH_LOOKBACK
    start = (pd.Timestamp(today) - pd.DateOffset(months=CASH_LOOKBACK + 4)).date().isoformat()

    factor_px, f_missing = closes(FACTOR_PROXY, start)
    sector_px, s_missing = closes(SECTOR_YF, start)
    spx_px, _ = closes({"SPX": "^GSPC"}, start)

    def momentum(px, missing):
        lvl = month_end_level(px, base)
        last = px.iloc[-1]
        mom = (last / lvl - 1).dropna()
        return mom, missing + [c for c in px.columns if c not in mom.index]

    fmom, f_missing = momentum(factor_px, f_missing)
    smom, s_missing = momentum(sector_px, s_missing)
    asof = max(factor_px.index[-1], sector_px.index[-1]).date()

    spx_now = float(spx_px["SPX"].iloc[-1])
    spx_base = float(month_end_level(spx_px, cash_base)["SPX"])
    spx_ret = spx_now / spx_base - 1
    cash = spx_ret < CASH_THRESHOLD

    f_pick = str(fmom.idxmax()) if len(fmom) else None
    s_pick = str(smom.idxmax()) if len(smom) else None
    hf, hs, hmonth = held_now()

    if cash:
        would = "<b>cash, both legs</b>"
    else:
        would = " + ".join(
            f"{NAMES[p]} (<code>{LSE[p]}</code>)" if p else "leg unavailable"
            for p in (f_pick, s_pick))
    change = [] if cash else (
        ([f"factor: {NAMES.get(hf, hf)} → <b>{NAMES[f_pick]}</b>"] if f_pick and f_pick != hf else []) +
        ([f"sector: {NAMES.get(hs, hs)} → <b>{NAMES[s_pick]}</b>"] if s_pick and s_pick != hs else []))
    partial = bool(f_missing or s_missing)

    # month to date on what is actually held, in sterling, against the benchmark
    mtd = ""
    try:
        wanted = ({} if hf == "CASH" else {hf: LSE[hf], hs: LSE[hs]}) | {"All-World": "VWRL.L"}
        lse, _ = closes(wanted, start=str((now - 2).to_timestamp("M").date()), adjusted=True)
        m0 = month_end_level(lse, now - 1)
        mtd = "  " + " · ".join(
            f"{NAMES.get(k, k)} {(lse[k].iloc[-1] / m0[k] - 1) * 100:+.1f}%"
            for k in lse.columns if k in m0.index)
    except Exception as e:
        print(f"  !! month to date unavailable ({type(e).__name__})")

    last_day = monthrange(today.year, today.month)[1]
    signal_date = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    days_out = (signal_date - today).days

    head = ("<b>Lodestar — the cash rule would fire</b>" if cash
            else "<b>Lodestar — rebalance today: NO CHANGE</b>" if not change
            else "<b>Lodestar — rebalance today: WOULD TRADE</b>")

    msg = [head, "",
           f"Holding now: <b>{NAMES.get(hf, hf)}</b> + <b>{NAMES.get(hs, hs)}</b>, 50/50"
           + (f" (signal of {hmonth})" if hmonth != "?" else ""),
           f"If rebalanced today: {would}"]
    if change:
        msg += [""] + ["  " + c for c in change]
    msg += ["",
            f"Next signal at the {now} close, acting <b>{signal_date:%-d %b}</b>: "
            f"<b>{days_out} day{'s' if days_out != 1 else ''} out</b>, "
            f"{today.day} of {last_day} days of the month run."]
    if mtd:
        msg += ["", "<b>Month to date</b>", mtd]
    if len(fmom):
        msg += ["", "<b>Factor</b> (ETF proxy, 1-4pp adrift of the index)",
                "\n".join(f"  {NAMES[k]:<22s}{v*100:+6.1f}%" for k, v in ranked(fmom)[:4])]
    if len(smom):
        msg += ["<b>Sector</b>",
                "\n".join(f"  {NAMES[k]:<22s}{v*100:+6.1f}%" for k, v in ranked(smom)[:4])]
    msg += ["",
            f"Cash rule: S&amp;P 500 {spx_ret*100:+.1f}% against its {cash_base} close, "
            f"fires below {CASH_THRESHOLD*100:.0f}%. "
            + ("<b>Cash.</b>" if cash else "Invested.")]
    if partial:
        msg += ["", "<i>Incomplete: no price today for "
                + ", ".join(NAMES.get(k, k) for k in f_missing + s_missing)
                + ". Those slots are absent from the ranking above.</i>"]
    msg += ["",
            f"<i>Prices to {asof:%-d %b %Y}. Provisional. The signal that counts is "
            "computed at the month-end close.</i>",
            SITE]
    body = "\n".join(msg)
    print(body)

    plain = body
    for a, b in (("<b>", ""), ("</b>", ""), ("<i>", ""), ("</i>", ""),
                 ("<code>", ""), ("</code>", ""), ("&amp;", "&")):
        plain = plain.replace(a, b)
    html = ("<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
            "font-size:15px;line-height:1.5;max-width:640px\">"
            + body.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
            + "</div>")
    subject = ("Lodestar: the cash rule would fire" if cash
               else f"Lodestar: no change, {days_out}d to rebalance" if not change
               else f"Lodestar: WOULD TRADE, {days_out}d to rebalance")

    telegram(body)
    email(subject, plain, html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

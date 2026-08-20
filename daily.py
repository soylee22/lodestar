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
import json, sys
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


def closes(symbols, start, adjusted=False):
    d = yf.download(list(symbols), start=start, interval="1d",
                    auto_adjust=adjusted, progress=False, threads=False)
    px = d["Close"] if isinstance(d.columns, pd.MultiIndex) else d[["Close"]]
    px.index = pd.to_datetime(px.index).tz_localize(None)
    return px.dropna(how="all").ffill()


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
    base = now - LOOKBACK                 # the month-end the window opens at
    cash_base = now - CASH_LOOKBACK
    start = (pd.Timestamp(today) - pd.DateOffset(months=CASH_LOOKBACK + 3)).date().isoformat()

    factor_px = closes(FACTOR_PROXY.values(), start)
    sector_px = closes(SECTOR_YF.values(), start)
    spx = closes(["^GSPC"], start)

    f_base, s_base = month_end_level(factor_px, base), month_end_level(sector_px, base)
    f_now, s_now = factor_px.iloc[-1], sector_px.iloc[-1]
    asof = max(factor_px.index[-1], sector_px.index[-1]).date()

    fmom = pd.Series({k: f_now[v] / f_base[v] - 1 for k, v in FACTOR_PROXY.items()})
    smom = pd.Series({k: s_now[v] / s_base[v] - 1 for k, v in SECTOR_YF.items()})
    f_pick, s_pick = str(fmom.idxmax()), str(smom.idxmax())

    spx_now = float(spx.iloc[-1].iloc[0])
    spx_base = float(month_end_level(spx, cash_base).iloc[0])
    spx_ret = spx_now / spx_base - 1
    cash = spx_ret < CASH_THRESHOLD

    hf, hs, hmonth = held_now()
    if cash:
        would = "CASH"
    else:
        would = f"{NAMES[f_pick]} (<code>{LSE[f_pick]}</code>) + {NAMES[s_pick]} (<code>{LSE[s_pick]}</code>)"
    change = [] if cash else (
        ([f"factor: {NAMES.get(hf, hf)} → <b>{NAMES[f_pick]}</b>"] if f_pick != hf else []) +
        ([f"sector: {NAMES.get(hs, hs)} → <b>{NAMES[s_pick]}</b>"] if s_pick != hs else []))

    # month-to-date on what is actually held, in sterling, against the benchmark
    mtd = ""
    try:
        held_syms = [] if hf == "CASH" else [LSE[hf], LSE[hs]]
        lse = closes(held_syms + ["VWRL.L"], start=str(base.to_timestamp("M").date()), adjusted=True)
        m0 = month_end_level(lse, now - 1)
        parts = [f"{sym} {(lse.iloc[-1][sym] / m0[sym] - 1) * 100:+.1f}%"
                 for sym in held_syms + ["VWRL.L"]]
        mtd = "  " + " · ".join(parts)
    except Exception as e:
        print(f"  !! MTD unavailable ({type(e).__name__})")

    last_day = monthrange(today.year, today.month)[1]
    signal_date = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    days_out = (signal_date - today).days
    elapsed = today.day

    head = ("<b>Lodestar — GO TO CASH would trigger</b>" if cash
            else "<b>Lodestar — rebalance today: NO CHANGE</b>" if not change
            else "<b>Lodestar — rebalance today: WOULD TRADE</b>")

    msg = [
        head, "",
        f"Holding now: <b>{NAMES.get(hf, hf)}</b> + <b>{NAMES.get(hs, hs)}</b>, 50/50"
        + (f" (signal of {hmonth})" if hmonth != "?" else ""),
        f"If rebalanced today: {would}",
    ]
    if change:
        msg += ["", "\n".join("  " + c for c in change)]
    msg += [
        "",
        f"Next signal at the {now} close, acting <b>{signal_date:%-d %b}</b> — "
        f"<b>{days_out} day{'s' if days_out != 1 else ''} out</b> "
        f"({elapsed} of {last_day} days of the month run).",
    ]
    if mtd:
        msg += ["", "<b>Month to date</b>", mtd]
    msg += [
        "", "<b>Factor</b> (ETF proxy, 1-4pp adrift of the index)",
        "\n".join(f"  {NAMES[k]:<22s}{v*100:+6.1f}%" for k, v in ranked(fmom)[:4]),
        "<b>Sector</b>",
        "\n".join(f"  {NAMES[k]:<22s}{v*100:+6.1f}%" for k, v in ranked(smom)[:4]),
        "",
        f"Cash rule: S&amp;P 500 {spx_ret*100:+.1f}% vs its {cash_base} close "
        f"(fires below {CASH_THRESHOLD*100:.0f}%) — "
        + ("<b>CASH</b>" if cash else "invested"),
        "",
        f"<i>Prices to {asof:%-d %b %Y}. Provisional: the signal that counts is "
        f"computed at the month-end close.</i>",
        SITE,
    ]
    body = "\n".join(msg)
    print(body)

    plain = (body.replace("<b>", "").replace("</b>", "").replace("<i>", "")
                 .replace("</i>", "").replace("<code>", "").replace("</code>", "")
                 .replace("&amp;", "&"))
    html = ("<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
            "font-size:15px;line-height:1.5;max-width:640px\">"
            + body.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
            + "</div>")
    subject = ("Lodestar: cash signal would trigger" if cash
               else f"Lodestar: no change, {days_out}d to rebalance" if not change
               else f"Lodestar: WOULD TRADE, {days_out}d to rebalance")

    telegram(body)
    email(subject, plain, html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

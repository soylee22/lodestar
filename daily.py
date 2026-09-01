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
but they are not the index: momentum runs 1-4pp adrift.

The sector leg reads the S&P sector indices, which are daily and exact when the
feed is whole. Since 21 Aug 2026 that feed has served a partial window: recent
closes arrive but the month-end eight months back does not, which leaves the
momentum undefined for every sector at once. When any sector slot comes back
incomplete the whole leg is recomputed from the SPDR Select Sector ETFs, which
are unadjusted price series on the same indices and matched the index momentum
to 0.05pp on the last day both could be read. The leg switches wholesale, never
slot by slot: a ranking built half on the index and half on a proxy is not a
ranking.
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
SECTOR_PROXY = {"CONS_DISC": "XLY", "CONS_STAP": "XLP", "ENERGY": "XLE",
                "HEALTH": "XLV", "INDUST": "XLI", "INFOTECH": "XLK",
                "MATERIALS": "XLB"}
LSE = {"USA_MOM": "IUMO.L", "USA_QUAL": "IUQA.L", "USA_VAL": "IUVL.L",
       "USA_SMALL": "CUSS.L", "EUR_MOM": "IEMO.L", "EUR_QUAL": "IEQU.L",
       "EUR_VAL": "IEVL.L", "EUR_SMALL": "XXSC.L", "CONS_DISC": "IUCD.L",
       "CONS_STAP": "IUCS.L", "ENERGY": "IUES.L", "HEALTH": "IHCU.L",
       "INDUST": "IUIS.L", "INFOTECH": "IUIT.L", "MATERIALS": "IUMS.L"}


def _retry(fn, n=4, wait=5):
    last = None
    for _ in range(n):
        try:
            out = fn()
            if out is not None and len(out):
                return out
            last = "empty response"
        except Exception as e:
            last = e
        time.sleep(wait)
    raise RuntimeError(f"failed after {n} attempts: {last}")


def series(sym, start, need=None, adjusted=False, retries=4):
    """One symbol's daily closes, fetched alone and with retries.

    Yahoo throttles this runner's addresses and answers a throttled request with
    a short series rather than an error, so a response that does not reach back
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


def closes(mapping, start, need=None, adjusted=False, retries=4, stale_days=5):
    """Frame of daily closes keyed by slot, plus the slots that would not load.

    A missing slot is reported, never raised: this is a read, not a trade, and a
    partial ranking flagged as partial beats a failed job and a silent morning.

    Every slot in one mapping trades on one calendar, so a slot whose own last
    close sits more than `stale_days` behind the newest date in the frame has
    stopped updating at the source. It is dropped rather than carried: the frame
    is forward-filled to align the calendars, and forward-filling a dead series
    would hand `iloc[-1]` a months-old price under today's date. Yahoo currently
    answers a long-window request for the S&P sector indices with rows that run
    to yesterday and values that stop in July, which is exactly this.
    """
    out, missing = {}, []
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
        print(f"  !! {c} ({mapping[c]}) stops at {last[c]:%Y-%m-%d}, "
              f"{asof:%Y-%m-%d} elsewhere: dropped as stale")
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


def momentum(px: pd.DataFrame, base: pd.Period, missing: list) -> tuple[pd.Series, list]:
    """Price return from the close of `base` to the latest close, per slot."""
    lvl = month_end_level(px, base)
    if lvl.empty:
        return pd.Series(dtype=float), missing + list(px.columns)
    mom = (px.iloc[-1][lvl.index] / lvl - 1).dropna()
    return mom, missing + [c for c in px.columns if c not in mom.index]


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


def margin(mom: pd.Series, held: str) -> str | None:
    """What has to happen, from here to the month end, for the held slot to keep
    it. Momentum is a price ratio, so the gap between two rows compounds: the
    difference in percentage points is not the return the laggard must make up.
    Without this line a mid-month ranking reads as a decision, and it is not one.
    """
    if held not in mom.index or len(mom) < 2:
        return None
    (lead, lv), (second, sv) = ranked(mom)[:2]
    if lead == held:
        give = 1 - (1 + sv) / (1 + mom[held])
        return (f"  <i>{NAMES[held]} holds the slot unless it gives back "
                f"{give*100:.1f}% against {NAMES[second]} this month.</i>")
    need = (1 + lv) / (1 + float(mom[held])) - 1
    return (f"  <i>{NAMES[held]} keeps the slot only if it beats {NAMES[lead]} by "
            f"{need*100:.1f}% over the rest of the month.</i>")


def send(subject: str, body: str) -> int:
    plain = body
    for a, b in (("<b>", ""), ("</b>", ""), ("<i>", ""), ("</i>", ""),
                 ("<code>", ""), ("</code>", ""), ("&amp;", "&")):
        plain = plain.replace(a, b)
    html = ("<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
            "font-size:15px;line-height:1.5;max-width:640px\">"
            + body.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
            + "</div>")
    print(body)
    telegram(body)
    email(subject, plain, html)
    return 0


def main() -> int:
    today = date.today()
    now = pd.Timestamp(today).to_period("M")
    base = now - LOOKBACK                 # the month-end the eight-month window opens at
    cash_base = now - CASH_LOOKBACK
    start = (pd.Timestamp(today) - pd.DateOffset(months=CASH_LOOKBACK + 4)).date().isoformat()

    factor_px, f_missing = closes(FACTOR_PROXY, start, need=base)
    sector_px, s_missing = closes(SECTOR_YF, start, need=base, retries=2)
    spx_px, _ = closes({"SPX": "^GSPC"}, start, need=cash_base)

    fmom, f_missing = momentum(factor_px, base, f_missing)
    smom, s_missing = momentum(sector_px, base, s_missing)

    sector_proxy = False
    if s_missing:
        print(f"  .. sector index feed incomplete ({', '.join(sorted(s_missing))}); "
              "recomputing the leg from the sector ETFs")
        px2, miss2 = closes(SECTOR_PROXY, start, need=base)
        smom2, miss2 = momentum(px2, base, miss2)
        if len(smom2) > len(smom):
            sector_px, smom, s_missing, sector_proxy = px2, smom2, miss2, True

    if not len(fmom) and not len(smom):
        return send("Lodestar: no read today",
                    "<b>Lodestar — no read today</b>\n\nYahoo returned no usable price "
                    "history for either leg. Nothing is wrong with the strategy and "
                    "nothing needs doing; the month-end signal is computed from a "
                    "different path.\n\n" + SITE)

    dates = [px.index[-1] for px in (factor_px, sector_px) if not px.empty]
    asof = max(dates).date()

    spx_lvl = month_end_level(spx_px, cash_base)
    if len(spx_lvl):
        spx_ret = float(spx_px["SPX"].iloc[-1] / spx_lvl["SPX"] - 1)
        cash = spx_ret < CASH_THRESHOLD
    else:
        spx_ret, cash = None, False

    f_pick = str(fmom.idxmax()) if len(fmom) else None
    s_pick = str(smom.idxmax()) if len(smom) else None
    hf, hs, hmonth = held_now()

    if cash:
        would = "<b>cash, both legs</b>"
    else:
        would = " + ".join(f"{NAMES[p]} (<code>{LSE[p]}</code>)" if p else "leg unavailable"
                           for p in (f_pick, s_pick))
    change = [] if cash else (
        ([f"factor: {NAMES.get(hf, hf)} → <b>{NAMES[f_pick]}</b>"] if f_pick and f_pick != hf else []) +
        ([f"sector: {NAMES.get(hs, hs)} → <b>{NAMES[s_pick]}</b>"] if s_pick and s_pick != hs else []))

    # month to date on what is actually held, in sterling, against the benchmark
    mtd = ""
    try:
        wanted = ({} if hf == "CASH" else {hf: LSE[hf], hs: LSE[hs]}) | {"All-World": "VWRL.L"}
        lse, _ = closes(wanted, start=str((now - 2).to_timestamp("M").date()),
                        need=now - 1, adjusted=True)
        m0 = month_end_level(lse, now - 1)
        if len(m0):
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
        msg += [m for m in [margin(fmom, hf)] if m]
    if len(smom):
        msg += ["<b>Sector</b>" + (" (SPDR ETF proxy, the index feed is incomplete)"
                                   if sector_proxy else ""),
                "\n".join(f"  {NAMES[k]:<22s}{v*100:+6.1f}%" for k, v in ranked(smom)[:4])]
        msg += [m for m in [margin(smom, hs)] if m]
    msg += [""]
    if spx_ret is None:
        msg += ["Cash rule: not read today, no S&amp;P 500 history."]
    else:
        msg += [f"Cash rule: S&amp;P 500 {spx_ret*100:+.1f}% against its {cash_base} close, "
                f"fires below {CASH_THRESHOLD*100:.0f}%. "
                + ("<b>Cash.</b>" if cash else "Invested.")]
    if f_missing or s_missing:
        msg += ["", "<i>Incomplete: no price today for "
                + ", ".join(NAMES.get(k, k) for k in f_missing + s_missing)
                + ". Those slots are absent from the ranking above.</i>"]
    msg += ["",
            f"<i>Prices to {asof:%-d %b %Y}. Provisional. The signal that counts is "
            "computed at the month-end close.</i>",
            SITE]

    subject = ("Lodestar: the cash rule would fire" if cash
               else f"Lodestar: no change, {days_out}d to rebalance" if not change
               else f"Lodestar: WOULD TRADE, {days_out}d to rebalance")
    return send(subject, "\n".join(msg))


if __name__ == "__main__":
    raise SystemExit(main())

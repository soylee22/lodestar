#!/usr/bin/env python3
"""Compile the Lodestar track record into a single data.js literal.

Reads the tradable monthly/annual return CSVs and the monthly holding book, computes
every statistic the page displays, and writes `data.js` next to this script as

    const DATA = { ... };

Nothing is computed in the browser.

The source CSVs live outside this folder. Give the script their directory, either
as an argument or in the LODESTAR_DATA environment variable:

    python3 build_data.py /path/to/data
    LODESTAR_DATA=/path/to/data python3 build_data.py

That directory must contain final_tradable.csv, final_tradable_annual.csv,
final_book.csv and universe.py one level above it.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
SOURCE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LODESTAR_DATA", "")).strip()
if not SOURCE:
    raise SystemExit("usage: build_data.py <data-dir>   (or set LODESTAR_DATA)")
DATA_DIR = Path(SOURCE).expanduser().resolve()
TOOL_DIR = DATA_DIR.parent
if not (DATA_DIR / "final_tradable.csv").is_file():
    raise SystemExit(f"final_tradable.csv not found in {DATA_DIR}")

sys.path.insert(0, str(TOOL_DIR))
from universe import FACTOR_NAMES, SECTOR_NAMES, TRADABLE  # noqa: E402

RISK_FREE = 0.02  # annual, used for Sharpe and Sortino

# The strategy is the first column of both CSVs; benchmarks are named.
STRATEGY_COL = 0
BENCHMARKS = {"allworld": "FTSE All-World", "sp500": "S&P 500", "world": "MSCI World"}
LABELS = {
    "strategy": "Lodestar",
    "allworld": "FTSE All-World",
    "sp500": "S&P 500",
    "world": "MSCI World",
}

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# The factor basket is four styles crossed with two regions, not eight unrelated
# indices. The page draws one glyph per style and tags the region typographically.
STYLES = [("MOM", "Momentum"), ("QUAL", "Quality"), ("VAL", "Value"), ("SMALL", "Size")]
REGION = {"USA": "US", "EUR": "EU"}

# Look-through: the funds report their own sector taxonomy, which is not the GICS
# naming the sector basket uses. Map both onto the glyph set the page already draws,
# so a holding and a sector index that mean the same thing carry the same mark.
SECTOR_META = {
    "technology":             ("Technology", "INFOTECH"),
    "energy":                 ("Energy", "ENERGY"),
    "healthcare":             ("Health Care", "HEALTH"),
    "consumer_cyclical":      ("Consumer Cyclical", "CONS_DISC"),
    "consumer_defensive":     ("Consumer Defensive", "CONS_STAP"),
    "industrials":            ("Industrials", "INDUST"),
    "basic_materials":        ("Basic Materials", "MATERIALS"),
    "financial_services":     ("Financial Services", "FIN"),
    "communication_services": ("Communication Services", "COMMS"),
    "utilities":              ("Utilities", "UTIL"),
    "realestate":             ("Real Estate", "REALEST"),
    "real_estate":            ("Real Estate", "REALEST"),
}
MIX_FLOOR = 5.0          # sectors under this fold into one tail line
TOP_NAMED = 3            # how many companies the concentration headline names


def r6(x):
    """Round for transport; keep NaN/inf out of the JSON."""
    if x is None:
        return None
    if isinstance(x, (np.floating, float)) and (math.isnan(x) or math.isinf(x)):
        return None
    return round(float(x), 6)


def stats_for(pct: pd.Series) -> dict:
    """Full statistics block for one monthly-percent return series."""
    r = pct.to_numpy(dtype=float) / 100.0
    n = len(r)
    wealth = np.cumprod(1.0 + r)
    growth = float(wealth[-1])
    cagr = growth ** (12.0 / n) - 1.0
    vol = float(np.std(r, ddof=1)) * math.sqrt(12.0)
    sharpe = (cagr - RISK_FREE) / vol
    downside = float(np.sqrt(np.mean(np.minimum(r, 0.0) ** 2))) * math.sqrt(12.0)
    sortino = (cagr - RISK_FREE) / downside
    peak = np.maximum.accumulate(wealth)
    dd = wealth / peak - 1.0
    trough = int(np.argmin(dd))

    # Peak month preceding the trough, and the month the drawdown was recovered.
    peak_i = int(np.argmax(wealth[: trough + 1]))
    rec_i = None
    for j in range(trough + 1, n):
        if wealth[j] >= peak[trough]:
            rec_i = j
            break

    idx = list(pct.index)
    best = int(np.argmax(r))
    worst = int(np.argmin(r))

    # Rolling 12-month total return, aligned to the ending month.
    roll = [None] * n
    for i in range(11, n):
        roll[i] = r6((np.prod(1.0 + r[i - 11: i + 1]) - 1.0) * 100.0)

    return {
        "months": n,
        "growth": r6(growth),
        "cagr": r6(cagr * 100.0),
        "vol": r6(vol * 100.0),
        "sharpe": r6(sharpe),
        "sortino": r6(sortino),
        "downside": r6(downside * 100.0),
        "maxdd": r6(float(dd.min()) * 100.0),
        "maxddPeak": idx[peak_i],
        "maxddTrough": idx[trough],
        "maxddRecovered": idx[rec_i] if rec_i is not None else None,
        "maxddRecoveryMonths": (rec_i - trough) if rec_i is not None else None,
        "best": r6(float(r[best]) * 100.0),
        "bestMonth": idx[best],
        "worst": r6(float(r[worst]) * 100.0),
        "worstMonth": idx[worst],
        "posRate": r6(float((r > 0).mean()) * 100.0),
        "posMonths": int((r > 0).sum()),
        "wealth": [r6(v) for v in wealth],
        "drawdown": [r6(v * 100.0) for v in dd],
        "rolling12": roll,
    }


def num(x):
    """Coerce a possibly-missing, possibly-stringified figure to a float or None."""
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def sector_meta(raw) -> tuple:
    key = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key in SECTOR_META:
        return SECTOR_META[key]
    return (str(raw).strip() or "Other", "OTHER")


def build_book(current: dict) -> dict | None:
    """Look-through into the two funds actually held: top ten and fundamentals.

    Fund weights are shares of that fund; book weights are the same figure scaled by
    the leg's 50%, so every number on the page is directly comparable. Written by the
    monthly job; absent or half-empty, the page simply drops the section.
    """
    path = DATA_DIR / "holdings.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None

    live = {leg["leg"].lower(): leg for leg in current["legs"]}
    legs, stale = [], False

    for key in ("factor", "sector"):
        src = (raw.get("legs") or {}).get(key) or {}
        rows = src.get("holdings") or []
        if not rows or key not in live:
            return None                       # a half-drawn book is worse than none
        held = live[key]
        if src.get("slot") and src["slot"] != held["code"]:
            stale = True
        leg_w = float(held["weight"])         # 50, the leg's share of the book

        holdings = []
        for r in rows:
            w = num(r.get("weight")) or 0.0
            sec_name, glyph = sector_meta(r.get("sector"))
            holdings.append({
                "sym": str(r.get("symbol") or "").upper(),
                # The fund's own holdings table names the company; the fundamentals
                # feed sometimes disagrees with itself (ExxonMobil Holdings
                # Corporation), so the fund is the authority on its own book.
                "name": str(r.get("name") or r.get("company") or r.get("symbol") or ""),
                "sector": sec_name,
                "glyph": glyph,
                "industry": (str(r["industry"]) if r.get("industry") else None),
                "w": r6(w * 100.0),           # % of the fund
                "bw": r6(w * leg_w),          # % of the whole book
                "mcap": num(r.get("market_cap")),
                "pe": r6(num(r.get("pe"))),
                "fpe": r6(num(r.get("forward_pe"))),
                "rev": r6(num(r.get("revenue_growth"))),
                "earn": r6(num(r.get("earnings_growth"))),
                "margin": r6(num(r.get("profit_margin"))),
                "yield": r6(num(r.get("dividend_yield"))),
            })
        holdings.sort(key=lambda h: -(h["w"] or 0.0))

        top10 = num(src.get("top10_weight"))
        if top10 is None:
            top10 = sum((h["w"] or 0.0) for h in holdings) / 100.0
        top10 *= 100.0

        mix = sorted(
            ({"name": sector_meta(k)[0], "glyph": sector_meta(k)[1], "w": r6(num(v) * 100.0)}
             for k, v in (src.get("sector_weights") or {}).items() if num(v)),
            key=lambda m: -m["w"],
        )
        shown = [m for m in mix if m["w"] >= MIX_FLOOR]
        tail = [m for m in mix if m["w"] < MIX_FLOOR]

        legs.append({
            "leg": held["leg"],                       # Factor / Sector
            "code": held["code"],
            "name": held["name"],
            "ticker": str(src.get("ticker") or held["lse"]),
            "isin": held["isin"],
            "legWeight": r6(leg_w),
            "top10": r6(top10),                       # % of the fund
            "top10Book": r6(top10 * leg_w / 100.0),   # % of the book
            "restBook": r6(leg_w - top10 * leg_w / 100.0),
            "count": len(holdings),
            "holdings": holdings,
            "mix": shown,
            "mixTail": ({"n": len(tail), "w": r6(sum(m["w"] for m in tail))} if tail else None),
        })

    flat = [dict(h, leg=leg["leg"], legKey=leg["leg"].lower()) for leg in legs for h in leg["holdings"]]
    flat.sort(key=lambda h: -(h["bw"] or 0.0))
    named = flat[:TOP_NAMED]

    return {
        "asOf": raw.get("as_at"),
        "asOfPretty": pretty(raw["as_at"]) if raw.get("as_at") else None,
        "stale": stale,
        "legs": legs,
        "named": [{"sym": h["sym"], "name": h["name"], "leg": h["leg"], "legKey": h["legKey"],
                   "w": h["w"], "bw": h["bw"]} for h in named],
        "namedBook": r6(sum((h["bw"] or 0.0) for h in named)),
        "namedCount": len(named),
        "topBook": r6(flat[0]["bw"]) if flat else None,
        "topSym": flat[0]["sym"] if flat else None,
    }


def pretty(period: str) -> str:
    y, m = period.split("-")
    return f"{MONTH_ABBR[int(m) - 1]} {y}"


def ordinal(period: str) -> int:
    y, m = period.split("-")
    return int(y) * 12 + int(m) - 1


def main() -> None:
    monthly = pd.read_csv(DATA_DIR / "final_tradable.csv", index_col=0)
    annual = pd.read_csv(DATA_DIR / "final_tradable_annual.csv", index_col=0)
    book = pd.read_csv(DATA_DIR / "final_book.csv", index_col=0)

    months = [str(i) for i in monthly.index]
    start, end = months[0], months[-1]

    # The tradable record is not contiguous: a month is dropped when either leg held
    # that month had no London price history yet. Keep the calendar separate from the
    # observed months so charts sit on a true time axis and the gap is visible.
    calendar = [f"{o // 12}-{o % 12 + 1:02d}" for o in range(ordinal(start), ordinal(end) + 1)]
    observed = set(months)
    gaps = [m for m in calendar if m not in observed]
    t_index = [ordinal(m) - ordinal(start) for m in months]

    book = book.loc[start:end]
    if [str(i) for i in book.index] != calendar:
        raise SystemExit(f"book/calendar mismatch: {len(book)} vs {len(calendar)}")

    cols = {"strategy": monthly.columns[STRATEGY_COL]}
    cols.update(BENCHMARKS)

    returns, stats = {}, {}
    for key, col in cols.items():
        col_s = monthly[col]
        returns[key] = [r6(v) for v in col_s.to_numpy(dtype=float)]
        stats[key] = stats_for(col_s)

    strat = monthly[cols["strategy"]].to_numpy(dtype=float)
    aw = monthly[cols["allworld"]].to_numpy(dtype=float)
    excess_m = strat - aw
    wins = int((strat > aw).sum())

    # Calendar years. 2015 is Dec only; the final year runs to the last month held.
    years = []
    for y, row in annual.iterrows():
        in_year = [m for m in months if m.startswith(f"{y}-")]
        years.append({
            "year": int(y),
            "strategy": r6(row.iloc[STRATEGY_COL]),
            "allworld": r6(row[BENCHMARKS["allworld"]]),
            "sp500": r6(row[BENCHMARKS["sp500"]]),
            "world": r6(row[BENCHMARKS["world"]]),
            "excess": r6(row["vs All-World"]),
            "monthsCovered": len(in_year),
            "partial": len(in_year) < 12,
            "span": f"{pretty(in_year[0])} – {pretty(in_year[-1])}",
        })
    beat_years = sum(1 for y in years if (y["excess"] or 0) > 0)

    # Holding book, and the run-length ribbon used by the allocation strip.
    slot_names = {**FACTOR_NAMES, **SECTOR_NAMES}
    ret_by_month = {m: returns["strategy"][i] for i, m in enumerate(months)}
    holdings = []
    for period, row in book.iterrows():
        cash = bool(row["cash"])
        holdings.append({
            "m": str(period),
            "factor": None if cash else str(row["factor"]),
            "sector": None if cash else str(row["sector"]),
            "cash": cash,
            "ret": ret_by_month.get(str(period)),
            "priced": str(period) in observed,
        })

    def runs(field):
        """Merge consecutive months with the same holding into a single bar.

        Each run also carries the strategy's mean monthly return while that pick was
        in the book, so the ribbon can shade by outcome as well as show tenure.
        (The strategy holds both legs 50/50, so this is the whole book's return during
        the run, not an attribution to the one leg.)
        """
        out, cur = [], None
        for i, h in enumerate(holdings):
            slot = "CASH" if h["cash"] else h[field]
            if cur and cur["slot"] == slot:
                cur["end"] = i
                cur["months"] += 1
            else:
                cur = {"slot": slot, "start": i, "end": i, "months": 1}
                out.append(cur)
        for run in out:
            window = holdings[run["start"]: run["end"] + 1]
            vals = [h["ret"] for h in window if h["ret"] is not None]
            run["priced"] = len(vals)
            run["avgRet"] = r6(float(np.mean(vals))) if vals else None
            run["from"] = pretty(window[0]["m"])
            run["to"] = pretty(window[-1]["m"])
            run["name"] = "Cash" if run["slot"] == "CASH" else slot_names[run["slot"]]
        return out

    # Months each slot was actually held, counted over the priced record only.
    held_priced = {c: 0 for c in list(FACTOR_NAMES) + list(SECTOR_NAMES)}
    for h in holdings:
        if h["cash"] or not h["priced"]:
            continue
        held_priced[h["factor"]] += 1
        held_priced[h["sector"]] += 1

    factor_slots = list(FACTOR_NAMES.keys())
    sector_slots = list(SECTOR_NAMES.keys())
    tenure = {s: 0 for s in factor_slots + sector_slots}
    for h in holdings:
        if h["cash"]:
            continue
        tenure[h["factor"]] += 1
        tenure[h["sector"]] += 1

    lanes = [
        {
            "id": "factor",
            "title": "Factor leg",
            "subtitle": "8 MSCI factor indices",
            "slots": [{"code": c, "name": FACTOR_NAMES[c], "ticker": TRADABLE[c][2],
                       "isin": TRADABLE[c][0], "months": tenure[c]} for c in factor_slots],
            "runs": runs("factor"),
        },
        {
            "id": "sector",
            "title": "Sector leg",
            "subtitle": "7 S&P 500 sector indices",
            "slots": [{"code": c, "name": SECTOR_NAMES[c], "ticker": TRADABLE[c][2],
                       "isin": TRADABLE[c][0], "months": tenure[c]} for c in sector_slots],
            "runs": runs("sector"),
        },
    ]

    # Monthly returns heatmap: one row per calendar year, 12 columns.
    grid, ylist = [], sorted({int(m[:4]) for m in months})
    lookup = {m: returns["strategy"][i] for i, m in enumerate(months)}
    for y in ylist:
        cells = [lookup.get(f"{y}-{mm:02d}") for mm in range(1, 13)]
        ytd = None
        present = [c for c in cells if c is not None]
        if present:
            ytd = r6((np.prod([1 + c / 100 for c in present]) - 1) * 100)
        grid.append({"year": y, "cells": cells, "ytd": ytd})

    last = holdings[-1]
    current = {
        "asof": end,
        "asofPretty": pretty(end),
        "cash": last["cash"],
        "legs": [
            {"leg": "Factor", "code": last["factor"], "name": slot_names[last["factor"]],
             "isin": TRADABLE[last["factor"]][0], "lse": TRADABLE[last["factor"]][2],
             "xetra": TRADABLE[last["factor"]][1], "us": TRADABLE[last["factor"]][3],
             "weight": 50},
            {"leg": "Sector", "code": last["sector"], "name": slot_names[last["sector"]],
             "isin": TRADABLE[last["sector"]][0], "lse": TRADABLE[last["sector"]][2],
             "xetra": TRADABLE[last["sector"]][1], "us": TRADABLE[last["sector"]][3],
             "weight": 50},
        ],
    }

    book = build_book(current)

    payload = {
        "meta": {
            "name": "Lodestar",
            "strapline": "Factor and sector rotation",
            "start": start,
            "end": end,
            "startPretty": pretty(start),
            "endPretty": pretty(end),
            "months": len(months),
            "calendarMonths": len(calendar),
            "gaps": gaps,
            "riskFree": RISK_FREE * 100,
            "currency": "GBP",
            "basis": "Total return, net of index-level costs, gross of dealing costs and spread.",
        },
        "labels": LABELS,
        "order": ["strategy", "allworld", "sp500", "world"],
        "months": months,
        "monthsPretty": [pretty(m) for m in months],
        "t": t_index,
        "span": len(calendar) - 1,
        "calendar": calendar,
        "returns": returns,
        "stats": stats,
        "excess": {
            "monthly": [r6(v) for v in excess_m],
            "wins": wins,
            "losses": len(months) - wins,
            "winRate": r6(wins / len(months) * 100.0),
            "beatYears": beat_years,
            "totalYears": len(years),
            "avgMonthly": r6(float(excess_m.mean())),
        },
        "years": years,
        "heatmap": {"years": grid, "monthLabels": MONTH_ABBR},
        "holdings": holdings,
        "lanes": lanes,
        "current": current,
        "book": book,
        "universe": {
            "pricedMonths": len(months),
            "maxHeld": max(held_priced.values()),
            "styles": [{"key": k, "name": n} for k, n in STYLES],
            "factors": [{
                "code": c, "name": FACTOR_NAMES[c],
                "style": c.split("_", 1)[1], "region": REGION[c.split("_", 1)[0]],
                "isin": TRADABLE[c][0], "lse": TRADABLE[c][2], "xetra": TRADABLE[c][1],
                "held": held_priced[c], "heldAll": tenure[c],
                "live": c == last["factor"],
            } for c in factor_slots],
            "sectors": [{
                "code": c, "name": SECTOR_NAMES[c],
                "isin": TRADABLE[c][0], "lse": TRADABLE[c][2], "xetra": TRADABLE[c][1],
                "held": held_priced[c], "heldAll": tenure[c],
                "live": c == last["sector"],
            } for c in sector_slots],
        },
    }

    body = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    out = HERE / "data.js"
    out.write_text(
        "/* Generated by build_data.py. Do not edit by hand. */\n"
        f"const DATA = {body};\n",
        encoding="utf-8",
    )

    s = stats["strategy"]
    a = stats["allworld"]
    print(f"wrote {out}  ({out.stat().st_size/1024:.1f} KB)")
    print(f"  window        {start} .. {end}  ({len(months)} months)")
    print(f"  strategy      CAGR {s['cagr']:.2f}%  vol {s['vol']:.2f}%  Sharpe {s['sharpe']:.2f}  "
          f"Sortino {s['sortino']:.2f}  maxDD {s['maxdd']:.2f}%  x{s['growth']:.2f}")
    print(f"  all-world     CAGR {a['cagr']:.2f}%  vol {a['vol']:.2f}%  Sharpe {a['sharpe']:.2f}  "
          f"maxDD {a['maxdd']:.2f}%  x{a['growth']:.2f}")
    for k in ("sp500", "world"):
        st = stats[k]
        print(f"  {LABELS[k]:<13} CAGR {st['cagr']:.2f}%  Sharpe {st['sharpe']:.2f}  x{st['growth']:.2f}")
    print(f"  monthly wins  {wins}/{len(months)} = {wins/len(months)*100:.1f}%")
    print(f"  beat years    {beat_years}/{len(years)}")
    print(f"  cash months   {sum(1 for h in holdings if h['cash'])}")
    print(f"  current       {current['legs'][0]['name']} ({current['legs'][0]['lse']}) + "
          f"{current['legs'][1]['name']} ({current['legs'][1]['lse']})")
    if book is None:
        print("  look-through  none (data/holdings.json missing or incomplete) — section dropped")
    else:
        print(f"  look-through  as at {book['asOfPretty']}"
              + ("  !! STALE: holdings do not match the current legs" if book["stale"] else ""))
        for leg in book["legs"]:
            print(f"    {leg['leg']:<7} {leg['ticker']:<8} top10 {leg['top10']:.1f}% of fund, "
                  f"{leg['top10Book']:.1f}% of book")
        print("    " + " + ".join(f"{h['sym']} {h['bw']:.1f}%" for h in book["named"])
              + f" = {book['namedBook']:.1f}% of the book")


if __name__ == "__main__":
    main()

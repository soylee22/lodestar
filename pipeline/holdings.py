"""Top holdings and fundamentals for whichever two funds are currently held.

Refreshed with the rest of the monthly job. The two funds change when the signal
changes, so this is deliberately driven by data/signal.json rather than pinned to
any particular pair.

Everything is written into data/holdings.json and baked into the page at build
time. Nothing is fetched at page load: the site makes no external requests.
"""
import json, time
from pathlib import Path

import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"

FIELDS = ["shortName", "longName", "sector", "industry", "marketCap", "trailingPE",
          "forwardPE", "revenueGrowth", "earningsGrowth", "profitMargins",
          "dividendYield", "country", "website"]


def _retry(fn, n=3, wait=2):
    for i in range(n):
        try:
            out = fn()
            if out is not None:
                return out
        except Exception:
            pass
        time.sleep(wait)
    return None


def fund_holdings(ticker, top=10):
    def go():
        fd = yf.Ticker(ticker).funds_data
        th = fd.top_holdings
        if th is None or th.empty:
            return None
        rows = []
        for sym, r in th.head(top).iterrows():
            rows.append({"symbol": str(sym),
                         "name": str(r.get("Name") or sym),
                         "weight": float(r.get("Holding Percent") or 0.0)})
        return {"holdings": rows,
                "sector_weights": {k: float(v) for k, v in (fd.sector_weightings or {}).items() if v}}
    return _retry(go)


def fundamentals(symbol):
    def go():
        info = yf.Ticker(symbol).info
        if not info:
            return None
        out = {k: info.get(k) for k in FIELDS}
        for k in ("marketCap",):
            if out.get(k) is not None:
                out[k] = float(out[k])
        return out
    return _retry(go) or {}


def normalise_yields(rows):
    """yfinance reports dividendYield in PERCENT (Verizon 6.1 = 6.10%), unlike
    every other rate here, and it has flipped between percent and fraction
    across versions. Decide the unit from the SET, never per value: Micron
    genuinely yields 0.06%, so a per-row threshold would inflate it a
    hundredfold. If most payers look like fractions, scale the whole set.
    """
    vals = [r["dividend_yield"] for r in rows
            if r.get("dividend_yield") not in (None, 0)]
    if not vals:
        return rows, "percent"
    vals = sorted(float(v) for v in vals)
    median = vals[len(vals) // 2]
    if median < 0.25:                      # a 0.25% median payer is implausible
        for r in rows:
            if r.get("dividend_yield") is not None:
                r["dividend_yield"] = float(r["dividend_yield"]) * 100
        return rows, "percent (converted from fractions)"
    return rows, "percent"


def build(signal=None):
    signal = signal or json.loads((DATA / "signal.json").read_text())
    funds = {
        "factor": {"slot": signal["factor"], "ticker": signal["factor_ticker"]},
        "sector": {"slot": signal["sector"], "ticker": signal["sector_ticker"]},
    }
    out = {"as_at": signal["signal_month"], "legs": {}}
    for leg, meta in funds.items():
        t = meta["ticker"]
        print(f"  holdings {leg:7s} {t}")
        h = fund_holdings(t)
        if not h:
            print(f"  !! no holdings for {t}")
            out["legs"][leg] = {**meta, "holdings": [], "sector_weights": {}}
            continue
        enriched = []
        for row in h["holdings"]:
            f = fundamentals(row["symbol"])
            enriched.append({
                **row,
                "company": f.get("longName") or f.get("shortName") or row["name"],
                "sector": f.get("sector"),
                "industry": f.get("industry"),
                "market_cap": f.get("marketCap"),
                "pe": f.get("trailingPE"),
                "forward_pe": f.get("forwardPE"),
                "revenue_growth": f.get("revenueGrowth"),
                "earnings_growth": f.get("earningsGrowth"),
                "profit_margin": f.get("profitMargins"),
                "dividend_yield": f.get("dividendYield"),
            })
            print(f"     {row['symbol']:6s} {row['weight']*100:5.2f}%  "
                  f"PE {f.get('trailingPE')}")
        enriched, yield_unit = normalise_yields(enriched)
        print(f"     dividend yield unit: {yield_unit}")
        top_n = sum(r["weight"] for r in enriched)
        out["legs"][leg] = {**meta, "holdings": enriched, "yield_unit": yield_unit,
                            "sector_weights": h["sector_weights"],
                            "top10_weight": top_n}
    (DATA / "holdings.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\nwrote {DATA/'holdings.json'}")
    return out


if __name__ == "__main__":
    build()

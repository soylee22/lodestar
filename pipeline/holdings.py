"""Top holdings and fundamentals for whichever two funds are currently held.

Refreshed with the rest of the monthly job. The two funds change when the signal
changes, so this is deliberately driven by data/signal.json rather than pinned to
any particular pair.

Everything is written into data/holdings.json and baked into the page at build
time. Company icons are optional website favicons with ticker fallbacks.
"""
import json, time, math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
from pipeline import issuer_holdings, logos

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"

FIELDS = ["shortName", "longName", "sector", "industry", "marketCap", "trailingPE",
          "forwardPE", "revenueGrowth", "earningsGrowth", "profitMargins",
          "dividendYield", "dividendRate", "currentPrice", "regularMarketPrice", "country", "website"]


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
            weight = float(r.get('Holding Percent'))
            if not str(sym).strip() or not math.isfinite(weight) or not 0 <= weight <= 1:
                raise ValueError('Invalid Yahoo fund holding weight')
            rows.append({"symbol": str(sym),
                         "name": str(r.get("Name") or sym),
                         "weight": weight})
        if sum(row['weight'] for row in rows) > 1.000001:
            raise ValueError('Yahoo fund holding coverage exceeds 100%')
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


def dividend_yield_percent(info):
    """Use annual dividend rate / quote price, without guessing a feed's unit."""
    try:
        rate = float(info['dividendRate'])
        price = float(info.get('currentPrice') or info.get('regularMarketPrice'))
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(rate) or not math.isfinite(price) or rate < 0 or price <= 0:
        return None
    return rate / price * 100


def annotate_sources(out):
    """Separate dated holdings from company figures retrieved from Yahoo."""
    for leg in out['legs'].values():
        issuer = bool(leg.get('all_holdings'))
        leg['source'] = 'iShares dated equity holdings' if issuer else 'Yahoo Finance top-ten holdings'
        leg['fundamentals_source'] = 'Yahoo Finance'
        leg['fundamentals_fetched_at'] = out.get('fetched_at')
    sources = {leg['source'] for leg in out['legs'].values()}
    dates = {leg.get('snapshot_date') for leg in out['legs'].values()}
    out['source'] = next(iter(sources)) if len(sources) == 1 else 'Mixed holdings sources. See each leg.'
    out['snapshot_date'] = next(iter(dates)) if len(dates) == 1 else None
    return out


def build(signal=None):
    signal = signal or json.loads((DATA / "signal.json").read_text())
    funds = {
        "factor": {"slot": signal["factor"], "ticker": signal["factor_ticker"]},
        "sector": {"slot": signal["sector"], "ticker": signal["sector_ticker"]},
    }
    out = {"as_at": signal["signal_month"],
           "fetched_at": datetime.now(timezone.utc).isoformat(),
           "snapshot_date": None, "source": "Yahoo Finance top-ten holdings", "legs": {}}
    for leg, meta in funds.items():
        t = meta["ticker"]
        print(f"  holdings {leg:7s} {t}")
        issuer = None
        try:
            issuer = issuer_holdings.fetch(t, signal["signal_month"])
        except Exception as exc:
            print(f"  !! issuer holdings unavailable for {t}: {exc}; using top-ten disclosure")
        if issuer:
            sectors = {}
            for row in issuer['holdings']:
                key = row.get('sector') or 'Other'
                sectors[key] = sectors.get(key, 0) + row['weight']
            h = {'holdings': issuer['holdings'][:10], 'sector_weights': sectors}
        else:
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
                "sector": row.get("sector") or f.get("sector"),
                "website": f.get("website"),
                "industry": f.get("industry"),
                "market_cap": f.get("marketCap"),
                "pe": f.get("trailingPE"),
                "forward_pe": f.get("forwardPE"),
                "revenue_growth": f.get("revenueGrowth"),
                "earnings_growth": f.get("earningsGrowth"),
                "profit_margin": f.get("profitMargins"),
                "dividend_yield": dividend_yield_percent(f),
            })
            print(f"     {row['symbol']:6s} {row['weight']*100:5.2f}%  "
                  f"PE {f.get('trailingPE')}")
        yield_unit = 'percent. Annual dividend rate / quote price'
        top_n = sum(r["weight"] for r in enriched)
        out["legs"][leg] = {**meta, "holdings": enriched, "yield_unit": yield_unit,
                            "sector_weights": h["sector_weights"],
                            "top10_weight": top_n}
        if issuer:
            # Existing fundamentals cards retain their own top-ten disclosure.
            websites = {r['symbol']: r.get('website') for r in enriched}
            for row in issuer['holdings']:
                row['website'] = websites.get(row['symbol'])
            out['legs'][leg].update(all_holdings=issuer['holdings'],
                                    snapshot_date=issuer['snapshot_date'],
                                    source_url=issuer['source_url'],
                                    net_disclosed_weight=issuer['net_disclosed_weight'],
                                    disclosure_rounding_tolerance=issuer['disclosure_rounding_tolerance'])
            print(f"     issuer look-through: {len(issuer['holdings'])} equities at {issuer['snapshot_date']}")
    annotate_sources(out)
    try:
        positions = [row for key, leg in out['legs'].items()
                     if key == 'sector' or leg['slot'].startswith('USA_')
                     for row in leg.get('all_holdings') or leg.get('holdings') or []]
        logos.refresh(positions)
    except Exception as exc:
        print(f'  !! optional logos unavailable: {exc}')
    (DATA / "holdings.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\nwrote {DATA/'holdings.json'}")
    return out


if __name__ == "__main__":
    build()

"""Lodestar: fetch, compute the signal, and rebuild the track.

Self-contained so a monthly GitHub Action can run it from a clean checkout.

The rule, in full:
  * 15 indices: 8 MSCI factor + 7 S&P 500 sector.
  * At each month-end close, score every index by its price return over the
    trailing 8 months, on its own published currency levels.
  * Hold the top of each basket, 50/50.
  * Both legs go to cash if the S&P 500 sits more than 25% below its level
    19 months earlier.
  * Reset to 50/50 only when a leg changes; otherwise let the split drift.
  * Signals come from the indices. Returns come from the London-listed UCITS
    lines, in sterling, total return.
"""
import json, time, urllib.request
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
DATA.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

LOOKBACK, SKIP = 8, 0
CASH_LOOKBACK, CASH_THRESHOLD = 19, -0.25

FACTOR = {"USA_MOM": "703025", "USA_QUAL": "702789", "USA_VAL": "705973",
          "USA_SMALL": "106229", "EUR_MOM": "703764", "EUR_QUAL": "702790",
          "EUR_VAL": "705382", "EUR_SMALL": "106233"}
SECTOR_YF = {"CONS_DISC": "^SP500-25", "CONS_STAP": "^SP500-30", "ENERGY": "^GSPE",
             "HEALTH": "^SP500-35", "INDUST": "^SP500-20", "INFOTECH": "^SP500-45",
             "MATERIALS": "^SP500-15"}
LSE = {"USA_MOM": "IUMO.L", "USA_QUAL": "IUQA.L", "USA_VAL": "IUVL.L",
       "USA_SMALL": "CUSS.L", "EUR_MOM": "IEMO.L", "EUR_QUAL": "IEQU.L",
       "EUR_VAL": "IEVL.L", "EUR_SMALL": "XXSC.L", "CONS_DISC": "IUCD.L",
       "CONS_STAP": "IUCS.L", "ENERGY": "IUES.L", "HEALTH": "IHCU.L",
       "INDUST": "IUIS.L", "INFOTECH": "IUIT.L", "MATERIALS": "IUMS.L"}
BENCH = {"FTSE All-World": ("VWRL.L", "GBP"), "S&P 500": ("VUSA.L", "GBP"),
         "MSCI World": ("IWDA.L", "USD")}
FACTOR_SLOTS, SECTOR_SLOTS = list(FACTOR), list(SECTOR_YF)


def _retry(fn, n=2, wait=3):
    last = None
    for _ in range(n):
        try:
            out = fn()
            if out is not None:
                return out
        except Exception as e:
            last = e
        time.sleep(wait)
    raise RuntimeError(f"failed after {n} attempts: {last}")


def msci(code, currency):
    def go():
        url = ("https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph"
               f"?currency_symbol={currency}&index_variant=STRD&start_date=19981231"
               f"&end_date=20991231&data_frequency=END_OF_MONTH&index_codes={code}")
        raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25).read()
        lv = json.loads(raw)["indexes"]["INDEX_LEVELS"]
        if not lv:
            return None
        s = pd.Series({pd.Timestamp(str(x["calc_date"])): x["level_eod"] for x in lv}).sort_index()
        s.index = s.index.to_period("M")
        return s
    return _retry(go)


def yahoo_monthly(sym, adjusted, start="1998-11-01"):
    def go():
        d = yf.Ticker(sym).history(start=start, interval="1d", auto_adjust=adjusted)["Close"]
        if d.empty:
            return None
        d.index = pd.to_datetime(d.index).tz_localize(None)
        m = d.resample("ME").last().dropna()
        m.index = m.index.to_period("M")
        # the current month has not ended: its "month-end" is just today's close
        return m[m.index < pd.Timestamp.today().to_period("M")]
    return _retry(go)


def currency_of(sym):
    def go():
        c = yf.Ticker(sym).fast_info.get("currency")
        return c or None
    try:
        return _retry(go, n=3, wait=2)
    except Exception:
        return "GBP"


PANEL_CSV = DATA / "panel_local.csv"


def build_signal_panel():
    """Index levels in each index's own published currency: USA and sectors in
    USD, Europe in EUR.

    The panel is cached in the repo and refreshed incrementally. MSCI's public
    endpoint is intermittently unreachable, so a failed fetch must never be
    allowed to silently produce a stale or partial signal: we fall back to the
    cache and mark the result stale, and the caller refuses to publish a signal
    whose newest month is older than the month just ended.
    """
    cached = None
    if PANEL_CSV.is_file():
        cached = pd.read_csv(PANEL_CSV, index_col=0)
        cached.index = pd.PeriodIndex(cached.index, freq="M")
        print(f"  cache: {len(cached)} months to {cached.index.max()}")

    fresh, failures, msci_down = {}, [], False
    for name, code in FACTOR.items():
        if msci_down:
            failures.append(f"MSCI {name}: skipped (endpoint down)")
            continue
        try:
            fresh[name] = msci(code, "EUR" if name.startswith("EUR_") else "USD")
            print(f"  msci  {name:10s} n={len(fresh[name])}")
        except Exception as e:
            failures.append(f"MSCI {name}: {type(e).__name__}")
            if not fresh:
                msci_down = True          # first one failed: stop hammering
                print("  !! MSCI endpoint unreachable, skipping remaining factor fetches")
    for name, sym in SECTOR_YF.items():
        try:
            fresh[name] = yahoo_monthly(sym, adjusted=False)
            print(f"  sp500 {name:10s} n={len(fresh[name])}")
        except Exception as e:
            failures.append(f"Yahoo {name}: {type(e).__name__}")

    if failures:
        print(f"  !! {len(failures)} source(s) unavailable: {'; '.join(failures[:4])}")
    if cached is None and failures:
        raise RuntimeError("no cached panel and live fetch failed: " + "; ".join(failures))

    panel = cached.copy() if cached is not None else pd.DataFrame()
    for name, s in fresh.items():
        panel = panel.reindex(panel.index.union(s.index)) if len(panel) else pd.DataFrame(index=s.index)
        panel[name] = s.reindex(panel.index).combine_first(panel[name]) if name in panel else s.reindex(panel.index)
    panel = panel[list(FACTOR) + list(SECTOR_YF)].sort_index().dropna(how="all")
    panel.to_csv(PANEL_CSV)
    # a signal needs every one of the fifteen levels; partial months are unusable
    complete = panel.dropna(how="any")
    if len(complete) < len(panel):
        print(f"  dropping {len(panel)-len(complete)} incomplete month(s) from the signal panel")
    return complete, failures


def compute_book(panel, spx):
    """Month-by-month holding, from month-end signals."""
    mom = (panel.shift(SKIP) / panel.shift(SKIP + LOOKBACK) - 1) if SKIP else (panel / panel.shift(LOOKBACK) - 1)
    fpick = mom[FACTOR_SLOTS].idxmax(axis=1)
    spick = mom[SECTOR_SLOTS].idxmax(axis=1)
    cash = (spx / spx.shift(CASH_LOOKBACK) - 1) < CASH_THRESHOLD
    rows = {}
    idx = panel.index
    for i in range(1, len(idx)):
        prev, now = idx[i - 1], idx[i]
        f, s = fpick.get(prev), spick.get(prev)
        if not isinstance(f, str) or not isinstance(s, str):
            continue
        rows[now] = {"factor": f, "sector": s, "cash": bool(cash.get(prev, False))}
    return pd.DataFrame(rows).T, mom, fpick, spick, cash


def gbp_panel(symbols):
    fx = {}
    for pair, cur in (("USDGBP=X", "USD"), ("EURGBP=X", "EUR")):
        fx[cur] = yahoo_monthly(pair, adjusted=False, start="2013-01-01")
    out = {}
    for key, sym in symbols.items():
        m = yahoo_monthly(sym, adjusted=True, start="2013-01-01")
        cur = currency_of(sym)
        if cur == "GBp":
            m = m / 100.0
        elif cur in ("USD", "EUR"):
            m = (m * fx[cur].reindex(m.index)).dropna()
        out[key] = m
        print(f"  lse   {key:14s} {sym:8s} {cur}")
    return pd.DataFrame(out), fx


def build_track(book, funds, bench_rets):
    """Rule D: reset to 50/50 only when a leg changes; otherwise let it drift."""
    r = funds.pct_change() * 100
    wf, prev, rows = 0.5, None, {}
    for m in book.index:
        row = book.loc[m]
        if m not in r.index:
            continue
        if bool(row["cash"]):
            rows[m] = 0.0; wf, prev = 0.5, ("CASH", "CASH"); continue
        f, s = row["factor"], row["sector"]
        if f not in r.columns or s not in r.columns:
            continue
        rf, rs = r.at[m, f], r.at[m, s]
        if pd.isna(rf) or pd.isna(rs):
            continue
        if prev is None or f != prev[0] or s != prev[1]:
            wf = 0.5
        tot = wf * (1 + rf / 100) + (1 - wf) * (1 + rs / 100)
        rows[m] = (tot - 1) * 100
        wf = wf * (1 + rf / 100) / tot
        prev = (f, s)
    track = pd.Series(rows).rename("Lodestar")
    out = pd.concat([track, bench_rets.reindex(track.index)], axis=1).dropna()
    return out


def main():
    print("fetching signal panel...")
    panel, failures = build_signal_panel()
    spx = yahoo_monthly("^GSPC", adjusted=False)
    print("computing book...")
    book, mom, fpick, spick, cash = compute_book(panel, spx)
    print("fetching London lines...")
    funds, fx = gbp_panel(LSE)
    bench = {}
    for label, (sym, cur) in BENCH.items():
        m = yahoo_monthly(sym, adjusted=True, start="2013-01-01")
        if cur == "USD":
            m = (m * fx["USD"].reindex(m.index)).dropna()
        bench[label] = m.pct_change() * 100
    track = build_track(book, funds, pd.DataFrame(bench))

    book.to_csv(DATA / "final_book.csv")
    out = track.rename(columns={"Lodestar": "MarketFighter (recovered, LSE)"})
    out.to_csv(DATA / "final_tradable.csv")

    yr = pd.Series(pd.PeriodIndex(out.index).year, index=out.index)
    ann = out.groupby(yr).apply(lambda g: (np.prod(1 + g / 100) - 1) * 100)
    ann["vs All-World"] = ann.iloc[:, 0] - ann["FTSE All-World"]
    ann.to_csv(DATA / "final_tradable_annual.csv")

    last = panel.index[-1]
    expected = (pd.Timestamp.today().to_period("M") - 1)
    stale = last < expected
    if stale:
        print(f"  !! panel newest month is {last}, expected {expected} -- signal NOT fresh")
    signal = {
        "signal_month": str(last),
        "factor": str(fpick.loc[last]), "sector": str(spick.loc[last]),
        "cash": bool(cash.get(last, False)),
        "factor_ticker": LSE[str(fpick.loc[last])], "sector_ticker": LSE[str(spick.loc[last])],
        "factor_table": {k: float(mom.loc[last, k]) for k in FACTOR_SLOTS},
        "sector_table": {k: float(mom.loc[last, k]) for k in SECTOR_SLOTS},
        "previous": {"factor": str(book.iloc[-1]["factor"]), "sector": str(book.iloc[-1]["sector"])},
        "stale": bool(stale), "expected_month": str(expected),
        "source_failures": failures,
    }
    (DATA / "signal.json").write_text(json.dumps(signal, indent=2))
    print(f"\nsignal at {last}: {signal['factor']} ({signal['factor_ticker']}) + "
          f"{signal['sector']} ({signal['sector_ticker']}){'  [CASH]' if signal['cash'] else ''}")
    print(f"track: {len(track)} months to {track.index[-1]}")
    return signal


if __name__ == "__main__":
    main()

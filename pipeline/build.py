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
from functools import lru_cache
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf
import exchange_calendars as xcals

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
       "USA_SMALL": "CUSS.L", "EUR_MOM": "IEFM.L", "EUR_QUAL": "IEQU.L",
       "EUR_VAL": "IEVL.L", "EUR_SMALL": "XXSC.L", "CONS_DISC": "IUCD.L",
       "CONS_STAP": "IUCS.L", "ENERGY": "IUES.L", "HEALTH": "IHCU.L",
       "INDUST": "IUIS.L", "INFOTECH": "IUIT.L", "MATERIALS": "IUMS.L"}
BENCH = {"FTSE All-World": ("VWRL.L", "GBP"), "S&P 500": ("VUSA.L", "GBP"),
         "MSCI World": ("IWDA.L", "USD")}

# Fallback for the factor leg when MSCI's endpoint is unreachable. These are the
# US-listed, DISTRIBUTING factor funds, so their unadjusted close is a price
# series, like the index. The three Europe slots map to International ex-US funds
# (the substitution the strategy's originator uses himself), so they are the
# least faithful part of the fallback -- acceptable because the Europe slots
# rarely win, and the whole fallback is flagged wherever it is used.
FACTOR_PROXY = {"USA_MOM": "MTUM", "USA_QUAL": "QUAL", "USA_VAL": "VLUE",
                "USA_SMALL": "SMLF", "EUR_MOM": "IMTM", "EUR_QUAL": "IQLT",
                "EUR_VAL": "IVLU", "EUR_SMALL": "IEUS"}

# Fallback for the sector leg when Yahoo's S&P GICS index series stop updating.
# The SPDR Select Sector funds track the same seven GICS sectors, and they
# distribute, so their unadjusted close is a price series like the index. The
# whole leg is recomputed from the proxies, never spliced slot by slot: a
# ranking built half on the index and half on a proxy is not a ranking.
SECTOR_PROXY = {"CONS_DISC": "XLY", "CONS_STAP": "XLP", "ENERGY": "XLE",
                "HEALTH": "XLV", "INDUST": "XLI", "INFOTECH": "XLK",
                "MATERIALS": "XLB"}
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


@lru_cache(maxsize=2)
def exchange_calendar(name):
    return xcals.get_calendar(name, start="1998-01-01", end=str(pd.Timestamp.today().year + 2) + "-12-31")


def completed_months(d, sym, today=None):
    """Keep actual final-session closes, never an earlier print or a partial month."""
    today = pd.Timestamp.today() if today is None else pd.Timestamp(today)
    d = d.dropna().copy()
    d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
    groups = d.groupby(d.index.to_period("M"))
    rows = {}
    for month, prices in groups:
        if month >= today.to_period("M"):
            continue
        if sym.endswith("=X"):
            # FX has no single stock-exchange calendar. Only accept a weekday close.
            last = pd.bdate_range(month.start_time, month.end_time)[-1]
        else:
            cal = exchange_calendar("XLON" if sym.endswith(".L") else "XNYS")
            last = cal.sessions_in_range(month.start_time.normalize(), month.end_time.normalize())[-1]
        if prices.index[-1] == last:
            rows[month] = float(prices.iloc[-1])
    return pd.Series(rows, dtype=float).sort_index()


def yahoo_monthly(sym, adjusted, start="1998-11-01"):
    def go():
        d = yf.Ticker(sym).history(start=start, interval="1d", auto_adjust=adjusted)["Close"]
        if d.empty:
            return None
        months = completed_months(d, sym)
        expected = pd.Timestamp.today().to_period("M") - 1
        if expected not in months.index:
            # Yahoo's long-range response can omit the newest London close.
            # Make a separate short-window request with the SAME adjustment
            # convention. Never substitute raw closes for total-return prices.
            recent_start = str((expected - 2).start_time.date())
            try:
                recent = yf.Ticker(sym).history(start=recent_start, interval="1d",
                                              auto_adjust=adjusted)["Close"]
                months = months.combine_first(completed_months(recent, sym))
            except Exception as exc:
                print(f"  !! final-session retry failed for {sym}: {type(exc).__name__}")
        if expected not in months.index:
            print(f"::warning::Missing verified {expected} final-session close: {sym}, adjusted={adjusted}")
        return months
    return _retry(go)


def currency_of(sym):
    def go():
        c = yf.Ticker(sym).fast_info.get("currency")
        return c or None
    try:
        return _retry(go, n=3, wait=2)
    except Exception as exc:
        raise RuntimeError(f"currency unavailable for {sym}") from exc


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
    # never persist a month that has not ended: a partial month would be locked
    # into the cache and could not be corrected on a later run
    panel = panel[panel.index < pd.Timestamp.today().to_period("M")]
    panel.to_csv(PANEL_CSV)
    # a signal needs every one of the fifteen levels; partial months are unusable
    complete = panel.dropna(how="any")
    if len(complete) < len(panel):
        print(f"  dropping {len(panel)-len(complete)} incomplete month(s) from the signal panel")
    # the raw panel goes back too: the two legs have different sources, so a month
    # unusable for one of them may be perfectly good for the other
    return complete, panel, failures


def proxy_panel(proxies):
    """Keep a whole leg in its own price history, without index/proxy splicing."""
    cols = {}
    for slot, sym in proxies.items():
        cols[slot] = yahoo_monthly(sym, adjusted=False, start="2015-01-01")
    return pd.DataFrame(cols)


def proxy_momentum(proxies, month):
    mom = _momentum(proxy_panel(proxies))
    if month not in mom.index or mom.loc[month].isna().any():
        raise RuntimeError(f"proxy has no complete momentum for {month}")
    return mom.loc[month]


def proxy_factor_momentum(month):
    return proxy_momentum(FACTOR_PROXY, month)


def proxy_sector_momentum(month):
    return proxy_momentum(SECTOR_PROXY, month)


def _momentum(px):
    # Eight calendar months, even when a source omits an intermediate month.
    px = px.reindex(pd.period_range(px.index.min(), px.index.max(), freq="M"))
    m = (px.shift(SKIP) / px.shift(SKIP + LOOKBACK) - 1) if SKIP else (px / px.shift(LOOKBACK) - 1)
    # the first LOOKBACK months have no trailing window; an all-NA row is an
    # error for idxmax on newer pandas, so drop those rows rather than rank them
    return m.replace([np.inf, -np.inf], np.nan).dropna(how="any")


def compute_book(raw, spx, history=None):
    """Month-by-month holding, from month-end signals.

    Each basket is ranked on its own completeness. The two come from different
    sources -- MSCI for the factors, S&P via Yahoo for the sectors -- and they
    fail separately, so requiring all fifteen levels in a month would discard a
    good basket alongside a missing one.
    """
    fmom = _momentum(raw[FACTOR_SLOTS])
    smom = _momentum(raw[SECTOR_SLOTS])
    fpick, spick = fmom.idxmax(axis=1), smom.idxmax(axis=1)
    spx = spx.reindex(pd.period_range(spx.index.min(), spx.index.max(), freq="M"))
    cash_mom = (spx / spx.shift(CASH_LOOKBACK) - 1).dropna()
    cash = cash_mom < CASH_THRESHOLD
    sig = sorted(set(fpick.index) & set(spick.index))
    # A month's holding was set by the previous signal, so the month after the
    # last signal is already determined and belongs in the book. Without this the
    # page loses a month whenever a source is late, even though nothing about
    # what is held that month is in doubt.
    rows = {}
    for month in sig:
        rows[month + 1] = {"factor": fpick[month], "sector": spick[month],
                           "cash": bool(cash.get(month, False)),
                           "factor_source": "MSCI", "sector_source": "S&P index"}
    # Published signals remain fixed when an index feed later returns or backfills.
    for month, row in (history or {}).items():
        rows[pd.Period(month, "M") + 1] = {k: row[k] for k in
            ("factor", "sector", "cash", "factor_source", "sector_source")}
    return pd.DataFrame(rows).T.sort_index(), fmom, smom, fpick, spick, cash


def resolve_history(raw, spx, history, expected, panels=None):
    """Fill recent missing signals before building returns from those holdings."""
    _, fmom, smom, _, _, cash = compute_book(raw, spx)
    history = dict(history)
    last_primary = min(fmom.index.max(), smom.index.max())
    pending = [m for m in pd.period_range(last_primary + 1, expected, freq="M")
               if str(m) not in history]
    needed = {leg for leg, mom in (("factor", fmom), ("sector", smom))
              if any(m not in mom.index for m in pending)}
    proxies = {}
    for leg in needed:
        panel = (panels or {}).get(leg)
        if panel is None:
            panel = proxy_panel(FACTOR_PROXY if leg == "factor" else SECTOR_PROXY)
        proxies[leg] = _momentum(panel)
    # Also archive each new complete primary signal, so later revisions cannot change it.
    if str(expected) not in history and expected not in pending:
        pending.append(expected)
    for month in pending:
        entry = {"provenance": "Computed at month end" if month == expected else "Reconstructed ETF fallback"}
        for leg, primary, source in (("factor", fmom, "MSCI"), ("sector", smom, "S&P index")):
            mom = primary if month in primary.index else proxies.get(leg)
            if mom is None or month not in mom.index:
                raise RuntimeError(f"no complete {leg} ranking for {month}")
            table = mom.loc[month]
            entry[leg] = str(table.idxmax())
            entry[leg + "_source"] = source if month in primary.index else "ETF proxy"
            entry[leg + "_table"] = {k: float(v) for k, v in table.items()}
        if month not in cash.index:
            raise RuntimeError(f"cash-rule price window unavailable for {month}")
        entry["cash"] = bool(cash[month])
        history[str(month)] = entry
    if str(expected) not in history:
        raise RuntimeError(f"no current signal for {expected}")
    return history


def gbp_panel(symbols):
    fx = {}
    for pair, cur in (("USDGBP=X", "USD"), ("EURGBP=X", "EUR")):
        fx[cur] = yahoo_monthly(pair, adjusted=False, start="2013-01-01")
    out = {}
    for key, sym in symbols.items():
        try:
            m = yahoo_monthly(sym, adjusted=True, start="2013-01-01")
            cur = currency_of(sym)
        except Exception as exc:
            print(f"  !! London line unavailable: {sym} ({exc}); using confirmed cache if present")
            continue
        if cur == "GBp":
            m = m / 100.0
        elif cur in ("USD", "EUR"):
            m = (m * fx[cur].reindex(m.index)).dropna()
        out[key] = m
        print(f"  lse   {key:14s} {sym:8s} {cur}")
    fresh = pd.DataFrame(out)
    cache_path = DATA / "fund_prices_gbp.csv"
    if cache_path.exists():
        cached = pd.read_csv(cache_path, index_col=0)
        cached.index = pd.PeriodIndex(cached.index, freq="M")
        fresh = fresh.combine_first(cached)
    fresh = fresh.reindex(columns=list(symbols)).sort_index()
    fresh.to_csv(cache_path)
    return fresh, fx


def build_track(book, funds, bench_rets):
    """Rule D: reset to 50/50 only when a leg changes; otherwise let it drift."""
    r = funds.pct_change(fill_method=None) * 100
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


def append_track(recorded, fresh):
    """Append consecutive complete months. Keep published history and gaps fixed."""
    expected = recorded.index.max() + 1
    additions = []
    for month in fresh.index[fresh.index > recorded.index.max()]:
        if month != expected:
            break
        additions.append(month)
        expected += 1
    return pd.concat([recorded, fresh.loc[additions]]).sort_index()


def main():
    print("fetching signal panel...")
    _, raw, failures = build_signal_panel()
    spx = yahoo_monthly("^GSPC", adjusted=False)
    print("computing book...")
    expected = pd.Timestamp.today().to_period("M") - 1
    history_path = DATA / "signal_history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else {}
    history = resolve_history(raw, spx, history, expected)
    book, fmom, smom, fpick, spick, cash = compute_book(raw, spx, history)
    print("fetching London lines...")
    funds, fx = gbp_panel(LSE)
    bench, bench_prices = {}, {}
    for label, (sym, cur) in BENCH.items():
        m = yahoo_monthly(sym, adjusted=True, start="2013-01-01")
        if cur == "USD":
            m = (m * fx["USD"].reindex(m.index)).dropna()
        bench_prices[label] = m
        bench[label] = m.pct_change(fill_method=None) * 100
    track = build_track(book, funds, pd.DataFrame(bench))
    record_path = DATA / "track_record.csv"
    if record_path.exists():
        recorded = pd.read_csv(record_path, index_col=0)
        recorded.index = pd.PeriodIndex(recorded.index, freq="M")
        # Completed, published returns stay fixed. Only append newly priced months.
        track = append_track(recorded, track)
    track.to_csv(record_path)

    held = book.loc[expected]
    inputs = []
    if not bool(held["cash"]):
        for leg in ("factor", "sector"):
            slot = held[leg]
            for month in (expected - 1, expected):
                value = funds.at[month, slot] if month in funds.index and slot in funds else np.nan
                inputs.append({"ticker": LSE[slot], "month": str(month),
                               "available": bool(pd.notna(value)), "basis": "GBP adjusted close"})
    for label, prices in bench_prices.items():
        for month in (expected - 1, expected):
            inputs.append({"ticker": BENCH[label][0], "month": str(month),
                           "available": bool(month in prices.index and pd.notna(prices.get(month))),
                           "basis": "GBP adjusted close"})
    price_status = {"expected": str(expected), "performanceThrough": str(track.index[-1]),
                    "inputs": inputs, "missing": [r for r in inputs if not r["available"]]}
    (DATA / "price_status.json").write_text(json.dumps(price_status, indent=2) + "\n")
    if track.index[-1] < expected:
        print(f"::warning::Performance incomplete: through {track.index[-1]}, expected {expected}")

    book.to_csv(DATA / "final_book.csv")
    out = track.rename(columns={"Lodestar": "MarketFighter (recovered, LSE)"})
    out.to_csv(DATA / "final_tradable.csv")

    yr = pd.Series(pd.PeriodIndex(out.index).year, index=out.index)
    # compound each column within each year, explicitly per column: a frame-wide
    # np.prod would collapse every column into one scalar
    ann = pd.DataFrame({c: out[c].groupby(yr).apply(lambda s: ((1 + s / 100).prod() - 1) * 100)
                        for c in out.columns})
    ann["vs All-World"] = ann[out.columns[0]] - ann["FTSE All-World"]
    ann.to_csv(DATA / "final_tradable_annual.csv")

    row = history[str(expected)]
    held = book.loc[expected]
    signal = {
        "signal_month": str(expected), "holding_month": str(expected + 1),
        "factor_source": row["factor_source"], "sector_source": row["sector_source"],
        "factor": row["factor"], "sector": row["sector"], "cash": row["cash"],
        "factor_ticker": LSE[row["factor"]], "sector_ticker": LSE[row["sector"]],
        "factor_table": row.get("factor_table", {}), "sector_table": row.get("sector_table", {}),
        "sector_month": str(expected), "previous": {
            "factor": str(held["factor"]), "sector": str(held["sector"]), "cash": bool(held["cash"])},
        "stale": False, "expected_month": str(expected), "source_failures": failures,
        "performance_month": str(track.index[-1]),
    }
    # Persist the same source-labelled signal history used by the return book.
    history_path.write_text(json.dumps(history, indent=2, allow_nan=False) + "\n")
    (DATA / "signal.json").write_text(json.dumps(signal, indent=2))
    print(f"\nsignal at {signal['signal_month']}: {signal['factor']} ({signal['factor_ticker']}) [{row['factor_source']}] + "
          f"{signal['sector']} ({signal['sector_ticker']}) [{row['sector_source']}]"
          f"{'  [CASH]' if signal['cash'] else ''}")
    print(f"track: {len(track)} months to {track.index[-1]}")
    return signal


if __name__ == "__main__":
    main()

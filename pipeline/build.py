"""Lodestar: fetch, compute the signal, and rebuild the track.

Self-contained so a monthly GitHub Action can run it from a clean checkout.

The rule, in full:
  * 15 indices: 8 MSCI factor + 7 S&P 500 sector.
  * At each month-end close, score every index by its price return over the
    trailing 8 months, on its own published currency levels.
  * Hold the top of each basket, 50/50.
  * Canary gate: evaluate HAA's TIP 1-3-6-12 score in USD total return at each
    month-end. If TIP <= 0%, exit both equity legs and hold 100% in whichever
    of CBU0.L (IEF) or IB01.L (BIL) has higher USD 1-3-6-12 momentum.
  * Reset to 50/50 only when equity holdings change; otherwise let drift.
  * Signals come from the indices (and US Treasury ETFs for the canary).
    Returns come from London-listed UCITS lines, in sterling, total return.
"""
import json, time, urllib.request
from functools import lru_cache
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf
import exchange_calendars as xcals

from universe import DEFENSIVE_NAMES, DEFENSIVE_TRADABLE
from pipeline.crash_protection import evaluate_canary_history

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
DATA.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

RULE_VERSION = "tip-canary-v1"
LOOKBACK, SKIP = 8, 0
# Legacy MarketFighter comparator threshold, kept solely for the classic comparison track.
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
DEFENSIVE_LSE = {slot: meta[2] for slot, meta in DEFENSIVE_TRADABLE.items()}
BENCH = {"FTSE All-World": ("VWRL.L", "GBP"), "S&P 500": ("VUSA.L", "GBP"),
         "MSCI World": ("IWDA.L", "USD")}

# Fallback for the factor leg when MSCI's endpoint is unreachable.
FACTOR_PROXY = {"USA_MOM": "MTUM", "USA_QUAL": "QUAL", "USA_VAL": "VLUE",
                "USA_SMALL": "SMLF", "EUR_MOM": "IMTM", "EUR_QUAL": "IQLT",
                "EUR_VAL": "IVLU", "EUR_SMALL": "IEUS"}

# Fallback for the sector leg when Yahoo's S&P GICS index series stop updating.
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
    """Index levels in each index's own published currency: USA and sectors in USD, Europe in EUR."""
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
                msci_down = True
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
    panel = panel[panel.index < pd.Timestamp.today().to_period("M")]
    panel.to_csv(PANEL_CSV)
    complete = panel.dropna(how="any")
    if len(complete) < len(panel):
        print(f"  dropping {len(panel)-len(complete)} incomplete month(s) from the signal panel")
    return complete, panel, failures


def canary_panel():
    """Fetch USD total-return monthly closes for TIP, IEF and BIL."""
    cols = {}
    for sym in ("TIP", "IEF", "BIL"):
        cols[sym] = yahoo_monthly(sym, adjusted=True, start="2002-01-01")
    return pd.DataFrame(cols).sort_index()


def proxy_panel(proxies):
    """Keep a whole leg in its own price history, without index/proxy splicing."""
    cols = {}
    for slot, sym in proxies.items():
        cols[slot] = yahoo_monthly(sym, adjusted=False, start="2015-01-01")
    return pd.DataFrame(cols)


def _momentum(px):
    px = px.reindex(pd.period_range(px.index.min(), px.index.max(), freq="M"))
    m = (px.shift(SKIP) / px.shift(SKIP + LOOKBACK) - 1) if SKIP else (px / px.shift(LOOKBACK) - 1)
    return m.replace([np.inf, -np.inf], np.nan).dropna(how="any")


def compute_book(raw, canary, history=None):
    """Month-by-month holding, applying the TIP 1-3-6-12 canary in USD total return."""
    fmom = _momentum(raw[FACTOR_SLOTS])
    smom = _momentum(raw[SECTOR_SLOTS])
    fpick, spick = fmom.idxmax(axis=1), smom.idxmax(axis=1)
    cstate = evaluate_canary_history(canary["TIP"], canary["IEF"], canary["BIL"])
    sig = sorted(set(fpick.index) & set(spick.index))

    rows = {}
    for month in sig:
        c_row = cstate.loc[month] if month in cstate.index else None
        is_def = bool(c_row["cash"]) if c_row is not None else False
        rows[month + 1] = {
            "factor": fpick[month],
            "sector": spick[month],
            "cash": is_def,
            "defensive": c_row["defensive"] if (c_row is not None and is_def) else None,
            "factor_source": "MSCI",
            "sector_source": "S&P index",
        }
    for month, row in (history or {}).items():
        is_def = bool(row["cash"])
        def_slot = row.get("defensive") if is_def else None
        rows[pd.Period(month, "M") + 1] = {
            "factor": row["factor"],
            "sector": row["sector"],
            "cash": is_def,
            "defensive": def_slot,
            "factor_source": row["factor_source"],
            "sector_source": row["sector_source"],
        }
    return pd.DataFrame(rows).T.sort_index(), fmom, smom, fpick, spick, cstate


def compute_classic_book(raw, spx, history=None):
    """Legacy -25%/19m S&P 500 gate comparator across the same equity selections."""
    fmom = _momentum(raw[FACTOR_SLOTS])
    smom = _momentum(raw[SECTOR_SLOTS])
    fpick, spick = fmom.idxmax(axis=1), smom.idxmax(axis=1)
    spx_r = spx.reindex(pd.period_range(spx.index.min(), spx.index.max(), freq="M"))
    cash_mom = (spx_r / spx_r.shift(CASH_LOOKBACK) - 1).dropna()
    classic_cash = cash_mom < CASH_THRESHOLD
    sig = sorted(set(fpick.index) & set(spick.index))
    rows = {}
    for month in sig:
        rows[month + 1] = {
            "factor": fpick[month],
            "sector": spick[month],
            "cash": bool(classic_cash.get(month, False)),
            "defensive": None,
            "factor_source": "MSCI",
            "sector_source": "S&P index",
        }
    for month, row in (history or {}).items():
        m = pd.Period(month, "M")
        rows[m + 1] = {
            "factor": row["factor"],
            "sector": row["sector"],
            "cash": bool(classic_cash.get(m, False)),
            "defensive": None,
            "factor_source": row["factor_source"],
            "sector_source": row["sector_source"],
        }
    return pd.DataFrame(rows).T.sort_index()


def resolve_history(raw, canary, history, expected, panels=None):
    """Fill recent missing signals and persist settled TIP canary decisions."""
    _, fmom, smom, _, _, cstate = compute_book(raw, canary)
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
    if str(expected) not in history and expected not in pending:
        pending.append(expected)
    for month in pending:
        entry = {
            "provenance": "Computed at month end" if month == expected else "Reconstructed ETF fallback",
            "rule": RULE_VERSION,
        }
        for leg, primary, source in (("factor", fmom, "MSCI"), ("sector", smom, "S&P index")):
            mom = primary if month in primary.index else proxies.get(leg)
            if mom is None or month not in mom.index:
                raise RuntimeError(f"no complete {leg} ranking for {month}")
            table = mom.loc[month]
            entry[leg] = str(table.idxmax())
            entry[leg + "_source"] = source if month in primary.index else "ETF proxy"
            entry[leg + "_table"] = {k: float(v) for k, v in table.items()}
        if month not in cstate.index:
            raise RuntimeError(f"cash-rule price window unavailable for {month}")
        c_row = cstate.loc[month]
        is_def = bool(c_row["cash"])
        def_slot = str(c_row["defensive"]) if is_def else None
        entry["cash"] = is_def
        entry["defensive"] = def_slot
        entry["defensive_ticker"] = DEFENSIVE_TRADABLE[def_slot][2] if def_slot else None
        entry["canary"] = {
            "tip_score": round(float(c_row["tip_score"]), 6),
            "ief_score": round(float(c_row["ief_score"]), 6),
            "bil_score": round(float(c_row["bil_score"]), 6),
        }
        history[str(month)] = entry
    if str(expected) not in history:
        raise RuntimeError(f"no current signal for {expected}")
    return history


def splice_pre_listing_bil(ib01_gbp: pd.Series, bil_gbp: pd.Series) -> tuple[pd.Series, list[str]]:
    """Backfill DEF_BIL prior to IB01.L's Feb 2019 listing without a level jump."""
    if ib01_gbp.empty:
        return bil_gbp, [str(m) for m in bil_gbp.index]
    first_live = ib01_gbp.index.min()
    if bil_gbp.empty or first_live not in bil_gbp.index:
        return ib01_gbp, []
    scale = float(ib01_gbp.loc[first_live]) / float(bil_gbp.loc[first_live])
    pre = bil_gbp[bil_gbp.index < first_live] * scale
    combined = pd.concat([pre, ib01_gbp]).sort_index()
    return combined, [str(m) for m in pre.index]


def gbp_panel(symbols):
    fx = {}
    for pair, cur in (("USDGBP=X", "USD"), ("EURGBP=X", "EUR")):
        fx[cur] = yahoo_monthly(pair, adjusted=False, start="2013-01-01")
    out, pre_listing_bil = {}, []
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

    if "DEF_BIL" in out and not out["DEF_BIL"].empty:
        try:
            bil_usd = yahoo_monthly("BIL", adjusted=True, start="2013-01-01")
            bil_gbp = (bil_usd * fx["USD"].reindex(bil_usd.index)).dropna()
            out["DEF_BIL"], pre_listing_bil = splice_pre_listing_bil(out["DEF_BIL"], bil_gbp)
        except Exception as exc:
            print(f"  !! pre-listing BIL backfill unavailable ({exc})")

    fresh = pd.DataFrame(out)
    cache_path = DATA / "fund_prices_gbp.csv"
    if cache_path.exists():
        cached = pd.read_csv(cache_path, index_col=0)
        cached.index = pd.PeriodIndex(cached.index, freq="M")
        fresh = fresh.combine_first(cached)
    fresh = fresh.reindex(columns=list(symbols)).sort_index()
    fresh.to_csv(cache_path)
    return fresh, fx, pre_listing_bil


def build_track(book, funds, bench_rets):
    """Rule D: reset equity split to 50/50 only when equity or regime changes."""
    r = funds.pct_change(fill_method=None) * 100
    wf, prev, rows = 0.5, None, {}
    for m in book.index:
        row = book.loc[m]
        if m not in r.index:
            continue
        if bool(row["cash"]):
            def_slot = row.get("defensive")
            if pd.isna(def_slot) or not str(def_slot).strip() or str(def_slot) == "CASH":
                rows[m] = 0.0
                wf, prev = 0.5, ("CASH", "CASH")
                continue
            def_key = str(def_slot)
            if def_key not in r.columns:
                continue
            rd = r.at[m, def_key]
            if pd.isna(rd):
                continue
            rows[m] = float(rd)
            wf, prev = 0.5, ("DEF", def_key)
            continue
        f, s = row["factor"], row["sector"]
        if f not in r.columns or s not in r.columns:
            continue
        rf, rs = r.at[m, f], r.at[m, s]
        if pd.isna(rf) or pd.isna(rs):
            continue
        if prev is None or prev[0] in ("CASH", "DEF") or f != prev[0] or s != prev[1]:
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


def _tradable_track_matches_rule(record_path: Path) -> bool:
    meta_path = DATA / "price_status.json"
    if not record_path.exists() or not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
        return meta.get("rule") == RULE_VERSION
    except Exception:
        return False


def main():
    print("fetching signal panel...")
    _, raw, failures = build_signal_panel()
    print("fetching TIP canary panel...")
    canary = canary_panel()
    spx = yahoo_monthly("^GSPC", adjusted=False)

    print("computing book...")
    expected = pd.Timestamp.today().to_period("M") - 1
    history_path = DATA / "signal_history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else {}
    # Migrate legacy pre-TIP archived months so signal_history carries canary decisions.
    history = {m: row for m, row in history.items() if row.get("rule") == RULE_VERSION}
    history = resolve_history(raw, canary, history, expected)
    book, fmom, smom, fpick, spick, cstate = compute_book(raw, canary, history)
    classic_book = compute_classic_book(raw, spx, history)

    print("fetching London lines + UCITS Treasuries...")
    all_symbols = {**LSE, **DEFENSIVE_LSE}
    funds, fx, pre_listing_bil = gbp_panel(all_symbols)
    bench, bench_prices = {}, {}
    for label, (sym, cur) in BENCH.items():
        m = yahoo_monthly(sym, adjusted=True, start="2013-01-01")
        if cur == "USD":
            m = (m * fx["USD"].reindex(m.index)).dropna()
        bench_prices[label] = m
        bench[label] = m.pct_change(fill_method=None) * 100
    bench_df = pd.DataFrame(bench)
    track = build_track(book, funds, bench_df)
    classic_track = build_track(classic_book, funds, bench_df)
    if not classic_track.empty:
        # Align start with London UCITS equity inception (Dec 2015) for fair apples-to-apples track.
        track = track.loc[classic_track.index.min():]

    record_path = DATA / "track_record.csv"
    if _tradable_track_matches_rule(record_path):
        recorded = pd.read_csv(record_path, index_col=0)
        recorded.index = pd.PeriodIndex(recorded.index, freq="M")
        if not recorded.empty and (track.empty or recorded.index.min() >= track.index.min()):
            track = append_track(recorded, track)
    track.to_csv(record_path)
    classic_track["Lodestar"].rename("Lodestar (-25%/19m)").to_csv(DATA / "classic_track.csv")

    held = book.loc[expected]
    inputs = []
    if bool(held["cash"]):
        def_slot = str(held["defensive"])
        def_ticker = DEFENSIVE_LSE[def_slot]
        for month in (expected - 1, expected):
            value = funds.at[month, def_slot] if month in funds.index and def_slot in funds else np.nan
            inputs.append({"ticker": def_ticker, "month": str(month),
                           "available": bool(pd.notna(value)), "basis": "GBP adjusted close"})
    else:
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

    track_set = set(track.index)
    def_months_bil = [str(m) for m, r in book.iterrows()
                      if m in track_set and bool(r["cash"]) and r.get("defensive") == "DEF_BIL"
                      and str(m) in set(pre_listing_bil)]
    price_status = {
        "rule": RULE_VERSION,
        "expected": str(expected),
        "performanceThrough": str(track.index[-1]),
        "preListingBilProxyMonths": def_months_bil,
        "inputs": inputs,
        "missing": [r for r in inputs if not r["available"]],
    }
    (DATA / "price_status.json").write_text(json.dumps(price_status, indent=2) + "\n")
    if track.index[-1] < expected:
        print(f"::warning::Performance incomplete: through {track.index[-1]}, expected {expected}")

    book.to_csv(DATA / "final_book.csv")
    out = track.rename(columns={"Lodestar": "Lodestar (TIP canary, LSE)"})
    out.to_csv(DATA / "final_tradable.csv")

    yr = pd.Series(pd.PeriodIndex(out.index).year, index=out.index)
    ann = pd.DataFrame({c: out[c].groupby(yr).apply(lambda s: ((1 + s / 100).prod() - 1) * 100)
                        for c in out.columns})
    ann["vs All-World"] = ann[out.columns[0]] - ann["FTSE All-World"]
    ann.to_csv(DATA / "final_tradable_annual.csv")

    row = history[str(expected)]
    prev_held = book.loc[expected]
    def_slot = row.get("defensive")
    prev_def = prev_held.get("defensive") if bool(prev_held["cash"]) else None
    signal = {
        "rule": RULE_VERSION,
        "signal_month": str(expected),
        "holding_month": str(expected + 1),
        "factor_source": row["factor_source"],
        "sector_source": row["sector_source"],
        "factor": row["factor"],
        "sector": row["sector"],
        "cash": bool(row["cash"]),
        "defensive": def_slot,
        "defensive_ticker": row.get("defensive_ticker"),
        "defensive_name": DEFENSIVE_NAMES.get(def_slot) if def_slot else None,
        "canary": row.get("canary", {}),
        "factor_ticker": LSE[row["factor"]],
        "sector_ticker": LSE[row["sector"]],
        "factor_table": row.get("factor_table", {}),
        "sector_table": row.get("sector_table", {}),
        "sector_month": str(expected),
        "previous": {
            "factor": str(prev_held["factor"]),
            "sector": str(prev_held["sector"]),
            "cash": bool(prev_held["cash"]),
            "defensive": str(prev_def) if pd.notna(prev_def) and prev_def else None,
            "defensive_ticker": DEFENSIVE_LSE.get(str(prev_def)) if pd.notna(prev_def) and prev_def else None,
        },
        "stale": False,
        "expected_month": str(expected),
        "source_failures": failures,
        "performance_month": str(track.index[-1]),
    }
    history_path.write_text(json.dumps(history, indent=2, allow_nan=False) + "\n")
    (DATA / "signal.json").write_text(json.dumps(signal, indent=2))
    status_tag = f"  [DEFENSIVE: {signal['defensive_ticker']}]" if signal["cash"] else ""
    print(f"\nsignal at {signal['signal_month']}: {signal['factor']} ({signal['factor_ticker']}) [{row['factor_source']}] + "
          f"{signal['sector']} ({signal['sector_ticker']}) [{row['sector_source']}]{status_tag}")
    print(f"track: {len(track)} months to {track.index[-1]}")
    return signal


if __name__ == "__main__":
    main()

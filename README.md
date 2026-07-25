# Lodestar

A monthly factor-and-sector ETF rotation, and the page that tracks it:
**https://soylee22.github.io/lodestar/**

## The rule

1. Fifteen indices: eight MSCI factor indices (USA and Europe × momentum,
   quality, enhanced value, small cap) and seven S&P 500 GICS sector indices.
2. At each month-end close, score every index by its **price return over the
   trailing eight months**, on its own published currency levels.
3. Hold the strongest of each basket, **50/50**.
4. Both legs go to **cash** if the S&P 500 sits more than 25% below its level
   nineteen months earlier.
5. Reset to 50/50 **only when a leg changes**; otherwise let the split drift.
6. Trade on the first trading day of the month. If neither winner changed,
   nothing trades — which is most months.

Signals come from the indices. Returns come from the London-listed UCITS lines,
in sterling, on a total-return basis.

## Running it

    pip install -r requirements.txt
    python run_monthly.py

Recomputes the signal, rebuilds `data.js`, and sends a Telegram alert if
`TG_TOKEN` and `TG_CHAT` are set.

**It runs every morning from the 1st to the 5th, not just the 1st.** The job is
idempotent: it alerts once per signal month and then goes quiet, so the extra
days are retries rather than noise. If a data source is down on the 1st, the
following morning picks the month up instead of losing it. Failures alert on
every attempt, because a month with no signal is the thing you must not miss.

Half the universe is MSCI data (the eight factor indices); the sector leg and the
cash rule come from S&P 500 indices. MSCI's public endpoint is intermittently
unreachable, so there are three layers of defence:

1. `data/panel_local.csv` caches the index panel and is refreshed incrementally,
   so an outage cannot destroy history. Partial months are never persisted.
2. If MSCI is behind for the current month, the factor leg is recomputed from
   the US-listed factor ETFs, whose unadjusted closes are price series like the
   index. Tested against the live month: same pick, momentum values within about
   1&ndash;4pp. Any signal produced this way is flagged as such in the alert.
3. Only if both fail does the job refuse to publish, and it then says so loudly
   and retries the next morning.

## Layout

| Path | |
|---|---|
| `pipeline/build.py` | fetch, compute the signal, rebuild the track |
| `build_data.py` | turn the CSVs into `data.js` for the page |
| `run_monthly.py` | orchestrates both, then alerts |
| `universe.py` | the fifteen slots, ISINs, and LSE/Xetra/US tickers |
| `index.html` | the page; self-contained, no external requests |

## Caveats

The record is a backtest, not a live track record. The window contains no slow
correlated bear market, and the cash rule never fires in it, so the drawdown
figure is untested. Not financial advice.

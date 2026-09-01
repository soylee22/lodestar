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

Recomputes the signal, rebuilds `data.js`, and announces the result on Telegram
(`TG_TOKEN`, `TG_CHAT`) and by email (`GMAIL_APP_PASSWORD`, sent from and to
`leeslater1992@gmail.com` unless `MAIL_TO` says otherwise). Missing credentials
print the message instead of sending it, so the script is safe to run locally.

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

The sector leg has the same defence. Yahoo's S&P GICS index series stopped
updating after July 2026, so when that feed falls behind the month just ended,
the whole leg is recomputed from the SPDR Select Sector ETFs. The leg is
replaced whole, never spliced slot by slot, and the alert names every leg that
used a proxy.

## The daily read

    python daily.py

A drumbeat between signals: where the rule stands part-way through the month, so
the 1st is never a surprise. It sends the same message to Telegram and email at
07:00 UTC on weekdays, an hour after the monthly job, and reports what is held
now, what a rebalance today would buy, how many days remain to the next signal,
month-to-date performance of the two holdings against the All-World, the top of
each ranking and where the cash rule sits.

It changes no files and commits nothing. What is held now is read from
`data/last_alert.json`, which `run_monthly.py` writes when it announces a signal.

Two things to know about the numbers. The factor leg uses the US-listed
distributing factor ETFs, because MSCI publishes month-end levels only and there
is no intra-month index level to read; their momentum runs 1&ndash;4pp adrift of
the indices, enough to reorder slots that are close together. And early in a
month the eight-month window has barely moved, so the read swings about. The
signal that counts is the one computed at the month-end close.

## Layout

| Path | |
|---|---|
| `pipeline/build.py` | fetch, compute the signal, rebuild the track |
| `build_data.py` | turn the CSVs into `data.js` for the page |
| `run_monthly.py` | orchestrates both, then alerts (monthly) |
| `daily.py` | provisional read between signals (weekdays) |
| `notify.py`, `mailer.py` | Telegram and Gmail delivery |
| `universe.py` | the fifteen slots, ISINs, and LSE/Xetra/US tickers |
| `index.html` | the page; self-contained, no external requests |

## Caveats

The record is a backtest, not a live track record. The window contains no slow
correlated bear market, and the cash rule never fires in it, so the drawdown
figure is untested. Not financial advice.

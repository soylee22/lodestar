# Lodestar

A monthly factor-and-sector ETF rotation, and the page that tracks it:
**https://soylee22.github.io/lodestar/**

## The rule

1. Fifteen equity indices: eight MSCI factor indices (USA and Europe across momentum,
   quality, enhanced value, small cap) and seven S&P 500 GICS sector indices.
2. At each month-end close, score every equity index by its **price return over
   the trailing eight months**, on its own published currency levels.
3. Check the monthly **TIP canary** using the 1-3-6-12 weighted momentum score on
   `TIP` in USD total return (`(12 * r1 + 4 * r3 + 2 * r6 + r12) / 19`).
4. When `TIP` sits at or below **0%**, exit both equity legs into 100% of whichever
   London-listed UCITS US Treasury ETF scores higher on 1-3-6-12 in USD total
   return: `IB01.L` (US Treasury 0-1yr, `BIL` twin) or `CBU0.L` (US Treasury 7-10yr, `IEF` twin).
   Prior to `IB01.L`'s February 2019 listing, defensive short-bill months splice
   `BIL` converted to GBP total return at zero price jump.
5. When `TIP` is above **0%**, hold the strongest factor and strongest sector **50/50**.
   Reset to 50/50 **only when a leg changes or on re-entering equities**. Otherwise let the split drift.
6. Trade on the first trading day of the month.

Signals come from equity indices plus `TIP`, `IEF`, and `BIL`. Executed returns come from
the London-listed UCITS accumulating lines in sterling on a total-return basis.

**Crash protection architecture.** Lodestar replaces the legacy `-25%` 19-month S&P 500 gate
with Keller and Keuning's Hybrid Asset Allocation `TIP` 1-3-6-12 canary, implemented with
London-listed UCITS US Treasury ETFs. The original `-25%/19m` path is computed alongside
and plotted on the website as the `Lodestar (-25%/19m)` comparator.

## Running it

    pip install -r requirements.txt
    python run_monthly.py

Recomputes the signal, rebuilds `data.js`, and announces the result on Telegram
(`TG_TOKEN`, `TG_CHAT`) and by email (`GMAIL_APP_PASSWORD`, sent from and to
`leeslater1992@gmail.com` unless `MAIL_TO` says otherwise). Missing credentials fail delivery without printing private message contents.
Both channels must accept the message before a monthly alert is recorded.

**It refreshes every morning at 06:00 UTC through GitHub Actions.** Alerts remain
once per signal month. Daily refreshes collect delayed month-end fund prices,
including delays beyond the fifth day. If a data source is down on the 1st, the
following morning picks the month up instead of losing it. Failures alert on
every attempt, because a month with no signal is the thing you must not miss.

Half the equity universe is MSCI data (the eight factor indices). The sector leg
uses S&P 500 GICS indices, and the crash-protection canary uses `TIP`, `IEF`, and `BIL`
USD total-return series from Yahoo Finance. MSCI's public endpoint is intermittently
unreachable, so there are three layers of defence:

1. `data/panel_local.csv` caches the index panel and is refreshed incrementally,
   so an outage cannot destroy history. Current months are never persisted.
2. If MSCI is behind for the current month, the factor leg is recomputed from
   the US-listed factor ETFs, whose unadjusted closes are price series like the
   index. Tested against the live month: same pick, momentum values within about
   1-4pp. Any signal produced this way is flagged as such in the alert.
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

It does not commit broker information. What is held now comes from
`data/signal_history.json`, the same archive used by the website. The previous
alert state is retained only as a fallback for older checkouts.

Two things to know about the numbers. The factor leg uses the US-listed
distributing factor ETFs, because MSCI publishes month-end levels only and there
is no intra-month index level to read. Their momentum runs 1-4pp adrift of
the indices, enough to reorder slots that are close together. Early in a
month the eight-month window and `TIP` canary can swing around flat thresholds. The
signal that counts is the one computed at the month-end close.

## Layout

| Path | |
|---|---|
| `pipeline/build.py` | fetch equity indices, `TIP`/`IEF`/`BIL` canary, and rebuild tracks |
| `pipeline/crash_protection.py` | compute `TIP` `1-3-6-12` canary and `IB01.L`/`CBU0.L` selection |
| `build_data.py` | turn the CSVs into `data.js` for the page |
| `run_monthly.py` | orchestrates both, then alerts (monthly) |
| `daily.py` | provisional read between signals (weekdays) |
| `notify.py`, `mailer.py` | Telegram and Gmail delivery |
| `universe.py` | equity slots plus defensive Treasury sleeves (`IB01.L`, `CBU0.L`) |
| `index.html` | the page, self-contained, no external requests |

## Caveats

The historical track is a backtest before November 2026 deployment rather than a live
broker record throughout. Under the active `TIP` 1-3-6-12 canary, defensive rotations
hold `IB01.L` or `CBU0.L` in real shocks, with US `BIL` converted to GBP total return
spliced prior to `IB01.L`'s February 2019 launch. Past performance is not a guide, and
this is not financial advice.

## Website freshness

`data/signal_history.json` keeps each published signal and its source. The July
and August 2026 entries come from the original alert commits. New month-end
signals are archived before the return book is built. Index backfills cannot
change those recorded holdings.

The page shows the current holding month separately from the final priced month.
ETF fallback holdings enter the historical return book and carry source labels.
Only actual final-session closes count as monthly prices. If London fund prices
arrive late, the current holding still updates and the page identifies the pending
performance month. Daily scheduled refreshes pick up those prices when available.

For a local build without sending messages or changing the alert state:

    python run_monthly.py --build-only

Run the focused checks with:

    python -m unittest discover -s tests -v

Europe Momentum uses `IEFM.L`, the sterling London listing of
`IE00BQN1K786`. The issuer confirms this listing. The old `IEMO.L` Yahoo mapping
returned no history. This correction retains the same fund and published returns.

Issuer listing: https://www.ishares.com/ch/professionals/en/products/272019/ishares-msci-europe-momentum-factor-ucits-etf

## GitHub operation and private reports

GitHub hosts the jobs. The computer can be switched off. Schedules use UTC.
The website refreshes daily at 06:00. Private reports run weekdays at 07:00.
Monthly alerts are sent once per signal month after a scheduled refresh.

Encrypted repository secrets hold `T212_KEY_ID`, `T212_SECRET`, `TG_TOKEN`,
`TG_CHAT` and `GMAIL_APP_PASSWORD`. The private daily report reads current
Trading 212 positions and values the tracked purchase at the broker's GBP price.
Broker authentication is checked on the GitHub runner. Fresh broker values are
not written to public build artefacts or logs. The public portfolio display uses
saved purchase quantities and market quotes, refreshed on the daily schedule.

Only Lodestar performance and signal reports are active. Pullback scans, alerts
and the hourly cloud notification workflow were retired at Lee's request on
12 November 2026. The older Simple Pullback job in Lodestar World is also disabled.
The historical Pullback website remains available as a saved snapshot.

Failed Lodestar jobs request a Telegram warning with the run link. They no longer
send a second custom failure email. GitHub's own genuine failure notifications
remain subject to the account settings. Routine performance emails stay enabled.
Both delivery helpers retry transient errors. Telegram messages request normal
audible notifications. Phone settings still control the sound. GitHub schedules
can run late.

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
`TG_TOKEN` and `TG_CHAT` are set. Runs automatically on the 1st of each month.

`data/panel_local.csv` is a cached index panel, refreshed incrementally. MSCI's
public endpoint is intermittently unreachable; when it is, the job falls back to
the cache and **refuses to publish a signal** rather than emit a stale one, and
says so in the alert.

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

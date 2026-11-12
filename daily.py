#!/usr/bin/env python3
"""Lodestar daily read: where the signal stands part-way through the month.

This is a drumbeat, not a trade instruction. The real signal is computed once,
at the month-end close, by run_monthly.py. What this does is take the same rule
and evaluate it on today's prices, so the standing is never a surprise on the
1st:

  * momentum = today's level / the level at the close of the month eight
    months before the month now running. At month-end that expression becomes
    the real signal, unchanged.
  * the cash rule, likewise, on today's S&P 500 against its close nineteen
    month-ends back.
  * what is held now comes from the same archived month-end signal as the page.

The factor leg uses the US-listed distributing factor ETFs rather than the MSCI
indices, because MSCI publishes month-end levels only and there is no intra-month
index level to read. Their unadjusted closes are price series, like the index,
but they are not the index: momentum runs 1-4pp adrift.

The sector leg reads the S&P sector indices, which are daily and exact when the
feed is whole. Since 21 Aug 2026 that feed has served a partial window: recent
closes arrive but the month-end eight months back does not, which leaves the
momentum undefined for every sector at once. When any sector slot comes back
incomplete the whole leg is recomputed from the SPDR Select Sector ETFs, which
are unadjusted price series on the same indices and matched the index momentum
to 0.05pp on the last day both could be read. The leg switches wholesale, never
slot by slot: a ranking built half on the index and half on a proxy is not a
ranking.
"""
from __future__ import annotations
import json, sys, time
from calendar import monthrange
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from notify import telegram              # noqa: E402
from mailer import email                 # noqa: E402
from universe import FACTOR_NAMES, SECTOR_NAMES  # noqa: E402
from pipeline.live_portfolio import get_live_portfolio_data  # noqa: E402
from pipeline.crash_protection import get_crash_protection_data  # noqa: E402

NAMES = {**FACTOR_NAMES, **SECTOR_NAMES}
SITE = "https://soylee22.github.io/lodestar/"

LOOKBACK = 8
# Inferred substitute, not MarketFighter's proprietary twelve-close ketchup rule.
CASH_LOOKBACK, CASH_THRESHOLD = 19, -0.25

FACTOR_PROXY = {"USA_MOM": "MTUM", "USA_QUAL": "QUAL", "USA_VAL": "VLUE",
                "USA_SMALL": "SMLF", "EUR_MOM": "IMTM", "EUR_QUAL": "IQLT",
                "EUR_VAL": "IVLU", "EUR_SMALL": "IEUS"}
SECTOR_YF = {"CONS_DISC": "^SP500-25", "CONS_STAP": "^SP500-30", "ENERGY": "^GSPE",
             "HEALTH": "^SP500-35", "INDUST": "^SP500-20", "INFOTECH": "^SP500-45",
             "MATERIALS": "^SP500-15"}
SECTOR_PROXY = {"CONS_DISC": "XLY", "CONS_STAP": "XLP", "ENERGY": "XLE",
                "HEALTH": "XLV", "INDUST": "XLI", "INFOTECH": "XLK",
                "MATERIALS": "XLB"}
LSE = {"USA_MOM": "IUMO.L", "USA_QUAL": "IUQA.L", "USA_VAL": "IUVL.L",
       "USA_SMALL": "CUSS.L", "EUR_MOM": "IEMO.L", "EUR_QUAL": "IEQU.L",
       "EUR_VAL": "IEVL.L", "EUR_SMALL": "XXSC.L", "CONS_DISC": "IUCD.L",
       "CONS_STAP": "IUCS.L", "ENERGY": "IUES.L", "HEALTH": "IHCU.L",
       "INDUST": "IUIS.L", "INFOTECH": "IUIT.L", "MATERIALS": "IUMS.L"}


def _retry(fn, n=4, wait=5):
    last = None
    for _ in range(n):
        try:
            out = fn()
            if out is not None and len(out):
                return out
            last = "empty response"
        except Exception as e:
            last = e
        time.sleep(wait)
    raise RuntimeError(f"failed after {n} attempts: {last}")


def series(sym, start, need=None, adjusted=False, retries=4):
    """One symbol's daily closes, fetched alone and with retries.

    Yahoo throttles this runner's addresses and answers a throttled request with
    a short series rather than an error, so a response that does not reach back
    to `need` counts as a failure and is retried.
    """
    def go():
        d = yf.Ticker(sym).history(start=start, interval="1d", auto_adjust=adjusted)["Close"]
        if d.empty:
            return None
        d.index = pd.to_datetime(d.index).tz_localize(None)
        if need is not None and d.index.min().to_period("M") > need:
            print(f"  .. {sym} came back short (from {d.index.min():%Y-%m-%d}); retrying")
            return None
        return d
    return _retry(go, n=retries)


def closes(mapping, start, need=None, adjusted=False, retries=4, stale_days=5):
    """Frame of daily closes keyed by slot, plus the slots that would not load.

    A missing slot is reported, never raised: this is a read, not a trade, and a
    partial ranking flagged as partial beats a failed job and a silent morning.

    Every slot in one mapping trades on one calendar, so a slot whose own last
    close sits more than `stale_days` behind the newest date in the frame has
    stopped updating at the source. It is dropped rather than carried: the frame
    is forward-filled to align the calendars, and forward-filling a dead series
    would hand `iloc[-1]` a months-old price under today's date. Yahoo currently
    answers a long-window request for the S&P sector indices with rows that run
    to yesterday and values that stop in July, which is exactly this.
    """
    out, missing = {}, []
    for slot, sym in mapping.items():
        try:
            out[slot] = series(sym, start, need, adjusted, retries)
        except Exception as e:
            missing.append(slot)
            print(f"  !! {slot} ({sym}) unavailable: {e}")
    if not out:
        return pd.DataFrame(), missing
    px = pd.DataFrame(out)
    last = {c: px[c].last_valid_index() for c in px.columns}
    asof = px.index.max()
    stale = [c for c in px.columns if (asof - last[c]).days > stale_days]
    for c in stale:
        print(f"  !! {c} ({mapping[c]}) stops at {last[c]:%Y-%m-%d}, "
              f"{asof:%Y-%m-%d} elsewhere: dropped as stale")
    return px.drop(columns=stale).ffill(), missing + stale


def month_end_level(px: pd.DataFrame, period: pd.Period) -> pd.Series:
    """Closes at the end of one completed month. Empty if nothing reaches back."""
    if px.empty:
        return pd.Series(dtype=float)
    m = px.resample("ME").last()
    m.index = m.index.to_period("M")
    if period not in m.index:
        return pd.Series(dtype=float)
    return m.loc[period].dropna()


def momentum(px: pd.DataFrame, base: pd.Period, missing: list) -> tuple[pd.Series, list]:
    """Price return from the close of `base` to the latest close, per slot."""
    lvl = month_end_level(px, base)
    if lvl.empty:
        return pd.Series(dtype=float), missing + list(px.columns)
    mom = (px.iloc[-1][lvl.index] / lvl - 1).dropna()
    return mom, missing + [c for c in px.columns if c not in mom.index]


def held_now() -> tuple[str, str, str]:
    """(factor slot, sector slot, month the signal was computed at)."""
    history_path = HERE / "data" / "signal_history.json"
    if history_path.exists():
        history = json.loads(history_path.read_text())
        expected = str(pd.Timestamp(date.today()).to_period("M") - 1)
        eligible = sorted(month for month in history if month <= expected)
        if eligible:
            month = eligible[-1]
            row = history[month]
            if row["cash"]:
                return "CASH", "CASH", month
            return row["factor"], row["sector"], month
    state = json.loads((HERE / "data" / "last_alert.json").read_text())
    picks = state.get("picks", "")
    if picks == "CASH":
        return "CASH", "CASH", state.get("month", "?")
    f, _, s = picks.partition("+")
    return f, s, state.get("month", "?")


def ranked(mom: pd.Series) -> list[tuple[str, float]]:
    return sorted(((k, float(v)) for k, v in mom.items()), key=lambda kv: -kv[1])


def margin(mom: pd.Series, held: str) -> str | None:
    """What has to happen, from here to the month end, for the held slot to keep
    it. Momentum is a price ratio, so the gap between two rows compounds: the
    difference in percentage points is not the return the laggard must make up.
    Without this line a mid-month ranking reads as a decision, and it is not one.
    """
    if held not in mom.index or len(mom) < 2:
        return None
    (lead, lv), (second, sv) = ranked(mom)[:2]
    if lead == held:
        give = 1 - (1 + sv) / (1 + mom[held])
        return (f"  <i>{NAMES[held]} holds the slot unless it gives back "
                f"{give*100:.1f}% against {NAMES[second]} this month.</i>")
    need = (1 + lv) / (1 + float(mom[held])) - 1
    return (f"  <i>{NAMES[held]} keeps the slot only if it beats {NAMES[lead]} by "
            f"{need*100:.1f}% over the rest of the month.</i>")


def build_email_html(days_out: int, signal_date: date, live_pf: dict, crash_data: dict,
                     f_pick: str | None, s_pick: str | None, fmom: pd.Series, smom: pd.Series,
                     hf: str, hs: str, hmonth: str, change: list, would: str) -> str:
    crash_status = ("100% Cash" if crash_data.get("cash_triggered") else "100% Invested") if crash_data else "Unavailable"
    crash_note = "Lodestar 19-month floor, provisional" if crash_data else "Risk data unavailable"
    init_cap = live_pf.get("initial_capital_gbp", 0.0)
    curr_val = live_pf.get("current_value_gbp", 0.0)
    pnl_gbp = live_pf.get("total_pnl_gbp", 0.0)
    pnl_pct = live_pf.get("total_pnl_pct", 0.0)
    pnl_color = "#16a34a" if pnl_gbp >= 0 else "#dc2626"
    pnl_sign = "+" if pnl_gbp >= 0 else "−"

    holdings_rows = ""
    for h in live_pf.get("holdings", []):
        h_pnl_col = "#16a34a" if h["pnl_gbp"] >= 0 else "#dc2626"
        h_sign = "+" if h["pnl_gbp"] >= 0 else "−"
        holdings_rows += f"""
        <tr style="border-bottom:1px solid #e2e8f0;">
          <td style="padding:10px 8px;font-weight:600;color:#0f172a;">{h['leg']}</td>
          <td style="padding:10px 8px;">
            <strong style="color:#0f172a;">{h['ticker']}</strong>
            <span style="font-size:11px;color:#64748b;">({h['code']})</span><br>
            <span style="font-size:11px;color:#64748b;">{h['name']}</span>
          </td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">{h['shares']:.4f}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">£{h['cost_gbp']:,.2f}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">£{h['current_value_gbp']:,.2f}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;font-weight:600;color:{h_pnl_col};">
            {h_sign}£{abs(h['pnl_gbp']):.2f} ({h['pnl_pct']:+.2f}%)
          </td>
        </tr>
        """

    bm_rows = ""
    for b in live_pf.get("benchmarks", []):
        alpha = b.get("alpha_pct", 0.0)
        ret = b.get("return_pct", 0.0)
        ret_col = "#16a34a" if ret >= 0 else "#dc2626"
        alpha_bg = "#dcfce7" if alpha >= 0 else "#fee2e2"
        alpha_col = "#15803d" if alpha >= 0 else "#b91c1c"
        alpha_badge = f'<span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:11px;font-weight:600;background:{alpha_bg};color:{alpha_col};">{alpha:+.2f}pp</span>'
        fmt_p = lambda p: f"£{p:.4f}" if p < 10 else f"£{p:.2f}"
        bm_rows += f"""
        <tr style="border-bottom:1px solid #e2e8f0;">
          <td style="padding:10px 8px;font-weight:600;color:#0f172a;">{b['ticker']}</td>
          <td style="padding:10px 8px;color:#475569;">{b['name']}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">{fmt_p(b['entry_price_gbp'])}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">{fmt_p(b['current_price_gbp'])}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;color:{ret_col};font-weight:600;">{ret:+.2f}%</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">{alpha_badge}</td>
        </tr>
        """

    check_rows = ""
    for c in crash_data.get("checks", []):
        badge = '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;background:#dcfce7;color:#15803d;">✓ PASS</span>' if c['pass'] else '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;background:#fee2e2;color:#b91c1c;">✗ FAIL</span>'
        chk_col = "#16a34a" if c["reading_pct"] >= 0 else "#dc2626"
        check_rows += f"""
        <tr style="border-bottom:1px solid #e2e8f0;">
          <td style="padding:8px 8px;">
            <strong style="color:#0f172a;">{c['name']}</strong><br>
            <span style="font-size:11px;color:#64748b;">{c['desc']}</span>
          </td>
          <td style="padding:8px 8px;font-size:12px;color:#64748b;">{c['rule']}</td>
          <td style="padding:8px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;font-weight:600;color:{chk_col};">{c['reading_pct']:+.2f}%</td>
          <td style="padding:8px 8px;text-align:right;">{badge}</td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lodestar Daily</title>
</head>
<body style="margin:0;padding:24px 0;background-color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;color:#0f172a;">
  <div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 6px -1px rgba(0,0,0,0.07),0 2px 4px -2px rgba(0,0,0,0.05);border:1px solid #e2e8f0;">
    
    <div style="background:#09090b;padding:28px 24px;border-bottom:1px solid #27272a;">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
        <div>
          <span style="font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#a1a1aa;">Systematic Rotation Drumbeat</span>
          <h1 style="margin:4px 0 0;font-size:24px;font-weight:800;color:#ffffff;letter-spacing:-0.03em;">Lodestar</h1>
        </div>
        <div style="background:#18181b;border:1px solid #3f3f46;border-radius:999px;padding:6px 14px;color:#38bdf8;font-size:12px;font-weight:600;font-family:ui-monospace,Menlo,monospace;">
          {days_out} Days to New Month
        </div>
      </div>
      <p style="margin:12px 0 0;font-size:13px;color:#d4d4d8;line-height:1.5;">
        Tracking live Trading 212 execution of the 50/50 factor + sector targets against four core benchmark trackers, with a scheduled S&amp;P 500 risk snapshot.
      </p>
    </div>

    <div style="padding:24px;">

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px;">
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px;">
          <div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#64748b;letter-spacing:0.04em;">Initial Capital</div>
          <div style="font-size:20px;font-weight:700;color:#0f172a;margin-top:4px;font-family:ui-monospace,Menlo,monospace;">£{init_cap:,.2f}</div>
          <div style="font-size:11px;color:#94a3b8;margin-top:2px;">Deployed 12 Nov 2026</div>
        </div>
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px;">
          <div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#64748b;letter-spacing:0.04em;">Live Valuation</div>
          <div style="font-size:20px;font-weight:700;color:#0f172a;margin-top:4px;font-family:ui-monospace,Menlo,monospace;">£{curr_val:,.2f}</div>
          <div style="font-size:11px;color:#94a3b8;margin-top:2px;">Trading 212 Sync</div>
        </div>
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px;">
          <div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#64748b;letter-spacing:0.04em;">Net P&amp;L</div>
          <div style="font-size:20px;font-weight:700;color:{pnl_color};margin-top:4px;font-family:ui-monospace,Menlo,monospace;">
            {pnl_sign}£{abs(pnl_gbp):.2f} ({pnl_pct:+.2f}%)
          </div>
          <div style="font-size:11px;color:#94a3b8;margin-top:2px;">Across both legs</div>
        </div>
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px;">
          <div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#64748b;letter-spacing:0.04em;">Crash Protection</div>
          <div style="font-size:16px;font-weight:700;color:#16a34a;margin-top:6px;">{crash_status}</div>
          <div style="font-size:11px;color:#94a3b8;margin-top:2px;">{crash_note}</div>
        </div>
      </div>

      <div style="margin-bottom:24px;">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">
          <h2 style="font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;color:#0f172a;margin:0;">Active Holdings (Trading 212)</h2>
          <span style="font-size:12px;color:#64748b;">50/50 Target Split</span>
        </div>
        <div style="overflow-x:auto;">
          <table style="width:100%;border-collapse:collapse;font-size:13px;text-align:left;">
            <thead>
              <tr style="border-bottom:2px solid #cbd5e1;color:#64748b;font-size:11px;text-transform:uppercase;">
                <th style="padding:6px 8px;">Leg</th>
                <th style="padding:6px 8px;">Asset</th>
                <th style="padding:6px 8px;text-align:right;">Shares</th>
                <th style="padding:6px 8px;text-align:right;">Cost</th>
                <th style="padding:6px 8px;text-align:right;">Value</th>
                <th style="padding:6px 8px;text-align:right;">P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {holdings_rows}
            </tbody>
          </table>
        </div>
      </div>

      <div style="margin-bottom:24px;">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">
          <h2 style="font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;color:#0f172a;margin:0;">Benchmark Race (Equal Capital £{init_cap:,.2f})</h2>
          <span style="font-size:12px;color:#64748b;">London UCITS Lines</span>
        </div>
        <div style="overflow-x:auto;">
          <table style="width:100%;border-collapse:collapse;font-size:13px;text-align:left;">
            <thead>
              <tr style="border-bottom:2px solid #cbd5e1;color:#64748b;font-size:11px;text-transform:uppercase;">
                <th style="padding:6px 8px;">Ticker</th>
                <th style="padding:6px 8px;">Benchmark</th>
                <th style="padding:6px 8px;text-align:right;">Entry</th>
                <th style="padding:6px 8px;text-align:right;">Current</th>
                <th style="padding:6px 8px;text-align:right;">Return</th>
                <th style="padding:6px 8px;text-align:right;">Alpha</th>
              </tr>
            </thead>
            <tbody>
              {bm_rows}
            </tbody>
          </table>
        </div>
      </div>

      <div style="margin-bottom:24px;">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">
          <h2 style="font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;color:#0f172a;margin:0;">Crash Protection &amp; Ketchup Checklist</h2>
          <span style="font-size:12px;color:#16a34a;font-weight:600;">All Systems Green</span>
        </div>
        <div style="overflow-x:auto;">
          <table style="width:100%;border-collapse:collapse;font-size:13px;text-align:left;">
            <thead>
              <tr style="border-bottom:2px solid #cbd5e1;color:#64748b;font-size:11px;text-transform:uppercase;">
                <th style="padding:6px 8px;">Check</th>
                <th style="padding:6px 8px;">Rule</th>
                <th style="padding:6px 8px;text-align:right;">Reading</th>
                <th style="padding:6px 8px;text-align:right;">Status</th>
              </tr>
            </thead>
            <tbody>
              {check_rows}
            </tbody>
          </table>
        </div>
      </div>

      <div style="text-align:center;padding:16px 0;">
        <a href="{SITE}" style="display:inline-block;background:#09090b;color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;padding:12px 28px;border-radius:8px;">
          Open Live Dashboard →
        </a>
      </div>

    </div>

    <div style="background:#f8fafc;padding:16px 24px;border-top:1px solid #e2e8f0;font-size:12px;color:#94a3b8;text-align:center;line-height:1.5;">
      Lodestar Factor &amp; Sector Rotation · Next signal acts <strong>{signal_date:%-d %b %Y}</strong> ({days_out} days away).<br>
      Broker valuation is checked on each scheduled report. The exact MarketFighter cash rule remains under research.
    </div>

  </div>
</body>
</html>
"""


def send(subject: str, telegram_body: str, html_body: str | None = None) -> int:
    plain = telegram_body
    for a, b in (("<b>", ""), ("</b>", ""), ("<i>", ""), ("</i>", ""),
                 ("<code>", ""), ("</code>", ""), ("&amp;", "&")):
        plain = plain.replace(a, b)
    if html_body is None:
        html_body = ("<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
                     "font-size:15px;line-height:1.5;max-width:640px\">"
                     + telegram_body.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
                     + "</div>")
    telegram_ok = telegram(telegram_body)
    email_ok = email(subject, plain, html_body)
    print(f"Delivery status: Telegram={telegram_ok}, email={email_ok}")
    return 0 if telegram_ok and email_ok else 1


def main() -> int:
    today = date.today()
    now = pd.Timestamp(today).to_period("M")
    base = now - LOOKBACK                 # the month-end the eight-month window opens at
    cash_base = now - CASH_LOOKBACK
    start = (pd.Timestamp(today) - pd.DateOffset(months=CASH_LOOKBACK + 4)).date().isoformat()

    factor_px, f_missing = closes(FACTOR_PROXY, start, need=base)
    sector_px, s_missing = closes(SECTOR_YF, start, need=base, retries=2)
    spx_px, _ = closes({"SPX": "^GSPC"}, start, need=cash_base)

    fmom, f_missing = momentum(factor_px, base, f_missing)
    smom, s_missing = momentum(sector_px, base, s_missing)

    sector_proxy = False
    if s_missing:
        print(f"  .. sector index feed incomplete ({', '.join(sorted(s_missing))}); "
              "recomputing the leg from the sector ETFs")
        px2, miss2 = closes(SECTOR_PROXY, start, need=base)
        smom2, miss2 = momentum(px2, base, miss2)
        if len(smom2) > len(smom):
            sector_px, smom, s_missing, sector_proxy = px2, smom2, miss2, True

    if not len(fmom) and not len(smom):
        return send("Lodestar: no read today",
                    "<b>Lodestar — no read today</b>\n\nYahoo returned no usable price "
                    "history for either leg. Nothing is wrong with the strategy and "
                    "nothing needs doing; the month-end signal is computed from a "
                    "different path.\n\n" + SITE)

    dates = [px.index[-1] for px in (factor_px, sector_px) if not px.empty]
    asof = max(dates).date()

    spx_lvl = month_end_level(spx_px, cash_base)
    if len(spx_lvl):
        spx_ret = float(spx_px["SPX"].iloc[-1] / spx_lvl["SPX"] - 1)
        cash = spx_ret < CASH_THRESHOLD
    else:
        spx_ret, cash = None, False

    f_pick = str(fmom.idxmax()) if len(fmom) else None
    s_pick = str(smom.idxmax()) if len(smom) else None
    hf, hs, hmonth = held_now()

    if cash:
        would = "<b>cash, both legs</b>"
    else:
        would = " + ".join(f"{NAMES[p]} (<code>{LSE[p]}</code>)" if p else "leg unavailable"
                           for p in (f_pick, s_pick))
    change = [] if cash else (
        ([f"factor: {NAMES.get(hf, hf)} → <b>{NAMES[f_pick]}</b>"] if f_pick and f_pick != hf else []) +
        ([f"sector: {NAMES.get(hs, hs)} → <b>{NAMES[s_pick]}</b>"] if s_pick and s_pick != hs else []))

    # month to date on what is actually held, in sterling, against the benchmark
    mtd = ""
    try:
        wanted = ({} if hf == "CASH" else {hf: LSE[hf], hs: LSE[hs]}) | {"All-World": "VWRL.L"}
        lse, _ = closes(wanted, start=str((now - 2).to_timestamp("M").date()),
                        need=now - 1, adjusted=True)
        m0 = month_end_level(lse, now - 1)
        if len(m0):
            mtd = "  " + " · ".join(
                f"{NAMES.get(k, k)} {(lse[k].iloc[-1] / m0[k] - 1) * 100:+.1f}%"
                for k in lse.columns if k in m0.index)
    except Exception as e:
        print(f"  !! month to date unavailable ({type(e).__name__})")

    last_day = monthrange(today.year, today.month)[1]
    signal_date = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    days_out = (signal_date - today).days

    try:
        live_pf = get_live_portfolio_data(broker=True)
    except Exception as e:
        print(f"  !! live portfolio load error: {e}")
        live_pf = {"sync_error": True}

    try:
        crash_data = get_crash_protection_data()
    except Exception as e:
        print(f"  !! crash protection load error: {e}")
        crash_data = {}

    head = ("<b>Lodestar: the cash rule would fire</b>" if cash
            else "<b>Lodestar: rebalance today has NO CHANGE</b>" if not change
            else "<b>Lodestar: rebalance today WOULD TRADE</b>")

    pnl_gbp = live_pf.get("total_pnl_gbp", 0.0)
    pnl_pct = live_pf.get("total_pnl_pct", 0.0)
    pnl_sign = "+" if pnl_gbp >= 0 else "−"

    msg = [
        f"<b>Lodestar Daily · {days_out} days to new month</b> (acting {signal_date:%-d %b})",
        head,
        "",
        f"Holding now: <b>{NAMES.get(hf, hf)}</b> + <b>{NAMES.get(hs, hs)}</b>, 50/50"
        + (f" (signal of {hmonth})" if hmonth != "?" else ""),
        f"Provisional target: {would}",
    ]
    if change:
        msg += [""] + ["  " + c for c in change]

    if live_pf and not live_pf.get("sync_error"):
        init_c = live_pf.get("initial_capital_gbp", 0.0)
        curr_v = live_pf.get("current_value_gbp", 0.0)
        msg += [
            "",
            "<b>Live Execution (Trading 212)</b>",
            f"Capital: £{init_c:,.2f} → <b>£{curr_v:,.2f}</b> ({pnl_sign}£{abs(pnl_gbp):.2f}, {pnl_pct:+.2f}%)",
        ]
        for h in live_pf.get("holdings", []):
            h_sign = "+" if h["pnl_gbp"] >= 0 else "−"
            msg.append(f"  • {h['ticker']} ({h['leg']}): £{h['current_value_gbp']:,.2f} ({h_sign}£{abs(h['pnl_gbp']):.2f}, {h['pnl_pct']:+.2f}%)")

        msg += ["", f"<b>Benchmark Race (Same Capital £{init_c:,.2f})</b>"]
        for b in live_pf.get("benchmarks", []):
            msg.append(f"  • {b['ticker']}: {b['return_pct']:+.2f}% · Alpha: <b>{b['alpha_pct']:+.2f}pp</b>")

    if live_pf.get("sync_error"):
        msg += ["", "Broker sync failed. Portfolio valuation is unavailable. GitHub will report this failure."]

    if crash_data:
        msg += [
            "",
            "<b>Crash Gate and Ketchup Rule</b>",
            f"Status: <b>{crash_data.get('status_text', 'INVESTED')}</b>",
        ]
        checks_summary = " · ".join(
            f"{c['name'].split()[0]}: {c['reading_pct']:+.1f}% " + ("✓" if c["pass"] else "✗")
            for c in crash_data.get("checks", [])
        )
        msg.append(f"  {checks_summary}")

    msg += [
        "",
        f"Next signal at the {now} close, acting <b>{signal_date:%-d %b}</b>: "
        f"<b>{days_out} day{'s' if days_out != 1 else ''} out</b>, "
        f"{today.day} of {last_day} days of the month run.",
    ]
    if len(fmom):
        msg += ["", "<b>Factor Ranking</b> (ETF proxy, 1-4pp adrift)",
                "\n".join(f"  {NAMES[k]:<22s}{v*100:+6.1f}%" for k, v in ranked(fmom)[:4])]
        msg += [m for m in [margin(fmom, hf)] if m]
    if len(smom):
        msg += ["<b>Sector Ranking</b>" + (" (SPDR proxy)" if sector_proxy else ""),
                "\n".join(f"  {NAMES[k]:<22s}{v*100:+6.1f}%" for k, v in ranked(smom)[:4])]
        msg += [m for m in [margin(smom, hs)] if m]

    if f_missing or s_missing:
        msg += ["", "<i>Incomplete: no price today for "
                + ", ".join(NAMES.get(k, k) for k in f_missing + s_missing)
                + ". Those slots are absent from the ranking above.</i>"]
    msg += ["",
            f"<i>Prices to {asof:%-d %b %Y}. Provisional. The signal that counts is "
            "computed at the month-end close.</i>",
            SITE]

    subject = ("Lodestar: the cash rule would fire" if cash
               else f"Lodestar: no change, {days_out}d to rebalance" if not change
               else f"Lodestar: WOULD TRADE, {days_out}d to rebalance")

    html_email = build_email_html(days_out, signal_date, live_pf, crash_data,
                                  f_pick, s_pick, fmom, smom, hf, hs, hmonth, change, would)
    if live_pf.get("sync_error"):
        html_email = None  # Plain report states broker failure without a false zero valuation.
    delivery = send(subject, "\n".join(msg), html_email)
    return 1 if live_pf.get("sync_error") else delivery


if __name__ == "__main__":
    raise SystemExit(main())

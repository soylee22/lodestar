#!/usr/bin/env python3
"""Lodestar daily read: where the signal stands part-way through the month.

Consumes the same shared provisional momentum engine (pipeline.emerging) and
crash-protection evaluator (pipeline.crash_protection) used to build the
website, so Telegram, HTML email, and https://soylee22.github.io/lodestar/
never diverge.
"""
from __future__ import annotations

import argparse
from datetime import date
import html
import json
from pathlib import Path
import sys

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from mailer import email  # noqa: E402
from notify import telegram  # noqa: E402
from pipeline.crash_protection import get_crash_protection_data  # noqa: E402
from pipeline.emerging import (  # noqa: E402
    closes,
    compute_emerging_data,
    momentum,
    month_end_level,
    ranked,
)
from pipeline.live_portfolio import get_live_portfolio_data  # noqa: E402
from universe import (  # noqa: E402
    FACTOR_PROXY,
    LSE,
    NAMES,
    SECTOR_PROXY,
    SECTOR_YF,
    held_now as _universe_held_now,
)

SITE = "https://soylee22.github.io/lodestar/"


def held_now() -> tuple[str, str, str]:
    """(factor slot, sector slot, month the signal was computed at)."""
    return _universe_held_now(HERE, date.today())


def _esc(val: object) -> str:
    return html.escape(str(val if val is not None else ""), quote=True)


def _format_holding_line(items: list[dict]) -> str:
    if not items:
        return "Unavailable"
    if len(items) == 1 and items[0].get("code") == "CASH":
        return "Cash (100%)"
    return " + ".join(f"{it['name']} ({it['ticker']})" for it in items)


def _render_rank_table_html(title: str, subtitle: str, rows: list[dict], margin_text: str | None) -> str:
    if not rows:
        return ""
    tr_html = ""
    for r in rows:
        ret = float(r.get("momentum_pct", 0.0))
        ret_col = "#16a34a" if ret >= 0 else "#dc2626"
        status_pill = ""
        if r.get("is_held"):
            status_pill = '<span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:700;background:#dcfce7;color:#15803d;">HELD</span>'
        elif r.get("rank") == 2:
            status_pill = '<span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:700;background:#dbeafe;color:#1d4ed8;">#2</span>'
        else:
            status_pill = '<span style="color:#94a3b8;font-size:11px;">-</span>'
        tr_html += f"""
        <tr style="border-bottom:1px solid #e2e8f0;">
          <td style="padding:6px;font-family:ui-monospace,Menlo,monospace;font-weight:700;color:#475569;">#{int(r.get('rank', 0))}</td>
          <td style="padding:6px;">
            <strong style="color:#0f172a;">{_esc(r.get('name'))}</strong>
            <span style="font-size:11px;color:#64748b;">({_esc(r.get('ticker'))})</span>
          </td>
          <td style="padding:6px;text-align:right;font-family:ui-monospace,Menlo,monospace;font-weight:600;color:{ret_col};">{ret:+.2f}%</td>
          <td style="padding:6px;text-align:right;font-family:ui-monospace,Menlo,monospace;color:#64748b;">{_esc(r.get('lead_margin', '-'))}</td>
          <td style="padding:6px;text-align:right;">{status_pill}</td>
        </tr>"""
    margin_html = (
        f'<div style="margin-top:6px;font-size:12px;color:#475569;font-style:italic;">{_esc(margin_text)}</div>'
        if margin_text else ""
    )
    return f"""
      <div style="margin-bottom:18px;">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;gap:8px;flex-wrap:wrap;">
          <h3 style="font-size:13px;font-weight:700;color:#0f172a;margin:0;">{_esc(title)}</h3>
          <span style="font-size:11px;color:#64748b;">{_esc(subtitle)}</span>
        </div>
        <div style="overflow-x:auto;">
          <table style="width:100%;border-collapse:collapse;font-size:12px;text-align:left;">
            <thead>
              <tr style="border-bottom:2px solid #cbd5e1;color:#64748b;font-size:10px;text-transform:uppercase;">
                <th style="padding:6px;">Rank</th>
                <th style="padding:6px;">Index</th>
                <th style="padding:6px;text-align:right;">8M Ret</th>
                <th style="padding:6px;text-align:right;">Margin</th>
                <th style="padding:6px;text-align:right;">Book</th>
              </tr>
            </thead>
            <tbody>{tr_html}</tbody>
          </table>
        </div>
        {margin_html}
      </div>"""


def build_telegram_message(emerging: dict, live_pf: dict, crash_data: dict) -> str:
    days_out = emerging.get("days_out", "?")
    next_date = emerging.get("next_action_date", "?")
    days_run = emerging.get("days_run", "")
    action = emerging.get("action", "HOLD")
    cash = bool(emerging.get("cash_triggered", False))
    changes = emerging.get("changes", [])

    if cash:
        head = "<b>Lodestar: the cash rule would fire</b>"
    elif not changes and action == "HOLD":
        head = "<b>Lodestar: rebalance today has NO CHANGE</b>"
    else:
        head = "<b>Lodestar: rebalance today WOULD TRADE</b>"

    current_items = emerging.get("current_holdings", [])
    projected_items = emerging.get("projected_holdings", [])
    hmonth = emerging.get("holding_month", "?")

    if emerging.get("held_cash"):
        held_line = "Holding now: <b>Cash (100%)</b>"
    else:
        held_names = " + ".join(f"<b>{_esc(it['name'])}</b>" for it in current_items)
        held_line = f"Holding now: {held_names}, 50/50"
    if hmonth and hmonth != "?":
        held_line += f" (signal of {_esc(hmonth)})"

    if cash:
        would_line = "<b>cash, both legs</b>"
    elif projected_items:
        would_line = " + ".join(
            f"{_esc(it['name'])} (<code>{_esc(it['ticker'])}</code>)"
            for it in projected_items
        )
    else:
        would_line = "unavailable"

    msg = [
        f"<b>Lodestar Daily · {days_out} days to new month</b> (acting {_esc(next_date)})",
        head,
        "",
        held_line,
        f"Provisional target: {would_line}",
    ]

    if changes:
        msg.append("")
        for ch in changes:
            msg.append(
                f"  {_esc(ch['leg'])}: {_esc(ch['from_name'])} -> <b>{_esc(ch['to_name'])}</b>"
            )

    pnl_gbp = live_pf.get("total_pnl_gbp", 0.0)
    pnl_pct = live_pf.get("total_pnl_pct", 0.0)
    pnl_sign = "+" if pnl_gbp >= 0 else "-"

    if live_pf and not live_pf.get("sync_error"):
        init_c = live_pf.get("initial_capital_gbp", 0.0)
        curr_v = live_pf.get("current_value_gbp", 0.0)
        msg += [
            "",
            "<b>Live Execution (Trading 212)</b>",
            f"Capital: £{init_c:,.2f} -> <b>£{curr_v:,.2f}</b> ({pnl_sign}£{abs(pnl_gbp):.2f}, {pnl_pct:+.2f}%)",
        ]
        for h in live_pf.get("holdings", []):
            h_sign = "+" if h["pnl_gbp"] >= 0 else "-"
            msg.append(
                f"  • {_esc(h['ticker'])} ({_esc(h['leg'])}): £{h['current_value_gbp']:,.2f} "
                f"({h_sign}£{abs(h['pnl_gbp']):.2f}, {h['pnl_pct']:+.2f}%)"
            )

        msg += ["", f"<b>Benchmark Race (Same Capital £{init_c:,.2f})</b>"]
        for b in live_pf.get("benchmarks", []):
            msg.append(
                f"  • {_esc(b['ticker'])}: {b['return_pct']:+.2f}% · Alpha: <b>{b['alpha_pct']:+.2f}pp</b>"
            )
    elif live_pf.get("sync_error"):
        msg += [
            "",
            "Broker sync failed. Portfolio valuation is unavailable. GitHub will report this failure.",
        ]

    if crash_data:
        msg += [
            "",
            "<b>Crash Gate and Research Checks</b>",
            f"Status: <b>{_esc(crash_data.get('status_text', 'INVESTED'))}</b>",
        ]
        checks_summary = " · ".join(
            f"{_esc(c['name'].split()[0])}: {c['reading_pct']:+.1f}% " + ("✓" if c["pass"] else "✗")
            for c in crash_data.get("checks", [])
        )
        if checks_summary:
            msg.append(f"  {checks_summary}")

    day_label = "day" if days_out == 1 else "days"
    msg += [
        "",
        f"Next signal at the {_esc(emerging.get('month', '?'))} close, acting <b>{_esc(next_date)}</b>: "
        f"<b>{days_out} {day_label} out</b>, {_esc(days_run)} of the month run.",
    ]

    factors = emerging.get("factors", [])
    sectors = emerging.get("sectors", [])
    if factors:
        msg += [
            "",
            "<b>Factor Ranking</b> (ETF proxy, 1-4pp adrift)",
            "\n".join(f"  {r['name']:<22s}{r['momentum_pct']:+6.1f}%" for r in factors[:4]),
        ]
        f_margin = emerging.get("factor_lead", {}).get("margin_text")
        if f_margin:
            msg.append(f"  <i>{_esc(f_margin)}</i>")

    if sectors:
        sector_tag = " (SPDR proxy)" if emerging.get("sector_proxy") else ""
        msg += [
            f"<b>Sector Ranking</b>{sector_tag}",
            "\n".join(f"  {r['name']:<22s}{r['momentum_pct']:+6.1f}%" for r in sectors[:4]),
        ]
        s_margin = emerging.get("sector_lead", {}).get("margin_text")
        if s_margin:
            msg.append(f"  <i>{_esc(s_margin)}</i>")

    missing = emerging.get("factor_missing", []) + emerging.get("sector_missing", [])
    if missing:
        msg += [
            "",
            "<i>Incomplete: no price today for "
            + ", ".join(_esc(NAMES.get(k, k)) for k in missing)
            + ". Those slots are absent from the ranking above.</i>",
        ]

    msg += [
        "",
        f"<i>Prices to {_esc(emerging.get('asof', '?'))} (built {_esc(emerging.get('computed_at', '?'))}). "
        "Provisional. The signal that counts is computed at the month-end close.</i>",
        SITE,
    ]
    return "\n".join(msg)


def build_email_html(emerging: dict, live_pf: dict, crash_data: dict) -> str:
    days_out = emerging.get("days_out", "?")
    next_date = emerging.get("next_action_date", "?")
    asof = emerging.get("asof", "?")
    computed_at = emerging.get("computed_at", "?")
    action = emerging.get("action", "HOLD")
    action_text = emerging.get("action_text", "HOLD · ZERO TURNOVER")
    action_summary = emerging.get("action_summary", "")
    changes = emerging.get("changes", [])

    if action == "HOLD":
        action_bg, action_fg, action_bd = "#dcfce7", "#15803d", "#86efac"
    elif action == "CASH":
        action_bg, action_fg, action_bd = "#fee2e2", "#b91c1c", "#fca5a5"
    else:
        action_bg, action_fg, action_bd = "#fef3c7", "#b45309", "#fcd34d"

    tone = crash_data.get("status_tone", "fail" if crash_data.get("cash_triggered") else "pass") if crash_data else "warn"
    if tone == "fail":
        crash_col = "#dc2626"
    elif tone == "warn":
        crash_col = "#d97706"
    else:
        crash_col = "#16a34a"

    crash_status = ("100% Cash" if crash_data.get("cash_triggered") else "100% Invested") if crash_data else "Unavailable"
    crash_heading = crash_data.get("status_text", "Status unavailable") if crash_data else "Risk data unavailable"
    crash_note = f"19M floor: {crash_data['m19_pct']:+.2f}% vs -25%" if crash_data and crash_data.get("m19_pct") is not None else "Lodestar 19-month floor"

    held_str = _format_holding_line(emerging.get("current_holdings", []))
    target_str = _format_holding_line(emerging.get("projected_holdings", []))
    hmonth = emerging.get("holding_month", "?")

    changes_html = ""
    if changes:
        lines = "".join(
            f'<li style="margin:3px 0;"><strong>{_esc(c["leg"].capitalize())}:</strong> '
            f'{_esc(c["from_name"])} -&gt; <strong>{_esc(c["to_name"])} ({_esc(c["to_ticker"])})</strong></li>'
            for c in changes
        )
        changes_html = f'<ul style="margin:8px 0 0;padding-left:18px;font-size:13px;color:#0f172a;">{lines}</ul>'

    init_cap = live_pf.get("initial_capital_gbp", 0.0)
    curr_val = live_pf.get("current_value_gbp", 0.0)
    pnl_gbp = live_pf.get("total_pnl_gbp", 0.0)
    pnl_pct = live_pf.get("total_pnl_pct", 0.0)
    pnl_color = "#16a34a" if pnl_gbp >= 0 else "#dc2626"
    pnl_sign = "+" if pnl_gbp >= 0 else "-"

    holdings_rows = ""
    for h in live_pf.get("holdings", []):
        h_pnl_col = "#16a34a" if h["pnl_gbp"] >= 0 else "#dc2626"
        h_sign = "+" if h["pnl_gbp"] >= 0 else "-"
        holdings_rows += f"""
        <tr style="border-bottom:1px solid #e2e8f0;">
          <td style="padding:10px 8px;font-weight:600;color:#0f172a;">{_esc(h['leg'])}</td>
          <td style="padding:10px 8px;">
            <strong style="color:#0f172a;">{_esc(h['ticker'])}</strong>
            <span style="font-size:11px;color:#64748b;">({_esc(h['code'])})</span><br>
            <span style="font-size:11px;color:#64748b;">{_esc(h['name'])}</span>
          </td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">{h['shares']:.4f}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">£{h['cost_gbp']:,.2f}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">£{h['current_value_gbp']:,.2f}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;font-weight:600;color:{h_pnl_col};">
            {h_sign}£{abs(h['pnl_gbp']):.2f} ({h['pnl_pct']:+.2f}%)
          </td>
        </tr>"""

    bm_rows = ""
    for b in live_pf.get("benchmarks", []):
        alpha = b.get("alpha_pct", 0.0)
        ret = b.get("return_pct", 0.0)
        ret_col = "#16a34a" if ret >= 0 else "#dc2626"
        alpha_bg = "#dcfce7" if alpha >= 0 else "#fee2e2"
        alpha_col = "#15803d" if alpha >= 0 else "#b91c1c"
        alpha_badge = (
            f'<span style="display:inline-block;padding:2px 6px;border-radius:4px;font-size:11px;'
            f'font-weight:600;background:{alpha_bg};color:{alpha_col};">{alpha:+.2f}pp</span>'
        )
        fmt_p = lambda p: f"£{p:.4f}" if p < 10 else f"£{p:.2f}"
        bm_rows += f"""
        <tr style="border-bottom:1px solid #e2e8f0;">
          <td style="padding:10px 8px;font-weight:600;color:#0f172a;">{_esc(b['ticker'])}</td>
          <td style="padding:10px 8px;color:#475569;">{_esc(b['name'])}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">{fmt_p(b['entry_price_gbp'])}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">{fmt_p(b['current_price_gbp'])}</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;color:{ret_col};font-weight:600;">{ret:+.2f}%</td>
          <td style="padding:10px 8px;text-align:right;font-family:ui-monospace,Menlo,monospace;">{alpha_badge}</td>
        </tr>"""

    check_rows = ""
    for c in crash_data.get("checks", []):
        badge = (
            '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;background:#dcfce7;color:#15803d;">✓ PASS</span>'
            if c["pass"]
            else '<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;background:#fee2e2;color:#b91c1c;">✗ FAIL</span>'
        )
        chk_col = "#16a34a" if c["reading_pct"] >= 0 else "#dc2626"
        check_rows += f"""
        <tr style="border-bottom:1px solid #e2e8f0;">
          <td style="padding:8px;">
            <strong style="color:#0f172a;">{_esc(c['name'])}</strong><br>
            <span style="font-size:11px;color:#64748b;">{_esc(c['desc'])}</span>
          </td>
          <td style="padding:8px;font-size:12px;color:#64748b;">{_esc(c['rule'])}</td>
          <td style="padding:8px;text-align:right;font-family:ui-monospace,Menlo,monospace;font-weight:600;color:{chk_col};">{c['reading_pct']:+.2f}%</td>
          <td style="padding:8px;text-align:right;">{badge}</td>
        </tr>"""

    factor_table_html = _render_rank_table_html(
        "Factor Momentum Ranking (8-Month)",
        "US ETF proxy (1-4pp vs MSCI month-end)",
        emerging.get("factors", []),
        emerging.get("factor_lead", {}).get("margin_text"),
    )
    sector_sub = "SPDR ETF proxy" if emerging.get("sector_proxy") else "S&P 500 sector indices"
    sector_table_html = _render_rank_table_html(
        "Sector Momentum Ranking (8-Month)",
        sector_sub,
        emerging.get("sectors", []),
        emerging.get("sector_lead", {}).get("margin_text"),
    )

    spx_lvl_str = f"S&amp;P 500: {crash_data['latest_price']:,.2f} ({_esc(crash_data.get('asof', '?'))})" if crash_data and crash_data.get("latest_price") else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lodestar Daily</title>
</head>
<body style="margin:0;padding:24px 0;background-color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;color:#0f172a;">
  <div style="max-width:660px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 6px -1px rgba(0,0,0,0.07),0 2px 4px -2px rgba(0,0,0,0.05);border:1px solid #e2e8f0;">

    <div style="background:#09090b;padding:26px 24px;border-bottom:1px solid #27272a;">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
        <div>
          <span style="font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#a1a1aa;">Systematic Rotation Drumbeat</span>
          <h1 style="margin:4px 0 0;font-size:24px;font-weight:800;color:#ffffff;letter-spacing:-0.03em;">Lodestar</h1>
        </div>
        <div style="background:#18181b;border:1px solid #3f3f46;border-radius:999px;padding:6px 14px;color:#38bdf8;font-size:12px;font-weight:600;font-family:ui-monospace,Menlo,monospace;">
          {_esc(days_out)} Days to New Month
        </div>
      </div>
      <p style="margin:12px 0 0;font-size:13px;color:#d4d4d8;line-height:1.5;">
        Unified snapshot matching Telegram and the public Lodestar dashboard. Market closes to <strong>{_esc(asof)}</strong>, built <strong>{_esc(computed_at)}</strong>.
      </p>
    </div>

    <div style="padding:24px;">

      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px 18px;margin-bottom:22px;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;">
          <span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#475569;">Next Month's Target Allocation (Provisional)</span>
          <span style="display:inline-block;padding:4px 10px;border-radius:6px;font-size:11px;font-weight:700;font-family:ui-monospace,Menlo,monospace;background:{action_bg};color:{action_fg};border:1px solid {action_bd};">{_esc(action_text)}</span>
        </div>
        <div style="font-size:14px;color:#0f172a;line-height:1.5;">
          <div><strong>Holding now</strong> (signal {_esc(hmonth)}): {_esc(held_str)}</div>
          <div style="margin-top:4px;"><strong>Provisional target</strong> (acting {_esc(next_date)}): <strong>{_esc(target_str)}</strong></div>
        </div>
        <p style="margin:8px 0 0;font-size:13px;color:#475569;line-height:1.45;">{_esc(action_summary)}</p>
        {changes_html}
      </div>

      {factor_table_html}
      {sector_table_html}

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:22px 0;">
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
          <div style="font-size:16px;font-weight:700;color:{crash_col};margin-top:6px;">{_esc(crash_status)}</div>
          <div style="font-size:11px;color:#94a3b8;margin-top:2px;">{_esc(crash_note)}</div>
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
            <tbody>{holdings_rows}</tbody>
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
            <tbody>{bm_rows}</tbody>
          </table>
        </div>
      </div>

      <div style="margin-bottom:24px;">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;gap:8px;flex-wrap:wrap;">
          <h2 style="font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;color:#0f172a;margin:0;">Crash Gate &amp; Research Checks</h2>
          <span style="font-size:12px;color:{crash_col};font-weight:700;">{_esc(crash_heading)}</span>
        </div>
        <div style="font-size:11px;color:#64748b;margin-bottom:8px;">{spx_lvl_str}</div>
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
            <tbody>{check_rows}</tbody>
          </table>
        </div>
      </div>

      <div style="text-align:center;padding:16px 0;">
        <a href="{SITE}" style="display:inline-block;background:#09090b;color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;padding:12px 28px;border-radius:8px;">
          Open Live Dashboard -&gt;
        </a>
      </div>

    </div>

    <div style="background:#f8fafc;padding:16px 24px;border-top:1px solid #e2e8f0;font-size:12px;color:#94a3b8;text-align:center;line-height:1.5;">
      Lodestar Factor &amp; Sector Rotation · Next signal acts <strong>{_esc(next_date)}</strong> ({_esc(days_out)} days away).<br>
      Prices to {_esc(asof)} ({_esc(computed_at)}). Broker valuation is checked on each scheduled report.
    </div>

  </div>
</body>
</html>
"""


def send(subject: str, telegram_body: str, html_body: str | None = None) -> int:
    plain = telegram_body
    for a, b in (
        ("<b>", ""),
        ("</b>", ""),
        ("<i>", ""),
        ("</i>", ""),
        ("<code>", ""),
        ("</code>", ""),
        ("&amp;", "&"),
    ):
        plain = plain.replace(a, b)
    if html_body is None:
        html_body = (
            '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'font-size:15px;line-height:1.5;max-width:640px">'
            + telegram_body.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
            + "</div>"
        )
    telegram_ok = telegram(telegram_body)
    email_ok = email(subject, plain, html_body)
    print(f"Delivery status: Telegram={telegram_ok}, email={email_ok}")
    return 0 if telegram_ok and email_ok else 1


def _load_snapshot(today: date) -> tuple[dict, dict] | None:
    em_path = HERE / "data" / "emerging.json"
    cp_path = HERE / "data" / "crash_protection.json"
    if not em_path.exists():
        return None
    try:
        emerging = json.loads(em_path.read_text())
        crash_data = json.loads(cp_path.read_text()) if cp_path.exists() else {}
        if (
            not emerging
            or emerging.get("stale_cache")
            or (not emerging.get("factors") and not emerging.get("sectors"))
        ):
            return None
        return emerging, crash_data
    except Exception as e:
        print(f"  !! snapshot read error ({e}); recomputing live")
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send Lodestar daily Telegram and email report.")
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Reuse data/emerging.json and data/crash_protection.json just built for the website.",
    )
    args = parser.parse_args(argv)

    today = date.today()
    snap = _load_snapshot(today) if args.snapshot else None
    if snap is not None:
        emerging, crash_data = snap
    else:
        try:
            crash_data = get_crash_protection_data(today=today)
        except Exception as e:
            print(f"  !! crash protection load error: {e}")
            crash_data = {}

        emerging = compute_emerging_data(
            today=today,
            root=HERE,
            crash_data=crash_data,
            use_cache=False,
        )
    if not emerging or (not emerging.get("factors") and not emerging.get("sectors")):
        return send(
            "Lodestar: no read today",
            "<b>Lodestar: no read today</b>\n\nYahoo returned no usable price "
            "history for either leg. Nothing is wrong with the strategy and "
            "nothing needs doing. The month-end signal is computed from a "
            "different path.\n\n" + SITE,
        )

    try:
        live_pf = get_live_portfolio_data(broker=True)
    except Exception as e:
        print(f"  !! live portfolio load error: {e}")
        live_pf = {"sync_error": True}

    tg_body = build_telegram_message(emerging, live_pf, crash_data)
    days_out = emerging.get("days_out", "?")
    pnl_pct = live_pf.get("total_pnl_pct", 0.0)
    action_tag = (
        "CRASH GATE CASH" if emerging.get("cash_triggered")
        else "WOULD TRADE" if emerging.get("changes")
        else "NO CHANGE"
    )
    subject = (
        "Lodestar daily: broker valuation unavailable"
        if live_pf.get("sync_error")
        else f"Lodestar daily: {action_tag}, {pnl_pct:+.2f}%, {days_out}d to rebalance"
    )

    html_email = None if live_pf.get("sync_error") else build_email_html(emerging, live_pf, crash_data)
    delivery = send(subject, tg_body, html_email)
    return 1 if live_pf.get("sync_error") else delivery


if __name__ == "__main__":
    raise SystemExit(main())

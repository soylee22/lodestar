#!/usr/bin/env python3
"""Monthly job: recompute the signal, rebuild the page, send the alert.

Run by .github/workflows/monthly.yml on the 1st of each month, before the
London open. Safe to run by hand at any time.

If a data source is unavailable, this refuses to publish a signal and says so
in the alert. A wrong signal is worse than a late one.
"""
import json, os, subprocess, sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from pipeline import build            # noqa: E402
from pipeline import holdings        # noqa: E402
from notify import telegram           # noqa: E402
from mailer import email              # noqa: E402
from universe import DEFENSIVE_NAMES, DEFENSIVE_TRADABLE, FACTOR_NAMES, SECTOR_NAMES  # noqa: E402

NAMES = {**FACTOR_NAMES, **SECTOR_NAMES, **DEFENSIVE_NAMES}


STATE = HERE / "data" / "last_alert.json"


def already_alerted(month: str, picks: str) -> bool:
    """True if this exact signal has already been announced."""
    if not STATE.is_file():
        return False
    try:
        prev = json.loads(STATE.read_text())
    except Exception:
        return False
    return prev.get("month") == month and prev.get("picks") == picks


def record_alert(month: str, picks: str) -> None:
    STATE.write_text(json.dumps({"month": month, "picks": picks,
                                 "sent": date.today().isoformat()}, indent=2))


def _plain(html: str) -> str:
    for a, b in (("<b>", ""), ("</b>", ""), ("<i>", ""), ("</i>", ""),
                 ("<code>", ""), ("</code>", ""), ("&amp;", "&")):
        html = html.replace(a, b)
    return html


def announce(subject: str, msg: str) -> bool:
    """Telegram and email carry the same words; only the wrapping differs."""
    ok = telegram(msg)
    body = ("<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
            "font-size:15px;line-height:1.5;max-width:640px\">"
            + msg.replace("\n", "<br>") + "</div>")
    email_ok = email(subject, _plain(msg), body)
    return ok and email_ok


def main(build_only=False) -> int:
    signal = build.main()
    try:
        holdings.build(signal)
    except Exception as e:               # holdings are decorative, never fatal
        print(f"  !! holdings refresh failed ({type(e).__name__}); keeping previous")
    subprocess.run([sys.executable, str(HERE / "build_data.py"), str(HERE / "data")], check=True)
    if build_only:
        print("Page rebuilt. Alert state unchanged.")
        return 0

    prev, stale = signal["previous"], signal.get("stale")
    fails = signal.get("source_failures") or []
    f, s = signal["factor"], signal["sector"]
    ft, st = signal["factor_ticker"], signal["sector_ticker"]
    canary = signal.get("canary") or {}
    tip_pct = canary.get("tip_score") * 100.0 if canary.get("tip_score") is not None else None
    ief_pct = canary.get("ief_score") * 100.0 if canary.get("ief_score") is not None else None
    bil_pct = canary.get("bil_score") * 100.0 if canary.get("bil_score") is not None else None
    def_slot = signal.get("defensive") or "DEF_BIL"
    def_ticker = signal.get("defensive_ticker") or DEFENSIVE_TRADABLE.get(def_slot, ("", "", "IB01.L", ""))[2]
    def_name = signal.get("defensive_name") or NAMES.get(def_slot, def_slot)
    changed = []

    if stale:
        msg = ("<b>Lodestar - SIGNAL NOT COMPUTED</b>\n\n"
               f"The index panel only runs to {signal['signal_month']}, but "
               f"{signal['expected_month']} has ended.\n"
               f"Sources unavailable: {len(fails)}\n\n"
               "<b>Do not trade on this.</b> The page has not been updated with a new month. "
               "Re-run the job once the data source is back.")
    elif signal["cash"]:
        score_line = (f"TIP 1-3-6-12 sits at <b>{tip_pct:+.2f}%</b> (&lt;= 0%) "
                      f"with IEF {ief_pct:+.2f}% vs BIL {bil_pct:+.2f}%."
                      if tip_pct is not None else "TIP 1-3-6-12 sits at or below 0%.")
        prev_def = prev.get("defensive")
        if prev.get("cash") and prev_def == def_slot:
            changed = []
            head = "<b>Lodestar - DEFENSIVE HOLD</b>"
            body = f"Already in {def_name} (<code>{def_ticker}</code>). Do nothing."
        elif prev.get("cash"):
            prev_t = prev.get("defensive_ticker") or NAMES.get(prev_def, prev_def)
            changed = [f"DEFENSIVE: sell {NAMES.get(prev_def, prev_def)} ({prev_t}) -> buy <b>{def_name}</b> (<code>{def_ticker}</code>), 100%"]
            head = "<b>Lodestar - DEFENSIVE SWAP</b>"
            body = changed[0]
        else:
            changed = [f"REGIME EXIT: sell {NAMES.get(prev['factor'], prev['factor'])} + {NAMES.get(prev['sector'], prev['sector'])} -> buy <b>{def_name}</b> (<code>{def_ticker}</code>), 100%"]
            head = "<b>Lodestar - GO DEFENSIVE</b>"
            body = changed[0]
        msg = (f"{head}\n\n"
               f"Signal at {signal['signal_month']} close: {score_line}\n\n"
               f"{body}\n\n"
               f"<b>Hold:</b> 100% <code>{def_ticker}</code> ({def_name})")
    else:
        if prev.get("cash"):
            prev_def = prev.get("defensive")
            prev_t = prev.get("defensive_ticker") or NAMES.get(prev_def, prev_def) or "Treasury sleeve"
            changed.append(f"RE-ENTER EQUITIES: sell {prev_t} -> buy <b>{NAMES.get(f, f)}</b> (<code>{ft}</code>) + <b>{NAMES.get(s, s)}</b> (<code>{st}</code>), 50/50")
        else:
            if f != prev["factor"]:
                changed.append(f"FACTOR: sell {NAMES.get(prev['factor'], prev['factor'])} -> "
                               f"buy <b>{NAMES.get(f, f)}</b> (<code>{ft}</code>)")
            if s != prev["sector"]:
                changed.append(f"SECTOR: sell {NAMES.get(prev['sector'], prev['sector'])} -> "
                               f"buy <b>{NAMES.get(s, s)}</b> (<code>{st}</code>)")
        head = "<b>Lodestar - NO TRADE</b>" if not changed else "<b>Lodestar - TRADE</b>"
        body = ("Both legs unchanged. Do nothing.\n" if not changed
                else "\n".join(changed) + "\n\nReset both legs to 50/50 while you are trading.\n")
        ftab = sorted(signal["factor_table"].items(), key=lambda kv: -kv[1])[:3]
        stab = sorted(signal["sector_table"].items(), key=lambda kv: -kv[1])[:3]
        proxied = [n for n, k in (("Factor", "factor_source"), ("Sector", "sector_source"))
                   if signal.get(k) == "ETF proxy"]
        src = ("" if not proxied else
               f"\n<i>{' and '.join(proxied)} leg{'s' if len(proxied) > 1 else ''} computed "
               "from ETF proxies: the index feed was unavailable. Momentum values differ "
               "by roughly 1-4pp from the indices.</i>\n")
        canary_line = (f"TIP 1-3-6-12: <b>{tip_pct:+.2f}%</b> (&gt; 0% risk-on)\n\n"
                       if tip_pct is not None else "")
        msg = (f"{head}\n\nSignal computed at the {signal['signal_month']} close.{src}\n\n"
               f"{canary_line}{body}\n"
               f"<b>Hold:</b> <code>{ft}</code> + <code>{st}</code>, 50/50\n\n"
               "<b>Factor ranking</b>\n" +
               "\n".join(f"  {NAMES.get(k,k)} {v*100:+.1f}%" for k, v in ftab) +
               "\n<b>Sector ranking</b>\n" +
               "\n".join(f"  {NAMES.get(k,k)} {v*100:+.1f}%" for k, v in stab) +
               "\n\nhttps://soylee22.github.io/lodestar/")
    picks = f"DEF:{def_slot}" if signal["cash"] else f"{f}+{s}"
    month = signal["signal_month"]

    # Alert once per signal month. Failures always shout, every retry, because a
    # month with no signal is the thing you must not miss.
    if stale:
        print("\n--- alert (failure) ---\n" + msg)
        announce("Lodestar: SIGNAL NOT COMPUTED - do not trade", msg)
        return 2

    if already_alerted(month, picks):
        print(f"\nalready alerted {month} ({picks}); page refreshed, no repeat message")
        return 0

    print("\n--- alert ---\n" + msg)
    if signal["cash"]:
        if not prev.get("cash"):
            subject = f"Lodestar {month}: GO DEFENSIVE - {def_ticker}"
        elif changed:
            subject = f"Lodestar {month}: DEFENSIVE SWAP - {def_ticker}"
        else:
            subject = f"Lodestar {month}: hold defensive ({def_ticker}), site updated"
    elif changed:
        subject = f"Lodestar {month}: TRADE - {NAMES.get(f, f)} + {NAMES.get(s, s)}"
    else:
        subject = f"Lodestar {month}: no trade, site updated"
    site = "https://soylee22.github.io/lodestar/"
    msg = msg.rstrip().removesuffix(site).rstrip()
    if tip_pct is not None:
        msg += f"\n\nCrash protection: TIP 1-3-6-12 canary ({tip_pct:+.2f}%, IEF {ief_pct:+.2f}% vs BIL {bil_pct:+.2f}%)."
    else:
        msg += "\n\nCrash protection: monthly TIP 1-3-6-12 canary."
    msg += (f"\n\nPage built: holdings for {signal['holding_month']}, "
            f"performance through {signal['performance_month']}.\n{site}")
    if not announce(subject, msg):
        print("Alert delivery incomplete. Signal will be retried.")
        return 1
    record_alert(month, picks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(build_only="--build-only" in sys.argv))

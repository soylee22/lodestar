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
from universe import FACTOR_NAMES, SECTOR_NAMES  # noqa: E402

NAMES = {**FACTOR_NAMES, **SECTOR_NAMES}


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
    email(subject, _plain(msg), body)
    return ok


def main() -> int:
    signal = build.main()
    try:
        holdings.build(signal)
    except Exception as e:               # holdings are decorative, never fatal
        print(f"  !! holdings refresh failed ({type(e).__name__}); keeping previous")
    subprocess.run([sys.executable, str(HERE / "build_data.py"), str(HERE / "data")], check=True)

    prev, stale = signal["previous"], signal.get("stale")
    fails = signal.get("source_failures") or []
    f, s = signal["factor"], signal["sector"]
    ft, st = signal["factor_ticker"], signal["sector_ticker"]

    if stale:
        msg = ("<b>Lodestar — SIGNAL NOT COMPUTED</b>\n\n"
               f"The index panel only runs to {signal['signal_month']}, but "
               f"{signal['expected_month']} has ended.\n"
               f"Sources unavailable: {len(fails)}\n\n"
               "<b>Do not trade on this.</b> The page has not been updated with a new month. "
               "Re-run the job once the data source is back.")
    elif signal["cash"]:
        msg = ("<b>Lodestar — GO TO CASH</b>\n\n"
               f"Signal for {signal['signal_month']}: the S&amp;P 500 sits more than 25% below its "
               "level nineteen months ago, so <b>both legs move to cash</b>.\n\n"
               f"Sell {prev['factor']} and {prev['sector']}. Hold cash until the signal lifts.\n\n"
               "This has never fired before in the record.")
    else:
        changed = []
        if f != prev["factor"]:
            changed.append(f"FACTOR: sell {NAMES.get(prev['factor'], prev['factor'])} → "
                           f"buy <b>{NAMES.get(f, f)}</b> (<code>{ft}</code>)")
        if s != prev["sector"]:
            changed.append(f"SECTOR: sell {NAMES.get(prev['sector'], prev['sector'])} → "
                           f"buy <b>{NAMES.get(s, s)}</b> (<code>{st}</code>)")
        head = "<b>Lodestar — NO TRADE</b>" if not changed else "<b>Lodestar — TRADE</b>"
        body = ("Both legs unchanged. Do nothing.\n" if not changed
                else "\n".join(changed) + "\n\nReset both legs to 50/50 while you are trading.\n")
        ftab = sorted(signal["factor_table"].items(), key=lambda kv: -kv[1])[:3]
        stab = sorted(signal["sector_table"].items(), key=lambda kv: -kv[1])[:3]
        src = ("" if signal.get("factor_source") == "MSCI" else
               "\n<i>Factor leg computed from ETF proxies: MSCI was unavailable. "
               "Momentum values differ by roughly 1-4pp from the indices.</i>\n")
        msg = (f"{head}\n\nSignal computed at the {signal['signal_month']} close.{src}\n\n{body}\n"
               f"<b>Hold:</b> <code>{ft}</code> + <code>{st}</code>, 50/50\n\n"
               "<b>Factor ranking</b>\n" +
               "\n".join(f"  {NAMES.get(k,k)} {v*100:+.1f}%" for k, v in ftab) +
               "\n<b>Sector ranking</b>\n" +
               "\n".join(f"  {NAMES.get(k,k)} {v*100:+.1f}%" for k, v in stab) +
               "\n\nhttps://soylee22.github.io/lodestar/")
    picks = "CASH" if signal["cash"] else f"{f}+{s}"
    month = signal["signal_month"]

    # Alert once per signal month. Failures always shout, every retry, because a
    # month with no signal is the thing you must not miss.
    if stale:
        print("\n--- alert (failure) ---\n" + msg)
        announce("Lodestar: SIGNAL NOT COMPUTED — do not trade", msg)
        return 2

    if already_alerted(month, picks):
        print(f"\nalready alerted {month} ({picks}); page refreshed, no repeat message")
        return 0

    print("\n--- alert ---\n" + msg)
    if signal["cash"]:
        subject = f"Lodestar {month}: GO TO CASH"
    elif changed:
        subject = f"Lodestar {month}: TRADE — {NAMES.get(f, f)} + {NAMES.get(s, s)}"
    else:
        subject = f"Lodestar {month}: no trade, site updated"
    site = "https://soylee22.github.io/lodestar/"
    msg = msg.rstrip().removesuffix(site).rstrip()
    msg += f"\n\nThe site has been rebuilt with {month} included:\n{site}"
    if not announce(subject, msg):
        print("(telegram not configured; message printed only)")
    record_alert(month, picks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

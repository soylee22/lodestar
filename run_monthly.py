#!/usr/bin/env python3
"""Monthly job: recompute the signal, rebuild the page, send the alert.

Run by .github/workflows/monthly.yml on the 1st of each month, before the
London open. Safe to run by hand at any time.

If a data source is unavailable, this refuses to publish a signal and says so
in the alert. A wrong signal is worse than a late one.
"""
import json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from pipeline import build            # noqa: E402
from notify import telegram           # noqa: E402
from universe import FACTOR_NAMES, SECTOR_NAMES  # noqa: E402

NAMES = {**FACTOR_NAMES, **SECTOR_NAMES}


def main() -> int:
    signal = build.main()
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
        msg = (f"{head}\n\nSignal computed at the {signal['signal_month']} close.\n\n{body}\n"
               f"<b>Hold:</b> <code>{ft}</code> + <code>{st}</code>, 50/50\n\n"
               "<b>Factor ranking</b>\n" +
               "\n".join(f"  {NAMES.get(k,k)} {v*100:+.1f}%" for k, v in ftab) +
               "\n<b>Sector ranking</b>\n" +
               "\n".join(f"  {NAMES.get(k,k)} {v*100:+.1f}%" for k, v in stab) +
               "\n\nhttps://soylee22.github.io/lodestar/")
    print("\n--- alert ---\n" + msg)
    if not telegram(msg):
        print("(telegram not configured; message printed only)")
    return 2 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())

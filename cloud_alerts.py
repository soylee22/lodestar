"""GitHub-only delivery checks, Pullback notifications and failure alerts."""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import html
import json
import os
import urllib.request

from notify import telegram
from mailer import email

STATE = Path(__file__).resolve().parent / 'data' / 'pullback-alert-state.json'
SITE = 'https://soylee22.github.io/pullback-scanner/'


def fetch(name):
    with urllib.request.urlopen(SITE + 'data/' + name, timeout=30) as response:
        return json.load(response)


def deliver(subject, body, key=None, state=None):
    receipt = state.setdefault('deliveries', {}).setdefault(key, {}) if key else {}
    if not receipt.get('telegram'):
        receipt['telegram'] = telegram(body)
    if not receipt.get('email'):
        receipt['email'] = email(subject, body.replace('<b>', '').replace('</b>', ''))
    if key:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(state, indent=2) + '\n')
    if not all(receipt.get(channel) for channel in ['telegram', 'email']):
        raise RuntimeError('Notification delivery incomplete')


def pullback():
    scan = fetch('scan.json')
    journal = fetch('journal.json')
    state = json.loads(STATE.read_text()) if STATE.exists() else {'deliveries': {}}
    entries = journal.get('entries', [])
    known = set(state.get('known', []))
    current = {f"{r['symbol']}:{r['date']}:{r['type']}:{r['version']}": r for r in entries}
    if not state.get('initialised'):
        state.update(initialised=True, known=list(current), generated=scan['generated'])
        STATE.write_text(json.dumps(state, indent=2) + '\n')
        print('Pullback notification baseline saved; prior events will not be repeated')
        return
    for key in sorted(set(current) - known):
        row = current[key]
        body = (f"<b>Pullback confirmed · {html.escape(row['label'])}</b>\n"
                f"{html.escape(row['type'])} · completed week {row['date']}\n"
                f"Scan coverage: {scan['count']} instruments, {len(scan.get('failed', []))} failed.\n"
                f"Signal for review.\n{SITE}")
        deliver('Pullback confirmed: ' + row['label'], body, key, state)
        known.add(key)
    state.update(known=sorted(known), generated=scan['generated'])
    STATE.write_text(json.dumps(state, indent=2) + '\n')
    print(f"Pullback coverage {scan['count']}; no repeated notifications")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['test', 'pullback', 'failure'])
    args = parser.parse_args()
    if args.mode == 'test':
        from pipeline.broker import health
        health()
        scan = fetch('scan.json')
        body = ("<b>GitHub automation delivery check</b>\n"
                "Trading 212 read authenticated on the GitHub runner.\n"
                f"Pullback coverage: {scan['count']} instruments, {len(scan.get('failed', []))} failed.\n"
                "Lodestar refresh and weekday reports are scheduled on GitHub.\n"
                "Telegram and email delivery are being checked by this message.\n"
                "Your computer is not required.\nhttps://soylee22.github.io/lodestar/")
        deliver('Lodestar GitHub automation delivery check', body)
    elif args.mode == 'pullback':
        pullback()
    else:
        url = (f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'soylee22/lodestar')}"
               f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
        state = json.loads(STATE.read_text()) if STATE.exists() else {'deliveries': {}}
        key = 'failure:' + os.environ.get('GITHUB_WORKFLOW', 'job') + ':' + datetime.now(timezone.utc).date().isoformat()
        deliver('Lodestar GitHub job failed', '<b>GitHub automation needs attention</b>\n' + url, key, state)


if __name__ == '__main__':
    main()

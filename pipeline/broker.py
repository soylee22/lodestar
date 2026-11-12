"""Read-only Trading 212 access for private reports and cloud health checks."""
from datetime import datetime, timezone
import base64
import json
import time
import urllib.error
import urllib.request


def request(path):
    from pipeline.live_portfolio import load_t212_credentials
    key, secret, base = load_t212_credentials()
    if not key or not secret:
        raise RuntimeError('Trading 212 credentials are missing')
    auth = base64.b64encode(f'{key}:{secret}'.encode()).decode()
    for attempt in range(3):
        req = urllib.request.Request(base + '/api/v0/equity/' + path,
                                     headers={'Authorization': 'Basic ' + auth})
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(min(float(exc.headers.get('Retry-After', 3)), 20))
                continue
            raise RuntimeError(f'Trading 212 request failed with HTTP {exc.code}') from None
    raise RuntimeError('Trading 212 did not respond')


def positions():
    result = {}
    for row in request('positions'):
        instrument = row.get('instrument', {})
        wallet = row.get('walletImpact', {})
        if wallet.get('currency') != 'GBP':
            raise RuntimeError('Broker valuation requires a GBP account')
        result[instrument['ticker']] = {
            'quantity': float(row['quantity']),
            'value_gbp': float(wallet['currentValue']),
        }
    return result


def health():
    rows = positions()
    print(f'Trading 212 read authenticated; {len(rows)} open positions. Account values withheld from logs.')
    return {'authenticated': True, 'checked_at': datetime.now(timezone.utc).isoformat()}


if __name__ == '__main__':
    health()

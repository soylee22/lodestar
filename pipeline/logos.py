"""Cache optional US company logos. Missing images never block portfolio data."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parents[1]
REGISTRY = HERE / 'data' / 'company_logos.json'
ASSETS = HERE / 'assets' / 'company-logos'


def company_key(name):
    return re.sub(r'\s+CLASS [A-Z]\b', '', str(name), flags=re.I).strip().upper()


def load_registry(path):
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {symbol: row for symbol, row in raw.items()
            if isinstance(row, dict) and isinstance(row.get('companyKey'), str)
            and isinstance(row.get('path'), str)
            and row['path'].startswith('assets/company-logos/')}


def refresh(positions):
    from PIL import Image
    registry = load_registry(REGISTRY)
    unique = {p['symbol']: p['name'] for p in positions}
    ASSETS.mkdir(parents=True, exist_ok=True)

    def fetch(item):
        symbol, name = item
        if not re.fullmatch(r'[A-Z0-9.-]+', symbol):
            return symbol, None
        existing = registry.get(symbol)
        if existing and existing['companyKey'] == company_key(name) and (HERE / existing['path']).exists():
            return symbol, existing
        url = 'https://images.financialmodelingprep.com/symbol/' + symbol + '.png'
        try:
            request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urlopen(request, timeout=12) as response:
                payload = response.read()
            if not payload.startswith(b'\x89PNG\r\n\x1a\n'):
                return symbol, None
            with Image.open(io.BytesIO(payload)) as image:
                image.verify()
            path = ASSETS / (symbol + '.png')
            path.write_bytes(payload)
            return symbol, {'companyKey': company_key(name),
                            'path': str(path.relative_to(HERE)), 'source': url,
                            'sha256': hashlib.sha256(payload).hexdigest()}
        except Exception:
            return symbol, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        for symbol, result in pool.map(fetch, unique.items()):
            if result:
                registry[symbol] = result
    REGISTRY.write_text(json.dumps(registry, indent=2, sort_keys=True) + '\n')
    available = sum(symbol in registry and registry[symbol]['companyKey'] == company_key(name)
                    for symbol, name in unique.items())
    print(f'  logos: {available}/{len(unique)} company symbols cached')
    return registry


if __name__ == '__main__':
    source = json.loads((HERE / 'data' / 'holdings.json').read_text())
    positions = [row for key, leg in source['legs'].items()
                 if key == 'sector' or leg['slot'].startswith('USA_')
                 for row in leg.get('all_holdings') or leg.get('holdings') or []]
    refresh(positions)

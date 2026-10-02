"""Combine disclosed ETF positions without inflating partial coverage to 100%."""
from urllib.parse import urlsplit
import math
import re


def company_domain(website):
    try:
        parsed = urlsplit(str(website or ''))
        host = parsed.hostname or ''
        if parsed.scheme not in ('https', 'http') or '.' not in host:
            return None
        return host.removeprefix('www.')
    except ValueError:
        return None


def consolidate(legs):
    identities = {}
    for leg in legs:
        for row in leg['holdings']:
            if row.get('isin'):
                identities.setdefault(row['sym'], set()).add(row['isin'])
    aliases = {sym: next(iter(values)) for sym, values in identities.items() if len(values) == 1}
    classes = {}
    for leg in legs:
        for row in leg['holdings']:
            if re.search(r'\bCLASS [A-Z]\b', row['name'], re.I):
                base = re.sub(r'\s+CLASS [A-Z]\b', '', row['name'], flags=re.I).strip().upper()
                classes.setdefault(base, set()).add(row['sym'])
    merged_classes = {name for name, symbols in classes.items() if len(symbols) > 1}
    companies = {}
    for leg in legs:
        for row in leg['holdings']:
            weight = float(row['bw'])
            if not math.isfinite(weight) or weight < 0:
                raise ValueError('Invalid company weight')
            # ISIN and ticker resolve repeated positions. Explicit share classes
            # with matching issuer names are combined as company exposure.
            base = re.sub(r'\s+CLASS [A-Z]\b', '', row['name'], flags=re.I).strip().upper()
            key = 'issuer:' + base if base in merged_classes else row.get('isin') or aliases.get(row['sym']) or row['sym']
            if not key:
                raise ValueError('Company identity missing')
            company = companies.setdefault(key, {
                'sym': row['sym'], 'symbols': [], 'name': base if base in merged_classes else row['name'], 'sector': row['sector'],
                'domain': row.get('domain') or {'MU': 'micron.com', 'CSCO': 'cisco.com'}.get(row['sym']),
                'bw': 0.0, 'contributions': []})
            if row['sym'] not in company['symbols']:
                company['symbols'].append(row['sym'])
            company['bw'] += weight
            company['contributions'].append({
                'leg': leg['leg'], 'ticker': leg['ticker'],
                'fundWeight': row['w'], 'bookWeight': weight})
    rows = sorted(companies.values(), key=lambda row: (-row['bw'], row['sym']))
    covered = sum(row['bw'] for row in rows)
    if covered > 100.0001:
        raise ValueError('Disclosed holdings exceed the portfolio')
    for row in rows:
        row['bw'] = round(row['bw'], 6)
        row['sym'] = ' / '.join(row['symbols'])
        funds = {}
        for c in row['contributions']:
            key = (c['leg'], c['ticker'])
            total = funds.setdefault(key, {**c, 'fundWeight': 0, 'bookWeight': 0})
            total['fundWeight'] += c['fundWeight']
            total['bookWeight'] += c['bookWeight']
        row['contributions'] = list(funds.values())
        row['shared'] = len({c['leg'] for c in row['contributions']}) > 1
    return {'holdings': rows, 'coverage': round(covered, 6),
            'other': round(max(0, 100 - covered), 6),
            'sharedCount': sum(row['shared'] for row in rows),
            'basis': 'Target allocation weights. Top-ten disclosure only.'}

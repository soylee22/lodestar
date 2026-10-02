"""Read iShares' dated equity holdings, retaining source and disclosure date."""
import csv
import io
import urllib.request
from urllib.parse import urlencode
import pandas as pd

# Product identities verified against the issuer's ISIN listings.
PRODUCTS = {'IUVL.L': 285207, 'IUIT.L': 280510}


def parse_csv(text, expected_date=None):
    lines = text.lstrip('\ufeff').splitlines()
    start, snapshot = None, None
    for i, line in enumerate(lines):
        fields = next(csv.reader([line]))
        if fields and fields[0] in ('Ticker', 'Issuer Ticker') and 'Weight (%)' in fields:
            start = i
            break
        if fields and 'holdings as of' in fields[0].lower() and len(fields) > 1:
            date = pd.to_datetime(fields[1].replace('Sept', 'Sep'), errors='coerce')
            if pd.notna(date):
                snapshot = str(date.date())
    if start is None or snapshot is None:
        raise ValueError('Issuer CSV has no dated holdings table')
    if expected_date and snapshot != expected_date:
        raise ValueError('Issuer snapshot does not match requested date')
    rows = []
    for row in csv.DictReader(io.StringIO('\n'.join(lines[start:]))):
        if (row.get('Asset Class') or '').strip() != 'Equity':
            continue
        symbol = (row.get('Ticker') or row.get('Issuer Ticker') or '').strip()
        try:
            weight = float(row['Weight (%)'].replace(',', '')) / 100
        except (ValueError, KeyError, TypeError):
            continue
        if not symbol or not 0 <= weight <= 1:
            raise ValueError('Invalid issuer equity position')
        if weight:
            isin = (row.get('ISIN') or '').strip()
            rows.append({'symbol': symbol, 'name': row['Name'], 'weight': weight,
                         'isin': None if isin in ('', '-') else isin, 'sector': row.get('Sector')})
    if not rows or sum(row['weight'] for row in rows) > 1.000001:
        raise ValueError('Invalid issuer coverage')
    return {'holdings': sorted(rows, key=lambda row: -row['weight']), 'snapshot_date': snapshot}


def fetch(ticker, month):
    product = PRODUCTS.get(ticker)
    if not product:
        return None
    date = pd.Period(month, 'M').end_time.strftime('%Y-%m-%d')
    query = urlencode({'appType': 'PRODUCT_PAGE', 'appSubType': 'ISHARES',
                       'targetSite': 'ishares-uk', 'locale': 'en_GB',
                       'portfolioId': product, 'userType': 'individual',
                       'asOfDate': date.replace('-', ''), 'component': 'holdings'})
    # The issuer's current productPageUrlUtils constructs this document URL.
    url = ('https://www.ishares.com/varnish-api/uk-retail01-product-data/'
           'product-data/api/v1/get-fund-document?' + query)
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                 'Accept': 'text/csv',
                                                 'x-application-id': 'pp-ui-csr'})
    with urllib.request.urlopen(request, timeout=25) as response:
        result = parse_csv(response.read().decode('utf-8-sig'), expected_date=date)
    return {**result, 'source_url': url}

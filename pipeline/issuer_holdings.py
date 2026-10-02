"""Read iShares' dated equity holdings, retaining source and disclosure date."""
import csv
import io
import math
import urllib.request
from urllib.parse import urlencode
import pandas as pd
import exchange_calendars as xcals

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
    rows, disclosed_weights = [], []
    for row in csv.DictReader(io.StringIO('\n'.join(lines[start:]))):
        asset_class = (row.get('Asset Class') or '').strip()
        if not asset_class:
            continue
        symbol = (row.get('Ticker') or row.get('Issuer Ticker') or '').strip()
        try:
            weight = float(row['Weight (%)'].replace(',', '')) / 100
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError('Invalid issuer equity weight') from exc
        if not math.isfinite(weight):
            raise ValueError('Invalid issuer equity weight')
        disclosed_weights.append(weight)
        if asset_class != 'Equity':
            continue
        if not symbol or not 0 <= weight <= 1:
            raise ValueError('Invalid issuer equity position')
        if weight:
            isin = (row.get('ISIN') or '').strip()
            rows.append({'symbol': symbol, 'name': row['Name'], 'weight': weight,
                         'isin': None if isin in ('', '-') else isin, 'sector': row.get('Sector')})
    # Published weights are rounded to 0.01 percentage points. Equities may
    # exceed 100% when cash is negative. Validate the net table, not gross equity.
    tolerance = len(disclosed_weights) * .00005 + .000001
    net_weight = sum(disclosed_weights)
    if not rows or abs(net_weight - 1) > tolerance:
        raise ValueError('Invalid issuer coverage')
    return {'holdings': sorted(rows, key=lambda row: -row['weight']), 'snapshot_date': snapshot,
            'net_disclosed_weight': net_weight, 'disclosure_rounding_tolerance': tolerance}


def fetch(ticker, month):
    product = PRODUCTS.get(ticker)
    if not product:
        return None
    # Both verified products hold US equities. The issuer has no disclosure for
    # weekends or US market holidays, even when they are calendar month-end.
    period = pd.Period(month, 'M')
    calendar = xcals.get_calendar('XNYS', start=f'{period.year}-01-01',
                                 end=f'{period.year}-12-31')
    date = calendar.sessions_in_range(period.start_time.normalize(),
                                     period.end_time.normalize())[-1].strftime('%Y-%m-%d')
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

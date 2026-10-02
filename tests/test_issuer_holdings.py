import unittest
from unittest.mock import patch, MagicMock
from pipeline.issuer_holdings import fetch
from urllib.parse import urlparse, parse_qs
from pipeline.issuer_holdings import parse_csv

SAMPLE = '''Fund Holdings as of,"30/Sept/2026"
Ticker,Name,Sector,Asset Class,Weight (%),ISIN
MU,Micron,Information Technology,Equity,20.43,US5951121038
CSCO,Cisco,Information Technology,Equity,4.18,US17275R1023
USD,US Dollar,Cash and/or Derivatives,Cash,0.50,-
'''


class IssuerHoldingsTests(unittest.TestCase):
    def test_dated_equities_and_isin_retained_cash_stays_in_residual(self):
        result = parse_csv(SAMPLE, expected_date='2026-09-30')
        self.assertEqual(result['snapshot_date'], '2026-09-30')
        self.assertEqual([r['symbol'] for r in result['holdings']], ['MU', 'CSCO'])
        self.assertAlmostEqual(result['holdings'][0]['weight'], .2043)
        self.assertEqual(result['holdings'][0]['isin'], 'US5951121038')

    def test_fetch_uses_current_issuer_document_endpoint_and_dated_parameters(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = SAMPLE.encode()
        with patch('pipeline.issuer_holdings.urllib.request.urlopen', return_value=response) as get:
            result = fetch('IUVL.L', '2026-09')
        url = get.call_args.args[0].full_url
        query = parse_qs(urlparse(url).query)
        self.assertIn('/api/v1/get-fund-document', url)
        self.assertEqual(query['portfolioId'], ['285207'])
        self.assertEqual(query['asOfDate'], ['20260930'])
        self.assertEqual(query['component'], ['holdings'])
        self.assertEqual(result['snapshot_date'], '2026-09-30')

    def test_stale_or_undated_download_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'does not match'):
            parse_csv(SAMPLE, expected_date='2026-10-01')
        with self.assertRaisesRegex(ValueError, 'dated holdings'):
            parse_csv('\n'.join(SAMPLE.splitlines()[1:]))

    def test_html_response_and_invalid_total_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_csv('<html>Consent required</html>')
        with self.assertRaisesRegex(ValueError, 'coverage'):
            parse_csv(SAMPLE.replace('20.43', '99.0'))


if __name__ == '__main__':
    unittest.main()

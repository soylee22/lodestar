import unittest
from unittest.mock import patch, MagicMock
from pipeline.issuer_holdings import fetch
from urllib.parse import urlparse, parse_qs
from pipeline.issuer_holdings import parse_csv

SAMPLE = '''Fund Holdings as of,"30/Sept/2026"
Ticker,Name,Sector,Asset Class,Weight (%),ISIN
MU,Micron,Information Technology,Equity,20.43,US5951121038
CSCO,Cisco,Information Technology,Equity,4.18,US17275R1023
USD,US Dollar,Cash and/or Derivatives,Cash,75.39,-
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

    def test_month_end_uses_final_us_trading_session(self):
        for month, label, expected in [('2026-05', '29/May/2026', '20260529'),
                                       ('2024-03', '28/Mar/2024', '20240328')]:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = SAMPLE.replace('30/Sept/2026', label).encode()
            with self.subTest(month=month), patch('pipeline.issuer_holdings.urllib.request.urlopen', return_value=response) as get:
                fetch('IUVL.L', month)
            self.assertEqual(parse_qs(urlparse(get.call_args.args[0].full_url).query)['asOfDate'], [expected])

    def test_malformed_equity_weight_is_not_silently_omitted(self):
        for weight in ['-', 'NaN', 'Infinity', '-0.1']:
            with self.subTest(weight=weight), self.assertRaisesRegex(ValueError, 'Invalid issuer equity'):
                parse_csv(SAMPLE.replace('20.43', weight))

    def test_negative_cash_balances_equities_above_100_percent(self):
        result = parse_csv(SAMPLE.replace('20.43', '96.47').replace('75.39', '-0.65'))
        self.assertAlmostEqual(sum(r['weight'] for r in result['holdings']), 1.0065)
        self.assertAlmostEqual(result['net_disclosed_weight'], 1)

    def test_html_response_and_invalid_total_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_csv('<html>Consent required</html>')
        with self.assertRaisesRegex(ValueError, 'coverage'):
            parse_csv(SAMPLE.replace('20.43', '99.0'))


if __name__ == '__main__':
    unittest.main()

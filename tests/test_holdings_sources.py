import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
from pipeline.holdings import annotate_sources, fund_holdings, dividend_yield_percent


class HoldingsSourceTests(unittest.TestCase):
    def test_all_issuer_sources_share_the_actual_disclosure_date(self):
        out = {'fetched_at': '2026-10-02T12:00:00Z', 'legs': {
            key: {'all_holdings': [{'symbol': 'MU'}], 'snapshot_date': '2026-09-30'}
            for key in ('factor', 'sector')}}
        result = annotate_sources(out)
        self.assertEqual(result['source'], 'iShares dated equity holdings')
        self.assertEqual(result['snapshot_date'], '2026-09-30')
        self.assertEqual(result['legs']['factor']['fundamentals_source'], 'Yahoo Finance')
        self.assertEqual(result['legs']['factor']['fundamentals_fetched_at'], out['fetched_at'])

    def test_mixed_or_undated_sources_do_not_claim_a_common_issuer_date(self):
        out = {'legs': {'factor': {'all_holdings': [1], 'snapshot_date': '2026-09-30'},
                        'sector': {'holdings': [1]}}}
        result = annotate_sources(out)
        self.assertIsNone(result['snapshot_date'])
        self.assertIn('Mixed', result['source'])
        self.assertEqual(result['legs']['sector']['source'], 'Yahoo Finance top-ten holdings')

    def test_invalid_fallback_weights_are_rejected_instead_of_drawn(self):
        for weights in [[float('nan')], [-.1], [23.81], [.7, .7]]:
            ticker = MagicMock()
            ticker.funds_data.top_holdings = pd.DataFrame({'Name': ['Company'] * len(weights),
                'Holding Percent': weights}, index=[f'S{i}' for i in range(len(weights))])
            with self.subTest(weights=weights), patch('pipeline.holdings.yf.Ticker', return_value=ticker), patch('pipeline.holdings.time.sleep'):
                self.assertIsNone(fund_holdings('ETF.L'))

    def test_low_yields_are_not_inflated_by_a_basket_unit_guess(self):
        self.assertAlmostEqual(dividend_yield_percent({'dividendRate': .5, 'currentPrice': 1000, 'dividendYield': .05}), .05)
        self.assertAlmostEqual(dividend_yield_percent({'dividendRate': .5, 'regularMarketPrice': 1000, 'dividendYield': .0005}), .05)
        self.assertIsNone(dividend_yield_percent({'dividendYield': .05}))
        self.assertIsNone(dividend_yield_percent({'dividendRate': .5, 'currentPrice': 0}))


if __name__ == '__main__':
    unittest.main()

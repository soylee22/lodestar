import unittest
from pipeline.lookthrough import consolidate, company_domain


def leg(name, rows):
    return {'leg': name, 'ticker': 'ETF-' + name, 'holdings': [
        {'sym': sym, 'name': sym, 'sector': 'Technology', 'w': w, 'bw': w / 2}
        for sym, w in rows]}


class LookthroughTests(unittest.TestCase):
    def test_duplicate_micron_is_summed_once_at_portfolio_weights(self):
        c = consolidate([leg('Factor', [('MU', 20.43183), ('CSCO', 4.18179)]),
                         leg('Sector', [('MU', 4.38431), ('CSCO', 1.80719)])])
        self.assertEqual(len(c['holdings']), 2)
        self.assertAlmostEqual(c['holdings'][0]['bw'], 12.40807)
        self.assertEqual(c['sharedCount'], 2)
        self.assertEqual(len(c['holdings'][0]['contributions']), 2)
        self.assertAlmostEqual(c['coverage'] + c['other'], 100.)

    def test_partial_disclosure_is_not_normalised_to_100(self):
        c = consolidate([leg('Factor', [('MU', 8)]), leg('Sector', [('MU', 6)])])
        self.assertEqual(c['holdings'][0]['bw'], 7.)
        self.assertEqual(c['other'], 93.)

    def test_invalid_weight_refuses_to_draw_a_misleading_chart(self):
        for w in [-1, float('inf'), float('nan'), 250]:
            with self.subTest(w=w), self.assertRaises(ValueError):
                consolidate([leg('Factor', [('MU', w)])])

    def test_identity_uses_isin_when_tickers_differ(self):
        a, b = leg('Factor', [('AAA', 8)]), leg('Sector', [('BBB', 6)])
        a['holdings'][0]['isin'] = b['holdings'][0]['isin'] = 'US0000000001'
        c = consolidate([a, b])
        self.assertEqual(len(c['holdings']), 1)
        self.assertEqual(c['holdings'][0]['bw'], 7.)

    def test_mixed_issuer_and_top_ten_feeds_still_merge_the_same_ticker(self):
        a, b = leg('Factor', [('MU', 8)]), leg('Sector', [('MU', 6)])
        a['holdings'][0]['isin'] = 'US5951121038'
        c = consolidate([a, b])
        self.assertEqual(len(c['holdings']), 1)
        self.assertEqual(c['holdings'][0]['bw'], 7.)

    def test_explicit_share_classes_merge_into_one_company_and_one_fund_contribution(self):
        a = leg('Factor', [('FOXA', .2), ('FOX', .1)])
        a['holdings'][0]['name'] = 'FOX CLASS A'
        a['holdings'][1]['name'] = 'FOX CLASS B'
        c = consolidate([a])
        self.assertEqual(len(c['holdings']), 1)
        fox = c['holdings'][0]
        self.assertEqual(fox['name'], 'FOX')
        self.assertAlmostEqual(fox['bw'], .15)
        self.assertEqual(len(fox['contributions']), 1)
        self.assertAlmostEqual(fox['contributions'][0]['fundWeight'], .3)
        self.assertFalse(fox['shared'])

    def test_company_domain_ignores_non_web_and_invalid_urls(self):
        self.assertEqual(company_domain('https://www.micron.com/about'), 'micron.com')
        self.assertIsNone(company_domain('javascript:alert(1)'))
        self.assertIsNone(company_domain('https://[invalid'))


if __name__ == '__main__':
    unittest.main()

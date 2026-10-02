import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


class PageTests(unittest.TestCase):
    def compile_page(self, cash=False, holdings=None, company_logos=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            if holdings:
                (data / 'holdings.json').write_text(json.dumps(holdings))
            if company_logos:
                (data / 'company_logos.json').write_text(json.dumps(company_logos))
                for entry in company_logos.values():
                    path = root / entry['path']
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'checked separately as an image')
            for name in ['build_data.py', 'universe.py']:
                shutil.copy(ROOT / name, root / name)
            shutil.copytree(ROOT / 'pipeline', root / 'pipeline')
            months = pd.period_range('2024-01', '2026-02', freq='M').astype(str)
            values = np.sin(np.arange(len(months))) * 3 + 1
            monthly = pd.DataFrame({
                'Lodestar': values, 'FTSE All-World': values * .8,
                'S&P 500': values * .7, 'MSCI World': values * .6}, index=months)
            monthly.to_csv(data / 'final_tradable.csv')
            annual = monthly.groupby(monthly.index.str[:4]).agg(lambda s: ((1+s/100).prod()-1)*100)
            annual['vs All-World'] = annual['Lodestar'] - annual['FTSE All-World']
            annual.to_csv(data / 'final_tradable_annual.csv')
            book_months = pd.period_range('2024-01', '2026-03', freq='M').astype(str)
            book = pd.DataFrame({'factor': 'USA_VAL', 'sector': 'ENERGY', 'cash': False,
                'factor_source': 'ETF proxy', 'sector_source': 'ETF proxy'}, index=book_months)
            book.loc['2026-03', 'sector'] = 'INFOTECH'
            book.loc['2026-03', 'cash'] = cash
            book.to_csv(data / 'final_book.csv')
            result = subprocess.run([sys.executable, str(root / 'build_data.py'), str(data)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = (root / 'data.js').read_text()
            return json.loads(text[text.index('{'):].strip().rstrip(';'))

    def test_current_holding_is_independent_of_last_priced_month(self):
        page = self.compile_page()
        self.assertEqual(page['meta']['end'], '2026-02')
        self.assertEqual(page['current']['holdingMonth'], '2026-03')
        self.assertEqual(page['current']['legs'][1]['code'], 'INFOTECH')
        self.assertEqual(page['holdings'][-1]['sector'], 'INFOTECH')
        self.assertEqual(page['allocationCalendar'][-1], '2026-03')
        self.assertFalse(page['holdings'][-1]['priced'])
        self.assertIsNone(page['holdings'][-1]['ret'])
        self.assertEqual(page['calendar'][-1], '2026-02')
        self.assertEqual(page['current']['factorSource'], 'ETF proxy')

    def test_cash_page_compiles_with_no_equity_tiles(self):
        page = self.compile_page(cash=True)
        self.assertTrue(page['current']['cash'])
        self.assertEqual(page['current']['legs'], [])
        self.assertIsNone(page['book'])
        self.assertFalse(any(x['live'] for x in page['universe']['factors']))

    def test_full_issuer_exposure_does_not_use_top_ten_weights(self):
        raw = {'as_at': '2026-02', 'legs': {}}
        for key, slot, weights in [('factor', 'USA_VAL', (.20, .30)),
                                   ('sector', 'INFOTECH', (.10, .10))]:
            raw['legs'][key] = {'slot': slot, 'holdings': [
                {'symbol': 'MU', 'name': 'Micron', 'weight': .08}],
                'all_holdings': [
                    {'symbol': 'MU', 'name': 'Micron', 'weight': weights[0], 'isin': 'US5951121038'},
                    {'symbol': 'NEW', 'name': 'Another company', 'weight': weights[1], 'isin': 'US0000000001'}],
                'snapshot_date': '2026-02-27'}
        page = self.compile_page(holdings=raw)
        combined = page['book']['consolidated']
        self.assertTrue(combined['fullDisclosure'])
        self.assertEqual(combined['holdings'][0]['sym'], 'NEW')
        self.assertEqual(combined['holdings'][0]['bw'], 20.)
        self.assertEqual(combined['holdings'][1]['bw'], 15.)
        self.assertEqual(combined['other'], 65.)
        self.assertEqual(page['book']['legs'][0]['holdings'][0]['bw'], 4.)

    def test_same_fund_with_old_month_or_wrong_identity_is_not_shown(self):
        for fault in ['month', 'date', 'ticker']:
            raw = {'as_at': '2026-02', 'legs': {}}
            for key, slot, ticker in [('factor', 'USA_VAL', 'IUVL.L'), ('sector', 'INFOTECH', 'IUIT.L')]:
                raw['legs'][key] = {'slot': slot, 'ticker': ticker,
                    'snapshot_date': '2026-02-27',
                    'holdings': [{'symbol': 'MU', 'name': 'Micron', 'weight': .2}]}
            if fault == 'month':
                raw['as_at'] = '2026-01'
            elif fault == 'date':
                raw['legs']['factor']['snapshot_date'] = '2026-01-30'
            else:
                raw['legs']['factor']['ticker'] = 'WRONG.L'
            with self.subTest(fault=fault):
                self.assertIsNone(self.compile_page(holdings=raw)['book'])

    def test_logo_matches_the_company_identity_instead_of_only_its_ticker(self):
        raw = {'as_at': '2026-02', 'legs': {}}
        for key, slot in [('factor', 'USA_VAL'), ('sector', 'INFOTECH')]:
            raw['legs'][key] = {'slot': slot, 'holdings': [
                {'symbol': 'MU', 'name': 'Micron', 'weight': .2}]}
        for name, expected in [('MICRON', 'assets/company-logos/MU.png'), ('ANOTHER COMPANY', None)]:
            registry = {'MU': {'companyKey': name, 'path': 'assets/company-logos/MU.png'}}
            with self.subTest(name=name):
                page = self.compile_page(holdings=raw, company_logos=registry)
                self.assertEqual(page['book']['consolidated']['holdings'][0]['logo'], expected)


if __name__ == '__main__':
    unittest.main()

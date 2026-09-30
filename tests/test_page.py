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
    def compile_page(self, cash=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            data.mkdir()
            for name in ['build_data.py', 'universe.py']:
                shutil.copy(ROOT / name, root / name)
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
        self.assertEqual(page['holdings'][-1]['sector'], 'ENERGY')
        self.assertEqual(page['current']['factorSource'], 'ETF proxy')

    def test_cash_page_compiles_with_no_equity_tiles(self):
        page = self.compile_page(cash=True)
        self.assertTrue(page['current']['cash'])
        self.assertEqual(page['current']['legs'], [])
        self.assertIsNone(page['book'])
        self.assertFalse(any(x['live'] for x in page['universe']['factors']))


if __name__ == '__main__':
    unittest.main()

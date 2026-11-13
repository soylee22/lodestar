from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
import math
from unittest.mock import patch
import daily
import pandas as pd
from pipeline import live_portfolio


class DailyTests(unittest.TestCase):
    def test_daily_read_and_page_use_the_same_archived_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            (root/'data'/'last_alert.json').write_text(json.dumps({'month':'2026-08','picks':'USA_VAL+ENERGY'}))
            history={'2026-09':{'factor':'USA_VAL','sector':'INFOTECH','cash':False},
                     '2026-10':{'factor':'USA_MOM','sector':'ENERGY','cash':False}}
            (root/'data'/'signal_history.json').write_text(json.dumps(history))
            with patch.object(daily,'HERE',root),patch.object(daily,'date') as clock:
                clock.today.return_value=date(2026,10,1)
                self.assertEqual(daily.held_now(),('USA_VAL','INFOTECH','2026-09'))

    def test_live_portfolio_handles_unopened_lse_nan_when_fx_trades_24h(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = {
                'entry_date': '2026-11-12',
                'initial_capital_gbp': 1000.0,
                'holdings': [{'leg': 'Factor', 'code': 'USA_VAL', 'ticker': 'IUVL.L', 'name': 'USA Value', 'shares': 10.0, 'cost_gbp': 1000.0, 'fill_price_usd': 130.0}],
                'benchmarks': {'VWRP.L': {'name': 'FTSE All-World', 'entry_price_gbp': 100.0, 'initial_shares': 10.0}},
            }
            (root / 'live_portfolio.json').write_text(json.dumps(cfg))
            idx = pd.to_datetime(['2026-11-12', '2026-11-13'])
            close = pd.DataFrame({'IUVL.L': [132.65, float('nan')], 'VWRP.L': [101.0, float('nan')], 'GBPUSD=X': [1.3265, 1.3200]}, index=idx)
            with patch.object(live_portfolio, 'DATA_DIR', root), \
                 patch.object(live_portfolio.yf, 'download', return_value={'Close': close}):
                pf = live_portfolio.get_live_portfolio_data(broker=False)
            self.assertTrue(math.isfinite(pf['current_value_gbp']))
            self.assertAlmostEqual(pf['benchmarks'][0]['current_price_gbp'], 101.0)
            json.dumps(pf, allow_nan=False)


if __name__=='__main__':
    unittest.main()

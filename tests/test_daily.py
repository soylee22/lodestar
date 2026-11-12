from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import daily


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

    def test_held_now_returns_defensive_slot_from_history_or_alert(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            (root / 'data' / 'last_alert.json').write_text(json.dumps({'month': '2026-08', 'picks': 'DEF:DEF_IEF'}))
            with patch.object(daily, 'HERE', root), patch.object(daily, 'date') as clock:
                clock.today.return_value = date(2026, 9, 1)
                self.assertEqual(daily.held_now(), ('DEF_IEF', '', '2026-08'))
            history = {'2026-09': {'factor': 'USA_VAL', 'sector': 'INFOTECH', 'cash': True, 'defensive': 'DEF_BIL'}}
            (root / 'data' / 'signal_history.json').write_text(json.dumps(history))
            with patch.object(daily, 'HERE', root), patch.object(daily, 'date') as clock:
                clock.today.return_value = date(2026, 10, 1)
                self.assertEqual(daily.held_now(), ('DEF_BIL', '', '2026-09'))

    def test_build_email_html_renders_defensive_canary_alert_styling(self):
        html = daily.build_email_html(
            days_out=5,
            signal_date=date(2026, 12, 1),
            live_pf={'initial_capital_gbp': 5000.0, 'current_value_gbp': 5050.0, 'total_pnl_gbp': 50.0, 'total_pnl_pct': 1.0},
            crash_data={
                'cash_triggered': True,
                'defensive_ticker': 'IB01.L',
                'tip_score_pct': -2.05,
                'checks': [{'name': 'TIP 1-3-6-12 Canary', 'desc': 'USD composite', 'rule': '> 0.00%', 'reading_pct': -2.05, 'pass': False}],
            },
            f_pick='USA_VAL',
            s_pick='INFOTECH',
            fmom=daily.pd.Series(dtype=float),
            smom=daily.pd.Series(dtype=float),
            hf='DEF_BIL',
            hs='',
            hmonth='2026-10',
            change=[],
            would='<b>100% US Treasury 0-1yr (BIL)</b>',
        )
        self.assertIn('100% Defensive (IB01.L)', html)
        self.assertIn('Canary Triggered · IB01.L', html)
        self.assertIn('#dc2626', html)
        self.assertNotIn('All Systems Green', html)


if __name__=='__main__':
    unittest.main()

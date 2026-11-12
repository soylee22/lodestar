import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import daily
import run_monthly
from pipeline import broker


class CloudOperationTests(unittest.TestCase):
    def test_daily_failure_if_either_channel_fails(self):
        with patch.object(daily, 'telegram', return_value=True), patch.object(daily, 'email', return_value=False):
            self.assertEqual(daily.send('Test', 'Test'), 1)

    def test_modern_broker_currency_is_account_currency(self):
        payload = [{'instrument': {'ticker': 'TEST_US_EQ'}, 'quantity': 2,
                    'walletImpact': {'currency': 'GBP', 'currentValue': 30}}]
        with patch.object(broker, 'request', return_value=payload):
            self.assertEqual(broker.positions()['TEST_US_EQ']['value_gbp'], 30)

    def test_non_gbp_account_refuses_wrong_currency(self):
        payload = [{'instrument': {'ticker': 'TEST'}, 'quantity': 2,
                    'walletImpact': {'currency': 'EUR', 'currentValue': 30}}]
        with patch.object(broker, 'request', return_value=payload):
            with self.assertRaisesRegex(RuntimeError, 'GBP'):
                broker.positions()

    def test_monthly_alert_formats_defensive_entry_with_distinct_pick_key(self):
        fake_signal = {
            'signal_month': '2026-08',
            'holding_month': '2026-09',
            'performance_month': '2026-08',
            'expected_month': '2026-08',
            'stale': False,
            'source_failures': [],
            'factor_source': 'MSCI',
            'sector_source': 'S&P index',
            'factor': 'USA_VAL',
            'sector': 'INFOTECH',
            'factor_ticker': 'IUVL.L',
            'sector_ticker': 'IUIT.L',
            'factor_table': {'USA_VAL': 0.2},
            'sector_table': {'INFOTECH': 0.25},
            'cash': True,
            'defensive': 'DEF_BIL',
            'defensive_ticker': 'IB01.L',
            'defensive_name': 'US Treasury 0-1yr (BIL)',
            'canary': {'tip_score': -0.015, 'ief_score': -0.03, 'bil_score': 0.002},
            'previous': {'factor': 'USA_VAL', 'sector': 'INFOTECH', 'cash': False,
                         'defensive': None, 'defensive_ticker': None},
        }
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / 'last_alert.json'
            with patch.object(run_monthly, 'STATE', state), \
                 patch.object(run_monthly.build, 'main', return_value=fake_signal), \
                 patch.object(run_monthly.holdings, 'build'), \
                 patch.object(run_monthly.subprocess, 'run'), \
                 patch.object(run_monthly, 'announce', return_value=True) as ann:
                rc = run_monthly.main(build_only=False)
        self.assertEqual(rc, 0)
        subject, body = ann.call_args[0]
        self.assertIn('GO DEFENSIVE - IB01.L', subject)
        self.assertIn('TIP 1-3-6-12 sits at <b>-1.50%</b>', body)
        self.assertIn('100% <code>IB01.L</code>', body)

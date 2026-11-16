from pathlib import Path
import unittest
from unittest.mock import patch
import daily
from pipeline import broker

ROOT = Path(__file__).resolve().parents[1]


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

    def test_github_workflows_coordinate_without_local_machine(self):
        wf_dir = ROOT / '.github' / 'workflows'
        site = (wf_dir / 'site-refresh.yml').read_text(encoding='utf-8')
        daily_wf = (wf_dir / 'daily.yml').read_text(encoding='utf-8')
        monthly_wf = (wf_dir / 'monthly.yml').read_text(encoding='utf-8')

        for text in (site, daily_wf, monthly_wf):
            self.assertIn('group: lodestar-refresh', text)
            self.assertIn('cancel-in-progress: false', text)
            self.assertIn('schedule:', text)
            self.assertIn('workflow_dispatch:', text)

        self.assertIn('python run_monthly.py --build-only', site)
        self.assertNotIn('daily.py', site)
        self.assertIn('--once-today', daily_wf)
        self.assertIn('data/last_daily.json', daily_wf)
        self.assertIn('daily.already_sent_today()', daily_wf)

import unittest
from unittest.mock import patch
import daily
import cloud_alerts
from pipeline import broker


class CloudOperationTests(unittest.TestCase):
    def test_daily_failure_if_either_channel_fails(self):
        with patch.object(daily, 'telegram', return_value=True), patch.object(daily, 'email', return_value=False):
            self.assertEqual(daily.send('Test', 'Test'), 1)

    def test_delivery_does_not_repeat_a_successful_channel(self):
        state = {'deliveries': {'event': {'telegram': True}}}
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder, patch.object(cloud_alerts, 'STATE', Path(folder)/'state.json'):
            with patch.object(cloud_alerts, 'telegram') as tg, patch.object(cloud_alerts, 'email', return_value=True):
                cloud_alerts.deliver('Test', 'Test', 'event', state)
                tg.assert_not_called()
                self.assertTrue(state['deliveries']['event']['email'])

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

    def test_stale_pullback_scan_alerts(self):
        from datetime import datetime, timezone
        with patch.object(cloud_alerts, 'deliver') as send:
            cloud_alerts.scan_health({'generated': '2026-11-09T21:30:00Z', 'failed': []}, {},
                                     datetime(2026, 11, 12, 19, tzinfo=timezone.utc))
            send.assert_called_once()

    def test_weekend_gap_does_not_alert(self):
        from datetime import datetime, timezone
        with patch.object(cloud_alerts, 'deliver') as send:
            cloud_alerts.scan_health({'generated': '2026-11-14T08:00:00Z', 'failed': []}, {},
                                     datetime(2026, 11, 16, 10, tzinfo=timezone.utc))
            send.assert_not_called()

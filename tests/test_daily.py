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

    def test_lse_ticker_map_shares_universe_iefm(self):
        import universe
        from pipeline import build
        expected = {
            'USA_MOM': 'IUMF.L', 'USA_QUAL': 'IUQF.L', 'USA_VAL': 'IUVF.L', 'USA_SMALL': 'CUS1.L',
            'EUR_MOM': 'IEFM.L', 'EUR_QUAL': 'IEFQ.L', 'EUR_VAL': 'IEFV.L', 'EUR_SMALL': 'XXSC.L',
            'CONS_DISC': 'ICDU.L', 'CONS_STAP': 'ICSU.L', 'ENERGY': 'IESU.L', 'HEALTH': 'IHCU.L',
            'INDUST': 'IISU.L', 'INFOTECH': 'IITU.L', 'MATERIALS': 'IMSU.L',
        }
        self.assertEqual(daily.LSE, expected)
        self.assertEqual(daily.LSE, universe.LSE)
        self.assertEqual(build.LSE, universe.LSE)

    def test_live_portfolio_handles_gbp_pence_and_mixed_usd_holdings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = {
                'entry_date': '2026-11-12',
                'initial_capital_gbp': 2000.0,
                'holdings': [
                    {'leg': 'Factor', 'code': 'USA_VAL', 'ticker': 'IUVF.L', 'name': 'USA Value', 'shares': 10.0, 'cost_gbp': 1000.0, 'currency': 'GBp', 'fill_price_gbp': 100.0},
                    {'leg': 'Sector', 'code': 'INFOTECH', 'ticker': 'IUIT.L', 'name': 'Info Tech', 'shares': 10.0, 'cost_gbp': 1000.0, 'fill_price_usd': 130.0},
                ],
                'benchmarks': {},
            }
            (root / 'live_portfolio.json').write_text(json.dumps(cfg))
            idx = pd.to_datetime(['2026-11-13'])
            close = pd.DataFrame({'IUVF.L': [10500.0], 'IUIT.L': [132.65], 'GBPUSD=X': [1.3265]}, index=idx)
            with patch.object(live_portfolio, 'DATA_DIR', root), \
                 patch.object(live_portfolio.yf, 'download', return_value={'Close': close}):
                pf = live_portfolio.get_live_portfolio_data(broker=False)
            self.assertAlmostEqual(pf['holdings'][0]['current_value_gbp'], 1050.0)
            self.assertAlmostEqual(pf['holdings'][1]['current_value_gbp'], 1000.0)
            # Fallback to stored fill_price_gbp when Yahoo returns no quote for the GBp line.
            with patch.object(live_portfolio, 'DATA_DIR', root), \
                 patch.object(live_portfolio, '_last_valid_quotes', return_value={'GBPUSD=X': 1.3265}):
                pf_fb = live_portfolio.get_live_portfolio_data(broker=False)
            self.assertAlmostEqual(pf_fb['holdings'][0]['current_value_gbp'], 1000.0)

    def test_emerging_telegram_and_email_stay_in_parity_across_states(self):
        from pipeline import crash_protection, emerging
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            (root / 'data' / 'signal_history.json').write_text(json.dumps({
                '2026-10': {'factor': 'USA_VAL', 'sector': 'INFOTECH', 'cash': False}
            }))

            dates = pd.to_datetime(['2026-03-31', '2026-11-12'])
            f_px = pd.DataFrame({
                'USA_VAL': [100.0, 130.0],
                'USA_MOM': [100.0, 118.0],
            }, index=dates)
            s_px = pd.DataFrame({
                'HEALTH': [100.0, 129.0],
                'INFOTECH': [100.0, 126.0],
            }, index=dates)

            # 1) ROTATE state when Health Care leads Info Tech and cash gate is clear
            em_rotate = emerging.compute_emerging_data(
                today=date(2026, 11, 13),
                root=root,
                crash_data={'cash_triggered': False, 'status': 'INVESTED', 'status_text': 'All Systems Green · 100% Invested', 'status_tone': 'pass', 'checks': []},
                use_cache=False,
                factor_px=f_px,
                sector_px=s_px,
                sector_is_proxy=True,
            )
            self.assertEqual(em_rotate['asof'], '2026-11-12')
            self.assertEqual(em_rotate['action'], 'ROTATE')
            self.assertEqual(em_rotate['sector_lead']['leader_code'], 'HEALTH')
            self.assertIn('beats Health Care', em_rotate['sector_lead']['margin_text'])

            pf = {
                'initial_capital_gbp': 1000.0,
                'current_value_gbp': 1025.0,
                'total_pnl_gbp': 25.0,
                'total_pnl_pct': 2.5,
                'holdings': [],
                'benchmarks': [],
            }
            crash_ok = {
                'asof': '2026-11-12',
                'latest_price': 6700.0,
                'cash_triggered': False,
                'status': 'INVESTED',
                'status_text': 'All Systems Green · 100% Invested',
                'status_tone': 'pass',
                'm19_pct': 20.0,
                'checks': [{'name': '19-Month Crash Floor', 'desc': 'vs -25%', 'rule': 'Lodestar Circuit Breaker', 'reading_pct': 20.0, 'pass': True}],
            }
            tg_rotate = daily.build_telegram_message(em_rotate, pf, crash_ok)
            html_rotate = daily.build_email_html(em_rotate, pf, crash_ok)
            self.assertIn('WOULD TRADE', tg_rotate)
            self.assertIn('ROTATE · REBALANCE PROJECTED', html_rotate)
            self.assertIn('Health Care', tg_rotate)
            self.assertIn('Health Care', html_rotate)
            self.assertIn('2026-11-12', tg_rotate)
            self.assertIn('2026-11-12', html_rotate)

            # 2) CASH state when 19-month crash floor breaches: must not say All Systems Green
            spx_dates = pd.date_range('2025-03-31', '2026-11-12', freq='ME').union(pd.to_datetime(['2026-11-12']))
            spx_vals = [5000.0] * len(spx_dates)
            spx_vals[-1] = 3500.0  # -30% vs 19m earlier
            spx_vals[-2] = 4000.0
            spx_series = pd.Series(spx_vals, index=spx_dates)
            crash_cash = crash_protection.get_crash_protection_data(today=date(2026, 11, 13), spx_daily=spx_series)
            self.assertTrue(crash_cash['cash_triggered'])
            self.assertEqual(crash_cash['status'], 'CASH')

            em_cash = emerging.compute_emerging_data(
                today=date(2026, 11, 13),
                root=root,
                crash_data=crash_cash,
                use_cache=False,
                factor_px=f_px,
                sector_px=s_px,
                sector_is_proxy=True,
            )
            self.assertEqual(em_cash['action'], 'CASH')
            tg_cash = daily.build_telegram_message(em_cash, pf, crash_cash)
            html_cash = daily.build_email_html(em_cash, pf, crash_cash)
            self.assertIn('cash rule would fire', tg_cash)
            self.assertIn('CRASH GATE · MOVE TO CASH', html_cash)
            self.assertNotIn('All Systems Green', html_cash)
            self.assertNotIn('All Systems Green', tg_cash)

    def test_snapshot_mode_uses_built_site_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            em = {
                'asof': '2026-11-13',
                'computed_at': '2026-11-13 15:30 UTC',
                'month': '2026-11',
                'holding_month': '2026-10',
                'held_factor': 'USA_VAL',
                'held_sector': 'INFOTECH',
                'held_cash': False,
                'days_out': 18,
                'next_action_date': '1 Dec 2026',
                'days_run': '13 of 30 days',
                'cash_triggered': False,
                'action': 'ROTATE',
                'action_text': 'ROTATE · REBALANCE PROJECTED',
                'action_summary': 'Sector: Information Technology to Health Care if month closed on latest prices.',
                'changes': [{'leg': 'sector', 'from_code': 'INFOTECH', 'from_name': 'Information Technology', 'to_code': 'HEALTH', 'to_name': 'Health Care', 'to_ticker': 'IHCU.L'}],
                'current_holdings': [{'leg': 'Factor', 'code': 'USA_VAL', 'name': 'USA Value', 'ticker': 'IUVL.L'}, {'leg': 'Sector', 'code': 'INFOTECH', 'name': 'Information Technology', 'ticker': 'IUIT.L'}],
                'projected_holdings': [{'leg': 'Factor', 'code': 'USA_VAL', 'name': 'USA Value', 'ticker': 'IUVL.L', 'weight_pct': 50.0, 'status': 'KEEP'}, {'leg': 'Sector', 'code': 'HEALTH', 'name': 'Health Care', 'ticker': 'IHCU.L', 'weight_pct': 50.0, 'status': 'NEW BUY'}],
                'factor_lead': {'margin_text': 'USA Value holds unless it gives back 5.0% against USA Momentum this month.'},
                'sector_lead': {'margin_text': 'Information Technology keeps the slot only if it beats Health Care by 1.0% over the rest of the month.'},
                'factors': [{'rank': 1, 'code': 'USA_VAL', 'name': 'USA Value', 'ticker': 'IUVL.L', 'momentum_pct': 30.0, 'lead_margin': '+10.0pp', 'is_held': True}],
                'sectors': [{'rank': 1, 'code': 'HEALTH', 'name': 'Health Care', 'ticker': 'IHCU.L', 'momentum_pct': 29.0, 'lead_margin': '+2.0pp', 'is_held': False}],
                'stale_cache': False,
            }
            cp = {
                'asof': '2026-11-13',
                'latest_price': 6730.0,
                'cash_triggered': False,
                'status': 'INVESTED',
                'status_text': 'All Systems Green · 100% Invested',
                'status_tone': 'pass',
                'm19_pct': 19.5,
                'checks': [{'name': '19-Month Crash Floor', 'desc': 'vs -25%', 'rule': 'Lodestar Circuit Breaker', 'reading_pct': 19.5, 'pass': True}],
            }
            (root / 'data' / 'emerging.json').write_text(json.dumps(em))
            (root / 'data' / 'crash_protection.json').write_text(json.dumps(cp))
            pf = {'initial_capital_gbp': 1000.0, 'current_value_gbp': 1010.0, 'total_pnl_gbp': 10.0, 'total_pnl_pct': 1.0, 'holdings': [], 'benchmarks': []}
            captured = {}
            def fake_send(subject, tg_body, html_body=None):
                captured['subject'] = subject
                captured['tg'] = tg_body
                captured['html'] = html_body
                return 0
            with patch.object(daily, 'HERE', root), \
                 patch.object(daily, 'get_live_portfolio_data', return_value=pf), \
                 patch.object(daily, 'send', side_effect=fake_send):
                rc = daily.main(['--snapshot'])
            self.assertEqual(rc, 0)
            self.assertIn('WOULD TRADE', captured['subject'])
            self.assertIn('15:30 UTC', captured['tg'])
            self.assertIn('15:30 UTC', captured['html'])
            self.assertIn('Health Care', captured['html'])

    def test_emerging_builds_normalised_8m_lookback_charts(self):
        from pipeline import emerging
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            (root / 'data' / 'signal_history.json').write_text(json.dumps({
                '2026-10': {'factor': 'USA_VAL', 'sector': 'INFOTECH', 'cash': False}
            }))
            dates = pd.to_datetime(['2026-03-31', '2026-07-15', '2026-11-12'])
            f_px = pd.DataFrame({
                'USA_VAL': [100.0, 110.0, 125.0],
                'USA_QUAL': [200.0, 190.0, 210.0],
            }, index=dates)
            s_px = pd.DataFrame({
                'INFOTECH': [50.0, 55.0, 60.0],
                'HEALTH': [80.0, 76.0, 72.0],
            }, index=dates)

            em = emerging.compute_emerging_data(
                today=date(2026, 11, 13),
                root=root,
                crash_data={'cash_triggered': False, 'status': 'INVESTED', 'status_text': 'All Systems Green', 'status_tone': 'pass', 'checks': []},
                use_cache=False,
                factor_px=f_px,
                sector_px=s_px,
                sector_is_proxy=True,
            )
            f_chart = em['factor_chart']
            s_chart = em['sector_chart']
            self.assertEqual(f_chart['base_month'], '2026-03')
            self.assertEqual(f_chart['base_date'], '2026-03-31')
            self.assertEqual(f_chart['dates'], ['2026-03-31', '2026-07-15', '2026-11-12'])
            self.assertEqual([s['code'] for s in f_chart['series']], ['USA_VAL', 'USA_QUAL'])
            self.assertEqual(f_chart['series'][0]['values'], [0.0, 10.0, 25.0])
            self.assertEqual(f_chart['series'][1]['values'], [0.0, -5.0, 5.0])
            self.assertEqual(f_chart['series'][0]['latest_pct'], em['factors'][0]['momentum_pct'])

            self.assertEqual([s['code'] for s in s_chart['series']], ['INFOTECH', 'HEALTH'])
            self.assertEqual(s_chart['series'][0]['values'], [0.0, 10.0, 20.0])
            self.assertEqual(s_chart['series'][1]['values'], [0.0, -5.0, -10.0])
            self.assertEqual(s_chart['series'][1]['latest_pct'], em['sectors'][1]['momentum_pct'])


if __name__=='__main__':
    unittest.main()

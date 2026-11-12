import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from pipeline import build, crash_protection, emerging, holdings


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.months = pd.period_range('2023-01', '2026-09', freq='M')
        levels = np.arange(len(self.months), dtype=float) + 100
        self.raw = pd.DataFrame({s: levels.copy() for s in build.FACTOR_SLOTS + build.SECTOR_SLOTS}, index=self.months)
        self.canary = pd.DataFrame({
            'TIP': levels.copy(),
            'IEF': levels.copy(),
            'BIL': np.linspace(100.0, 104.0, len(self.months)),
        }, index=self.months)
        self.expected = pd.Period('2026-09', 'M')
        self.proxy = self.raw.copy()
        self.proxy['USA_VAL'] = levels ** 2
        self.proxy['INFOTECH'] = levels ** 2

    def test_fallback_signals_enter_the_book_and_keep_the_source(self):
        self.raw.loc['2026-07':, :] = np.nan
        history = build.resolve_history(self.raw, self.canary, {}, self.expected,
            {'factor': self.proxy[build.FACTOR_SLOTS], 'sector': self.proxy[build.SECTOR_SLOTS]})
        book, *_ = build.compute_book(self.raw, self.canary, history)
        self.assertEqual(book.index[-1], pd.Period('2026-10', 'M'))
        for month in ['2026-08', '2026-09', '2026-10']:
            self.assertEqual(book.loc[month, 'factor'], 'USA_VAL')
            self.assertEqual(book.loc[month, 'sector'], 'INFOTECH')
            self.assertEqual(book.loc[month, 'factor_source'], 'ETF proxy')

    def test_published_signal_survives_index_backfill(self):
        saved = {'2026-08': {'factor': 'USA_VAL', 'sector': 'ENERGY', 'cash': False,
            'defensive': None, 'defensive_ticker': None,
            'factor_source': 'ETF proxy', 'sector_source': 'ETF proxy'}}
        book, *_ = build.compute_book(self.raw, self.canary, saved)
        self.assertEqual(book.loc['2026-09', 'sector'], 'ENERGY')
        self.assertEqual(book.loc['2026-09', 'factor'], 'USA_VAL')

    def test_missing_calendar_month_does_not_stretch_the_lookback(self):
        px = self.proxy.drop(pd.Period('2026-02', 'M'))
        mom = build._momentum(px)
        self.assertNotIn(pd.Period('2026-02', 'M'), mom.index)
        actual = mom.loc['2026-09', 'USA_VAL']
        expected = self.proxy.loc['2026-09', 'USA_VAL'] / self.proxy.loc['2026-01', 'USA_VAL'] - 1
        self.assertEqual(actual, expected)

    def test_incomplete_proxy_basket_refuses_to_rank(self):
        self.raw.loc['2026-09', :] = np.nan
        self.proxy.loc['2026-09', 'USA_MOM'] = np.nan
        with self.assertRaisesRegex(RuntimeError, 'no complete factor ranking'):
            build.resolve_history(self.raw, self.canary, {}, self.expected,
                {'factor': self.proxy[build.FACTOR_SLOTS], 'sector': self.proxy[build.SECTOR_SLOTS]})

    def test_missing_cash_window_refuses_to_publish(self):
        self.canary.loc['2026-09', 'TIP'] = np.nan
        with self.assertRaisesRegex(RuntimeError, 'cash-rule price window unavailable'):
            build.resolve_history(self.raw, self.canary, {}, self.expected)

    def test_missing_final_close_is_not_a_completed_month(self):
        prices = pd.Series([10., np.nan], index=pd.to_datetime(['2026-09-29', '2026-09-30']))
        self.assertTrue(build.completed_months(prices, 'IUVL.L', '2026-10-01').empty)
        prices.iloc[-1] = 11.
        self.assertEqual(build.completed_months(prices, 'IUVL.L', '2026-10-01').loc['2026-09'], 11.)

    def test_short_window_repairs_a_missing_final_close_with_same_basis(self):
        from unittest.mock import MagicMock
        expected = pd.Timestamp.today().to_period('M') - 1
        cal = build.exchange_calendar('XLON')
        last = cal.sessions_in_range(expected.start_time.normalize(), expected.end_time.normalize())[-1]
        long = pd.DataFrame({'Close': [10., np.nan]}, index=[last - pd.Timedelta(days=1), last])
        short = pd.DataFrame({'Close': [12.]}, index=[last])
        ticker = MagicMock()
        ticker.history.side_effect = [long, short]
        with patch.object(build.yf, 'Ticker', return_value=ticker):
            result = build.yahoo_monthly('IUVL.L', adjusted=True)
        self.assertEqual(result.loc[expected], 12.)
        self.assertEqual(ticker.history.call_count, 2)
        self.assertTrue(all(call.kwargs['auto_adjust'] for call in ticker.history.call_args_list))

    def test_short_window_with_no_final_close_keeps_month_unpriced(self):
        from unittest.mock import MagicMock
        expected = pd.Timestamp.today().to_period('M') - 1
        cal = build.exchange_calendar('XLON')
        last = cal.sessions_in_range(expected.start_time.normalize(), expected.end_time.normalize())[-1]
        prices = pd.DataFrame({'Close': [np.nan]}, index=[last])
        ticker = MagicMock()
        ticker.history.return_value = prices
        with patch.object(build.yf, 'Ticker', return_value=ticker):
            result = build.yahoo_monthly('IUVL.L', adjusted=True)
        self.assertNotIn(expected, result.index)

    def test_month_end_exchange_holiday_uses_last_session(self):
        prices = pd.Series([10.], index=pd.to_datetime(['2024-03-28']))
        self.assertEqual(build.completed_months(prices, 'IUVL.L', '2024-04-01').loc['2024-03'], 10.)

    def test_partial_current_month_is_excluded(self):
        prices = pd.Series([10.], index=pd.to_datetime(['2026-09-30']))
        self.assertTrue(build.completed_months(prices, 'VLUE', '2026-09-30').empty)

    def test_missing_fund_price_is_not_forward_filled_into_a_return(self):
        idx = pd.period_range('2026-06', '2026-08', freq='M')
        funds = pd.DataFrame({'USA_VAL': [10, np.nan, 12], 'ENERGY': [10, 11, 12]}, index=idx)
        book = pd.DataFrame({'factor': 'USA_VAL', 'sector': 'ENERGY', 'cash': False}, index=idx)
        track = build.build_track(book, funds, pd.DataFrame({'world': [1, 1, 1]}, index=idx))
        self.assertTrue(track.empty)

    def test_return_archive_cannot_jump_over_a_missing_month(self):
        recorded = pd.DataFrame({'Lodestar': [1.]}, index=pd.PeriodIndex(['2026-07'], freq='M'))
        fresh = pd.DataFrame({'Lodestar': [9., 2., 3.]}, index=pd.PeriodIndex(['2026-07', '2026-08', '2026-10'], freq='M'))
        result = build.append_track(recorded, fresh)
        self.assertEqual(list(result.index.astype(str)), ['2026-07', '2026-08'])
        self.assertEqual(result.loc['2026-07', 'Lodestar'], 1.)

    def test_failed_unused_fund_does_not_remove_confirmed_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            cached = pd.DataFrame({'USA_VAL': [12.]}, index=['2026-08'])
            cached.to_csv(Path(folder) / 'fund_prices_gbp.csv')
            fx = pd.Series([1.], index=pd.PeriodIndex(['2026-08'], freq='M'))
            def fetch(sym, adjusted, start):
                if sym.endswith('=X'):
                    return fx
                raise RuntimeError('source unavailable')
            with patch.object(build, 'DATA', Path(folder)), patch.object(build, 'yahoo_monthly', side_effect=fetch):
                result, _, _ = build.gbp_panel({'USA_VAL': 'IUVL.L'})
            self.assertEqual(result.loc['2026-08', 'USA_VAL'], 12.)

    def test_tip_canary_zero_triggers_defensive_and_selects_stronger_treasury(self):
        months = pd.period_range('2024-01', '2026-09', freq='M')
        flat_tip = pd.Series(np.full(len(months), 100.0), index=months)
        falling_ief = pd.Series(np.linspace(110.0, 95.0, len(months)), index=months)
        rising_bil = pd.Series(np.linspace(100.0, 108.0, len(months)), index=months)
        cstate_bil = crash_protection.evaluate_canary_history(flat_tip, falling_ief, rising_bil)
        self.assertEqual(cstate_bil.loc['2026-09', 'tip_score'], 0.0)
        self.assertTrue(bool(cstate_bil.loc['2026-09', 'cash']))
        self.assertEqual(cstate_bil.loc['2026-09', 'defensive'], 'DEF_BIL')
        self.assertEqual(cstate_bil.loc['2026-09', 'defensive_ticker'], 'IB01.L')

        rising_ief = pd.Series(np.linspace(100.0, 120.0, len(months)), index=months)
        cstate_ief = crash_protection.evaluate_canary_history(flat_tip, rising_ief, rising_bil)
        self.assertEqual(cstate_ief.loc['2026-09', 'defensive'], 'DEF_IEF')
        self.assertEqual(cstate_ief.loc['2026-09', 'defensive_ticker'], 'CBU0.L')

    def test_splice_pre_listing_bil_joins_without_price_jump(self):
        ib01 = pd.Series([80.0, 80.4], index=pd.PeriodIndex(['2019-02', '2019-03'], freq='M'))
        bil = pd.Series([70.0, 70.7, 71.4, 72.1], index=pd.PeriodIndex(['2018-12', '2019-01', '2019-02', '2019-03'], freq='M'))
        spliced, proxy_months = build.splice_pre_listing_bil(ib01, bil)
        self.assertEqual(proxy_months, ['2018-12', '2019-01'])
        self.assertAlmostEqual(spliced.loc['2019-02'], 80.0)
        self.assertAlmostEqual(spliced.loc['2019-03'], 80.4)
        self.assertAlmostEqual(spliced.loc['2019-02'] / spliced.loc['2019-01'] - 1, bil.loc['2019-02'] / bil.loc['2019-01'] - 1)

    def test_build_track_applies_defensive_return_and_resets_equity_split(self):
        idx = pd.period_range('2026-07', '2026-09', freq='M')
        funds = pd.DataFrame({
            'USA_VAL': [100.0, 110.0, 121.0],
            'ENERGY': [100.0, 100.0, 100.0],
            'DEF_BIL': [100.0, 100.5, 101.0],
        }, index=idx)
        book = pd.DataFrame({
            'factor': ['USA_VAL', 'USA_VAL', 'USA_VAL'],
            'sector': ['ENERGY', 'ENERGY', 'ENERGY'],
            'cash': [False, True, False],
            'defensive': [None, 'DEF_BIL', None],
        }, index=idx)
        bench = pd.DataFrame({'world': [0.0, 0.0, 0.0]}, index=idx)
        track = build.build_track(book, funds, bench)
        self.assertAlmostEqual(track.loc['2026-08', 'Lodestar'], 0.5)
        self.assertAlmostEqual(track.loc['2026-09', 'Lodestar'], 5.0)

    def test_holdings_build_skips_equity_lookthrough_when_defensive(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(holdings, 'DATA', Path(folder)), \
                 patch.object(holdings.issuer_holdings, 'fetch') as fetch_mock:
                out = holdings.build({
                    'signal_month': '2026-08',
                    'cash': True,
                    'defensive': 'DEF_BIL',
                    'defensive_ticker': 'IB01.L',
                    'defensive_name': 'US Treasury 0-1yr (BIL)',
                })
                saved = Path(folder) / 'holdings.json'
                self.assertTrue(saved.exists())
            fetch_mock.assert_not_called()
            self.assertTrue(out['cash'])
            self.assertEqual(out['defensive_ticker'], 'IB01.L')
            self.assertEqual(out['legs'], {})

    def test_emerging_projects_defensive_allocation_when_tip_triggered(self):
        now = pd.Timestamp.today().to_period('M')
        dates = pd.bdate_range((now - 9).start_time, pd.Timestamp.today())
        def fake_download(syms, **kwargs):
            data = {s: np.linspace(100.0, 110.0 + idx, len(dates)) for idx, s in enumerate(syms)}
            return {'Close': pd.DataFrame(data, index=dates)}
        fake_crash = {
            'asof': '2026-11-12',
            'cash_triggered': True,
            'tip_score_pct': -2.05,
            'ief_score_pct': -3.10,
            'bil_score_pct': 0.28,
            'defensive_slot': 'DEF_BIL',
            'defensive_ticker': 'IB01.L',
            'defensive_name': 'US Treasury 0-1yr (BIL)',
            'defensive_isin': 'IE00BGSF1X88',
        }
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder) / 'data'
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / 'signal_history.json').write_text(
                '{"2026-10": {"factor": "USA_VAL", "sector": "INFOTECH", "cash": false}}'
            )
            with patch.object(emerging, 'HERE', Path(folder)), \
                 patch.object(emerging.yf, 'download', side_effect=fake_download), \
                 patch.object(emerging, 'get_crash_protection_data', return_value=fake_crash):
                out = emerging.compute_emerging_data()
        self.assertEqual(out['action'], 'DEFENSIVE')
        self.assertTrue(out['canary']['triggered'])
        self.assertEqual(out['projected_holdings'][0]['ticker'], 'IB01.L')
        self.assertEqual(out['projected_holdings'][0]['weight_pct'], 100.0)


if __name__ == '__main__':
    unittest.main()

import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from pipeline import build


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.months = pd.period_range('2023-01', '2026-09', freq='M')
        levels = np.arange(len(self.months), dtype=float) + 100
        self.raw = pd.DataFrame({s: levels.copy() for s in build.FACTOR_SLOTS + build.SECTOR_SLOTS}, index=self.months)
        self.spx = pd.Series(levels, index=self.months)
        self.expected = pd.Period('2026-09', 'M')
        self.proxy = self.raw.copy()
        self.proxy['USA_VAL'] = levels ** 2
        self.proxy['INFOTECH'] = levels ** 2

    def test_fallback_signals_enter_the_book_and_keep_the_source(self):
        self.raw.loc['2026-07':, :] = np.nan
        history = build.resolve_history(self.raw, self.spx, {}, self.expected,
            {'factor': self.proxy[build.FACTOR_SLOTS], 'sector': self.proxy[build.SECTOR_SLOTS]})
        book, *_ = build.compute_book(self.raw, self.spx, history)
        self.assertEqual(book.index[-1], pd.Period('2026-10', 'M'))
        for month in ['2026-08', '2026-09', '2026-10']:
            self.assertEqual(book.loc[month, 'factor'], 'USA_VAL')
            self.assertEqual(book.loc[month, 'sector'], 'INFOTECH')
            self.assertEqual(book.loc[month, 'factor_source'], 'ETF proxy')

    def test_published_signal_survives_index_backfill(self):
        saved = {'2026-08': {'factor': 'USA_VAL', 'sector': 'ENERGY', 'cash': False,
            'factor_source': 'ETF proxy', 'sector_source': 'ETF proxy'}}
        book, *_ = build.compute_book(self.raw, self.spx, saved)
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
            build.resolve_history(self.raw, self.spx, {}, self.expected,
                {'factor': self.proxy[build.FACTOR_SLOTS], 'sector': self.proxy[build.SECTOR_SLOTS]})

    def test_missing_cash_window_refuses_to_publish(self):
        self.spx.loc['2026-09'] = np.nan
        with self.assertRaisesRegex(RuntimeError, 'cash-rule price window unavailable'):
            build.resolve_history(self.raw, self.spx, {}, self.expected)

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
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            cached = pd.DataFrame({'USA_VAL': [12.]}, index=['2026-08'])
            cached.to_csv(Path(folder) / 'fund_prices_gbp.csv')
            fx = pd.Series([1.], index=pd.PeriodIndex(['2026-08'], freq='M'))
            def fetch(sym, adjusted, start):
                if sym.endswith('=X'):
                    return fx
                raise RuntimeError('source unavailable')
            with patch.object(build, 'DATA', Path(folder)), patch.object(build, 'yahoo_monthly', side_effect=fetch):
                result, _ = build.gbp_panel({'USA_VAL': 'IUVL.L'})
            self.assertEqual(result.loc['2026-08', 'USA_VAL'], 12.)

    def test_msci_uses_today_end_date_instead_of_2099(self):
        import io
        import json
        from unittest.mock import MagicMock
        payload = json.dumps({'indexes': {'INDEX_LEVELS': [
            {'calc_date': 19981231, 'level_eod': 100.0},
            {'calc_date': 20261030, 'level_eod': 500.0},
        ]}}).encode('utf-8')
        seen = {}
        def fake_open(req, timeout=25):
            seen['url'] = req.full_url
            resp = MagicMock()
            resp.read.return_value = payload
            return resp
        with patch.object(build.urllib.request, 'urlopen', side_effect=fake_open):
            res = build.msci('704892', 'USD')
        today_str = pd.Timestamp.today().strftime('%Y%m%d')
        self.assertIn(f'end_date={today_str}', seen['url'])
        self.assertNotIn('end_date=2099', seen['url'])
        self.assertEqual(str(res.index[0]), '1998-12')
        self.assertEqual(str(res.index[-1]), '2026-10')

    def test_merge_long_track_preserves_continuity_and_appends_new_months(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'final_index_tr.csv'
            pd.DataFrame(
                {'Lodestar (Index TR, GBP)': [1.5, 2.5]},
                index=['2001-01', '2001-02'],
            ).to_csv(path)
            fresh = pd.DataFrame(
                {'Lodestar': [9.0, 9.0, 3.0, 4.0]},
                index=pd.PeriodIndex(['2001-01', '2001-02', '2001-03', '2001-05'], freq='M'),
            )
            merged = build._merge_long_track(path, fresh, 'Lodestar (Index TR, GBP)')
            self.assertEqual(list(merged.index.astype(str)), ['2001-01', '2001-02', '2001-03'])
            self.assertEqual(merged.loc['2001-02', 'Lodestar (Index TR, GBP)'], 2.5)
            self.assertEqual(merged.loc['2001-03', 'Lodestar (Index TR, GBP)'], 3.0)

    def test_ftse_uses_london_calendar_on_uk_bank_holiday_month_end(self):
        # 2026-08-31 was a UK Summer Bank Holiday: XLON closed on 2026-08-28, XNYS traded on 2026-08-31.
        prices = pd.Series([8300.0], index=pd.to_datetime(['2026-08-28']))
        ftse_months = build.completed_months(prices, '^FTSE', '2026-09-02')
        spx_months = build.completed_months(prices, '^GSPC', '2026-09-02')
        self.assertEqual(list(ftse_months.index.astype(str)), ['2026-08'])
        self.assertTrue(spx_months.empty)

    def test_append_track_backfills_new_column_without_altering_existing(self):
        recorded = pd.DataFrame(
            {'Lodestar': [1.0, 2.0]},
            index=pd.PeriodIndex(['2026-07', '2026-08'], freq='M'),
        )
        fresh = pd.DataFrame(
            {'Lodestar': [9.0, 9.0, 3.0], 'FTSE 100': [0.5, 0.6, 0.7]},
            index=pd.PeriodIndex(['2026-07', '2026-08', '2026-09'], freq='M'),
        )
        result = build.append_track(recorded, fresh)
        self.assertEqual(list(result.index.astype(str)), ['2026-07', '2026-08', '2026-09'])
        self.assertEqual(result.loc['2026-07', 'Lodestar'], 1.0)
        self.assertEqual(result.loc['2026-08', 'FTSE 100'], 0.6)
        self.assertEqual(result.loc['2026-09', 'FTSE 100'], 0.7)


if __name__ == '__main__':
    unittest.main()

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


if __name__ == '__main__':
    unittest.main()

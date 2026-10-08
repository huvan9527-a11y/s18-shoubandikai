import unittest
import pandas as pd
import numpy as np
from src import signals


class SignalTests(unittest.TestCase):
    def history(self):
        dates = pd.bdate_range('2026-06-01', periods=61)
        # Yesterday's first limit-up follows a price below the historical median.
        close = [12.] * 59 + [10., 11.]
        return pd.DataFrame(dict(time=dates, code='000001.XSHE', close=close,
                                 open=[11.] * 59 + [10., 10.5],
                                 high_limit=[13.2] * 59 + [11., 11.],
                                 low_limit=[10.8] * 59 + [9., 9.], money=2e8, paused=0))

    def test_first_board_requires_prior_price_below_median(self):
        h = self.history()
        raw = {'000001.XSHE': 11.}
        got = signals.stock_flags(h, raw, h.time.iloc[-1].date())
        self.assertTrue(got.loc['000001.XSHE', 'fb'])
        self.assertFalse(got.loc['000001.XSHE', 'lb'])
        h.loc[59, 'close'] = 14.
        got = signals.stock_flags(h, raw, h.time.iloc[-1].date())
        self.assertFalse(got.loc['000001.XSHE', 'fb'])

    def test_missing_history_does_not_create_candidate(self):
        h = self.history().iloc[1:]
        with self.assertRaises(ValueError):
            signals.stock_flags(h, {'000001.XSHE': 11.}, h.time.iloc[-1].date())

    def test_component_priority_is_first_board_then_chain_then_deep(self):
        flags = pd.DataFrame([dict(fb=True, lb=True, gd=True, close_t=11., money=2e8)], index=['000001.XSHE'])
        got = signals.candidates(flags, dict(FB_OK=True, LB_OK=True, DEEP=True), {'000001.XSHE': '测试'}, 1e8)
        self.assertEqual(got[0]['component'], 'FB_dip')

    def test_temperature_detects_prior_limit_up_loss(self):
        codes = [f'{i:06d}.XSHE' for i in range(100)]
        first = pd.DataFrame(dict(code=codes, close=11., high_limit=11., low_limit=9., paused=0))
        second = pd.DataFrame(dict(code=codes, close=10.7, high_limit=12.1, low_limit=9.9, paused=0))
        ice, dn = signals.ice_day(first, second)
        self.assertTrue(ice)
        self.assertEqual(dn, 0.)

    def test_st_flag_nan_cannot_pass(self):
        with self.assertRaises(ValueError):
            signals.checked_st(pd.DataFrame([[np.nan]], columns=['000001.XSHE']), ['000001.XSHE'])


if __name__ == '__main__':
    unittest.main()

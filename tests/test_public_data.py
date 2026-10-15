import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from src.data import PublicData, cent, price_limit
from src import data
from src import run, paper

class PublicDataTests(unittest.TestCase):
    def test_morning_minute_normalization_needs_only_complete_morning_window(self):
        import pandas as pd
        rows = [dict(时间=f'2026-10-14 09:{i:02d}:00', 开盘=10, 最高=10.1, 最低=9.9,
                     收盘=10, 成交量=12, 成交额=12000) for i in range(30, 37)]
        normalized = data.normalize_minute_frame(pd.DataFrame(rows), date(2026, 10, 14),
                                                 date(2026, 10, 14), require_exit=False)
        self.assertEqual(normalized['09:36']['volume'], 1200)
        with self.assertRaisesRegex(ValueError, '09:36'):
            data.normalize_minute_frame(pd.DataFrame(rows[:-1]), date(2026, 10, 14),
                                        date(2026, 10, 14), require_exit=False)

    def test_public_morning_session_fetches_current_window_only(self):
        import pandas as pd
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            provider = PublicData(root)
            provider.today = date(2026, 10, 14)
            code = '000001.XSHE'
            candidate = dict(code=code, name='测试', component='FB_dip', pri=0, prev_close=10.,
                             signal_close=10., money=2e8, open=9.5, high_limit=11., low_limit=9.)
            rows = [dict(时间=f'2026-10-14 09:{i:02d}:00', 开盘=9.5, 最高=9.52, 最低=9.48,
                         收盘=9.5, 成交量=12, 成交额=12000) for i in range(30, 37)]
            with patch('akshare.stock_zh_a_hist_min_em', return_value=pd.DataFrame(rows), create=True) as fetch:
                daily, minutes = provider.morning_session(provider.today, [candidate], {}, paper.DEFAULTS)
            self.assertEqual(fetch.call_args.kwargs['end_date'], '2026-10-14 09:37:00')
            self.assertEqual(minutes[code]['09:36']['volume'], 1200)
            self.assertEqual(daily[code]['open'], 9.5)

    def test_missing_held_stock_morning_mark_does_not_block_candidate_fills(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as root:
            provider = PublicData(root)
            provider.today = date(2026, 10, 14)
            code = '000001.XSHE'
            held = '600000.XSHG'
            candidate = dict(code=code, name='测试', component='FB_dip', pri=0, prev_close=10.,
                             signal_close=10., money=2e8, open=9.5, high_limit=11., low_limit=9.)
            rows = [dict(时间=f'2026-10-14 09:{i:02d}:00', 开盘=9.5, 最高=9.52, 最低=9.48,
                         收盘=9.5, 成交量=12, 成交额=12000) for i in range(30, 37)]
            with patch('akshare.stock_zh_a_hist_min_em', side_effect=[pd.DataFrame(rows),
                       RuntimeError('held mark feed unavailable')], create=True):
                _, minutes = provider.morning_session(provider.today, [candidate], {held: {}}, paper.DEFAULTS)
            self.assertIn(code, minutes)
            self.assertNotIn(held, minutes)

    def test_minute_normalization_converts_hands_to_shares_and_validates_day(self):
        import pandas as pd
        self.assertTrue(callable(getattr(data, 'normalize_minute_frame', None)))
        rows = []
        for tm in [*(f'09:{i:02d}' for i in range(30, 37)), *(f'14:{i:02d}' for i in range(50, 58))]:
            rows.append(dict(时间=f'2026-10-13 {tm}:00', 开盘=10, 最高=10.1, 最低=9.9,
                             收盘=10, 成交量=12, 成交额=12000))
        normalized = data.normalize_minute_frame(pd.DataFrame(rows), date(2026, 10, 13), date(2026, 10, 13))
        self.assertEqual(normalized['09:30']['volume'], 1200)
        self.assertEqual(normalized['09:30']['money'], 12000)
        with self.assertRaisesRegex(ValueError, 'target date'):
            data.normalize_minute_frame(pd.DataFrame(rows), date(2026, 10, 12), date(2026, 10, 13))

    def test_recent_historical_minute_replay_requires_exact_date_and_complete_windows(self):
        import pandas as pd
        rows = []
        for tm in [*(f'09:{i:02d}' for i in range(30, 37)), *(f'14:{i:02d}' for i in range(50, 58))]:
            rows.append(dict(时间=f'2026-10-14 {tm}:00', 开盘=10, 最高=10.1, 最低=9.9,
                             收盘=10, 成交量=12, 成交额=12000))
        normalized = data.normalize_minute_frame(pd.DataFrame(rows), date(2026, 10, 14),
                                                 date(2026, 10, 15), allow_historical=True)
        self.assertEqual(normalized['09:30']['volume'], 1200)
        with self.assertRaisesRegex(ValueError, 'target date'):
            data.normalize_minute_frame(pd.DataFrame(rows), date(2026, 10, 13),
                                        date(2026, 10, 15), allow_historical=True)
        with self.assertRaisesRegex(ValueError, 'latest available'):
            data.normalize_minute_frame(pd.DataFrame(rows), date(2026, 10, 14),
                                        date(2026, 10, 15), allow_historical=False)

    def test_minute_normalization_rejects_missing_windows_zero_open_and_bad_ohlc(self):
        import pandas as pd
        self.assertTrue(callable(getattr(data, 'normalize_minute_frame', None)))
        rows = []
        for tm in [*(f'09:{i:02d}' for i in range(30, 37)), *(f'14:{i:02d}' for i in range(50, 58))]:
            rows.append(dict(时间=f'2026-10-13 {tm}:00', 开盘=10, 最高=10.1, 最低=9.9,
                             收盘=10, 成交量=12, 成交额=12000))
        frame = pd.DataFrame(rows)
        with self.assertRaisesRegex(ValueError, '14:57'):
            data.normalize_minute_frame(frame.iloc[:-1], date(2026, 10, 13), date(2026, 10, 13))
        frame.loc[0, '开盘'] = 0
        with self.assertRaisesRegex(ValueError, 'zero|invalid'):
            data.normalize_minute_frame(frame, date(2026, 10, 13), date(2026, 10, 13))
        frame.loc[0, '开盘'] = 11
        with self.assertRaisesRegex(ValueError, 'OHLC'):
            data.normalize_minute_frame(frame, date(2026, 10, 13), date(2026, 10, 13))

    def test_session_uses_candidate_name_to_exclude_st_stocks(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as root:
            p = PublicData(root)
            p.today = date(2026, 10, 13)
            code = '000001.XSHE'
            p.frames[code] = pd.DataFrame([dict(time=pd.Timestamp('2026-10-13'), open=9.5, close=9.6,
                high_limit=11., low_limit=9., paused=0, factor=1.)])
            c = dict(code=code, name='ST测试', component='FB_dip', pri=0, prev_close=10.,
                     money=2e8, open=9.5, high_limit=11., low_limit=9.)
            bars = pd.DataFrame([dict(时间=f'2026-10-13 {tm}:00', 开盘=10, 最高=10.1, 最低=9.9,
                收盘=10, 成交量=12, 成交额=12000) for tm in
                [*(f'09:{i:02d}' for i in range(30, 37)), *(f'14:{i:02d}' for i in range(50, 58))]])
            with patch('akshare.stock_zh_a_hist_min_em', return_value=bars, create=True):
                daily, minutes = p.session(date(2026, 10, 13), [c], {}, paper.DEFAULTS)
            self.assertEqual(daily[code]['name'], 'ST测试')
            self.assertTrue(daily[code]['is_st'])
            self.assertNotIn(code, minutes)

    def test_sina_factor_json_ignores_trailing_javascript(self):
        import json
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            p = PublicData(root)
            hist = Mock(text='var data="compressed";')
            factors = Mock(text='var qfq={"data":[{"d":"2026-10-09","f":"1.0"}]}\nvar extra=1;')
            rows = [dict(date='2026-10-08',open=10,high=10,low=10,close=10,volume=100,amount=1000),
                    dict(date='2026-10-09',open=11,high=11,low=11,close=11,volume=100,amount=1100)]
            with patch('src.data.get', side_effect=[hist,factors]), patch('py_mini_racer.MiniRacer') as js:
                js.return_value.call.return_value = rows
                frame = p.daily('000001.XSHE')
            self.assertEqual(len(frame), 2)
            self.assertEqual(frame.iloc[-1].high_limit, 11)

    def test_price_limits_use_decimal_half_up(self):
        self.assertEqual(cent(10.005), 10.01)
        self.assertEqual(cent(11.55), 11.55)

    def test_failed_empty_account_preparation_does_not_consume_day(self):
        with tempfile.TemporaryDirectory() as root:
            p = PublicData(root)
            account = paper.new_account('2026-10-08')
            before = copy.deepcopy(account)
            with patch.object(p, 'days', return_value=[date(2026,10,8)]), patch.object(p, 'prepare', side_effect=ValueError('missing history')):
                with self.assertRaises(ValueError):
                    run.process(p, account, paper.DEFAULTS, date(2026,10,8), Path(root))
            self.assertEqual(account, before)

    def test_limit_rounding_avoids_binary_float_half_cent_error(self):
        self.assertEqual(price_limit(1.65, -.1), 1.49)
        self.assertEqual(price_limit(8.45, -.1), 7.61)
        self.assertEqual(price_limit(10.25, .1), 11.28)

class CloseDateTests(unittest.TestCase):
    def test_scan_prepares_next_session_from_report_date(self):
        import pandas as pd
        from src.data import INDEX
        with tempfile.TemporaryDirectory() as root:
            provider=PublicData(root)
            dates=pd.bdate_range(end='2026-10-12', periods=70)
            provider.pool={f'{i:06d}.XSHE': dict(name='测试', inclusion='1900-01-01') for i in range(100)}
            provider.names={c: '测试' for c in provider.pool}
            provider.listed={c: date(2000,1,1) for c in provider.pool}
            for code in list(provider.pool)+[INDEX]:
                frame=pd.DataFrame(dict(time=dates,code=code,open=10.,close=10.,high_limit=11.,low_limit=9.,
                                        money=2e8,paused=0,factor=1.,corporate_action=False))
                frame.loc[frame.index[-1], 'close']=11.
                provider.frames[code]=frame
            with patch.object(provider, 'load_pool'), patch('src.data.signals.stock_flags') as flags:
                flags.return_value=pd.DataFrame([dict(fb=True,lb=False,gd=False,close_t=11.,money=2e8)],index=['000001.XSHE'])
                with patch('src.data.signals.candidates', return_value=[dict(code='000001.XSHE',name='测试',component='FB_dip',pri=0,signal_close=11.,prev_close=11.,money=2e8)]):
                    result=provider.scan(date(2026,10,12),{},paper.DEFAULTS,after_close=True)
            self.assertEqual(result['signal_date'],'2026-10-12')
            self.assertEqual(result['candidates'][0]['signal_date'],'2026-10-12')
            raw=flags.call_args.args[1]
            self.assertEqual(raw['000001.XSHE'],11.)
            self.assertEqual(flags.call_args.args[2],date(2026,10,12))

if __name__ == '__main__':
    unittest.main()


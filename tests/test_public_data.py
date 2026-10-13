import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from src.data import PublicData, cent, price_limit
from src import run, paper

class PublicDataTests(unittest.TestCase):
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

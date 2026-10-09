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

if __name__ == '__main__':
    unittest.main()

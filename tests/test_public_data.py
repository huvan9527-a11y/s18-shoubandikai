import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from src.data import PublicData, cent
from src import run, paper

class PublicDataTests(unittest.TestCase):
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

    def test_held_corporate_action_stops_session(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as root:
            p = PublicData(root)
            frame = pd.DataFrame([dict(time=pd.Timestamp('2026-10-09'),corporate_action=True)])
            with patch.object(p, 'daily', return_value=frame):
                with self.assertRaisesRegex(ValueError, 'corporate action'):
                    p.session(date(2026,10,9), [], {'000001.XSHE':{'name':'test'}}, paper.DEFAULTS)

if __name__ == '__main__':
    unittest.main()

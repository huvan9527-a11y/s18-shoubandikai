import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src.daily_accounts import buy, close_account, execute, save
from src.paper import new_account, DEFAULTS, COMPONENTS

class DailyAccounts(unittest.TestCase):
    def test_deep_range(self):
        self.assertEqual(COMPONENTS['DEEP']['gap'], (-.07,-.03))

    def test_open_price_next_day_close_and_independent_accounts(self):
        a, b = new_account('2026-10-26'), new_account('2026-10-26')
        c = dict(code='000001.XSHE', name='示例', component='DEEP', money=1e9)
        q = {c['code']:dict(open=9.5, prev_close=10, name='示例')}
        fills,_ = buy(a,[c],q,'2026-10-26',DEFAULTS,True)
        self.assertEqual(fills[0]['price'],9.5)
        self.assertEqual(b['cash'],200000)
        daily={c['code']:dict(close=9.8, low_limit=9, paused=False)}
        self.assertEqual(close_account(a,'2026-10-26',daily,DEFAULTS)[0],[])
        sold,_=close_account(a,'2026-10-27',daily,DEFAULTS)
        self.assertEqual(sold[0]['price'],9.8)
        self.assertFalse(a['positions'])
        # B does not require a second low-gap signal.
        q[c['code']]['open']=10.5
        fills,_=buy(b,[c],q,'2026-10-27',DEFAULTS,False)
        self.assertEqual(fills[0]['price'],10.5)
        self.assertEqual(close_account(b,'2026-10-27',daily,DEFAULTS)[0],[])
        self.assertTrue(close_account(b,'2026-10-28',daily,DEFAULTS)[0])

    def test_limit_down_defers_exit(self):
        a=new_account('2026-10-26')
        c=dict(code='000001.XSHE',name='示例',component='LB',money=1e9)
        buy(a,[c],{c['code']:dict(open=9.5,prev_close=10,name='示例')},'2026-10-26',DEFAULTS,False)
        fills,blocked=close_account(a,'2026-10-27',{c['code']:dict(close=9,low_limit=9,paused=False)},DEFAULTS)
        self.assertFalse(fills)
        self.assertTrue(blocked)
        self.assertTrue(a['positions'])

    def test_retry_only_notifies_saved_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            report={n:dict(fills=[],blocked=[]) for n in ('A','B')}
            state=dict(A=new_account('2026-10-26'),B=new_account('2026-10-26'),
                       sessions={'2026-10-26-open':report},notifications={})
            save(root,state)
            with patch('src.daily_accounts.notify') as notify, patch('src.daily_accounts.PublicData') as provider:
                execute('open',root,dt.datetime(2026,10,26,9,31,tzinfo=dt.timezone(dt.timedelta(hours=8))))
                provider.assert_not_called()
                notify.assert_called_once()

if __name__=='__main__': unittest.main()

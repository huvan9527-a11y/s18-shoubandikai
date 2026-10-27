import datetime as dt
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from src import daily_accounts as d
from src.paper import new_account, DEFAULTS

NOW = dt.datetime(2026,10,28,15,0,5,tzinfo=d.TZ)
CODE = '600001.XSHG'

class CapturedExecution(unittest.TestCase):
    def seed(self, root):
        (root/'config.json').write_text('{}')
        a = new_account('2026-10-27')
        a['cash'] -= 1005
        a['positions'][CODE] = dict(name='demo',component='LB',amount=100,
            entry_date='2026-10-27',cost=1005,mark=10)
        state=dict(A=a,B=new_account('2026-10-27'),sessions={},notifications={})
        d.save(root,state)
        return state

    def calendar(self):
        return patch.dict(sys.modules,akshare=types.SimpleNamespace(
            tool_trade_date_hist_sina=lambda:pd.DataFrame({'trade_date':[
                dt.date(2026,10,27),dt.date(2026,10,28),dt.date(2026,10,29)]})))

    def test_capture_saves_before_notification_and_restart_does_not_resell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.seed(root)
            quotes={CODE:dict(close=10.5,low_limit=9,paused=False,quote_time=NOW.isoformat())}
            with self.calendar(),patch.object(d,'close_quotes',return_value=quotes),patch.object(d,'notify',side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):
                    d.execute('capture-close',root,NOW)
            state=d.load_accounts(root)
            self.assertEqual(len(state['A']['trades']),1)
            self.assertFalse(state['A']['positions'])
            self.assertTrue((root/'outputs/evidence-2026-10-28-close.json').exists())
            cash=state['A']['cash']
            with patch.object(d,'close_quotes') as fetch,patch.object(d,'notify'):
                d.execute('capture-close',root,NOW+dt.timedelta(minutes=1))
                fetch.assert_not_called()
            self.assertEqual(d.load_accounts(root)['A']['cash'],cash)
            self.assertEqual(len(d.load_accounts(root)['A']['trades']),1)

    def test_evening_never_fetches_old_prices_or_creates_a_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.seed(root)
            with self.calendar(),patch.object(d,'PublicData') as provider,patch.object(d,'notify') as notify:
                d.execute('close',root,NOW.replace(hour=18))
                provider.assert_not_called()
                notify.assert_not_called()
            state=d.load_accounts(root)
            self.assertIn(CODE,state['A']['positions'])
            self.assertFalse(state['A']['trades'])
            self.assertEqual(state['sessions']['2026-10-28-close']['A']['status'],'missed_close_capture')

    def test_missing_live_close_retains_position_and_allows_live_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.seed(root)
            with self.calendar(),patch.object(d,'close_quotes',side_effect=ValueError('missing')):
                with self.assertRaises(ValueError):d.execute('capture-close',root,NOW)
            state=d.load_accounts(root)
            self.assertIn(CODE,state['A']['positions'])
            self.assertNotIn('2026-10-28-close',state['sessions'])

    def test_live_window_rejects_late_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.seed(root)
            with self.calendar(),patch.object(d,'close_quotes') as fetch:
                with self.assertRaises(ValueError):d.execute('capture-close',root,NOW.replace(hour=18))
                fetch.assert_not_called()

    def test_final_quote_requires_current_final_timestamp(self):
        def response(stamp):
            fields=['0']*49
            for k,v in {2:'600001',3:'10.5',6:'100',30:stamp,48:'9'}.items():fields[k]=v
            return types.SimpleNamespace(content=('v_sh600001="'+ '~'.join(fields)+'";').encode('gbk'),
                                         raise_for_status=lambda:None)
        self.assertEqual(d.close_quotes([CODE],NOW,fetch=lambda *a,**kw:response('20261028150000'))[CODE]['close'],10.5)
        for stamp in ['20261027150000','20261028145959']:
            with self.assertRaises(ValueError):
                d.close_quotes([CODE],NOW,fetch=lambda *a,**kw:response(stamp))

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import postclose as p

NOW=dt.datetime(2026,10,30,17,0,tzinfo=ZoneInfo('Asia/Shanghai'))

class PostcloseTests(unittest.TestCase):
    def test_missing_or_stale_data_is_waiting_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertIsNone(p.snapshot(root,'s18','2026-10-30')['count'])
            p.save(root/'reports/screening_status.json',dict(date='2026-10-29',status='provisional',signals=0))
            data=p.snapshot(root,'longhu','2026-10-30')
            self.assertEqual(data['status'],'waiting');self.assertIsNone(data['count'])

    def test_initial_waiting_then_changed_supplement_and_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.dict('os.environ',{'SERVERCHAN_SENDKEY':'SCTtest'},clear=True),patch.object(p,'deliver',return_value={'code':0}) as sender:
                p.run('s18',root=root,now=NOW)
                p.save(root/'outputs/signals-2026-10-30.json',dict(date='2026-10-30',status='scanned',next_session=dict(signal_date='2026-10-30',candidates=[])))
                p.run('s18','update',root, NOW)
                p.run('s18','update',root, NOW)
                self.assertEqual(sender.call_count,2)
            state=p.read(root/'state/postclose_notifications.json')
            self.assertEqual(len(state['2026-10-30']['accepted']),2)

    def test_failed_send_not_deduped_and_secret_not_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.dict('os.environ',{'SERVERCHAN_SENDKEY':'SCTtest'},clear=True),patch.object(p,'deliver',side_effect=RuntimeError('secretURL')):
                p.run('s18',root=root,now=NOW)
            contents=(root/'state/postclose_notifications.json').read_text()
            self.assertNotIn('secretURL',contents)
            self.assertEqual(p.read(root/'state/postclose_notifications.json')['2026-10-30']['accepted'],[])
            with patch.dict('os.environ',{'SERVERCHAN_SENDKEY':'SCTtest'},clear=True),patch.object(p,'deliver',return_value={'code':0}) as sender:
                p.run('s18',root=root,now=NOW)
                self.assertEqual(sender.call_count,1)

    def test_quota_failure_visible_without_repeated_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.dict('os.environ',{'SERVERCHAN_SENDKEY':'SCTtest'},clear=True),patch.object(p,'deliver',return_value={'code':40001,'message':'每日发送次数已超限'}) as sender:
                p.run('s18',root=root,now=NOW);p.run('s18',root=root,now=NOW)
                self.assertEqual(sender.call_count,1)
            self.assertEqual(p.read(root/'state/postclose_notifications.json')['2026-10-30']['attempts'][-1]['delivery'],'quota_blocked')

    def test_no_scanning_or_ledger_writes_and_not_before_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p.save(root/'state/account.json',{'cash':123})
            before=(root/'state/account.json').read_bytes()
            with patch.object(p,'deliver') as sender:
                p.run('s18',root=root,now=NOW.replace(hour=16,minute=59))
                sender.assert_not_called()
            self.assertEqual((root/'state/account.json').read_bytes(),before)

    def test_longhu_zero_remains_provisional(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            p.save(root/'reports/screening_status.json',dict(date='2026-10-30',status='provisional',signals=0))
            (root/'reports/current_signals.csv').write_text('date,code,name\n')
            data=p.snapshot(root,'longhu','2026-10-30')
            self.assertEqual(data['status'],'provisional');self.assertEqual(data['count'],0)
            self.assertIn('不代表全市场',p.render(data,NOW)[1])

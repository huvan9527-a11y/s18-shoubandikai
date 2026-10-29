import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import requests
from src import daily_accounts as d, scan
from src.paper import new_account

class DeliveryStatus(unittest.TestCase):
    def test_failed_delivery_preserves_fills_and_retries_only_once_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            state=dict(A=new_account('2026-10-28'), B=new_account('2026-10-28'), sessions={}, notifications={})
            fill=dict(side='buy',code='000001.XSHE',name='test',amount=100,price=10)
            report=dict(A=dict(fills=[fill],blocked=[]), B=dict(fills=[],blocked=[]))
            with patch.dict('os.environ', SERVERCHAN_SENDKEY='SCTtest'), patch.object(d.requests,'post',side_effect=requests.RequestException()) as post:
                d.notify(root,state,'2026-10-28-open',report)
                d.notify(root,state,'2026-10-28-open',report)
                self.assertEqual(post.call_count,1)
            saved=json.loads((root/'state/daily_accounts.json').read_text())
            self.assertNotIn('2026-10-28-open',saved['notifications'])
            self.assertEqual(saved['notification_attempts']['2026-10-28-open']['status'],'deferred')

    def test_empty_session_does_not_consume_quota(self):
        state=dict(notifications={})
        report={n:dict(fills=[],blocked=[]) for n in ('A','B')}
        with patch.object(d.requests,'post') as post:
            d.notify(Path('/unused'),state,'2026-10-28-close',report)
            post.assert_not_called()

    def test_quota_exhaustion_suppresses_same_day_scan_retries(self):
        from src import notify
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'outputs').mkdir()
            (root/'outputs/signals-2026-10-28.md').write_text('report')
            response=Mock()
            response.raise_for_status.side_effect=requests.HTTPError()
            response.json.return_value=dict(code=40001,message='超过当天的发送次数限制[5]')
            report=dict(date='2026-10-28',status='scanned',opening_candidates=[])
            with patch.dict('os.environ',SERVERCHAN_SENDKEY='SCTtest'), patch.object(notify.requests,'post',return_value=response) as post:
                self.assertFalse(notify.push_reports(root,[report]))
                self.assertFalse(notify.push_reports(root,[report]))
                self.assertEqual(post.call_count,1)
            saved=json.loads((root/'state/notifications.json').read_text())
            self.assertNotIn('2026-10-28',saved)
            self.assertIn('_quota_blocked_day',saved)

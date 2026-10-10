import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import requests
from src.notify import endpoint, push_reports


class NotifyTests(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(endpoint('SCT123abc'), 'https://sctapi.ftqq.com/SCT123abc.send')
        self.assertEqual(endpoint('sctp123tabc'), 'https://123.push.ft07.com/send/sctp123tabc.send')
        with self.assertRaises(ValueError):
            endpoint('https://bad/key')

    def test_success_deduplicates_and_changed_report_sends_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'outputs').mkdir()
            (root / 'outputs/signals-2026-10-09.md').write_text('历史报告')
            report = dict(date='2026-10-09', status='scanned', opening_candidates=[])
            response = Mock()
            response.json.return_value = {'code': 0}
            with patch.dict(os.environ, {'SERVERCHAN_SENDKEY': 'SCTtest'}, clear=True), patch('src.notify.requests.post', return_value=response) as post:
                self.assertTrue(push_reports(root, [report]))
                self.assertTrue(push_reports(root, [report]))
                self.assertEqual(post.call_count, 1)
                self.assertIn('不是实时买入通知', post.call_args.kwargs['data']['desp'])
                report['opening_candidates'] = [{'code': '000001'}]
                self.assertTrue(push_reports(root, [report]))
                self.assertEqual(post.call_count, 2)

    def test_rejection_or_timeout_is_not_marked_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'outputs').mkdir()
            (root / 'outputs/signals-2026-10-09.md').write_text('失败报告')
            report = dict(date='2026-10-09', status='failed_data_incomplete', error='missing')
            response = Mock()
            response.json.return_value = {'code': 1}
            with patch.dict(os.environ, {'SERVERCHAN_SENDKEY': 'SCTtest'}, clear=True), patch('src.notify.requests.post', return_value=response) as post:
                self.assertFalse(push_reports(root, [report]))
                self.assertFalse((root / 'state/notifications.json').exists())
                post.side_effect = requests.Timeout('secret URL')
                self.assertFalse(push_reports(root, [report]))
                self.assertFalse((root / 'state/notifications.json').exists())

    def test_no_key_skips_network(self):
        with patch.dict(os.environ, {}, clear=True), patch('src.notify.requests.post') as post:
            self.assertTrue(push_reports('.', []))
            post.assert_not_called()

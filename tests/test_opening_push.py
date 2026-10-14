import copy
import datetime as dt
import unittest
from src.opening_push import evaluate_opening, make_opening_report, main
from src import opening_push
from src.signals import candidates
import pandas as pd

class OpeningTests(unittest.TestCase):
    def test_morning_paper_window_accepts_late_runner_until_ten(self):
        self.assertTrue(callable(getattr(opening_push, 'in_morning_paper_window', None)))
        self.assertTrue(opening_push.in_morning_paper_window(dt.datetime(2026, 10, 14, 9, 37)))
        self.assertTrue(opening_push.in_morning_paper_window(dt.datetime(2026, 10, 14, 10, 0)))
        self.assertFalse(opening_push.in_morning_paper_window(dt.datetime(2026, 10, 14, 10, 1)))
        self.assertFalse(opening_push.in_morning_paper_window(dt.datetime(2026, 10, 17, 9, 37)))

    def test_static_open_quote_remains_valid_during_morning_data_fetch(self):
        checked_at = dt.datetime(2026, 10, 13, 9, 59, tzinfo=opening_push.TZ)
        selected, rows = evaluate_opening(self.report, self.day, self.quotes, checked_at=checked_at)
        self.assertEqual(rows[0]['status'], 'OPENING_RULE_MATCH')
        self.assertEqual(len(selected), 1)

    def test_morning_paper_requires_complete_candidate_quote_coverage(self):
        second = dict(self.c, code='601069.XSHG', name='西部黄金', signal_close=25.48,
                      prev_close=25.48)
        self.report['next_session']['candidates'].append(second)
        with self.assertRaisesRegex(ValueError, 'data incomplete'):
            make_opening_report(self.report, self.day, self.quotes, strict_data=True)

    def test_opening_workflow_can_reach_morning_minutes_and_persist_ledger(self):
        from pathlib import Path
        workflow = Path(__file__).parents[1] / '.github/workflows/s18-opening-alert.yml'
        legacy_workflow = workflow.with_name('s18-opening-0926.yml')
        text = workflow.read_text(encoding='utf-8')
        self.assertRegex(text, r'timeout-minutes:\s*(?:7[5-9]|[8-9]\d|[1-9]\d{2,})')
        self.assertIn('src.opening_push --paper-morning', text)
        self.assertIn('state/account.json', text)
        self.assertIn("github.ref == 'refs/heads/main' && (secrets.SERVERCHAN_SENDKEY", text)
        self.assertIn('while true; do', text)
        self.assertIn('sleep 45', text)
        self.assertFalse(legacy_workflow.exists(), 'legacy 09:26 job can race the morning ledger workflow')
    def test_live_window_includes_0925_and_0935_only(self):
        self.assertTrue(callable(getattr(opening_push, 'in_live_window', None)))
        self.assertTrue(opening_push.in_live_window(dt.datetime(2026, 10, 13, 9, 25)))
        self.assertTrue(opening_push.in_live_window(dt.datetime(2026, 10, 13, 9, 35, 59)))
        self.assertFalse(opening_push.in_live_window(dt.datetime(2026, 10, 13, 9, 24, 59)))
        self.assertFalse(opening_push.in_live_window(dt.datetime(2026, 10, 13, 9, 36)))

    def test_late_run_skips_without_fetching_or_pushing(self):
        from unittest.mock import patch
        current = dt.datetime(2026, 10, 13, 9, 36)
        fake = type('FakeDateTime', (), {'now': staticmethod(lambda tz: current)})
        with patch('src.opening_push.dt.datetime', fake), \
             patch('src.opening_push.quote_batch', side_effect=AssertionError('must skip before quote')), \
             patch('builtins.print') as output:
            self.assertEqual(main([]), 0)
        self.assertIn('skipped', output.call_args.args[0].lower())

    def test_primary_workflow_runs_paper_after_successful_scan(self):
        from pathlib import Path
        workflow = Path(__file__).parents[1] / '.github/workflows/s18-paper.yml'
        text = workflow.read_text(encoding='utf-8')
        scan = text.index('python -u -m src.scan')
        replay = text.index('python -u -m src.run --paper-from-scan')
        self.assertLess(scan, replay)
        self.assertIn('if: success()', text[replay-150:replay])
        self.assertIn('git add state/account.json state/metadata outputs logs', text)
        self.assertIn("github.ref == 'refs/heads/main' && (secrets.SERVERCHAN_SENDKEY", text)

    def setUp(self):
        self.day = dt.date(2026, 10, 13)
        self.c = dict(code='002181.XSHE', name='粤传媒', component='FB_dip', pri=0,
                      signal_date='2026-10-12', signal_close=10.01, prev_close=9.39)
        self.report = dict(date='2026-10-12', status='scanned', previous_day='2026-10-09',
                           candidates=[dict(self.c, signal_close=9.39)],
                           next_session=dict(signal_date='2026-10-12', candidates=[self.c]))
        self.quotes = {'002181.XSHE': dict(open=9.69, prev_close=10.01, name='粤传媒', quote_time='20261013092500')}

    def test_current_close_date_and_new_field_win_over_legacy(self):
        selected, rows = evaluate_opening(self.report, self.day, self.quotes)
        self.assertEqual(rows[0]['status'], 'OPENING_RULE_MATCH')
        self.assertEqual(selected[0]['signal_close'], 10.01)
        self.assertAlmostEqual(selected[0]['gap'], 9.69/10.01-1)
        self.assertEqual(make_opening_report(self.report, self.day, self.quotes), selected)

    def test_reject_legacy_shifted_day_instead_of_renaming_price(self):
        self.report.pop('next_session')
        with self.assertRaisesRegex(ValueError, 'next_session'):
            make_opening_report(self.report, self.day, self.quotes)

    def test_mismatch_isolated_without_relaxing_threshold(self):
        second = dict(self.c, code='601069.XSHG', name='西部黄金', signal_close=25.48)
        self.report['next_session']['candidates'].append(second)
        self.quotes['601069.XSHG'] = dict(open=24.88, prev_close=25.48, name='西部黄金', quote_time='20261013092500')
        self.c['signal_close'] = 9.39
        selected, rows = evaluate_opening(self.report, self.day, self.quotes)
        self.assertEqual(rows[0]['status'], 'PREV_CLOSE_MISMATCH')
        self.assertEqual([c['code'] for c in selected], ['601069.XSHG'])

    def test_all_data_failures_are_not_zero_signal(self):
        self.c['signal_close'] = 9.39
        with self.assertRaisesRegex(ValueError, 'data quality'):
            make_opening_report(self.report, self.day, self.quotes)

    def test_stale_quote_nonfinite_and_gap_reasons(self):
        for changes, reason in [(dict(quote_time='20261012092500'), 'STALE_QUOTE'),
                                (dict(open=float('nan')), 'INVALID_PRICE'),
                                (dict(open=10.01), 'GAP_OUTSIDE_RANGE')]:
            quotes=copy.deepcopy(self.quotes)
            quotes['002181.XSHE'].update(changes)
            selected, rows=evaluate_opening(self.report, self.day, quotes)
            self.assertEqual(selected, [])
            self.assertEqual(rows[0]['status'], reason)

    def test_legacy_price_fallback_without_eager_keyerror(self):
        self.c.pop('signal_close')
        self.c['prev_close']=10.01
        self.assertEqual(len(make_opening_report(self.report, self.day, self.quotes)),1)
        self.c['signal_close']=10.01
        self.c.pop('prev_close')
        self.assertEqual(len(make_opening_report(self.report, self.day, self.quotes)),1)

    def test_signal_emits_raw_close_both_fields(self):
        flags=pd.DataFrame([dict(fb=True,lb=False,gd=False,close_t=10.01,money=2e8)],index=['002181.XSHE'])
        result=candidates(flags,dict(FB_OK=True,LB_OK=True,DEEP=False),{'002181.XSHE':'粤传媒'},1e8)
        self.assertEqual(result[0]['signal_close'],result[0]['prev_close'])


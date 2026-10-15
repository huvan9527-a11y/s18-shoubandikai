import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from datetime import date
from src import run, paper
import test_paper


class Provider:
    def __init__(self):
        self.fail_prepare = False

    def days(self, start_date=None, end_date=None, **kwargs):
        return [date(2026, 10, 8), date(2026, 10, 9)]

    def prepare(self, day, account, cfg):
        if self.fail_prepare:
            raise ValueError('fixture preparation unavailable')
        return test_paper.ReplayTests().fixture()[0], dict(ICE=False)

    def session(self, day, candidates, positions, cfg):
        _, d, m = test_paper.ReplayTests().fixture()
        return d, m


class SingleSessionProvider:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def days(self, start_date=None, end_date=None, **kwargs):
        return [date(2026, 10, 8)]

    def session(self, day, candidates, positions, cfg):
        self.calls.append((day, candidates, positions))
        if self.fail:
            raise ValueError('minute gap')
        return test_paper.ReplayTests().fixture()[1:]


class CatchUpProvider:
    def __init__(self):
        self.calls = []

    def days(self, start_date=None, end_date=None, **kwargs):
        out = []
        day = start_date
        while day <= end_date:
            if day.weekday() < 5:
                out.append(day)
            from datetime import timedelta
            day += timedelta(days=1)
        return out

    def session(self, day, candidates, positions, cfg):
        self.calls.append(day.isoformat())
        return test_paper.ReplayTests().fixture()[1:]


class BrokenCalendarProvider(SingleSessionProvider):
    def days(self, start_date=None, end_date=None, **kwargs):
        raise OSError('calendar source unavailable')


class RunnerTests(unittest.TestCase):
    def test_morning_process_persists_immediate_buys_and_is_idempotent(self):
        self.assertTrue(callable(getattr(run, 'process_morning', None)))
        class MorningProvider:
            def morning_session(self, day, candidates, positions, cfg):
                c, daily, minutes = test_paper.ReplayTests().fixture()
                morning = {'000001.XSHE': {k: v for k, v in minutes['000001.XSHE'].items()
                                            if k.startswith('09:')}}
                return daily, morning
        with tempfile.TemporaryDirectory() as root:
            account = paper.new_account('2026-10-08')
            candidates = test_paper.ReplayTests().fixture()[0]
            self.assertEqual(run.process_morning(MorningProvider(), account, paper.DEFAULTS,
                         '2026-10-08', candidates, Path(root)), 'morning_processed')
            self.assertEqual(len(account['trades']), 1)
            self.assertTrue((Path(root) / 'logs/trades.csv').exists())
            self.assertEqual(json.loads((Path(root) / 'outputs/latest.json').read_text())['status'],
                             'morning_processed')
            before = copy.deepcopy(account)
            self.assertEqual(run.process_morning(MorningProvider(), account, paper.DEFAULTS,
                         '2026-10-08', candidates, Path(root)), 'up_to_date')
            self.assertEqual(account, before)

    def test_morning_data_failure_keeps_published_ledger_unchanged(self):
        class BrokenMorningProvider:
            def morning_session(self, day, candidates, positions, cfg):
                raise ValueError('missing 09:36 bar')
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            account = paper.new_account('2026-10-08')
            run.export(account, root, 'waiting_for_configuration')
            before = copy.deepcopy(account)
            files = {name: (Path(root) / name).read_bytes() for name in
                     ('state/account.json', 'logs/trades.csv', 'outputs/latest.json',
                      'outputs/latest.md', 'outputs/holdings.csv')}
            result = run.process_morning(BrokenMorningProvider(), account, paper.DEFAULTS,
                                         '2026-10-08', [], Path(root))
            self.assertEqual(result, 'invalid_morning_session')
            self.assertEqual(account, before)
            self.assertEqual(files, {name: (Path(root) / name).read_bytes() for name in files})

    def test_report_replay_starts_at_first_valid_date_and_is_idempotent(self):
        self.assertTrue(callable(getattr(run, 'process_report', None)))
        with tempfile.TemporaryDirectory() as root:
            a = paper.new_account('2026-10-01')
            provider = SingleSessionProvider()
            report = dict(date='2026-10-08', status='scanned',
                          opening_candidates=test_paper.ReplayTests().fixture()[0])
            output = io.StringIO()
            with redirect_stdout(output):
                status = run.process_report(provider, a, paper.DEFAULTS, report, Path(root))
            self.assertEqual(status, 'processed')
            self.assertIn('new_fills=1', output.getvalue())
            self.assertEqual(a['start_date'], '2026-10-08')
            self.assertEqual(a['last_processed'], '2026-10-08')
            self.assertEqual(len(a['trades']), 1)
            before = copy.deepcopy(a)
            self.assertEqual(run.process_report(provider, a, paper.DEFAULTS, report, Path(root)), 'up_to_date')
            self.assertEqual(a, before)

    def test_bad_minutes_leave_account_and_published_files_unchanged(self):
        self.assertTrue(callable(getattr(run, 'process_report', None)))
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            a = paper.new_account('2026-10-01')
            run.export(a, root, 'waiting_for_configuration')
            files = {p: p.read_bytes() for p in (root / 'state/account.json', root / 'outputs/latest.json',
                     root / 'outputs/latest.md', root / 'outputs/holdings.csv', root / 'logs/trades.csv',
                     root / 'logs/equity.csv')}
            before = copy.deepcopy(a)
            report = dict(date='2026-10-08', status='scanned',
                          opening_candidates=test_paper.ReplayTests().fixture()[0])
            status = run.process_report(SingleSessionProvider(fail=True), a, paper.DEFAULTS, report, root)
            self.assertEqual(status, 'invalid_session')
            self.assertEqual(a, before)
            self.assertEqual({p: p.read_bytes() for p in files}, files)
            self.assertTrue((root / 'outputs/paper-status.json').exists())

    def test_non_scanned_report_cannot_change_paper_account(self):
        self.assertTrue(callable(getattr(run, 'process_report', None)))
        with tempfile.TemporaryDirectory() as root:
            a = paper.new_account('2026-10-01')
            before = copy.deepcopy(a)
            result = run.process_report(SingleSessionProvider(), a, paper.DEFAULTS,
                                        dict(date='2026-10-08', status='failed_data_incomplete'), Path(root))
            self.assertEqual(result, 'scan_not_successful')
            self.assertEqual(a, before)

    def test_calendar_lookup_failure_is_reported_without_mutating_account(self):
        a = paper.new_account('2026-10-01')
        a['last_processed'] = '2026-10-07'
        before = copy.deepcopy(a)
        with tempfile.TemporaryDirectory() as root:
            report = dict(date='2026-10-08', status='scanned', opening_candidates=[])
            result = run.process_report(BrokenCalendarProvider(), a, paper.DEFAULTS, report, Path(root))
            self.assertEqual(result, 'calendar_unavailable')
            self.assertEqual(a, before)
            self.assertEqual(json.loads((Path(root) / 'outputs/paper-status.json').read_text(encoding='utf-8'))['status'], 'calendar_unavailable')
    def test_paper_from_scan_replays_missed_reports_in_date_order(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            account = paper.new_account('2026-10-01')
            account['last_processed'] = '2026-10-07'
            cands = test_paper.ReplayTests().fixture()[0]
            for day in ('2026-10-08', '2026-10-09'):
                (root / 'outputs').mkdir(parents=True, exist_ok=True)
                (root / 'outputs' / f'signals-{day}.json').write_text(json.dumps(
                    dict(date=day, status='scanned', opening_candidates=cands)), encoding='utf-8')
            provider = CatchUpProvider()
            status = run.process_reports_from_files(provider, account, paper.DEFAULTS, root)
            self.assertEqual(status, 'processed')
            self.assertEqual(provider.calls, ['2026-10-08', '2026-10-09'])
            self.assertEqual(account['last_processed'], '2026-10-09')
            before = copy.deepcopy(account)
            self.assertEqual(run.process_reports_from_files(provider, account, paper.DEFAULTS, root), 'up_to_date')
            self.assertEqual(account, before)

    def test_catchup_and_repeated_run_preserve_cash(self):
        with tempfile.TemporaryDirectory() as root:
            a = paper.new_account('2026-10-08')
            run.process(Provider(), a, paper.DEFAULTS, date(2026, 10, 9), Path(root))
            self.assertEqual(a['last_processed'], '2026-10-09')
            self.assertEqual(len(a['trades']), 2)
            before = copy.deepcopy(a)
            run.process(Provider(), a, paper.DEFAULTS, date(2026, 10, 9), Path(root))
            self.assertEqual(a, before)
            self.assertTrue((Path(root) / 'logs/trades.csv').exists())

    def test_prepare_failure_still_exits_old_holding(self):
        with tempfile.TemporaryDirectory() as root:
            a = paper.new_account('2026-10-08')
            c, d, m = test_paper.ReplayTests().fixture()
            paper.replay_day(a, '2026-10-08', c, d, m, paper.DEFAULTS)
            provider = Provider()
            provider.fail_prepare = True
            run.process(provider, a, paper.DEFAULTS, date(2026, 10, 9), Path(root))
            self.assertFalse(a['positions'])
            self.assertTrue(a['days'][-1]['preparation_error'])

    def test_export_does_not_fabricate_trades(self):
        with tempfile.TemporaryDirectory() as root:
            a = paper.new_account('2026-10-08')
            run.export(a, Path(root), 'waiting_for_configuration')
            self.assertEqual(len((Path(root) / 'logs/trades.csv').read_text().splitlines()), 1)
            self.assertIn('200,000.00', (Path(root) / 'outputs/latest.md').read_text())


if __name__ == '__main__':
    unittest.main()


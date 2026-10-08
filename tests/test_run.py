import copy
import tempfile
import unittest
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


class RunnerTests(unittest.TestCase):
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

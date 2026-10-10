import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from src import scan

class DailyScanTests(unittest.TestCase):
    def test_daily_scan_succeeds_without_any_minute_provider_or_ledger_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.json').write_text('{}')
            (root / 'state').mkdir()
            ledger = root / 'state/account.json'
            ledger.write_text('{"cash":200000}')
            provider = Mock()
            provider.session.side_effect = AssertionError('Minute data must not be requested')
            provider.scan.return_value = dict(status='scanned', pool_size=1000,
                temperature={}, limitations=[], candidates=[], opening_candidates=[])
            with patch.object(scan, 'ROOT', root), patch.object(scan, 'PublicData', return_value=provider), patch.object(scan, 'push_reports', return_value=True) as notify:
                result = scan.main(['--dates', '2026-10-08', '2026-10-09'])
            self.assertEqual(result, 0)
            notify.assert_called_once()
            self.assertEqual(len(notify.call_args.args[1]), 2)
            self.assertEqual(provider.scan.call_count, 2)
            provider.session.assert_not_called()
            self.assertEqual(ledger.read_text(), '{"cash":200000}')
            for day in ['2026-10-08', '2026-10-09']:
                report = json.loads((root / f'outputs/signals-{day}.json').read_text())
                self.assertNotIn('minute_status', report)
                self.assertNotIn('model_buys', report)

if __name__ == '__main__':
    unittest.main()


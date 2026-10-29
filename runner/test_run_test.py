import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from run_test import run


class RunTestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'src/site_agent').mkdir(parents=True)
        self.ctx = SimpleNamespace(state=self.root / 'state')
        self.test = {'id': 'intake-surface', 'tier': 'deterministic', 'argv': ['python3', '-m', 'pytest']}

    def execute(self, exit_code=0, error=None):
        def command(argv, **kwargs):
            if argv[0] == 'git':
                return SimpleNamespace(stdout='a' * 40)
            self.assertEqual(kwargs['env']['PYTHONPATH'], str(self.root / 'src'))
            self.assertNotIn('shell', kwargs)
            if error:
                raise error
            return SimpleNamespace(returncode=exit_code)
        with patch('run_test.subprocess.run', side_effect=command):
            return run(self.ctx, self.test, self.root)

    def test_pass_is_queued(self):
        receipt = self.execute()
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual(len(list((self.ctx.state / 'test-results').glob('*.json'))), 1)

    def test_nonzero_is_observed_failure_not_unproven_product_cause(self):
        receipt = self.execute(exit_code=1)
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['failure_kind'], 'observed')
        self.assertEqual(receipt['failure_code'], 'test-command-nonzero')

    def test_command_unavailable_is_execution_failure(self):
        receipt = self.execute(error=OSError('missing executable'))
        self.assertEqual(receipt['status'], 'unavailable')
        self.assertEqual(receipt['failure_kind'], 'execution')

    def test_live_is_never_implicitly_executed(self):
        self.test['tier'] = 'live'
        with self.assertRaisesRegex(ValueError, 'target authorization'):
            run(self.ctx, self.test, self.root)

    def test_logs_remain_private(self):
        receipt = self.execute()
        self.assertEqual(Path(receipt['log']).stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import deploy


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patches = [
            patch.object(deploy, 'ROOT', self.root),
            patch.object(deploy, 'receipt_path', lambda project='site-agent': self.root / 'deploy.json'),
            patch.object(deploy, 'service_state',
                         lambda: {'active': 'active', 'started_at': 'now', 'pid': 1}),
            patch.object(deploy, 'health', lambda project='site-agent': None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def checkout(self, sha='a' * 40, dirty=(), branch='main'):
        with patch.object(deploy, 'describe', return_value={
                'sha': sha, 'branch': branch, 'dirty': bool(dirty),
                'dirty_paths': list(dirty), 'dirty_count': len(dirty),
                'checkout': str(self.root)}):
            return deploy.write_receipt(service_started_at='now', service_pid=1)

    def check(self, **kwargs):
        with patch.object(deploy, 'describe', return_value={
                'sha': kwargs.get('sha', 'a' * 40), 'branch': 'main',
                'dirty': bool(kwargs.get('dirty', ())),
                'dirty_paths': list(kwargs.get('dirty', ())),
                'dirty_count': len(kwargs.get('dirty', ())), 'checkout': str(self.root)}):
            return deploy.check()

    def test_never_deployed_is_not_current(self):
        ok, problems = self.check()
        self.assertFalse(ok)
        self.assertIn('never been deployed', problems[0])

    def test_matching_deployment_is_current(self):
        self.checkout()
        ok, problems = self.check()
        self.assertTrue(ok, problems)

    def test_checkout_moved_after_deploy(self):
        self.checkout()
        ok, problems = self.check(sha='b' * 40)
        self.assertFalse(ok)
        self.assertIn('checkout moved', problems[0])

    def test_dirty_work_is_deployed_but_never_reported_as_current(self):
        self.checkout(dirty=('runner/x.py',))
        ok, problems = self.check(dirty=('runner/x.py',))
        self.assertFalse(ok)
        self.assertTrue(any('dirty' in item for item in problems))

    def test_changed_dirty_set_is_reported(self):
        self.checkout()
        ok, problems = self.check(dirty=('runner/new.py',))
        self.assertFalse(ok)
        self.assertTrue(any('differ' in item for item in problems))

    def test_inactive_service_blocks(self):
        self.checkout()
        with patch.object(deploy, 'service_state',
                          lambda: {'active': 'failed', 'started_at': 'now', 'pid': None}):
            ok, problems = deploy.check()
        self.assertFalse(ok)
        self.assertTrue(any('not active' in item for item in problems))

    def test_receipt_records_what_was_deployed(self):
        value = self.checkout(sha='c' * 40, dirty=('runner/a.py',))
        self.assertEqual(value['sha'], 'c' * 40)
        self.assertTrue(value['dirty'])
        self.assertEqual(value['dirty_paths'], ['runner/a.py'])
        self.assertIn('deployed_at', value)
        self.assertEqual(json.loads((self.root / 'deploy.json').read_text())['sha'], 'c' * 40)

    def test_failed_install_never_restarts_or_records(self):
        with patch.object(deploy.subprocess, 'run',
                          return_value=type('R', (), {'returncode': 1, 'stdout': '', 'stderr': ''})()):
            with self.assertRaisesRegex(RuntimeError, 'install.sh failed'):
                deploy.deploy()
        self.assertFalse((self.root / 'deploy.json').exists())


if __name__ == '__main__':
    unittest.main()

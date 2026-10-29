import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import release_gap


class Ctx:
    def __init__(self, root, cfg=None):
        self.state = Path(root)
        self.cfg = cfg or {}
        self.calls = []

    def api(self, url, method='GET', body=None):
        self.calls.append(url)
        if '/commits/main' in url:
            return {'sha': 'a' * 40}
        if '/compare/' in url:
            return {'commits': [{'sha': 'b' * 40, 'files': []}]}
        if '/check-runs' in url:
            return {'check_runs': [{'name': 'ci', 'status': 'completed', 'conclusion': 'success'}]}
        if '/commits/' in url:
            return {'files': [{'filename': 'src/x.py'}]}
        raise AssertionError(url)


class ReleaseGapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Ctx(self.tmp.name, {'auto_release_api': True})

    def running(self):
        return {'releaseId': 'r1', 'sourceSha': 'b' * 40, 'status': 'success'}

    def test_success_queues_once_and_dedupes_event(self):
        state = {'release_gap_events': {}, 'auto_release_queued': {}}

        def record(ctx, st, gap):
            st['auto_release_queued'][gap['main_sha']] = {'releaseId': 'r1', 'queued_at': 0}
            return True

        with patch.object(release_gap, 'running_api', return_value=self.running()), \
             patch.object(release_gap, 'queue_release', side_effect=record) as queue:
            release_gap.step(self.ctx, state, 10)
            release_gap.step(self.ctx, state, 11)
            queue.assert_called_once()
            self.assertEqual(len(list((Path(self.tmp.name) / 'outbox').glob('*.json'))), 1)
            self.assertEqual(json.loads(next((Path(self.tmp.name) / 'outbox').glob('*.json')).read_text())['kind'], 'delivery-gap')
            gap = release_gap.inspect(self.ctx, state, 10)
            self.assertIsNone(gap['blocked_reason'])


    def test_queued_file_matches_canonical_site_agent_queue_contract(self):
        root = Path(self.tmp.name) / 'rel'
        release_dir = root / 'api' / 'releases' / 'r1'
        release_dir.mkdir(parents=True)
        (root / 'api' / 'current').symlink_to(release_dir)
        receipt = root / 'runs' / 'api' / 'r1'
        receipt.mkdir(parents=True)
        (receipt / 'receipt.json').write_text(json.dumps({'status': 'deployed', 'sourceSha': 'b' * 40}))

        written = {}

        class Done:
            returncode = 0
            stdout = ''
            stderr = ''

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ['git', 'clone']:
                (Path(cmd[-1]) / 'deploy').mkdir(parents=True, exist_ok=True)
            return Done()

        def capture_write(path_self, data, *args, **kwargs):
            if path_self.name == 'desired-api-release.json':
                written['value'] = json.loads(data)
            return len(data)

        state = {}
        gap = {'main_sha': 'a' * 40, 'running_sha': 'b' * 40, 'blocked_reason': None}
        with patch.object(release_gap, 'RELEASES', root), \
             patch.object(release_gap.subprocess, 'run', side_effect=fake_run), \
             patch.object(release_gap.secrets, 'token_hex', return_value='deadbeef'), \
             patch.object(release_gap.time, 'strftime', return_value='20261029220000'), \
             patch.object(release_gap.Path, 'write_text', capture_write):
            queued = release_gap.queue_release(self.ctx, state, gap)

        self.assertTrue(queued)
        queue = written['value']
        self.assertEqual(set(queue), {'schema', 'release'})
        self.assertEqual(queue['schema'], 1)
        self.assertEqual(set(queue['release']), {'id', 'sha'})
        self.assertEqual(queue['release']['sha'], 'a' * 40)
        self.assertRegex(queue['release']['id'], r'^[a-z0-9][a-z0-9-]{0,79}$')
        self.assertEqual(queue['release']['id'], '20261029220000-api-auto-deadbeef')

    def test_ci_pending_blocks(self):
        with patch.object(release_gap, 'running_api', return_value=self.running()), \
             patch.object(self.ctx, 'api', side_effect=[{'sha': 'a'*40}, {'commits': []},
                 {'check_runs': [{'name': 'ci', 'status': 'in_progress'}]}]):
            self.assertIn('checks', release_gap.inspect(self.ctx, {}, 0)['blocked_reason'])

    def test_migration_blocks(self):
        with patch.object(release_gap, 'running_api', return_value=self.running()), \
             patch.object(self.ctx, 'api', side_effect=[{'sha': 'a'*40},
                 {'commits': [{'sha': 'c'*40, 'files': [{'filename': 'src/migrations/001.sql'}]}]},
                 {'check_runs': [{'name': 'ci', 'status': 'completed', 'conclusion': 'success'}]}]):
            self.assertIn('migration', release_gap.inspect(self.ctx, {}, 0)['blocked_reason'])


    def test_queue_file_commit_does_not_block(self):
        ctx = Ctx(self.tmp.name)
        original = ctx.api

        def api(url, method='GET', body=None):
            if '/compare/' in url:
                return {'commits': [{'sha': 'c' * 40, 'files': [{'filename': 'deploy/desired-api-release.json'}]}]}
            return original(url, method, body)

        ctx.api = api
        with patch.object(release_gap, 'running_api', return_value=self.running()):
            gap = release_gap.inspect(ctx, {}, 0)
        self.assertIsNone(gap['blocked_reason'])
        self.assertEqual(len(gap['commits']), 1)

    def test_blocked_gap_still_queues_after_block_clears(self):
        state = {'release_gap_events': {'a' * 40 + ':' + 'b' * 40: {'queued': False, 'at': 0}}}
        with patch.object(release_gap, 'running_api', return_value=self.running()), \
             patch.object(release_gap, 'queue_release', return_value=True) as queue:
            release_gap.step(self.ctx, state, 1)
        queue.assert_called_once()
    def test_duplicate_main_sha_and_auto_disabled(self):
        gap = {'main_sha': 'a'*40, 'running_sha': 'b'*40}
        state = {'auto_release_queued': {'a'*40: {} }}
        self.assertFalse(release_gap.queue_release(self.ctx, state, gap))
        self.ctx.cfg['auto_release_api'] = False
        self.assertFalse(release_gap.queue_release(self.ctx, {}, gap))

    def test_inflight_receipt_blocks(self):
        gap = {'main_sha': 'a'*40, 'running_sha': 'b'*40}
        root = Path(self.tmp.name)
        active = root / 'active'
        active.mkdir()
        api = root / 'api'
        api.mkdir()
        (api / 'current').symlink_to(active, target_is_directory=True)
        with patch.object(release_gap, 'RELEASES', root), \
             patch.object(release_gap, '_receipts', return_value=[(active.stat().st_mtime + 5,
                                                                    {'status': 'building'})]):
            self.assertFalse(release_gap.queue_release(self.ctx, {}, gap))


if __name__ == '__main__':
    unittest.main()

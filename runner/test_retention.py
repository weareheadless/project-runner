import gzip
import json
import tempfile
import unittest
from pathlib import Path

import retention


class Ctx:
    def __init__(self, root):
        self.state = Path(root)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Ctx(self.tmp.name)
        self.now = 1_800_000_000

    def test_prunes_only_old_terminal_workers_and_archives_them(self):
        workers = {
            'old': {'session': 's1', 'terminal': True, 'started': self.now - 9 * 86400, 'issue': 1},
            'recent': {'session': 's2', 'terminal': True, 'started': self.now - 2 * 86400},
            'live': {'session': 's3', 'terminal': False, 'started': self.now - 30 * 86400},
        }
        (self.ctx.state / 'workers.json').write_text(json.dumps(workers))
        state = {}
        removed = retention.prune(self.ctx, state, self.now)
        self.assertEqual(removed['workers'], 1)
        kept = json.loads((self.ctx.state / 'workers.json').read_text())
        self.assertEqual(set(kept), {'recent', 'live'})
        archive = next((self.ctx.state / 'archive').glob('workers-*.jsonl.gz'))
        rows = [json.loads(line) for line in gzip.open(archive, 'rt').read().splitlines()]
        self.assertEqual(rows[0]['session'], 's1')
        self.assertEqual(rows[0]['issue'], 1)

    def test_drops_aged_bookkeeping_and_orphan_session_bindings(self):
        (self.ctx.state / 'workers.json').write_text(json.dumps({'live': {'session': 's3', 'terminal': False}}))
        state = {
            'active': {'id': 's3'},
            'release_gap_events': {'old': {'at': self.now - 30 * 86400, 'queued': True},
                                   'new': {'at': self.now - 60, 'queued': True}},
            'auto_release_queued': {'a' * 40: {'queued_at': self.now - 30 * 86400, 'releaseId': 'r'}},
            'session_credentials': {'s3': 'cred_a', 'gone': 'cred_b'},
            'quota_seen': [f'm{i}' for i in range(600)],
        }
        removed = retention.prune(self.ctx, state, self.now)
        self.assertEqual(removed['gap_events'], 1)
        self.assertEqual(removed['auto_release'], 1)
        self.assertEqual(removed['sessions'], 1)
        self.assertEqual(removed['quota_markers'], 100)
        self.assertEqual(set(state['release_gap_events']), {'new'})
        self.assertEqual(set(state['session_credentials']), {'s3'})
        self.assertEqual(len(state['quota_seen']), 500)

    def test_runs_at_most_hourly(self):
        state = {'retention_last_run': self.now - 60}
        removed = retention.prune(self.ctx, state, self.now)
        self.assertEqual(removed, {'skipped': 'interval'})
        self.assertFalse((self.ctx.state / 'archive').exists())


if __name__ == '__main__':
    unittest.main()

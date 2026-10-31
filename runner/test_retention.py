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


class BuildSweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'releases'
        self.root.mkdir()
        self.now = 1_800_000_000
        self.ctx = Ctx(self.tmp.name)
        self.ctx.cfg = {}

    def checkout(self, name, age_days=2):
        path = self.root / name
        (path / 'node_modules').mkdir(parents=True)
        (path / 'node_modules' / 'blob').write_bytes(b'x' * 1024)
        import os
        stamp = self.now - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def fake_git(self, answers):
        def git(directory, *args):
            key = ' '.join(args)
            for pattern, value in answers.items():
                if pattern in key:
                    return value
            raise AssertionError(key)
        return git

    def test_removes_clean_synced_old_checkout(self):
        path = self.checkout('build-clean')
        answers = {
            'rev-parse --is-inside-work-tree': 'true',
            'symbolic-full-name': 'origin/codex/x',
            'status --porcelain': '',
            'rev-list --count': '0',
        }
        original = retention._git
        retention._git = self.fake_git(answers)
        self.addCleanup(setattr, retention, '_git', original)
        summary = retention.sweep_builds(self.ctx, {}, self.now, roots=[str(self.root)])
        self.assertEqual(summary['removed'], ['build-clean'])
        self.assertEqual(summary['freed_bytes'], 1024)
        self.assertFalse(path.exists())

    def test_keeps_dirty_ahead_recent_unowned_and_unknown(self):
        self.checkout('build-dirty')
        self.checkout('build-ahead')
        self.checkout('build-recent', age_days=0)
        self.checkout('build-no-upstream')
        (self.root / 'not-git').mkdir()

        def git(directory, *args):
            key = ' '.join(args)
            name = Path(directory).name
            if 'is-inside-work-tree' in key:
                return 'true' if name.startswith('build') else None
            if 'symbolic-full-name' in key:
                return None if name == 'build-no-upstream' else 'origin/x'
            if 'status --porcelain' in key:
                return ' M file' if name == 'build-dirty' else ''
            if 'rev-list --count' in key:
                return '2' if name == 'build-ahead' else '0'
            raise AssertionError(key)

        original = retention._git
        retention._git = git
        self.addCleanup(setattr, retention, '_git', original)
        summary = retention.sweep_builds(self.ctx, {}, self.now, roots=[str(self.root)])
        self.assertEqual(summary['removed'], [])
        self.assertEqual(summary['skipped']['dirty'], 1)
        self.assertEqual(summary['skipped']['ahead'], 1)
        self.assertEqual(summary['skipped']['recent'], 1)
        self.assertEqual(summary['skipped']['no-upstream'], 1)
        self.assertEqual(summary['skipped']['not-git'], 1)

    def test_git_failure_is_unknown_not_deletable(self):
        path = self.checkout('build-unknown')
        original = retention._git

        def broken(*args, **kwargs):
            return None

        retention._git = broken
        self.addCleanup(setattr, retention, '_git', original)
        summary = retention.sweep_builds(self.ctx, {}, self.now, roots=[str(self.root)])
        self.assertEqual(summary['removed'], [])
        self.assertTrue(path.exists())

    def test_missing_root_is_ignored(self):
        summary = retention.sweep_builds(self.ctx, {}, self.now, roots=['/nonexistent/root'])
        self.assertEqual(summary['removed'], [])

    def test_configured_roots_come_from_project_config(self):
        self.checkout('build-configured')
        self.ctx.cfg = {'build_checkout_roots': [str(self.root)]}
        answers = {
            'rev-parse --is-inside-work-tree': 'true',
            'symbolic-full-name': 'origin/x',
            'status --porcelain': '',
            'rev-list --count': '0',
        }
        original = retention._git
        retention._git = self.fake_git(answers)
        self.addCleanup(setattr, retention, '_git', original)
        summary = retention.sweep_builds(self.ctx, {}, self.now)
        self.assertEqual(summary['removed'], ['build-configured'])


class SessionsDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'sessions.db'
        self.now = 1_800_000_000

    def build(self, rows=40000):
        import sqlite3
        connection = sqlite3.connect(self.db)
        connection.execute('CREATE TABLE part (id integer primary key, data text)')
        connection.executemany('INSERT INTO part (data) VALUES (?)', [('x' * 2000,) for _ in range(rows)])
        connection.commit()
        connection.execute('DELETE FROM part WHERE id > 50')
        connection.commit()
        connection.close()

    def free_bytes(self):
        import sqlite3
        connection = sqlite3.connect(self.db)
        value = connection.execute('PRAGMA freelist_count').fetchone()[0] * connection.execute('PRAGMA page_size').fetchone()[0]
        connection.close()
        return value

    def test_rewrites_database_with_many_free_pages(self):
        self.build()
        before = self.free_bytes()
        self.assertGreater(before, 1024 * 1024)
        state = {}
        summary = retention.reclaim_sessions_db(state, self.db, threshold_mb=1, now=self.now)
        self.assertGreater(summary['freed_bytes'], 0)
        self.assertLess(self.free_bytes(), 1024 * 1024)
        self.assertEqual(state['sessions_db_vacuumed_at'], self.now)

    def test_skips_small_database_and_recent_rewrite(self):
        self.build(rows=200)
        self.assertEqual(retention.reclaim_sessions_db({}, self.db, threshold_mb=512, now=self.now)['skipped'], 'below-threshold')
        self.db.unlink()
        self.build()
        state = {'sessions_db_vacuumed_at': self.now - 60}
        self.assertEqual(retention.reclaim_sessions_db(state, self.db, threshold_mb=1, now=self.now)['skipped'], 'interval')

    def test_missing_or_corrupt_database_never_raises(self):
        self.assertEqual(retention.reclaim_sessions_db({}, self.db, now=self.now)['skipped'], 'missing')
        self.db.write_bytes(b'not a database')
        self.assertIn(retention.reclaim_sessions_db({}, self.db, now=self.now)['skipped'],
                      {'DatabaseError', 'OperationalError'})

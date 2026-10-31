#!/usr/bin/env python3
"""Bounded, archived pruning of runner state.

Everything removed here is first appended to an archive file, so pruning is
recoverable and never the only copy of a record. Only terminal worker rows and
aged bookkeeping entries are eligible; live work is never touched.
"""
import gzip
import json
import shutil
import subprocess
import time
from pathlib import Path

DEFAULT_DAYS = 7
DEFAULT_BUILD_ROOTS = ['/home/admin/releases']
DEFAULT_SESSIONS_DB = '/home/admin/.local/share/opencode/opencode.db'
INTERVAL_SECONDS = 3600


def _save(path, data):
    # Imported lazily: supervisor imports this module during its own setup.
    from supervisor import save
    save(path, data)


def _archive(path: Path, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, 'at', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + '\n')


def _age(value, now):
    try:
        return now - float(value)
    except (TypeError, ValueError):
        return None


def _git(directory, *args):
    """Run one bounded read-only git query; any failure means 'unknown'."""
    try:
        result = subprocess.run(['git', '-C', str(directory), *args],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return result.stdout.strip()


def _tree_bytes(directory):
    total = 0
    for path in Path(directory).rglob('*'):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def reclaim_sessions_db(state, path=None, threshold_mb=512, now=None):
    """Rewrite the agent session database when deleted events leave free pages.

    OpenCode deletes old stream events but SQLite keeps the pages, so the file
    only ever grows (it reached 9.8GB with 8.8GB free). A rewrite is expensive,
    so it runs at most daily and only past a real threshold.
    """
    import sqlite3

    now = time.time() if now is None else now
    target = Path(path or DEFAULT_SESSIONS_DB)
    summary = {'freed_bytes': 0, 'skipped': None}
    if not target.is_file():
        summary['skipped'] = 'missing'
        return summary
    if now - float(state.get('sessions_db_vacuumed_at') or 0) < 86400:
        summary['skipped'] = 'interval'
        return summary
    try:
        connection = sqlite3.connect(target, timeout=10)
        try:
            free = connection.execute('PRAGMA freelist_count').fetchone()[0]
            page = connection.execute('PRAGMA page_size').fetchone()[0]
        finally:
            connection.close()
        free_bytes = free * page
        if free_bytes < threshold_mb * 1024 * 1024:
            summary['skipped'] = 'below-threshold'
            return summary
        before = target.stat().st_size
        connection = sqlite3.connect(target, timeout=30)
        try:
            connection.execute('PRAGMA busy_timeout=30000')
            connection.execute('VACUUM')
        finally:
            connection.close()
        summary['freed_bytes'] = before - target.stat().st_size
    except (sqlite3.Error, OSError) as exc:
        summary['skipped'] = type(exc).__name__
        return summary
    state['sessions_db_vacuumed_at'] = now
    return summary


def sweep_builds(ctx, state, now=None, days=1, roots=None):
    """Delete agent build checkouts that provably hold no unique work.

    Only immediate children of the configured roots are considered. A checkout
    is removable only when it is a git work tree, clean, tracks an upstream, has
    no commit ahead of that upstream, and has been untouched for ``days``.
    Anything unknown is kept: losing unpushed work costs far more than disk.
    """
    now = time.time() if now is None else now
    configured = roots or (ctx.cfg.get('build_checkout_roots') if getattr(ctx, 'cfg', None) else None)
    summary = {'removed': [], 'freed_bytes': 0,
               'skipped': {'dirty': 0, 'ahead': 0, 'no-upstream': 0, 'recent': 0, 'not-git': 0}}
    for root in configured or DEFAULT_BUILD_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        for directory in sorted(base.iterdir()):
            try:
                if directory.is_symlink() or not directory.is_dir():
                    continue
                age = now - directory.stat().st_mtime
            except OSError:
                continue
            if _git(directory, 'rev-parse', '--is-inside-work-tree') != 'true':
                summary['skipped']['not-git'] += 1
                continue
            if _git(directory, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}') is None:
                summary['skipped']['no-upstream'] += 1
                continue
            dirty = _git(directory, 'status', '--porcelain')
            if dirty is None or dirty:
                summary['skipped']['dirty'] += 1
                continue
            if _git(directory, 'rev-list', '--count', '@{u}..HEAD') != '0':
                summary['skipped']['ahead'] += 1
                continue
            if age < days * 86400:
                summary['skipped']['recent'] += 1
                continue
            size = _tree_bytes(directory)
            shutil.rmtree(directory, ignore_errors=True)
            if directory.exists():
                continue
            summary['removed'].append(directory.name)
            summary['freed_bytes'] += size
    return summary


def prune(ctx, state, now=None, days=DEFAULT_DAYS):
    """Prune aged bookkeeping once per interval. Returns a summary."""
    now = time.time() if now is None else now
    last = state.get('retention_last_run') or 0
    if now - float(last) < INTERVAL_SECONDS:
        return {'skipped': 'interval'}
    cutoff = now - days * 86400
    archive_dir = ctx.state / 'archive'
    removed = {'workers': 0, 'gap_events': 0, 'auto_release': 0, 'sessions': 0, 'quota_markers': 0}

    workers_path = ctx.state / 'workers.json'
    if workers_path.exists():
        ledger = json.loads(workers_path.read_text())
        keep, gone = {}, []
        for key, row in ledger.items():
            started = _age(row.get('started'), now)
            if row.get('terminal') and started is not None and started > days * 86400:
                gone.append({'key': key, **row})
            else:
                keep[key] = row
        if gone:
            _archive(archive_dir / f'workers-{time.strftime("%Y%m%d", time.gmtime(now))}.jsonl.gz', gone)
            _save(workers_path, keep)
            removed['workers'] = len(gone)

    for field in ('release_gap_events', 'auto_release_queued'):
        table = state.get(field) or {}
        gone = []
        for key, value in list(table.items()):
            at = (value or {}).get('at') if isinstance(value, dict) else None
            at = at if at is not None else (value or {}).get('queued_at') if isinstance(value, dict) else None
            age = _age(at, now)
            if age is not None and age > days * 86400:
                gone.append({'key': key, 'value': value})
                del table[key]
        if gone:
            _archive(archive_dir / f'{field}-{time.strftime("%Y%m%d", time.gmtime(now))}.jsonl.gz', gone)
            removed['gap_events' if field == 'release_gap_events' else 'auto_release'] = len(gone)

    live = {w.get('session') for w in (json.loads(workers_path.read_text()).values() if workers_path.exists() else [])}
    if state.get('active'):
        live.add(state['active'].get('id'))
    bindings = state.get('session_credentials') or {}
    for session in list(bindings):
        if session not in live:
            del bindings[session]
            removed['sessions'] += 1

    seen = state.get('quota_seen') or []
    if len(seen) > 500:
        removed['quota_markers'] = len(seen) - 500
        state['quota_seen'] = seen[-500:]

    removed['builds'] = sweep_builds(ctx, state, now)
    removed['sessions_db'] = reclaim_sessions_db(
        state, ctx.cfg.get('opencode_db') if getattr(ctx, 'cfg', None) else None, now=now)

    state['retention_last_run'] = now
    state['retention_last_result'] = {'at': now, 'days': days, **removed}
    return removed

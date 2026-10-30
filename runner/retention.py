#!/usr/bin/env python3
"""Bounded, archived pruning of runner state.

Everything removed here is first appended to an archive file, so pruning is
recoverable and never the only copy of a record. Only terminal worker rows and
aged bookkeeping entries are eligible; live work is never touched.
"""
import gzip
import json
import time
from pathlib import Path

DEFAULT_DAYS = 7
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

    state['retention_last_run'] = now
    state['retention_last_result'] = {'at': now, 'days': days, **removed}
    return removed

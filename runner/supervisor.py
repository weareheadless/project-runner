#!/usr/bin/env python3
"""Zero-model idle loop. GitHub reconciliation + durable OpenCode session receipt.

No inbound webhook or exposed port is needed. Polls every 20s; wakes an isolated
coordinator only on changed project/PR/CI/release state. flock prevents overlap.
"""
import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path

from tick import ROOT, Ctx, board, fields


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def oc(method, path, body=None):
    cmd = ['opencode', 'api', method, path,
           '--header', f'x-opencode-directory:{ROOT}']
    if body is not None:
        cmd += ['--data', json.dumps(body)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError(f'OpenCode {method} {path} failed (see service journal)')
    return json.loads(result.stdout) if result.stdout.strip() else {}


def snapshot(ctx):
    cards = []
    for n in board(ctx):
        c = n.get('content') or {}
        if c.get('repository', {}).get('nameWithOwner') not in ctx.cfg.get('repositories', [ctx.repo]):
            continue
        cards.append({'id': n['id'], 'issue': c.get('number'),
                      'state': c.get('state'), 'title': c.get('title'),
                      'body': c.get('body'), 'fields': fields(n)})
    pulls = []
    runs_all = []
    for repo in ctx.cfg.get('repositories', [ctx.repo]):
      prs = ctx.api(f'https://api.github.com/repos/{repo}/pulls?state=open&per_page=100')
      for p in prs:
        # Do not dispatch reviews for unrelated legacy PRs.
        if not p['head']['ref'].startswith(ctx.cfg.get('branch_prefix', 'codex/')):
            continue
        sha = p['head']['sha']
        checks = ctx.api(f'https://api.github.com/repos/{repo}/commits/{sha}/check-runs?per_page=100')
        status = ctx.api(f'https://api.github.com/repos/{repo}/commits/{sha}/status')
        pulls.append({'repository': repo, 'number': p['number'], 'sha': sha, 'draft': p['draft'],
                      'checks': sorted([(x['name'], x['status'], x['conclusion'])
                                        for x in checks['check_runs']]),
                      'status': status['state']})
      runs = ctx.api(f'https://api.github.com/repos/{repo}/actions/runs?branch=main&per_page=5')
      runs_all.extend([(repo, x['id'], x['head_sha'], x['status'], x['conclusion'])
                       for x in runs['workflow_runs']])
    inbox = ctx.state / 'inbox.json'
    replies = []
    if inbox.exists():
        issue = json.loads(inbox.read_text())
        replies = ctx.api(f'https://api.github.com/repos/{ctx.cfg.get("notification_repo", ctx.repo)}/issues/{issue["number"]}/comments?per_page=100')
    return {'cards': sorted(cards, key=lambda x: x['id']),
            'pulls': sorted(pulls, key=lambda x: (x['repository'], x['number'])),
            'runs': runs_all,
            'owner_replies': [{'id': c['id'], 'body': c['body']} for c in replies
                              if c['user']['login'] == ctx.cfg['project']['owner']],
            'releases': receipts(ctx)}


def receipts(ctx):
    result = []
    for directory in ctx.cfg.get('receipt_roots', []):
        for path in sorted(Path(directory).glob('*/receipt.json'))[-20:]:
            result.append((str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def ui_notify(event):
    endpoint = os.environ.get('OPENCHAMBER_AGENT_TOOL_URL')
    token = os.environ.get('OPENCHAMBER_AGENT_TOOL_TOKEN')
    if not endpoint or not token:
        return {'delivered': False, 'reason': 'OpenChamber connection not configured'}
    base = urllib.parse.urlsplit(endpoint)
    url = urllib.parse.urlunsplit((base.scheme, base.netloc, '/api/notifications/emit', '', ''))
    req = urllib.request.Request(url, method='POST',
        data=json.dumps({'title': 'Project runner — ' + event['kind'],
                         'body': event['message'][:500], 'tag': event['id'][:128],
                         'showWhenFocused': True, 'directory': str(ROOT)}).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)
    except Exception:
        return {'delivered': False, 'reason': 'OpenChamber unreachable or credential expired'}


def notify(ctx, event):
    """Deduplicated durable GitHub inbox + optional desktop notification.

    No model is used. Human test payload must identify the live URL, release SHA,
    steps and expected result. Delivery is persisted only after GitHub accepts it.
    """
    required = {'id', 'kind', 'message'}
    if not required <= event.keys():
        raise ValueError('notification requires id, kind, message')
    if event['kind'] == 'human-test':
        for key in ('url', 'sha', 'steps', 'expected'):
            if not event.get(key):
                raise ValueError(f'human-test requires {key}')
    ledger_path = ctx.state / 'notifications.json'
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    if event['id'] in ledger:
        return
    body = (f"@{ctx.cfg['project']['owner']} **{event['kind']}**\n\n"
            f"{event['message']}\n\n```json\n{json.dumps(event, indent=2)}\n```\n"
            f"<!-- project-runner:{event['id']} -->")
    inbox = ctx.state / 'inbox.json'
    if not inbox.exists():
        issue = ctx.api(f'https://api.github.com/repos/{ctx.cfg.get("notification_repo", ctx.repo)}/issues', 'POST',
                        {'title': 'Project runner — milestones and tests needing you',
                         'body': 'Runner notifications. Subscribe to this issue for GitHub email/mobile alerts. '
                                 'Reply to test requests here; include the notification ID and pass/fail.'})
        save(inbox, {'number': issue['number'], 'url': issue['html_url']})
    issue = json.loads(inbox.read_text())
    comments_url = f"https://api.github.com/repos/{ctx.cfg.get('notification_repo', ctx.repo)}/issues/{issue['number']}/comments"
    # Recover after crash between comment POST and ledger write.
    existing = []
    page = 1
    while True:
        batch = ctx.api(f'{comments_url}?per_page=100&page={page}')
        existing.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if not any(f"<!-- project-runner:{event['id']} -->" in (c.get('body') or '') for c in existing):
        ctx.api(comments_url, 'POST', {'body': body})
    ledger[event['id']] = {'event': event, 'delivered': time.time(), 'inbox': issue['url']}
    ledger[event['id']]['openchamber'] = ui_notify(event)
    save(ledger_path, ledger)
    subprocess.run(['sh', '-c', 'command -v notify-send >/dev/null && notify-send "Project runner" "$1"',
                    'notify', event['message']], capture_output=True, timeout=10)


def process_outbox(ctx):
    for path in sorted((ctx.state / 'outbox').glob('*.json')):
        event = json.loads(path.read_text())
        notify(ctx, event)
        path.rename(path.with_suffix('.sent'))


def step(ctx, state):
    process_outbox(ctx)
    current = snapshot(ctx)
    fingerprint = digest(current)
    save(ctx.state / 'snapshot.json', current)
    active = state.get('active')
    if active:
        info = oc('get', f'/api/session/{active["id"]}')['data']
        if not info.get('time', {}).get('idle'):
            if time.time() - active['started'] > ctx.cfg.get('max_tick_seconds', 5400):
                oc('post', f'/api/session/{active["id"]}/interrupt', {})
                notify(ctx, {'id': f'timeout-{active["id"]}', 'kind': 'blocked',
                             'message': f'Coordinator timed out: {active["id"]}. Work preserved; inspect before retry.'})
                state['paused'] = True
            return state
        state['active'] = None
        state['last_outcome'] = info.get('outcome')
        if info.get('outcome') != 'succeeded':
            notify(ctx, {'id': f'failed-{active["id"]}', 'kind': 'blocked',
                         'message': f'Coordinator failed: {active["id"]}. Automation paused; no automatic retry storm.'})
            state['paused'] = True
        # The changes made by this run are reconciled on the next pass, not lost.
    if state.get('paused') or state.get('fingerprint') == fingerprint:
        return state
    # Validate installed agent before creating sessions.
    oc('get', '/api/agent/sol-tick')
    session = oc('post', '/api/session', {
        'title': 'Project runner — reconcile', 'agent': 'sol-tick',
        'model': ctx.cfg['orchestrator_model'],
        'location': {'directory': str(ROOT)}})['data']
    # Persist before prompt: restart must recover this session, never duplicate it.
    state.update(active={'id': session['id'], 'started': time.time(), 'admitted': False},
                 fingerprint=fingerprint)
    save(ctx.state / 'supervisor.json', state)
    oc('post', f'/api/session/{session["id"]}/prompt', {
        'text': f'Read {ROOT}/rules/TICK.md. Reconcile project {ctx.cfg["name"]}. '
                f'Configuration: {ROOT}/projects/{ctx.cfg["name"]}.json. '
                f'Event snapshot: {ctx.state}/snapshot.json. '
                'Process existing PRs/deployments/test replies first, then ready work. '
                'One bounded pass; leave durable continuation and notification events.'})
    state['active']['admitted'] = True
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', default='site-agent')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--notify', type=Path)
    args = ap.parse_args()
    ctx = Ctx(json.loads((ROOT / 'projects' / f'{args.project}.json').read_text()))
    lock = (ctx.state / 'supervisor.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.notify:
        notify(ctx, json.loads(args.notify.read_text()))
        return
    path = ctx.state / 'supervisor.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    while True:
        try:
            # A crash before prompt acknowledgement is ambiguous. Pause rather
            # than risk submitting duplicate implementation work.
            if state.get('active') and not state['active'].get('admitted'):
                state['paused'] = True
                raise RuntimeError('unacknowledged admission: inspect session inbox before recovery')
            state = step(ctx, state)
            save(path, state)
            save(ctx.state / 'health.json', {'ok': True, 'at': time.time(), 'paused': state.get('paused', False)})
        except Exception as exc:
            # No credential/error response bodies in the journal.
            print(f'supervisor error: {type(exc).__name__}: {str(exc)[:180]}', flush=True)
            save(path, state)
            save(ctx.state / 'health.json', {'ok': False, 'at': time.time(), 'error': type(exc).__name__})
            if args.once:
                raise
        if args.once:
            return
        time.sleep(ctx.cfg.get('poll_seconds', 20))


if __name__ == '__main__':
    main()

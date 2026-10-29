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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tick import ROOT, Ctx, board, fields
from quota import quota_error, suspension


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
    def repo_state(repo):
      pulls = []
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
      runs_all = [(repo, x['id'], x['head_sha'], x['status'], x['conclusion'])
                  for x in runs['workflow_runs']]
      return pulls, runs_all
    pulls, runs_all = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for repo_pulls, repo_runs in pool.map(repo_state, ctx.cfg.get('repositories', [ctx.repo])):
            pulls.extend(repo_pulls)
            runs_all.extend(repo_runs)
    inbox = ctx.state / 'inbox.json'
    replies = []
    if inbox.exists():
        issue = json.loads(inbox.read_text())
        replies = ctx.api(f'https://api.github.com/repos/{ctx.cfg.get("notification_repo", ctx.repo)}/issues/{issue["number"]}/comments?per_page=100')
    worker_path = ctx.state / 'workers.json'
    workers = json.loads(worker_path.read_text()) if worker_path.exists() else {}
    worker_states, changed = [], False
    for run, worker in workers.items():
        if worker.get('terminal'):
            continue
        info = oc('get', f'/api/session/{worker["session"]}')['data']
        terminal = bool(info.get('time', {}).get('idle'))
        if terminal:
            worker['terminal'], worker['outcome'] = True, info.get('outcome')
            changed = True
        worker_states.append({'run': run, 'session': worker['session'],
                              'repository': worker['repository'], 'issue': worker['issue'],
                              'admitted': worker.get('admitted'),
                              'terminal': terminal,
                              'outcome': info.get('outcome')})
    if changed:
        save(worker_path, workers)
    return {'cards': sorted(cards, key=lambda x: x['id']),
            'pulls': sorted(pulls, key=lambda x: (x['repository'], x['number'])),
            'runs': runs_all,
            'workers': worker_states,
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


def quota_failures(ctx, state, exclude=()):
    sessions = [state['active']['id']] if state.get('active') else []
    path = ctx.state / 'workers.json'
    if path.exists():
        sessions.extend(w['session'] for w in json.loads(path.read_text()).values())
    ledger = {}
    if path.exists():
        ledger = json.loads(path.read_text())
    failures, finished = [], False
    for sid in dict.fromkeys(sessions):
        if sid in exclude:
            continue
        info = oc('get', f'/api/session/{sid}')['data']
        messages = oc('get', f'/api/session/{sid}/message?limit=6&order=desc')['data']
        hit = None
        for m in messages:
            if m['type'] == 'user':
                break
            error = m.get('error') or (m.get('retry') or {}).get('error')
            marker = sid + ':' + m['id']
            if error and marker not in state.get('quota_seen', []) and quota_error(error):
                hit = (sid, marker, error)
                break
        if hit:
            failures.append(hit)
        elif info.get('time', {}).get('idle'):
            # Keep the ledger truthful while suspended, so status and capacity
            # are not blocked by sessions that finished before the cooldown.
            for worker in ledger.values():
                if worker['session'] == sid and not worker.get('terminal'):
                    worker['terminal'], worker['outcome'] = True, info.get('outcome')
                    finished = True
    if finished:
        save(path, ledger)
    return failures


def quota_check(ctx, state):
    """Return True while suspended/recovering. No model calls before reset."""
    now = time.time()
    hold = state.get('quota')
    if hold:
        if now < hold['until']:
            # Catch workers that hit the same exhausted account a few seconds
            # later; don't leave their internal provider retries running.
            for sid, marker, error in quota_failures(ctx, state, hold['sessions']):
                info = oc('get', f'/api/session/{sid}')['data']
                if not info.get('time', {}).get('idle'):
                    oc('post', f'/api/session/{sid}/interrupt', {})
                hold['sessions'].append(sid)
                hold['markers'].append(marker)
                hold['until'] = max(hold['until'], suspension(error, now, hold['attempt'])['until'])
            return True
        # Persist admission intent before resuming. Ambiguous delivery must not
        # cause repeated prompts after a process restart.
        if hold.get('resuming'):
            state['paused'] = True
            return True
        hold['resuming'] = True
        save(ctx.state / 'supervisor.json', state)
        for sid in hold['sessions']:
            info = oc('get', f'/api/session/{sid}')['data']
            if not info.get('time', {}).get('idle'):
                continue  # A provider/internal retry already resumed this run.
            oc('post', f'/api/session/{sid}/prompt', {'text':
                'Provider quota cooldown has elapsed. Resume your existing assigned run from durable '
                'state. Preserve its claim, worktree, branch and completed steps; inspect current '
                'state before retrying writes. Do not start a duplicate task or repeat a completed merge/deployment.'})
        if state.get('active') and state['active']['id'] in hold['sessions']:
            state['active']['started'] = now
        state.setdefault('quota_seen', []).extend(hold['markers'])
        state['quota_recovering'] = hold['sessions']
        state.pop('quota')
        state.pop('fingerprint', None)
        save(ctx.state / 'supervisor.json', state)
        return True
    if state.get('paused'):
        return False  # Quota recovery must never clear an unrelated safety pause.
    # An episode ends when the resumed sessions have stopped running, whatever
    # the outcome. A later, different failure is not this episode's problem, and
    # leaving the flag set would silence the next quota alert.
    recovering = state.get('quota_recovering', [])
    if recovering and all(oc('get', f'/api/session/{sid}')['data'].get('time', {}).get('idle')
                          for sid in recovering):
        state.pop('quota_recovering', None)
        state.pop('quota_attempts', None)
    failures = quota_failures(ctx, state)
    if not failures:
        return False
    attempt = state.get('quota_attempts', 0) + 1
    timers = [suspension(error, now, attempt) for _, _, error in failures]
    hold = max(timers, key=lambda x: x['until'])
    hold.update(sessions=[sid for sid, _, _ in failures], markers=[m for _, m, _ in failures])
    # Stop an internal provider retry loop before parking it; only affected
    # sessions are interrupted, never unrelated workers that are making progress.
    for sid in hold['sessions']:
        info = oc('get', f'/api/session/{sid}')['data']
        if not info.get('time', {}).get('idle'):
            oc('post', f'/api/session/{sid}/interrupt', {})
    state['quota'] = hold
    state['quota_attempts'] = attempt
    save(ctx.state / 'supervisor.json', state)
    # One durable event per exhaustion episode, not one alert per poll/retry.
    if state.get('quota_notified') not in hold['markers']:
        event = {'id': f'quota-{hold["markers"][0]}', 'kind': 'quota-paused',
                 'message': 'Codex/provider quota exhausted. Work and claims are preserved. '
                            + ('Will retry after the provider reset.' if hold['explicit_reset']
                               else 'No reset time supplied; retrying after 5 minutes with backoff capped at one hour.'),
                 'resume_at': hold['until']}
        outbox = ctx.state / 'outbox'
        outbox.mkdir(exist_ok=True)
        save(outbox / (event['id'] + '.json'), event)
        ui_notify(event)
        state['quota_notified'] = hold['markers'][0]
    return True


def step(ctx, state):
    process_outbox(ctx)
    if quota_check(ctx, state):
        return state
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
    errors = 0
    print(f'supervisor started for {ctx.cfg["name"]}; polling {ctx.cfg.get("poll_seconds", 20)}s', flush=True)
    while True:
        try:
            # A crash before prompt acknowledgement is ambiguous. Pause rather
            # than risk submitting duplicate implementation work.
            if state.get('active') and not state['active'].get('admitted'):
                state['paused'] = True
                raise RuntimeError('unacknowledged admission: inspect session inbox before recovery')
            state = step(ctx, state)
            errors = 0
            save(path, state)
            save(ctx.state / 'health.json', {'ok': True, 'at': time.time(), 'paused': state.get('paused', False)})
        except Exception as exc:
            # No credential/error response bodies in the journal.
            print(f'supervisor error: {type(exc).__name__}: {str(exc)[:180]}', flush=True)
            save(path, state)
            save(ctx.state / 'health.json', {'ok': False, 'at': time.time(), 'error': type(exc).__name__})
            errors += 1
            if errors == 3:
                event = {'id': f'supervisor-error-{int(time.time() // 3600)}', 'kind': 'blocked',
                         'message': 'Project runner cannot reconcile GitHub/OpenCode after three attempts. Check service health; no new work has been dispatched.'}
                ui_notify(event)
                try:
                    notify(ctx, event)
                except Exception:
                    print('durable notification unavailable; OpenChamber delivery attempted', flush=True)
            if args.once:
                raise
        if args.once:
            return
        time.sleep(ctx.cfg.get('poll_seconds', 20))


if __name__ == '__main__':
    main()

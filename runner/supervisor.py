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
import credentials
import availability
import test_results
import release_gap
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
    deliveries_path = ctx.state / 'deliveries.json'
    deliveries = json.loads(deliveries_path.read_text()) if deliveries_path.exists() else {}
    reviews = []
    for key, delivery in deliveries.items():
        if delivery.get('state') == 'reviewing':
            info = oc('get', f'/api/session/{delivery["reviewer_session"]}')['data']
            reviews.append({'delivery': key, 'session': delivery['reviewer_session'],
                            'outcome': info.get('outcome'), 'idle': bool(info['time'].get('idle'))})
    return {'cards': sorted(cards, key=lambda x: x['id']),
            'deliveries': deliveries, 'delivery_reviews': reviews,
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


def terminal_error(session_id):
    """Last provider error for a stopped session, if it is the newest event."""
    try:
        payload = oc('get', f'/api/session/{session_id}/message?limit=4&order=desc')
    except Exception:
        return None
    messages = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict) or message.get('type') == 'user':
            return None
        error = message.get('error') or (message.get('retry') or {}).get('error')
        if error:
            return error
    return None


def provider_failures(ctx, state, exclude=()):
    """Return (kind, sid, marker, error) for quota or access-refusal failures."""
    return [(kind, sid, marker, error)
            for kind, sid, marker, error in _scan_failures(ctx, state, exclude)]


def quota_failures(ctx, state, exclude=()):
    return [(sid, marker, error) for kind, sid, marker, error in
            _scan_failures(ctx, state, exclude) if kind == 'quota']


def _scan_failures(ctx, state, exclude=()):
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
            if not error or marker in state.get('quota_seen', []):
                continue
            if quota_error(error):
                hit = ('quota', sid, marker, error)
                break
            if credentials.auth_failure(error):
                hit = ('auth', sid, marker, error)
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


def prefer_credential(ctx, state, hold, now):
    """Use a spare OpenAI account instead of waiting; wait only when all are dry."""
    if not ctx.cfg.get('credential_failover', True):
        return
    if hold.get('switched') or hold.get('resuming'):
        return
    listed = credentials.accounts(oc)
    if len(listed) < 2:
        return
    target = credentials.choose(state, listed, now)
    if not target:
        # Every account is exhausted: wait for whichever window resets first.
        earliest = credentials.earliest_reset(state, listed, now)
        if earliest:
            hold['until'] = min(hold['until'], earliest)
        return
    # A usable credential exists: use it now. When it is already active (manual
    # switch or a legacy hold), only shorten the wait; do not re-activate.
    if not target['active']:
        credentials.activate(oc, target['id'])
        hold['credential_changed'] = True
        event = {'id': f'quota-switch-{hold["markers"][0]}-{target["id"]}',
                 'kind': 'quota-switched',
                 'message': f'OpenAI quota exhausted on one account; switched to credential '
                            f'{target["label"]} (account {target["account"]}). Resuming preserved work.'}
        outbox = ctx.state / 'outbox'
        outbox.mkdir(exist_ok=True)
        save(outbox / (event['id'] + '.json'), event)
        ui_notify(event)
    hold['switched'] = target['id']
    hold['until'] = min(hold['until'], now + ctx.cfg.get('credential_switch_settle', 15))
    save(ctx.state / 'supervisor.json', state)


def handle_auth(ctx, state, failures, now):
    """Rotate away from a refused credential; pause only if none can work."""
    state.setdefault('quota_seen', []).extend(marker for _, _, marker, _ in failures)
    listed = credentials.accounts(oc) if ctx.cfg.get('credential_failover', True) else []
    current = next((c for c in listed if c['active']), None)
    until = now + ctx.cfg.get('credential_broken_seconds', 3600)
    for _, sid, marker, _ in failures:
        credential_id = state.get('session_credentials', {}).get(sid)
        if credential_id:
            credentials.mark_broken(state, credential_id, until, marker)
    target = credentials.choose(state, listed, now) if len(listed) > 1 else None
    if target:
        changed = False
        if not target['active']:
            credentials.activate(oc, target['id'])
            changed = True
        hold = {'until': now + ctx.cfg.get('credential_switch_settle', 15),
                'credential_changed': changed,
                'attempt': state.get('quota_attempts', 0) + 1, 'explicit_reset': False,
                'sessions': [sid for _, sid, _, _ in failures],
                'markers': [m for _, _, m, _ in failures],
                'switched': target['id'], 'reason': 'auth'}
        state['quota'] = hold
        save(ctx.state / 'supervisor.json', state)
        return True
    # Nothing usable: a 401 does not heal by waiting, so stop and ask for a human.
    state['paused'] = True
    state['paused_reason'] = 'credential refused by provider access policy'
    active = next((c for c in listed if c['active']), None)
    if active:
        state['active_credential_id'] = active['id']
    event = {'id': 'auth-blocked-' + failures[0][2], 'kind': 'blocked',
             'message': 'OpenAI refused the active credential (401 access enforcement) and no other '
                        'configured account can be used. Log in with an authorized OpenAI account; the '
                        'runner will resume automatically when it detects the new usable credential.'}
    outbox = ctx.state / 'outbox'
    outbox.mkdir(exist_ok=True)
    save(outbox / (event['id'] + '.json'), event)
    ui_notify(event)
    save(ctx.state / 'supervisor.json', state)
    return True


def observe_active_credential(ctx, state, now):
    """Invalidate stale change-detection state after a usable account switch."""
    listed = credentials.accounts(oc)
    active = next((c for c in listed if c['active']), None)
    usable = credentials.choose(state, listed, now)
    if not active or not usable or usable['id'] != active['id']:
        return False
    previous = state.get('active_credential_id')
    changed = previous is not None and previous != active['id']
    # One-time migration for pre-identity state: the old failed coordinator
    # fingerprint otherwise suppresses recovery forever after an operator clears
    # the pause. Do not repeat this on subsequent polls.
    legacy_failed = (previous is None and not state.get('paused') and bool(state.get('fingerprint'))
                    and state.get('last_outcome') == 'failed'
                    and not state.get('active') and not state.get('quota'))
    state['active_credential_id'] = active['id']
    if legacy_failed and not state.get('legacy_credential_recovery_initialized'):
        state['legacy_credential_recovery_initialized'] = True
        changed = True
    if changed:
        state.pop('fingerprint', None)
    if state.get('paused') and state.get('paused_reason') == 'credential refused by provider access policy':
        if changed:
            state.pop('paused', None)
            state.pop('paused_reason', None)
        else:
            return False
    return changed


def quota_check(ctx, state):
    """Return True while suspended/recovering. No model calls before reset."""
    now = time.time()
    if not state.get('paused'):
        auth = [f for f in _scan_failures(ctx, state) if f[0] == 'auth']
        if auth:
            return handle_auth(ctx, state, auth, now)
    hold = state.get('quota')
    if hold:
        if availability.check(ctx, state, oc, save, now):
            return True
        prefer_credential(ctx, state, hold, now)
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
        listed = credentials.accounts(oc)
        target = credentials.choose(state, listed, now)
        if target and not target['active']:
            credentials.activate(oc, target['id'])
            hold['credential_changed'] = True
        if hold.get('credential_changed'):
            # A session opened under one account is rejected when resumed under
            # another (401 rejected_by_access_enforcement). Close it out and let
            # the next pass start fresh from the board and continuation file.
            state['active'] = None
            state['resume_after_switch'] = True
            worker_path = ctx.state / 'workers.json'
            if worker_path.exists():
                ledger = json.loads(worker_path.read_text())
                for worker in ledger.values():
                    if worker['session'] in hold['sessions']:
                        worker['terminal'], worker['outcome'] = True, 'interrupted'
                save(worker_path, ledger)
        else:
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
    listed = credentials.accounts(oc) if ctx.cfg.get('credential_failover', True) else []
    current = next((c for c in listed if c['active']), None)
    bindings = state.get('session_credentials', {})
    worker_path = ctx.state / 'workers.json'
    if worker_path.exists():
        bindings = dict(bindings)
        bindings.update({w['session']: w['credential_id']
                         for w in json.loads(worker_path.read_text()).values() if w.get('credential_id')})
    for (sid, marker, error), timer in zip(failures, timers):
        credential_id = bindings.get(sid)
        if credential_id:
            credentials.mark_exhausted(state, credential_id, timer['until'], marker)
            hold['exhausted_credential'] = credential_id
    # Never infer that the currently active credential produced an old error.
    # Stop an internal provider retry loop before parking it; only affected
    # sessions are interrupted, never unrelated workers that are making progress.
    for sid in hold['sessions']:
        info = oc('get', f'/api/session/{sid}')['data']
        if not info.get('time', {}).get('idle'):
            oc('post', f'/api/session/{sid}/interrupt', {})
    state['quota'] = hold
    state['quota_attempts'] = attempt
    prefer_credential(ctx, state, hold, now)
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


def check_pr_closures(ctx, state, current):
    """Alarm when an owned PR disappears without being merged."""
    previous = {tuple(item) for item in state.get('open_prs', [])}
    open_now = {(pull['repository'], pull['number']) for pull in current['pulls']}
    events = []
    for repo, number in sorted(previous - open_now):
        try:
            pull = ctx.api(f'https://api.github.com/repos/{repo}/pulls/{number}')
        except Exception:
            continue
        if pull.get('state') == 'closed' and not pull.get('merged_at'):
            events.append({'id': f'pr-closed-{repo.replace("/", "-")}-{number}',
                           'kind': 'blocked',
                           'message': f'PR #{number} in {repo} was closed without merging '
                                      f'({pull.get("title", "")[:60]}). Reopen it or repair the same '
                                      f'branch; do not start a new run for that issue.'})
    state['open_prs'] = sorted([list(item) for item in open_now])
    return events


def step(ctx, state):
    # GitHub reporting is zero-model work: retain it even during quota cooldowns.
    test_reports = test_results.process_pending(ctx)
    if test_reports['processed'] or test_reports['errors']:
        save(ctx.state / 'test-report-health.json', test_reports)
    process_outbox(ctx)
    release_gap.step(ctx, state, time.time())
    if quota_check(ctx, state):
        return state
    current = snapshot(ctx)
    for event in check_pr_closures(ctx, state, current):
        outbox = ctx.state / 'outbox'
        outbox.mkdir(exist_ok=True)
        save(outbox / (event['id'] + '.json'), event)
        ui_notify(event)
    # Management policy changes are actionable events too. Without this, a
    # completed pass can remain asleep on its old policy until GitHub changes.
    policy_paths = [ROOT / 'rules' / 'TICK.md',
                    ROOT / 'projects' / f'{ctx.cfg["name"]}.json']
    if ctx.cfg.get('goal_file'):
        policy_paths.append(ROOT / ctx.cfg['goal_file'])
    policy = {str(p.relative_to(ROOT)): digest(p.read_text())
              for p in policy_paths if p.exists()}
    fingerprint = digest({'project': current, 'policy': policy})
    save(ctx.state / 'snapshot.json', current)
    if state.get('paused') and state.get('paused_reason') != 'credential refused by provider access policy':
        return state
    observe_active_credential(ctx, state, time.time())
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
        error = terminal_error(active['id'])
        recoverable = bool(error) and (quota_error(error) or credentials.auth_failure(error))
        if info.get('outcome') != 'succeeded' and recoverable:
            return state  # provider failure: rotation or cooldown owns it
        if info.get('outcome') != 'succeeded':
            # A session may outlive the credential it was admitted with. If an
            # operator replaced that account and the replacement is usable,
            # leave recovery to observe_active_credential below; do not turn an
            # otherwise unexplained terminal outcome into a safety pause.
            bound = state.get('session_credentials', {}).get(active['id'])
            replaced_with_usable = False
            if bound:
                listed = credentials.accounts(oc)
                replaced_with_usable = (not any(account.get('id') == bound for account in listed)
                                        and bool(credentials.choose(state, listed, time.time())))
            if replaced_with_usable:
                # Admit the replacement pass even when the board snapshot is
                # unchanged; otherwise the old fingerprint suppresses recovery.
                state.pop('fingerprint', None)
            else:
                notify(ctx, {'id': f'failed-{active["id"]}', 'kind': 'blocked',
                             'message': f'Coordinator failed: {active["id"]}. Automation paused; no automatic retry storm.'})
                state['paused'] = True
        # The changes made by this run are reconciled on the next pass, not lost.
    if state.get('paused') or state.get('fingerprint') == fingerprint:
        return state
    # Validate installed agent before creating sessions.
    oc('get', '/api/agent/sol-tick')
    credential_id = state.get('active_credential_id') if ctx.cfg.get('credential_failover') else None
    session = oc('post', '/api/session', {
        'title': 'Project runner — reconcile', 'agent': 'sol-tick',
        'model': ctx.cfg['orchestrator_model'],
        'location': {'directory': str(ROOT)}})['data']
    # Persist before prompt: restart must recover this session, never duplicate it.
    state.update(active={'id': session['id'], 'started': time.time(), 'admitted': False},
                  fingerprint=fingerprint)
    if credential_id:
        state.setdefault('session_credentials', {})[session['id']] = credential_id
    save(ctx.state / 'supervisor.json', state)
    oc('post', f'/api/session/{session["id"]}/prompt', {
        'text': f'Read {ROOT}/rules/TICK.md. Reconcile project {ctx.cfg["name"]}. '
                f'Project mandate: {ROOT / ctx.cfg.get("goal_file", "rules/TICK.md")}. '
                f'Configuration: {ROOT}/projects/{ctx.cfg["name"]}.json. '
                f'Event snapshot: {ctx.state}/snapshot.json. '
                'Recover the management handoff and choose actions that advance the project goal. '
                + ('The previous session was closed by an OpenAI account switch; its branch and claim '
                   'state is authoritative, so inspect before re-dispatching anything. '
                   if state.pop('resume_after_switch', False) else '')
                + 'One bounded pass; leave durable continuation and notification events.'})
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

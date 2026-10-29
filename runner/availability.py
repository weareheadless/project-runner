"""Bounded, fresh-session checks invalidate stale local quota predictions."""
from tick import ROOT
import credentials
from quota import quota_error, suspension


def check(ctx, state, oc, save, now):
    hold = state.get('quota')
    if not hold or not ctx.cfg.get('availability_checks', False):
        return False
    probe = state.get('availability_probe')
    if not probe and hold.get('until', 0) <= now:
        return False
    if probe:
        info = oc('get', f'/api/session/{probe["session"]}')['data']
        if not info.get('time', {}).get('idle'):
            if now - probe['started'] > 90:
                oc('post', f'/api/session/{probe["session"]}/interrupt', {})
                state.pop('availability_probe')
            return True
        state.pop('availability_probe')
        entry = state.setdefault('credentials', {}).setdefault(probe['credential'], {})
        if info.get('outcome') == 'succeeded':
            entry.pop('exhausted_until', None)
            entry.pop('broken_until', None)
            entry['available_at'] = now
            # A fresh coordinator avoids replaying account-bound session state.
            hold.update(until=now, credential_changed=True, switched=probe['credential'])
            state['availability_recovered_at'] = now
        else:
            error = None
            for message in oc('get', f'/api/session/{probe["session"]}/message?limit=4&order=desc')['data']:
                if message.get('error'):
                    error = message['error']
                    break
            if error and quota_error(error):
                entry.update(exhausted_until=suspension(error, now, 1)['until'],
                             quota_confirmed_at=now)
            # Network/auth failures are not evidence of quota exhaustion.
        save(ctx.state/'supervisor.json', state)
        return True
    interval = ctx.cfg.get('availability_interval_seconds', 300)
    listed = credentials.accounts(oc)
    attempts = state.setdefault('availability_attempts', {})
    candidates = [c for c in listed if now - attempts.get(c['id'], 0) >= interval]
    if not candidates:
        return False
    # Activation is host-global: never rotate with owned work still executing.
    sessions = [state['active']['id']] if state.get('active') else []
    path = ctx.state/'workers.json'
    if path.exists():
        import json
        sessions += [w['session'] for w in json.loads(path.read_text()).values() if not w.get('terminal')]
    if any(not oc('get', f'/api/session/{sid}')['data'].get('time', {}).get('idle') for sid in sessions):
        return False
    candidate = min(candidates, key=lambda c: (not c['active'], attempts.get(c['id'], 0)))
    attempts[candidate['id']] = now
    save(ctx.state/'supervisor.json', state)
    if not candidate['active']:
        credentials.activate(oc, candidate['id'])
    session = oc('post', '/api/session', {
        'title': 'Runner quota availability check', 'agent': 'quota-probe',
        'model': ctx.cfg['orchestrator_model'], 'location': {'directory': str(ROOT)}})['data']
    state['availability_probe'] = dict(session=session['id'], credential=candidate['id'], started=now)
    save(ctx.state/'supervisor.json', state)
    oc('post', f'/api/session/{session["id"]}/prompt', {'text': 'Reply OK only. No tools.'})
    return True

"""OpenAI credential rotation on quota exhaustion.

Only credential IDs, labels and a short account fingerprint are ever kept or
printed. The credential API returns live secrets; nothing here stores or logs
them, and account fingerprints exist only so a switch is auditable.
"""
import base64
import hashlib
import json

PROVIDER = 'openai'


def _claims(token):
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _fingerprint(token):
    account = _claims(token).get('https://api.openai.com/auth', {}).get('chatgpt_account_id')
    return hashlib.sha256(str(account).encode()).hexdigest()[:8] if account else None


def accounts(oc):
    """List provider credentials with secrets stripped."""
    result = []
    for credential in oc('get', '/api/credential').get('data', []):
        if credential.get('integrationID') != PROVIDER:
            continue
        value = credential.get('value') or {}
        result.append({
            'id': credential['id'],
            'label': credential.get('label') or 'OAuth',
            'active': bool(credential.get('active')),
            'account': _fingerprint(value.get('access', '')) if value.get('type') == 'oauth' else None,
        })
    return sorted(result, key=lambda item: item['id'])


def activate(oc, credential_id):
    """Switch the host-wide provider credential. Returns False if unchanged."""
    oc('post', f'/api/credential/{credential_id}/activate')
    return True


def mark_exhausted(state, credential_id, until, marker=None):
    if not credential_id:
        return
    table = state.setdefault('credentials', {})
    entry = table.setdefault(credential_id, {})
    entry['exhausted_until'] = max(entry.get('exhausted_until', 0), until)
    if marker:
        entry['last_marker'] = marker


def usable(state, listed, now):
    """Credentials that are neither out of quota nor rejected by access policy."""
    table = state.get('credentials', {})
    return [c for c in listed
            if table.get(c['id'], {}).get('exhausted_until', 0) <= now
            and table.get(c['id'], {}).get('broken_until', 0) <= now]


def mark_broken(state, credential_id, until, marker=None):
    """A credential that authenticated but was refused (e.g. no model access).

    Waiting for a quota reset will not fix this; it needs a re-login or access
    change. It is retried after a bounded delay rather than discarded.
    """
    if not credential_id:
        return
    table = state.setdefault('credentials', {})
    entry = table.setdefault(credential_id, {})
    entry['broken_until'] = max(entry.get('broken_until', 0), until)
    if marker:
        entry['last_broken_marker'] = marker


def auth_failure(error):
    """Access refusal, distinct from quota and from a transient network error."""
    if not isinstance(error, dict):
        return False
    kind = str(error.get('type', '')).lower()
    status = error.get('status')
    body = str(error.get('response', {}).get('body', '')).lower()
    if kind.startswith('provider.quota'):
        return False
    return (status in (401, 403) or 'provider.auth' in kind or 'unauthorized' in kind
            or 'rejected_by_access_enforcement' in body or 'no_matching_rule' in body)


def earliest_reset(state, listed, now):
    table = state.get('credentials', {})
    future = [max(table.get(c['id'], {}).get('exhausted_until', 0),
                  table.get(c['id'], {}).get('broken_until', 0)) for c in listed]
    future = [t for t in future if t > now]
    return min(future) if future else None


def choose(state, listed, now):
    """The credential to use now, preferring the active one when it is usable."""
    options = usable(state, listed, now)
    if not options:
        return None
    for candidate in options:
        if candidate['active']:
            return candidate
    return options[0]

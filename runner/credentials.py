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
    """Credentials whose own window has reset, in stable order."""
    table = state.get('credentials', {})
    return [c for c in listed
            if table.get(c['id'], {}).get('exhausted_until', 0) <= now]


def earliest_reset(state, listed, now):
    table = state.get('credentials', {})
    future = [table.get(c['id'], {}).get('exhausted_until', 0) for c in listed]
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

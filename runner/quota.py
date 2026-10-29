"""Recognize provider quota exhaustion, not every HTTP 429 or task budget gate."""
import json
import re
from datetime import datetime, timezone


def quota_error(error):
    text = ' '.join(str(error.get(k, '')) for k in ('type', 'message'))
    body = error.get('response', {}).get('body', '')
    text = (text + ' ' + body).lower()
    return bool(re.search(
        r'usage[_ -]limit[_ -](?:reached|exceeded)|insufficient_quota|'
        r'quota[_ -](?:exceeded|exhausted)|billing_hard_limit|'
        r'(?:codex|subscription|weekly|monthly|daily).{0,60}(?:limit reached|quota exhausted)|'
        r"you(?: have|'ve) (?:hit|reached).{0,30}(?:usage|plan) limit", text))


def reset_time(error, now):
    """Only trust explicit reset fields, Retry-After, or labelled timestamps."""
    candidates = []

    def timestamp(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value / 1000 if value > 100_000_000_000 else value
        if isinstance(value, str):
            if value.isdigit():
                return timestamp(int(value))
            try:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if dt.tzinfo is not None:
                    return dt.timestamp()
            except ValueError:
                pass
        return None

    def visit(obj):
        if not isinstance(obj, dict):
            return
        for key, value in obj.items():
            if key in ('reset_at', 'resets_at', 'resetAt', 'resetsAt', 'reset_time'):
                t = timestamp(value)
                if t is not None and t > now:
                    candidates.append(t)
            elif key in ('retryAfterMs', 'retry_after_ms') and isinstance(value, (int, float)) and value > 0:
                candidates.append(now + value / 1000)
            elif key in ('retry_after_seconds', 'retry_after') and isinstance(value, (int, float)) and value > 0:
                candidates.append(now + value)
            elif isinstance(value, dict):
                visit(value)

    visit(error)
    try:
        visit(json.loads(error.get('response', {}).get('body', '')))
    except (ValueError, TypeError):
        pass
    message = error.get('message', '')
    for value in re.findall(r'(?:resets? (?:at|on)|reset_at[=:])\s*(\d{4}-\d\d-\d\d[T ][\d:.]+(?:Z|[+-]\d\d:\d\d))', message, flags=re.I):
        t = timestamp(value)
        if t is not None and t > now:
            candidates.append(t)
    # Use the later signal if the provider reports both a rate reset and a
    # subscription reset; never retry earlier than an explicit exhausted window.
    return max(candidates) if candidates else None


def suspension(error, now, attempt):
    reset = reset_time(error, now)
    delay = min(3600, 300 * 2 ** min(max(attempt - 1, 0), 4))
    return {'until': (reset + 15) if reset else now + delay,
            'attempt': attempt, 'explicit_reset': reset is not None}

#!/usr/bin/env python3
"""Durable independent worker sessions, so SOL need not wait for a whole batch."""
import argparse
import fcntl
import json
import time

from tick import ROOT, Ctx
from supervisor import oc, save


def start(ctx, plan, brief, effort):
    if plan['repository'] not in ctx.cfg.get('repositories', [ctx.repo]):
        raise ValueError('worker repository not authorized')
    for key in ('agentRun', 'branch', 'baseline', 'worktree', 'scope', 'issueNumber'):
        if not plan.get(key):
            raise ValueError(f'missing worker plan {key}')
    if effort not in ('low', 'medium', 'high'):
        raise ValueError('unsupported effort')
    path = ctx.state / 'workers.json'
    ledger = json.loads(path.read_text()) if path.exists() else {}
    # OS lock protects process races. Caller holds it throughout this function.
    for record in ledger.values():
        info = oc('get', f'/api/session/{record["session"]}')['data']
        record['terminal'] = bool(info.get('time', {}).get('idle'))
    same_issue = [w for w in ledger.values() if not w.get('terminal') and
                  w['repository'] == plan['repository'] and w['issue'] == plan['issueNumber']]
    if same_issue or plan['agentRun'] in ledger:
        raise ValueError('issue or run already dispatched; inspect existing worker')
    if sum(not w.get('terminal') for w in ledger.values()) >= ctx.cfg['max_dispatch']:
        raise ValueError('worker capacity full')
    agent = {'low': 'luna-worker', 'medium': 'luna-worker-med', 'high': 'luna-worker-high'}[effort]
    # Resolve agent before creating a session, so a missing installation is visible.
    oc('get', f'/api/agent/{agent}')
    session = oc('post', '/api/session', {'title': f'{plan["agentRun"]} — {effort}',
        'agent': agent, 'model': {'providerID': 'openai', 'id': 'gpt-6-luna', 'variant': effort},
        'location': {'directory': str(ROOT)}})['data']
    record = dict(session=session['id'], repository=plan['repository'], issue=plan['issueNumber'],
                  effort=effort, terminal=False, started=time.time(), admitted=False)
    ledger[plan['agentRun']] = record
    save(path, ledger)
    oc('post', f'/api/session/{session["id"]}/prompt', {'text':
        'SOL assigned this independent worker run. No human is waiting in this session. '
        'Do not merge or deploy. If blocked, record the exact blocker and stop; never wait on a question. '
        'Follow the supplied plan and work only in its isolated checkout.\n\n'
        + json.dumps(plan, indent=2) + '\n\n' + brief})
    record['admitted'] = True
    save(path, ledger)
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--plan', required=True)
    ap.add_argument('--brief', required=True)
    ap.add_argument('--effort', default='low', choices=['low', 'medium', 'high'])
    args = ap.parse_args()
    ctx = Ctx(json.loads((ROOT / 'projects' / f'{args.project}.json').read_text()))
    with (ctx.state / 'workers.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(json.dumps(start(ctx, json.load(open(args.plan)), open(args.brief).read(), args.effort)))


if __name__ == '__main__':
    main()

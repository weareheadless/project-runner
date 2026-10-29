#!/usr/bin/env python3
"""Durable outcome ownership and independent acceptance-review admission.

Proposals never complete/block a delivery directly. A separate fresh reviewer
checks evidence, then an exact proposal digest gates the state transition.
"""
import argparse
import fcntl
import json
import time
from pathlib import Path
from tick import ROOT, Ctx
from supervisor import oc, save, digest

STATES = {'investigating', 'implementing', 'merged', 'published', 'deploying',
          'deployed', 'awaiting-human', 'verified', 'blocked'}


def validate(proposal):
    if proposal.get('state') not in STATES:
        raise ValueError('invalid delivery state')
    for field in ('repository', 'issue', 'request', 'owner_session', 'next_action'):
        if not proposal.get(field):
            raise ValueError(f'missing {field}')
    if proposal['state'] == 'verified':
        for field in ('source_sha', 'live_url', 'deployment_receipt', 'acceptance_evidence'):
            if not proposal.get(field):
                raise ValueError(f'verified requires {field}')
    if proposal['state'] == 'blocked':
        for field in ('investigation', 'unavailable_resource', 'owner_action', 'alternatives'):
            if not proposal.get(field):
                raise ValueError(f'blocker requires {field}')
        if not isinstance(proposal['investigation'], list) or not all(
            isinstance(x, dict) and x.get('source') and x.get('result') and x.get('evidence')
            for x in proposal['investigation']):
            raise ValueError('investigation must record sources, results and evidence')


def accepted(proposal, verdict, reviewer_id, session):
    validate(proposal)
    if reviewer_id == proposal['owner_session']:
        raise ValueError('coordinator cannot approve its own delivery')
    if session.get('agent') != 'delivery-reviewer' or session.get('outcome') != 'succeeded':
        raise ValueError('independent reviewer not successfully finished')
    if verdict.get('proposal_hash') != digest(proposal) or verdict.get('reviewer_session') != reviewer_id:
        raise ValueError('stale or unbound verdict')
    if verdict.get('accepted') is not True or not verdict.get('evidence_checked'):
        raise ValueError('review rejected or lacks independent evidence')


def apply(ctx, key, record):
    proposal = record['proposal']
    path = ctx.state / 'delivery-reviews' / f'{record["reviewer_session"]}.json'
    verdict = json.loads(path.read_text())
    info = oc('get', f'/api/session/{record["reviewer_session"]}')['data']
    accepted(proposal, verdict, record['reviewer_session'], info)
    record.update(state=proposal['state'], reviewed_at=time.time(), verdict=verdict)
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--proposal', type=Path)
    ap.add_argument('--apply', help='repository#issue key')
    ap.add_argument('--list', action='store_true')
    args = ap.parse_args()
    ctx = Ctx(json.loads((ROOT/'projects'/f'{args.project}.json').read_text()))
    with (ctx.state/'deliveries.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = ctx.state/'deliveries.json'
        ledger = json.loads(path.read_text()) if path.exists() else {}
        if args.list:
            print(json.dumps(ledger, indent=2))
            return
        if args.apply:
            ledger[args.apply] = apply(ctx, args.apply, ledger[args.apply])
        elif args.proposal:
            p = json.loads(args.proposal.read_text())
            validate(p)
            if p['repository'] not in ctx.cfg.get('repositories', [ctx.repo]):
                raise ValueError('unauthorized repository')
            key = f'{p["repository"]}#{p["issue"]}'
            prior = ledger.get(key, {})
            if p['state'] not in ('verified', 'blocked'):
                ledger[key] = dict(state=p['state'], proposal=p, updated_at=time.time(),
                                   history=prior.get('history', []) + [prior] if prior else [])
            else:
                if prior.get('state') == 'reviewing':
                    raise ValueError('review already pending; reconcile it first')
                session = oc('post', '/api/session', {
                    'title': f'Independent outcome review — {key}', 'agent': 'delivery-reviewer',
                    'model': ctx.cfg['orchestrator_model'], 'location': {'directory': str(ROOT)}})['data']
                folder = ctx.state/'delivery-reviews'
                folder.mkdir(exist_ok=True)
                proposal_path = folder/f'{session["id"]}-proposal.json'
                save(proposal_path, p)
                ledger[key] = dict(state='reviewing', proposal=p, reviewer_session=session['id'],
                                   prior_state=prior.get('state'), admitted=False)
                save(path, ledger)
                oc('post', f'/api/session/{session["id"]}/prompt', {'text':
                    f'Independently review {proposal_path}. Proposal hash {digest(p)}. '
                    f'Your session ID is {session["id"]}. Read original issue and evidence directly. '
                    f'Write verdict to {folder/session["id"]}.json with proposal_hash, '
                    'reviewer_session, accepted (boolean), evidence_checked (specific evidence), '
                    'gaps and recommended_next_action. Do not mutate product, board or claims.'})
                ledger[key]['admitted'] = True
        else:
            ap.error('choose --list, --proposal or --apply')
        save(path, ledger)
        print(json.dumps(ledger.get(args.apply or key), indent=2))


if __name__ == '__main__':
    main()

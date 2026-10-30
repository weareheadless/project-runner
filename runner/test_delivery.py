import unittest
from delivery import validate, accepted, transition_record
import json
from supervisor import digest


class DeliveryTests(unittest.TestCase):
    def test_repeated_transitions_have_linear_history_and_keep_evidence(self):
        record = {}
        for index in range(40):
            proposal = dict(state='implementing', next_action=f'phase {index}', evidence=f'proof-{index}')
            record = transition_record(proposal, record, updated_at=index)
        self.assertEqual(len(record['history']), 39)
        self.assertEqual([x['updated_at'] for x in record['history']], list(range(39)))
        self.assertEqual([x['proposal']['evidence'] for x in record['history']],
                         [f'proof-{i}' for i in range(39)])
        self.assertTrue(all('history' not in x for x in record['history']))
        self.assertLess(len(json.dumps(record)), 10000)

    def test_legacy_nested_snapshots_are_flattened_without_mutating_prior(self):
        first = dict(state='investigating', proposal={'evidence': 'first'}, updated_at=1, history=[])
        second = dict(state='implementing', proposal={'evidence': 'second'}, updated_at=2, history=[first])
        prior = dict(state='merged', proposal={'evidence': 'third'}, updated_at=3, history=[first, second])
        before = json.dumps(prior)
        record = transition_record({'state': 'deployed'}, prior, updated_at=4)
        self.assertEqual([x['proposal']['evidence'] for x in record['history']], ['first', 'second', 'third'])
        self.assertTrue(all('history' not in x for x in record['history']))
        self.assertEqual(json.dumps(prior), before)

    def setUp(self):
        self.p = dict(repository='o/r', issue=1, request='Remove intake button',
                      owner_session='ses_owner', state='verified', next_action='finish',
                      source_sha='a'*40, live_url='https://test.example',
                      deployment_receipt='receipt.json', acceptance_evidence=['browser evidence'])
        self.v = dict(proposal_hash=digest(self.p), reviewer_session='ses_review',
                      accepted=True, evidence_checked=['browser evidence'])
        self.session = dict(agent='delivery-reviewer', outcome='succeeded')

    def test_acceptance(self):
        accepted(self.p, self.v, 'ses_review', self.session)

    def test_publication_not_verification(self):
        del self.p['deployment_receipt']
        with self.assertRaises(ValueError):
            validate(self.p)

    def test_self_approval_denied(self):
        with self.assertRaises(ValueError):
            accepted(self.p, self.v, 'ses_owner', self.session)

    def test_changed_proposal_denied(self):
        self.p['live_url'] = 'https://other.example'
        with self.assertRaises(ValueError):
            accepted(self.p, self.v, 'ses_review', self.session)

    def test_rejected_review_denied(self):
        self.v['accepted'] = False
        with self.assertRaises(ValueError):
            accepted(self.p, self.v, 'ses_review', self.session)

    def test_failed_session_denied(self):
        self.session['outcome'] = 'failed'
        with self.assertRaises(ValueError):
            accepted(self.p, self.v, 'ses_review', self.session)

    def test_missing_logs_not_a_substantiated_blocker(self):
        self.p.update(state='blocked', investigation=[], unavailable_resource='logs',
                      owner_action='provide logs', alternatives='none')
        with self.assertRaises(ValueError):
            validate(self.p)

    def test_investigation_evidence_required(self):
        self.p.update(state='blocked', investigation=[{'source':'run store', 'result':'denied'}],
                      unavailable_resource='run store', owner_action='grant access', alternatives='logs')
        with self.assertRaises(ValueError):
            validate(self.p)

    def test_intermediate_state_keeps_next_action(self):
        self.p['state'] = 'published'
        validate(self.p)
        self.p['next_action'] = ''
        with self.assertRaises(ValueError):
            validate(self.p)

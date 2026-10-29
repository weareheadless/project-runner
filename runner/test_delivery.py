import unittest
from delivery import validate, accepted
from supervisor import digest


class DeliveryTests(unittest.TestCase):
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

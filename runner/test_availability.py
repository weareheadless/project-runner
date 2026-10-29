import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import availability
from supervisor import save


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Mock()
        self.ctx.state = Path(self.tmp.name)
        self.ctx.cfg = {'availability_checks': True, 'orchestrator_model': {'id': 'model', 'providerID': 'openai'}}

    def test_fresh_success_overrides_old_quota(self):
        state = {'quota': {'until': 9000},
                 'credentials': {'cred': {'exhausted_until': 9000}},
                 'availability_probe': {'session': 'ses_probe', 'credential': 'cred', 'started': 1}}
        oc = Mock(return_value={'data': {'outcome': 'succeeded', 'time': {'idle': 2}}})
        self.assertTrue(availability.check(self.ctx, state, oc, save, 100))
        self.assertEqual(state['quota']['until'], 100)
        self.assertNotIn('exhausted_until', state['credentials']['cred'])
        self.assertTrue(state['quota']['credential_changed'])

    def test_bounded_checks_not_every_poll(self):
        state = {'quota': {'until': 9000}, 'availability_attempts': {'cred': 90}}
        oc = Mock(return_value={'data': [{'id': 'cred', 'integrationID': 'openai', 'active': True}]})
        self.assertFalse(availability.check(self.ctx, state, oc, save, 100))
        self.assertEqual(oc.call_count, 1)

    def test_no_check_when_owned_work_running(self):
        state = {'quota': {'until': 9000}, 'active': {'id': 'ses_work'}}
        oc = Mock(side_effect=[{'data': [{'id':'cred','integrationID':'openai','active':True}]},
                              {'data': {'time': {}}}])
        self.assertFalse(availability.check(self.ctx, state, oc, save, 100))
        self.assertFalse(any(c.args[0] == 'post' for c in oc.call_args_list))

    def test_pending_probe_does_not_duplicate(self):
        state = {'quota': {'until': 9000}, 'availability_probe': {'session':'ses_probe', 'started':90}}
        oc = Mock(return_value={'data': {'time': {}}})
        self.assertTrue(availability.check(self.ctx, state, oc, save, 100))
        self.assertEqual(oc.call_count, 1)

    def test_network_failure_does_not_mark_account_exhausted(self):
        state = {'quota': {'until':9000}, 'availability_probe': {'session':'ses_probe','credential':'cred','started':90}}
        oc = Mock(side_effect=[{'data':{'outcome':'failed','time':{'idle':99}}},
                              {'data':[{'error':{'type':'provider.network','message':'timeout'}}]}])
        availability.check(self.ctx, state, oc, save, 100)
        self.assertNotIn('exhausted_until', state['credentials']['cred'])

    def test_expired_cooldown_proceeds_without_another_probe(self):
        oc = Mock()
        self.assertFalse(availability.check(self.ctx, {'quota': {'until':99}}, oc, save, 100))
        oc.assert_not_called()

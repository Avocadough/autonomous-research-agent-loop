"""Offline route-authority and actual Controller request regression checks."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.quant_research_loop import controller as C
from tools.quant_research_loop import model_routing as M


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(M, 'ROOT', self.root).start()
        self.path = self.root / M.AUTHORIZATION
        self.path.parent.mkdir(parents=True)
        self.source = self.path.parent / 'USER_AUTHORIZATION.txt'
        self.source.write_text('Change Lead and QA to Sol 6.1 high; no resume.', encoding='utf-8')
        self.auth = {
            'revision_id': M.REVISION, 'campaign_id': 'PAYOUT_V1', 'approved': True,
            'source': self.source.name, 'source_sha256': C.digest(self.source),
            'provider': 'codex', 'login': 'chatgpt',
            'routes': {role: {'model': M.MODEL, 'reasoning': M.REASONING} for role in ('lead', 'qa')},
            'executor': {'provider': 'codex', 'model': 'gpt-6-luna', 'reasoning': 'max'},
            'global_call_cap': 1000, 'spend_usd': 0, 'live': 'DISABLED',
            'resume_authorized': False, 'qa_tools_allowed': False,
        }
        self.state = {
            'status': 'HUMAN_ACTION_REQUIRED', 'model_calls': 520, 'bootstrap_invocations': 1,
            'prior_reserved_calls': 12, 'operator_safety_hold': {'active': True},
            'completed': {}, 'decisions': 0, 'evidence_id': 'INVENTORY', 'trials': 0,
            'live': 'DISABLED', 'qualification_verified': False,
        }
        self.persist_authority()

    def persist_authority(self):
        self.path.write_text(json.dumps(self.auth), encoding='utf-8')
        self.state['model_route_authority'] = {
            'path': self.path.relative_to(self.root).as_posix(), 'revision_id': self.auth['revision_id'],
            'sha256': C.digest(self.path),
        }

    def profile(self):
        return {
            'lead': 'gpt-6-astra', 'reasoning': 'xhigh',
            'worker': 'gpt-6-luna', 'worker_provider': 'codex', 'worker_variant': 'max',
            'spend_authorized': 0, 'live': 'DISABLED', 'max_calls': 1000,
            'max_configs': 90, 'max_active_seconds': 21600,
            'risk_ceiling_usd': 1000, 'test_reserved': 'NOT_AUTHORIZED',
            'data': {'allowed_partitions': ['train', 'validation'], 'pin': 'unchanged'},
            'route_authority': {'worker_model': 'gpt-6-luna', 'worker_reasoning': 'max',
                                'historical_permission': 'retained'},
            'jobs': [{'job_id': 'fixture', 'candidate_id': 'fixture'}],
        }

    def test_authority_binds_route_and_direct_user_source(self):
        self.assertEqual(M.authority(self.state), self.auth)
        self.source.write_text('Different authorization text', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'user authorization hash mismatch'):
            M.authority(self.state)

    def test_authority_rejects_changed_hash_or_pointer(self):
        original = copy.deepcopy(self.state)
        for field, replacement in (('sha256', 'changed'), ('path', '../outside.json'),
                                   ('revision_id', 'different')):
            with self.subTest(field=field):
                state = copy.deepcopy(original)
                state['model_route_authority'][field] = replacement
                with self.assertRaisesRegex(ValueError, 'authorization .*mismatch'):
                    M.authority(state)

    def test_rehashed_route_and_permission_changes_still_fail_closed(self):
        original = copy.deepcopy(self.auth)
        changes = {
            'routes': {'lead': {'model': 'gpt-6-astra', 'reasoning': 'high'},
                       'qa': {'model': M.MODEL, 'reasoning': M.REASONING}},
            'executor': {'provider': 'codex', 'model': 'gpt-6.1-sol', 'reasoning': 'high'},
            'global_call_cap': 1001, 'spend_usd': 1, 'live': 'ENABLED',
            'resume_authorized': True, 'qa_tools_allowed': True,
            'provider': 'external', 'login': 'api', 'approved': False,
        }
        for key, replacement in changes.items():
            with self.subTest(field=key):
                self.auth = dict(copy.deepcopy(original), **{key: replacement})
                self.persist_authority()
                with self.assertRaisesRegex(ValueError, 'route or preserved permission mismatch'):
                    M.authority(self.state)

    def test_apply_changes_only_lead_route_and_provenance(self):
        profile = self.profile()
        before_profile, before_state = copy.deepcopy(profile), copy.deepcopy(self.state)
        effective = M.apply(profile, self.state)
        self.assertEqual((effective['lead'], effective['reasoning']), (M.MODEL, M.REASONING))
        changed_keys = {'lead', 'reasoning', 'route_authority', 'model_route_authority'}
        self.assertEqual({k: v for k, v in effective.items() if k not in changed_keys},
                         {k: v for k, v in profile.items() if k not in changed_keys})
        for key, value in profile['route_authority'].items():
            self.assertEqual(effective['route_authority'][key], value)
        self.assertEqual(profile, before_profile)
        self.assertEqual(self.state, before_state)
        effective['data']['allowed_partitions'].append('mutated only in copy')
        effective['model_route_authority']['sha256'] = 'mutated only in copy'
        self.assertEqual(profile, before_profile)
        self.assertEqual(self.state, before_state)

    def test_historical_state_without_route_pointer_is_unchanged(self):
        profile = self.profile()
        state = dict(self.state)
        del state['model_route_authority']
        self.assertIsNone(M.authority(state))
        self.assertIs(M.apply(profile, state), profile)
        self.assertEqual((profile['lead'], profile['reasoning']), ('gpt-6-astra', 'xhigh'))

    def controller_fixture(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        gate = self.root / 'gate'
        gate.mkdir()
        C.write(gate / 'latest_gate.json', {'status': 'GATE_ZERO_PASSED'})
        profile = self.profile()
        profile.update(worker='opencode/muse-spark-1.3-contributor-free', worker_variant='xhigh')
        profile.pop('worker_provider')
        C.write(runtime / 'profile.json', profile)
        C.write(runtime / 'profile.sha256.json', {'sha256': C.digest(runtime / 'profile.json')})
        C.write(runtime / 'state.json', self.state)
        return runtime, gate, profile

    def test_controller_init_applies_new_route_after_existing_permissions(self):
        runtime, gate, profile = self.controller_fixture()
        state_bytes = (runtime / 'state.json').read_bytes()
        with patch.object(C, 'ROOT', self.root), patch.object(C, 'RUNTIME', runtime), \
                patch.object(C, 'BASE', gate), patch.object(C.Controller, 'save'):
            controller = C.Controller()
        self.assertEqual((controller.profile['lead'], controller.profile['reasoning']), (M.MODEL, M.REASONING))
        self.assertEqual(controller.profile['worker'], profile['worker'])
        self.assertEqual(controller.state, self.state)
        self.assertEqual((runtime / 'state.json').read_bytes(), state_bytes)

    def test_controller_rejects_financial_permission_before_model_overlay(self):
        runtime, gate, profile = self.controller_fixture()
        profile['spend_authorized'] = 1
        C.write(runtime / 'profile.json', profile)
        C.write(runtime / 'profile.sha256.json', {'sha256': C.digest(runtime / 'profile.json')})
        with patch.object(C, 'ROOT', self.root), patch.object(C, 'RUNTIME', runtime), \
                patch.object(C, 'BASE', gate), patch.object(C.Controller, 'save'), \
                patch.object(M, 'apply', wraps=M.apply) as overlay:
            with self.assertRaisesRegex(ValueError, 'financial permission mismatch'):
                C.Controller()
            overlay.assert_not_called()

    def test_cross_route_saved_call_is_rejected_before_qa_or_lead_spending(self):
        runtime = self.root / 'runtime'
        call = runtime / 'decisions/000/call_0'
        call.mkdir(parents=True)
        controller = C.Controller.__new__(C.Controller)
        controller.state = copy.deepcopy(self.state)
        controller.profile = M.apply(self.profile(), controller.state)
        controller.model_call = lambda *args, **kwargs: self.fail('must not dispatch Lead')
        before = copy.deepcopy(controller.state)
        for argv in (['-m', 'gpt-6-astra', '-c', 'model_reasoning_effort="xhigh"'],
                     ['-m', M.MODEL, '-c', 'model_reasoning_effort="max"'], ['-m']):
            with self.subTest(argv=argv):
                C.write(call / 'request.json', {'argv': argv})
                with patch.object(C, 'RUNTIME', runtime), \
                        patch('tools.quant_research_loop.qa_review.review') as qa:
                    with self.assertRaisesRegex(ValueError, 'saved route differs'):
                        controller.lead()
                    qa.assert_not_called()
                self.assertEqual(controller.state, before)

        (call / 'request.json').unlink()
        with patch.object(C, 'RUNTIME', runtime), \
                patch('tools.quant_research_loop.qa_review.review') as qa:
            with self.assertRaisesRegex(ValueError, 'saved route missing'):
                controller.lead()
            qa.assert_not_called()
        self.assertEqual(controller.state, before)

    def test_actual_lead_requests_new_model_and_prompt_for_each_prompt_branch(self):
        for branch in ('inventory', 'mandate', 'generic'):
            with self.subTest(branch=branch):
                runtime = self.root / branch
                runtime.mkdir()
                C.write(self.root / 'rules.json', {'protected_data': False})
                C.write(self.root / 'activation.json', {'status': 'offline fixture'})
                controller = C.Controller.__new__(C.Controller)
                controller.state = copy.deepcopy(self.state)
                controller.state['operator_safety_hold'] = {'active': False}
                profile = self.profile()
                profile['rules_path'] = 'rules.json'
                if branch in ('mandate', 'generic'):
                    profile.update(mandate_revision='offline fixture', activation_evidence_path='activation.json')
                controller.profile = M.apply(profile, controller.state)
                controller.save = lambda: None
                requests = []
                def model_call(cmd, prompt, folder, **kwargs):
                    requests.append((cmd, prompt))
                    out = Path(cmd[cmd.index('-o') + 1])
                    C.write(out, {
                        'campaign_id': 'PAYOUT_V1', 'evidence_id': 'INVENTORY', 'action': 'PAUSE',
                        'rationale': 'Offline fixture stops without research or model execution.',
                        'artifact_refs': [(folder.parent / 'evidence.json').relative_to(self.root).as_posix()],
                        'next_job': None, 'worker_dispatch_ready': False,
                        'missing_information': [], 'pause_category': 'DATA_OR_RULES_BLOCKED',
                    })
                    return {'exit_code': 0}, ''
                controller.model_call = model_call
                with patch.object(C, 'ROOT', self.root), patch.object(C, 'RUNTIME', runtime), \
                        patch.object(C, 'native_command', return_value=['codex']), \
                        patch.object(C, '_supplemental_audit', return_value=None), \
                        patch('tools.quant_research_loop.qa_review.review', return_value=None), \
                        patch('tools.quant_research_loop.generic_jobs.authorized', return_value=branch == 'generic'), \
                        patch('tools.quant_research_loop.generic_jobs.authority_catalog', return_value=[]), \
                        patch('tools.quant_research_loop.generic_jobs.accounted_seconds', return_value=0), \
                        patch('tools.quant_research_loop.runtime_policy.cumulative_runtime_cap_bypassed', return_value=True):
                    self.assertEqual(controller.lead()['action'], 'PAUSE')
                self.assertEqual(len(requests), 1)
                cmd, prompt = requests[0]
                self.assertEqual(cmd[cmd.index('-m') + 1], M.MODEL)
                self.assertEqual(cmd[cmd.index('--disable') + 1], 'multi_agent')
                reasoning = self.auth['routes']['lead']['reasoning']
                self.assertIn(f'model_reasoning_effort="{reasoning}"', cmd)
                self.assertIn('You are the ', prompt)
                self.assertIn(f'Research Lead ({M.MODEL}, {reasoning})', prompt)
                self.assertNotIn('Astra Research Lead', prompt)
                bundle = C.read(runtime / 'decisions/000/evidence.json')
                self.assertEqual(bundle['active_model_routes'], self.auth)
                self.assertEqual(bundle['profile']['worker'], 'gpt-6-luna')
                self.assertEqual(controller.state['model_calls'], self.state['model_calls'])


class AutonomousModelRoutingTests(unittest.TestCase):
    persist_authority = ModelRoutingTests.persist_authority
    profile = ModelRoutingTests.profile

    def setUp(self):
        ModelRoutingTests.setUp(self)
        self.path = self.root / M.AUTONOMOUS_AUTHORIZATION
        self.path.parent.mkdir(parents=True)
        self.source = self.path.parent / 'USER_AUTHORIZATION.txt'
        self.source.write_text('Only Lead: Sol 6.1 ultra; max if unsupported. No trading.', encoding='utf-8')
        self.capability_path = self.root / M.MODEL_CAPABILITY
        self.capability = {
            'schema_version': 1, 'revision_id': M.AUTONOMOUS_REVISION, 'model': M.MODEL,
            'provider': 'codex', 'login': 'chatgpt', 'model_call_units': 0,
            'no_thread_or_model_turn_sent': True, 'api_credentials_used': False,
            'sent_methods': ['initialize', 'initialized', 'model/list', 'config/read'],
            'selected_reasoning': 'ultra',
            'model_catalog': {'method': 'model/list', 'nextCursor': None,
                              'model': {'id': M.MODEL, 'model': M.MODEL,
                                        'supportedReasoningEfforts': [
                                            {'reasoningEffort': effort} for effort in ('high', 'max', 'ultra')]}},
            'native_config': {'key': 'model_reasoning_effort', 'requested': 'ultra', 'observed': 'ultra',
                              'initialize_completed': True, 'config_read_completed': True},
        }
        self.auth.update(revision_id=M.AUTONOMOUS_REVISION, source_sha256=C.digest(self.source),
                         skill_engineering={'model': M.MODEL, 'reasoning': 'high'})
        self.auth['routes']['lead']['reasoning'] = 'ultra'
        self.persist_capability()

    def persist_capability(self):
        self.capability_path.write_text(json.dumps(self.capability), encoding='utf-8')
        self.auth['model_capability'] = {'path': M.MODEL_CAPABILITY.as_posix(),
                                         'sha256': C.digest(self.capability_path)}
        self.persist_authority()

    def test_ultra_changes_only_lead_and_keeps_other_roles_and_permissions(self):
        original_profile, original_state = self.profile(), copy.deepcopy(self.state)
        approved = M.authority(self.state)
        self.assertEqual(approved['routes']['lead'], {'model': M.MODEL, 'reasoning': 'ultra'})
        self.assertEqual(approved['routes']['qa'], {'model': M.MODEL, 'reasoning': 'high'})
        self.assertEqual(approved['skill_engineering'], {'model': M.MODEL, 'reasoning': 'high'})
        effective = M.apply(original_profile, self.state)
        self.assertEqual(effective['reasoning'], 'ultra')
        for key in ('worker', 'worker_provider', 'worker_variant', 'max_calls', 'spend_authorized',
                    'live', 'data', 'jobs', 'test_reserved', 'max_configs', 'max_active_seconds'):
            self.assertEqual(effective[key], original_profile[key])
        self.assertEqual(self.state, original_state)
        self.assertFalse(approved['resume_authorized'])

    def test_max_fallback_when_native_catalog_lacks_ultra(self):
        self.capability['model_catalog']['model']['supportedReasoningEfforts'].pop()
        self.capability['selected_reasoning'] = 'max'
        self.capability['native_config'].update(requested='max', observed='max')
        self.auth['routes']['lead']['reasoning'] = 'max'
        self.persist_capability()
        self.assertEqual(M.apply(self.profile(), self.state)['reasoning'], 'max')

    def test_max_config_fallback_requires_recorded_native_ultra_rejection(self):
        self.capability['selected_reasoning'] = 'max'
        self.capability['native_config'].update(requested='max', observed='max')
        self.auth['routes']['lead']['reasoning'] = 'max'
        self.persist_capability()
        with self.assertRaisesRegex(ValueError, 'fallback lacks native rejection'):
            M.authority(self.state)
        self.capability['native_config']['ultra_rejection'] = {
            'reasoning': 'ultra', 'stage': 'native_config', 'error': 'Fixture: unsupported effort ultra',
            'no_thread_or_model_turn_sent': True}
        self.persist_capability()
        self.assertEqual(M.authority(self.state)['routes']['lead']['reasoning'], 'max')

    def test_rehashed_unrequested_efforts_roles_or_permissions_are_rejected(self):
        original = copy.deepcopy(self.auth)
        changes = [('routes', {'lead': {'model': M.MODEL, 'reasoning': effort},
                               'qa': {'model': M.MODEL, 'reasoning': 'high'}})
                   for effort in ('max', 'high', 'xhigh', 'unknown')]
        changes += [('routes', {'lead': {'model': M.MODEL, 'reasoning': 'ultra'},
                                'qa': {'model': M.MODEL, 'reasoning': 'ultra'}}),
                    ('skill_engineering', {'model': M.MODEL, 'reasoning': 'ultra'}),
                    ('executor', {'provider': 'codex', 'model': M.MODEL, 'reasoning': 'ultra'}),
                    ('resume_authorized', True), ('spend_usd', False), ('approved', 1)]
        for key, changed in changes:
            with self.subTest(key=key, changed=changed):
                self.auth = dict(copy.deepcopy(original), **{key: changed})
                self.persist_authority()
                with self.assertRaisesRegex(ValueError, 'preserved permission mismatch'):
                    M.authority(self.state)

    def test_capability_and_source_pins_fail_closed(self):
        self.capability_path.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'capability hash mismatch'):
            M.authority(self.state)
        self.persist_capability()
        self.auth['model_capability']['path'] = '../other-capability.json'
        self.persist_authority()
        with self.assertRaisesRegex(ValueError, 'capability identity mismatch'):
            M.authority(self.state)
        self.persist_capability()
        ModelRoutingTests.test_authority_binds_route_and_direct_user_source(self)

    def test_rehashed_malformed_or_unsupported_capability_fails_closed(self):
        original = copy.deepcopy(self.capability)
        mutations = [
            lambda c: c.update(no_thread_or_model_turn_sent=False),
            lambda c: c.update(model_call_units=1),
            lambda c: c.update(model_call_units=False),
            lambda c: c.update(sent_methods=['initialize', 'thread/start', 'turn/start']),
            lambda c: c.update(selected_reasoning='high'),
            lambda c: c['native_config'].update(observed='high'),
            lambda c: c['native_config'].update(config_read_completed=1),
            lambda c: c['model_catalog'].update(nextCursor='more'),
            lambda c: c['model_catalog']['model'].update(model='gpt-6-astra'),
            lambda c: c['model_catalog']['model'].update(supportedReasoningEfforts=[]),
            lambda c: c['model_catalog']['model'].update(supportedReasoningEfforts=[{'reasoningEffort':'high'}]),
            lambda c: c['model_catalog']['model']['supportedReasoningEfforts'].append({'reasoningEffort':'ultra'}),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                self.capability = copy.deepcopy(original)
                mutate(self.capability)
                self.persist_capability()
                with self.assertRaisesRegex(ValueError, 'Model capability'):
                    M.authority(self.state)

    def test_actual_lead_ultra_requests_and_prompts_all_branches(self):
        ModelRoutingTests.test_actual_lead_requests_new_model_and_prompt_for_each_prompt_branch(self)

    def test_authorization_pin_and_historical_route_compatibility(self):
        ModelRoutingTests.test_authority_rejects_changed_hash_or_pointer(self)
        for pointer in ([], {}, {'revision_id': []}):
            with self.subTest(pointer=pointer), self.assertRaisesRegex(ValueError, 'identity mismatch'):
                M.authority(dict(self.state, model_route_authority=pointer))
        historical = copy.deepcopy(self.state)
        path = self.root / M.AUTHORIZATION
        historical['model_route_authority'] = {'path': M.AUTHORIZATION.as_posix(),
                                               'revision_id': M.REVISION, 'sha256': C.digest(path)}
        self.assertEqual(M.authority(historical)['routes']['lead']['reasoning'], 'high')


if __name__ == '__main__':
    unittest.main()

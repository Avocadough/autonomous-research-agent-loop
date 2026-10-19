"""Offline R25 authority and native worker identity checks; no model turns."""
import copy
import io
import json
import unittest
from unittest.mock import Mock, patch

from tools.quant_research_loop import model_routing as M
from tools.quant_research_loop import native_preflight as N
from tools.quant_research_loop import generic_worker as W
from tools.quant_research_loop import worker_scope as S
from tools import test_quant_model_routing as routing_fixtures
from tools import test_quant_native_preflight as native_fixtures


class LeanExecutorRoutingTests(unittest.TestCase):
    persist_authority = routing_fixtures.AutonomousModelRoutingTests.persist_authority
    persist_capability = routing_fixtures.AutonomousModelRoutingTests.persist_capability
    profile = routing_fixtures.AutonomousModelRoutingTests.profile

    def setUp(self):
        routing_fixtures.AutonomousModelRoutingTests.setUp(self)
        self.path = self.root / M.LEAN_AUTHORIZATION
        self.path.parent.mkdir(parents=True)
        self.source = self.path.parent / 'USER_AUTHORIZATION.txt'
        self.source.write_text('Executor Sol 6.1 medium; retain Lead ultra and QA high.', encoding='utf-8')
        from tools.quant_research_loop.prepare import digest
        self.auth.update(revision_id=M.LEAN_REVISION, source_sha256=digest(self.source),
                         executor={'provider': 'codex', 'model': M.MODEL, 'reasoning': 'medium'})
        # Keep the already pinned historical capability immutable in this test.
        capability = copy.deepcopy(self.capability)
        capability['model_catalog']['model']['supportedReasoningEfforts'].append({'reasoningEffort': 'medium'})
        self.capability = capability
        self.persist_capability()
        historical = self.root / M.AUTONOMOUS_AUTHORIZATION
        old_auth = json.loads(historical.read_text())
        old_auth['model_capability'] = copy.deepcopy(self.auth['model_capability'])
        historical.write_text(json.dumps(old_auth))

    def test_executor_overlay_preserves_safety_accounting_and_other_roles(self):
        profile, state = self.profile(), copy.deepcopy(self.state)
        before = copy.deepcopy(profile)
        out = M.apply(profile, state)
        self.assertEqual((out['worker_provider'], out['worker'], out['worker_variant']),
                         ('codex', M.MODEL, 'medium'))
        self.assertEqual((out['lead'], out['reasoning']), (M.MODEL, 'ultra'))
        allowed = {'lead', 'reasoning', 'worker', 'worker_provider', 'worker_variant',
                   'route_authority', 'model_route_authority'}
        self.assertEqual({k: v for k, v in out.items() if k not in allowed},
                         {k: v for k, v in before.items() if k not in allowed})
        self.assertEqual(profile, before)
        self.assertEqual(state, self.state)
        auth = M.authority(state)
        self.assertEqual(auth['routes']['qa'], {'model': M.MODEL, 'reasoning': 'high'})
        self.assertEqual(auth['skill_engineering'], {'model': M.MODEL, 'reasoning': 'high'})
        self.assertFalse(auth['resume_authorized'])
        self.assertEqual(out['route_authority']['worker_reasoning'], 'medium')

    def test_rehashed_unapproved_route_and_permission_changes_fail_closed(self):
        original = copy.deepcopy(self.auth)
        changes = [('executor', {'provider': 'codex', 'model': M.MODEL, 'reasoning': 'high'}),
                   ('executor', {'provider': 'codex', 'model': 'gpt-6-luna', 'reasoning': 'max'}),
                   ('resume_authorized', True), ('qa_tools_allowed', True), ('spend_usd', 1),
                   ('global_call_cap', 1001), ('live', 'ENABLED'),
                   ('skill_engineering', {'model': M.MODEL, 'reasoning': 'medium'})]
        for key, changed in changes:
            with self.subTest(key=key, changed=changed):
                self.auth = dict(copy.deepcopy(original), **{key: changed})
                self.persist_authority()
                with self.assertRaisesRegex(ValueError, 'preserved permission mismatch'):
                    M.authority(self.state)

    def test_executor_medium_requires_pinned_advertised_capability(self):
        self.capability['model_catalog']['model']['supportedReasoningEfforts'].pop()
        self.persist_capability()
        with self.assertRaisesRegex(ValueError, 'executor reasoning unsupported'):
            M.authority(self.state)

    def test_worker_command_requires_exact_pinned_sol_medium_overlay(self):
        effective = M.apply(self.profile(), self.state)
        with patch.object(W, 'native_command', return_value=['codex']):
            cmd = W.worker_command(effective, 'fixture')
            self.assertIn('model_reasoning_effort="medium"', cmd)
            self.assertEqual(cmd[cmd.index('-m') + 1], M.MODEL)
            unpinned = dict(effective)
            unpinned.pop('model_route_authority')
            self.assertFalse(W.codex_route(unpinned))
            with self.assertRaisesRegex(ValueError, 'Unapproved'):
                W.worker_command(unpinned, 'fixture')
            with self.assertRaisesRegex(ValueError, 'Unapproved'):
                W.worker_command(dict(effective, worker_variant='high'), 'fixture')

    def test_r24_and_r19_saved_authorities_keep_luna(self):
        from tools.quant_research_loop.prepare import digest
        for relative, revision in ((M.AUTONOMOUS_AUTHORIZATION, M.AUTONOMOUS_REVISION),
                                   (M.AUTHORIZATION, M.REVISION)):
            state = dict(self.state, model_route_authority={'path': relative.as_posix(),
                         'revision_id': revision, 'sha256': digest(self.root / relative)})
            out = M.apply(self.profile(), state)
            self.assertEqual((out['worker'], out['worker_variant']), ('gpt-6-luna', 'max'))


class NativeExecutorReasoningTests(unittest.TestCase):
    def setUp(self):
        native_fixtures.NativePreflightTests.setUp(self)

    def test_sol_medium_is_proven_on_same_native_session_before_turn(self):
        response = dict(self.response, model=M.MODEL, reasoningEffort='medium')
        messages = [{'id': 1, 'result': {}}, {'id': 2, 'result': response}]
        proc = Mock()
        proc.stdout = io.StringIO('\n'.join(json.dumps(x) for x in messages) + '\n')
        proc.stderr = io.StringIO('')
        proc.stdin = io.StringIO()
        proc.stdin.close = Mock()
        proc.returncode = 0
        path = self.cwd / 'medium_proof.json'
        with patch.object(N, 'native_command', return_value=['codex']), \
                patch.object(N.subprocess, 'Popen', return_value=proc):
            session = N.prepare_session(self.cwd, M.MODEL, path, reasoning='medium')
            self.assertIn('model_reasoning_effort="medium"', session.argv)
            self.assertEqual(session.params['config']['model_reasoning_effort'], 'medium')
            proof = json.loads(path.read_text())
            self.assertTrue(proof['passed'])
            self.assertEqual(proof['response']['reasoningEffort'], 'medium')
            self.assertNotIn('turn/start', proc.stdin.getvalue())
            session.close()
        for field, value in (('model', 'gpt-6-luna'), ('reasoningEffort', 'max')):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'pre-turn policy'):
                N.validate_policy(dict(response, **{field: value}), self.cwd, M.MODEL, 'medium')

    def test_historical_default_remains_max_and_empty_reasoning_rejected(self):
        self.assertEqual(N.thread_parameters(self.cwd, 'gpt-6-luna')['config']['model_reasoning_effort'], 'max')
        N.validate_policy(self.response, self.cwd, 'gpt-6-luna')
        for effort in ('', ' ', None, 1):
            with self.subTest(effort=effort), self.assertRaises(ValueError):
                N.thread_parameters(self.cwd, M.MODEL, effort)

    def test_completion_binds_saved_sol_medium_request_and_exact_preflight(self):
        call = self.cwd / 'call'
        call.mkdir()
        proof_path = self.cwd / 'preflight.json'
        params = N.thread_parameters(self.cwd, M.MODEL, 'medium')
        argv = ['codex', 'app-server', '-c', 'model_reasoning_effort="medium"']
        response = dict(self.response, model=M.MODEL, reasoningEffort='medium')
        proof = {'passed': True, 'no_model_turn_sent': True, 'response': response,
                 'thread_start_params': params, 'argv': argv,
                 'parent_identity_environment_excluded': list(N.PARENT_IDENTITY_ENV)}
        request = {'transport': 'native_app_server', 'argv': argv, 'cwd': str(self.cwd),
                   'thread_start_params': params, 'preflight_path': str(proof_path),
                   'native_session_id': 'test-session'}
        proof_path.write_text(json.dumps(proof))
        (call / 'request.json').write_text(json.dumps(request))
        from tools.quant_research_loop.prepare import digest
        exported = {'info': {'id': 'test-session', 'model': W.codex_identity(M.MODEL, 'medium')},
                    'identity_evidence': {'request_sha256': digest(call / 'request.json')}}
        self.assertEqual(W.codex_requested_route(call), (M.MODEL, 'medium'))
        self.assertTrue(W.completion_matches(call, exported))
        missing = copy.deepcopy(exported)
        missing.pop('identity_evidence')
        self.assertFalse(W.completion_matches(call, missing))
        changed = copy.deepcopy(exported)
        changed['identity_evidence']['request_sha256'] = 'wrong-pin'
        self.assertFalse(W.completion_matches(call, changed))
        for exclusions in (None, [], [N.PARENT_IDENTITY_ENV[0]]):
            with self.subTest(exclusions=exclusions):
                incomplete = dict(proof, parent_identity_environment_excluded=exclusions)
                proof_path.write_text(json.dumps(incomplete))
                self.assertFalse(W.completion_matches(call, exported))
        proof_path.write_text(json.dumps(proof))
        proof['response']['reasoningEffort'] = 'max'
        proof_path.write_text(json.dumps(proof))
        self.assertFalse(W.completion_matches(call, exported))
        request['thread_start_params']['config']['model_reasoning_effort'] = 'high'
        (call / 'request.json').write_text(json.dumps(request))
        self.assertFalse(W.completion_matches(call, exported))

    def test_scope_uses_saved_request_identity_and_rejects_wrong_context(self):
        import os
        call = self.cwd / 'call'
        call.mkdir()
        params = N.thread_parameters(self.cwd, M.MODEL, 'medium')
        (call / 'request.json').write_text(json.dumps({'transport': 'native_app_server',
            'cwd': str(self.cwd), 'argv': ['codex', 'app-server'], 'thread_start_params': params}))
        sid = '01234567-1234-1234-1234-123456789abc'
        sessions = self.cwd / 'home/sessions'
        sessions.mkdir(parents=True)
        fixed = 'py -3.12 -X utf8 -m fixed'
        context = {'cwd': str(self.cwd), 'approval_policy': 'on-request',
            'sandbox_policy': {'type': 'workspace-write'}, 'model': M.MODEL, 'effort': 'medium'}
        events = [{'type': 'turn_context', 'payload': context},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
                'type': 'CommandExecution', 'id': 'cmd', 'command': ['pwsh.exe', '-Command', fixed],
                'cwd': str(self.cwd), 'exit_code': 0}}}]
        log = sessions / ('rollout-' + sid + '.jsonl')
        job = {'job_id': 'fixture', 'inputs': [], 'allowed_file_scope': []}
        with patch.object(S, 'ROOT', self.cwd), patch.dict(os.environ, {'CODEX_HOME': str(self.cwd / 'home')}):
            log.write_text('\n'.join(json.dumps(e) for e in events))
            self.assertEqual(S.native_session_receipt(call, sid, job, fixed, [])['policy_contexts'][0]['model'], M.MODEL)
            context['effort'] = 'max'
            log.write_text('\n'.join(json.dumps(e) for e in events))
            with self.assertRaisesRegex(RuntimeError, 'model/reasoning mismatch'):
                S.native_session_receipt(call, sid, job, fixed, [])


if __name__ == '__main__':
    unittest.main()

import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.quant_research_loop import qa_review as Q
from tools.quant_research_loop import safety_hold as S


class IncidentQATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = 'research/campaigns/PAYOUT_V1/revisions/R26/'
        self.incident = self.put(self.base + 'INCIDENT.json', {'incident_id': 'internal-test'})
        self.source = self.put('tools/quant_research_loop/safety_hold.py', '# containment\n')
        self.history = self.put('research/campaigns/PAYOUT_V1/runtime/decisions/0/accepted.json', {'action': 'PAUSE'})
        self.proof = self.put(self.base + 'RESTORATION.json', {'passed': True, 'byte_exact': True})
        self.auth = self.put(self.base + 'AUTHORIZATION.json', {'revision_id': 'R26'})
        self.packet = {'incident': self.incident,
            'source_hashes': {self.source['path']: self.source['sha256']},
            'history_artifacts': [self.history],
            'history_restoration': {'passed': True, 'byte_exact': True},
            'no_inflight': True, 'validation': {'passed': True, 'tests': 175},
            'proof_files': [self.proof]}
        self.state = {'operator_safety_hold': {'active': True, 'incident': self.incident['path']},
            'lead_delegated_recovery_authority': self.auth,
            'qa_authority': {'path': 'legacy.json', 'sha256': 'legacy'},
            'model_calls': 596, 'completed': {}}
        self.refresh()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(Q, 'ROOT', self.root))
        self.stack.enter_context(patch.object(S, 'delegated_review_allowed', return_value=True, create=True))
        self.stack.enter_context(patch.object(S, 'delegated_authority', return_value={'revision_id': 'R26'}, create=True))
        self.route = {'model': 'gpt-6.1-sol', 'reasoning': 'high'}
        self.stack.enter_context(patch.object(Q, 'authority', return_value=self.route))

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding='utf-8')
        return {'path': name, 'sha256': Q.digest(path)}

    def refresh(self):
        self.state['delegated_incident_review'] = self.put(self.base + 'REPAIR_PACKET.json', self.packet)

    def report(self, bundle):
        return {'review_key': bundle['review_key'], 'job_id': bundle['job_id'], 'verdict': 'PASS',
            'summary': 'Restoration and containment checked.', 'reviewed_files': list(bundle['files']),
            'facets': [{'facet': facet, 'status': 'PASS', 'evidence': 'Pinned restoration proof'}
                       for facet in Q.FACETS], 'findings': [], 'limitations': ['No historical market acceptance.']}

    def test_full_bounded_packet_hash_checks_history_without_transporting_it(self):
        bundle = Q.build_delegated_bundle(self.state)
        self.assertEqual(bundle['job_id'], 'internal-test')
        self.assertTrue(bundle['deterministically_verified'])
        self.assertEqual(json.loads(bundle['files'][self.state['delegated_incident_review']['path']]['content']), self.packet)
        self.assertNotIn(self.history['path'], bundle['files'])
        self.assertFalse(any(value['truncated'] for value in bundle['files'].values()))
        self.assertEqual(bundle['delegated_incident_review']['incident'], self.incident)
        self.assertEqual(Q.validate_report(self.report(bundle), bundle)['verdict'], 'PASS')
        (self.root / self.history['path']).write_text('{}', encoding='utf-8')
        changed = Q.build_delegated_bundle(self.state)
        self.assertFalse(changed['deterministically_verified'])
        self.assertNotEqual(changed['review_key'], bundle['review_key'])
        self.assertEqual(changed['history_hash_checks'][0]['expected_sha256'], self.history['sha256'])
        self.assertEqual(changed['history_hash_checks'][0]['actual_sha256'], Q.digest(self.root / self.history['path']))
        self.assertNotIn(self.history['path'], changed['files'])
        with self.assertRaisesRegex(ValueError, 'PASS conflicts'):
            Q.validate_report(self.report(changed), changed)

    def test_source_proof_packet_and_incident_pins_are_mandatory(self):
        for ref in (self.source, self.proof, self.incident, self.state['delegated_incident_review']):
            path = self.root / ref['path']
            original = path.read_bytes()
            path.write_text('changed', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'identity'):
                Q.build_delegated_bundle(self.state)
            path.write_bytes(original)

    def test_failed_restoration_validation_or_inflight_never_passes(self):
        for section, key, value in [('history_restoration', 'passed', False),
                ('history_restoration', 'byte_exact', False), ('validation', 'passed', False),
                ('validation', 'tests', 0)]:
            packet = copy.deepcopy(self.packet)
            packet[section][key] = value
            self.state['delegated_incident_review'] = self.put(self.base + 'REPAIR_PACKET.json', packet)
            bundle = Q.build_delegated_bundle(self.state)
            self.assertFalse(bundle['deterministically_verified'])
            with self.assertRaisesRegex(ValueError, 'PASS conflicts'):
                Q.validate_report(self.report(bundle), bundle)
            diagnostic = dict(self.report(bundle), verdict='INCOMPLETE')
            self.assertEqual(Q.validate_report(diagnostic, bundle)['verdict'], 'INCOMPLETE')
        self.refresh()
        self.state['inflight_call'] = 'unresolved'
        with self.assertRaisesRegex(ValueError, 'inflight'):
            Q.build_delegated_bundle(self.state)

    def test_packet_must_cover_active_incident_and_nonempty_proofs(self):
        self.state['operator_safety_hold']['incident'] = 'other.json'
        with self.assertRaisesRegex(ValueError, 'incident'):
            Q.build_delegated_bundle(self.state)
        self.state['operator_safety_hold']['incident'] = self.incident['path']
        for key, value in [('source_hashes', {}), ('history_artifacts', []), ('proof_files', [])]:
            packet = dict(self.packet, **{key: value})
            self.state['delegated_incident_review'] = self.put(self.base + 'REPAIR_PACKET.json', packet)
            if key == 'source_hashes':
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    Q.build_delegated_bundle(self.state)
            else:
                self.assertFalse(Q.build_delegated_bundle(self.state)['deterministically_verified'])

    def test_price_global_and_out_of_scope_proof_paths_are_rejected(self):
        for name in ('dataset/train.json', '../outside.json', 'research/campaigns/PAYOUT_V1/runtime/proof.json'):
            packet = dict(self.packet, proof_files=[{'path': name, 'sha256': 'unused'}])
            self.state['delegated_incident_review'] = self.put(self.base + 'REPAIR_PACKET.json', packet)
            with self.assertRaisesRegex(ValueError, 'scope'):
                Q.build_delegated_bundle(self.state)

    def test_large_required_proof_is_rejected_without_partial_transport(self):
        large = self.put(self.base + 'LARGE.txt', 'x' * 720001)
        self.packet['proof_files'] = [large]
        self.refresh()
        with self.assertRaisesRegex(ValueError, 'complete|bound'):
            Q.build_delegated_bundle(self.state)

    def test_incident_pass_requires_every_complete_required_file_reviewed(self):
        bundle = Q.build_delegated_bundle(self.state)
        report = self.report(bundle)
        report['reviewed_files'].remove(self.proof['path'])
        with self.assertRaisesRegex(ValueError, 'required evidence'):
            Q.validate_report(report, bundle)
        bundle['deterministically_verified'] = False
        with self.assertRaisesRegex(ValueError, 'PASS conflicts'):
            Q.validate_report(self.report(bundle), bundle)

    def test_containment_bundle_uses_shared_disposition_and_keeps_unknown_claim(self):
        self.packet['history_restoration'] = {'passed': False, 'byte_exact': False, 'status': 'UNKNOWN'}
        self.packet['containment_disposition'] = {'mode': S.GIT_CONTAINMENT_MODE}
        self.refresh()
        with patch.object(S, 'incident_disposition', return_value=S.GIT_CONTAINMENT_MODE) as check:
            bundle = Q.build_delegated_bundle(self.state)
        check.assert_called_once_with(self.packet)
        self.assertTrue(bundle['deterministically_verified'])
        self.assertIn('UNKNOWN', bundle['scope'])
        self.assertFalse(bundle['restoration_checks']['byte_exact'])
        self.assertIn('UNKNOWN', bundle['verification_basis'])

    def historical_fixture(self):
        result = self.put('research/campaigns/PAYOUT_V1/runtime/jobs/J/work/result.json', {'preserved': True})
        report = {'job_id': 'J', 'review_key': 'saved', 'verdict': 'PASS', 'findings': [],
            'facets': [{'facet': facet, 'status': 'PASS'} for facet in Q.FACETS]}
        qa_pin = self.put('research/campaigns/PAYOUT_V1/runtime/qa/saved/report.json', report)
        qa_ref = dict(qa_pin, job_id='J', review_key='saved', verdict='PASS', source_hashes={result['path']: result['sha256']})
        evidence = self.put('research/campaigns/PAYOUT_V1/runtime/decisions/5/evidence.json', {'independent_sol_qa': qa_ref})
        accepted = self.put('research/campaigns/PAYOUT_V1/runtime/decisions/5/accepted.json',
            {'action': 'FIX_EXPERIMENT', 'evidence_id': 'J', 'artifact_refs': [evidence['path'], qa_pin['path']]})
        self.put('research/campaigns/PAYOUT_V1/runtime/decisions/5/decision_0.json',
            Q.read(self.root / accepted['path']))
        self.put('research/campaigns/PAYOUT_V1/runtime/decisions/5/call_0/receipt.json',
            {'exit_code': 0, 'process_reaped': True})
        self.put('research/campaigns/PAYOUT_V1/runtime/decisions/5/call_0/request.json',
            {'argv': ['codex', '-o', str(self.root / 'research/campaigns/PAYOUT_V1/runtime/decisions/5/decision_0.json')]})
        return {'evidence_id': 'J', 'completed': {'J': dict(result, review=accepted['path'])},
            'reviewed_results': {'J': result['sha256']}, 'qa_reviews': {'saved': qa_ref}}, result, qa_pin

    def test_reuse_is_only_accepted_unchanged_historical_context_without_pending_job(self):
        state, result, qa_pin = self.historical_fixture()
        reused = Q.accepted_historical_qa(state)
        self.assertTrue(reused['historical_for_pending_plan'])
        self.assertIn('No dispatch', reused['claim_limit'])
        for change in ({'pending_job': {'job_id': 'NEW'}}, {'operator_safety_hold': {'active': True}}):
            self.assertIsNone(Q.accepted_historical_qa(dict(state, **change)))
        with self.assertRaisesRegex(ValueError, 'Historical QA'):
            Q.validate_decision({'action': 'READY_FOR_FORWARD'}, reused)
        (self.root / result['path']).write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'result identity'):
            Q.accepted_historical_qa(state)

    def test_historical_qa_report_change_and_result_pin_mismatch_fail_closed(self):
        state, result, qa_pin = self.historical_fixture()
        (self.root / qa_pin['path']).write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'report or result pin'):
            Q.accepted_historical_qa(state)
        state['reviewed_results']['J'] = 'other'
        self.assertIsNone(Q.accepted_historical_qa(state))

    def test_historical_reuse_requires_registered_report_and_real_accepted_lead_call(self):
        state, result, qa_pin = self.historical_fixture()
        state['qa_reviews'] = {}
        with self.assertRaisesRegex(ValueError, 'report or result pin'):
            Q.accepted_historical_qa(state)
        state, result, qa_pin = self.historical_fixture()
        self.put('research/campaigns/PAYOUT_V1/runtime/decisions/5/call_0/receipt.json',
            {'exit_code': 1, 'process_reaped': True})
        with self.assertRaisesRegex(ValueError, 'accepted Lead receipt'):
            Q.accepted_historical_qa(state)
        state, result, qa_pin = self.historical_fixture()
        self.put('research/campaigns/PAYOUT_V1/runtime/decisions/5/call_0/request.json',
            {'argv': ['codex', '-o', str(self.root / 'unrelated.json')]})
        with self.assertRaisesRegex(ValueError, 'accepted Lead receipt'):
            Q.accepted_historical_qa(state)

    def test_provider_retry_generation_changes_incident_cache_identity(self):
        from tools.quant_research_loop import autonomous_recovery as A
        contexts = [None, {'failed_review_key': 'failed', 'generation': 1},
                    {'failed_review_key': 'failed', 'generation': 2}]
        with patch.object(A, 'qa_retry_context', side_effect=contexts) as retry:
            bundles = [Q.build_delegated_bundle(self.state) for _ in contexts]
        self.assertEqual(retry.call_args_list[0].args, (self.state, 'internal-test'))
        self.assertEqual(len({bundle['review_key'] for bundle in bundles}), 3)
        self.assertEqual([bundle['qa_retry_context'] for bundle in bundles], contexts)
        self.assertTrue(all(bundle['delegated_incident_review']['repair_packet'] ==
                            self.state['delegated_incident_review'] for bundle in bundles))

    def run_review(self, events=None, malformed=False, cap=1000):
        calls = []
        def model_call(cmd, prompt, call, **kwargs):
            calls.append((cmd, prompt))
            bundle = Q.read(call.parent / 'evidence.json')
            Path(cmd[cmd.index('-o') + 1]).write_text(json.dumps(self.report(bundle)), encoding='utf-8')
            raw = 'bad json' if malformed else '\n'.join(json.dumps(e) for e in (events or [
                {'type': 'thread.started', 'thread_id': 'fixture'}, {'type': 'turn.completed'}]))
            return {'exit_code': 0}, raw
        controller = SimpleNamespace(state=self.state, profile={'max_calls': cap}, save=lambda: None, model_call=model_call)
        with patch.object(Q, 'native_command', return_value=['codex']), \
                patch('tools.quant_research_loop.skill_engineering.maybe_review') as skills, \
                patch.object(Q, 'target', side_effect=AssertionError('Legacy job target must not be read')):
            result = Q.review(controller)
            skills.assert_not_called()
        return result, calls

    def test_real_accepted_call_precedes_legacy_cache_and_saves_exact_incident_descriptor(self):
        self.state['latest_qa_review'] = {'path': 'missing-old-report.json', 'sha256': 'old'}
        self.state['operator_safety_recovery'] = {'released': True}
        result, calls = self.run_review()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['verdict'], 'PASS')
        self.assertTrue(result['incident_review'])
        self.assertEqual(result['incident'], self.incident)
        self.assertEqual(result['repair_packet'], self.state['delegated_incident_review'])
        cmd, prompt = calls[0]
        self.assertEqual(cmd[cmd.index('-m') + 1], 'gpt-6.1-sol')
        self.assertIn('model_reasoning_effort="high"', cmd)
        self.assertIn('restoration and containment only', prompt)
        self.assertIn('does not grant worker dispatch', prompt)
        self.assertTrue(self.state['operator_safety_hold']['active'])
        self.assertEqual(self.state['model_calls'], 596)
        with self.assertRaisesRegex(ValueError, 'cannot promote'):
            Q.validate_decision({'action': 'READY_FOR_FORWARD'}, result)

    def test_failed_native_completion_has_no_accepted_incident_descriptor(self):
        result, _ = self.run_review(malformed=True)
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertNotIn('incident_review', result)

    def test_forbidden_tool_attempt_and_call_cap_preserve_hold(self):
        with self.assertRaisesRegex(RuntimeError, 'forbidden tool'):
            self.run_review(events=[{'type': 'item.completed', 'item': {'type': 'command_execution'}}])
        self.state['model_calls'] = 999
        with self.assertRaisesRegex(RuntimeError, 'QA plus mandatory Lead'):
            self.run_review()
        self.assertTrue(self.state['operator_safety_hold']['active'])


if __name__ == '__main__':
    unittest.main()

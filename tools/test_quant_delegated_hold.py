"""R26 internal incident disposition with entirely isolated synthetic evidence."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.quant_research_loop import safety_hold as H, generic_jobs as G
from tools.quant_research_loop.gate_zero import write
from tools.quant_research_loop.prepare import digest
from tools.quant_research_loop.qa_review import FACETS


class DelegatedHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Both aliases and every revision/runtime/proof path are under tmp.
        # Patching only H.ROOT would leave imported relpath bound to the repo.
        for module in (H, G):
            p = patch.object(module, 'ROOT', self.root)
            p.start()
            self.addCleanup(p.stop)
        self.base = self.root / 'research/campaigns/PAYOUT_V1/revisions' / H.DELEGATED_REVISION
        self.runtime = self.root / 'research/campaigns/PAYOUT_V1/runtime'
        self.base.mkdir(parents=True)
        self.runtime.mkdir(parents=True)
        self.source = self.base / 'USER_AUTHORIZATION.txt'
        self.source.write_text('Delegate internal incident disposition to the Lead.', encoding='utf-8')
        self.auth = dict(approved=True, campaign_id='PAYOUT_V1', revision_id=H.DELEGATED_REVISION,
                         source=self.relative(self.source), source_sha256=digest(self.source),
                         lead_model='gpt-6.1-sol', lead_reasoning='ultra', global_call_cap=1000,
                         spend_usd=0, live='DISABLED', delegate_internal_incident_disposition=True,
                         preserve_user_stop=True, preserve_data_and_financial_permissions=True)
        self.auth_path = self.put(self.base / 'AUTHORIZATION.json', self.auth)
        self.incident_path = self.put(self.base / 'INCIDENT.json', {'incident_id': 'TEST_INTERNAL_INCIDENT'})
        self.history_path = self.put(self.base / 'history.json', {'historical': 'immutable'})
        self.code_path = self.put(self.root / 'tools/repaired.py', {'repair': 'current'})
        self.restoration = self.put(self.base / 'RESTORATION.json', {'byte_exact_original_restored': True})
        self.packet = dict(incident=self.pin(self.incident_path), scope='INTERNAL_PROJECT_INCIDENT',
                           protected_permissions_affected=False, user_stop=False, no_inflight=True,
                           validation={'passed': True}, history_restoration={'passed': True, 'byte_exact': True},
                           source_hashes={self.relative(self.code_path): digest(self.code_path)},
                           history_artifacts=[self.pin(self.history_path)], proof_files=[self.pin(self.restoration)])
        self.packet_path = self.put(self.base / 'REPAIR_PACKET.json', self.packet)
        self.state = dict(campaign_id='PAYOUT_V1', status='HUMAN_ACTION_REQUIRED', model_calls=580,
                          bootstrap_invocations=6, prior_reserved_calls=10, evidence_id='CURRENT_EVIDENCE',
                          pending_job=None, live='DISABLED',
                          lead_delegated_recovery_authority=self.pin(self.auth_path),
                          delegated_incident_review=self.pin(self.packet_path),
                          operator_safety_hold=dict(active=True, incident=self.relative(self.incident_path),
                                                    incident_sha256=digest(self.incident_path),
                                                    restoration=self.pin(self.restoration),
                                                    prior_unreleased_hold={'active': False, 'incident': 'released_old',
                                                                           'release': {'path': 'old', 'sha256': 'old'}}))
        self.report = dict(job_id='TEST_INTERNAL_INCIDENT', review_key='incident-review', verdict='PASS',
                           reviewed_files=[self.relative(self.packet_path)], findings=[],
                           facets=[{'facet': facet, 'status': 'PASS'} for facet in FACETS])
        self.report_path = self.put(self.runtime / 'qa/incident/report.json', self.report)
        self.state['latest_qa_review'] = dict(self.pin(self.report_path), incident_review=True,
                                             incident=self.packet['incident'], repair_packet=self.pin(self.packet_path),
                                             job_id=self.report['job_id'], review_key=self.report['review_key'],
                                             verdict='PASS', requested_model='gpt-6.1-sol', requested_reasoning='high')
        self.decision = dict(action='RELEASE_REPAIRED_HOLD', next_job=None, worker_dispatch_ready=False,
                             evidence_id=self.state['evidence_id'], artifact_refs=[self.relative(self.incident_path),
                                 self.relative(self.packet_path), self.relative(self.report_path)])
        self.folder = self.runtime / 'decisions/300'
        self.accepted = self.put(self.folder / 'accepted.json', self.decision)
        self.put(self.folder / 'decision_0.json', self.decision)
        self.request = self.put(self.folder / 'call_0/request.json', {
            'argv': ['codex.exe', '-m', 'gpt-6.1-sol', '-c', 'model_reasoning_effort="ultra"', '-s', 'read-only']})
        self.receipt = self.put(self.folder / 'call_0/receipt.json', {'exit_code': 0, 'process_reaped': True})
        self.state['last_decision'] = self.relative(self.accepted)
        self.state['worker_dispatch_decision'] = self.pin(self.accepted)
        self.controller = SimpleNamespace(state=self.state, save=Mock())

    def relative(self, path):
        return path.relative_to(self.root).as_posix()

    def pin(self, path):
        return {'path': self.relative(path), 'sha256': digest(path)}

    def put(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        write(path, value)
        return path

    def repin_packet(self):
        self.put(self.packet_path, self.packet)
        self.state['delegated_incident_review'] = self.pin(self.packet_path)
        self.state['latest_qa_review']['repair_packet'] = self.pin(self.packet_path)

    def repin_qa(self):
        self.put(self.report_path, self.report)
        self.state['latest_qa_review'].update(self.pin(self.report_path))

    def test_explicit_pinned_authority_and_source_are_required(self):
        self.assertEqual(H.delegated_authority(self.state), self.auth)
        self.source.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'pin mismatch'):
            H.delegated_review_allowed(self.state)

    def test_financial_or_route_authority_changes_cannot_enable_release(self):
        for key, value in [('spend_usd', 1), ('live', 'ENABLED'), ('global_call_cap', 1001),
                           ('lead_model', 'other'), ('preserve_user_stop', False),
                           ('preserve_data_and_financial_permissions', False)]:
            changed = dict(self.auth, **{key: value})
            self.put(self.auth_path, changed)
            self.state['lead_delegated_recovery_authority'] = self.pin(self.auth_path)
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'scope mismatch'):
                H.delegated_authority(self.state)

    def test_diagnosis_is_read_only_and_does_not_grant_worker(self):
        before = copy.deepcopy(self.state)
        for folder in ('decisions/301/call_0', 'qa/incident/call_0', 'skill_engineering/600/call_engineer'):
            H.guard_call(self.controller, self.runtime / folder)
        with self.assertRaisesRegex(RuntimeError, 'active operator safety hold'):
            H.guard_call(self.controller, self.runtime / 'jobs/J/worker_call_0')
        with self.assertRaisesRegex(RuntimeError, 'active operator safety hold'):
            H.guard_worker(self.controller, {'job_id': 'J'})
        self.assertEqual(self.state, before)

    def test_failed_repairs_can_be_reviewed_but_cannot_be_released(self):
        self.packet['validation']['passed'] = False
        self.packet['history_restoration']['passed'] = False
        self.repin_packet()
        self.assertTrue(H.delegated_review_allowed(self.state))
        H.guard_call(self.controller, self.runtime / 'decisions/301/call_0')
        with self.assertRaisesRegex(RuntimeError, 'not verified'):
            H.release_delegated_after_review(self.controller, self.decision)
        self.assertTrue(H.active(self.state))

    def test_pause_cancel_inflight_data_and_budget_stops_are_preserved(self):
        for status in ('PAUSED_USER', 'CANCELLED', 'DATA_OR_RULES_BLOCKED', 'RESOURCE_LIMIT'):
            self.state['status'] = status
            with self.subTest(status=status):
                self.assertFalse(H.delegated_review_allowed(self.state))
        self.state['status'] = 'HUMAN_ACTION_REQUIRED'
        self.state['inflight_call'] = 'uncertain'
        self.assertFalse(H.delegated_review_allowed(self.state))
        del self.state['inflight_call']
        self.state['model_calls'] = 984
        self.assertFalse(H.delegated_review_allowed(self.state))
        self.state['model_calls'] = 580
        for name in ('pause.request', 'cancel.request'):
            path = self.runtime / name
            path.write_text('user stop', encoding='utf-8')
            self.assertFalse(H.delegated_review_allowed(self.state))
            with self.assertRaisesRegex(RuntimeError, 'override a stop'):
                H.release_delegated_after_review(self.controller, self.decision)
            self.assertTrue(path.exists())
            path.unlink()

    def test_protected_permission_and_user_incidents_cannot_be_released(self):
        for key, value in [('scope', 'DATA_PERMISSION_INCIDENT'), ('protected_permissions_affected', True),
                           ('user_stop', True)]:
            original = self.packet[key]
            self.packet[key] = value
            self.repin_packet()
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'scope mismatch'):
                H.release_delegated_after_review(self.controller, self.decision)
            self.packet[key] = original

    def test_changed_source_or_history_fails_closed(self):
        for path in (self.code_path, self.history_path, self.restoration):
            before = path.read_bytes()
            path.write_bytes(before + b' ')
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'pin mismatch'):
                H.release_delegated_after_review(self.controller, self.decision)
            path.write_bytes(before)

    def test_changed_history_is_diagnostic_only_until_exactly_restored(self):
        self.history_path.write_bytes(self.history_path.read_bytes() + b' ')
        self.assertTrue(H.delegated_review_allowed(self.state))
        H.guard_call(self.controller, self.runtime / 'qa/incident/call_0')
        with self.assertRaisesRegex(RuntimeError, 'pin mismatch'):
            H.release_delegated_after_review(self.controller, self.decision)
        self.assertTrue(H.active(self.state))

    def test_unacknowledged_nested_hold_cannot_be_hidden(self):
        self.state['operator_safety_hold']['prior_unreleased_hold']['active'] = True
        with self.assertRaisesRegex(RuntimeError, 'scope mismatch'):
            H.release_delegated_after_review(self.controller, self.decision)

    def test_exact_qa_identity_and_all_facets_are_required(self):
        self.report['facets'][0]['status'] = 'UNKNOWN'
        self.repin_qa()
        with self.assertRaisesRegex(RuntimeError, 'independent QA'):
            H.release_delegated_after_review(self.controller, self.decision)
        self.report['facets'][0]['status'] = 'PASS'
        self.repin_qa()
        self.state['latest_qa_review']['incident']['sha256'] = 'other'
        with self.assertRaises(RuntimeError):
            H.release_delegated_after_review(self.controller, self.decision)

    def test_release_must_be_accepted_current_decision_with_pinned_native_call(self):
        self.state['worker_dispatch_decision']['sha256'] = 'different'
        with self.assertRaisesRegex(RuntimeError, 'pin mismatch'):
            H.release_delegated_after_review(self.controller, self.decision)
        self.state['worker_dispatch_decision'] = self.pin(self.accepted)
        self.put(self.receipt, {'exit_code': None, 'process_reaped': True})
        with self.assertRaisesRegex(RuntimeError, 'Lead disposition receipt'):
            H.release_delegated_after_review(self.controller, self.decision)

    def test_worker_dispatch_and_unbound_evidence_block_release(self):
        for decision in (dict(self.decision, worker_dispatch_ready=True),
                         dict(self.decision, next_job={'job_id': 'J'}),
                         dict(self.decision, artifact_refs=[])):
            with self.subTest(decision=decision), self.assertRaises(RuntimeError):
                H.release_delegated_after_review(self.controller, decision)
        self.assertTrue(H.active(self.state))

    def test_success_keeps_historical_releases_and_counters_and_is_idempotent(self):
        original = copy.deepcopy(self.state['operator_safety_hold'])
        counters = {key: self.state[key] for key in ('model_calls', 'bootstrap_invocations', 'prior_reserved_calls')}
        # Only internal controller decision refs may use native separators.
        self.state['last_decision'] = self.state['last_decision'].replace('/', '\\')
        self.state['worker_dispatch_decision']['path'] = self.state['last_decision']
        self.assertTrue(H.release_delegated_after_review(self.controller, self.decision))
        self.assertFalse(H.active(self.state))
        self.assertEqual(self.state['operator_safety_hold']['prior_unreleased_hold'], original['prior_unreleased_hold'])
        for key, value in counters.items():
            self.assertEqual(self.state[key], value)
        self.assertIsNone(self.state['pending_job'])
        self.assertEqual(self.state['operator_safety_hold_history'][0]['incident'], original['incident'])
        proof = G.read(self.root / self.state['delegated_incident_review_release']['path'])
        self.assertEqual(proof['call_units_used'], 596)
        self.assertFalse(proof['historical_result_reclassified'])
        self.assertFalse(proof['worker_dispatch_authorized'])
        self.assertFalse(H.release_delegated_after_review(self.controller, self.decision))

    def containment_fixture(self):
        before, after, rows = {}, {}, []
        for index in range(770):
            name = '.git/objects/' + f'{index:040x}'[:2] + '/' + f'{index:040x}'[2:]
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f'current object {index}'.encode())
            after[name] = [path.stat().st_size, 2, 1, 'FILE']
            if index < 762:
                before[name] = [path.stat().st_size, 1, 1, 'FILE']
            rows.append({'path': name, 'current_content_identity': 'PASS', 'file_sha256': digest(path),
                'saved_metadata_before': before.get(name), 'saved_metadata_after': after[name]})
        before_pin = self.pin(self.put(self.base / 'BEFORE.json', before))
        after_pin = self.pin(self.put(self.base / 'AFTER.json', after))
        scope = {'before': before_pin, 'after': after_pin,
            'changes': {p: {'before': before.get(p), 'after': after[p]} for p in sorted(after)},
            'unexpected': sorted(after)}
        job = self.pin(self.put(self.base / 'job.json', {'allowed_file_scope': []}))
        result = self.pin(self.put(self.base / 'work/result.json', {'payload': {'overall_status': 'FAIL_RETURN_TO_LEAD'}}))
        original = {'pins': [job, result], 'call_spent': True, 'result_accepted': False,
            'new_worker_allowance': False, 'counter_changes': False}
        aux = self.pin(self.put(self.base / 'NATIVE_AUX.json', {'preserved': True}))
        values = {'lead_decision': {'action': 'PAUSE', 'artifact_refs': [self.relative(self.incident_path)]},
            'metadata_scope': scope, 'native_audit': {'write_policy_valid': True, 'audit_error': None,
                'errors': [], 'protocol_errors': [], 'unparsed_tools': [], 'commands': [{'kind': 'fixed_entry'}],
                'edits_count': 5, 'raw': aux, 'pretool_policy': aux},
            'original_preservation': original, 'original_packet': {'history_artifacts': self.packet['history_artifacts']}}
        pins = {key: self.pin(self.put(self.base / (key + '.json'), value)) for key, value in values.items()}
        for name, value in [('GIT_CONTAINMENT_PINS', pins), ('GIT_CONTAINMENT_INCIDENT', self.packet['incident'])]:
            patcher = patch.object(H, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.integrity = {'verdict': 'CURRENT_CONTENT_IDENTITIES_VERIFIED', 'historical_restoration': 'UNKNOWN',
            'protected_metadata_changes': [], 'rows': rows}
        self.integrity_path = self.put(self.base / 'CURRENT_INTEGRITY.json', self.integrity)
        preserved = {'original_pins': original['pins'], 'call_spent': True, 'result_accepted': False,
            'worker_allowance_added': False, 'counters_reset': False}
        preservation = self.pin(self.put(self.base / 'CURRENT_PRESERVATION.json', preserved))
        self.packet['containment_disposition'] = {'mode': H.GIT_CONTAINMENT_MODE,
            'object_integrity': self.pin(self.integrity_path), 'preservation': preservation}
        self.packet['history_restoration'] = {'passed': False, 'byte_exact': False, 'status': 'UNKNOWN'}
        self.packet['validation']['tests'] = 10
        self.packet['proof_files'] += list(pins.values()) + [self.pin(self.integrity_path), preservation]
        self.repin_packet()

    def test_containment_keeps_unknown_failure_counters_and_requires_separate_release(self):
        self.containment_fixture()
        before = copy.deepcopy(self.state)
        self.assertEqual(H.incident_disposition(self.packet), H.GIT_CONTAINMENT_MODE)
        self.assertEqual(self.state, before)
        self.assertTrue(H.active(self.state))
        self.assertTrue(H.release_delegated_after_review(self.controller, self.decision))
        proof = G.read(self.root / self.state['delegated_incident_review_release']['path'])
        self.assertEqual(proof['history_restoration']['status'], 'UNKNOWN')
        self.assertFalse(proof['history_restoration']['byte_exact'])
        self.assertFalse(proof['historical_result_reclassified'])
        self.assertFalse(proof['worker_dispatch_authorized'])
        self.assertEqual(self.state['model_calls'], before['model_calls'])
        self.assertIsNone(self.state['pending_job'])

    def test_containment_rejects_generic_wrong_incident_and_unproven_restore_claims(self):
        self.containment_fixture()
        for section, key, value in [('containment_disposition', 'mode', 'GENERIC_WAIVER'),
                ('incident', 'sha256', 'other'), ('history_restoration', 'status', 'RESTORED'),
                ('history_restoration', 'byte_exact', True), ('validation', 'tests', 0)]:
            packet = copy.deepcopy(self.packet)
            packet[section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(RuntimeError):
                H.incident_disposition(packet)

    def test_containment_rejects_current_linked_git_directory(self):
        self.containment_fixture()
        linked = self.root / '.git/objects'
        with patch.object(Path, 'is_junction', autospec=True, side_effect=lambda path: path == linked):
            with self.assertRaisesRegex(RuntimeError, 'Git containment'):
                H.incident_disposition(self.packet)

    def test_containment_requires_exact_coverage_current_bytes_and_preserved_failed_result(self):
        self.containment_fixture()
        original = copy.deepcopy(self.integrity)
        for mutation in ('missing', 'duplicate', 'failed', 'protected'):
            changed = copy.deepcopy(original)
            if mutation == 'missing': changed['rows'].pop()
            if mutation == 'duplicate': changed['rows'][-1] = changed['rows'][0]
            if mutation == 'failed': changed['rows'][0]['current_content_identity'] = 'FAIL'
            if mutation == 'protected': changed['protected_metadata_changes'] = ['.git/HEAD']
            old_pin = self.packet['containment_disposition']['object_integrity']
            self.put(self.integrity_path, changed)
            new_pin = self.pin(self.integrity_path)
            self.packet['containment_disposition']['object_integrity'] = new_pin
            self.packet['proof_files'] = [new_pin if pin == old_pin else pin for pin in self.packet['proof_files']]
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                H.incident_disposition(self.packet)
        old_pin = self.packet['containment_disposition']['object_integrity']
        self.put(self.integrity_path, original)
        new_pin = self.pin(self.integrity_path)
        self.packet['containment_disposition']['object_integrity'] = new_pin
        self.packet['proof_files'] = [new_pin if pin == old_pin else pin for pin in self.packet['proof_files']]
        (self.root / original['rows'][0]['path']).write_bytes(b'changed object')
        with self.assertRaisesRegex(RuntimeError, 'pin mismatch'):
            H.incident_disposition(self.packet)


if __name__ == '__main__':
    unittest.main()

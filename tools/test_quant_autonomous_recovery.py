"""Offline checks for autonomous technical recovery; no model or data calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.quant_research_loop import autonomous_recovery as A
from tools.quant_research_loop.gate_zero import write
from tools.quant_research_loop.prepare import digest


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'research/campaigns/PAYOUT_V1/runtime'
        self.runtime.mkdir(parents=True)
        for name, value in (('ROOT', self.root), ('RUNTIME', self.runtime)):
            patcher = patch.object(A, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        base = self.root / A.BASE
        base.mkdir(parents=True)
        (base / 'USER_AUTHORIZATION.txt').write_text('Direct user technical autonomy request', encoding='utf-8')
        write(base / 'AUTHORIZATION.json', dict(A.EXPECTED,
              source_sha256=digest(base / 'USER_AUTHORIZATION.txt')))
        self.c = Mock()
        self.c.state = {
            'status': 'ENGINEERING_BLOCKED', 'error': 'ENGINEERING_BLOCKED: ordinary analysis import error',
            'decisions': 0, 'model_calls': 551, 'bootstrap_invocations': 6,
            'prior_reserved_calls': 10, 'pending_job': {'job_id': 'JOB'},
            'blocker_repairs': {'preserved': {'attempts': 2}},
            'recovery_episodes': {'old': {'attempts': [1, 2]}},
            'autonomous_recovery_authority': {
                'revision_id': A.REVISION, 'path': A.AUTHORIZATION.as_posix(),
                'sha256': digest(base / 'AUTHORIZATION.json')},
        }
        self.c.profile = {'max_calls': 1000, 'campaign_stop_calls': 1000}
        self.c.model_call.side_effect = AssertionError('No model calls permitted in helper')

    def failed_lead(self, index=None, *, exit_code=1, reaped=True, raw='Selected model is at capacity'):
        index = self.c.state['decisions'] if index is None else index
        folder = self.runtime / 'decisions' / f'{index:03d}'
        call = folder / 'call_0'
        call.mkdir(parents=True)
        write(call / 'request.json', {'argv': ['codex', 'exec'], 'started_utc': str(index)})
        receipt = {'exit_code': exit_code}
        if reaped is not None:
            receipt['process_reaped'] = reaped
        write(call / 'receipt.json', receipt)
        (call / 'stdout.jsonl').write_text(raw, encoding='utf-8')
        self.c.state.update(error='Lead runtime unavailable; no worker fallback', status='PAUSED_LEAD')
        return folder, call

    def accepted(self, action='FIX_EXPERIMENT'):
        folder = self.runtime / 'decisions' / f"{self.c.state['decisions']:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'accepted.json'
        write(path, {'action': action, 'worker_dispatch_ready': action != 'PAUSE'})
        self.c.state.update(last_decision=path.relative_to(self.root).as_posix(),
                            decisions=self.c.state['decisions'] + 1)
        self.c.state['model_calls'] += 1
        return path

    def qa_report(self, generated=True, reaped=True, raw='Selected model is at capacity'):
        folder = self.runtime / 'qa' / 'qa-failed'
        call = folder / 'call_0'
        call.mkdir(parents=True)
        write(call / 'request.json', {'argv': ['codex', 'exec']})
        write(call / 'receipt.json', {'exit_code': 1, 'process_reaped': reaped})
        (call / 'stdout.jsonl').write_text(raw, encoding='utf-8')
        report = {'review_key': 'qa-failed', 'job_id': 'JOB', 'verdict': 'INCOMPLETE',
                  'summary': 'QA provider/session/output incomplete; inspect preserved call receipt',
                  'reviewed_files': [], 'facets': [], 'findings': [],
                  'limitations': ['Controller-generated failure envelope; no accepted model QA verdict.']
                  if generated else ['Source evidence cannot establish causality']}
        write(folder / 'report.json', report)
        self.c.state['latest_qa_review'] = dict(A._pin(folder / 'report.json'),
                                               verdict='INCOMPLETE')
        self.c.state['error'] = 'LEAD_PREREQUISITE: Independent QA failed before code acceptance'
        self.c.state['status'] = 'PAUSED_LEAD'
        return folder

    def test_authority_does_not_accept_boolean_as_zero_spend(self):
        path = self.root / A.AUTHORIZATION
        value = json.loads(path.read_text(encoding='utf-8'))
        value['spend_usd'] = False
        write(path, value)
        self.c.state['autonomous_recovery_authority']['sha256'] = digest(path)
        with self.assertRaisesRegex(ValueError, 'permission mismatch'):
            A.authority(self.c.state)

    def test_ordinary_recovery_retains_accounting_and_revokes_only_old_active_grant(self):
        self.c.state['worker_dispatch_decision'] = {'path': 'old', 'sha256': 'old'}
        before = copy.deepcopy(self.c.state)
        result = A.recover(self.c, now=0)
        self.assertEqual(result['action'], 'RESUME')
        for key in ('model_calls', 'bootstrap_invocations', 'prior_reserved_calls',
                    'blocker_repairs', 'recovery_episodes', 'pending_job'):
            self.assertEqual(self.c.state[key], before[key])
        self.assertNotIn('worker_dispatch_decision', self.c.state)
        self.assertTrue(self.c.state['autonomous_recovery']['review_required'])
        self.assertEqual(A.context(self.c.state)['prior_worker_dispatch_decision'], before['worker_dispatch_decision'])
        self.c.model_call.assert_not_called()

    def test_raw_engineering_exception_gets_lead_context(self):
        self.c.state['error'] = "name 'local_count' is not defined"
        self.assertEqual(A.recover(self.c)['action'], 'RESUME')

    def test_protected_error_and_unknown_human_action_never_become_engineering(self):
        for error in ('HUMAN_ACTION_REQUIRED: worker metadata scope violation',
                      'ENGINEERING_BLOCKED: permission denied',
                      'LEAD_PREREQUISITE: opening test_reserved is required',
                      'HUMAN_ACTION_REQUIRED: unknown ordinary issue',
                      'authorization hash mismatch'):
            with self.subTest(error=error):
                self.c.state['error'] = error
                before = copy.deepcopy(self.c.state)
                self.assertEqual(A.recover(self.c)['action'], 'STOP')
                self.assertEqual(self.c.state, before)

    def test_user_markers_and_terminal_statuses_preserved(self):
        for name in ('pause.request', 'cancel.request'):
            marker = self.runtime / name
            marker.write_text('user marker', encoding='utf-8')
            self.assertEqual(A.recover(self.c)['action'], 'STOP')
            self.assertEqual(marker.read_text(encoding='utf-8'), 'user marker')
            marker.unlink()
        for status in ('PAUSED_USER', 'READY_FOR_FORWARD', 'DATA_OR_RULES_BLOCKED', 'RESOURCE_LIMIT'):
            self.c.state['status'] = status
            self.assertEqual(A.recover(self.c)['action'], 'STOP')

    def test_nested_hold_inflight_and_cap_stop_without_counter_change(self):
        self.c.state['operator_safety_hold'] = {'active': False, 'prior_unreleased_hold': {'active': True}}
        self.assertEqual(A.recover(self.c)['action'], 'STOP')
        self.c.state.pop('operator_safety_hold')
        self.c.state['inflight_call'] = {'call': 'uncertain'}
        self.assertEqual(A.recover(self.c)['action'], 'STOP')
        self.c.state.pop('inflight_call')
        self.c.state['model_calls'] = 984
        self.assertEqual(A.recover(self.c)['action'], 'STOP')
        self.assertEqual(self.c.state['model_calls'], 984)

    def test_capacity_backoff_preserves_failed_folder_and_advances_once(self):
        folder, call = self.failed_lead()
        before = {path.name: path.read_bytes() for path in call.iterdir()}
        first = A.recover(self.c, now=100)
        self.assertEqual((first['action'], first['delay_seconds']), ('BACKOFF', 60))
        self.assertEqual(self.c.state['decisions'], 0)
        second = A.recover(self.c, now=161)
        self.assertEqual(second['action'], 'RESUME')
        self.assertEqual(self.c.state['decisions'], 1)
        self.assertEqual(before, {path.name: path.read_bytes() for path in call.iterdir()})
        self.assertEqual(A.recover(self.c, now=162)['action'], 'STOP')

    def test_pause_during_cooldown_prevents_new_review(self):
        self.failed_lead()
        self.assertEqual(A.recover(self.c, now=0)['action'], 'BACKOFF')
        (self.runtime / 'cancel.request').write_text('stop', encoding='utf-8')
        self.assertEqual(A.recover(self.c, now=999)['action'], 'STOP')
        self.assertEqual(self.c.state['decisions'], 0)

    def test_missing_reaped_or_receipt_and_unknown_interrupt_do_not_retry(self):
        for reaped, exit_code, raw in ((None, 1, 'Selected model is at capacity'),
                                      (False, 1, 'Selected model is at capacity'),
                                      (True, None, 'partial unknown stream')):
            with self.subTest(reaped=reaped, exit_code=exit_code):
                self.c.state['decisions'] += 1
                self.failed_lead(exit_code=exit_code, reaped=reaped, raw=raw)
                self.assertEqual(A.recover(self.c)['action'], 'STOP')

    def test_only_known_fully_reaped_timeout_without_completed_output_may_retry(self):
        self.failed_lead(exit_code=None, raw='CONTROLLER_INACTIVITY_STALL')
        self.assertEqual(A.recover(self.c, now=0)['action'], 'BACKOFF')
        self.assertEqual(A.recover(self.c, now=61)['action'], 'RESUME')
        self.failed_lead(exit_code=None, raw='CONTROLLER_TIMEOUT\nturn.completed')
        self.assertEqual(A.recover(self.c, now=62)['action'], 'STOP')

    def test_generic_provider_error_cannot_mask_authentication_failure(self):
        self.failed_lead(raw='login required: authentication expired')
        self.assertEqual(A.recover(self.c)['action'], 'STOP')

    def test_successful_output_is_not_discarded_for_provider_retry(self):
        folder, call = self.failed_lead(exit_code=0, raw='turn.completed')
        write(folder / 'decision_0.json', {'action': 'PAUSE'})
        self.assertEqual(A.recover(self.c)['action'], 'STOP')
        write(folder / 'rejection_0.json', {'error': 'schema missing required field'})
        self.c.state['error'] = 'Lead format/permission validation exhausted; proposal preserved'
        self.assertEqual(A.recover(self.c)['action'], 'RESUME')

    def test_schema_rejection_prose_cannot_look_like_a_new_protected_action(self):
        folder, call = self.failed_lead(exit_code=0,
            raw='turn.completed: live trading stays disabled, no payment is authorized')
        write(folder / 'decision_0.json', {'action': 'PAUSE'})
        write(folder / 'rejection_0.json', {'error': 'schema missing required field'})
        self.c.state['error'] = 'Lead format/permission validation exhausted; proposal preserved'
        self.assertEqual(A.recover(self.c)['action'], 'RESUME')

    def test_identical_orphan_incident_recovers_without_overwriting_it(self):
        before = copy.deepcopy(self.c.state)
        self.assertTrue(A.capture(self.c, self.c.state['error']))
        ref = self.c.state['autonomous_recovery']['current']
        path = self.root / ref['path']
        raw = path.read_bytes()
        self.c.state = before
        self.assertTrue(A.capture(self.c, self.c.state['error']))
        self.assertEqual(path.read_bytes(), raw)

    def test_orphan_with_changed_resume_index_is_not_adopted(self):
        before = copy.deepcopy(self.c.state)
        self.assertTrue(A.capture(self.c, self.c.state['error']))
        ref = self.c.state['autonomous_recovery']['current']
        path = self.root / ref['path']
        record = json.loads(path.read_text(encoding='utf-8'))
        record['next_decision_index'] = 100
        write(path, record)
        self.c.state = before
        self.assertEqual(A.recover(self.c)['action'], 'STOP')
        self.assertEqual(self.c.state['decisions'], 0)

    def test_receipt_tampering_during_backoff_fails_closed(self):
        folder, call = self.failed_lead()
        self.assertEqual(A.recover(self.c, now=0)['action'], 'BACKOFF')
        write(call / 'receipt.json', {'exit_code': 2, 'process_reaped': True})
        self.assertEqual(A.recover(self.c, now=61)['action'], 'STOP')
        self.assertEqual(len(self.c.state['autonomous_recovery']['history']), 1)

    def test_provider_attempts_remain_budget_bound_not_arbitrarily_capped(self):
        now = 0
        for attempt in range(8):
            self.failed_lead()
            result = A.recover(self.c, now=now)
            self.assertEqual(result['action'], 'BACKOFF')
            self.assertLessEqual(result['delay_seconds'], 1800)
            now += result['delay_seconds'] + 1
            self.assertEqual(A.recover(self.c, now=now)['action'], 'RESUME')
            self.c.state['model_calls'] += 1
        self.assertEqual(self.c.state['decisions'], 8)

    def test_repeated_prerequisite_waits_for_changed_evidence_not_new_wording(self):
        self.c.state.update(error='LEAD_PREREQUISITE: controller parser repair requires independent acceptance',
                            status='PAUSED_LEAD')
        original = self.c.state['error']
        self.assertEqual(A.recover(self.c, now=0)['action'], 'RESUME')
        self.c.state['latest_qa_review']={'path':'new-qa.json','sha256':'new-qa-pin'}
        accepted = self.accepted(action='PAUSE')
        self.assertTrue(A.complete_review(self.c, accepted))
        self.c.state.update(error=original+'; same question rephrased', status='PAUSED_LEAD')
        before=copy.deepcopy(self.c.state)
        self.assertEqual(A.recover(self.c, now=10)['action'], 'STOP')
        self.assertEqual(self.c.state,before)
        self.assertEqual(len(self.c.state['autonomous_recovery']['semantic_attempts']), 1)
        repair=self.root/A.BASE/'new_evidence.json'
        write(repair,{'changed_pinned_inputs':True})
        self.c.state['pending_lead_evidence']=A._pin(repair)
        self.assertEqual(A.recover(self.c,now=11)['action'],'RESUME')

    def test_released_hold_accepted_lead_prerequisite_yields_for_maintenance(self):
        from tools.quant_research_loop import safety_hold as H
        accepted=self.accepted(action='PAUSE')
        self.c.state.update(status='PAUSED_LEAD',error='LEAD_PREREQUISITE: saved proof transport needs repair',
                            pending_job=None,operator_safety_hold={'active':False},
                            lead_delegated_recovery_authority={'path':'r26.json','sha256':'authority'},
                            worker_dispatch_decision=A._pin(accepted))
        before=copy.deepcopy(self.c.state)
        with patch.object(H,'delegated_authority',return_value={'approved':True}):
            self.assertEqual(A.recover(self.c)['action'],'MAINTENANCE_REQUIRED')
            self.assertEqual(A.recover(self.c)['action'],'MAINTENANCE_REQUIRED')
        handoff=self.c.state.pop('maintenance_handoff')
        self.assertEqual(self.c.state,before)
        self.assertEqual(handoff['accepted_decision'],A._pin(accepted))
        self.assertFalse(handoff['worker_dispatch_authorized'])
        self.c.model_call.assert_not_called()

    def test_lead_maintenance_handoff_requires_current_pause_identity_and_no_grant(self):
        from tools.quant_research_loop import safety_hold as H
        accepted=self.accepted(action='PAUSE')
        self.c.state.update(status='PAUSED_LEAD',error='LEAD_PREREQUISITE: internal maintenance',
                            pending_job=None,lead_delegated_recovery_authority={'path':'r26.json','sha256':'authority'},
                            worker_dispatch_decision=A._pin(accepted))
        original=copy.deepcopy(self.c.state)
        for fields in ({'last_decision':'old/accepted.json'},
                       {'inflight_call':{'path':'uncertain'}},
                       {'status':'PAUSED_USER'},
                       {'error':'LEAD_PREREQUISITE: test_reserved requires access'},
                       {'pending_job':{'job_id':'worker'}}):
            with self.subTest(fields=fields),patch.object(H,'delegated_authority',return_value={'approved':True}):
                self.c.state={**copy.deepcopy(original),**fields}
                self.assertEqual(A.recover(self.c)['action'],'STOP')
                self.assertNotIn('maintenance_handoff',self.c.state)
        self.c.state=copy.deepcopy(original)
        write(accepted,{'action':'PAUSE','next_job':None,'worker_dispatch_ready':True})
        self.c.state['worker_dispatch_decision']=A._pin(accepted)
        with patch.object(H,'delegated_authority',return_value={'approved':True}):
            self.assertEqual(A.recover(self.c)['action'],'STOP')
        self.assertNotIn('maintenance_handoff',self.c.state)

    def test_completion_rejects_old_decision_and_pins_only_fresh_acceptance(self):
        self.assertEqual(A.recover(self.c)['action'], 'RESUME')
        old = self.runtime / 'decisions' / '999' / 'accepted.json'
        old.parent.mkdir(parents=True)
        write(old, {})
        self.c.state['last_decision'] = old.relative_to(self.root).as_posix()
        with self.assertRaisesRegex(ValueError, 'fresh accepted'):
            A.complete_review(self.c, old)
        current = self.accepted()
        self.assertTrue(A.complete_review(self.c, current))
        self.assertIsNone(A.context(self.c.state))

    def test_missing_source_and_outside_authorization_fail_closed(self):
        self.c.state['autonomous_recovery_authority']['path'] = '../AUTHORIZATION.json'
        self.assertEqual(A.recover(self.c)['action'], 'STOP')

    def test_evidence_cannot_read_datasets_or_escape_workspace(self):
        dataset = self.root / 'dataset' / 'test_reserved.csv'
        dataset.parent.mkdir()
        dataset.write_text('not read as evidence', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'engineering scope'):
            A.capture(self.c, self.c.state['error'], evidence_refs=['dataset/test_reserved.csv'])
        with self.assertRaisesRegex(ValueError, 'workspace file'):
            A.capture(self.c, self.c.state['error'], evidence_refs=['../outside.json'])

    def test_generated_failed_qa_receipt_gets_one_new_cache_generation(self):
        folder = self.qa_report()
        before = (folder / 'report.json').read_bytes()
        self.assertEqual(A.recover(self.c, now=0)['action'], 'BACKOFF')
        self.assertIsNone(A.qa_retry_context(self.c.state, 'JOB'))
        self.assertEqual(A.recover(self.c, now=61)['action'], 'RESUME')
        retry = A.qa_retry_context(self.c.state, 'JOB')
        self.assertEqual(retry['failed_review_key'], 'qa-failed')
        self.assertEqual((folder / 'report.json').read_bytes(), before)
        self.assertIsNone(A.qa_retry_context(self.c.state, 'OTHER'))
        self.assertEqual(self.c.state['decisions'], 0)

    def test_actual_model_incomplete_never_gets_qa_cache_retry_generation(self):
        self.qa_report(generated=False)
        self.assertEqual(A.recover(self.c, now=0)['action'], 'RESUME')
        self.assertIsNone(A.qa_retry_context(self.c.state, 'JOB'))

    def test_uncertain_qa_invocation_or_mutated_retry_receipt_stops(self):
        folder = self.qa_report(reaped=False)
        self.assertEqual(A.recover(self.c, now=0)['action'], 'STOP')
        write(folder / 'call_0' / 'receipt.json', {'exit_code': 1, 'process_reaped': True})
        self.assertEqual(A.recover(self.c, now=1)['action'], 'BACKOFF')
        self.assertEqual(A.recover(self.c, now=62)['action'], 'RESUME')
        write(folder / 'call_0' / 'receipt.json', {'exit_code': 2, 'process_reaped': True})
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            A.qa_retry_context(self.c.state, 'JOB')

    def test_qa_generation_cannot_change_without_a_new_preserved_incident(self):
        self.qa_report()
        self.assertEqual(A.recover(self.c, now=0)['action'], 'BACKOFF')
        self.assertEqual(A.recover(self.c, now=61)['action'], 'RESUME')
        self.c.state['autonomous_recovery']['qa_retries']['JOB']['generation'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'generation does not match'):
            A.qa_retry_context(self.c.state, 'JOB')


class DelegatedTransportTests(unittest.TestCase):
    def test_failed_incident_qa_uses_authenticated_recovery_instead_of_blind_resume(self):
        from tools.quant_research_loop import safety_hold as H
        c=Mock();c.state={'status':'ENGINEERING_BLOCKED','error':'QA provider/session/output incomplete'}
        with patch.object(A,'_gate',return_value=None),patch.object(H,'delegated_review_allowed',return_value=True),\
                patch.object(A,'_qa_failure',return_value={'category':'TRANSPORT'}),patch.object(A,'capture',return_value=False):
            self.assertEqual(A.recover(c)['action'],'STOP')
        c.save.assert_not_called()
        c.model_call.assert_not_called()

    def test_unchanged_failed_release_waits_without_repeating_review(self):
        from tools.quant_research_loop import safety_hold as H
        packet={'path':'repair.json','sha256':'same'};qa={'path':'qa.json','sha256':'same'}
        c=Mock();c.state={'status':'ENGINEERING_BLOCKED','delegated_incident_review':packet,
                         'latest_qa_review':qa,'delegated_incident_wait':{'repair_packet':packet,'qa':qa}}
        with patch.object(A,'_gate',return_value=None),patch.object(H,'delegated_review_allowed',return_value=True),\
                patch.object(A,'_qa_failure',return_value=None):
            result=A.recover(c)
        self.assertEqual(result['action'],'MAINTENANCE_REQUIRED')
        self.assertEqual(c.state['status'],'ENGINEERING_BLOCKED')
        self.assertEqual(c.state['maintenance_handoff']['repair_packet'],packet)
        self.assertEqual(c.state['maintenance_handoff']['qa'],qa)
        self.assertFalse(c.state['maintenance_handoff']['worker_dispatch_authorized'])
        c.save.assert_called_once_with()
        c.model_call.assert_not_called()

    def test_changed_delegated_evidence_returns_to_existing_review_and_clears_handoff(self):
        from tools.quant_research_loop import safety_hold as H
        packet={'path':'repair.json','sha256':'new'}
        c=Mock();c.state={'status':'ENGINEERING_BLOCKED','delegated_incident_review':packet,
                         'latest_qa_review':None,'delegated_incident_wait':{'repair_packet':{},'qa':None},
                         'maintenance_handoff':{'action':'MAINTENANCE_REQUIRED'}}
        with patch.object(A,'_gate',return_value=None),patch.object(H,'delegated_review_allowed',return_value=True),\
                patch.object(A,'_qa_failure',return_value=None):
            result=A.recover(c)
        self.assertEqual(result['action'],'RESUME')
        self.assertNotIn('maintenance_handoff',c.state)
        self.assertTrue(c.state['delegated_hold_review_required'])
        c.model_call.assert_not_called()


if __name__ == '__main__':
    unittest.main()

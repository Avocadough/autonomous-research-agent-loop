import copy
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from quant_research_loop.controller import validate_decision, writer_lock, Controller, snapshot
from quant_research_loop import controller as C
from quant_research_loop.prepare import digest
from quant_research_loop.gate_zero import write


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.state=dict(evidence_id='NEGATIVE_VALID_J1',completed={'J1':{}},families={},
                        qualification_verified=False)
        self.profile=dict(jobs=[dict(job_id='J1',candidate_id='A'),dict(job_id='J2',candidate_id='B')])
        self.decision=dict(campaign_id='PAYOUT_V1',evidence_id='NEGATIVE_VALID_J1',
            action='RUN_BASELINE',rationale='Valid negative result; distinct next registered candidate',
            artifact_refs=['result.json'],next_job={'job_id':'J2'},missing_information=[],pause_category='NONE',
            worker_dispatch_ready=True)

    def test_valid_negative_result_allows_next_registered_job(self):
        self.assertEqual(validate_decision(self.decision,self.state,self.profile)['next_job']['job_id'],'J2')

    def test_stale_evidence_rejected(self):
        self.decision['evidence_id']='OLD'
        with self.assertRaises(ValueError):validate_decision(self.decision,self.state,self.profile)

    def test_delegated_incident_release_requires_pass_and_exact_refs(self):
        self.state.update(operator_safety_hold={'incident':'incident.json'},
            delegated_incident_review={'path':'repair.json','sha256':'repair'},
            latest_qa_review={'path':'qa.json','verdict':'PASS','incident_review':True,
                              'repair_packet':{'path':'repair.json','sha256':'repair'}})
        self.decision.update(action='RELEASE_REPAIRED_HOLD',next_job=None,worker_dispatch_ready=False,
                             artifact_refs=['incident.json','repair.json','qa.json'])
        # No real authority or runtime files: only this schema boundary is exercised.
        from quant_research_loop import safety_hold as H
        with patch.object(H,'delegated_review_allowed',return_value=True):
            self.assertEqual(validate_decision(self.decision,self.state,self.profile)['action'],'RELEASE_REPAIRED_HOLD')
            self.state['latest_qa_review']['verdict']='INCOMPLETE'
            with self.assertRaisesRegex(ValueError,'current incident QA PASS'):
                validate_decision(self.decision,self.state,self.profile)
            self.state['latest_qa_review']['verdict']='PASS'
            self.decision.update(next_job={'job_id':'J2'},worker_dispatch_ready=True)
            with self.assertRaisesRegex(ValueError,'cannot dispatch a worker'):
                validate_decision(self.decision,self.state,self.profile)

    def test_r25_rejects_code_recipe_but_accepts_brief_outcome(self):
        from quant_research_loop.model_routing import LEAN_REVISION
        profile={'model_route_authority':{'revision_id':LEAN_REVISION}}
        job={'job_class':'DIAGNOSTIC','objective':'Reproduce the reported cash-risk mismatch and report evidence.'}
        C._lean_work_order({'next_job':job},profile)
        for objective in ('```python\ndef run(ctx): pass\n```', 'def run(ctx):\n    return {}', 'x'*2201):
            with self.subTest(objective=objective[:30]), self.assertRaisesRegex(ValueError,'Lean handoff'):
                C._lean_work_order({'next_job':dict(job,objective=objective)},profile)

    def test_actual_r25_lead_prompt_preserves_exact_generic_contract(self):
        from quant_research_loop import generic_jobs as G, qa_review as Q, skill_engineering as S
        from quant_research_loop import runtime_policy as P
        from quant_research_loop.model_routing import LEAN_REVISION
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);runtime=root/'runtime'
            write(root/'rules.json',{'protected_data':False})
            controller=Controller.__new__(Controller)
            controller.state=dict(evidence_id='INVENTORY',completed={},decisions=0,model_calls=0,
                bootstrap_invocations=0,prior_reserved_calls=0,families={},active_invocation_s=0)
            controller.profile=dict(lead='gpt-6.1-sol',reasoning='ultra',worker='gpt-6.1-sol',
                worker_variant='medium',jobs=[{'job_id':'INITIAL'}],rules_path='rules.json',
                max_calls=1000,max_active_seconds=21600,
                model_route_authority={'revision_id':LEAN_REVISION})
            controller.save=lambda:None
            prompts=[]

            def model_call(command,prompt,folder,**kwargs):
                prompts.append(prompt)
                write(Path(command[command.index('-o')+1]),dict(campaign_id='PAYOUT_V1',
                    evidence_id='INVENTORY',action='PAUSE',rationale='Offline prompt contract fixture.',
                    artifact_refs=[(folder.parent/'evidence.json').relative_to(root).as_posix()],
                    next_job=None,worker_dispatch_ready=False,missing_information=[],pause_category='PAUSED_LEAD'))
                return {'exit_code':0},''

            controller.model_call=model_call
            with patch.object(C,'ROOT',root),patch.object(C,'RUNTIME',runtime),\
                    patch.object(C,'native_command',return_value=['codex']),\
                    patch.object(C,'_supplemental_audit',return_value=None),\
                    patch.object(G,'authorized',return_value=True),patch.object(G,'authority_catalog',return_value={}),\
                    patch.object(G,'accounted_seconds',return_value=0),patch.object(Q,'review',return_value=None),\
                    patch.object(S,'maybe_review'),patch.object(P,'cumulative_runtime_cap_bypassed',return_value=True):
                self.assertEqual(controller.lead()['action'],'PAUSE')
            self.assertEqual(len(prompts),1)
            prompt=prompts[0]
            self.assertIn('prohibited_actions must be EXACTLY '+json.dumps(G.PROHIBITED),prompt)
            self.assertIn('with no extra entries; put job-specific restrictions in objective/verification',prompt)
            self.assertIn('permissions={"network":"none","spend_usd":0,"live":false}',prompt)
            for invariant in ('Every new contract includes risk_ceiling_usd','allowed_data equals exactly those input paths',
                              'allowed_file_scope and required_outputs each contain exactly',
                              'verification.method=reproduce_json','min_records checks payload.records',
                              'implementation.write_paths are exact tools/NAME.py','pin existing source as RESEARCH_CODE',
                              'test_modules are tools.test_NAME, each pinned or authored in write_paths',
                              'include write_paths in both allowed_file_scope and required_outputs',
                              'gross_pnl,commission,slippage_cost,net_pnl,planned_risk_usd,instrument'):
                self.assertIn(invariant,prompt)

    def test_r25_preserves_frozen_reference_and_historical_long_objective(self):
        from quant_research_loop.model_routing import LEAN_REVISION
        profile={'model_route_authority':{'revision_id':LEAN_REVISION}}
        C._lean_work_order({'next_job':{'job_id':'FROZEN'}},profile)
        C._lean_work_order({'next_job':{'job_class':'DIAGNOSTIC','objective':'x'*5000}}, {})
        with self.assertRaisesRegex(ValueError,'changed_approach'):
            C._lean_work_order({'next_job':{'job_id':'FROZEN','recovery_episode':{'changed_approach':'x'*1201}}},profile)

    def test_r25_cannot_fall_back_to_legacy_executor(self):
        from quant_research_loop.model_routing import LEAN_REVISION
        self.profile['model_route_authority']={'revision_id':LEAN_REVISION}
        with self.assertRaisesRegex(ValueError,'authorized Sol executor route'):
            validate_decision(self.decision,self.state,self.profile)

    def test_completed_job_cannot_double_count(self):
        self.decision['next_job']['job_id']='J1'
        with self.assertRaises(ValueError):validate_decision(self.decision,self.state,self.profile)

    def test_unregistered_or_path_traversal_job_refused(self):
        self.decision['next_job']['job_id']='../../dataset/test_reserved'
        with self.assertRaises(ValueError):validate_decision(self.decision,self.state,self.profile)

    def test_model_cannot_relax_external_forward_gates(self):
        self.decision.update(action='READY_FOR_FORWARD',next_job=None,worker_dispatch_ready=False)
        with self.assertRaises(ValueError):validate_decision(self.decision,self.state,self.profile)

    def test_conditional_worker_dispatch_requires_pause(self):
        self.decision.update(action='FIX_EXPERIMENT', worker_dispatch_ready=False,
                             missing_information=['Trusted maintenance and independent acceptance first'])
        with self.assertRaisesRegex(ValueError, 'prerequisites require PAUSE'):
            validate_decision(self.decision, self.state, self.profile)
        self.decision.update(action='PAUSE', next_job=None, pause_category='PAUSED_LEAD')
        self.assertEqual(validate_decision(self.decision, self.state, self.profile)['action'], 'PAUSE')

    def test_legacy_decision_cannot_silently_authorize_a_new_worker(self):
        self.decision.pop('worker_dispatch_ready')
        with self.assertRaises(Exception):
            validate_decision(self.decision, self.state, self.profile)

    def test_worker_dispatch_requires_current_pinned_explicit_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / 'runtime'
            accepted = runtime / 'decisions/001/accepted.json'
            accepted.parent.mkdir(parents=True)
            write(accepted, self.decision)
            path = accepted.relative_to(root).as_posix()
            state = {'last_decision': path,
                     'worker_dispatch_decision': {'path': path, 'sha256': digest(accepted)}}
            with patch.object(C, 'ROOT', root), patch.object(C, 'RUNTIME', runtime):
                C.guard_lead_dispatch(state, {'job_id': 'J2'})
                for bad in ({}, {'last_decision': 'wrong', 'worker_dispatch_decision': state['worker_dispatch_decision']},
                            dict(state, worker_dispatch_decision={'path': path, 'sha256': '0' * 64})):
                    with self.subTest(state=bad), self.assertRaisesRegex(RuntimeError, 'LEAD_DISPATCH_BLOCKED'):
                        C.guard_lead_dispatch(bad, {'job_id': 'J2'})
                with self.assertRaisesRegex(RuntimeError, 'LEAD_DISPATCH_BLOCKED'):
                    C.guard_lead_dispatch(state, {'job_id': 'other'})
                self.decision['worker_dispatch_ready'] = False
                write(accepted, self.decision)
                state['worker_dispatch_decision']['sha256'] = digest(accepted)
                with self.assertRaisesRegex(RuntimeError, 'LEAD_DISPATCH_BLOCKED'):
                    C.guard_lead_dispatch(state, {'job_id': 'J2'})

    def test_blocked_worker_does_not_reserve_or_execute(self):
        c = object.__new__(Controller)
        c.state = {'status': 'RUNNING', 'model_calls': 567, 'trials': 9}
        c.save = lambda: None
        with patch.object(C, 'guard_lead_dispatch', side_effect=RuntimeError('LEAD_DISPATCH_BLOCKED')):
            with self.assertRaisesRegex(RuntimeError, 'LEAD_DISPATCH_BLOCKED'):
                c.worker({'job_id': 'J2'})
        self.assertEqual(c.state, {'status': 'ENGINEERING_BLOCKED', 'model_calls': 567, 'trials': 9})

    def test_missing_decision_not_keyword_pass(self):
        with self.assertRaises(Exception):validate_decision({'text':'PASS'},self.state,self.profile)

    def test_family_limit(self):
        self.state['families']={str(i):1 for i in range(6)}
        with self.assertRaises(ValueError):validate_decision(self.decision,self.state,self.profile)

    def test_variant_limit(self):
        self.state['families']={'B':3}
        with self.assertRaises(ValueError):validate_decision(self.decision,self.state,self.profile)

    def test_single_writer_and_release(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'lock'
            with writer_lock(path):
                with self.assertRaises(RuntimeError):
                    with writer_lock(path):pass
            with writer_lock(path):pass

    def test_atomic_json(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json'
            write(path,{'pending_job':'J2'})
            write(path,{'completed':['J2']})
            self.assertIn('completed',path.read_text())
            self.assertFalse(path.with_suffix('.json.tmp').exists())

    def test_budget_reserves_bootstrap_and_old_calls(self):
        c=object.__new__(Controller)
        c.profile=dict(max_calls=80,max_active_seconds=21600,max_configs=18)
        c.state=dict(model_calls=69,bootstrap_invocations=1,prior_reserved_calls=10,
                     active_invocation_s=0,prior_reserved_seconds=0,trials=0)
        c.save=lambda:None
        with self.assertRaises(RuntimeError):c.check_budget()
        self.assertEqual(c.state['status'],'PAUSED_BUDGET')

    def test_blocked_resume_does_not_repeat_lead_or_worker(self):
        c=object.__new__(Controller); c.state={'status':'DATA_OR_RULES_BLOCKED'}
        c.lead=lambda: self.fail('Must not invoke Lead again')
        self.assertEqual(c.run(resume=True)['status'],'DATA_OR_RULES_BLOCKED')

    def test_resume_preserves_operator_pause_and_cancel_without_calls(self):
        for name in ('pause.request', 'cancel.request'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                runtime = Path(tmp)
                marker = runtime / name
                marker.write_text('operator', encoding='utf-8')
                c = object.__new__(Controller)
                c.state = {'status': 'PAUSED_LEAD', 'model_calls': 567}
                c.save = lambda: None
                c.lead = lambda: self.fail('Operator stop must precede any Lead call')
                with patch.object(C, 'RUNTIME', runtime):
                    self.assertEqual(c.run(resume=True)['status'], 'PAUSED_USER')
                self.assertEqual(marker.read_text(), 'operator')
                self.assertEqual(c.state['model_calls'], 567)

    def test_cancel_before_model_dispatch_is_uncharged(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / 'cancel.request').write_text('operator', encoding='utf-8')
            c = object.__new__(Controller)
            c.state = {'status': 'RUNNING', 'model_calls': 567}
            c.save = lambda: None
            with patch.object(C, 'RUNTIME', runtime), self.assertRaisesRegex(RuntimeError, 'PAUSED_USER'):
                c.model_call([], '', runtime / 'call')
            self.assertEqual(c.state['model_calls'], 567)

    def timing_controller(self, root):
        c = object.__new__(Controller)
        c.profile = dict(max_calls=1000, max_active_seconds=21600, max_configs=3000)
        c.state = dict(status='RUNNING', model_calls=1, bootstrap_invocations=0,
                       prior_reserved_calls=0, active_invocation_s=0, prior_reserved_seconds=0,
                       trials=0, runtime_policy={'inactivity_stall_seconds': 3600})
        c.state_path = root / 'state.json'
        return c

    def test_future_call_timing_covers_all_dispatch_paths(self):
        for route in ('invoke', 'cwd', 'native'):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                c = self.timing_controller(root)
                folder = root / 'call'
                command = ['fixture', '-']

                def native_invoke(prompt, target, **kwargs):
                    write(target / 'request.json', {'argv': command, 'transport': 'native_app_server'})
                    return {'exit_code': 0, 'elapsed_s': 2.5, 'process_reaped': True}, 'terminal data'

                native = SimpleNamespace(invoke=native_invoke) if route == 'native' else None
                with patch.object(C, 'ROOT', root), patch.object(C, 'RUNTIME', root), \
                     patch.object(C.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='terminal data', stderr='')), \
                     patch.object(C.time, 'monotonic', side_effect=[10, 12.5]):
                    result, raw = c.model_call(command, 'bounded fixture', folder,
                        cwd=None if route == 'invoke' else root, native_session=native)
                self.assertEqual(raw, 'terminal data')
                self.assertEqual(result['elapsed_s'], 2.5)
                self.assertEqual(result['exit_code'], 0)
                self.assertEqual(result['timing_basis'], 'observed_utc_with_monotonic_elapsed')
                self.assertIsNotNone(C.dt.datetime.fromisoformat(result['started_utc']).tzinfo)
                self.assertIsNotNone(C.dt.datetime.fromisoformat(result['finished_utc']).tzinfo)
                self.assertEqual(json.loads((folder / 'receipt.json').read_text()), result)
                self.assertEqual(json.loads((folder / 'request.json').read_text())['argv'], command)
                self.assertEqual(json.loads(c.state_path.read_text())['model_calls'], 2)
                self.assertNotIn('inflight_call', c.state)

    def test_call_timing_preserves_failure_timeout_and_unreconciled_crash(self):
        for outcome in ('nonzero', 'timeout', 'crash'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                c = self.timing_controller(root)
                folder = root / 'call'
                effect = (C.subprocess.TimeoutExpired(['fixture'], 1, output=b'partial', stderr=b'late')
                          if outcome == 'timeout' else RuntimeError('transport lost') if outcome == 'crash' else None)
                with patch.object(C, 'ROOT', root), patch.object(C, 'RUNTIME', root), \
                     patch.object(C.subprocess, 'run', side_effect=effect,
                                  return_value=SimpleNamespace(returncode=2, stdout='failed', stderr='error')), \
                     patch.object(C.time, 'monotonic', side_effect=[10, 13]):
                    if outcome == 'crash':
                        with self.assertRaisesRegex(RuntimeError, 'transport lost'):
                            c.model_call(['fixture', '-'], 'fixture', folder, cwd=root)
                        self.assertFalse((folder / 'receipt.json').exists())
                        self.assertEqual(c.state['inflight_call'], 'call')
                    else:
                        result, raw = c.model_call(['fixture', '-'], 'fixture', folder, cwd=root)
                        self.assertEqual(result['exit_code'], None if outcome == 'timeout' else 2)
                        self.assertEqual(result['elapsed_s'], 3)
                        self.assertIn('finished_utc', result)
                        self.assertNotIn('SUCCESS', result.values())
                        self.assertEqual(raw, 'partial' if outcome == 'timeout' else 'failed')
                self.assertEqual(c.state['model_calls'], 2)

    def test_wall_clock_reversal_does_not_replace_monotonic_duration(self):
        start = C.dt.datetime(2026, 10, 8, 10, tzinfo=C.dt.timezone.utc)
        finish = C.dt.datetime(2026, 10, 8, 9, tzinfo=C.dt.timezone.utc)
        times = iter((start, finish))

        class BackwardsClock(C.dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return next(times)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = self.timing_controller(root)
            with patch.object(C, 'ROOT', root), patch.object(C, 'RUNTIME', root), \
                 patch.object(C.dt, 'datetime', BackwardsClock), \
                 patch.object(C.time, 'monotonic', side_effect=[10, 12.5]), \
                 patch.object(C.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')):
                result, _ = c.model_call(['fixture', '-'], '', root / 'call', cwd=root)
            self.assertEqual(result['started_utc'], '2026-10-08T10:00:00+00:00')
            self.assertEqual(result['finished_utc'], '2026-10-08T09:00:00+00:00')
            self.assertEqual(result['elapsed_s'], 2.5)
            self.assertEqual(c.state['active_invocation_s'], 2.5)

    def test_exhausted_cap_does_not_dispatch_or_charge_for_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = self.timing_controller(root)
            c.state['model_calls'] = 1000
            with patch.object(C, 'ROOT', root), patch.object(C, 'RUNTIME', root):
                with self.assertRaisesRegex(RuntimeError, 'PAUSED_BUDGET'):
                    c.model_call(['fixture', '-'], '', root / 'call', cwd=root)
            self.assertEqual(c.state['model_calls'], 1000)
            self.assertFalse((root / 'call').exists())

    def test_bootstrap_fix_limit_cannot_reset_on_resume(self):
        c=object.__new__(Controller)
        c.profile=dict(max_calls=80,max_active_seconds=21600,max_configs=18,max_integration_fixes=3)
        c.state=dict(model_calls=8,bootstrap_invocations=1,prior_reserved_calls=10,
                     active_invocation_s=216,prior_reserved_seconds=3600,trials=0,integration_fix_cycles=3)
        c.save=lambda:None
        with self.assertRaisesRegex(RuntimeError,'ENGINEERING_FIX_ALLOWANCE_EXHAUSTED'):c.check_budget()
        self.assertEqual(c.state['status'],'ENGINEERING_BLOCKED')

    def test_scope_hashes_detect_existing_untracked_edit_and_deletion(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            subprocess.run(['git','init','-q',d],check=True,capture_output=True)
            p=root/'untracked ไทย.txt';p.write_text('before',encoding='utf-8')
            first=snapshot(root)
            p.write_text('after',encoding='utf-8')
            second=snapshot(root)
            self.assertNotEqual(first,second)
            p.unlink()
            self.assertNotEqual(second,snapshot(root))


if __name__=='__main__':unittest.main()

"""Offline tests: transport diagnostics cannot become QA or dispatch authority."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tools.quant_research_loop import controller as C, qa_review as Q
from tools.quant_research_loop import safety_hold as H, skill_engineering as S
from tools.quant_research_loop import generic_jobs as G, runtime_policy as P
from tools.quant_research_loop.gate_zero import write


class PreflightTests(unittest.TestCase):
    def bundle(self):
        return {'review_key': 'blocked', 'job_id': 'J1', 'files': {
            'result.json': {'sha256': 'result-pin', 'truncated': True,
                            'complete_content_chars': 1925133, 'content': 'excerpt'}},
            'required_complete_files': ['result.json'], 'deterministically_verified': True}

    def test_preflight_is_diagnostic_only_and_keeps_complete_size(self):
        error = Q.RequiredEvidenceIncomplete(self.bundle(), ['result.json'])
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(error.diagnostic['required_files']['result.json']['complete_content_chars'], 1925133)
        self.assertNotIn('content', error.diagnostic['required_files']['result.json'])
        self.assertFalse(error.diagnostic['independent_qa_accepted'])
        self.assertFalse(error.diagnostic['worker_dispatch_authorized'])
        self.assertIn('LEAD_PREREQUISITE:', str(error))

    def test_preflight_decision_allows_only_non_dispatching_maintenance_pause(self):
        good = dict(action='PAUSE', next_job=None, worker_dispatch_ready=False,
                    pause_category='PAUSED_LEAD')
        Q.validate_preflight_decision(good)
        for changed in ({'action': 'READY_FOR_FORWARD'}, {'next_job': {'job_id': 'J2'}},
                        {'worker_dispatch_ready': True}, {'pause_category': 'DATA_OR_RULES_BLOCKED'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                Q.validate_preflight_decision(dict(good, **changed))

    def test_actual_lead_receives_preflight_without_accepting_result_or_using_old_qa(self):
        self.check_actual_lead_scoped_review(False)

    def test_actual_lead_accepts_maintenance_pause_with_null_job_without_retry(self):
        self.check_actual_lead_scoped_review(True)

    def check_actual_lead_scoped_review(self, maintenance):
        from tools.quant_research_loop.model_routing import LEAN_REVISION
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root=Path(tmp);runtime=root/'runtime'
            write(root/'rules.json', {'protected_data': False})
            write(root/'result.json', {'payload': {'trades': [], 'trade_count': 0}})
            ctl=C.Controller.__new__(C.Controller)
            ctl.state=dict(evidence_id='J1',completed={'J1': {'path':'result.json','sha256':'result-pin'}},
                decisions=0, model_calls=627,bootstrap_invocations=0,prior_reserved_calls=0,
                families={},active_invocation_s=0,
                latest_qa_review={'verdict':'PASS','job_id':'OLDER_INCIDENT'})
            ctl.profile=dict(lead='gpt-6.1-sol',reasoning='ultra',worker='gpt-6.1-sol',
                worker_variant='medium',jobs=[],rules_path='rules.json',max_calls=1000,
                max_active_seconds=21600,model_route_authority={'revision_id':LEAN_REVISION})
            ctl.save=lambda:None
            before=copy.deepcopy(ctl.state)
            error=Q.RequiredEvidenceIncomplete(self.bundle(), ['result.json'])
            prompts=[]
            def model_call(command,prompt,folder,**kwargs):
                prompts.append(prompt)
                schema=json.loads((folder.parent/'schema.json').read_text())
                self.assertEqual(schema['properties']['action']['enum'], ['PAUSE'])
                self.assertEqual(schema['properties']['next_job'], {'type':'null'})
                write(Path(command[command.index('-o')+1]), dict(campaign_id='PAYOUT_V1',
                    evidence_id='J1',action='PAUSE',rationale='Provide complete bounded transport.',
                    artifact_refs=[(folder.parent/'evidence.json').relative_to(root).as_posix()],
                    next_job=None,worker_dispatch_ready=False,missing_information=['Exact repair outcome'],
                    pause_category='PAUSED_LEAD'))
                return {'exit_code':0}, ''
            ctl.model_call=model_call
            for obj,name,value in [(C,'ROOT',root),(C,'RUNTIME',runtime),(Q,'ROOT',root)]:
                stack.enter_context(patch.object(obj,name,value))
            for obj,name,kwargs in [
                (C,'native_command',{'return_value':['codex']}),
                (C,'_supplemental_audit',{'return_value':None}),
                (G,'authorized',{'return_value':True}), (G,'authority_catalog',{'return_value':{}}),
                (G,'accounted_seconds',{'return_value':0}),
                (Q,'review',{'return_value':{'verdict':'CHANGES_REQUIRED','job_id':'J1',
                    'review_key':'maintenance','partial_review':True,'maintenance_review':True,
                    'report':{'limitations':[]}}} if maintenance else {'side_effect':error}),
                (H,'delegated_authority',{'return_value':{'approved':True}}),
                (S,'maybe_review',{}), (P,'cumulative_runtime_cap_bypassed',{'return_value':True})]:
                stack.enter_context(patch.object(obj,name,**kwargs))
            self.assertEqual(ctl.lead()['action'], 'PAUSE')
            evidence=json.loads((runtime/'decisions/000/evidence.json').read_text())
            self.assertIn('independent_sol_qa' if maintenance else 'qa_transport_preflight',evidence)
            self.assertNotIn('qa_transport_preflight' if maintenance else 'independent_sol_qa',evidence)
            self.assertEqual(ctl.state['completed'],before['completed'])
            self.assertNotIn('reviewed_results',ctl.state)
            self.assertEqual(ctl.state['latest_qa_review'],before['latest_qa_review'])
            self.assertEqual(ctl.state['model_calls'],627)
            self.assertIsNone(ctl.state['pending_job'])
            if not maintenance:
                self.assertFalse(ctl.state['qa_transport_preflight_review']['worker_dispatch_authorized'])
                self.assertIn('not an independent QA verdict',prompts[0])
                with self.assertRaisesRegex(RuntimeError,'unchanged QA transport'):
                    ctl.lead()
            self.assertEqual(len(prompts),1)

    def test_handoff_respects_stop_hold_inflight_and_delegation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(Q,'ROOT',Path(tmp)):
            runtime=Path(tmp)/'research/campaigns/PAYOUT_V1/runtime';runtime.mkdir(parents=True)
            error=Q.RequiredEvidenceIncomplete(self.bundle(), ['result.json'])
            for state in ({'inflight_call':{'role':'worker'}}, {'operator_safety_hold':{'active':True}},
                          {'status':'PAUSED_USER'}):
                with patch.object(H,'delegated_authority',return_value={'approved':True}):
                    with self.assertRaises(RuntimeError):Q.preflight_for_lead(state,error)
            with patch.object(H,'delegated_authority',return_value=None):
                with self.assertRaises(RuntimeError):Q.preflight_for_lead({},error)
            (runtime/'pause.request').write_text('user stop')
            with patch.object(H,'delegated_authority',return_value={'approved':True}):
                with self.assertRaises(RuntimeError):Q.preflight_for_lead({},error)
            self.assertEqual((runtime/'pause.request').read_text(),'user stop')


if __name__ == '__main__':
    unittest.main()

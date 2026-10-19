"""Synthetic recovery proofs only; never touch the campaign or dispatch models."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.quant_research_loop import safety_hold as H, generic_jobs as G
from tools.quant_research_loop import generic_worker as W, controller as C
from tools.quant_research_loop.gate_zero import write as atomic_write
from tools.quant_research_loop.prepare import digest


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    atomic_write(path,value)


class SafetyHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for module in (H,G,W,C):
            p=patch.object(module,'ROOT',self.root);p.start();self.addCleanup(p.stop)
        self.base=self.root/'research/campaigns/PAYOUT_V1/revisions/R13'
        self.runtime=self.root/'research/campaigns/PAYOUT_V1/runtime'
        p=patch.object(C,'RUNTIME',self.runtime);p.start();self.addCleanup(p.stop)
        self.base.mkdir(parents=True);self.runtime.mkdir(parents=True)
        write(self.root/'work/analysis.py',{'preserved':'incident artifact'})
        write(self.base/'incident.json',{'actual_unexpected_path':'work/analysis.py',
              'actual_unexpected_sha256':digest(self.root/'work/analysis.py')})
        write(self.base/'fixture.json',{'synthetic':True,'values':[-2,0,7]})
        self.auth={'incident':self.pin(self.base/'incident.json'),
                   'global_call_cap':1000,'live':'DISABLED','spend_usd':0,
                   'resume_after_new_breach':False,
                   'route_probe_inputs':[self.relative(self.base/'fixture.json')]}
        write(self.base/'authorization.json',self.auth)
        write(self.base/'preflight.json',{'passed':True,
              'authorization_sha256':digest(self.base/'authorization.json'),'source_hashes':{}})
        self.state={'status':'HUMAN_ACTION_REQUIRED','model_calls':480,
                    'bootstrap_invocations':6,'prior_reserved_calls':10,
                    'operator_safety_hold':{'active':True,'incident':self.relative(self.base/'incident.json')},
                    'operator_safety_recovery':{'authorization':self.pin(self.base/'authorization.json'),
                        'preflight':self.pin(self.base/'preflight.json'),'released':False},
                    'completed':{}}
        self.job={'job_id':'R13_PROBE','job_class':'DIAGNOSTIC',
                  'inputs':[{'path':self.relative(self.base/'fixture.json'),'partition':'POLICY_OR_METADATA'}],
                  'dependencies':[],'recovery_episode':{'max_attempts':1},
                  'allowed_file_scope':['research/campaigns/PAYOUT_V1/runtime/jobs/R13_PROBE/work/'+n
                                        for n in ('analysis.py','result.json')]}
        self.state['pending_job']=self.job
        self.c=SimpleNamespace(state=self.state,save=Mock())

    def relative(self,p):return p.relative_to(self.root).as_posix()
    def pin(self,p):return {'path':self.relative(p),'sha256':digest(p)}

    def test_hold_without_acknowledgement_cannot_run_or_charge(self):
        del self.state['operator_safety_recovery']
        before=copy.deepcopy(self.state)
        controller=C.Controller.__new__(C.Controller)
        controller.state=self.state;controller.worker=Mock();controller.lead=Mock()
        self.assertIs(controller.run(resume=True),self.state)
        self.assertEqual(self.state,before)
        controller.worker.assert_not_called();controller.lead.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'active operator safety hold'):
            H.guard_call(self.c,self.runtime/'decisions/209/call_0')

    def test_exact_incident_and_preflight_are_required(self):
        self.assertEqual(H.authority(self.state),self.auth)
        write(self.root/'work/analysis.py',{'changed':True})
        with self.assertRaisesRegex(RuntimeError,'incident artifact changed'):H.authority(self.state)

    def test_changed_or_revoked_authority_fails_closed(self):
        self.state['operator_safety_recovery']['revoked_due_to']='new breach'
        with self.assertRaisesRegex(RuntimeError,'active operator safety hold'):H.authority(self.state)
        del self.state['operator_safety_recovery']['revoked_due_to']
        write(self.base/'preflight.json',{'passed':False})
        with self.assertRaisesRegex(RuntimeError,'pin mismatch'):H.authority(self.state)

    def test_outer_inactive_cannot_hide_unreleased_prior_hold(self):
        self.state['operator_safety_hold']={'active':False,'prior_unreleased_hold':{'active':True,'incident':'older'}}
        self.state['operator_safety_recovery']['released']=True
        self.assertTrue(H.active(self.state))
        with self.assertRaisesRegex(RuntimeError,'active operator safety hold'):
            H.guard_worker(self.c,dict(self.job,job_class='ENGINEERING'))

    def test_auxiliary_unknown_blocks_worker_and_missing_resolution_cannot_clear_gate(self):
        self.state['operator_auxiliary_incidents']={'aux':{'resolved':False,'outcome':'UNKNOWN'}}
        with self.assertRaisesRegex(RuntimeError,'unresolved auxiliary'):
            H.guard_worker(self.c,self.job)
        self.state['operator_auxiliary_incidents']['aux']['resolved']=True
        with self.assertRaisesRegex(RuntimeError,'resolution receipt missing'):
            H.guard_auxiliary(self.state)

    def test_each_nested_incident_needs_its_exact_prior_acknowledgement(self):
        prior=self.base/'older_incident.json';write(prior,{'kind':'retained failure'})
        self.state['operator_safety_hold']['prior_unreleased_hold']={'active':True,'incident':self.relative(prior)}
        with self.assertRaisesRegex(RuntimeError,'prior hold lacks exact'):
            H.release_authorities(self.state,self.auth)
        self.assertTrue(H.active(self.state))
        auth=dict(self.auth,incident=self.pin(prior));ap=self.base/'older_authorization.json';write(ap,auth)
        covered=dict(self.auth,acknowledged_prior_incidents=[{'incident':self.pin(prior),'authorization':self.pin(ap)}])
        self.assertEqual(len(H.release_authorities(self.state,covered)),2)
        wrong=dict(auth,incident=self.auth['incident']);write(ap,wrong)
        covered['acknowledged_prior_incidents'][0]['authorization']=self.pin(ap)
        with self.assertRaisesRegex(RuntimeError,'does not cover incident'):
            H.release_authorities(self.state,covered)

    def test_only_one_nonmarket_worker_reservation_no_counter_refund(self):
        before=self.state['model_calls']
        H.guard_call(self.c,self.runtime/'decisions/209/call_0')
        H.guard_call(self.c,self.runtime/'jobs/R13_PROBE/worker_call_0')
        self.assertEqual(self.state['operator_safety_recovery']['worker_calls_reserved'],1)
        with self.assertRaisesRegex(RuntimeError,'call exhausted'):
            H.guard_call(self.c,self.runtime/'jobs/R13_PROBE/worker_call_1')
        self.assertEqual(self.state['model_calls'],before)
        with self.assertRaisesRegex(ValueError,'bounded nonmarket'):
            H.validate_probe(self.state,dict(self.job,job_class='EXPERIMENT'),self.auth)
        with self.assertRaisesRegex(ValueError,'bounded nonmarket'):
            H.validate_probe(self.state,dict(self.job,job_id='OTHER'),self.auth)

    def test_additional_call_needs_new_pinned_authority_and_preserves_counter(self):
        H.guard_call(self.c,self.runtime/'jobs/R13_PROBE/worker_call_0')
        changed=dict(self.auth,max_route_worker_calls=2)
        write(self.base/'authorization.json',changed)
        with self.assertRaisesRegex(RuntimeError,'pin mismatch'):
            H.guard_call(self.c,self.runtime/'jobs/R13_PROBE/worker_call_1')
        self.state['operator_safety_recovery']['authorization']=self.pin(self.base/'authorization.json')
        write(self.base/'preflight.json',{'passed':True,'authorization_sha256':digest(self.base/'authorization.json'),'source_hashes':{}})
        self.state['operator_safety_recovery']['preflight']=self.pin(self.base/'preflight.json')
        H.guard_call(self.c,self.runtime/'jobs/R13_PROBE/worker_call_1')
        self.assertEqual(self.state['operator_safety_recovery']['worker_calls_reserved'],2)
        with self.assertRaisesRegex(RuntimeError,'call exhausted'):
            H.guard_call(self.c,self.runtime/'jobs/R13_PROBE/worker_call_2')

    def test_autonomous_recovery_requires_real_new_bounded_reserved_episode(self):
        import hashlib
        changed=dict(self.auth,max_route_worker_calls=2,lead_scoped_recovery_authorized=True,
                     lead_recovery_min_decision_index=218)
        write(self.base/'authorization.json',changed)
        self.state['operator_safety_recovery']['authorization']=self.pin(self.base/'authorization.json')
        write(self.base/'preflight.json',{'passed':True,'authorization_sha256':digest(self.base/'authorization.json'),'source_hashes':{}})
        self.state['operator_safety_recovery']['preflight']=self.pin(self.base/'preflight.json')
        self.state['operator_safety_recovery']['worker_calls_reserved']=2
        with self.assertRaisesRegex(RuntimeError,'fresh bounded'):
            H.guard_call(self.c,self.runtime/'jobs/R13_PROBE/worker_call_2')
        episode={'component':'runtime','root_cause':'specific root cause','changed_approach':'different repair','max_attempts':1}
        key=hashlib.sha256(json.dumps({k:episode[k] for k in ('component','root_cause','changed_approach')},sort_keys=True).encode()).hexdigest()
        self.state['lead_recovery_authorizations']={'R13_PROBE':{'episode':episode,'decision_index':218}}
        self.state['recovery_episodes']={key:{'job_id':'R13_PROBE','attempts':[{'evidence':'new'}]}}
        H.guard_call(self.c,self.runtime/'jobs/R13_PROBE/worker_call_2')
        self.assertEqual(self.state['operator_safety_recovery']['worker_calls_reserved'],3)
        with self.assertRaisesRegex(RuntimeError,'exhausted'):
            H.guard_call(self.c,self.runtime/'jobs/R13_PROBE/worker_call_3')

    def test_release_requires_actual_identity_qa_and_lead_review(self):
        H.guard_worker(self.c,self.job)
        decision={'evidence_id':'R13_PROBE','action':'FIX_EXPERIMENT'}
        self.assertFalse(H.release_after_review(self.c,decision))
        folder=self.runtime/'jobs/R13_PROBE'
        write(folder/'work/result.json',{'synthetic':True})
        write(folder/'verification_0.json',{'exit_code':0})
        record=dict(self.pin(folder/'work/result.json'),verification=self.relative(folder/'verification_0.json'))
        self.state['completed']['R13_PROBE']=record
        with self.assertRaisesRegex(RuntimeError,'complete QA'):H.release_after_review(self.c,decision)
        write(self.runtime/'qa/probe/report.json',{'verdict':'PASS'})
        self.state['latest_qa_review']=dict(self.pin(self.runtime/'qa/probe/report.json'),job_id='R13_PROBE',verdict='PASS')
        self.state['reviewed_results']={'R13_PROBE':record['sha256']}
        write(folder/'scope_check_0.json',{'unexpected':[]})
        write(folder/'worker_call_0/receipt.json',{'exit_code':0})
        write(self.runtime/'decisions/210/accepted.json',decision)
        self.state['last_decision']=self.relative(self.runtime/'decisions/210/accepted.json')
        prior=self.base/'nested_incident.json';write(prior,{'kind':'old still active failure'})
        self.state['operator_safety_hold']['prior_unreleased_hold']={'active':True,'incident':self.relative(prior)}
        with self.assertRaisesRegex(RuntimeError,'prior hold lacks exact'):
            H.release_after_review(self.c,decision)
        self.assertTrue(H.active(self.state))
        self.assertFalse(self.state['operator_safety_recovery']['released'])
        prior_auth=self.base/'nested_auth.json';write(prior_auth,dict(self.auth,incident=self.pin(prior)))
        updated=dict(self.auth,acknowledged_prior_incidents=[{'incident':self.pin(prior),'authorization':self.pin(prior_auth)}])
        write(self.base/'authorization.json',updated)
        self.state['operator_safety_recovery']['authorization']=self.pin(self.base/'authorization.json')
        write(self.base/'preflight.json',{'passed':True,'authorization_sha256':digest(self.base/'authorization.json'),'source_hashes':{}})
        self.state['operator_safety_recovery']['preflight']=self.pin(self.base/'preflight.json')
        original=copy.deepcopy(self.state['operator_safety_hold'])
        self.state['operator_auxiliary_incidents']={'aux':{'resolved':False}}
        with self.assertRaisesRegex(RuntimeError,'unresolved auxiliary'):
            H.release_after_review(self.c,decision)
        self.assertTrue(H.active(self.state))
        self.state.pop('operator_auxiliary_incidents')
        # Controller persists native Windows separators. Normalization is only
        # for this internal decision reference; absolute/traversal stays denied.
        good=self.state['last_decision']
        for bad in ('C:\\outside\\accepted.json', '\\\\server\\share\\accepted.json',
                    '..\\outside\\accepted.json'):
            self.state['last_decision']=bad
            with self.subTest(path=bad), self.assertRaises(ValueError):
                H.release_after_review(self.c,decision)
            self.assertTrue(H.active(self.state))
        self.state['last_decision']=good.replace('/', '\\')
        self.assertTrue(H.release_after_review(self.c,decision))
        self.assertFalse(H.active(self.state))
        self.assertFalse(self.state['operator_safety_hold']['prior_unreleased_hold']['active'])
        self.assertTrue((self.root/self.state['operator_safety_hold']['prior_unreleased_hold']['release']['path']).exists())
        self.assertEqual(self.state['operator_safety_hold_history'][0]['incident'],original['incident'])
        self.assertEqual(self.state['model_calls'],480)
        receipt=G.read(self.base/'RECOVERY_RELEASE_RECEIPT.json')
        self.assertEqual(receipt['call_units_used'],496)
        self.assertFalse(H.release_after_review(self.c,decision))

    def test_prompt_and_native_cwd_share_exact_job_destination(self):
        profile={'worker_route':'codex','worker':'gpt-6-luna','worker_variant':'max'}
        # Use actual route predicate keys, without invoking a provider.
        profile['worker_provider']='codex'
        write(self.runtime/'jobs/R13_PROBE/job.json',self.job)
        with patch.object(W,'codex_route',return_value=True),patch.object(W,'native_command',return_value=['codex.exe']):
            cwd=W.worker_workdir(profile,self.job)
            prompt=W.build_worker_prompt(profile,self.job,{},'FIXED','')
            argv=W.worker_command(profile,prompt,cwd=cwd)
        self.assertEqual(cwd,self.runtime/'jobs/R13_PROBE/work')
        self.assertIn(str(cwd/'analysis.py'),prompt)
        self.assertIn('Fixed entry invocation limit for this call: 1.',prompt)
        self.assertEqual(argv[argv.index('-C')+1],str(cwd))
        self.assertTrue(any(x.startswith('shell_environment_policy.set.PYTHONPATH=') for x in argv))
        self.assertNotIn('--add-dir',argv)
        bad=dict(self.job,allowed_file_scope=['work/analysis.py'])
        with self.assertRaisesRegex(ValueError,'frozen scope'):
            W.build_worker_prompt(profile,bad,{},'FIXED','')

    def test_recovery_native_history_rejects_repeat_and_success_duplicate(self):
        folder=self.runtime/'jobs/R13_PROBE/worker_call_0';folder.mkdir(parents=True)
        write(folder/'request.json',{'argv':['codex.exe','-m','gpt-6-luna']})
        (folder/'prompt.txt').write_text('Fixed entry invocation limit for this call: 1.',encoding='utf-8')
        commands=[{'command':'fixed command','exit_code':1,'output':'failed'},
                  {'command':'fixed command','exit_code':0,'output':'ok'}]
        with patch.object(W,'codex_commands',return_value=commands):
            with self.assertRaisesRegex(RuntimeError,'repair limit'):
                W.validate_worker_history('','fixed command',self.c,folder)
        (folder/'prompt.txt').write_text('historical prompt',encoding='utf-8')
        commands[0]['exit_code']=0
        with patch.object(W,'codex_commands',return_value=commands):
            with self.assertRaisesRegex(RuntimeError,'repeated successful'):
                W.validate_worker_history('','fixed command',self.c,folder)

    def test_scoped_prompt_renders_copyable_literal_read_commands(self):
        from tools.quant_research_loop import worker_scope as S
        job=copy.deepcopy(self.job)
        job['inputs'][0]['kind']='json'
        write(self.runtime/'jobs/R13_PROBE/job.json',job)
        profile={'worker':'gpt-6-luna','worker_provider':'codex','worker_variant':'max'}
        with patch.object(S,'ROOT',self.root):
            prompt=W.build_worker_prompt(profile,job,
                {'worker_command_policy':{'version':S.VERSION}},'FIXED','')
            for command in S.inspections(job):
                self.assertIn('\n'+command+'\n',prompt)
                self.assertEqual(S.command_kind(command,job,'FIXED',[]),'pinned_read')


if __name__=='__main__':unittest.main()

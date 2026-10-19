import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.quant_research_loop import controller as C
from tools.quant_research_loop.gate_zero import write
from tools.quant_research_loop.prepare import digest


class LeadCapacityRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.camp=self.root/'research/campaigns/PAYOUT_V1'
        self.runtime=self.camp/'runtime'
        for name,value in [('ROOT',self.root),('CAMP',self.camp),('RUNTIME',self.runtime)]:
            p=patch.object(C,name,value);p.start();self.addCleanup(p.stop)
        p=patch('tools.quant_research_loop.qa_review.cycle_reserve',return_value=5)
        p.start();self.addCleanup(p.stop)
        self.call=self.runtime/'decisions/241/call_0'
        self.call.mkdir(parents=True)
        write(self.call/'receipt.json',{'exit_code':1,'process_reaped':True})
        message='Selected model is at capacity. Please try a different model.'
        self.events=[{'type':'thread.started','thread_id':'fixture'},
                     {'type':'turn.started'},{'type':'error','message':message},
                     {'type':'turn.failed','error':{'message':message}}]
        (self.call/'stdout.jsonl').write_text('\n'.join(map(json.dumps,self.events)),encoding='utf-8')
        self.authpath=self.camp/'revisions/R22/AUTHORIZATION.json'
        self.authpath.parent.mkdir(parents=True)
        self.auth={'kind':'ONE_TIME_LEAD_CAPACITY_RECOVERY','approved':True,
                   'max_recoveries':1,'global_call_cap':1000,'model':'gpt-6.1-sol',
                   'reasoning':'high','live':'DISABLED','spend_usd':0,
                   'resume_after_new_breach':False,'user_verbatim':'Inspect and fix',
                   'prior_decision_index':241,'used_call_units_before':560}
        self.c=object.__new__(C.Controller)
        self.c.profile={'max_calls':1000,'lead':'gpt-6.1-sol','reasoning':'high'}
        self.c.state={'status':'PAUSED_LEAD','decisions':241,'model_calls':544,
                      'bootstrap_invocations':6,'prior_reserved_calls':10,
                      'error':'Lead runtime unavailable; no worker fallback',
                      'lead_capacity_retries':1,'transport_retries':2,'trials':45,
                      'pending_job':{'job_id':'FROZEN'},'completed':{'OLD':{'sha':'saved'}},
                      'operator_lead_capacity_recovery':{}}
        self.c.save=Mock();self.c.check_budget=Mock()
        self.pin()

    def pin(self):
        for key,name in [('prior_receipt','receipt.json'),('prior_stdout','stdout.jsonl')]:
            p=self.call/name
            self.auth[key]={'path':p.relative_to(self.root).as_posix(),'sha256':digest(p)}
        write(self.authpath,self.auth)
        self.c.state['operator_lead_capacity_recovery']['authorization']={
            'path':self.authpath.relative_to(self.root).as_posix(),'sha256':digest(self.authpath)}

    def test_recovery_preserves_spent_calls_trials_job_and_failed_bytes(self):
        before=copy.deepcopy(self.c.state)
        original={p:p.read_bytes() for p in self.call.iterdir()}
        self.assertTrue(self.c.reconcile_authorized_lead_capacity())
        self.assertEqual(self.c.state['decisions'],242)
        for k in ['model_calls','bootstrap_invocations','prior_reserved_calls','lead_capacity_retries',
                  'transport_retries','trials','pending_job','completed']:
            self.assertEqual(self.c.state[k],before[k])
        self.assertTrue(all(p.read_bytes()==b for p,b in original.items()))
        self.assertFalse((self.runtime/'decisions/242').exists())
        self.c.state.update(status='PAUSED_LEAD',error='Lead runtime unavailable; no worker fallback')
        self.assertFalse(self.c.reconcile_authorized_lead_capacity())
        self.assertEqual(self.c.state['decisions'],242)

    def test_no_grant_is_no_recovery(self):
        self.c.state.pop('operator_lead_capacity_recovery')
        self.assertFalse(self.c.reconcile_authorized_lead_capacity())
        self.c.save.assert_not_called()

    def test_safety_pause_cancel_and_inflight_block_recovery(self):
        for change in [{'operator_safety_hold':{'active':True}}, {'status':'HUMAN_ACTION_REQUIRED'},
                       {'inflight_call':'unreconciled'}]:
            with self.subTest(change=change):
                before=copy.deepcopy(self.c.state);self.c.state.update(change)
                with self.assertRaises(RuntimeError):self.c.reconcile_authorized_lead_capacity()
                self.c.state=before
        for name in ['pause.request','cancel.request']:
            p=self.runtime/name;p.write_text('{}')
            with self.assertRaises(RuntimeError):self.c.reconcile_authorized_lead_capacity()
            p.unlink()
        self.c.save.assert_not_called()

    def test_changed_pin_or_checkpoint_is_rejected(self):
        self.authpath.write_text('{}')
        with self.assertRaises(ValueError):self.c.reconcile_authorized_lead_capacity()
        self.pin();self.c.state['model_calls']+=1
        with self.assertRaises(ValueError):self.c.reconcile_authorized_lead_capacity()
        self.c.save.assert_not_called()

    def test_noncapacity_completed_or_tool_output_cannot_be_retired(self):
        for event in [{'type':'turn.completed'}, {'type':'item.completed','item':{'type':'command_execution'}},
                      {'type':'error','message':'permission denied'}]:
            (self.call/'stdout.jsonl').write_text('\n'.join(map(json.dumps,self.events+[event])),encoding='utf-8')
            self.pin()
            with self.assertRaises(ValueError):self.c.reconcile_authorized_lead_capacity()
        self.c.save.assert_not_called()

    def test_insufficient_cycle_budget_and_existing_destination_block(self):
        self.c.profile['max_calls']=565;self.auth['global_call_cap']=565;self.pin()
        with self.assertRaisesRegex(RuntimeError,'review reserve'):self.c.reconcile_authorized_lead_capacity()
        self.c.profile['max_calls']=1000;self.auth['global_call_cap']=1000;self.pin()
        (self.runtime/'decisions/242').mkdir()
        with self.assertRaisesRegex(ValueError,'already exists'):self.c.reconcile_authorized_lead_capacity()
        self.c.save.assert_not_called()

    def test_reconciliation_write_before_crash_can_resume_once(self):
        before=copy.deepcopy(self.c.state)
        self.c.save.side_effect=OSError('simulated crash before state persisted')
        with self.assertRaises(OSError):self.c.reconcile_authorized_lead_capacity()
        self.c.state=before;self.c.save.side_effect=None
        self.assertTrue(self.c.reconcile_authorized_lead_capacity())
        self.assertFalse(self.c.reconcile_authorized_lead_capacity())


if __name__=='__main__':unittest.main()

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.quant_research_loop import controller as CT
from tools.quant_research_loop import generic_worker as GW
from tools.quant_research_loop import runtime_policy as RP


class R08RouteTests(unittest.TestCase):
    def test_r07_to_r08_keeps_inflight_and_counters_until_reconciled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for relative, text in ((RP.BASE_AUTH,'base'),
                                   (RP.AUTHORIZATION_CODEX_LUNA,'r07'),
                                   (RP.AUTHORIZATION_GPT6_LUNA,'r08')):
                path=root/relative
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(text,encoding='utf-8')
            common={'approved':True,'global_max_calls':1000,'campaign_stop_cap':1000,
                    'global_max_seconds_reported':21600,'allow_cumulative_runtime_hard_cap':False,
                    'cumulative_runtime_is_reporting_only':True,
                    'shorten_new_timeouts_by_cumulative_remaining':False,
                    'per_call_timeouts_active':True,'per_job_timeouts_active':True,
                    'max_repairs_per_same_blocker':2,'spend_usd':0,'live':'DISABLED',
                    'lead_model':'gpt-6-astra','lead_reasoning':'xhigh','worker_provider':'codex',
                    'worker_variant':'max','pending_job_id':'PENDING'}
            for path,model in ((RP.AUTHORIZATION_CODEX_LUNA_JSON,'gpt-5.6-luna'),
                               (RP.AUTHORIZATION_GPT6_LUNA_JSON,'gpt-6-luna')):
                target=root/path
                target.write_text(json.dumps(dict(common,worker_model=model)),encoding='utf-8')
            def sha(path):
                return hashlib.sha256((root/path).read_bytes()).hexdigest()
            state={'generic_authority':{'path':RP.BASE_AUTH.as_posix(),'sha256':sha(RP.BASE_AUTH)},
                   'runtime_policy_revision':RP.REVISION_CODEX_LUNA,
                   'runtime_policy':{'authorization_revision':{
                       'revision_id':RP.REVISION_CODEX_LUNA,
                       'path':RP.AUTHORIZATION_CODEX_LUNA.as_posix(),
                       'sha256':sha(RP.AUTHORIZATION_CODEX_LUNA),
                       'json_sha256':sha(RP.AUTHORIZATION_CODEX_LUNA_JSON)},
                       'global_max_calls':1000},
                   'status':'RUNNING','model_calls':269,'bootstrap_invocations':6,
                   'prior_reserved_calls':10,'trials':31,
                   'pending_job':{'job_id':'PENDING'},'inflight_call':'old/worker_call_3',
                   'worker_route_migration':{'to':{'model':'gpt-5.6-luna'}}}
            profile={'max_calls':80,'max_active_seconds':21600}
            with patch.object(RP,'ROOT',root):
                migrated,changed=RP.migrate_state(state,profile)
                self.assertTrue(changed)
                self.assertEqual(migrated['inflight_call'],'old/worker_call_3')
                self.assertEqual(migrated['trials'],31)
                self.assertEqual(migrated['runtime_budget_migration']['used_calls_at_migration'],285)
                self.assertEqual(migrated['worker_route_migration_history'][0]['to']['model'],'gpt-5.6-luna')
                self.assertEqual(RP.apply_runtime_budget(profile,migrated)['worker'],'gpt-6-luna')
                self.assertEqual(RP.remaining_call_units(migrated,RP.apply_runtime_budget(profile,migrated)),715)
                again,changed=RP.migrate_state(migrated,profile)
                self.assertFalse(changed)
                self.assertEqual(len(again['authorization_history']),1)

    def test_gpt6_command_and_completion_identity(self):
        profile={'worker_provider':'codex','worker':'gpt-6-luna','worker_variant':'max'}
        argv=GW.worker_command(profile,'prompt')
        self.assertEqual(argv[argv.index('-m')+1],'gpt-6-luna')
        self.assertIn('model_reasoning_effort="max"',argv)
        with tempfile.TemporaryDirectory() as tmp:
            call=Path(tmp)/'worker_call_4';call.mkdir()
            GW.write(call/'request.json',{'argv':argv})
            raw='\n'.join((json.dumps({'type':'thread.started','thread_id':'thread-6'}),
                           json.dumps({'type':'turn.completed','usage':{}})))
            exported=GW.codex_completion(raw,{'exit_code':0},call)
            self.assertEqual(exported['info']['model'],GW.codex_identity('gpt-6-luna'))
            self.assertTrue(GW.completion_matches(call,exported))
            self.assertFalse(exported['identity_evidence']['provider_reported_model'])

    def test_quoted_rate_limit_in_command_output_is_not_provider_429(self):
        raw='\n'.join((json.dumps({'type':'thread.started','thread_id':'thread-6'}),
                       json.dumps({'type':'item.completed','item':{'type':'command_execution',
                           'aggregated_output':'historical rate limit note','exit_code':0}})))
        self.assertEqual(GW.codex_outcome(raw,{'exit_code':None},False,''),'TIMEOUT_OR_INTERRUPTED')
        error_raw=raw+'\n'+json.dumps({'type':'error','message':'HTTP 429 rate limit'})
        self.assertEqual(GW.codex_outcome(error_raw,{'exit_code':None},False,''),'RATE_LIMIT')

    def test_timestamp_digits_are_not_auth_status(self):
        raw='\n'.join((json.dumps({'type':'thread.started','thread_id':'thread-6'}),
                       json.dumps({'type':'turn.completed','usage':{}})))
        stderr='2026-09-22T21:00:26.234015Z WARN connection reset'
        self.assertEqual(GW.codex_outcome(raw,{'exit_code':0},True,stderr),'SUCCESS')
        self.assertEqual(GW.codex_outcome(raw,{'exit_code':0},True,'HTTP 401 Unauthorized'),'AUTH')

    def test_abandoned_call_reconciliation_preserves_reserved_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            runtime=root/'research/campaigns/PAYOUT_V1/runtime'
            jid='PAYOUT_R06_STOCH_CONTROL_RISK_EVIDENCE_AUDIT_V1'
            call=runtime/'jobs'/jid/'worker_call_3'
            call.mkdir(parents=True)
            (call/'prompt.txt').write_text('frozen',encoding='utf-8')
            (call/'request.json').write_text(json.dumps({'argv':['codex','exec','-m','gpt-5.6-luna']}),encoding='utf-8')
            instance=CT.Controller.__new__(CT.Controller)
            instance.state={'pending_job':{'job_id':jid,'inputs':[]},
                            'inflight_call':call.relative_to(root).as_posix(),
                            'model_calls':269}
            instance.save=Mock()
            with patch.object(CT,'ROOT',root), patch.object(CT,'RUNTIME',runtime), patch('psutil.process_iter',return_value=[]):
                instance.reconcile_interrupted_worker()
            self.assertNotIn('inflight_call',instance.state)
            self.assertEqual(instance.state['model_calls'],269)
            self.assertEqual(json.loads((call.parent/'failure_3.json').read_text())['runtime_outcome'],'UNKNOWN')
            self.assertFalse((call/'receipt.json').exists())


if __name__=='__main__':
    unittest.main()

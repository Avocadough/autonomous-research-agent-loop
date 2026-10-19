import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.quant_research_loop import runtime_policy as RP
from tools.quant_research_loop import generic_worker as GW


class R07CodexLunaRouteTests(unittest.TestCase):
    def _write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2), encoding='utf-8')

    def test_r06_to_r07_preserves_budget_and_changes_only_worker_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            base=root/RP.BASE_AUTH;base.parent.mkdir(parents=True,exist_ok=True)
            base.write_text('base',encoding='utf-8')
            r06=root/RP.AUTHORIZATION_CAMPAIGN_1000
            r06j=root/RP.AUTHORIZATION_CAMPAIGN_1000_JSON
            r06.parent.mkdir(parents=True,exist_ok=True);r06.write_text('r06',encoding='utf-8')
            common={
                'approved':True,'allow_cumulative_runtime_hard_cap':False,
                'cumulative_runtime_is_reporting_only':True,'global_max_calls':1000,
                'global_max_seconds_reported':21600,'shorten_new_timeouts_by_cumulative_remaining':False,
                'per_call_timeouts_active':True,'per_job_timeouts_active':True,
                'max_repairs_per_same_blocker':2,'spend_usd':0,'live':'DISABLED',
                'campaign_stop_cap':1000,'lead_model':'gpt-6-astra','lead_reasoning':'xhigh'}
            self._write_json(r06j,dict(common,
                worker_model='opencode/muse-spark-1.3-contributor-free',worker_variant='xhigh'))
            r07=root/RP.AUTHORIZATION_CODEX_LUNA
            r07j=root/RP.AUTHORIZATION_CODEX_LUNA_JSON
            r07.parent.mkdir(parents=True,exist_ok=True);r07.write_text('r07',encoding='utf-8')
            self._write_json(r07j,dict(common,worker_provider='codex',worker_model='gpt-5.6-luna',
                worker_variant='max',worker_reasoning='max',pending_job_id='PENDING'))
            ref={'revision_id':RP.REVISION_CAMPAIGN_1000,
                 'path':RP.AUTHORIZATION_CAMPAIGN_1000.as_posix(),
                 'sha256':hashlib.sha256(r06.read_bytes()).hexdigest(),
                 'json_path':RP.AUTHORIZATION_CAMPAIGN_1000_JSON.as_posix(),
                 'json_sha256':hashlib.sha256(r06j.read_bytes()).hexdigest(),
                 'base_authorization_path':RP.BASE_AUTH.as_posix(),
                 'base_authorization_sha256':hashlib.sha256(base.read_bytes()).hexdigest()}
            state={'generic_authority':{'path':RP.BASE_AUTH.as_posix(),'sha256':ref['base_authorization_sha256']},
                   'runtime_policy_revision':RP.REVISION_CAMPAIGN_1000,
                   'runtime_policy':{'authorization_revision':ref,'allow_cumulative_runtime_hard_cap_cancelled':True,
                                     'global_max_calls':1000,'campaign_stop_cap':1000},
                   'status':'ENGINEERING_BLOCKED','error':'old Muse 429','latest_budget_receipt':'old.json',
                   'model_calls':266,'bootstrap_invocations':6,'prior_reserved_calls':10,
                   'pending_job':{'job_id':'PENDING'}}
            profile={'max_calls':80,'max_active_seconds':21600}
            with patch.object(RP,'ROOT',root):
                migrated,changed=RP.migrate_state(state,profile)
                self.assertTrue(changed)
                self.assertEqual(migrated['runtime_policy_revision'],RP.REVISION_CODEX_LUNA)
                self.assertEqual(migrated['runtime_budget_migration']['used_calls_at_migration'],282)
                self.assertTrue(migrated['worker_route_migration_pending_execution'])
                overlaid=RP.apply_runtime_budget(profile,migrated)
                self.assertEqual(overlaid['lead'],'gpt-6-astra')
                self.assertEqual(overlaid['reasoning'],'xhigh')
                self.assertEqual(overlaid['worker_provider'],'codex')
                self.assertEqual(overlaid['worker'],'gpt-5.6-luna')
                self.assertEqual(overlaid['worker_variant'],'max')
                self.assertEqual(RP.remaining_call_units(migrated,overlaid),718)
                again,changed_again=RP.migrate_state(migrated,overlaid)
                self.assertFalse(changed_again)
                self.assertEqual(again['model_calls'],266)

    def test_codex_command_and_terminal_identity_are_pinned(self):
        profile={'worker_provider':'codex','worker':'gpt-5.6-luna','worker_variant':'max'}
        command=GW.worker_command(profile,'prompt')
        joined=' '.join(command)
        self.assertIn('gpt-5.6-luna',command)
        self.assertIn('model_reasoning_effort="max"',command)
        self.assertIn('forced_login_method="chatgpt"',command)
        self.assertIn('--approve-for-me',command)
        self.assertNotIn('never',command)
        self.assertIn('--ignore-user-config',command)
        self.assertIn('--ephemeral',command)
        self.assertNotIn('workspace-write',command)
        self.assertTrue(joined.lower().find('codex')>=0)
        self.assertEqual(command[-1],'-')
        with tempfile.TemporaryDirectory() as tmp:
            call=Path(tmp)/'job'/'worker_call_1';call.mkdir(parents=True)
            fixed='py -3.12 -X utf8 -m tools.quant_research_loop.generic_entry job.json'
            GW.write(call/'request.json',{'argv':command,'cwd':str(GW.ROOT)})
            raw='\n'.join([
                json.dumps({'type':'thread.started','thread_id':'thread-1'}),
                json.dumps({'type':'item.completed','item':{'type':'command_execution',
                    'command':fixed,'aggregated_output':'ok','exit_code':0}}),
                json.dumps({'type':'turn.completed','usage':{}})])
            receipt={'exit_code':0,'elapsed_s':1,'process_reaped':True}
            completion=GW.codex_completion(raw,receipt,call)
            self.assertEqual(completion['info']['model'],GW.CODEX_LUNA_IDENTITY)
            self.assertFalse(completion['identity_evidence']['provider_reported_model'])
            self.assertTrue(GW.completion_matches(call,completion))
            self.assertEqual(GW.codex_outcome(raw,receipt,True),'SUCCESS')
            history=GW.validate_worker_history(raw,fixed,object(),call)
            self.assertEqual(len(history),1)


if __name__ == '__main__':
    unittest.main()

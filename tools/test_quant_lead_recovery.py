import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.quant_research_loop import controller as C, generic_jobs as G, lead_recovery as R
from tools.quant_research_loop.gate_zero import write


class RecoveryTests(unittest.TestCase):
    def episode(self):
        return dict(component='bounded analysis runner component',
                    root_cause='specific unsupported import in generated checker',
                    changed_approach='replace unsupported import with pinned supported API',
                    stop_condition='stop after one dispatched invocation and return evidence',
                    evidence_refs=['evidence.json'], max_attempts=1)

    def test_frozen_contract_retains_separate_lead_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);folder=base/'runtime/jobs/TEST_JOB';folder.mkdir(parents=True)
            frozen={'job_id':'TEST_JOB','objective':'original frozen question'}
            write(folder/'job.json',frozen)
            proposed=dict(frozen,objective='specific revised implementation instruction',recovery_episode=self.episode())
            state={'pending_job':frozen,'decisions':40}
            with patch.object(G,'CAMP',base),patch.object(G,'validate_spec'):
                result=G.register(proposed,{},state)
            self.assertEqual(result,frozen)
            self.assertEqual(G.read(folder/'job.json'),frozen)
            self.assertEqual(state['lead_recovery_authorizations']['TEST_JOB']['episode'],self.episode())

    def test_episode_does_not_reset_or_expand_historical_counter(self):
        state={'blocker_repairs':{'old':{'attempts':2}},
               'lead_recovery_authorizations':{'TEST_JOB':{'episode':self.episode()}}}
        original=copy.deepcopy(state['blocker_repairs'])
        with patch.object(R,'enabled',return_value=True),patch.object(R,'validate_episode'):
            self.assertTrue(R.reserve_episode(state,{'job_id':'TEST_JOB'},'TIMEOUT','changed-source-hash'))
            with self.assertRaisesRegex(RuntimeError,'episode exhausted'):
                R.reserve_episode(state,{'job_id':'TEST_JOB'},'TIMEOUT','another-hash')
        self.assertEqual(original,state['blocker_repairs'])

    def test_null_pending_recovery_reuses_registered_identity_without_loosening_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);folder=base/'runtime/jobs/TEST_JOB';folder.mkdir(parents=True)
            frozen={'job_id':'TEST_JOB','objective':'original question','inputs':[{'path':'fixed'}]}
            write(folder/'job.json',frozen)
            proposed=dict(frozen,objective='repair the same job',recovery_episode=self.episode())
            state={'pending_job':None,'decisions':218,
                'generic_registry':{'TEST_JOB':{'sha256':G.digest(folder/'job.json')}}}
            with patch.object(G,'CAMP',base),patch.object(G,'validate_spec'):
                result=G.register(proposed,{},state)
                self.assertEqual(result,frozen)
                self.assertEqual(G.read(folder/'job.json'),frozen)
                altered=dict(proposed,inputs=[{'path':'expanded'}])
                with self.assertRaisesRegex(ValueError,'Frozen job identity conflict'):
                    G.register(altered,{},state)

    def test_worker_first_dispatch_reserves_episode(self):
        from tools.quant_research_loop import generic_worker as W
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);camp=root/'research/campaigns/PAYOUT_V1';folder=camp/'runtime/jobs/TEST_JOB'
            folder.mkdir(parents=True)
            job={'job_id':'TEST_JOB','job_class':'ENGINEERING','inputs':[],
                 'recovery_episode':self.episode(),
                 'allowed_file_scope':[(folder/'work'/name).relative_to(root).as_posix()
                                       for name in ('analysis.py','result.json')]}
            write(folder/'job.json',job)
            c=Mock();c.state={'completed':{}};c.profile={'max_calls':160,'worker':'fixture','worker_variant':'xhigh'}
            c.model_call.side_effect=RuntimeError('TEST_DISPATCH')
            with patch.object(W,'ROOT',root),patch.object(W,'CAMP',camp),patch.object(W,'validate_spec'),patch.object(W,'recover_existing',return_value=False),patch.object(W,'snapshot',return_value={}),patch.object(W,'native_command',return_value=['fixture']),patch.object(R,'enabled',return_value=True),patch.object(R,'validate_episode'):
                with self.assertRaisesRegex(RuntimeError,'TEST_DISPATCH'):
                    W.execute(c,job)
            c.model_call.assert_called_once()
            records=list(c.state['recovery_episodes'].values())
            self.assertEqual(len(records),1)
            self.assertEqual(len(records[0]['attempts']),1)
            self.assertEqual(records[0]['attempts'][0]['code'],'WORKER_INVOCATION')

    def test_new_lead_episode_supersedes_frozen_episode_without_reset(self):
        first=self.episode();second=dict(first,changed_approach='Inspect saved partial implementation before a minimal correction')
        job={'job_id':'TEST_JOB','recovery_episode':first};state={}
        with patch.object(R,'enabled',return_value=True),patch.object(R,'validate_episode'):
            R.reserve_episode(state,job,'WORKER_INVOCATION','original')
            old=copy.deepcopy(state['recovery_episodes'])
            state['lead_recovery_authorizations']={'TEST_JOB':{'episode':second}}
            R.reserve_episode(state,job,'WORKER_INVOCATION','partial-implementation')
            self.assertEqual(len(state['recovery_episodes']),2)
            for key,value in old.items():self.assertEqual(state['recovery_episodes'][key],value)
            with self.assertRaisesRegex(RuntimeError,'episode exhausted'):
                R.reserve_episode(state,job,'WORKER_INVOCATION','third')

    def test_real_run_control_flow_returns_worker_cap_to_lead(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            c=C.Controller.__new__(C.Controller)
            c.state={'status':'RUNNING','pending_job':{'job_id':'TEST_JOB'},
                     'model_calls':0,'bootstrap_invocations':0,'prior_reserved_calls':0}
            c.profile={'max_calls':160}
            c.save=Mock();c.check_budget=Mock()
            c.worker=Mock(side_effect=RuntimeError('ENGINEERING_BLOCKED: same blocker repair cap'))
            c.lead=Mock(return_value={'action':'NO_QUALIFYING_CANDIDATE','next_job':None})
            with patch.object(C,'RUNTIME',base),patch.object(R,'ROOT',base),patch.object(R,'BASE',base/'revision'),patch.object(R,'enabled',return_value=True):
                result=c.run()
            c.worker.assert_called_once();c.lead.assert_called_once()
            self.assertEqual(result['status'],'NO_QUALIFYING_CANDIDATE')
            self.assertEqual(len(result['recovery_history']),1)
            self.assertTrue((base/'revision/recovery/000.json').exists())

    def test_financial_permission_error_does_not_enter_local_recovery(self):
        c=Mock();c.state={'pending_job':{'job_id':'TEST_JOB'}}
        with patch.object(R,'enabled',return_value=True):
            self.assertFalse(R.capture(c,RuntimeError('HUMAN_ACTION_REQUIRED: protected data')))

    def test_registered_generic_reference_is_expanded_only_after_hash_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'jobs/TEST_JOB/job.json';p.parent.mkdir(parents=True)
            write(p,{'job_id':'TEST_JOB','job_class':'VERIFY'})
            state={'evidence_id':'E','generic_registry':{'TEST_JOB':{'sha256':C.digest(p)}}}
            decision=dict(campaign_id='PAYOUT_V1',evidence_id='E',action='FIX_EXPERIMENT',
                          rationale='Use registered frozen job',artifact_refs=['evidence.json'],
                          next_job={'job_id':'TEST_JOB'},worker_dispatch_ready=True,
                          missing_information=[],pause_category='NONE')
            with patch.object(C,'RUNTIME',root),patch.object(G,'authorized',return_value=True),patch.object(G,'validate_spec') as validate:
                self.assertEqual(C.validate_decision(decision,state,{}),decision)
                validate.assert_called_once()
                write(p,{'job_id':'TEST_JOB','job_class':'MODIFIED'})
                with self.assertRaisesRegex(ValueError,'hash mismatch'):
                    C.validate_decision(decision,state,{})


if __name__=='__main__':
    unittest.main()

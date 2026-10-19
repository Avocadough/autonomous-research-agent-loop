"""No model calls: native policy gate and immutable recovery authority checks."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.quant_research_loop import native_preflight as N
from tools.quant_research_loop import generic_worker as W
from tools.quant_research_loop.prepare import digest
from tools.quant_research_loop.gate_zero import write


class NativePreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.response = {'model': 'gpt-6-luna', 'modelProvider': 'openai',
            'reasoningEffort': 'max', 'cwd': str(self.cwd),
            'sandbox': {'type': 'workspaceWrite', 'networkAccess': False, 'writableRoots': []},
            'runtimeWorkspaceRoots': [str(self.cwd)], 'approvalPolicy': 'on-request',
            'approvalsReviewer': 'auto_review', 'thread': {'id': 'test-session', 'originator': 'payout-worker'}}

    def test_child_environment_drops_parent_identity_only(self):
        original={'CODEX_INTERNAL_ORIGINATOR_OVERRIDE':'Codex Desktop',
                  'CODEX_THREAD_ID':'parent-chat', 'PATH':'runtime-path', 'OTHER':'kept'}
        with patch.object(N,'env_clean',return_value=dict(original)):
            child=N.worker_environment()
        self.assertEqual(child,{'PATH':'runtime-path','OTHER':'kept'})
        self.assertEqual(original['CODEX_THREAD_ID'],'parent-chat')

    def test_desktop_identity_rejected_before_any_model_turn(self):
        response=copy.deepcopy(self.response);response['thread']['originator']='Codex Desktop'
        messages=[{'id':1,'result':{}},{'id':2,'result':response}]
        proc=Mock();proc.stdout=io.StringIO('\n'.join(json.dumps(x) for x in messages)+'\n')
        proc.stderr=io.StringIO('');proc.stdin=io.StringIO();proc.stdin.close=Mock();proc.returncode=0
        with patch.object(N,'native_command',return_value=['codex']),patch.object(N.subprocess,'Popen',return_value=proc):
            with self.assertRaisesRegex(RuntimeError,'unexpected parent identity'):
                N.prepare_session(self.cwd,'gpt-6-luna',self.cwd/'wrong_identity.json')
        self.assertNotIn('turn/start',proc.stdin.getvalue())

    def test_readonly_network_extra_roots_model_and_cwd_fail_before_resume(self):
        invalid = [dict(self.response, model='gpt-6-sol'),
                   dict(self.response, reasoningEffort='xhigh'),
                   dict(self.response, cwd=str(self.cwd/'other')),
                   dict(self.response, runtimeWorkspaceRoots=[str(self.cwd/'other')])]
        for delta in ({'type':'readOnly'}, {'networkAccess':True}, {'writableRoots':[str(self.cwd.parent)]}):
            value = copy.deepcopy(self.response); value['sandbox'].update(delta); invalid.append(value)
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'pre-turn policy'):
                N.validate_policy(value, self.cwd, 'gpt-6-luna')

    def test_preturn_transport_never_sends_model_turn_even_when_policy_rejected(self):
        response = copy.deepcopy(self.response); response['sandbox']['type']='readOnly'
        proc = Mock()
        proc.stdout = io.StringIO('\n'.join(json.dumps(x) for x in
            [{'id':1,'result':{}}, {'id':2,'result':response}])+'\n')
        proc.stderr=io.StringIO('');proc.stdin=io.StringIO()
        proc.stdin.close=Mock()
        receipt=self.cwd/'native_preflight.json'
        with patch.object(N,'native_command',return_value=['codex']), patch.object(N.subprocess,'Popen',return_value=proc):
            with self.assertRaisesRegex(RuntimeError,'pre-turn gate'):
                N.prepare_session(self.cwd,'gpt-6-luna',receipt)
        methods=[json.loads(line)['method'] for line in proc.stdin.getvalue().splitlines()]
        self.assertEqual(methods,['initialize','initialized','thread/start'])
        self.assertFalse(json.loads(receipt.read_text())['passed'])
        proc.stdin.close.assert_called_once()
        proc.wait.assert_called_once()

    def test_same_process_gate_precedes_only_metered_turn_and_preserves_raw_events(self):
        messages=[{'id':1,'result':{}},{'id':2,'result':self.response},
            {'id':3,'result':{'turn':{'id':'turn-one'}}},
            {'method':'item/completed','params':{'threadId':'test-session','item':
                {'type':'commandExecution','command':'fixed','exitCode':0,'aggregatedOutput':'observed'}}},
            {'method':'turn/completed','params':{'threadId':'test-session','turn':{'id':'turn-one','status':'completed'}}}]
        proc=Mock();proc.stdout=io.StringIO('\n'.join(json.dumps(x) for x in messages)+'\n')
        proc.stderr=io.StringIO('');proc.stdin=io.StringIO();proc.stdin.close=Mock();proc.returncode=0
        call=self.cwd/'call';call.mkdir()
        proof=self.cwd/'native_preflight.json'
        with patch.object(N,'native_command',return_value=['codex']),patch.object(N.subprocess,'Popen',return_value=proc) as popen:
            session=N.prepare_session(self.cwd,'gpt-6-luna',proof)
            self.assertTrue(json.loads(proof.read_text())['passed'])
            self.assertNotIn('turn/start',proc.stdin.getvalue())
            proc.stdin.close.assert_not_called()
            receipt,raw=session.invoke('frozen prompt',call,60,self.cwd/'cancel.request')
            self.assertEqual(receipt['exit_code'],0)
            self.assertTrue(receipt['turn_completed'])
            self.assertIn('"exit_code": 0',raw)
            self.assertIn('"exitCode": 0',(call/'native_stream.jsonl').read_text())
            self.assertEqual(popen.call_count,1)
            self.assertIs(session.params['config']['features.memories'],False)
            self.assertIs(session.params['config']['features.multi_agent'],False)
            self.assertIn('memories',session.argv)
            self.assertIn('multi_agent',session.argv)
            turns=[json.loads(x) for x in proc.stdin.getvalue().splitlines() if json.loads(x)['method']=='turn/start']
            self.assertEqual(len(turns),1)
            self.assertEqual(turns[0]['params']['threadId'],'test-session')
            with self.assertRaises(RuntimeError): session.invoke('again',call,60,self.cwd/'cancel.request')

    def test_failed_and_foreign_notifications_cannot_certify_completion(self):
        self.assertIsNone(N.canonical_event({'method':'turn/completed','params':{'threadId':'other','turn':{'status':'completed'}}},'sid'))
        self.assertEqual(N.canonical_event({'method':'turn/completed','params':{'threadId':'sid','turn':{'status':'failed','error':'provider'}}},'sid')['type'],'turn.failed')

    def test_fullpath_runtime_is_hash_pinned_without_launcher_lookup(self):
        exe=self.cwd/'python.exe';exe.write_bytes(b'existing-runtime')
        with patch.object(W,'ROOT',self.cwd):
            command=W.fixed_entry_command(self.cwd,{'native_worker_runtime':{'path':str(exe),'sha256':digest(exe)}})
            self.assertTrue(command.startswith("& '"+str(exe)+"' -X utf8"))
            with self.assertRaisesRegex(RuntimeError,'runtime changed'):
                W.fixed_entry_command(self.cwd,{'native_worker_runtime':{'path':str(exe),'sha256':'bad'}})

    def test_saved_native_configuration_requires_exact_historical_receipts(self):
        call=self.cwd/'runtime/jobs/fixture/worker_call_1';call.mkdir(parents=True)
        work=call.parent/'work';work.mkdir()
        response=copy.deepcopy(self.response);response['cwd']=str(work);response['runtimeWorkspaceRoots']=[str(work)]
        response['thread']['originator']='Codex Desktop'
        with patch.object(N,'ROOT',self.cwd):params=N.thread_parameters(work,'gpt-6-luna')
        del params['config']['features.memories'];del params['config']['features.multi_agent']
        argv=['codex','app-server','-c','model_reasoning_effort="max"']
        proof=call.parent/'native_preflight_1.json'
        write(proof,{'passed':True,'no_model_turn_sent':True,'argv':argv,'thread_start_params':params,'response':response})
        request={'transport':'native_app_server','cwd':str(work),'argv':argv,'thread_start_params':params,
                 'native_session_id':'test-session','preflight_path':str(proof)}
        write(call/'request.json',request)
        exported={'info':{'id':'test-session','model':W.codex_identity('gpt-6-luna','max')},
                  'identity_evidence':{'request_sha256':digest(call/'request.json')}}
        completion=call.parent/'worker_completion_1.json';write(completion,exported)
        authority=self.cwd/'authority.json';write(authority,{'existing_historical_authority':True})
        inputs={name:self.cwd/name for name in ('job.json','fixture.json','analysis.py','result.json')}
        for path in inputs.values():path.write_text('{}',encoding='utf-8')
        def pin(p):return {'path':p.relative_to(self.cwd).as_posix(),'sha256':digest(p)}
        record={'request':pin(call/'request.json'),'preflight':pin(proof),'completion':pin(completion),
                'authority':pin(authority)}
        record.update({name:pin(path) for name,path in inputs.items()})
        from tools.quant_research_loop import desktop_capture as D
        with patch.object(W,'ROOT',self.cwd),patch.object(N,'ROOT',self.cwd),patch.object(D,'recovery_context',return_value={'reconciliation':{'saved_native_proofs':[record]}}):
            self.assertTrue(W.completion_matches(call,exported,{}))
            for field,value in (('native_session_id','different'),('cwd',str(self.cwd)),
                                ('thread_start_params',dict(params,approvalPolicy='never')),
                                ('thread_start_params',dict(params,model='gpt-6.1-sol')),
                                ('argv',['codex','app-server','-c','model_reasoning_effort="high"'])):
                changed=copy.deepcopy(request);changed[field]=value;write(call/'request.json',changed)
                self.assertFalse(W.completion_matches(call,exported,{}))
            write(call/'request.json',request)
            original=proof.read_bytes();changed=copy.deepcopy(response);changed['sandbox']['networkAccess']=True
            item=json.loads(original);item['response']=changed;write(proof,item)
            self.assertFalse(W.completion_matches(call,exported,{}));proof.write_bytes(original)
            item=json.loads(original);item['response']['runtimeWorkspaceRoots']=[str(self.cwd)]
            write(proof,item);self.assertFalse(W.completion_matches(call,exported,{}));proof.write_bytes(original)
            for path in inputs.values():
                path.write_text('{"changed":true}',encoding='utf-8')
                self.assertFalse(W.completion_matches(call,exported,{}))
                path.write_text('{}',encoding='utf-8')
            write(authority,{'changed':True})
            self.assertFalse(W.completion_matches(call,exported,{}))


if __name__=='__main__':unittest.main()

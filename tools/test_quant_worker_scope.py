"""Scoped route controls on temporary fixtures; no provider/data calls."""
import json
import hashlib
import os
import zlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools.quant_research_loop import worker_scope as S
from tools.quant_research_loop import generic_worker as W
from tools.quant_research_loop import safety_hold as H


class WorkerScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.patch=patch.object(S,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)
        self.job={'job_id':'probe','inputs':[{'path':'fixture.json','kind':'json'}],
                  'allowed_file_scope':['work/analysis.py','work/result.json']}
        self.fixed='py -3.12 -X utf8 -m fixed'

    def literal(self,path=None,source='def run(ctx):\n    return {"x": 5}'):
        return "@'\n"+source+"\n'@ | Set-Content -LiteralPath "+S.quoted(path or self.root/'work/analysis.py')+' -Encoding utf8'

    def test_literal_write_only_exact_source_and_no_statement_escape(self):
        self.assertEqual(S.command_kind(self.literal(),self.job,self.fixed,[]),'literal_scoped_write')
        for cmd in [self.literal(self.root/'work/result.json'),self.literal(self.root/'else.py'),
                    self.literal()+'; Remove-Item data',self.literal(source="'@\nGet-Content secrets\n@'"),
                    self.fixed+'; other',self.fixed+' --other']:
            with self.subTest(cmd=cmd),self.assertRaisesRegex(RuntimeError,'HUMAN_ACTION_REQUIRED'):
                S.command_kind(cmd,self.job,self.fixed,[])
        read=S.inspections(self.job)[0]
        self.assertEqual(S.command_kind(read+'; '+read,self.job,self.fixed,[]),'pinned_read')

    def test_ignored_files_detected_without_reading_content(self):
        ignored=self.root/'ignored.bin';ignored.write_bytes(b'first')
        with patch.object(Path,'read_bytes',side_effect=AssertionError('content read')):
            before=S.metadata_snapshot(self.root)
        ignored.write_bytes(b'longer after')
        after=S.metadata_snapshot(self.root)
        changes,unexpected=S.metadata_diff(before,after,{'work/analysis.py'},'call/')
        self.assertEqual(unexpected,['ignored.bin']);self.assertIn('ignored.bin',changes)

    def storage_fixture(self, data=b'bounded fixture'):
        raw=b'blob '+str(len(data)).encode()+b'\0'+data
        oid=hashlib.sha1(raw).hexdigest()
        name='.git/objects/'+oid[:2]+'/'+oid[2:]
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(zlib.compress(raw))
        value=path.stat()
        after=[value.st_size,value.st_mtime_ns,value.st_ctime_ns,'FILE']
        proof={'write_policy_valid':True,'pretool_policy':{'path':'proof'},
               'errors':[],'protocol_errors':[],'unparsed_tools':[]}
        return path,name,after,proof

    def test_storage_classification_validates_identity_without_changing_raw_diff(self):
        path,name,after,proof=self.storage_fixture()
        changes,unexpected=S.metadata_diff({}, {name:after},set(),'call/')
        classified=S.validated_git_storage(changes,unexpected,proof,self.root)
        self.assertEqual(unexpected,[name])
        self.assertEqual(classified['remaining_unexpected'],[])
        self.assertEqual(classified['validated_storage'][name]['change'],'ADDITION')
        before=list(after);before[1]-=1
        changed=S.validated_git_storage({name:{'before':before,'after':after}},[name],proof,self.root)
        self.assertEqual(changed['validated_storage'][name]['change'],'MTIME_ONLY')
        for protected in ('.git/HEAD','.git/refs/heads/main','.git/index','.git/config','.git/packed-refs',
                          '.git/objects/info/alternates','ignored.bin','work/outside.py'):
            result=S.validated_git_storage({protected:{'before':None,'after':after}},[protected],proof,self.root)
            self.assertEqual(result['remaining_unexpected'],[protected])

    def test_storage_rejects_removed_resized_ctime_changed_corrupt_and_unclean_native(self):
        path,name,after,proof=self.storage_fixture()
        for before,later in ((after,None),([after[0]+1,*after[1:]],after),
                             ([after[0],after[1],after[2]-1,'FILE'],after),
                             (None,[*after[:3],'LINK_NOT_FOLLOWED'])):
            result=S.validated_git_storage({name:{'before':before,'after':later}},[name],proof,self.root)
            self.assertEqual(result['remaining_unexpected'],[name])
        for field in ('errors','protocol_errors','unparsed_tools'):
            with self.assertRaisesRegex(RuntimeError,'clean native permission evidence'):
                S.validated_git_storage({},[],dict(proof,**{field:['failed']}),self.root)
        path.write_bytes(zlib.compress(b'blob 8\0wrong'))
        value=path.stat();corrupt=[value.st_size,value.st_mtime_ns,value.st_ctime_ns,'FILE']
        result=S.validated_git_storage({name:{'before':None,'after':corrupt}},[name],proof,self.root)
        self.assertEqual(result['remaining_unexpected'],[name])
        path.unlink()
        self.assertEqual(S.validated_git_storage({name:{'before':None,'after':after}},[name],proof,self.root)
                         ['remaining_unexpected'],[name])

    def test_storage_pack_checksum_and_links_stay_strict(self):
        header=b'PACK'+(2).to_bytes(4,'big')+(0).to_bytes(4,'big')
        oid=hashlib.sha1(header).hexdigest()
        name='.git/objects/pack/pack-'+oid+'.pack'
        path=self.root/name;path.parent.mkdir(parents=True);path.write_bytes(header+bytes.fromhex(oid))
        value=path.stat();after=[value.st_size,value.st_mtime_ns,value.st_ctime_ns,'FILE']
        proof={'write_policy_valid':True,'pretool_policy':{'path':'proof'},'errors':[],'protocol_errors':[],'unparsed_tools':[]}
        before=list(after);before[1]-=1
        self.assertEqual(S.validated_git_storage({name:{'before':None,'after':after}},[name],proof,self.root)
                         ['remaining_unexpected'],[name])
        result=S.validated_git_storage({name:{'before':before,'after':after}},[name],proof,self.root)
        self.assertEqual(result['validated_storage'][name]['kind'],'PACK')
        with patch.object(Path,'is_symlink',return_value=True):
            self.assertEqual(S.validated_git_storage({name:{'before':None,'after':after}},[name],proof,self.root)
                             ['remaining_unexpected'],[name])
        path.write_bytes(header+b'x'*20)
        value=path.stat();later=[value.st_size,value.st_mtime_ns,value.st_ctime_ns,'FILE']
        self.assertEqual(S.validated_git_storage({name:{'before':None,'after':later}},[name],proof,self.root)
                         ['remaining_unexpected'],[name])

    def test_git_classification_requires_actual_native_audit_before_content_read(self):
        from types import SimpleNamespace
        path,name,after,proof=self.storage_fixture()
        call=self.root/'call';call.mkdir()
        commands=[{'kind':'fixed_entry','exit_code':0}]
        proof.update(native_session_id='session',commands=commands)
        (call/'native_policy_and_commands.json').write_text(json.dumps(proof),encoding='utf-8')
        events=[]
        def audited(*args):
            events.append('native_audit');return commands
        def classify(*args,**kwargs):
            events.append('storage_validation');return {'validated_storage':{name:{}},'remaining_unexpected':[]}
        with patch.object(W,'ROOT',self.root),patch.object(W,'worker_session_id',return_value='session'),\
                patch.object(W,'call_route',return_value='codex'),patch.object(W,'audit_worker_attempt',side_effect=audited),\
                patch.object(S,'validated_git_storage',side_effect=classify) as classification:
            result=W.classify_git_metadata('raw','fixed',SimpleNamespace(),call,self.job,0,
                                           {name:{'before':None,'after':after}},[name])
            self.assertEqual(events,['native_audit','storage_validation'])
            self.assertIn('native_permission_evidence',result)
            with patch.object(W,'audit_worker_attempt',side_effect=RuntimeError('native policy violation')):
                with self.assertRaisesRegex(RuntimeError,'native policy violation'):
                    W.classify_git_metadata('raw','fixed',SimpleNamespace(),call,self.job,0,{},[])
            self.assertEqual(classification.call_count,1)

    def test_saved_metadata_failure_cannot_be_reused_without_original_bound_classification(self):
        from types import SimpleNamespace
        path,name,after,proof=self.storage_fixture()
        folder=self.root/'job';folder.mkdir();call=folder/'worker_call_0';call.mkdir()
        raw={'changes':{name:{'before':None,'after':after}},'unexpected':[name]}
        scope=folder/'metadata_scope_check_0.json';scope.write_text(json.dumps(raw))
        with patch.object(W,'ROOT',self.root),patch.object(W,'worker_session_id',return_value='session'):
            with self.assertRaisesRegex(RuntimeError,'no original complete classification'):
                W.recover_existing(SimpleNamespace(),self.job,folder,'fixed')
            for file,value in (('receipt.json',{}),('request.json',{'cwd':str(self.root)}),('stdout.jsonl',{})):
                (call/file).write_text(json.dumps(value))
            pretool=folder/'native_preflight_0.json';pretool.write_text('{}')
            def pin(p):return {'path':p.relative_to(self.root).as_posix(),'sha256':W.digest(p)}
            for label,value in (('before',{}),('after',{name:after})):
                snapshot=folder/f'metadata_scope_{label}_0.json';snapshot.write_text(json.dumps(value))
                raw[label]=pin(snapshot)
            scope.write_text(json.dumps(raw))
            proof.update(pretool_policy=pin(pretool),native_session_id='session',request={'cwd':str(self.root)})
            native=call/'native_policy_and_commands.json';native.write_text(json.dumps(proof))
            classification=S.validated_git_storage(raw['changes'],[name],proof,self.root)
            classification.update(raw_metadata_scope=pin(scope),native_permission_evidence=pin(native),
                                  worker_receipt=pin(call/'receipt.json'),worker_stream=pin(call/'stdout.jsonl'),
                                  worker_request=pin(call/'request.json'),raw_unexpected=[name])
            stored=folder/'metadata_storage_check_0.json';stored.write_text(json.dumps(classification))
            W.require_saved_metadata_clean(folder)
            original=stored.read_bytes();old=path.stat()
            os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns+1000000))
            W.require_saved_metadata_clean(folder)
            self.assertEqual(stored.read_bytes(),original)
            self.assertEqual(S.validated_git_storage(raw['changes'],[name],proof,self.root)['remaining_unexpected'],[name])
            classification['remaining_unexpected']=[name];stored.write_text(json.dumps(classification))
            with self.assertRaisesRegex(RuntimeError,'no reuse or redispatch'):W.require_saved_metadata_clean(folder)
            classification['remaining_unexpected']=[];stored.write_text(json.dumps(classification))
            # Same object identity with a different compressed representation
            # cannot reuse the original exact file proof.
            original_storage=path.read_bytes()
            replacement=zlib.compress(zlib.decompress(original_storage),level=1)
            self.assertEqual(len(replacement),len(original_storage))
            self.assertNotEqual(replacement,original_storage)
            path.write_bytes(replacement)
            with self.assertRaisesRegex(RuntimeError,'no reuse or redispatch'):W.require_saved_metadata_clean(folder)
            path.write_bytes(b'x'*len(original_storage))
            with self.assertRaisesRegex(RuntimeError,'no reuse or redispatch'):W.require_saved_metadata_clean(folder)

    def test_native_arguments_not_display_strings_and_policy_preserved(self):
        call=self.root/'call';call.mkdir()
        (call/'request.json').write_text(json.dumps({'cwd':str(self.root),'argv':['codex']}))
        sid='01234567-1234-1234-1234-123456789abc'
        sessions=self.root/'home/sessions';sessions.mkdir(parents=True)
        context={'cwd':str(self.root),'approval_policy':'on-request',
                 'sandbox_policy':{'type':'workspace-write'},'model':'gpt-6-luna','effort':'max'}
        events=[{'type':'turn_context','payload':context}]
        for cmd in [self.literal(),self.fixed]:
            events.append({'type':'response_item','payload':{'type':'function_call','name':'exec_command',
                           'arguments':json.dumps({'cmd':cmd,'workdir':str(self.root)}),'call_id':'actual'}})
            events.append({'type':'event_msg','payload':{'type':'item_completed','item':{
                'type':'CommandExecution','id':'actual','command':['pwsh.exe','-Command',cmd],
                'cwd':str(self.root),'exit_code':0}}})
        log=sessions/('rollout-'+sid+'.jsonl')
        log.write_text('\n'.join(json.dumps(e) for e in events),encoding='utf-8')
        with patch.dict('os.environ',{'CODEX_HOME':str(self.root/'home')}):
            result=S.native_session_receipt(call,sid,self.job,self.fixed,[])
            self.assertEqual(result['policy_contexts'],[context])
            self.assertEqual(result['commands'][0]['kind'],'literal_scoped_write')
            events[-2]['payload']['arguments']=json.dumps({'cmd':self.fixed+'; bad'})
            log.write_text('\n'.join(json.dumps(e) for e in events),encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError,'HUMAN_ACTION_REQUIRED'):
                S.native_session_receipt(call,sid,self.job,self.fixed,[])

    def test_stop_does_not_become_failed_hold_release(self):
        from types import SimpleNamespace
        state={'operator_safety_hold':{'active':True}}
        self.assertFalse(H.release_after_review(SimpleNamespace(state=state),{'action':'HUMAN_ACTION_REQUIRED'}))
        self.assertTrue(state['operator_safety_hold']['active'])

    def test_new_prompt_enables_native_persistence_only(self):
        profile={'worker_provider':'codex','worker':'gpt-6-luna','worker_variant':'max'}
        with patch.object(W,'native_command',return_value=['codex']):
            old=W.worker_command(profile,'legacy',cwd=self.root)
            new=W.worker_command(profile,'ACTIVE COMMAND POLICY: SCOPED_LITERAL_V1',cwd=self.root)
        self.assertIn('--ephemeral',old);self.assertNotIn('--ephemeral',new)
        self.assertIn('windows.sandbox="elevated"',new)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',new)

    def test_native_executor_argv_unwrap_and_stop_order(self):
        def event(cmd,start,end,exit_code=0):
            return {'type':'event_msg','payload':{'type':'item_completed','started_at_ms':start,'completed_at_ms':end,
                'item':{'type':'CommandExecution','id':str(start),'command':['pwsh.exe','-NoProfile','-Command',cmd],
                        'cwd':self.root.as_uri(),'exit_code':exit_code}}}
        cmds,errors=S.native_command_events([event(self.fixed,1,2)],self.job,self.fixed,[],str(self.root))
        self.assertFalse(errors);self.assertEqual(cmds[0]['kind'],'fixed_entry')
        with self.assertRaisesRegex(RuntimeError,'unsupported native'):
            bad=event(self.fixed,1,2);bad['payload']['item']['command']=['pwsh.exe','-EncodedCommand','abc']
            S.native_command_events([bad],self.job,self.fixed,[],str(self.root))

    def test_post_error_continuation_stops_controller_and_autonomous_recovery(self):
        from tools.quant_research_loop import controller as C, lead_recovery as L, autonomous_recovery as A
        call=self.root/'worker_call_0';call.mkdir()
        (call/'request.json').write_text(json.dumps({'cwd':str(self.root),'argv':['codex','-m','gpt-6-luna','-c','model_reasoning_effort="max"']}))
        (call/'prompt.txt').write_text('ACTIVE COMMAND POLICY: SCOPED_LITERAL_V1')
        (self.root/'job.json').write_text(json.dumps(self.job))
        sid='01234567-1234-1234-1234-123456789abc'
        sessions=self.root/'home/sessions';sessions.mkdir(parents=True)
        context={'cwd':str(self.root),'sandbox_policy':{'type':'workspace-write'},'model':'gpt-6-luna','effort':'max'}
        events=[{'type':'turn_context','payload':context}]
        for command,start,code in [(S.inspections(self.job)[0],10,1),(self.fixed,30,0)]:
            events.append({'type':'event_msg','payload':{'type':'item_completed','started_at_ms':start,'completed_at_ms':start+10,
                'item':{'type':'CommandExecution','id':str(start),'command':['pwsh.exe','-Command',command],
                        'cwd':str(self.root),'exit_code':code}}})
        log=sessions/('rollout-'+sid+'.jsonl');log.write_text('\n'.join(json.dumps(e) for e in events))
        c=object.__new__(C.Controller)
        c.state={'status':'INITIALIZED','pending_job':self.job,'model_calls':11,'trials':1,'completed':{}}
        c.profile={'max_calls':1000};c.save=lambda:None;c.check_budget=lambda:None
        c.worker=lambda job:W.audit_worker_attempt(json.dumps({'type':'thread.started','thread_id':sid}),self.fixed,c,call,job,0)
        c.lead=lambda: self.fail('Safety breach must not request ordinary Lead recovery')
        with patch.dict('os.environ',{'CODEX_HOME':str(self.root/'home')}),patch.object(W,'ROOT',self.root),patch.object(C,'RUNTIME',self.root/'loop_runtime'),patch.object(L,'ROOT',self.root),patch.object(L,'BASE',self.root/'revision'),patch.object(L,'enabled',return_value=True),patch('tools.quant_research_loop.skill_engineering.maybe_review',return_value=None):
            result=c.run()
        self.assertEqual(result['status'],'HUMAN_ACTION_REQUIRED')
        self.assertIn('stop-condition violation',result['error'])
        self.assertEqual((result['model_calls'],result['trials'],result['completed']),(11,1,{}))
        self.assertNotIn('worker_dispatch_decision',result)
        with patch.object(A,'authority',return_value={'approved':True}):
            self.assertEqual(A.recover(c)['action'],'STOP')
        self.assertTrue(json.loads((call/'native_policy_and_commands.json').read_text())['protocol_errors'])
        events.pop();log.write_text('\n'.join(json.dumps(e) for e in events))
        with patch.dict('os.environ',{'CODEX_HOME':str(self.root/'home')}):
            with self.assertRaises(S.ScopedPreEntryIncomplete):S.native_session_receipt(call,sid,self.job,self.fixed,[])

    def test_current_readonly_receipt_fails_closed_and_is_preserved(self):
        call=self.root/'call';call.mkdir()
        (call/'request.json').write_text(json.dumps({'cwd':str(self.root),'argv':['codex']}))
        sid='01234567-1234-1234-1234-123456789abc'
        sessions=self.root/'home/sessions';sessions.mkdir(parents=True)
        context={'cwd':str(self.root),'approval_policy':'on-request','sandbox_policy':{'type':'read-only'},
                 'model':'gpt-6-luna','effort':'max'}
        events=[{'type':'turn_context','payload':context},
                {'type':'response_item','payload':{'type':'custom_tool_call','name':'exec',
                 'input':'text(await tools.exec_command({cmd:"fixed"}));'}},
                {'type':'event_msg','payload':{'type':'item_completed','item':{'type':'CommandExecution',
                 'command':['pwsh.exe','-Command',self.fixed],'cwd':str(self.root),'exit_code':0}}}]
        log=sessions/('rollout-'+sid+'.jsonl');log.write_text('\n'.join(json.dumps(e) for e in events))
        original=call/'native_policy_and_commands.json';original.write_text('{"historical":"unknown"}')
        with patch.dict('os.environ',{'CODEX_HOME':str(self.root/'home')}):
            with self.assertRaisesRegex(RuntimeError,'effective scoped write policy'):
                S.native_session_receipt(call,sid,self.job,self.fixed,[])
        self.assertEqual(original.read_text(),'{"historical":"unknown"}')
        receipt=json.loads((call/'native_policy_and_commands_parsed_v3.json').read_text())
        self.assertFalse(receipt['write_policy_valid']);self.assertEqual(receipt['unparsed_tools'],[])

    def patch_events(self, target=None, failed=False):
        target = target or self.root / 'work/analysis.py'
        literal = '*** Begin Patch\n*** Update File: ' + str(target) + '\n@@\n-old\n+new\n*** End Patch'
        code = 'const patch = ' + json.dumps(literal) + ';\nconst r = await tools.apply_patch(patch);\ntext(r);\n'
        events = [{'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec',
                   'input': code, 'call_id': 'patch-call'}}]
        if not failed:
            events.append({'type': 'event_msg', 'payload': {'type': 'item_completed', 'started_at_ms': 10,
                           'completed_at_ms': 20, 'item': {'type': 'FileChange', 'id': 'native-edit',
                           'status': 'completed', 'changes': {str(target): {'type': 'update', 'move_path': None}}}}})
        events.append({'type': 'response_item', 'payload': {'type': 'custom_tool_call_output',
                       'call_id': 'patch-call', 'output': [{'type': 'input_text',
                       'text': 'apply_patch verification failed: expected lines missing' if failed else 'Success'}]}})
        return events

    def test_literal_patch_wrapper_bound_to_native_file_change(self):
        events = self.patch_events()
        edits, errors, unknown, protocol, wrappers = S.native_patch_events(events, self.job, self.root)
        self.assertEqual((errors, unknown, protocol), ([], [], []))
        self.assertEqual(wrappers, {'patch-call'})
        self.assertEqual(edits[0]['native_file_change_ids'], ['native-edit'])
        self.assertEqual(edits[0]['status'], 'completed')
        literal = S.literal_patch_wrapper(events[0]['payload']['input'])
        self.assertEqual(S.literal_patch_wrapper('text(await tools.apply_patch(' + json.dumps(literal) + '));'), literal)

    def test_dynamic_or_extra_javascript_and_scope_mismatch_fail_closed(self):
        good = self.patch_events()[0]['payload']['input']
        for code in ('text(await tools.apply_patch(patch));', good + 'text(await tools.exec_command({cmd:"git status"}));',
                     good.replace('tools.apply_patch(patch)', 'tools.apply_patch(patch + "extra")'),
                     'text(await tools["apply_patch"]("patch"));'):
            with self.subTest(code=code), self.assertRaises(ValueError):
                S.literal_patch_wrapper(code)
        edits, errors, _, _, _ = S.native_patch_events(self.patch_events(self.root / 'outside.py'), self.job, self.root)
        self.assertFalse(edits)
        self.assertTrue(any('outside frozen' in error for error in errors))
        events = self.patch_events()
        events[1]['payload']['item']['changes'] = {str(self.root / 'outside.py'): {'type': 'update'}}
        self.assertTrue(S.native_patch_events(events, self.job, self.root)[1])

    def test_missing_moved_deleted_or_overlapping_native_patch_receipts_rejected(self):
        events = self.patch_events()
        del events[1]
        self.assertTrue(S.native_patch_events(events, self.job, self.root)[2])
        for change in ({'type': 'delete'}, {'type': 'update', 'move_path': str(self.root / 'else.py')}):
            events = self.patch_events()
            events[1]['payload']['item']['changes'] = {str(self.root / 'work/analysis.py'): change}
            self.assertTrue(S.native_patch_events(events, self.job, self.root)[1])
        events = self.patch_events()
        events.insert(1, {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'other'}})
        self.assertTrue(S.native_patch_events(events, self.job, self.root)[2])

    def test_failed_edit_must_stop_before_any_later_tools(self):
        events = self.patch_events(failed=True)
        edits, errors, unknown, protocol, _ = S.native_patch_events(events, self.job, self.root)
        self.assertEqual((errors, unknown, protocol), ([], [], []))
        self.assertEqual(edits[0]['status'], 'failed_before_change')
        events.append({'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command'}})
        self.assertEqual(S.native_patch_events(events, self.job, self.root)[3],
                         ['Worker issued tools after a failed edit'])

    def test_patch_support_preserves_normal_fixed_runner_gate(self):
        call = self.root / 'call'; call.mkdir()
        (call / 'request.json').write_text(json.dumps({'cwd': str(self.root), 'argv': ['codex']}))
        sid = '01234567-1234-1234-1234-123456789abc'
        sessions = self.root / 'home/sessions'; sessions.mkdir(parents=True)
        context = {'cwd': str(self.root), 'approval_policy': 'on-request',
                   'sandbox_policy': {'type': 'workspace-write'}, 'model': 'gpt-6-luna', 'effort': 'max'}
        command = S.inspections(self.job)[0]
        events = [{'type': 'turn_context', 'payload': context}, {'type': 'event_msg', 'payload': {
            'type': 'item_completed', 'started_at_ms': 1, 'completed_at_ms': 2,
            'item': {'type': 'CommandExecution', 'id': 'read', 'command': ['pwsh.exe', '-Command', command],
                     'cwd': str(self.root), 'exit_code': 0}}}, *self.patch_events()]
        events.append({'type': 'event_msg', 'payload': {'type': 'item_completed', 'started_at_ms': 30,
                       'completed_at_ms': 40, 'item': {'type': 'CommandExecution', 'id': 'runner',
                       'command': ['pwsh.exe', '-Command', self.fixed], 'cwd': str(self.root), 'exit_code': 0}}})
        log = sessions / ('rollout-' + sid + '.jsonl')
        log.write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
        with patch.dict('os.environ', {'CODEX_HOME': str(self.root / 'home')}):
            receipt = S.native_session_receipt(call, sid, self.job, self.fixed, [])
            self.assertEqual(receipt['edits'][0]['status'], 'completed')
            events.pop()
            log.write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
            with self.assertRaises(S.ScopedPreEntryIncomplete) as failed:
                S.native_session_receipt(call, sid, self.job, self.fixed, [])
            self.assertEqual(failed.exception.receipt['native_session_id'], sid)
            self.assertTrue(failed.exception.receipt_path.exists())
            self.assertEqual(failed.exception.receipt['errors'], [])
            for n in range(9):
                events.append({'type': 'event_msg', 'payload': {'type': 'item_completed',
                    'started_at_ms': 100 + n * 2, 'completed_at_ms': 101 + n * 2,
                    'item': {'type': 'CommandExecution', 'id': 'test-' + str(n),
                             'command': ['pwsh.exe', '-Command', 'scoped-test'],
                             'cwd': str(self.root), 'exit_code': 0}}})
            log.write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'HUMAN_ACTION_REQUIRED: scoped test call limit exceeded'):
                S.native_session_receipt(call, sid, self.job, self.fixed, ['scoped-test'])


if __name__=='__main__':unittest.main()

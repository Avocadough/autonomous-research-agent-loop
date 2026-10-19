import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.quant_research_loop import qa_review as Q


class SolQATests(unittest.TestCase):
    def test_qa_accepts_exact_completed_cli_fallback_but_not_unknown_errors(self):
        message = ('Falling back from WebSockets to HTTPS transport. stream disconnected before completion: '
                   'No connection could be made because the target machine actively refused it. (os error 10061)')
        for notice in (message, 'unknown runtime error'):
            with self.subTest(notice=notice), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                controller = Mock()
                controller.state = {'model_calls':10}
                controller.profile = {'max_calls':1000}
                report = self.report()
                events = [{'type':'thread.started','thread_id':'fixture'}, {'type':'turn.started'},
                    {'type':'item.completed','item':{'type':'error','message':notice}},
                    {'type':'item.completed','item':{'type':'agent_message','text':json.dumps(report)}},
                    {'type':'turn.completed'}]
                raw = '\n'.join(map(json.dumps, events))
                def invoke(cmd, prompt, folder, **kwargs):
                    folder.mkdir()
                    (folder.parent/'response.json').write_text(json.dumps(report),encoding='utf-8')
                    return {'exit_code':0,'process_reaped':True}, raw
                controller.model_call.side_effect = invoke
                with patch.object(Q,'ROOT',root), patch.object(Q,'native_command',return_value=['codex']), \
                        patch.object(Q,'authority',return_value={'model':'gpt-6.1-sol','reasoning':'high'}):
                    if notice == message:
                        result = Q.review_bundle(controller,self.bundle())
                        self.assertEqual(result['report'],report)
                        self.assertEqual(controller.state['model_calls'],10)
                    else:
                        with self.assertRaisesRegex(RuntimeError,'forbidden tool'):
                            Q.review_bundle(controller,self.bundle())
                controller.model_call.assert_called_once()

    def test_required_evidence_preflight_blocks_qa_call_before_reservation(self):
        from tools.quant_research_loop import skill_engineering as S
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            controller=Mock()
            controller.state={'model_calls':600}
            controller.profile={'max_calls':1000}
            bundle={'review_key':'key','files':{'receipt.json':{'truncated':True}},
                    'required_complete_files':['receipt.json','missing.json']}
            with patch.object(Q,'ROOT',root),patch.object(Q,'authority',return_value={'model':'gpt-6.1-sol'}),\
                    patch.object(Q,'target',return_value=root),patch.object(Q,'build_bundle',return_value=bundle),\
                    patch.object(S,'maybe_review'),\
                    patch('tools.quant_research_loop.runtime_policy.remaining_call_units') as reserve:
                with self.assertRaisesRegex(RuntimeError,'LEAD_PREREQUISITE:.*receipt.json.*missing.json'):
                    Q.review(controller)
            controller.model_call.assert_not_called()
            reserve.assert_not_called()
            controller.save.assert_not_called()
            self.assertEqual(controller.state,{'model_calls':600})
            self.assertEqual(list(root.iterdir()),[])

    def test_truncated_recovery_context_blocks_qa_call_before_reservation(self):
        from tools.quant_research_loop import skill_engineering as S
        controller=Mock();controller.state={'model_calls':605};controller.profile={'max_calls':1000}
        bundle={'review_key':'key','files':{},'required_complete_files':[],
                'recovery_context':{'content':'','truncated':True}}
        with patch.object(Q,'authority',return_value={'model':'gpt-6.1-sol'}),\
                patch.object(Q,'target',return_value=Path('fixture')),\
                patch.object(Q,'build_bundle',return_value=bundle),patch.object(S,'maybe_review'),\
                patch('tools.quant_research_loop.runtime_policy.remaining_call_units') as reserve:
            with self.assertRaisesRegex(RuntimeError,'LEAD_PREREQUISITE:.*recovery_context'):
                Q.review(controller)
        reserve.assert_not_called();controller.model_call.assert_not_called();controller.save.assert_not_called()
        self.assertEqual(controller.state,{'model_calls':605})
        complete=self.bundle();complete['recovery_context']={'truncated':True}
        with self.assertRaisesRegex(ValueError,'incomplete recovery context'):
            Q.validate_report(self.report(),complete)

    def test_current_incident_disposition_preserves_active_state_and_checks_release_proofs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)

            def proof(name,value):
                path=root/name
                path.write_text(json.dumps(value),encoding='utf-8')
                return {'path':name,'sha256':Q.digest(path)}

            incident=proof('incident.json',{'incident_id':'internal'})
            refs={key:proof(key+'.json',{'historical_source_hashes':{'old.py':'old-pin'}})
                  for key in ('authorization','repair_packet','independent_qa','lead_decision')}
            call=proof('receipt.json',{'exit_code':0})
            release=proof('release.json',dict(refs,incident=incident,lead_call_proofs=[call]))
            state={'status':'PAUSED_LEAD','operator_safety_hold':{'active':False,'incident':'incident.json'},
                   'delegated_incident_review_release':release}
            with patch.object(Q,'ROOT',root):
                disposition,proofs=Q.current_incident_disposition(state)
                self.assertFalse(disposition['active_hold'])
                self.assertTrue(disposition['release_matches_current_incident'])
                self.assertFalse(disposition['historical_result_reclassified'])
                self.assertFalse(disposition['worker_dispatch_authorized'])
                self.assertEqual(len(proofs),7)
                state['operator_safety_hold']['active']=True
                self.assertTrue(Q.current_incident_disposition(state)[0]['active_hold'])
                (root/'receipt.json').write_text('{}',encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'proof identity changed'):
                    Q.current_incident_disposition(state)
                state.pop('delegated_incident_review_release')
                active,proofs=Q.current_incident_disposition(state)
                self.assertTrue(active['active_hold'])
                self.assertIsNone(active['release'])
                self.assertEqual(proofs,[])

    def test_large_metadata_context_cannot_starve_primary_result_or_native_receipts(self):
        from tools.quant_research_loop import desktop_capture as D
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'job'
            (folder/'work').mkdir(parents=True)
            (folder/'worker_call_0').mkdir()
            support=root/'tools/quant_research_loop/generic_entry.py'
            support.parent.mkdir(parents=True);support.write_text('# entry',encoding='utf-8')
            (folder/'job.json').write_text(json.dumps({'job_id':'job','inputs':[]}),encoding='utf-8')
            required={'work/analysis.py':'# source','work/result.json':'{"saved_result":true}',
                      'verification_0.json':'{"verified":true}',
                      'failure_0.json':'{"preserved_failed_attempt":true}',
                      'worker_call_0/request.json':'{"argv":[]}',
                      'worker_call_0/receipt.json':'{"exit_code":0}',
                      'worker_call_0/native_policy_and_commands.json':'{"policy":"workspace-write"}'}
            for name,content in required.items():
                (folder/name).write_text(content,encoding='utf-8')
            revisions=root/'research/campaigns/PAYOUT_V1/revisions'
            revisions.mkdir(parents=True)

            def pin(name,content):
                path=revisions/name;path.write_text(content,encoding='utf-8')
                return {'path':path.relative_to(root).as_posix(),'sha256':Q.digest(path)}

            large=pin('large.txt','x'*600000)
            state={'qa_authority':{},'completed':{'job':{}},'operator_metadata_recovery':{
                'authorization':pin('auth.json','{}'),'reconciliation':pin('context.json','{}')}}
            state['recovery_episodes']={'episode':{'job_id':'job','attempts':[0,1]}}
            recovery={'reconciliation':{'review_files':[large]}}
            with patch.object(Q,'ROOT',root),patch.object(D,'recovery_context',return_value=recovery):
                bundle=Q.build_bundle(state,folder)
                for name in required:
                    self.assertFalse(bundle['files']['job/'+name]['truncated'])
                self.assertTrue(bundle['files'][large['path']]['truncated'])
                self.assertFalse(bundle['recovery_context']['truncated'])
                self.assertEqual(json.loads(bundle['recovery_context']['content'])['episodes'],state['recovery_episodes'])
                self.assertLessEqual(sum(len(value['content']) for value in bundle['files'].values())
                                     +len(bundle['recovery_context']['content']),560000)
                report=dict(self.report(),review_key=bundle['review_key'])
                with self.assertRaisesRegex(ValueError,'incomplete required evidence'):
                    Q.validate_report(report,bundle)

    def test_json_compaction_preserves_strings_numbers_null_and_fields(self):
        text = '{\n "quoted": "space \\" quote and \\\\ slash",\n "unknown": null, "number": 1.234567890123456789012345e+123, "nested": [true, "a\\nb"]\n}'
        compact = Q.compact_json_text(text)
        self.assertEqual(json.loads(text), json.loads(compact))
        self.assertIn('1.234567890123456789012345e+123', compact)
        self.assertIn('"unknown":null', compact)
        self.assertLess(len(compact), len(text))

    def test_native_skill_notice_is_not_tool_use_but_other_items_stay_blocked(self):
        def event(kind, **fields):
            return {'type':'item.completed','item':{'type':kind,**fields}}
        notice=event('error',message=Q.SKILL_CONTEXT_NOTICE)
        self.assertEqual(Q.unexpected_qa_items([notice,event('agent_message',text='done')]),[])
        self.assertEqual(Q.unexpected_qa_items([notice,event('command_execution',command='Get-Content x')]),['command_execution'])
        self.assertEqual(Q.unexpected_qa_items([event('error',message='different runtime error')]),['error'])
        self.assertEqual(Q.unexpected_qa_items([event('error',message=Q.SKILL_CONTEXT_NOTICE+' changed')]),['error'])

    def test_latest_worker_stop_explanation_is_bounded_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'job'
            for attempt in (2, 10):
                call = folder / ('worker_call_' + str(attempt))
                call.mkdir(parents=True)
                events = [{'type': 'item.completed', 'item': {'type': 'agent_message',
                           'text': ('old' if attempt == 2 else 'stopped before running')}}]
                if attempt == 2:
                    events.append({'type': 'turn.completed'})
                (call / 'stdout.jsonl').write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
            with patch.object(Q, 'ROOT', root):
                evidence = Q.worker_terminal_evidence(folder)
                self.assertIn('worker_call_10', evidence['source'])
                self.assertFalse(evidence['terminal_event_observed'])
                self.assertEqual(evidence['messages'][0]['text'], 'stopped before running')
                self.assertIn('not artifact', evidence['claim_limit'])
                stream = folder / 'worker_call_10/stdout.jsonl'
                stream.write_text('\n'.join(json.dumps({'type': 'item.completed',
                    'item': {'type': 'agent_message', 'text': 'x' * 5000}}) for _ in range(5)), encoding='utf-8')
                bounded = Q.worker_terminal_evidence(folder)
                self.assertEqual(bounded['older_messages_omitted'], 2)
                self.assertEqual(sum(len(x['text']) for x in bounded['messages']), 12000)
                self.assertTrue(all(x['truncated'] for x in bounded['messages']))
                self.assertNotEqual(evidence['sha256'], bounded['sha256'])

    def test_primary_source_and_recovery_history_share_bounded_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'job'
            (folder / 'work').mkdir(parents=True)
            (folder / 'job.json').write_text(json.dumps({'job_id': 'job', 'inputs': []}), encoding='utf-8')
            source = folder / 'work/analysis.py'
            content = '# source\n' * 16000 + '# END_OF_IMPLEMENTATION\n'
            source.write_text(content, encoding='utf-8')
            tools = root / 'tools/quant_research_loop'
            tools.mkdir(parents=True)
            for name in ('qa_review.py', 'generic_entry.py', 'generic_worker.py', 'controller.py'):
                (tools / name).write_text('# support\n', encoding='utf-8')
            (root / 'tools/test_quant_qa.py').write_text('# test\n', encoding='utf-8')
            state = {'qa_authority': {}, 'completed': {},
                     'recovery_episodes': {'episode1': {'job_id': 'job', 'attempts': [0]},
                                           'unrelated': {'job_id': 'other', 'attempts': [0]}},
                     'lead_recovery_authorizations': {'job': {'decision_index': 162}}}
            with patch.object(Q, 'ROOT', root):
                bundle = Q.build_bundle(state, folder)
                primary = bundle['files']['job/work/analysis.py']
                self.assertEqual(primary['content'], content)
                self.assertFalse(primary['truncated'])
                history = json.loads(bundle['recovery_context']['content'])
                self.assertEqual(set(history['episodes']), {'episode1'})
                self.assertEqual(history['latest_authorization']['decision_index'], 162)
                size = sum(len(v['content']) for v in bundle['files'].values())
                self.assertLessEqual(size + len(bundle['recovery_context']['content']), 560000)
                state['lead_recovery_authorizations']['job']['decision_index'] = 163
                self.assertNotEqual(bundle['review_key'], Q.build_bundle(state, folder)['review_key'])
                result = json.dumps({'rows': 'x' * 150000, 'data_gate': 'BLOCKED'})
                (folder / 'work/result.json').write_text(result, encoding='utf-8')
                result_bundle = Q.build_bundle(state, folder)
                transported = result_bundle['files']['job/work/result.json']
                self.assertFalse(transported['truncated'])
                self.assertEqual(json.loads(transported['content'])['data_gate'], 'BLOCKED')
                source.write_text('#' * 600000, encoding='utf-8')
                oversized = Q.build_bundle(state, folder)
                self.assertTrue(oversized['files']['job/work/analysis.py']['truncated'])
                size = sum(len(v['content']) for v in oversized['files'].values())
                self.assertLessEqual(size + len(oversized['recovery_context']['content']), 560000)

    def report(self):
        return {'review_key': 'key', 'job_id': 'job', 'verdict': 'PASS', 'summary': 'checked',
                'reviewed_files': ['job/work/analysis.py'],
                'facets': [{'facet': f, 'status': 'PASS', 'evidence': 'source'} for f in Q.FACETS],
                'findings': [], 'limitations': ['offline only']}

    def test_large_historical_scope_is_summarized_without_starving_current_evidence(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'job'
            (folder / 'work').mkdir(parents=True)
            source = folder / 'work/analysis.py'
            source.write_text('# current implementation\n' * 3000, encoding='utf-8')
            baseline = root / 'baseline.py'
            baseline.write_text('# pinned baseline implementation\n' * 3000, encoding='utf-8')
            support = root / 'tools/quant_research_loop/generic_entry.py'
            support.parent.mkdir(parents=True)
            support.write_text('# fixed entry\n', encoding='utf-8')
            (folder / 'job.json').write_text(json.dumps({'job_id': 'job', 'inputs': [
                {'path': 'baseline.py', 'sha256': Q.digest(baseline)}]}), encoding='utf-8')
            historical = folder / 'metadata_scope_check_2.json'
            old = {'before': {'path': 'before.json', 'sha256': 'before-pin', 'entries': 5000},
                   'after': {'path': 'after.json', 'sha256': 'after-pin', 'entries': 9000},
                   'changes': {f'.git/objects/{index}': {'before': None, 'after': 'x' * 150}
                               for index in range(4000)},
                   'unexpected': [f'.git/objects/{index}' for index in range(4000)],
                   'limitations': 'Historical failed comparison, not current scope proof.'}
            historical.write_text(json.dumps(old), encoding='utf-8')
            original_bytes = historical.read_bytes()
            latest = folder / 'metadata_scope_check_10.json'
            current = {'before': {'sha256': 'current-before'}, 'after': {'sha256': 'current-after'},
                       'changes': {}, 'unexpected': [], 'limitations': 'Current comparison only.'}
            latest.write_text(json.dumps(current), encoding='utf-8')
            state = {'qa_authority': {}, 'completed': {'job': {}}}
            with patch.object(Q, 'ROOT', root):
                bundle = Q.build_bundle(state, folder)
                self.assertFalse(bundle['files']['job/work/analysis.py']['truncated'])
                self.assertFalse(bundle['files']['baseline.py']['truncated'])
                self.assertEqual(json.loads(bundle['files']['job/metadata_scope_check_10.json']['content']), current)
                self.assertFalse(bundle['files']['job/metadata_scope_check_10.json']['truncated'])
                self.assertIn('job/metadata_scope_check_10.json', bundle['required_complete_files'])
                entry = bundle['files']['job/metadata_scope_check_2.json']
                summary = json.loads(entry['content'])
                self.assertEqual(entry['sha256'], hashlib.sha256(original_bytes).hexdigest())
                self.assertEqual(entry['transport_sha256'], hashlib.sha256(entry['content'].encode()).hexdigest())
                self.assertEqual(summary['source_sha256'], entry['sha256'])
                self.assertEqual(summary['unexpected_count'], 4000)
                self.assertEqual(summary['change_count'], 4000)
                self.assertFalse(entry['full_review_possible'])
                self.assertFalse(summary['full_raw_comparison_supplied'])
                self.assertTrue(entry['truncated'])
                self.assertFalse(entry['summary_truncated'])
                self.assertIn('not a new scope PASS', summary['claim_limit'])
                self.assertNotIn('job/metadata_scope_check_2.json', bundle['required_complete_files'])
                self.assertTrue(any(item['path'] == 'job/metadata_scope_check_2.json'
                                    and 'no full-review' in item['reason'] for item in bundle['omissions']))
                self.assertEqual(historical.read_bytes(), original_bytes)
                self.assertLessEqual(sum(len(value['content']) for value in bundle['files'].values())
                                     + len(bundle['recovery_context']['content']), 560000)
                # The current comparison never receives the historical exemption.
                latest.write_text(json.dumps(dict(current, changes={'large_current_delta': 'x' * 600000})), encoding='utf-8')
                oversized = Q.build_bundle(state, folder)
                self.assertTrue(oversized['files']['job/metadata_scope_check_10.json']['truncated'])
                report = dict(self.report(), review_key=oversized['review_key'])
                with self.assertRaisesRegex(ValueError, 'incomplete required evidence'):
                    Q.validate_report(report, oversized)

    def bundle(self):
        return {'review_key': 'key', 'job_id': 'job', 'deterministically_verified': True,
                'files': {'job/work/analysis.py': {'sha256': 'code', 'truncated': False}}}

    def test_qa_does_not_replace_deterministic_verification(self):
        bundle = self.bundle()
        bundle['deterministically_verified'] = False
        with self.assertRaisesRegex(ValueError, 'PASS conflicts'):
            Q.validate_report(self.report(), bundle)

    def test_required_evidence_cannot_be_missing_or_truncated_on_pass(self):
        bundle=self.bundle()
        bundle['required_complete_files']=['job/worker_call_0/receipt.json']
        with self.assertRaisesRegex(ValueError,'incomplete required evidence'):
            Q.validate_report(self.report(),bundle)
        bundle['files']['job/worker_call_0/receipt.json']={'sha256':'receipt','truncated':True}
        with self.assertRaisesRegex(ValueError,'incomplete required evidence'):
            Q.validate_report(self.report(),bundle)
        bundle['files']['job/worker_call_0/receipt.json']['truncated']=False
        self.assertEqual(Q.validate_report(self.report(),bundle)['verdict'],'PASS')

    def test_required_facets_cannot_be_replaced_with_duplicates(self):
        report = self.report()
        report['facets'][-1] = report['facets'][0]
        with self.assertRaisesRegex(ValueError, 'facets incomplete'):
            Q.validate_report(report, self.bundle())

    def test_identity_findings_and_truncation_fail_closed(self):
        report = self.report()
        report['review_key'] = 'old'
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            Q.validate_report(report, self.bundle())
        report = self.report()
        report['findings'] = [{'severity': 'major', 'file': 'job/work/analysis.py', 'line': 1,
                               'problem': 'defect', 'evidence': 'operand', 'required_change': 'fix',
                               'acceptance_check': 'independent regression'}]
        with self.assertRaisesRegex(ValueError, 'PASS conflicts'):
            Q.validate_report(report, self.bundle())
        bundle = self.bundle()
        bundle['files']['job/work/analysis.py']['truncated'] = True
        with self.assertRaisesRegex(ValueError, 'PASS conflicts'):
            Q.validate_report(self.report(), bundle)

    def test_readiness_and_dependent_replay_blocked_but_repair_allowed(self):
        qa = {'verdict': 'CHANGES_REQUIRED', 'job_id': 'job',
              'source_hashes': {'job/work/analysis.py': 'hash'}}
        with self.assertRaisesRegex(ValueError, 'forward promotion'):
            Q.validate_decision({'action': 'READY_FOR_FORWARD'}, qa)
        job = {'job_class': 'EXPERIMENT', 'inputs': [{'path': 'job/work/analysis.py'}]}
        with self.assertRaisesRegex(ValueError, 'before using'):
            Q.validate_decision({'next_job': job}, qa)
        job['job_class'] = 'DIAGNOSTIC'
        Q.validate_decision({'next_job': job}, qa)

    def test_authority_is_hash_pinned_and_keeps_existing_global_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'research/campaigns/PAYOUT_V1/revisions/qa/authorization.json'
            path.parent.mkdir(parents=True)
            data = {'revision_id': Q.REVISION, 'model': Q.MODEL, 'reasoning': 'max',
                    'provider': 'codex', 'login': 'chatgpt', 'live': 'DISABLED', 'spend_usd': 0,
                    'global_call_cap': 1000, 'tools_allowed': False}
            path.write_text(json.dumps(data), encoding='utf-8')
            state = {'qa_authority': {'path': path.relative_to(root).as_posix(), 'sha256': Q.digest(path)}}
            with patch.object(Q, 'ROOT', root):
                self.assertEqual(Q.authority(state)['model'], 'gpt-6-sol')
                self.assertEqual(Q.cycle_reserve(state), 3)
                self.assertEqual(Q.cycle_reserve({}), 2)
                path.write_text('{}', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                    Q.authority(state)

    def test_new_route_overlays_model_without_rewriting_legacy_qa_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'research/campaigns/PAYOUT_V1/revisions/qa/authorization.json'
            path.parent.mkdir(parents=True)
            data = {'revision_id': Q.REVISION, 'model': Q.MODEL, 'reasoning': 'max',
                    'provider': 'codex', 'login': 'chatgpt', 'live': 'DISABLED', 'spend_usd': 0,
                    'global_call_cap': 1000, 'tools_allowed': False}
            original = json.dumps(data)
            path.write_text(original, encoding='utf-8')
            route_ref = {'path': 'revisions/r19/authorization.json', 'sha256': 'route-hash'}
            state = {'qa_authority': {'path': path.relative_to(root).as_posix(), 'sha256': Q.digest(path)},
                     'model_route_authority': route_ref}
            route = {'revision_id': 'R19', 'routes': {'qa': {'model': 'gpt-6.1-sol', 'reasoning': 'high'}}}
            with patch.object(Q, 'ROOT', root), \
                    patch('tools.quant_research_loop.model_routing.authority', return_value=route) as gate:
                effective = Q.authority(state)
                gate.assert_called_once_with(state)
                self.assertEqual((effective['model'], effective['reasoning']), ('gpt-6.1-sol', 'high'))
                self.assertEqual(effective['model_route_authority'], route_ref)
                self.assertFalse(effective['tools_allowed'])
                self.assertEqual(effective['global_call_cap'], 1000)
                self.assertEqual(path.read_text(encoding='utf-8'), original)
                # A route revision never legitimizes a changed R11 permission record.
                data['model'] = 'gpt-6.1-sol'
                path.write_text(json.dumps(data), encoding='utf-8')
                state['qa_authority']['sha256'] = Q.digest(path)
                with self.assertRaisesRegex(ValueError, 'QA route or permission mismatch'):
                    Q.authority(state)

    def test_route_and_authorization_identity_separate_qa_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'job'
            folder.mkdir()
            (folder / 'job.json').write_text(json.dumps({'job_id': 'job', 'inputs': []}), encoding='utf-8')
            support = root / 'tools/quant_research_loop/generic_entry.py'
            support.parent.mkdir(parents=True)
            support.write_text('# support\n', encoding='utf-8')
            state = {'qa_authority': {'path': 'legacy.json', 'sha256': 'legacy'}, 'completed': {}}
            routes = [
                {'model': Q.MODEL, 'reasoning': 'max'},
                {'model': 'gpt-6.1-sol', 'reasoning': 'high',
                 'model_route_authority': {'path': 'r19.json', 'sha256': 'new-authority'}},
                {'model': 'gpt-6.1-sol', 'reasoning': 'high',
                 'model_route_authority': {'path': 'r19.json', 'sha256': 'changed-authority'}},
            ]
            with patch.object(Q, 'ROOT', root), patch.object(Q, 'authority', side_effect=routes):
                bundles = [Q.build_bundle(state, folder) for _ in routes]
            self.assertEqual(len({bundle['review_key'] for bundle in bundles}), 3)
            self.assertEqual(bundles[1]['qa_route']['model'], 'gpt-6.1-sol')
            self.assertEqual(bundles[1]['qa_route']['reasoning'], 'high')

    def test_new_qa_call_uses_effective_route_in_command_prompt_and_receipt(self):
        report = self.report()
        bundle = self.bundle()
        route = {'model': 'gpt-6.1-sol', 'reasoning': 'high',
                 'model_route_authority': {'path': 'r19.json', 'sha256': 'route-hash'}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            class Controller:
                state = {'qa_authority': {'path': 'legacy.json', 'sha256': 'legacy'}}
                profile = {'max_calls': 1000}
                def model_call(self, cmd, prompt, call, **kwargs):
                    calls.append((cmd, prompt))
                    response = Path(cmd[cmd.index('-o') + 1])
                    response.write_text(json.dumps(report), encoding='utf-8')
                    raw = '\n'.join(json.dumps(e) for e in [
                        {'type': 'thread.started', 'thread_id': 'offline-fixture'},
                        {'type': 'turn.completed'}])
                    return {'exit_code': 0}, raw
                def save(self):
                    pass
            controller = Controller()
            with patch.object(Q, 'ROOT', root), patch.object(Q, 'authority', return_value=route), \
                    patch.object(Q, 'target', return_value=root), \
                    patch.object(Q, 'build_bundle', return_value=bundle), \
                    patch.object(Q, 'native_command', return_value=['codex']):
                result = Q.review(controller)
                # Same-route reuse must be free of any second provider request.
                self.assertEqual(Q.review(controller)['path'], result['path'])
            self.assertEqual(len(calls), 1)
            cmd, prompt = calls[0]
            self.assertEqual(cmd[cmd.index('-m') + 1], 'gpt-6.1-sol')
            self.assertIn('model_reasoning_effort="high"', cmd)
            self.assertIn('gpt-6.1-sol with high reasoning', prompt)
            self.assertNotIn('Astra owns', prompt)
            self.assertNotIn('Luna implements', prompt)
            self.assertIn('the Executor chooses implementation', prompt)
            self.assertIn('Blocking findings must affect correctness', prompt)
            self.assertIn('Label optional improvements as optional', prompt)
            self.assertIn('without code or patch recipes', prompt)
            self.assertIn('decision-changing need', prompt)
            self.assertEqual(result['requested_model'], 'gpt-6.1-sol')
            self.assertEqual(result['requested_reasoning'], 'high')
            self.assertEqual(result['model_route_authority'], route['model_route_authority'])
            self.assertEqual(result['verdict'], 'PASS')

    def test_completed_qa_provider_failure_gets_one_new_cache_generation_not_model_findings(self):
        from types import SimpleNamespace
        from tools.quant_research_loop import autonomous_recovery as A
        from tools.quant_research_loop import controller as C
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / 'research/campaigns/PAYOUT_V1/runtime'
            folder = runtime / 'jobs/job'
            (folder / 'work').mkdir(parents=True)
            (folder / 'job.json').write_text(json.dumps({'job_id': 'job', 'inputs': []}), encoding='utf-8')
            source = folder / 'work/analysis.py'
            source.write_text('# incomplete implementation\n', encoding='utf-8')
            support = root / 'tools/quant_research_loop/generic_entry.py'
            support.parent.mkdir(parents=True)
            support.write_text('# fixed entry fixture\n', encoding='utf-8')
            auth_path = root / A.AUTHORIZATION
            auth_path.parent.mkdir(parents=True)
            source_auth = auth_path.parent / 'USER_AUTHORIZATION.txt'
            source_auth.write_text('Autonomous bounded technical recovery.', encoding='utf-8')
            auth_path.write_text(json.dumps(dict(A.EXPECTED, source_sha256=Q.digest(source_auth))), encoding='utf-8')
            state = {'status': 'RUNNING', 'model_calls': 0, 'decisions': 0,
                     'qa_authority': {'path': 'legacy.json', 'sha256': 'legacy'}, 'completed': {},
                     'pending_job': {'job_id': 'job'},
                     'autonomous_recovery_authority': {'path': A.AUTHORIZATION.as_posix(),
                        'revision_id': A.REVISION, 'sha256': Q.digest(auth_path)}}
            calls = []
            def model_call(cmd, prompt, call, **kwargs):
                calls.append(call)
                call.mkdir(parents=True)
                state['model_calls'] += 1
                receipt = {'exit_code': 1 if len(calls) == 1 else 0, 'process_reaped': True}
                events = [{'type': 'thread.started', 'thread_id': 'offline-fixture'}]
                if len(calls) == 1:
                    events.append({'type': 'turn.failed', 'error': {'message': 'invalid_json_schema'}})
                else:
                    bundle = Q.read(call.parent / 'evidence.json')
                    report = dict(self.report(), review_key=bundle['review_key'], verdict='INCOMPLETE',
                        reviewed_files=[source.relative_to(root).as_posix()],
                        facets=[{'facet': facet, 'status': 'UNKNOWN', 'evidence': 'Missing implementation'}
                                for facet in Q.FACETS], limitations=['Actual model finding; repair required.'])
                    Path(cmd[cmd.index('-o') + 1]).write_text(json.dumps(report), encoding='utf-8')
                    events.append({'type': 'turn.completed'})
                raw = '\n'.join(json.dumps(event) for event in events)
                (call / 'request.json').write_text(json.dumps({'argv': cmd}), encoding='utf-8')
                (call / 'receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
                (call / 'stdout.jsonl').write_text(raw, encoding='utf-8')
                (call / 'stderr.txt').write_text('', encoding='utf-8')
                return receipt, raw
            controller = SimpleNamespace(state=state, profile={'max_calls': 1000},
                                         save=lambda: None, model_call=model_call)
            route = {'model': 'gpt-6.1-sol', 'reasoning': 'high'}
            with patch.object(Q, 'ROOT', root), patch.object(A, 'ROOT', root), \
                    patch.object(A, 'RUNTIME', runtime), patch.object(Q, 'authority', return_value=route), \
                    patch.object(Q, 'target', return_value=folder), \
                    patch.object(Q, 'native_command', return_value=['codex']):
                failed = Q.review(controller)
                original_bytes = (root / failed['path']).read_bytes()
                self.assertEqual(Q.review(controller)['review_key'], failed['review_key'])
                self.assertEqual(len(calls), 1)
                lead = C.Controller.__new__(C.Controller)
                lead.state, lead.profile = state, controller.profile
                lead.model_call = lambda *args, **kwargs: self.fail('No Lead call before a valid QA verdict')
                with patch.object(C, 'RUNTIME', runtime), \
                        self.assertRaisesRegex(RuntimeError, 'QA provider/session/output incomplete'):
                    lead.lead()
                self.assertEqual(len(calls), 1)
                state.update(status='ENGINEERING_BLOCKED',
                             error='QA provider/session/output incomplete; inspect preserved call receipt')
                self.assertEqual(A.recover(controller)['action'], 'RESUME')
                retry = Q.build_bundle(state, folder)
                self.assertNotEqual(retry['review_key'], failed['review_key'])
                self.assertEqual(retry['qa_retry_context']['failed_review_key'], failed['review_key'])
                reviewed = Q.review(controller)
                self.assertEqual(len(calls), 2)
                self.assertEqual(reviewed['verdict'], 'INCOMPLETE')
                state.update(status='ENGINEERING_BLOCKED',
                             error='QA provider/session/output incomplete; inspect preserved call receipt')
                self.assertEqual(A.recover(controller)['action'], 'STOP')
                self.assertEqual(Q.review(controller)['review_key'], reviewed['review_key'])
                self.assertEqual(len(calls), 2)
                self.assertEqual(state['model_calls'], 2)
                self.assertEqual((root / failed['path']).read_bytes(), original_bytes)

    def test_historical_fallback_keeps_original_model_after_route_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'report.json'
            path.write_text(json.dumps(self.report()), encoding='utf-8')
            previous = {'path': 'report.json', 'sha256': Q.digest(path), 'verdict': 'PASS',
                        'requested_model': Q.MODEL, 'requested_reasoning': 'max'}
            class Controller:
                state = {'latest_qa_review': previous}
                def model_call(self, *args, **kwargs):
                    raise AssertionError('historical evidence must not trigger a model call')
            route = {'model': 'gpt-6.1-sol', 'reasoning': 'high'}
            with patch.object(Q, 'ROOT', root), patch.object(Q, 'authority', return_value=route), \
                    patch.object(Q, 'target', return_value=None):
                result = Q.review(Controller())
            self.assertTrue(result['historical_for_pending_plan'])
            self.assertEqual((result['requested_model'], result['requested_reasoning']), (Q.MODEL, 'max'))

    def test_resume_reuses_review_without_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'research/campaigns/PAYOUT_V1/runtime/qa/key/report.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.report()), encoding='utf-8')
            ref = {'path': path.relative_to(root).as_posix(), 'sha256': Q.digest(path), 'verdict': 'PASS',
                   'review_key': 'key', 'job_id': self.report()['job_id'],
                   'source_hashes': {k: v['sha256'] for k, v in self.bundle()['files'].items()}}
            class Controller:
                state = {'qa_reviews': {'key': ref}}
                def model_call(self, *args, **kwargs):
                    raise AssertionError('duplicate call')
            with patch.object(Q, 'ROOT', root), patch.object(Q, 'authority', return_value=True), \
                    patch.object(Q, 'target', return_value=root), \
                    patch.object(Q, 'build_bundle', return_value=self.bundle()):
                result = Q.review(Controller())
                self.assertEqual(result['report']['verdict'], 'PASS')
                path.write_text('{}', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'review changed'):
                    Q.review(Controller())

    def test_qa_preserves_one_call_for_lead(self):
        class Controller:
            state = {'model_calls': 999}
            profile = {'max_calls': 1000}
            def save(self):
                pass
        controller = Controller()
        with patch.object(Q, 'authority', return_value=True), patch.object(Q, 'target', return_value=Path('.')), \
                patch.object(Q, 'build_bundle', return_value=self.bundle()):
            with self.assertRaisesRegex(RuntimeError, 'QA plus mandatory Lead'):
                Q.review(controller)
            self.assertEqual(controller.state['status'], 'RESOURCE_LIMIT')
            self.assertEqual(controller.state['model_calls'], 999)

    def test_undispatched_plan_does_not_review_unrelated_old_completed_job(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(Q, 'ROOT', Path(tmp)):
            state = {'pending_job': {'job_id': 'new_repair', 'job_class': 'DIAGNOSTIC'},
                     'evidence_id': 'old_other_family', 'completed': {'old_other_family': {}}}
            self.assertIsNone(Q.target(state))

    def test_active_hold_finishes_completed_probe_qa_before_successor(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(Q,'ROOT',Path(tmp)):
            state={'operator_safety_hold':{'active':True},
                   'operator_safety_recovery':{'job_id':'probe'},
                   'pending_job':{'job_id':'successor','job_class':'DIAGNOSTIC'},
                   'completed':{'probe':{}},'evidence_id':'probe'}
            self.assertEqual(Q.target(state),Path(tmp)/'research/campaigns/PAYOUT_V1/runtime/jobs/probe')
            state['operator_safety_hold']['active']=False
            self.assertIsNone(Q.target(state))

    def test_historical_pass_cannot_promote_undispatched_plan(self):
        with self.assertRaisesRegex(ValueError, 'Historical QA'):
            Q.validate_decision({'action': 'READY_FOR_FORWARD'},
                                {'verdict': 'PASS', 'historical_for_pending_plan': True})

    def test_model_call_reserves_full_worker_review_cycle_before_counting(self):
        from tools.quant_research_loop import controller as C
        controller = C.Controller.__new__(C.Controller)
        controller.state = {'model_calls': 998}
        controller.profile = {'max_calls': 1000}
        controller.check_budget = lambda: None
        controller.save = lambda: None
        with tempfile.TemporaryDirectory() as tmp, patch.object(C, 'RUNTIME', Path(tmp)), patch.object(Q, 'cycle_reserve', return_value=3):
            with self.assertRaisesRegex(RuntimeError, 'cycle cannot be reserved'):
                controller.model_call([], '', Path('worker_call_0'))
        self.assertEqual(controller.state['model_calls'], 998)
        self.assertEqual(controller.state['status'], 'RESOURCE_LIMIT')


if __name__ == '__main__':
    unittest.main()

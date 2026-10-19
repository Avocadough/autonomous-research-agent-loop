import copy
import unittest
import json
import tempfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import Mock, patch
from tools.quant_research_loop import qa_transport as T


class CompleteViewTests(unittest.TestCase):
    def full(self):
        # Includes long-precision floats, exponent spelling, signed zero, null,
        # duplicate reference indices and a source pool item not used in a trace.
        result=T.loads('''{"job_id":"J1","payload":{
          "records":[{"execution_refs":["a","a"],"observed":1.230000000000000001e-3}],
          "root_cause_witnesses":[{"original_ref":"a|original","corrected_ref":"a","source_ref":"a"}],
          "executions":{"a":{"trace":[0,0]},"a|original":{"trace":[0]}},
          "sources":{"a":[0,0]},"source_row_pool":[{"x":-0.0},{"x":2}],
          "event_pool":[{"x":null,"text":"a b\\nquote\\\"","n":1e+02}],
          "synthetic_trades":[],"unresolved_exposure":[],"synthetic_financial_checks":[],
          "fixture_corrections":[],"market_rows_read":0}}''')
        files={'jobs/J1/job.json':T.file_content('job','{"job_id":"J1"}'),
               'jobs/J1/work/analysis.py':T.file_content('analysis','# source\n'),
               'jobs/J1/work/result.json':T.file_content('result',T.emit(result)),
               'receipt.json':T.file_content('receipt','{"exit_code":0}')}
        return {'job_id':'J1','files':files,'required_complete_files':list(files),
                'recovery_context':{'content':'{"spent":1}','sha256':'recovery','truncated':False},
                'deterministically_verified':True,'execution_trace':[{'command':'fixed entry once','exit_code':0}]}

    def test_lossless_complete_views_preserve_pool_order_multiplicity_and_numbers(self):
        full=self.full();views,coverage=T.build_views(full)
        self.assertTrue(coverage['complete'])
        self.assertEqual(coverage['lexical_value_sha256'],coverage['reconstructed_sha256'])
        self.assertEqual(coverage['section_counts']['source_row_pool'],2)
        text=''.join(v['files'].get('jobs/J1/work/result.json',{}).get('content','') for v in views)
        self.assertIn('1.230000000000000001e-3',text)
        self.assertIn('-0.0',text)
        self.assertIn('1e+02',text)
        self.assertIn('"trace":[0,0]',text)
        self.assertIn('"x":null',text)
        for view in views:self.assertLessEqual(T.size(view['files']),T.MAX_CONTENT)

    def test_finite_comparators_are_complete_without_reverse_linking_shared_positive(self):
        full=self.full();name='jobs/J1/work/result.json'
        result=T.loads(full['files'][name]['content']);p=result['payload']
        for key in ('baseline','recovery','positive','other','other_baseline','other_recovery'):
            p['executions'][key]={'trace':[T.Number('0')]}
            p['sources'][key]=[T.Number('0')]
        p['finite_data_checks']=[
            {'execution_ref':'a','baseline_preprocessed_ref':'baseline',
             'quota_recovery_ref':'recovery','positive_ref':'positive'},
            {'execution_ref':'other','baseline_preprocessed_ref':'other_baseline',
             'quota_recovery_ref':'other_recovery','positive_ref':'positive'}]
        full['files'][name]['content']=T.emit(result)
        selected=T.closure(p,executions=['a'])
        self.assertEqual(selected['finite_data_checks'],{'0'})
        self.assertEqual(selected['executions'],{'a','a|original','baseline','recovery','positive'})
        self.assertNotIn('other',selected['executions'])
        views,_=T.build_views(full)
        # Keep complete union coverage elsewhere: a partially paired duplicate
        # must still be rejected rather than hiding behind the union checksum.
        selected={k:list(v) for k,v in selected.items()}
        for missing in ('baseline','recovery','positive'):
            broken=copy.deepcopy(selected);broken['executions'].remove(missing)
            broken['sources'].remove(missing)
            partial={'files':{name:T.file_content('result',T.selected_content(result,broken))},
                     'scope':'paired regression','selection':broken,'static':False}
            with self.subTest(missing=missing),self.assertRaisesRegex(ValueError,'Dangling per-execution check'):
                T.validate_coverage(full,views+[partial])
        for field in ('baseline_preprocessed_ref','quota_recovery_ref','positive_ref'):
            bad=copy.deepcopy(full);obj=T.loads(bad['files'][name]['content'])
            obj['payload']['finite_data_checks'][0][field]='absent'
            bad['files'][name]['content']=T.emit(obj)
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'Dangling NR7 named reference'):
                T.build_views(bad)

    def test_v3_finite_money_and_prior_result_are_complete_bounded_views(self):
        full=self.full();name='jobs/J1/work/result.json'
        result=T.loads(full['files'][name]['content']);p=result['payload']
        p['finite_data_checks']=[{'execution_ref':'a','value':T.Number('1e+02')}] * 12
        p['synthetic_trades']=[{'execution_ref':'a','net':T.Number('-0.0')}] * 12
        p['control_coverage']={'valid_v2_ledger_regressions':[{'execution_ref':'a','net':None}] * 12}
        # The four-way noon pair must remain with both original executions.
        p['root_cause_witnesses']=[{'present_ref':'a','missing_ref':'b',
            'original_present_ref':'a|original','original_missing_ref':'b|original'}]
        p['sources']['b']=p['sources']['a']
        for key in ('b','a|original','b|original'):p['executions'][key]=copy.deepcopy(p['executions']['a'])
        full['files'][name]['content']=T.emit(result)
        prior=self.full()['files'][name]
        full['files']['jobs/J0/work/result.json']=prior
        full['files']['jobs/J0/job.json']=T.file_content('j0','{"job_id":"J0"}')
        full['files']['jobs/J0/work/analysis.py']=T.file_content('a0','# old source')
        full['required_complete_files']=list(full['files'])
        views,coverage=T.build_views(full)
        self.assertEqual(coverage['section_counts']['finite_data_checks'],12)
        self.assertEqual(coverage['section_counts']['control_coverage.valid_v2_ledger_regressions'],12)
        self.assertEqual(coverage['lexical_value_sha256'],coverage['reconstructed_sha256'])
        self.assertIn('jobs/J0/work/result.json',coverage['supporting_results'])
        relevant=[v for v in views if (v.get('selection') or {}).get('root_cause_witnesses') and not v.get('supporting_result')]
        self.assertTrue(relevant)
        for v in relevant:self.assertTrue({'a','b','a|original','b|original'}<=set(v['selection']['executions']))
        for mutation in ('finite_lexeme','noon_reference','missing_prior'):
            changed=copy.deepcopy(views)
            if mutation=='missing_prior':changed=[v for v in changed if not v.get('supporting_result')]
            else:
                view=next(v for v in changed if (v.get('selection') or {}).get('finite_data_checks') and not v.get('supporting_result'))
                item=view['files'][name];obj=T.loads(item['content'])
                if mutation=='finite_lexeme':obj['indexed_payload_sections']['finite_data_checks']['0']['value']=T.Number('100')
                else:obj['indexed_payload_sections']['root_cause_witnesses']['0']['original_missing_ref']='unknown'
                item['content']=T.emit(obj)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):T.validate_coverage(full,changed)

    def test_missing_view_and_context_and_receipt_fail_closed(self):
        full=self.full();views,_=T.build_views(full)
        for mutate in ('view','receipt','context','recovery'):
            changed=copy.deepcopy(views)
            if mutate=='view':changed.pop(0)
            else:
                path={'receipt':'receipt.json','context':'controller_state/qa_context.json',
                      'recovery':'controller_state/recovery_context.json'}[mutate]
                for v in changed:v['files'].pop(path,None)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):T.validate_coverage(full,changed)

    def test_historical_views_include_actual_current_source_in_their_size_budget(self):
        full=self.full();current='jobs/J1/work/analysis.py'
        full['files'][current]['content']='# current implementation\n' * 100
        for suffix in ('job.json','work/analysis.py','work/result.json'):
            item=copy.deepcopy(full['files']['jobs/J1/'+suffix])
            if suffix=='job.json':item['content']='{"job_id":"J0"}'
            full['files']['jobs/J0/'+suffix]=item
        full['required_complete_files']=list(full['files'])
        views,coverage=T.build_views(full)
        historical=[v for v in views if v.get('supporting_result') and
                    'jobs/J0/work/result.json' in v['files']]
        self.assertTrue(historical)
        for v in historical:
            self.assertEqual(v['files'].get(current),full['files'][current])
            self.assertLessEqual(T.size(v['files']),T.MAX_CONTENT)
        broken=copy.deepcopy(views)
        next(v for v in broken if v.get('supporting_result') and
             'jobs/J0/work/result.json' in v['files'])['files'].pop(current)
        with self.assertRaisesRegex(ValueError,'Historical comparison lacks current source'):
            T.validate_coverage(full,broken)

    def test_identical_view_reuses_exact_prior_bundle_but_changed_view_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(T,'ROOT',root):
                state,folder=self.fixture_plan(root)
                prior=json.loads(T.checked(state['qa_transport_plan']).read_text())
                full=json.loads(T.checked(prior['full_collection']).read_text())
                view=json.loads(T.checked(prior['views'][0]).read_text())
                old=T.view_bundle(prior,full,view,'view_000')
                new=dict(prior,plan_key='new-plan',reuse_plan=state['qa_transport_plan'])
                self.assertEqual(T.view_bundle(new,full,view,'view_000'),old)
                changed=copy.deepcopy(view);changed['scope']+=' Additional required check.'
                self.assertNotEqual(T.view_bundle(new,full,changed,'view_000')['review_key'],old['review_key'])
                changed_full=copy.deepcopy(full);changed_full['skill_guidance']={'changed':True}
                self.assertNotEqual(T.view_bundle(new,changed_full,view,'view_000')['review_key'],old['review_key'])
                self.assertNotEqual(T.view_bundle(new,full,view,'maintenance',maintenance=True)['review_key'],old['review_key'])
                new['reuse_plan']=dict(state['qa_transport_plan'],sha256='wrong')
                with self.assertRaises(ValueError):T.view_bundle(new,full,view,'view_000')

    def test_v4_prior_v3_ledger_checks_keep_exact_identity_and_complete_references(self):
        full=self.full();name='jobs/J1/work/result.json'
        result=T.loads(full['files'][name]['content']);p=result['payload']
        p['finite_data_checks']=[]
        p['control_coverage']={'valid_v3_ledger_regressions':[
            {'execution_ref':'a','net':T.Number('-0.0'),'value':None},
            {'execution_ref':'a','net':T.Number('1e+02'),'value':None}]}
        full['files'][name]['content']=T.emit(result)
        views,coverage=T.build_views(full)
        section='control_coverage.valid_v3_ledger_regressions'
        self.assertEqual(coverage['section_counts'][section],2)
        self.assertEqual(coverage['lexical_value_sha256'],coverage['reconstructed_sha256'])
        for mutation in ('missing_check','numeric_lexeme','dangling_reference'):
            changed=copy.deepcopy(views)
            view=next(v for v in changed if (v.get('selection') or {}).get(section))
            item=view['files'][name];obj=T.loads(item['content'])
            checks=obj['indexed_payload_sections'][section]
            if mutation=='missing_check':
                del checks['0'];view['selection'][section].remove('0')
            elif mutation=='numeric_lexeme':checks['1']['net']=T.Number('100')
            else:checks['1']['execution_ref']='absent'
            item['content']=T.emit(obj)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                T.validate_coverage(full,changed)

    def test_lexical_value_hash_truncation_and_selection_tampering_block(self):
        full=self.full();views,_=T.build_views(full)
        for mutate in ('lexeme','hash','truncated','selection'):
            changed=copy.deepcopy(views)
            v=next(v for v in changed if v['selection'] and v['selection']['records'])
            item=v['files']['jobs/J1/work/result.json']
            if mutate=='lexeme':item['content']=item['content'].replace('1e+02','100.0')
            elif mutate=='hash':item['sha256']='wrong'
            elif mutate=='truncated':item['truncated']=True
            else:v['selection']['records']=[]
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):T.validate_coverage(full,changed)

    def test_dangling_references_duplicate_keys_and_nan_fail_before_review(self):
        for mutation in ('pool','source','named'):
            full=self.full();path='jobs/J1/work/result.json';v=T.loads(full['files'][path]['content'])
            if mutation=='pool':v['payload']['executions']['a']['trace']=[T.Number('9')]
            elif mutation=='source':v['payload']['sources']={}
            else:v['payload']['records'][0]['execution_refs']=['unknown']
            full['files'][path]['content']=T.emit(v)
            with self.subTest(mutation=mutation),self.assertRaisesRegex(ValueError,'Dangling'):T.build_views(full)
        for value in ('{"a":1,"a":2}','{"a":NaN}'):
            with self.assertRaises(ValueError):T.loads(value)

    def test_oversized_complete_support_never_becomes_an_excerpt(self):
        full=self.full();full['files']['receipt.json']['content']='x'*(T.MAX_CONTENT+1)
        with self.assertRaisesRegex(ValueError,'cannot fit'):T.build_views(full)

    def fixture_plan(self,root):
        full=self.full()
        for name,item in full['files'].items():
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(item['content'],encoding='utf8')
            item['sha256']=T.digest(p)
        full['qa_route']={'model':'gpt-6.1-sol','reasoning':'high'}
        text=json.dumps({'episodes':{},'latest_authorization':None},ensure_ascii=False,sort_keys=True)
        full['recovery_context']={'content':text,'sha256':T.text_hash(text),'truncated':False}
        base=root/'research/campaigns/PAYOUT_V1/revisions/fixture';base.mkdir(parents=True)
        def saved(name,value):
            p=base/name;p.write_text(json.dumps(value),encoding='utf8');return T.pin(p)
        stdout=saved('stdout.txt','test output');stderr=saved('stderr.txt','OK')
        source_pin=T.pin(root/'jobs/J1/work/analysis.py')
        validation=saved('verification.json',{'exit_code':0,'preserved':True,'stdout':stdout,'stderr':stderr,
                                             'source_before':[source_pin],'source_after':[source_pin]})
        reconciliation=saved('reconciliation.json',{'verification':validation})
        decision=saved('accepted.json',{'action':'PAUSE','next_job':None,'worker_dispatch_ready':False})
        state={'evidence_id':'J1','qa_authority':{},'operator_metadata_recovery':{'reconciliation':reconciliation},
               'completed':{'J1':{'sha256':full['files']['jobs/J1/work/result.json']['sha256']}},'qa_reviews':{}}
        views,coverage=T.build_views(full)
        m={stdout['path']:T.file_content(stdout['sha256'],(root/stdout['path']).read_text())}
        state['qa_transport_plan']=T.save_plan(state,full,views,coverage,m,base/'plan',decision)
        return state,root/'J1'

    def test_saved_plan_checks_all_input_and_view_hashes_before_any_call(self):
        from tools.quant_research_loop import qa_review as Q,safety_hold as H
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);stack.enter_context(patch.object(T,'ROOT',root))
            stack.enter_context(patch.object(H,'delegated_authority',return_value={'approved':True}))
            stack.enter_context(patch.object(Q,'authority',return_value={'model':'gpt-6.1-sol','reasoning':'high'}))
            state,folder=self.fixture_plan(root)
            self.assertIsNotNone(T.load_plan(state,folder))
            ctl=Mock();ctl.state=state
            with patch.object(Q,'review_bundle') as call:
                (root/'receipt.json').write_text('{"exit_code":7}')
                with self.assertRaisesRegex(ValueError,'pin missing or changed'):T.review_plan(ctl,folder)
                call.assert_not_called()

    def test_source_review_context_is_complete_bounded_and_identity_stable(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(T,'ROOT',Path(tmp)):
            root=Path(tmp);p=root/'accepted.json';p.write_text('{"verdict":"PASS"}',encoding='utf8')
            ref=T.pin(p);bundle={'review_key':'view','files':{},'required_complete_files':[]}
            first=T.with_source_review(bundle,ref)
            self.assertEqual(first,T.with_source_review(bundle,ref))
            self.assertNotEqual(first['review_key'],'view')
            self.assertEqual(first['files'][ref['path']]['content'],p.read_text())
            self.assertEqual(bundle['files'],{})
            p.write_text('x'*(T.REVIEW_CONTEXT_RESERVE+1),encoding='utf8')
            with self.assertRaisesRegex(RuntimeError,'complete source review'):
                T.with_source_review(bundle,T.pin(p))

    def test_maintenance_text_must_match_its_actual_file_not_just_a_claimed_hash(self):
        from tools.quant_research_loop import qa_review as Q,safety_hold as H
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);stack.enter_context(patch.object(T,'ROOT',root))
            stack.enter_context(patch.object(H,'delegated_authority',return_value={'approved':True}))
            stack.enter_context(patch.object(Q,'authority',return_value={'model':'gpt-6.1-sol','reasoning':'high'}))
            state,folder=self.fixture_plan(root)
            p=root/state['qa_transport_plan']['path'];plan=json.loads(p.read_text())
            m=root/plan['maintenance']['path'];value=json.loads(m.read_text())
            next(iter(value['files'].values()))['content']='different text with unchanged claimed file hash'
            m.write_text(json.dumps(value),encoding='utf8');plan['maintenance']=T.pin(m)
            p.write_text(json.dumps(plan),encoding='utf8');state['qa_transport_plan']=T.pin(p)
            with self.assertRaisesRegex(ValueError,'maintenance content'):
                T.load_plan(state,folder)

    def test_maintenance_failure_blocks_job_views_and_success_requires_final_independent_review(self):
        from tools.quant_research_loop import qa_review as Q,safety_hold as H,skill_engineering as S,runtime_policy as P
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);stack.enter_context(patch.object(T,'ROOT',root))
            stack.enter_context(patch.object(H,'delegated_authority',return_value={'approved':True}))
            stack.enter_context(patch.object(Q,'authority',return_value={'model':'gpt-6.1-sol','reasoning':'high'}))
            stack.enter_context(patch.object(P,'remaining_call_units',return_value=100))
            skill=stack.enter_context(patch.object(S,'maybe_review'))
            state,folder=self.fixture_plan(root);ctl=Mock();ctl.state=state;ctl.profile={}
            calls=[];verdict='INCOMPLETE'
            def review(controller,bundle,**kwargs):
                calls.append(bundle)
                p=root/(bundle['review_key']+'.json');p.write_text(json.dumps({'verdict':verdict}),encoding='utf8')
                return dict(T.pin(p),review_key=bundle['review_key'],job_id='J1',verdict=verdict,
                            source_hashes={k:v['sha256'] for k,v in bundle['files'].items()},
                            report={'verdict':verdict},maintenance_review=bundle['transport_maintenance'],
                            partial_review=not bundle['transport_final'])
            stack.enter_context(patch.object(Q,'review_bundle',side_effect=review))
            failed=T.review_plan(ctl,folder)
            self.assertEqual(len(calls),1);self.assertTrue(failed['maintenance_review'])
            self.assertIn('jobs/J1/work/result.json',failed['blocked_source_hashes'])
            calls.clear();verdict='PASS'
            result=T.review_plan(ctl,folder)
            self.assertTrue(calls[0]['transport_maintenance'])
            self.assertTrue(calls[-1]['transport_final'])
            self.assertFalse(result['partial_review'])
            self.assertTrue(result['transport_coverage']['complete'])
            self.assertEqual(len(result['transport_view_reviews']),len(calls)-1)
            self.assertEqual(state['latest_qa_review']['review_key'],result['review_key'])
            self.assertGreaterEqual(skill.call_count,len(calls))

    def test_real_final_acceptance_checks_source_view_and_report_after_call_before_publication(self):
        from tools.quant_research_loop import qa_review as Q,safety_hold as H,skill_engineering as S,runtime_policy as P
        for mutation in ('source','serialized_view','accepted_report',None):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp)
                for module in (T,Q):stack.enter_context(patch.object(module,'ROOT',root))
                stack.enter_context(patch.object(H,'delegated_authority',return_value={'approved':True}))
                stack.enter_context(patch.object(Q,'authority',return_value={'model':'gpt-6.1-sol','reasoning':'high'}))
                stack.enter_context(patch.object(Q,'native_command',return_value=['codex']))
                stack.enter_context(patch.object(P,'remaining_call_units',return_value=100))
                stack.enter_context(patch.object(S,'maybe_review'))
                state,folder=self.fixture_plan(root);state['model_calls']=0
                ctl=Mock();ctl.state=state;ctl.profile={};snapshots=[];final={}
                ctl.save=lambda:snapshots.append(copy.deepcopy(state))
                def model_call(command,prompt,call,**kwargs):
                    call.mkdir(parents=True)
                    bundle=json.loads((call.parent/'evidence.json').read_text())
                    state['model_calls']+=1;ctl.save()
                    report={'review_key':bundle['review_key'],'job_id':'J1','verdict':'PASS','summary':'Offline fixture',
                        'reviewed_files':list(bundle['files']),
                        'facets':[{'facet':facet,'status':'PASS','evidence':'Offline fixture'} for facet in Q.FACETS],
                        'findings':[],'limitations':[]}
                    Path(command[command.index('-o')+1]).write_text(json.dumps(report),encoding='utf8')
                    receipt={'exit_code':0,'process_reaped':True}
                    raw='\n'.join(json.dumps(e) for e in [{'type':'thread.started','thread_id':'offline'}, {'type':'turn.completed'}])
                    (call/'request.json').write_text(json.dumps({'argv':command}),encoding='utf8')
                    (call/'receipt.json').write_text(json.dumps(receipt),encoding='utf8')
                    (call/'stdout.jsonl').write_text(raw,encoding='utf8');(call/'stderr.txt').write_text('',encoding='utf8')
                    if bundle['transport_final']:
                        final.update(bundle=bundle,call=call)
                        if mutation=='source':target=root/'jobs/J1/work/analysis.py'
                        elif mutation=='serialized_view':
                            plan=json.loads((root/state['qa_transport_plan']['path']).read_text());target=root/plan['views'][0]['path']
                        elif mutation=='accepted_report':
                            target=root/next(p for p in bundle['files'] if '/qa/' in p and p.endswith('/report.json'))
                        else:target=None
                        if target:
                            final['changed_path']=target;final['original_bytes']=target.read_bytes()
                            target.write_bytes(target.read_bytes()+b' ')
                    return receipt,raw
                ctl.model_call=model_call
                if mutation:
                    with self.assertRaises((ValueError,RuntimeError)):T.review_plan(ctl,folder)
                    key=final['bundle']['review_key']
                    self.assertNotIn(key,state.get('qa_reviews',{}))
                    self.assertNotEqual(state.get('latest_qa_review',{}).get('review_key'),key)
                    self.assertTrue(all(key not in snapshot.get('qa_reviews',{}) for snapshot in snapshots))
                    self.assertTrue((final['call']/'receipt.json').exists())
                    self.assertTrue((final['call'].parent/'response.json').exists())
                    spent=state['model_calls'];self.assertGreater(spent,0)
                    final['changed_path'].write_bytes(final['original_bytes'])
                    with self.assertRaisesRegex(RuntimeError,'rejected final'):
                        T.review_plan(ctl,folder)
                    self.assertEqual(state['model_calls'],spent)
                    with self.assertRaises(ValueError):Q.validate_decision({'action':'READY_FOR_FORWARD'},state['latest_qa_review'])
                    with self.assertRaisesRegex(ValueError,'before using'):
                        Q.validate_decision({'next_job':{'job_class':'EXPERIMENT','inputs':[
                            {'path':'jobs/J1/work/result.json'}]}},state['latest_qa_review'])
                else:
                    accepted=T.review_plan(ctl,folder);key=final['bundle']['review_key']
                    self.assertFalse(accepted['partial_review'])
                    self.assertEqual(state['latest_qa_review']['review_key'],key)
                    self.assertTrue((final['call'].parent/'acceptance.json').exists())
                    spent=state['model_calls'];T.review_plan(ctl,folder)
                    self.assertEqual(state['model_calls'],spent)

    def test_failed_research_view_delivers_actual_maintenance_pass_to_lead(self):
        from tools.quant_research_loop import qa_review as Q,safety_hold as H,skill_engineering as S
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);stack.enter_context(patch.object(T,'ROOT',root))
            stack.enter_context(patch.object(H,'delegated_authority',return_value={'approved':True}))
            stack.enter_context(patch.object(Q,'authority',return_value={'model':'gpt-6.1-sol','reasoning':'high'}))
            stack.enter_context(patch.object(S,'maybe_review'))
            state,folder=self.fixture_plan(root);ctl=Mock();ctl.state=state;ctl.profile={'max_calls':100}
            def review(controller,bundle,**kwargs):
                maintenance=bundle['transport_maintenance']
                return {'path':'maintenance' if maintenance else 'research','verdict':'PASS' if maintenance else 'CHANGES_REQUIRED',
                        'report':{'findings':[] if maintenance else ['material defect']},'partial_review':True}
            stack.enter_context(patch.object(Q,'review_bundle',side_effect=review))
            result=T.review_plan(ctl,folder)
            self.assertEqual(result['verdict'],'CHANGES_REQUIRED')
            self.assertTrue(result['partial_review'])
            self.assertEqual(result['transport_maintenance_review']['path'],'maintenance')
            self.assertEqual(result['transport_maintenance_review']['verdict'],'PASS')
            self.assertIn('jobs/J1/work/result.json',result['blocked_source_hashes'])

    def test_authenticated_view_retry_preserves_failure_and_skips_other_views(self):
        from tools.quant_research_loop import autonomous_recovery as A
        with tempfile.TemporaryDirectory() as tmp,patch.object(T,'ROOT',Path(tmp)):
            root=Path(tmp);p=root/'failed.json'
            p.write_text(json.dumps({'verdict':'INCOMPLETE','limitations':[
                'Controller-generated failure envelope; no accepted model QA verdict.']}),encoding='utf8')
            pin=T.pin(p);before=p.read_bytes()
            state={'qa_reviews':{'base':dict(pin,verdict='INCOMPLETE',job_id='J1')}}
            bundle={'review_key':'base','job_id':'J1','files':{},'transport_plan_key':'plan'}
            retry={'generation':'a'*64,'failed_review_key':'base','report':pin}
            with patch.object(A,'qa_retry_context',return_value=retry):
                first=T.retry_bundle(state,bundle)
                self.assertNotEqual(first['review_key'],'base')
                self.assertEqual(first,T.retry_bundle(state,bundle))
                other=dict(bundle,review_key='maintenance')
                self.assertEqual(T.retry_bundle(state,other),other)
                self.assertEqual(p.read_bytes(),before)
                # A later authenticated failure retries the same original view,
                # without changing an accepted sibling or refunding the old call.
                state['qa_reviews'][first['review_key']]=dict(pin,verdict='INCOMPLETE',job_id='J1',
                    transport_base_review_key='base')
                retry.update(generation='b'*64,failed_review_key=first['review_key'])
                second=T.retry_bundle(state,bundle)
                self.assertNotEqual(second['review_key'],first['review_key'])
                p.write_text('changed',encoding='utf8')
                with self.assertRaises(ValueError):T.retry_bundle(state,bundle)

    def test_scoped_pass_with_unknown_facet_remains_invalid(self):
        from tools.quant_research_loop import qa_review as Q
        full=self.full();full['review_key']='k';full['transport_review_scope']='Static source only'
        report={'review_key':'k','job_id':'J1','verdict':'PASS','summary':'fixture',
                'reviewed_files':list(full['files']),
                'facets':[{'facet':f,'status':'PASS','evidence':'Scoped fixture check'} for f in Q.FACETS],
                'findings':[],'limitations':[]}
        report['facets'][2]['status']='UNKNOWN'
        with self.assertRaisesRegex(ValueError,'PASS conflicts'):Q.validate_report(report,full)
        report['verdict']='INCOMPLETE'
        self.assertEqual(Q.validate_report(report,full)['verdict'],'INCOMPLETE')

    def test_two_view_recovery_keeps_accepted_sibling_and_exact_dependencies(self):
        from tools.quant_research_loop import qa_review as Q,safety_hold as H,skill_engineering as S
        from tools.quant_research_loop import runtime_policy as P,autonomous_recovery as A
        for later_failure in ('execution','synthesis'):
            with self.subTest(later_failure=later_failure),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp);runtime=root/'research/campaigns/PAYOUT_V1/runtime'
                for module in (T,Q,A):stack.enter_context(patch.object(module,'ROOT',root))
                stack.enter_context(patch.object(A,'RUNTIME',runtime))
                stack.enter_context(patch.object(A,'authority',return_value={'approved':True}))
                stack.enter_context(patch.object(H,'delegated_authority',return_value={'approved':True}))
                stack.enter_context(patch.object(Q,'authority',return_value={'model':'gpt-6.1-sol','reasoning':'high'}))
                stack.enter_context(patch.object(Q,'native_command',return_value=['offline-provider']))
                stack.enter_context(patch.object(P,'remaining_call_units',return_value=100))
                stack.enter_context(patch.object(S,'maybe_review'))
                state,folder=self.fixture_plan(root);state['model_calls']=0
                ctl=Mock();ctl.state=state;ctl.profile={};ctl.save=lambda:None
                calls=[];roles=[];failed_roles=set()
                def model_call(command,prompt,call,**kwargs):
                    call.mkdir(parents=True)
                    bundle=json.loads((call.parent/'evidence.json').read_text())
                    role=('maintenance' if bundle['transport_maintenance'] else 'synthesis'
                          if bundle['transport_final'] else 'static'
                          if not any('/report.json' in p for p in bundle['files']) else 'execution')
                    calls.append(copy.deepcopy(bundle));roles.append(role);state['model_calls']+=1
                    report={'review_key':bundle['review_key'],'job_id':'J1','verdict':'PASS','summary':'Offline fixture',
                            'reviewed_files':list(bundle['files']),
                            'facets':[{'facet':f,'status':'PASS','evidence':'Scoped fixture'} for f in Q.FACETS],
                            'findings':[],'limitations':[]}
                    if role in ('static',later_failure) and role not in failed_roles:
                        report['facets'][2]['status']='UNKNOWN';failed_roles.add(role)
                    Path(command[command.index('-o')+1]).write_text(json.dumps(report),encoding='utf8')
                    receipt={'exit_code':0,'process_reaped':True}
                    raw='\n'.join(json.dumps(e) for e in [{'type':'thread.started','thread_id':'offline'},{'type':'turn.completed'}])
                    (call/'request.json').write_text(json.dumps({'argv':command}),encoding='utf8')
                    (call/'receipt.json').write_text(json.dumps(receipt),encoding='utf8')
                    (call/'stdout.jsonl').write_text(raw,encoding='utf8');(call/'stderr.txt').write_text('',encoding='utf8')
                    return receipt,raw
                ctl.model_call=model_call
                def authenticate(failed,generation):
                    ref={k:failed[k] for k in ('path','sha256')};base=(root/ref['path']).parent
                    failure={'job_id':'J1','failed_review_key':failed['review_key'],'report':ref,
                             'evidence_refs':[T.pin(p) for p in base.rglob('*') if p.is_file()]}
                    incident=runtime/'autonomous_recovery'/generation/'incident.json'
                    incident.parent.mkdir(parents=True)
                    incident.write_text(json.dumps({'revision_id':A.REVISION,'worker_dispatch_authorized':False,
                                                     'qa_failure':failure}),encoding='utf8')
                    retry={'generation':generation,'incident':T.pin(incident),
                           'failed_review_key':failed['review_key'],'report':ref}
                    state.setdefault('autonomous_recovery',{}).setdefault('qa_retries',{})['J1']=retry
                    self.assertEqual(A.qa_retry_context(state,'J1'),retry)
                    return incident
                first=T.review_plan(ctl,folder);self.assertEqual(first['verdict'],'INCOMPLETE')
                first_bytes=T.checked(first).read_bytes();authenticate(first,'a'*64)
                second=T.review_plan(ctl,folder);self.assertEqual(second['verdict'],'INCOMPLETE')
                self.assertNotEqual(second['review_key'],first['review_key'])
                second_bytes=T.checked(second).read_bytes();authenticate(second,'b'*64)
                accepted=T.review_plan(ctl,folder)
                self.assertEqual(accepted['verdict'],'PASS');self.assertFalse(accepted['partial_review'])
                self.assertEqual(roles.count('maintenance'),1);self.assertEqual(roles.count('static'),2)
                self.assertEqual(T.checked(first).read_bytes(),first_bytes)
                self.assertEqual(T.checked(second).read_bytes(),second_bytes)
                self.assertEqual(state['qa_reviews'][first['review_key']]['verdict'],'INCOMPLETE')
                self.assertEqual(state['qa_reviews'][second['review_key']]['verdict'],'INCOMPLETE')
                source=next(b for b,r in zip(calls,roles) if r=='static' and 'qa_retry_context' in b)
                source_ref=state['qa_reviews'][source['review_key']]
                for bundle,role in zip(calls,roles):
                    if role=='execution':self.assertEqual(bundle['files'][source_ref['path']]['sha256'],source_ref['sha256'])
                spent=state['model_calls'];T.review_plan(ctl,folder);self.assertEqual(state['model_calls'],spent)
                self.assertTrue((root/accepted['path']).parent.joinpath('acceptance.json').exists())
                base=T.load_plan(state,folder)[3][0]
                self.assertEqual(T.retry_bundle(state,base)['review_key'],source['review_key'])
                for mutation in ('plan','source','dependency'):
                    changed=copy.deepcopy(base)
                    if mutation=='plan':changed['transport_plan_key']='different'
                    elif mutation=='source':next(iter(changed['files'].values()))['content']+=' changed'
                    else:changed['files']['dependency.json']=T.file_content('changed','{}')
                    with self.subTest(mutation=mutation),self.assertRaises(ValueError):T.retry_bundle(state,changed)
                changed=copy.deepcopy(base);changed['review_key']='new-plan-view'
                self.assertEqual(T.retry_bundle(state,changed),changed)
                # Authentic historical retry evidence must remain intact even
                # after the active generation has moved to a different view.
                historical=root/source['qa_retry_context']['incident']['path']
                historical.write_bytes(historical.read_bytes()+b' ')
                with self.assertRaises(ValueError):T.retry_bundle(state,base)


class MarketViewTests(unittest.TestCase):
    def full(self):
        result='''{"job_id":"M1","payload":{"records":[{"net":-0.0}],
          "trades":[{"net":1.230000000000000001e-3},{"net":1.230000000000000001e-3}],
          "daily":[{"net":1e+02}],"marks":[{"net":null}],
          "money_reconciliation":[],"correction_comparison":[],
          "integration_checks":{"fixtures":[{"expected":1e+02}],
          "market_execution_traces":{"a/b~c":[{"net":-0.0},{"net":-0.0}]}}}}'''
        files={'jobs/M1/job.json':T.file_content('job','{"job_id":"M1"}'),
               'jobs/M1/work/analysis.py':T.file_content('analysis','# source\n'),
               'jobs/M1/work/result.json':T.file_content('result',result),
               'jobs/M1/worker_call_0/receipt.json':T.file_content('receipt','{"exit_code":0}')}
        return {'job_id':'M1','files':files,'required_complete_files':list(files),
                'recovery_context':{'content':'{"spent":1}','sha256':'recovery','truncated':False},
                'deterministically_verified':True,
                'execution_trace':[{'command':'actual module entry','exit_code':0}]}

    def test_complete_market_views_preserve_lexemes_duplicates_and_invocation_context(self):
        full=self.full();views,coverage=T.build_market_views(full)
        self.assertEqual(coverage['lexical_value_sha256'],coverage['reconstructed_sha256'])
        self.assertEqual(coverage['array_counts']['/payload/trades'],2)
        self.assertEqual(coverage['array_counts']['/payload/integration_checks/market_execution_traces/a~1b~0c'],2)
        self.assertGreater(coverage['duplicate_rows_referenced'],0)
        self.assertIn('actual module entry',views[0]['files']['controller_state/qa_context.json']['content'])
        self.assertIn('jobs/M1/worker_call_0/receipt.json',views[0]['files'])
        text=''.join(v['files'].get('jobs/M1/work/result.json',{}).get('content','') for v in views)
        for token in ('1.230000000000000001e-3','1e+02','-0.0'):
            self.assertIn(token,text)
        self.assertEqual(T.validate_market_coverage(full,views),coverage)

    def test_market_missing_hash_and_same_value_different_lexeme_block(self):
        full=self.full();views,_=T.build_market_views(full);name='jobs/M1/work/result.json'
        for mode in ('missing','hash','lexeme','mapping','recovery'):
            changed=copy.deepcopy(views)
            if mode=='missing':changed=changed[1:]
            elif mode=='recovery':
                for view in changed:view['files'].pop('controller_state/recovery_context.json',None)
            else:
                kind='references' if mode=='mapping' else 'rows'
                item=next(v['files'][name] for v in changed
                          if name in v['files'] and T.loads(v['files'][name]['content'])['kind']==kind)
                if mode=='hash':item['sha256']='wrong'
                elif mode=='lexeme':item['content']=item['content'].replace('1e+02','100',1)
                else:
                    value=T.loads(item['content']);value['arrays']['/payload/trades'].pop()
                    item['content']=T.emit(value)
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                T.validate_market_coverage(full,changed)

    def test_market_source_preflight_rejects_missing_truncated_and_unverified(self):
        for mode in ('missing','truncated','unverified','recovery'):
            full=self.full()
            if mode=='missing':full['files'].pop('jobs/M1/worker_call_0/receipt.json')
            elif mode=='truncated':full['files']['jobs/M1/work/result.json']['truncated']=True
            elif mode=='unverified':full['deterministically_verified']=False
            else:full['recovery_context']['truncated']=True
            with self.subTest(mode=mode),self.assertRaises(ValueError):T.build_market_views(full)


if __name__=='__main__':unittest.main()

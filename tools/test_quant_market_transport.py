import copy
import unittest

from tools.quant_research_loop import qa_transport as T


class MarketTransportTests(unittest.TestCase):
    def full(self):
        result=T.loads('''{"job_id":"MARKET","payload":{"records":[{"trade_count":1}],
          "trades":[{"id":"t","net":1.23000000000000001e-3}],
          "daily":[{"n":-0.0}],"marks":[{"x":1e+02},{"x":1e+02},{"x":100.0}],
          "money_reconciliation":[{"ok":true}],"correction_comparison":[{"net":null}],
          "integration_checks":{"fixtures":[{"source_rows":[{"x":1}],"pass":true}],
            "market_execution_traces":{"a/~b":[{"x":1e+02},{"x":1e+02}],"c":[{"x":100.0}]}},
          "risk_ceiling_usd":200,"unresolved_exposure":[]}}''')
        files={'jobs/MARKET/job.json':T.file_content('job','{"job_id":"MARKET"}'),
               'jobs/MARKET/work/analysis.py':T.file_content('code','# code\n'),
               'jobs/MARKET/work/result.json':T.file_content('result',T.emit(result)),
               'receipt.json':T.file_content('receipt','{"exit_code":0}')}
        return {'job_id':'MARKET','files':files,'required_complete_files':list(files),
            'recovery_context':{'content':'{"spent":1}','sha256':'recovery','truncated':False},
            'deterministically_verified':True,'execution_trace':[],'market_transport':'exact_rows_v1'}

    def mutate(self,views,kind,change):
        for view in views:
            for name,item in view['files'].items():
                if name.endswith('/work/result.json'):
                    value=T.loads(item['content'])
                    if value['kind']==kind:
                        change(value);item['content']=T.emit(value);return
        self.fail('missing kind '+kind)

    def test_complete_roundtrip_preserves_order_duplicates_and_numeric_spelling(self):
        full=self.full();views,coverage=T.build_market_views(full)
        self.assertTrue(coverage['complete'])
        self.assertEqual(coverage['lexical_value_sha256'],coverage['reconstructed_sha256'])
        self.assertGreater(coverage['duplicate_rows_referenced'],0)
        self.assertEqual(coverage['array_counts']['/payload/marks'],3)
        text=''.join(x['content'] for v in views for x in v['files'].values())
        for token in ('1.23000000000000001e-3','-0.0','1e+02','100.0','a~1~0b'):
            self.assertIn(token,text)
        self.assertTrue(all(T.size(v['files'])<=T.MAX_CONTENT-T.REVIEW_CONTEXT_RESERVE for v in views))

    def test_missing_row_reference_range_or_view_rejected(self):
        for kind,change in [('rows',lambda v:v['rows'].pop(next(iter(v['rows'])))),
                            ('references',lambda v:next(iter(v['arrays'].values())).pop())]:
            full=self.full();views,_=T.build_market_views(full);self.mutate(views,kind,change)
            with self.subTest(kind=kind),self.assertRaises(ValueError):T.validate_coverage(full,views)
        full=self.full();views,_=T.build_market_views(full)
        with self.assertRaises(ValueError):T.validate_coverage(full,views[:-1])

    def test_numeric_change_dangling_reference_and_template_corruption_rejected(self):
        def numeric(v):
            row=next(x for x in v['rows'].values() if any(isinstance(y,T.Number) for y in x['values']))
            i=next(i for i,x in enumerate(row['values']) if isinstance(x,T.Number));row['values'][i]=T.Number('7.0')
        def dangling(v):next(iter(v['arrays'].values()))[0]=T.Number('99999')
        def template(v):next(iter(v['templates'].values()))[0]='changed'
        for kind,change in [('rows',numeric),('references',dangling),('rows',template)]:
            full=self.full();views,_=T.build_market_views(full);self.mutate(views,kind,change)
            with self.subTest(kind=kind),self.assertRaises(ValueError):T.validate_coverage(full,views)

    def test_hash_truncation_recovery_and_supporting_omission_rejected(self):
        for mutation in ['hash','truncated','recovery','support']:
            full=self.full();views,_=T.build_market_views(full)
            if mutation in ('hash','truncated'):
                item=next(v['files']['jobs/MARKET/work/result.json'] for v in views if 'jobs/MARKET/work/result.json' in v['files'])
                item['sha256' if mutation=='hash' else 'truncated']='wrong' if mutation=='hash' else True
            else:
                name='controller_state/recovery_context.json' if mutation=='recovery' else 'receipt.json'
                for v in views:v['files'].pop(name,None)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):T.validate_coverage(full,views)

    def test_forged_duplicate_location_and_reordered_rows_rejected(self):
        for kind,change in [('rows',lambda v:v['locations'].pop(next(iter(v['locations'])))),
            ('references',lambda v: v['arrays']['/payload/marks'].reverse())]:
            full=self.full();views,_=T.build_market_views(full);self.mutate(views,kind,change)
            with self.subTest(kind=kind),self.assertRaises(ValueError):T.validate_coverage(full,views)

    def test_failed_historical_baseline_remains_complete_required_dependency(self):
        full=self.full();name='jobs/OLD/work/result.json'
        old=T.loads('{"payload":{"records":[{"failed":true}],"trades":[{"net":-1e-2}],"daily_cash":[{"x":null}],"risk_observations":[{"x":-0.0}]}}')
        full['files'][name]=T.file_content('old',T.emit(old));full['required_complete_files'].append(name)
        full['supporting_market_results']=[name]
        views,coverage=T.build_market_views(full)
        self.assertIn(name,coverage['supporting_results'])
        self.assertTrue(any(v.get('supporting_result')==name for v in views))
        with self.assertRaises(ValueError):T.validate_coverage(full,[v for v in views if not v.get('supporting_result')])

    def asia(self):
        full=self.full()
        row='{"case":"end_label","actual":{"net":1.23000000000000001e-3,"open":null},"mutations":[{"delta":-0.01,"expected":false},{"delta":0.01,"expected":false}]}'
        result=T.loads('{"payload":{"records":[{"market_trades":0}],"synthetic_checks":['+row+','+row+'],"synthetic_trades":[{"net":-0.0}],"unresolved_exposure":[{"net":null}],"market_input_contract":{"research_prices_allowed_now":false,"date_inventory":[{"date":"2026-01-01","ok":false}]},"semantic_diff":{"serialization":{"before":false,"after":true},"monetary_comparator":{"witnesses":['+row+']}}}}')
        full['files']['jobs/MARKET/work/result.json']=T.file_content('result',T.emit(result))
        return full

    def test_asia_synthetic_witnesses_share_only_identical_complete_objects(self):
        full=self.asia();views,c=T.build_market_views(full)
        self.assertEqual(c['duplicate_rows_referenced'],2)
        self.assertEqual(c['array_counts']['/payload/synthetic_checks'],2)
        self.assertEqual(c['array_counts']['/payload/semantic_diff/monetary_comparator/witnesses'],1)
        self.assertEqual(c['lexical_value_sha256'],c['reconstructed_sha256'])
        self.assertTrue(all('synthetic Asia adapter' in v['scope'] for v in views))
        for token in ('1.23000000000000001e-3','-0.0','-0.01','0.01','null'):
            self.assertIn(token,''.join(i['content'] for v in views for i in v['files'].values()))

    def test_asia_missing_inventory_and_changed_nested_money_are_rejected(self):
        def money(v):
            for row in v['rows'].values():
                for value in row['values']:
                    if isinstance(value,dict) and 'net' in value:
                        value['net']=T.Number('0.00123');return
        def missing(v):v['arrays'].pop('/payload/market_input_contract/date_inventory')
        for kind,change in [('rows',money),('references',missing)]:
            full=self.asia();views,_=T.build_market_views(full);self.mutate(views,kind,change)
            with self.subTest(kind=kind),self.assertRaises(ValueError):T.validate_coverage(full,views)

    def asia_market(self):
        full=self.full();name='jobs/MARKET/work/result.json'
        result=T.loads(full['files'][name]['content']);p=result['payload']
        del p['money_reconciliation'];del p['correction_comparison']
        p['integration_checks']={'status':'PENDING_INDEPENDENT_QA','inherited_PASS':False,
            'cases':[{'case_id':str(i),'witnesses':[{'actual':{'net':T.Number('-0.0')},'expected':None}]*2}
                     for i in range(3)]}
        p['evaluation_contract']={'partition':'CONSUMED_2026_DEVELOPMENT_PROXY'}
        p['calendar_inventory']=[{'date':'2026-01-01','disposition':'excluded'},{'date':'2026-04-21','valid':False}]
        p['synthetic_checks']=[{'expected':T.Number('1.23000000000000001e-3'),'actual':T.Number('1.23000000000000001e-3')}]
        p['unresolved_exposure']=[{'net_pnl':None,'exit_price':None}]
        p['business_screen']=[{'monthly_floor':500,'accepted':False}]
        full['files'][name]=T.file_content('result',T.emit(result))
        return full

    def test_asia_market_schema_roundtrip_keeps_every_field_and_scope(self):
        full=self.asia_market();views,c=T.build_market_views(full)
        self.assertTrue(c['complete'])
        self.assertEqual(c['lexical_value_sha256'],c['reconstructed_sha256'])
        self.assertEqual(c['array_counts'],{'/payload/trades':1,'/payload/daily':1,'/payload/marks':3,
            '/payload/synthetic_checks':1,'/payload/calendar_inventory':2,'/payload/unresolved_exposure':1,
            **{'/payload/integration_checks/cases/'+str(i)+'/witnesses':2 for i in range(3)}})
        self.assertTrue(all('saved Asia fixed-rule baseline' in v['scope'] for v in views))
        self.assertTrue(all('synthetic Asia adapter evidence' not in v['scope'] for v in views))

    def test_asia_market_large_nested_execution_witnesses_stay_complete_and_bounded(self):
        full=self.asia_market();name='jobs/MARKET/work/result.json'
        result=T.loads(full['files'][name]['content'])
        witness={'evidence':'x'*6000,'actual':{'net':T.Number('1.23000000000000001e-3')}}
        result['payload']['integration_checks']['cases'][0]['witnesses']=[witness]*100
        full['files'][name]=T.file_content('result',T.emit(result))
        views,c=T.build_market_views(full)
        self.assertEqual(c['array_counts']['/payload/integration_checks/cases/0/witnesses'],100)
        self.assertEqual(c['lexical_value_sha256'],c['reconstructed_sha256'])
        self.assertTrue(all(T.size(v['files'])<=T.MAX_CONTENT-T.REVIEW_CONTEXT_RESERVE for v in views))
        bad=copy.deepcopy(views)
        self.mutate(bad,'references',lambda v:v['arrays'].pop('/payload/integration_checks/cases/0/witnesses'))
        with self.assertRaises(ValueError):T.validate_coverage(full,bad)

    def test_asia_market_missing_section_row_or_numeric_lexeme_change_rejected(self):
        def section(v):v['arrays'].pop('/payload/calendar_inventory')
        def row(v):v['rows'].pop(next(iter(v['rows'])))
        def number(v):
            for entry in v['rows'].values():
                if any(isinstance(x,T.Number) for x in entry['values']):
                    i=next(i for i,x in enumerate(entry['values']) if isinstance(x,T.Number))
                    entry['values'][i]=T.Number('0.00123');return
            self.fail('fixture has no numeric operand')
        for kind,change in [('references',section),('rows',row),('rows',number)]:
            full=self.asia_market();views,_=T.build_market_views(full);self.mutate(views,kind,change)
            with self.subTest(kind=kind),self.assertRaises(ValueError):T.validate_coverage(full,views)


if __name__=='__main__':unittest.main()

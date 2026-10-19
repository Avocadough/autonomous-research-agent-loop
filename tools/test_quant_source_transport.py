"""Lossless source-only permissions views; no prices or worker execution."""
import copy
import unittest
from unittest.mock import patch
from tools.quant_research_loop import qa_transport as T


def fixture():
    prefix='research/campaigns/PAYOUT_V1/runtime/jobs/SOURCE_DISCOVERY/'
    scope={'before':{'sha256':'b'},'after':{'sha256':'a'},'file_contents_read':False,
        'changes':{f'p{i}':{'before':[None,T.Number('1728000000000000001')],
                            'after':[i,T.Number('1728000000000000002')]} for i in range(8)},
        'unexpected':[f'p{i}' for i in range(7)],'limitations':'Actor UNKNOWN'}
    storage={'validated_storage':{f'p{i}':{'storage_sha256':str(i),'change':'MTIME_ONLY'} for i in range(7)},
        'remaining_unexpected':[],'rejected':{},'raw_unexpected':[f'p{i}' for i in range(7)],
        'claim_limit':'Current storage only; no scope PASS'}
    values={'job.json':{'job_class':'DISCOVERY'},'work/result.json':{'job_class':'DISCOVERY',
        'payload':{'market_rows_read':0,'trade_count':0,'records':[],'trades':[]}},
        'metadata_scope_check_0.json':scope,'metadata_storage_check_0.json':storage}
    files={prefix+n:T.file_content(T.text_hash(T.emit(v)),T.emit(v)) for n,v in values.items()}
    files[prefix+'work/analysis.py']=T.file_content('code','print("no prices")')
    files[prefix+'worker_call_0/receipt.json']=T.file_content('receipt','{"exit_code":0}')
    return {'job_id':'SOURCE_DISCOVERY','files':files,'required_complete_files':list(files),
        'deterministically_verified':True,'qa_route':{'model':'qa','reasoning':'high'},
        'recovery_context':{'sha256':'recovery','content':'{}','truncated':False}}


class SourceTransportTests(unittest.TestCase):
    def build(self):
        full=fixture()
        with patch.object(T,'SOURCE_METADATA_BATCH',3):views,coverage=T.build_views(full)
        return full,views,coverage

    def test_complete_ordered_lossless_coverage(self):
        full,views,c=self.build()
        self.assertEqual(len(views),4)
        self.assertTrue(c['complete'])
        self.assertEqual(c,T.validate_coverage(full,views))
        for meta in c['metadata'].values():
            self.assertEqual(meta['lexical_value_sha256'],meta['reconstructed_sha256'])
        self.assertEqual(full['source_transport'],'exact_metadata_v1')

    def test_missing_or_modified_source_and_context(self):
        full,views,_=self.build()
        for suffix in ('receipt.json','result.json','qa_context.json','recovery_context.json'):
            changed=copy.deepcopy(views)
            for v in changed:
                for n in list(v['files']):
                    if n.endswith(suffix):del v['files'][n]
            with self.subTest(suffix=suffix),self.assertRaises(ValueError):T.validate_coverage(full,changed)

    def test_missing_part_and_duplicate_entry(self):
        full,views,_=self.build()
        with self.assertRaises(ValueError):T.validate_coverage(full,views[:-1])
        changed=copy.deepcopy(views)
        n=next(n for n in changed[1]['files'] if 'metadata_scope_check' in n)
        obj=T.loads(changed[1]['files'][n]['content'])
        obj['indexed_fields']['changes']['entries'].append(obj['indexed_fields']['changes']['entries'][0])
        changed[1]['files'][n]['content']=T.emit(obj)
        with self.assertRaises(ValueError):T.validate_coverage(full,changed)

    def test_mutations_fail_closed(self):
        full,views,_=self.build()
        for mutation in ('number','key','null','order','count','static','root_order','kind','range','missing_section','hash','representation','truncated'):
            changed=copy.deepcopy(views);v=changed[1]
            n=next(n for n in v['files'] if 'metadata_scope_check' in n);item=v['files'][n]
            obj=T.loads(item['content']);s=obj['indexed_fields']['changes'];entry=s['entries'][0]
            if mutation=='number':entry[2]['after'][1]=T.Number('1728000000000000002.0')
            elif mutation=='key':entry[1]='different'
            elif mutation=='null':entry[2]['before'][0]='null'
            elif mutation=='order':s['entries'].reverse()
            elif mutation=='count':s['count']=9
            elif mutation=='static':obj['static_fields']['file_contents_read']=True
            elif mutation=='root_order':obj['root_field_order'].reverse()
            elif mutation=='kind':s['type']='array'
            elif mutation=='range':entry[0]=99
            elif mutation=='missing_section':del obj['indexed_fields']['unexpected']
            elif mutation=='hash':item['sha256']='bad'
            elif mutation=='representation':item['representation']='complete_source_text'
            elif mutation=='truncated':item['truncated']=True
            item['content']=T.emit(obj)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):T.validate_coverage(full,changed)

    def test_original_hash_and_result_content_cannot_change(self):
        full,views,_=self.build()
        changed=copy.deepcopy(views);n=next(n for n in changed[0]['files'] if n.endswith('/work/result.json'))
        changed[0]['files'][n]['content']='{}'
        with self.assertRaises(ValueError):T.validate_coverage(full,changed)

    def test_unverified_incomplete_oversize_and_non_source(self):
        for change in ('unverified','missing','truncated','oversize','prices'):
            full=fixture()
            if change=='unverified':full['deterministically_verified']=False
            elif change=='missing':del full['files'][full['required_complete_files'][0]]
            elif change=='truncated':full['files'][full['required_complete_files'][0]]['truncated']=True
            elif change=='oversize':full['files'][full['required_complete_files'][-1]]['content']='x'*T.MAX_CONTENT
            else:
                n=next(n for n in full['files'] if n.endswith('/work/result.json'))
                v=T.loads(full['files'][n]['content']);v['payload']['market_rows_read']=1;full['files'][n]['content']=T.emit(v)
            with self.subTest(change=change),self.assertRaises(ValueError):T.build_views(full)

    def test_original_json_parser_rejects_ambiguous_values(self):
        for text in ('{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}'):
            with self.assertRaises(ValueError):T.loads(text)


if __name__=='__main__':unittest.main()

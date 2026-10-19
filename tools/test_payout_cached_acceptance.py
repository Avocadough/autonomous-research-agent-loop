"""Lead307 regression: hash equality alone cannot accept a cached review."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from tools.quant_research_loop import qa_review as Q


class CachedAcceptanceTests(unittest.TestCase):
    def bundle(self):
        return dict(review_key='k', job_id='J', deterministically_verified=True,
                    transport_review_scope='complete fixture', required_complete_files=['source.py'],
                    files={'source.py': dict(sha256='source-pin', truncated=False, content='fixture')})

    def report(self):
        return dict(review_key='k',job_id='J',verdict='PASS',summary='Complete fixture',
                    reviewed_files=['source.py'],facets=[dict(facet=f,status='PASS',evidence='checked') for f in Q.FACETS],
                    findings=[],limitations=[])

    def test_cache_is_revalidated_without_another_call(self):
        for mutation in ('valid','incomplete','unknown','missing','identity','bundle','path'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);folder=root/'research/campaigns/PAYOUT_V1/runtime/qa/k';folder.mkdir(parents=True)
                bundle=self.bundle();report=self.report()
                if mutation=='incomplete': report['verdict']='INCOMPLETE'
                if mutation=='unknown': report['facets'][0]['status']='UNKNOWN'
                if mutation=='missing': report['reviewed_files']=[]
                if mutation=='identity': report['job_id']='OTHER'
                file=folder/'report.json';file.write_text(json.dumps(report),encoding='utf-8')
                ref=dict(path=file.relative_to(root).as_posix(),sha256=Q.digest(file),review_key='k',job_id='J',verdict='PASS',
                         source_hashes={'source.py':'source-pin'},transport_bundle_sha256=Q.hashlib.sha256(
                             json.dumps(bundle,ensure_ascii=False,sort_keys=True).encode()).hexdigest())
                if mutation=='bundle': ref['transport_bundle_sha256']='wrong'
                if mutation=='path': ref['path']='../outside.json'
                controller=Mock();controller.state={'qa_reviews':{'k':ref},'model_calls':12}
                original=copy.deepcopy(controller.state)
                with patch.object(Q,'ROOT',root),patch.object(Q,'authority',return_value={'model':'gpt-6.1-sol','reasoning':'high'}):
                    if mutation=='valid':
                        result=Q.review_bundle(controller,bundle)
                        self.assertEqual(result['report'],report)
                    else:
                        with self.assertRaises((ValueError,Q.jsonschema.ValidationError)):
                            Q.review_bundle(controller,bundle)
                self.assertEqual(controller.state,original)
                controller.model_call.assert_not_called()
                controller.save.assert_not_called()


if __name__=='__main__': unittest.main()

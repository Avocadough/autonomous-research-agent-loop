"""Exercise the actual Lead acceptance boundary with synthetic completed/failing jobs."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'tools/quant_research_loop/controller.py').is_file())
sys.path.insert(0, str(ROOT))
from tools.quant_research_loop import controller as C, generic_jobs as G, qa_review as Q, skill_engineering as S
from tools.quant_research_loop import runtime_policy as P
from tools.quant_research_loop.gate_zero import write
from tools.quant_research_loop.prepare import digest
from tools.quant_research_loop.model_routing import LEAN_REVISION


class ReviewLinkTests(unittest.TestCase):
    def test_only_matching_pass_updates_completed_review(self):
        for qa_job, verdict, pin_mode in [('FAILED_NEW', 'INCOMPLETE', 'match'), ('OTHER_JOB', 'PASS', 'match'),
                ('COMPLETED_OLD', 'INCOMPLETE', 'match'), ('COMPLETED_OLD', 'PASS', 'match'),
                ('COMPLETED_OLD', 'PASS', 'wrong'), ('COMPLETED_OLD', 'PASS', 'missing'),
                ('COMPLETED_OLD', 'PASS', 'history'), ('COMPLETED_OLD', 'PASS', 'no_ref')]:
            with self.subTest(qa_job=qa_job, verdict=verdict, pin_mode=pin_mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runtime = root / 'runtime'
                write(root / 'rules.json', {'protected_data': False})
                write(root / 'result.json', {'payload': {'records': []}})
                qa = {'job_id': qa_job, 'verdict': verdict, 'path': 'qa.json', 'report': {'limitations': []}}
                write(root / 'qa.json', qa)
                record = {'path': 'result.json', 'sha256': digest(root / 'result.json'), 'review': 'prior_accepted.json'}
                qa['source_hashes'] = ({'result.json': record['sha256'] if pin_mode in ('match','history','no_ref') else 'wrong-pin'}
                                       if pin_mode != 'missing' else {})
                if pin_mode == 'history':qa['historical_for_pending_plan'] = True
                controller = C.Controller.__new__(C.Controller)
                controller.state = dict(evidence_id='COMPLETED_OLD', completed={'COMPLETED_OLD': copy.deepcopy(record)},
                    reviewed_results={'COMPLETED_OLD': record['sha256']}, decisions=0, model_calls=0,
                    bootstrap_invocations=0, prior_reserved_calls=0, families={}, active_invocation_s=0)
                controller.profile = dict(lead='gpt-6.1-sol', reasoning='ultra', worker='gpt-6.1-sol',
                    worker_variant='medium', jobs=[{'job_id': 'INITIAL'}], rules_path='rules.json',
                    max_calls=1000, max_active_seconds=21600, model_route_authority={'revision_id': LEAN_REVISION})
                controller.save = lambda: None

                def model_call(command, prompt, folder, **kwargs):
                    write(Path(command[command.index('-o') + 1]), dict(campaign_id='PAYOUT_V1', evidence_id='COMPLETED_OLD',
                        action='PAUSE', rationale='Keep failed-work QA separate from accepted-result context.',
                        artifact_refs=[(folder.parent / 'evidence.json').relative_to(root).as_posix()] +
                            ([] if pin_mode == 'no_ref' else ['qa.json']),
                        next_job=None, worker_dispatch_ready=False, missing_information=[], pause_category='PAUSED_LEAD'))
                    return {'exit_code': 0}, ''

                controller.model_call = model_call
                with patch.object(C, 'ROOT', root), patch.object(C, 'RUNTIME', runtime), \
                        patch.object(C, 'native_command', return_value=['codex']), patch.object(C, '_supplemental_audit', return_value=None), \
                        patch.object(G, 'authorized', return_value=True), patch.object(G, 'authority_catalog', return_value={}), \
                        patch.object(G, 'accounted_seconds', return_value=0), patch.object(Q, 'review', return_value=qa), \
                        patch.object(S, 'maybe_review'), patch.object(P, 'cumulative_runtime_cap_bypassed', return_value=True):
                    self.assertEqual(controller.lead()['action'], 'PAUSE')
                updated = controller.state['completed']['COMPLETED_OLD']
                if qa_job == 'COMPLETED_OLD' and verdict == 'PASS' and pin_mode == 'match':
                    self.assertEqual(updated['review'], str(Path('runtime/decisions/000/accepted.json')))
                else:
                    self.assertEqual(updated, record)
                self.assertEqual(controller.state['reviewed_results'], {'COMPLETED_OLD': record['sha256']})
                self.assertIsNone(controller.state['pending_job'])


if __name__ == '__main__':
    unittest.main()

"""Offline integration of metered skill reviews at safe campaign boundaries."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.quant_research_loop import controller as C
from tools.quant_research_loop import generic_worker as W
from tools.quant_research_loop import lead_recovery as LR
from tools.quant_research_loop import qa_review as Q
from tools.quant_research_loop import runtime_policy as RP
from tools.quant_research_loop import safety_hold as H
from tools.quant_research_loop import skill_engineering as S
from tools.quant_research_loop.gate_zero import write
from tools.quant_research_loop.prepare import digest


class BoundaryReached(RuntimeError):
    """Stop at a mocked boundary without launching a process or model."""


class SkillIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.camp = self.root / 'research/campaigns/PAYOUT_V1'
        self.runtime = self.camp / 'runtime'
        self.runtime.mkdir(parents=True)

    def bare_controller(self, **state):
        controller = C.Controller.__new__(C.Controller)
        controller.state = dict(status='RUNNING', pending_job=None, model_calls=9,
                                bootstrap_invocations=0, prior_reserved_calls=0,
                                active_invocation_s=0, **state)
        controller.profile = {'max_calls': 1000, 'max_active_seconds': 100}
        controller.save = Mock()
        controller.check_budget = Mock()
        return controller

    def skill_guidance(self, version='one'):
        return {'review': {'path': 'skills/report.json', 'sha256': version},
                'skills': [{'path': '.codex/skills/payout-fixture/SKILL.md',
                            'sha256': version, 'content': 'Use the pinned fixture evidence ' + version}]}

    def test_model_call_crossing_threshold_never_nests_a_skill_review(self):
        for name in ('lead_call', 'worker_call_0', 'engineer_call'):
            with self.subTest(call=name):
                controller = self.bare_controller()
                controller.state['skill_engineering'] = {'next_review_at': 10}
                (self.root / name).mkdir()  # The real invoke creates its call folder before returning.
                with patch.object(C, 'ROOT', self.root), patch.object(C, 'RUNTIME', self.runtime), \
                        patch.object(C, 'invoke', return_value=({'exit_code': 0, 'elapsed_s': 1}, 'fixture')) as invoke, \
                        patch.object(C, 'p_remaining', return_value=100), \
                        patch.object(Q, 'cycle_reserve', return_value=5), \
                        patch.object(H, 'guard_call'), \
                        patch.object(RP, 'cumulative_runtime_cap_bypassed', return_value=True), \
                        patch.object(S, 'maybe_review', side_effect=AssertionError('Nested skill review')) as review:
                    receipt, _ = controller.model_call(['fixture'], 'fixture', self.root / name)
                self.assertEqual(receipt['exit_code'], 0)
                self.assertEqual(controller.state['model_calls'], 10)
                self.assertNotIn('inflight_call', controller.state)
                invoke.assert_called_once()
                review.assert_not_called()

    def test_pause_arriving_during_skill_checkpoint_stops_before_budget_charge_or_dispatch(self):
        controller = self.bare_controller()
        counters = {key: controller.state[key] for key in
                    ('model_calls', 'bootstrap_invocations', 'prior_reserved_calls', 'active_invocation_s')}

        def checkpoint(_):
            (self.runtime / 'pause.request').write_text('pause during skill review', encoding='utf-8')

        with patch.object(C, 'ROOT', self.root), patch.object(C, 'RUNTIME', self.runtime), \
                patch.object(S, 'maybe_review', side_effect=checkpoint) as review, \
                patch.object(C, 'invoke', side_effect=AssertionError('No dispatch after pause')) as invoke, \
                patch.object(H, 'guard_call') as guard:
            S.maybe_review(controller)
            with self.assertRaisesRegex(RuntimeError, 'PAUSED_USER: .* before model dispatch'):
                controller.model_call(['fixture'], 'fixture', self.root / 'worker_call_0')
        review.assert_called_once_with(controller)
        controller.check_budget.assert_not_called()
        guard.assert_not_called()
        invoke.assert_not_called()
        controller.save.assert_called_once()
        self.assertEqual(controller.state['status'], 'PAUSED_USER')
        self.assertEqual({key: controller.state[key] for key in counters}, counters)
        self.assertNotIn('inflight_call', controller.state)
        self.assertTrue((self.runtime / 'pause.request').exists())

    def test_cycle_reservation_includes_due_skill_review_before_next_cycle(self):
        state = {'model_calls': 7, 'bootstrap_invocations': 1, 'prior_reserved_calls': 1,
                 'skill_engineering': {'next_review_at': 12}}
        with patch.object(Q, 'authority', return_value={'model': 'fixture'}), \
                patch.object(S, 'authority', return_value={'interval_calls': 10}):
            self.assertEqual(Q.cycle_reserve(state), 5)
            self.assertEqual(Q.cycle_reserve(state, include_skill_review=False), 3)
            state['skill_engineering']['next_review_at'] = 13
            self.assertEqual(Q.cycle_reserve(state), 3)
        with patch.object(Q, 'authority', return_value=None), \
                patch.object(S, 'authority', return_value={'interval_calls': 10}):
            state['skill_engineering']['next_review_at'] = 11
            self.assertEqual(Q.cycle_reserve(state), 4)

    def test_human_stop_and_pause_request_prevent_skill_review_and_dispatch(self):
        for status, paused in [('HUMAN_ACTION_REQUIRED', False), ('PAUSED_USER', True)]:
            with self.subTest(status=status):
                controller = self.bare_controller()
                controller.state['status'] = status
                if paused:
                    (self.runtime / 'pause.request').write_text('fixture pause', encoding='utf-8')
                controller.lead = Mock(side_effect=AssertionError('No Lead dispatch'))
                controller.worker = Mock(side_effect=AssertionError('No worker dispatch'))
                with patch.object(C, 'RUNTIME', self.runtime), \
                        patch.object(S, 'maybe_review', side_effect=AssertionError('Stopped review')) as review:
                    result = controller.run()
                self.assertEqual(result['status'], status)
                review.assert_not_called()
                controller.lead.assert_not_called()
                controller.worker.assert_not_called()
        self.assertTrue((self.runtime / 'pause.request').exists())

    def test_lead_checks_skills_before_and_after_qa_before_building_evidence(self):
        controller = self.bare_controller(decisions=0, completed={}, evidence_id='FIXTURE')
        controller.profile['jobs'] = [{'job_id': 'FIXTURE'}]
        order = []

        def evidence_read(_):
            order.append('evidence')
            raise BoundaryReached('lead evidence')

        with patch.object(C, 'RUNTIME', self.runtime), patch.object(C, 'ROOT', self.root), \
                patch.object(H, 'authority', return_value=None), \
                patch.object(S, 'maybe_review', side_effect=lambda _: order.append('skill')), \
                patch.object(Q, 'review', side_effect=lambda _: order.append('qa')), \
                patch.object(C, 'read', side_effect=evidence_read):
            with self.assertRaisesRegex(BoundaryReached, 'lead evidence'):
                controller.lead()
        self.assertEqual(order, ['skill', 'qa', 'skill', 'evidence'])

    def test_qa_review_refreshes_skills_before_building_cache_identity(self):
        controller = self.bare_controller()
        order = []

        def build_bundle(state, _):
            order.append('bundle')
            self.assertEqual(state['fixture_guidance_version'], 'new')
            raise BoundaryReached('qa bundle')

        def review_skills(_):
            order.append('skill')
            controller.state['fixture_guidance_version'] = 'new'

        with patch.object(Q, 'authority', return_value={'model': 'fixture'}), \
                patch.object(Q, 'target', return_value=self.runtime / 'job'), \
                patch.object(S, 'maybe_review', side_effect=review_skills), \
                patch.object(Q, 'build_bundle', side_effect=build_bundle):
            with self.assertRaisesRegex(BoundaryReached, 'qa bundle'):
                Q.review(controller)
        self.assertEqual(order, ['skill', 'bundle'])

    def test_changed_skill_guidance_invalidates_qa_cache_without_changing_job_evidence(self):
        folder = self.root / 'job'
        folder.mkdir()
        write(folder / 'job.json', {'job_id': 'FIXTURE', 'inputs': []})
        support = self.root / 'tools/quant_research_loop/generic_entry.py'
        support.parent.mkdir(parents=True)
        support.write_text('# fixture support\n', encoding='utf-8')
        state = {'qa_authority': {'path': 'legacy.json', 'sha256': 'legacy'}, 'completed': {}}
        original = copy.deepcopy(state)
        route = {'model': 'gpt-6.1-sol', 'reasoning': 'high'}
        guidance = [self.skill_guidance('one'), self.skill_guidance('two')]
        with patch.object(Q, 'ROOT', self.root), patch.object(Q, 'authority', return_value=route), \
                patch.object(S, 'guidance', side_effect=guidance):
            first, second = [Q.build_bundle(state, folder) for _ in guidance]
        self.assertNotEqual(first['review_key'], second['review_key'])
        self.assertEqual(first['files'], second['files'])
        self.assertEqual(first['qa_route'], second['qa_route'])
        self.assertEqual(first['skill_guidance'], guidance[0])
        self.assertEqual(second['skill_guidance'], guidance[1])
        self.assertEqual(state, original)

    @contextmanager
    def worker_fixture(self, recovery=False):
        folder = self.runtime / 'jobs/FIXTURE_JOB'
        folder.mkdir(parents=True)
        paths = [(folder / 'work' / name).relative_to(self.root).as_posix()
                 for name in ('analysis.py', 'result.json')]
        job = {'job_id': 'FIXTURE_JOB', 'job_class': 'DIAGNOSTIC', 'inputs': [],
               'allowed_file_scope': paths, 'required_outputs': paths,
               'allowed_data': [], 'permissions': {'network': 'none', 'spend_usd': 0, 'live': False}}
        write(folder / 'job.json', job)
        if recovery:
            (folder / 'work').mkdir()
            (folder / 'work/analysis.py').write_text('# saved fixture', encoding='utf-8')
            write(folder / 'work/result.json', {'fixture': True})
        controller = SimpleNamespace(state={'status': 'RUNNING', 'completed': {}},
            profile={'max_calls': 1000, 'worker_provider': 'codex', 'worker': 'gpt-6-luna',
                     'worker_variant': 'max'}, state_path=self.runtime / 'state.json',
            save=Mock(), check_budget=Mock(), model_call=Mock())
        with ExitStack() as stack:
            for context in [patch.object(W, 'ROOT', self.root), patch.object(W, 'CAMP', self.camp),
                            patch.object(W, 'validate_spec'), patch.object(W, 'recover_existing', return_value=False),
                            patch.object(W, 'fixed_entry_command', return_value='FIXTURE --job'),
                            patch.object(W, 'worker_workdir', return_value=folder / 'work'),
                            patch.object(W, 'worker_command', return_value=['fixture-model']),
                            patch.object(S, 'guidance', return_value=self.skill_guidance())]:
                stack.enter_context(context)
            yield controller, job, folder

    def test_worker_inline_guidance_preserves_frozen_paths_and_permissions(self):
        with self.worker_fixture() as (controller, job, folder):
            original = copy.deepcopy(job)
            original_bytes = (folder / 'job.json').read_bytes()
            prompt = W.build_worker_prompt(controller.profile, job, controller.state, 'FIXTURE --job', '')
        self.assertIn('FROZEN JOB FILE: ' + str(folder / 'job.json'), prompt)
        self.assertIn('FROZEN JOB FILE SHA256: ' + digest(folder / 'job.json'), prompt)
        self.assertEqual((folder / 'job.json').read_bytes(), original_bytes)
        self.assertEqual(json.loads(original_bytes), original)
        self.assertNotIn('JOB: {', prompt)
        self.assertEqual(job, original)
        self.assertIn(self.skill_guidance()['skills'][0]['content'], prompt)
        self.assertIn('Do not independently load, install or edit skills.', prompt)
        self.assertNotIn('.codex/skills/payout-fixture/SKILL.md', job['allowed_file_scope'])
        self.assertFalse(job['permissions']['live'])

    def assert_worker_boundary(self, recovery):
        with self.worker_fixture(recovery=recovery) as (controller, job, folder):
            original = copy.deepcopy(job)
            order = []
            selected = self.root / '.codex/skills/payout-fixture/SKILL.md'

            def skill_review(_):
                order.append('skill')
                selected.parent.mkdir(parents=True)
                selected.write_text('reviewed fixture guidance', encoding='utf-8')

            def reserve(*_):
                order.append('reserve')
                self.assertTrue(selected.exists())

            def snapshot():
                order.append('snapshot')
                return {selected.relative_to(self.root).as_posix(): digest(selected)}

            def dispatch(*args, **_):
                order.append('dispatch')
                self.assertIn(self.skill_guidance()['skills'][0]['content'], args[1])
                raise BoundaryReached('mock model boundary')

            controller.model_call.side_effect = dispatch
            saved = {path: path.read_bytes() for path in (folder / 'work').glob('*')} if recovery else {}
            with patch.object(S, 'maybe_review', side_effect=skill_review), \
                    patch.object(LR, 'reserve_episode', side_effect=reserve), \
                    patch.object(W, 'snapshot', side_effect=snapshot):
                with self.assertRaisesRegex(BoundaryReached, 'mock model boundary'):
                    if recovery:
                        W.recovery_verification(controller, job, folder, 'FIXTURE --verify', {'fixture': True})
                    else:
                        W.execute(controller, job)
            self.assertEqual(order, ['skill', 'reserve', 'snapshot', 'dispatch'])
            label = 'recovery_0' if recovery else '0'
            self.assertEqual(json.loads((folder / f'scope_before_{label}.json').read_text(encoding='utf-8')),
                             {selected.relative_to(self.root).as_posix(): digest(selected)})
            self.assertEqual({path: path.read_bytes() for path in saved}, saved)
            self.assertEqual(job, original)

    def test_fresh_worker_reviews_skills_before_reserving_and_snapshotting(self):
        self.assert_worker_boundary(recovery=False)

    def test_recovery_reviews_skills_before_reserving_and_snapshotting(self):
        self.assert_worker_boundary(recovery=True)

    def test_skill_review_failure_prevents_worker_reservation_snapshot_and_dispatch(self):
        with self.worker_fixture() as (controller, job, _):
            with patch.object(S, 'maybe_review', side_effect=RuntimeError('HUMAN_ACTION_REQUIRED: fixture')), \
                    patch.object(LR, 'reserve_episode') as reserve, patch.object(W, 'snapshot') as snapshot:
                with self.assertRaisesRegex(RuntimeError, 'HUMAN_ACTION_REQUIRED'):
                    W.execute(controller, job)
            reserve.assert_not_called()
            snapshot.assert_not_called()
            controller.model_call.assert_not_called()


if __name__ == '__main__':
    unittest.main()

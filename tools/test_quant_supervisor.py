"""Offline supervision tests: no real controller, model, process or campaign writes."""
import copy
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.quant_research_loop import supervisor as S
from tools.quant_research_loop import controller as C
from tools.quant_research_loop import skill_engineering as Skills


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        self.controller = SimpleNamespace(
            state={'status': 'RUNNING', 'model_calls': 12,
                   'bootstrap_invocations': 6, 'prior_reserved_calls': 10},
            profile={'max_calls': 1000}, save=Mock(), run=Mock())

    def supervise(self, recover=None, sleep=None):
        return S.run(self.controller, runtime=self.runtime,
                     recover=recover or Mock(), sleep=sleep or Mock())

    def test_terminal_decisions_and_safety_hold_never_resume_or_recover(self):
        for status in S.TERMINAL_STATUSES:
            with self.subTest(status=status):
                self.controller.state['status'] = status
                recovery = Mock()
                self.supervise(recovery)
                self.controller.run.assert_not_called()
                recovery.assert_not_called()
        self.controller.state.update(status='ENGINEERING_BLOCKED',
            operator_safety_hold={'active': False, 'prior_unreleased_hold': {'active': True}})
        recovery = Mock()
        self.supervise(recovery)
        recovery.assert_not_called()

    def test_pause_and_cancel_are_preserved_without_dispatch(self):
        for name in ('pause.request', 'cancel.request'):
            with self.subTest(name=name):
                marker = self.runtime / name
                marker.write_text('operator request', encoding='utf-8')
                self.controller.state['status'] = 'RUNNING'
                recovery = Mock()
                self.supervise(recovery)
                self.assertEqual(self.controller.state['status'], 'PAUSED_USER')
                self.assertEqual(marker.read_text(encoding='utf-8'), 'operator request')
                self.controller.run.assert_not_called()
                recovery.assert_not_called()
                marker.unlink()

    def test_global_and_operational_cap_stop_before_controller_or_repair(self):
        for profile, used in [({'max_calls': 1000}, 1000),
                              ({'max_calls': 2000}, 1000),
                              ({'max_calls': 1000, 'campaign_stop_calls': 30}, 30)]:
            self.controller.profile = profile
            self.controller.state.update(status='RUNNING', model_calls=used-16)
            recovery = Mock()
            self.supervise(recovery)
            self.assertEqual(self.controller.state['status'], 'RESOURCE_LIMIT')
            self.assertEqual(self.controller.state['model_calls'], used-16)
            self.controller.run.assert_not_called()
            recovery.assert_not_called()

    def test_technical_stop_recovery_then_controller_then_qualification_stop(self):
        events = []
        self.controller.state['status'] = 'ENGINEERING_BLOCKED'

        def repair(controller):
            events.append('repair')
            controller.state.update(status='RUNNING', autonomous_recovery={'review_required': True})
            return {'action': 'RESUME', 'reason': 'fresh Lead review required'}

        def run(resume):
            self.assertTrue(resume)
            self.assertTrue(self.controller.state['autonomous_recovery']['review_required'])
            events.append('controller')
            self.controller.state['status'] = 'READY_FOR_FORWARD'
            return self.controller.state

        self.controller.run.side_effect = run
        result = self.supervise(repair)
        self.assertEqual(events, ['repair', 'controller'])
        self.assertEqual(result['status'], 'READY_FOR_FORWARD')

    def test_returned_technical_stop_is_repaired_before_a_second_run(self):
        events = []

        def run(resume):
            events.append('controller')
            self.controller.state['status'] = ('ENGINEERING_BLOCKED'
                if len(events) == 1 else 'NO_QUALIFYING_CANDIDATE')
            return self.controller.state

        def repair(controller):
            self.assertEqual(controller.state['status'], 'ENGINEERING_BLOCKED')
            events.append('repair')
            controller.state.update(status='RUNNING', decisions=42)
            return {'action': 'RESUME'}

        self.controller.run.side_effect = run
        self.supervise(repair)
        self.assertEqual(events, ['controller', 'repair', 'controller'])

    def test_provider_backoff_rechecks_recovery_not_controller(self):
        self.controller.state['status'] = 'PAUSED_LEAD'
        sleeps = []
        recovery = Mock(side_effect=[
            {'action': 'BACKOFF', 'delay_seconds': 0, 'reason': 'provider capacity'},
            {'action': 'STOP', 'reason': 'no permitted recovery'}])
        self.supervise(recovery, sleeps.append)
        self.assertEqual(sum(sleeps), S.MIN_BACKOFF_SECONDS)
        self.assertLessEqual(max(sleeps), S.POLL_SECONDS)
        self.assertEqual(recovery.call_count, 2)
        self.controller.run.assert_not_called()

    def test_repeated_backoff_grows_and_remains_bounded(self):
        self.controller.state['status'] = 'PAUSED_LEAD'
        sleeps = []
        recovery = Mock(side_effect=[
            {'action': 'BACKOFF', 'delay_seconds': 0},
            {'action': 'BACKOFF', 'delay_seconds': 0},
            {'action': 'BACKOFF', 'delay_seconds': 100000},
            {'action': 'STOP'}])
        self.supervise(recovery, sleeps.append)
        self.assertEqual(sum(sleeps), 3*S.MIN_BACKOFF_SECONDS + S.MAX_BACKOFF_SECONDS)
        self.controller.run.assert_not_called()

    def test_maintenance_handoff_returns_without_wait_or_controller_dispatch(self):
        self.controller.state.update(status='ENGINEERING_BLOCKED',
            maintenance_handoff={'action':'MAINTENANCE_REQUIRED','worker_dispatch_authorized':False})
        before=copy.deepcopy(self.controller.state)
        recovery=Mock(return_value={'action':'MAINTENANCE_REQUIRED'})
        sleep=Mock()
        self.supervise(recovery,sleep)
        recovery.assert_called_once_with(self.controller)
        sleep.assert_not_called()
        self.controller.run.assert_not_called()
        self.assertEqual(self.controller.state,before)

    def test_maintenance_handoff_rejects_missing_checkpoint_running_or_inflight(self):
        for fields in ({}, {'maintenance_handoff':{'action':'MAINTENANCE_REQUIRED','worker_dispatch_authorized':True}},
                       {'maintenance_handoff':{'action':'MAINTENANCE_REQUIRED','worker_dispatch_authorized':False},
                        'inflight_call':{'path':'uncertain'}},
                       {'maintenance_handoff':{'action':'MAINTENANCE_REQUIRED','worker_dispatch_authorized':False},
                        'status':'RUNNING','inflight_call':{'path':'uncertain'}}):
            with self.subTest(fields=fields):
                self.controller.state={'status':'ENGINEERING_BLOCKED','model_calls':12,**fields}
                recovery=Mock(return_value={'action':'MAINTENANCE_REQUIRED'})
                with self.assertRaisesRegex(RuntimeError,'safe persisted checkpoint'):
                    self.supervise(recovery)
                self.controller.run.assert_not_called()

    def test_maintenance_handoff_releases_main_writer_lock(self):
        events=[]
        self.controller.state.update(status='ENGINEERING_BLOCKED',
            maintenance_handoff={'action':'MAINTENANCE_REQUIRED','worker_dispatch_authorized':False})

        @contextmanager
        def lock(_path):
            events.append('acquired')
            try:
                yield
            finally:
                events.append('released')

        def recovery(_controller):
            self.assertEqual(events,['acquired'])
            return {'action':'MAINTENANCE_REQUIRED'}

        from tools.quant_research_loop import autonomous_recovery as A
        with patch.object(S,'RUNTIME',self.runtime),patch.object(S,'writer_lock',lock),\
                patch.object(S,'Controller',return_value=self.controller),\
                patch.object(A,'recover',side_effect=recovery),patch('builtins.print'):
            S.main()
        self.assertEqual(events,['acquired','released'])
        self.controller.run.assert_not_called()

    def test_pause_during_backoff_exits_before_another_recovery(self):
        self.controller.state['status'] = 'PAUSED_LEAD'
        recovery = Mock(return_value={'action': 'BACKOFF', 'delay_seconds': 3600})

        def sleep(_seconds):
            (self.runtime/'pause.request').write_text('{}', encoding='utf-8')

        self.supervise(recovery, sleep)
        self.assertEqual(recovery.call_count, 1)
        self.assertEqual(self.controller.state['status'], 'PAUSED_USER')
        self.controller.run.assert_not_called()

    def test_resume_without_a_state_change_fails_closed(self):
        self.controller.state['status'] = 'PAUSED_LEAD'
        before = copy.deepcopy(self.controller.state)
        recovery = Mock(return_value={'action': 'RESUME'})
        with self.assertRaisesRegex(RuntimeError, 'without a reconciled checkpoint'):
            self.supervise(recovery)
        self.controller.run.assert_not_called()
        self.assertEqual(self.controller.state, before)

    def test_pause_arriving_during_recovery_blocks_requested_resume(self):
        self.controller.state['status'] = 'ENGINEERING_BLOCKED'

        def repair(controller):
            controller.state['status'] = 'RUNNING'
            (self.runtime/'cancel.request').write_text('{}', encoding='utf-8')
            return {'action': 'RESUME'}

        self.supervise(repair)
        self.controller.run.assert_not_called()
        self.assertEqual(self.controller.state['status'], 'PAUSED_USER')

    def test_unresolved_inflight_is_given_to_recovery_without_controller_rerun(self):
        self.controller.state['inflight_call'] = {'path': 'saved-call'}
        recovery = Mock(return_value={'action': 'STOP', 'reason': 'unreconciled invocation'})
        self.supervise(recovery)
        recovery.assert_called_once_with(self.controller)
        self.controller.run.assert_not_called()

    def test_human_permission_stop_is_not_cleared_or_retried(self):
        self.controller.state.update(status='HUMAN_ACTION_REQUIRED', error='protected data denied')
        before = copy.deepcopy(self.controller.state)
        recovery = Mock(return_value={'action': 'STOP', 'reason': 'human permission'})
        self.supervise(recovery)
        self.assertEqual(self.controller.state, before)
        self.controller.run.assert_not_called()

    def test_unknown_controller_exception_is_not_silently_retried(self):
        self.controller.run.side_effect = ValueError('unclassified safety pin mismatch')
        recovery = Mock()
        with self.assertRaisesRegex(ValueError, 'safety pin mismatch'):
            self.supervise(recovery)
        recovery.assert_not_called()
        self.controller.run.assert_called_once_with(resume=True)

    def test_one_writer_lock_surrounds_construction_and_entire_supervision(self):
        events = []

        @contextmanager
        def lock(path):
            self.assertEqual(path, self.runtime/'writer.lock')
            events.append('acquired')
            yield
            events.append('released')

        def factory():
            self.assertEqual(events, ['acquired'])
            events.append('constructed')
            return self.controller

        def run(controller):
            self.assertEqual(events, ['acquired', 'constructed'])
            self.assertIs(controller, self.controller)
            events.append('supervised')
            return controller.state

        with patch.object(S, 'RUNTIME', self.runtime), patch.object(S, 'writer_lock', lock), \
                patch.object(S, 'Controller', factory), patch.object(S, 'run', run), \
                patch('builtins.print'):
            S.main()
        self.assertEqual(events, ['acquired', 'constructed', 'supervised', 'released'])

    def test_duplicate_writer_fails_before_controller_construction(self):
        with patch.object(S, 'writer_lock', side_effect=RuntimeError('One controller writer already active')), \
                patch.object(S, 'Controller') as constructor:
            with self.assertRaisesRegex(RuntimeError, 'writer already active'):
                S.main()
            constructor.assert_not_called()

    def test_autonomous_review_required_overrides_dispatch_bypass_flags_and_empty_pending(self):
        flags = ('review_first_plan_pending_execution', 'worker_route_migration_pending_execution',
                 'worker_route_adapter_repair_pending_execution')
        for pending in (None, {'job_id': 'SYNTHETIC_PENDING'}):
            for enabled_flags in ((flags[0],), (flags[1],), (flags[2],), flags):
                with self.subTest(pending=pending, bypass=enabled_flags):
                    controller = C.Controller.__new__(C.Controller)
                    controller.state = dict(status='ENGINEERING_BLOCKED', pending_job=pending,
                        model_calls=0, bootstrap_invocations=0, prior_reserved_calls=0,
                        autonomous_recovery={'review_required': True},
                        **{key: True for key in enabled_flags})
                    controller.profile = {'max_calls': 1000}
                    controller.save = Mock()
                    controller.check_budget = Mock()
                    controller.worker = Mock(side_effect=AssertionError('No worker before fresh Lead'))
                    controller.lead = Mock(return_value={'action': 'PAUSE', 'next_job': None,
                        'worker_dispatch_ready': False, 'pause_category': 'PAUSED_LEAD'})
                    with patch.object(C, 'RUNTIME', self.runtime), \
                            patch.object(Skills, 'maybe_review'):
                        result = controller.run(resume=True)
                    controller.lead.assert_called_once_with()
                    controller.worker.assert_not_called()
                    self.assertEqual(result['status'], 'PAUSED_LEAD')


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import hashlib
import errno

from tools.quant_research_loop import desktop_capture as guard


class DesktopCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'research/campaigns/PAYOUT_V1/runtime'
        self.runtime.mkdir(parents=True)
        self.ref = self.root / '.git/refs/codex/turn-diffs/captures/123/turn/base'
        self.ref.parent.mkdir(parents=True)
        self.receipt = self.runtime / 'wait.json'
        self.c = SimpleNamespace(state={'status': 'RUNNING', 'model_calls': 548,
                                       'runtime_policy': {'inactivity_stall_seconds': 2}},
                                 save=lambda: None)

    def test_empty_capture_does_not_write_or_reserve(self):
        guard.wait_before_worker(self.c, self.receipt, root=self.root)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.c.state['model_calls'], 548)

    def test_capture_must_clear_before_return(self):
        self.ref.write_text('a' * 40)
        with patch.object(guard.time, 'sleep', side_effect=lambda _: self.ref.unlink()) as sleep:
            guard.wait_before_worker(self.c, self.receipt, root=self.root)
        sleep.assert_called_once_with(1)
        r = json.loads(self.receipt.read_text())
        self.assertEqual(r['status'], 'CLEARED')
        self.assertEqual(r['scope_exemptions'], [])
        self.assertEqual(r['worker_attempts_reserved'], 0)
        self.assertEqual(self.c.state['model_calls'], 548)

    def test_confirmed_absent_capture_directory_is_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.git').mkdir()
            self.assertEqual(guard.active_captures(root), [])

    def test_scan_errors_stop_before_call_reservation(self):
        for error in (PermissionError('denied'), OSError(errno.EIO, 'disk error'),
                      FileNotFoundError('disappeared during scan')):
            with self.subTest(error=type(error).__name__):
                with patch.object(guard.os, 'scandir', side_effect=error):
                    with self.assertRaisesRegex(RuntimeError, 'HOST_CAPTURE_SCAN_FAILED'):
                        guard.wait_before_worker(self.c, self.receipt, root=self.root)
                self.assertEqual(self.c.state['model_calls'], 548)
                self.assertFalse(self.receipt.exists())

    def test_nested_scan_failure_cannot_return_a_partial_inventory(self):
        self.ref.write_text('a' * 40)
        real_scandir = guard.os.scandir
        def scandir(path):
            if Path(path) == self.ref.parent:
                raise PermissionError('nested capture denied')
            return real_scandir(path)
        with patch.object(guard.os, 'scandir', side_effect=scandir):
            with self.assertRaisesRegex(RuntimeError, 'HOST_CAPTURE_SCAN_FAILED'):
                guard.active_captures(self.root)

    def test_stat_failure_and_non_directory_are_not_absence(self):
        directory = self.root / '.git/refs/codex/turn-diffs/captures'
        real_stat = Path.stat
        def stat(path, **kwargs):
            if path == directory and kwargs.get('follow_symlinks', True):
                raise PermissionError('stat denied')
            return real_stat(path, **kwargs)
        with patch.object(Path, 'stat', stat):
            with self.assertRaisesRegex(RuntimeError, 'HOST_CAPTURE_SCAN_FAILED'):
                guard.active_captures(self.root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / '.git/refs/codex/turn-diffs/captures'
            target.parent.mkdir(parents=True)
            target.write_text('unexpected file')
            with self.assertRaisesRegex(RuntimeError, 'HOST_CAPTURE_SCAN_FAILED'):
                guard.active_captures(root)

    def test_nested_linked_metadata_still_fails_closed(self):
        with patch.object(Path, 'is_junction', lambda path: path == self.ref.parent):
            with self.assertRaisesRegex(RuntimeError, 'linked Desktop capture metadata'):
                guard.active_captures(self.root)

    def test_pause_and_cancel_prevent_dispatch_even_without_capture(self):
        for name in ('pause.request', 'cancel.request'):
            with self.subTest(name=name):
                marker = self.runtime / name
                marker.touch()
                with self.assertRaisesRegex(RuntimeError, 'HOST_CAPTURE_WAIT_CANCELLED'):
                    guard.wait_before_worker(self.c, self.receipt, root=self.root)
                self.assertEqual(self.c.state['status'], 'PAUSED_USER')
                marker.unlink()

    def test_stale_capture_stops_without_refund_or_new_call(self):
        self.ref.write_text('a' * 40)
        with patch.object(guard.time, 'monotonic', side_effect=[0, 3, 3]):
            with self.assertRaisesRegex(RuntimeError, 'HOST_CAPTURE_WAIT_STALLED'):
                guard.wait_before_worker(self.c, self.receipt, root=self.root)
        self.assertEqual(json.loads(self.receipt.read_text())['status'], 'STALLED')
        self.assertEqual(self.c.state['model_calls'], 548)
        self.assertEqual(self.c.state['status'], 'ENGINEERING_BLOCKED')

    def test_linked_checkout_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.git').write_text('gitdir: elsewhere')
            with self.assertRaisesRegex(RuntimeError, 'HOST_CAPTURE_GUARD_UNSUPPORTED'):
                guard.active_captures(root)

    def test_guard_precedes_episode_reservation_in_existing_route(self):
        source = (Path(__file__).parent / 'quant_research_loop/generic_worker.py').read_text()
        start = source.index('wait_before_worker(controller,')
        self.assertLess(start, source.index('reserve_episode(s,job,', start))
        self.assertLess(start, source.index('receipt,raw=controller.model_call', start))

    def test_recovery_context_requires_pinned_local_evidence(self):
        base = self.root / 'research/campaigns/PAYOUT_V1/revisions/R23'
        base.mkdir(parents=True)
        record = {}
        for key in ('authorization', 'reconciliation'):
            p = base / (key + '.json')
            p.write_text(json.dumps({'key': key}))
            record[key] = {'path': p.relative_to(self.root).as_posix(),
                           'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
        with patch.object(guard, 'ROOT', self.root):
            self.assertIsNone(guard.recovery_context({}))
            value = guard.recovery_context({'operator_metadata_recovery': record})
            self.assertEqual(value['authorization'], {'key': 'authorization'})
            (base / 'authorization.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError, 'evidence changed'):
                guard.recovery_context({'operator_metadata_recovery': record})

    def test_literal_assigned_patch_direct_output_still_rejects_extra_actions(self):
        from tools.quant_research_loop.worker_scope import literal_patch_wrapper
        literal = '*** Begin Patch\n*** Update File: work/analysis.py\n@@\n-old\n+new\n*** End Patch'
        wrapper = 'const patch = ' + json.dumps(literal) + ';\ntext(await tools.apply_patch(patch));'
        self.assertEqual(literal_patch_wrapper(wrapper), literal)
        for invalid in (wrapper + 'text(await tools.exec_command({cmd:"git status"}));',
                        wrapper.replace('apply_patch(patch)', 'apply_patch(patch + "x")'),
                        wrapper.replace('apply_patch(patch)', 'apply_patch(other)'),
                        wrapper.replace('tools.apply_patch', 'tools["apply_patch"]')):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    literal_patch_wrapper(invalid)


if __name__ == '__main__':
    unittest.main()

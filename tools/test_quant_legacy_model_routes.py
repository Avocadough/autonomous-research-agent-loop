"""Offline route checks: current fresh calls and unchanged historical evidence."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.quant_research_loop import continuation as C
from tools.quant_research_loop import gate_zero as G
from tools.quant_research_loop import prepare as P
from tools.quant_research_loop import recovery as R


def paused(evidence_id, ref, **extra):
    return dict(campaign_id='PAYOUT_V1', evidence_id=evidence_id, action='PAUSE',
                rationale=evidence_id, artifact_refs=[ref], next_job=None, worker_dispatch_ready=False,
                missing_information=[], pause_category='PAUSED_LEAD', **extra)


class LegacyModelRouteTests(unittest.TestCase):
    def assert_new_route(self, argv):
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-6.1-sol')
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertIn('forced_login_method="chatgpt"', argv)
        self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
        self.assertNotIn('gpt-6-astra', argv)

    def test_gate_fresh_command_uses_new_route_with_existing_permissions(self):
        with patch.object(G, 'native_command', return_value=['codex-fixture']):
            self.assert_new_route(G.lead_command('schema.json', 'decision.json'))
        self.assertEqual(G.lead_route()['configured_model'], 'gpt-6.1-sol')
        self.assertEqual(G.lead_route()['reasoning'], 'high')

    def test_old_gate_resume_stops_without_relabeling_or_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / 'bootstrap'
            attempt = base / 'old_gate'
            attempt.mkdir(parents=True)
            G.write(attempt / 'state.json', {'lead': 'gpt-6-astra', 'reasoning': 'medium',
                                          'model_calls': 2, 'status': 'SETUP_BLOCKED'})
            G.write(base / 'latest_gate.json', {'path': 'bootstrap/old_gate'})
            before = {p: p.read_bytes() for p in (attempt / 'state.json', base / 'latest_gate.json')}
            with patch.object(G, 'ROOT', root), patch.object(G, 'BASE', base), \
                    patch.object(G.sys, 'argv', ['gate_zero', '--resume']), \
                    patch.object(G, 'invoke', side_effect=AssertionError('No model calls')), \
                    patch.object(G.subprocess, 'run', side_effect=AssertionError('No process calls')):
                with self.assertRaisesRegex(RuntimeError, 'LEAD_ROUTE_RECONCILIATION_REQUIRED'):
                    G.main()
            self.assertEqual({p: p.read_bytes() for p in before}, before)
            self.assertFalse((base / 'gate.lock').exists())

    def test_continuation_fresh_call_records_new_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / 'continuation'
            base.mkdir()
            obj = C.Continuation.__new__(C.Continuation)
            seen = []

            def model_call(argv, prompt, folder):
                seen.append(argv)
                ref = (folder.parent / 'evidence.json').relative_to(root).as_posix()
                G.write(folder.parent / 'decision.json', paused(C.JOB, ref))
                return {'exit_code': 0}, ''

            with patch.object(C, 'ROOT', root), patch.object(C, 'BASE', base), \
                    patch.object(obj, 'model_call', side_effect=model_call), \
                    patch.object(G, 'native_command', return_value=['codex-fixture']):
                obj.decide('lead_after', {'evidence_id': C.JOB})
                original = (base / 'lead_after/accepted.json').read_bytes()
                with patch.object(obj, 'model_call', side_effect=AssertionError('No redispatch')):
                    obj.decide('lead_after', {'evidence_id': C.JOB})
                self.assertEqual((base / 'lead_after/accepted.json').read_bytes(), original)
            self.assertEqual(len(seen), 1)
            self.assert_new_route(seen[0])
            self.assertEqual(C.read(base / 'lead_after/lead_route.json'), G.lead_route())

    def test_recovery_fresh_call_metadata_uses_route_not_old_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / 'recovery'
            base.mkdir()
            G.write(base / 'state.json', {'lead_calls': 0, 'active_child_seconds': 0})
            obj = R.Recovery.__new__(R.Recovery)
            obj.profile = {'lead': 'gpt-6-astra', 'reasoning': 'medium'}
            seen = []

            def model_call(argv, prompt, folder):
                seen.append(argv)
                ref = (folder.parent / 'evidence.json').relative_to(root).as_posix()
                G.write(folder.parent / 'decision.json', paused(R.JOB, ref, result_sha256='fixture-sha'))
                raw = '\n'.join(json.dumps(event) for event in [
                    {'type': 'thread.started', 'thread_id': 'fixture-session'},
                    {'type': 'turn.completed'}])
                return {'exit_code': 0, 'elapsed_s': 1}, raw

            with patch.object(R, 'ROOT', root), patch.object(R, 'BASE', base), \
                    patch.object(obj, 'check_budget'), \
                    patch.object(R.Controller, 'model_call', side_effect=model_call), \
                    patch.object(G, 'native_command', return_value=['codex-fixture']):
                obj.review_call({'validation': {'result_sha256': 'fixture-sha'}})
            self.assert_new_route(seen[0])
            session = R.read(base / 'lead_review/session.json')
            self.assertEqual(session['configured_model'], 'gpt-6.1-sol')
            self.assertEqual(session['reasoning'], 'high')
            self.assertEqual(obj.profile, {'lead': 'gpt-6-astra', 'reasoning': 'medium'})

    def test_partial_historical_recovery_stops_without_rewriting_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / 'recovery'
            folder = base / 'lead_review'
            call = folder / 'call'
            call.mkdir(parents=True)
            G.write(call / 'request.json', {'argv': ['codex', '-m', 'gpt-6-astra',
                                                   '-c', 'model_reasoning_effort="medium"']})
            G.write(call / 'receipt.json', {'exit_code': 0})
            G.write(folder / 'decision.json', {'historical': True})
            before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
            obj = R.Recovery.__new__(R.Recovery)
            with patch.object(R, 'ROOT', root), patch.object(R, 'BASE', base), \
                    patch.object(R.Controller, 'model_call', side_effect=AssertionError('No redispatch')):
                with self.assertRaisesRegex(RuntimeError, 'LEAD_ROUTE_RECONCILIATION_REQUIRED'):
                    obj.review_call({})
            self.assertEqual({p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}, before)

    def test_prepare_new_default_and_retains_existing_frozen_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / 'bootstrap'
            camp = root / 'campaign'
            base.mkdir()
            camp.mkdir()
            G.write(base / 'latest_gate.json', {'status': 'GATE_ZERO_PASSED'})
            G.write(camp / 'TOPSTEP_RULES_50K.json', {'retrieval': {'snapshots': {}}})
            with patch.object(P, 'ROOT', root), patch.object(P, 'BASE', base), patch.object(P, 'CAMP', camp), \
                    patch.object(P, 'digest', return_value='fixture-sha'), \
                    patch.object(P.requests, 'get', side_effect=AssertionError('No network')), \
                    contextlib.redirect_stdout(io.StringIO()):
                P.main()
                profile_path = camp / 'runtime/profile.json'
                profile = json.loads(profile_path.read_text(encoding='utf-8'))
                self.assertEqual((profile['lead'], profile['reasoning']), ('gpt-6.1-sol', 'high'))
                historical = {'lead': 'gpt-6-astra', 'reasoning': 'medium', 'historical': True}
                G.write(profile_path, historical)
                before = profile_path.read_bytes()
                P.main()
                self.assertEqual(profile_path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

"""Fresh handoffs retain frozen authority without replaying implementation recipes."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.quant_research_loop import generic_worker as W, worker_scope as S
from tools.quant_research_loop.gate_zero import write
from tools.quant_research_loop.prepare import digest


class LeanPromptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / 'research/campaigns/PAYOUT_V1/runtime/jobs/LEAN_FIXTURE'
        self.folder.mkdir(parents=True)
        self.source = self.folder / 'work/analysis.py'
        self.contract = self.folder / 'job.json'
        write(self.root / 'fixture.json', {'value': 7})
        paths = [(self.folder / 'work' / name).relative_to(self.root).as_posix()
                 for name in ('analysis.py', 'result.json')]
        self.job = {
            'job_id': 'LEAN_FIXTURE', 'job_class': 'DIAGNOSTIC', 'candidate_id': 'FIXTURE',
            'objective': 'Return independent synthetic arithmetic evidence; no market rows.',
            'inputs': [{'alias': 'fixture', 'path': 'fixture.json',
                        'sha256': digest(self.root / 'fixture.json'), 'kind': 'json',
                        'partition': 'SYNTHETIC'}],
            'allowed_file_scope': paths, 'required_outputs': paths,
            'verification': {'required_keys': ['records'], 'min_records': 1},
            'permissions': {'network': 'none', 'spend_usd': 0, 'live': False},
        }
        write(self.contract, self.job)
        self.profile = {'worker': 'gpt-6.1-sol', 'worker_provider': 'codex',
                        'worker_variant': 'medium', 'model_route_authority': 'R25'}
        self.state = {'worker_command_policy': {'version': S.VERSION}}
        for context in (patch.object(W, 'ROOT', self.root), patch.object(S, 'ROOT', self.root),
                        patch.object(W, 'codex_route', return_value=True)):
            context.start()
            self.addCleanup(context.stop)

    def prompt(self, failure=''):
        return W.build_worker_prompt(self.profile, self.job, self.state, 'EXACT FIXED ENTRY', failure)

    def test_frozen_file_and_input_pins_survive_without_inline_contract_or_recipes(self):
        before = self.contract.read_bytes()
        original = copy.deepcopy(self.job)
        recipe = 'INLINE_RECIPE_SENTINEL\ndef replacement_kernel():\n    return "fabricated"'
        self.state['lead_recovery_authorizations'] = {'LEAN_FIXTURE': {
            'decision_index': 243, 'episode': {'changed_approach': recipe, 'max_attempts': 1}}}
        prompt = self.prompt('RAW_LOG_SENTINEL' * 4000)
        self.assertIn(str(self.contract), prompt)
        self.assertIn(digest(self.contract), prompt)
        self.assertIn(self.job['inputs'][0]['sha256'], prompt)
        self.assertIn('gpt-6.1-sol at medium reasoning', prompt)
        self.assertIn('RECOVERY HANDOFF: Lead decision 243', prompt)
        self.assertIn('Fixed entry invocation limit for this call: 1.', prompt)
        self.assertNotIn(recipe, prompt)
        self.assertNotIn('RAW_LOG_SENTINEL', prompt)
        self.assertNotIn('JOB: {', prompt)
        self.assertNotIn("@'\nSOURCE", prompt)
        self.assertLess(len(prompt), 10000)
        self.assertEqual(self.contract.read_bytes(), before)
        self.assertEqual(self.job, original)
        self.assertEqual(json.loads(before), original)

    def test_no_fabricated_pin_for_missing_or_changed_frozen_job(self):
        self.contract.unlink()
        with self.assertRaisesRegex(ValueError, 'Frozen job file'):
            self.prompt()
        changed = dict(self.job, permissions={'network': 'none', 'spend_usd': 1, 'live': True})
        write(self.contract, changed)
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.prompt()

    def test_r25_outcome_excerpt_keeps_full_numeric_and_acceptance_contract_pinned(self):
        self.profile['model_route_authority'] = {'revision_id': 'R25_LEAN_PAYOUT_SOL61_MEDIUM_20261003'}
        self.job['objective'] = ('Prove the two chronology defects using independent synthetic witnesses.'
                                 + '\n\n' + 'HISTORICAL_CODE_RECIPE_SENTINEL ' * 200)
        self.job['verification']['required_keys'].append('independent_expected')
        self.job['risk_ceiling_usd'] = 200
        write(self.contract, self.job)
        before = self.contract.read_bytes()
        prompt = self.prompt()
        self.assertIn('RESEARCH OUTCOME: Prove the two chronology defects', prompt)
        self.assertNotIn('HISTORICAL_CODE_RECIPE_SENTINEL', prompt)
        self.assertIn('historical coding recipes and role labels are advisory', prompt)
        self.assertIn('numeric, financial, behavioral acceptance, scope and permission constraints; none are relaxed', prompt)
        self.assertIn(digest(self.contract), prompt)
        self.assertEqual(self.contract.read_bytes(), before)
        self.assertEqual(json.loads(before)['risk_ceiling_usd'], 200)
        self.assertIn('independent_expected', json.loads(before)['verification']['required_keys'])

    def test_literal_policy_remains_copyable_and_rejects_expanded_authority(self):
        prompt = self.prompt()
        for command in S.inspections(self.job):
            self.assertIn('\n' + command + '\n', prompt)
            self.assertEqual(S.command_kind(command, self.job, 'EXACT FIXED ENTRY', []), 'pinned_read')
        for command in (
            "Get-Content -Raw -LiteralPath 'dataset/test_reserved.csv'",
            'Set-Location C:\\; EXACT FIXED ENTRY',
            '$env:PYTHONPATH="different"; EXACT FIXED ENTRY',
            "@'\n{}\n'@ | Set-Content -LiteralPath 'result.json' -Encoding utf8",
        ):
            with self.subTest(command=command), self.assertRaisesRegex(RuntimeError, 'outside exact'):
                S.command_kind(command, self.job, 'EXACT FIXED ENTRY', [])
        for restriction in ('No network, child agents/models', 'no filesystem/process/network access',
                            'Do not set sandbox/permission overrides.', 'never claim an edit occurred without its receipt'):
            self.assertIn(restriction, prompt)


if __name__ == '__main__':
    unittest.main()

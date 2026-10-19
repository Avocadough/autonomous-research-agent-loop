import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.quant_research_loop import runtime_policy as RP


class R05RuntimePolicyTests(unittest.TestCase):
    def _write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2), encoding='utf-8')

    def test_migration_is_idempotent_and_keeps_160_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_path = root / RP.ADDENDUM_160
            old_json_path = root / RP.ADDENDUM_160_JSON
            old_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.write_text('old 160 authority', encoding='utf-8')
            old_json = {
                'approved': True,
                'allow_cumulative_runtime_hard_cap': False,
                'cumulative_runtime_is_reporting_only': True,
                'global_max_calls': 160,
                'global_max_seconds_reported': 21600,
                'shorten_new_timeouts_by_cumulative_remaining': False,
                'per_call_timeouts_active': True,
                'per_job_timeouts_active': True,
                'max_repairs_per_same_blocker': 2,
                'spend_usd': 0,
                'live': 'DISABLED',
            }
            self._write_json(old_json_path, old_json)
            r05 = root / RP.AUTHORIZATION_1000
            r05_json = root / RP.AUTHORIZATION_1000_JSON
            r05.parent.mkdir(parents=True, exist_ok=True)
            r05.write_text('r05 authorization', encoding='utf-8')
            base = root / RP.BASE_AUTH
            base.parent.mkdir(parents=True, exist_ok=True)
            base.write_text('base authority', encoding='utf-8')
            r05_doc = {
                'approved': True,
                'allow_cumulative_runtime_hard_cap': False,
                'cumulative_runtime_is_reporting_only': True,
                'global_max_calls': 1000,
                'global_max_seconds_reported': 21600,
                'shorten_new_timeouts_by_cumulative_remaining': False,
                'per_call_timeouts_active': True,
                'per_job_timeouts_active': True,
                'max_repairs_per_same_blocker': 2,
                'spend_usd': 0,
                'live': 'DISABLED',
                'campaign_stop_cap': 320,
                'lead_model': 'gpt-6-astra',
                'lead_reasoning': 'xhigh',
                'worker_model': 'opencode/muse-spark-1.3-contributor-free',
                'worker_variant': 'xhigh',
            }
            r05_doc['source_sha256'] = hashlib.sha256(r05.read_bytes()).hexdigest()
            self._write_json(r05_json, r05_doc)
            old_ref = {
                'revision_id': RP.REVISION_160,
                'path': RP.ADDENDUM_160.as_posix(),
                'sha256': hashlib.sha256(old_path.read_bytes()).hexdigest(),
                'json_path': RP.ADDENDUM_160_JSON.as_posix(),
                'json_sha256': hashlib.sha256(old_json_path.read_bytes()).hexdigest(),
                'base_authorization_path': RP.BASE_AUTH.as_posix(),
                'base_authorization_sha256': hashlib.sha256(base.read_bytes()).hexdigest(),
            }
            state = {
                'generic_authority': {'path': RP.BASE_AUTH.as_posix(), 'sha256': old_ref['base_authorization_sha256']},
                'runtime_policy_revision': RP.REVISION_160,
                'runtime_policy': {
                    'authorization_revision': old_ref,
                    'allow_cumulative_runtime_hard_cap_cancelled': True,
                    'global_max_calls': 160,
                },
                'status': 'RESOURCE_LIMIT', 'error': 'old stop',
                'latest_budget_receipt': 'old-receipt.json',
                'model_calls': 143, 'bootstrap_invocations': 6, 'prior_reserved_calls': 10,
            }
            profile = {'max_calls': 80, 'max_active_seconds': 21600}
            with patch.object(RP, 'ROOT', root):
                migrated, changed = RP.migrate_state(state, profile)
                self.assertTrue(changed)
                self.assertEqual(migrated['runtime_policy_revision'], RP.REVISION_1000)
                self.assertEqual(migrated['runtime_policy']['global_max_calls'], 1000)
                self.assertEqual(migrated['runtime_policy']['campaign_stop_cap'], 320)
                self.assertEqual(migrated['runtime_budget_migration']['from_calls'], 160)
                self.assertEqual(migrated['runtime_budget_migration']['used_calls_at_migration'], 159)
                overlaid = RP.apply_runtime_budget(profile, migrated)
                self.assertEqual(overlaid['max_calls'], 1000)
                self.assertEqual(overlaid['campaign_stop_calls'], 320)
                self.assertEqual(overlaid['reasoning'], 'xhigh')
                self.assertEqual(RP.effective_call_cap(overlaid), 320)
                self.assertEqual(RP.remaining_call_units(migrated, overlaid), 161)
                self.assertEqual(RP.global_remaining_call_units(migrated, overlaid), 841)
                again, changed_again = RP.migrate_state(migrated, overlaid)
                self.assertFalse(changed_again)
                self.assertEqual(len(again['authorization_history']), 1)
                self.assertEqual(again['model_calls'], 143)


if __name__ == '__main__':
    unittest.main()

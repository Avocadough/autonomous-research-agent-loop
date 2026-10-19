import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.quant_research_loop import runtime_policy as RP


class R06CampaignCapTests(unittest.TestCase):
    def _write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2), encoding='utf-8')

    def test_r05_to_r06_preserves_counters_and_raises_only_campaign_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / RP.BASE_AUTH
            base.parent.mkdir(parents=True, exist_ok=True)
            base.write_text('base authority', encoding='utf-8')

            r05 = root / RP.AUTHORIZATION_1000
            r05_json = root / RP.AUTHORIZATION_1000_JSON
            r05.parent.mkdir(parents=True, exist_ok=True)
            r05.write_text('r05 authorization', encoding='utf-8')
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
            self._write_json(r05_json, r05_doc)

            r06 = root / RP.AUTHORIZATION_CAMPAIGN_1000
            r06_json = root / RP.AUTHORIZATION_CAMPAIGN_1000_JSON
            r06.parent.mkdir(parents=True, exist_ok=True)
            r06.write_text('r06 authorization', encoding='utf-8')
            r06_doc = dict(r05_doc, campaign_stop_cap=1000,
                           review_first_required=True,
                           review_first_worker_dispatch_allowed=False)
            self._write_json(r06_json, r06_doc)

            r05_ref = {
                'revision_id': RP.REVISION_1000,
                'path': RP.AUTHORIZATION_1000.as_posix(),
                'sha256': hashlib.sha256(r05.read_bytes()).hexdigest(),
                'json_path': RP.AUTHORIZATION_1000_JSON.as_posix(),
                'json_sha256': hashlib.sha256(r05_json.read_bytes()).hexdigest(),
                'base_authorization_path': RP.BASE_AUTH.as_posix(),
                'base_authorization_sha256': hashlib.sha256(base.read_bytes()).hexdigest(),
            }
            state = {
                'generic_authority': {'path': RP.BASE_AUTH.as_posix(),
                                      'sha256': r05_ref['base_authorization_sha256']},
                'runtime_policy_revision': RP.REVISION_1000,
                'runtime_policy': {
                    'authorization_revision': r05_ref,
                    'allow_cumulative_runtime_hard_cap_cancelled': True,
                    'global_max_calls': 1000,
                    'campaign_stop_cap': 320,
                },
                'status': 'ENGINEERING_BLOCKED',
                'error': 'HUMAN_ACTION_REQUIRED: worker runtime RATE_LIMIT',
                'latest_budget_receipt': 'r05-receipt.json',
                'model_calls': 264,
                'bootstrap_invocations': 6,
                'prior_reserved_calls': 10,
            }
            profile = {'max_calls': 80, 'max_active_seconds': 21600}

            with patch.object(RP, 'ROOT', root):
                migrated, changed = RP.migrate_state(state, profile)
                self.assertTrue(changed)
                self.assertEqual(migrated['runtime_policy_revision'], RP.REVISION_CAMPAIGN_1000)
                self.assertEqual(migrated['runtime_policy']['campaign_stop_cap'], 1000)
                self.assertEqual(migrated['runtime_budget_migration']['from_campaign_stop_cap'], 320)
                self.assertEqual(migrated['runtime_budget_migration']['to_campaign_stop_cap'], 1000)
                self.assertEqual(migrated['runtime_budget_migration']['used_calls_at_migration'], 280)
                self.assertEqual(migrated['model_calls'], 264)
                overlaid = RP.apply_runtime_budget(profile, migrated)
                self.assertEqual(overlaid['max_calls'], 1000)
                self.assertEqual(overlaid['campaign_stop_calls'], 1000)
                self.assertEqual(RP.remaining_call_units(migrated, overlaid), 720)
                again, changed_again = RP.migrate_state(migrated, overlaid)
                self.assertFalse(changed_again)
                self.assertEqual(len(again['authorization_history']), 1)


if __name__ == '__main__':
    unittest.main()

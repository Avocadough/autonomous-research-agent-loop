"""Validated runtime-policy revision for the existing PAYOUT controller.

The addendum changes one gate only: cumulative accounted seconds no longer
stop the continuation or shorten a new call's explicit timeout. It never
changes the active financial/data/model mandate or the call/trial/repair gates.
"""
from __future__ import annotations
import json
from pathlib import Path
from .gate_zero import ROOT
from .prepare import digest

ADDENDUM = Path('research/campaigns/PAYOUT_V1/continuations/G01_GENERIC_JOBS/AUTHORIZATION_RUNTIME_ADDENDUM_20260921.md')
ADDENDUM_JSON = Path('research/campaigns/PAYOUT_V1/continuations/G01_GENERIC_JOBS/AUTHORIZATION_RUNTIME_ADDENDUM_20260921.json')
# The 2026-09-21 addendum remains immutable history.  The user-authorized
# continuation adds a separate runtime authority for the second 80 units.
ADDENDUM_160 = Path('research/campaigns/PAYOUT_V1/continuations/G01_GENERIC_JOBS/AUTHORIZATION_RUNTIME_ADDENDUM_20260922_160.md')
ADDENDUM_160_JSON = Path('research/campaigns/PAYOUT_V1/continuations/G01_GENERIC_JOBS/AUTHORIZATION_RUNTIME_ADDENDUM_20260922_160.json')
AUTHORIZATION_1000 = Path('research/campaigns/PAYOUT_V1/revisions/R05_TOTAL_CAP1000_DECISION_FIRST_20260922/USER_AUTHORIZATION.txt')
AUTHORIZATION_1000_JSON = Path('research/campaigns/PAYOUT_V1/revisions/R05_TOTAL_CAP1000_DECISION_FIRST_20260922/authorization.json')
AUTHORIZATION_CAMPAIGN_1000 = Path('research/campaigns/PAYOUT_V1/revisions/R06_CAMPAIGN_CAP1000_REVIEW_FIRST_20260923/USER_AUTHORIZATION.txt')
AUTHORIZATION_CAMPAIGN_1000_JSON = Path('research/campaigns/PAYOUT_V1/revisions/R06_CAMPAIGN_CAP1000_REVIEW_FIRST_20260923/authorization.json')
AUTHORIZATION_CODEX_LUNA = Path('research/campaigns/PAYOUT_V1/revisions/R07_CODEX_LUNA_MAX_EXECUTOR_20260923/USER_AUTHORIZATION.txt')
AUTHORIZATION_CODEX_LUNA_JSON = Path('research/campaigns/PAYOUT_V1/revisions/R07_CODEX_LUNA_MAX_EXECUTOR_20260923/authorization.json')
AUTHORIZATION_GPT6_LUNA = Path('research/campaigns/PAYOUT_V1/revisions/R08_CODEX_GPT6_LUNA_MAX_EXECUTOR_20260923/USER_AUTHORIZATION.txt')
AUTHORIZATION_GPT6_LUNA_JSON = Path('research/campaigns/PAYOUT_V1/revisions/R08_CODEX_GPT6_LUNA_MAX_EXECUTOR_20260923/authorization.json')
AUTHORIZATION_UNBOUNDED = Path('research/campaigns/PAYOUT_V1/revisions/R09_UNBOUNDED_ACTIVE_RUNTIME_20260923/USER_AUTHORIZATION.txt')
AUTHORIZATION_UNBOUNDED_JSON = Path('research/campaigns/PAYOUT_V1/revisions/R09_UNBOUNDED_ACTIVE_RUNTIME_20260923/authorization.json')
REVISION_80 = 'G01_RUNTIME_ADDENDUM_20260921'
REVISION_160 = 'G01_RUNTIME_ADDENDUM_20260922_160_CALLS'
REVISION_1000 = 'R05_RUNTIME_ADDENDUM_20260922_1000_CALLS'
REVISION_CAMPAIGN_1000 = 'R06_RUNTIME_ADDENDUM_20260923_CAMPAIGN1000'
REVISION_CODEX_LUNA = 'R07_RUNTIME_ADDENDUM_20260923_CODEX_LUNA_MAX'
REVISION_GPT6_LUNA = 'R08_RUNTIME_ADDENDUM_20260923_GPT6_LUNA_MAX'
REVISION_UNBOUNDED = 'R09_RUNTIME_ADDENDUM_20260923_UNBOUNDED_ACTIVE'
BASE_AUTH = Path('research/campaigns/PAYOUT_V1/continuations/G01_GENERIC_JOBS/AUTHORIZATION.md')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _revision_spec(revision_id):
    if revision_id == REVISION_80:
        return ADDENDUM, ADDENDUM_JSON, 80
    if revision_id == REVISION_160:
        return ADDENDUM_160, ADDENDUM_160_JSON, 160
    if revision_id == REVISION_1000:
        return AUTHORIZATION_1000, AUTHORIZATION_1000_JSON, 1000
    if revision_id == REVISION_CAMPAIGN_1000:
        return AUTHORIZATION_CAMPAIGN_1000, AUTHORIZATION_CAMPAIGN_1000_JSON, 1000
    if revision_id == REVISION_CODEX_LUNA:
        return AUTHORIZATION_CODEX_LUNA, AUTHORIZATION_CODEX_LUNA_JSON, 1000
    if revision_id == REVISION_GPT6_LUNA:
        return AUTHORIZATION_GPT6_LUNA, AUTHORIZATION_GPT6_LUNA_JSON, 1000
    if revision_id == REVISION_UNBOUNDED:
        return AUTHORIZATION_UNBOUNDED, AUTHORIZATION_UNBOUNDED_JSON, 1000
    return None


def _campaign_stop_for_revision(revision_id):
    if revision_id == REVISION_1000:
        return 320
    if revision_id in (REVISION_CAMPAIGN_1000, REVISION_CODEX_LUNA, REVISION_GPT6_LUNA, REVISION_UNBOUNDED):
        return 1000
    return None


def _target_spec():
    """Select the newest immutable authority that is present on disk."""
    if ((ROOT / AUTHORIZATION_UNBOUNDED).exists()
            and (ROOT / AUTHORIZATION_UNBOUNDED_JSON).exists()):
        return (REVISION_UNBOUNDED, AUTHORIZATION_UNBOUNDED,
                AUTHORIZATION_UNBOUNDED_JSON, 1000)
    if ((ROOT / AUTHORIZATION_GPT6_LUNA).exists()
            and (ROOT / AUTHORIZATION_GPT6_LUNA_JSON).exists()):
        return (REVISION_GPT6_LUNA, AUTHORIZATION_GPT6_LUNA,
                AUTHORIZATION_GPT6_LUNA_JSON, 1000)
    if ((ROOT / AUTHORIZATION_CODEX_LUNA).exists()
            and (ROOT / AUTHORIZATION_CODEX_LUNA_JSON).exists()):
        return (REVISION_CODEX_LUNA, AUTHORIZATION_CODEX_LUNA,
                AUTHORIZATION_CODEX_LUNA_JSON, 1000)
    if ((ROOT / AUTHORIZATION_CAMPAIGN_1000).exists()
            and (ROOT / AUTHORIZATION_CAMPAIGN_1000_JSON).exists()):
        return (REVISION_CAMPAIGN_1000, AUTHORIZATION_CAMPAIGN_1000,
                AUTHORIZATION_CAMPAIGN_1000_JSON, 1000)
    if (ROOT / AUTHORIZATION_1000).exists() and (ROOT / AUTHORIZATION_1000_JSON).exists():
        return REVISION_1000, AUTHORIZATION_1000, AUTHORIZATION_1000_JSON, 1000
    if (ROOT / ADDENDUM_160_JSON).exists() and (ROOT / ADDENDUM_160).exists():
        return REVISION_160, ADDENDUM_160, ADDENDUM_160_JSON, 160
    return REVISION_80, ADDENDUM, ADDENDUM_JSON, 80


def _revision_matches(state, profile):
    policy = state.get('runtime_policy') or {}
    revision = policy.get('authorization_revision') or {}
    spec = _revision_spec(revision.get('revision_id'))
    if spec is None:
        return False
    addendum_path, addendum_json_path, expected_calls = spec
    if revision.get('path') != addendum_path.as_posix():
        return False
    if revision.get('sha256') != digest(ROOT / addendum_path):
        return False
    if revision.get('json_path') != addendum_json_path.as_posix():
        return False
    if revision.get('json_sha256') != digest(ROOT / addendum_json_path):
        return False
    if revision.get('base_authorization_path') != BASE_AUTH.as_posix():
        return False
    if revision.get('base_authorization_sha256') != digest(ROOT / BASE_AUTH):
        return False
    try:
        addendum = read(ROOT / addendum_json_path)
    except (OSError, ValueError):
        return False
    if not (addendum.get('approved') is True
            and addendum.get('allow_cumulative_runtime_hard_cap') is False
            and addendum.get('cumulative_runtime_is_reporting_only') is True
            and addendum.get('global_max_calls') == expected_calls
            and addendum.get('global_max_seconds_reported') == profile.get('max_active_seconds')
            and addendum.get('shorten_new_timeouts_by_cumulative_remaining') is False
            and addendum.get('per_call_timeouts_active') is (revision.get('revision_id') != REVISION_UNBOUNDED)
            and addendum.get('per_job_timeouts_active') is (revision.get('revision_id') != REVISION_UNBOUNDED)
            and addendum.get('max_repairs_per_same_blocker') == 2
            and addendum.get('spend_usd') == 0
            and addendum.get('live') == 'DISABLED'):
        return False
    if revision.get('revision_id') == REVISION_UNBOUNDED:
        if (addendum.get('fixed_elapsed_timeouts_active') is not False
                or addendum.get('inactivity_stall_seconds') != 3600
                or addendum.get('cancellation_active') is not True):
            return False
    if revision.get('revision_id') in (REVISION_1000, REVISION_CAMPAIGN_1000, REVISION_CODEX_LUNA, REVISION_GPT6_LUNA, REVISION_UNBOUNDED):
        worker_route = (
            addendum.get('worker_provider'), addendum.get('worker_model'),
            addendum.get('worker_variant'))
        expected_worker_route = (
            ('codex', 'gpt-6-luna', 'max') if revision.get('revision_id') in (REVISION_GPT6_LUNA, REVISION_UNBOUNDED) else
            ('codex', 'gpt-5.6-luna', 'max') if revision.get('revision_id') == REVISION_CODEX_LUNA else
            (None, 'opencode/muse-spark-1.3-contributor-free', 'xhigh'))
        if (addendum.get('campaign_stop_cap') != _campaign_stop_for_revision(revision.get('revision_id'))
                or addendum.get('lead_model') != 'gpt-6-astra'
                or addendum.get('lead_reasoning') != 'xhigh'
                or worker_route != expected_worker_route):
            return False
    if not policy.get('allow_cumulative_runtime_hard_cap_cancelled'):
        return False
    if policy.get('global_max_calls') != expected_calls:
        return False
    if policy.get('global_max_seconds_reported') != profile.get('max_active_seconds'):
        return False
    return True


def cumulative_runtime_cap_bypassed(state, profile):
    """Return true only for an exact persisted, hash-pinned addendum.

    Both the historical 80-unit revision and the current 160-unit revision
    intentionally bypass only the cumulative-time stop.  The profile argument
    is retained for the existing call sites and tests; the call-unit authority
    is validated from the revision itself so an old financial profile hash is
    never rewritten just to migrate runtime budget.
    """
    return _revision_matches(state, profile)


def migrate_state(state, profile):
    """Apply the next hash-pinned runtime authority without resetting state.

    A real repository contains both addenda.  A fixture that only supplies the
    old files continues to exercise the original 80-unit migration path.
    """
    target_id, addendum_path, addendum_json_path, target_calls = _target_spec()
    if cumulative_runtime_cap_bypassed(state, profile):
        # A state already carrying the newest authority is idempotent. Older
        # hash-bound revisions are upgraded in place without resetting any
        # counters or reopening a completed job.
        if state.get('runtime_policy_revision') == target_id:
            return state, False
    # A runtime revision can only be applied to the preserved G01 state. Do
    # not silently authorize an arbitrary fresh state or alter old receipts.
    if state.get('generic_authority', {}).get('path') != BASE_AUTH.as_posix():
        raise ValueError('Runtime addendum requires the preserved G01 authority')
    base_hash = digest(ROOT / BASE_AUTH)
    if state['generic_authority'].get('sha256') != base_hash:
        raise ValueError('Base G01 authorization changed')
    prior_revision = state.get('runtime_policy_revision')
    old_policy = state.get('runtime_policy') or {}
    if prior_revision and _revision_spec(prior_revision) is None:
        raise ValueError('Runtime addendum integrity mismatch')
    if prior_revision and prior_revision != target_id:
        # Validate the immediately previous revision before upgrading; this
        # catches a tampered historical authority instead of treating it as a
        # fresh budget.
        old_ref = old_policy.get('authorization_revision') or {}
        old_spec = _revision_spec(prior_revision)
        if old_spec is None:
            raise ValueError('Previous runtime addendum revision unknown')
        old_path, old_json_path, old_calls = old_spec
        if old_ref.get('revision_id') != prior_revision or old_ref.get('path') != old_path.as_posix():
            raise ValueError('Previous runtime addendum integrity mismatch')
        if old_ref.get('sha256') != digest(ROOT / old_path) or old_ref.get('json_sha256') != digest(ROOT / old_json_path):
            raise ValueError('Previous runtime addendum hash mismatch')
        if old_policy.get('global_max_calls') != old_calls:
            raise ValueError('Previous runtime call authority mismatch')
    addendum = read(ROOT / addendum_json_path)
    if (addendum.get('approved') is not True
            or addendum.get('global_max_calls') != target_calls
            or addendum.get('campaign_stop_cap') != _campaign_stop_for_revision(target_id)):
        raise ValueError('Invalid runtime addendum')
    previous_status = state.get('status')
    previous_error = state.get('error')
    previous_receipt = state.get('latest_budget_receipt')
    if target_id in (REVISION_CODEX_LUNA, REVISION_GPT6_LUNA, REVISION_UNBOUNDED):
        expected_state_hash = addendum.get('previous_state_sha256')
        if prior_revision in (REVISION_CAMPAIGN_1000, REVISION_CODEX_LUNA, REVISION_GPT6_LUNA) and expected_state_hash:
            current_state_hash = __import__('hashlib').sha256(
                json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
            ).hexdigest()
            # The on-disk snapshot is the controlling receipt. JSON encoding
            # can differ in fixtures, so enforce the exact hash only for the
            # real campaign state carrying the expected pending job.
            if (state.get('pending_job', {}).get('job_id') == addendum.get('pending_job_id')
                    and current_state_hash != expected_state_hash):
                from .prepare import digest as _digest
                state_path = ROOT / 'research/campaigns/PAYOUT_V1/runtime/state.json'
                if not state_path.exists() or _digest(state_path) != expected_state_hash:
                    raise ValueError('Route migration previous state hash mismatch')
        snapshot_path = ROOT / (AUTHORIZATION_UNBOUNDED if target_id == REVISION_UNBOUNDED else
                                AUTHORIZATION_GPT6_LUNA if target_id == REVISION_GPT6_LUNA else
                                AUTHORIZATION_CODEX_LUNA).parent / 'state_before.json'
        if not snapshot_path.exists():
            snapshot_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    entry = {
        'revision_id': target_id,
        'path': addendum_path.as_posix(),
        'sha256': digest(ROOT / addendum_path),
        'json_path': addendum_json_path.as_posix(),
        'json_sha256': digest(ROOT / addendum_json_path),
        'base_authorization_path': BASE_AUTH.as_posix(),
        'base_authorization_sha256': base_hash,
        'previous_status': previous_status,
        'previous_error': previous_error,
        'previous_latest_budget_receipt': previous_receipt,
        'previous_revision_id': prior_revision,
        'previous_global_max_calls': (_revision_spec(prior_revision)[2] if prior_revision else None),
        'applied_utc': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
    }
    state.setdefault('authorization_history', []).append(entry)
    state['runtime_policy'] = {
        'revision_id': target_id,
        'authorization_revision': entry,
        'allow_cumulative_runtime_hard_cap_cancelled': True,
        'cumulative_runtime_is_reporting_only': True,
        'global_max_calls': target_calls,
        'campaign_stop_cap': addendum.get('campaign_stop_cap'),
        'global_max_seconds_reported': addendum.get('global_max_seconds_reported', profile.get('max_active_seconds')),
        'prior_status_preserved': 'PAUSED_BUDGET',
        'prior_budget_receipt': previous_receipt,
        'per_call_timeout_mode': 'no_fixed_elapsed_cap_with_inactivity_guard' if target_id == REVISION_UNBOUNDED else 'explicit_argument_only',
        'per_job_timeout_mode': 'no_fixed_elapsed_cap_with_inactivity_guard' if target_id == REVISION_UNBOUNDED else 'existing_runner_limits',
        'inactivity_stall_seconds':addendum.get('inactivity_stall_seconds') if target_id == REVISION_UNBOUNDED else None,
        'max_repairs_per_same_blocker': 2,
        'previous_runtime_policy_revision': prior_revision,
    }
    history = state.get('status_history', [])
    history.append({
        'status': previous_status,
        'error': previous_error,
        'receipt': previous_receipt,
        'preserved_by_revision': target_id,
        'previous_runtime_policy_revision': prior_revision,
    })
    state['status_history'] = history
    state['runtime_policy_revision'] = target_id
    # Keep the terminal status and old error as a durable checkpoint.  The
    # Controller explicitly permits resume only when the 160-unit revision is
    # pinned and there are units left; it never resets this history.
    if state.get('runtime_budget_migration'):
        state.setdefault('runtime_budget_migration_history', []).append(state['runtime_budget_migration'])
    state['runtime_budget_migration'] = {
        'from_calls': (_revision_spec(prior_revision)[2] if prior_revision else None),
        'to_calls': target_calls,
        'from_campaign_stop_cap': old_policy.get('campaign_stop_cap') if prior_revision else None,
        'to_campaign_stop_cap': addendum.get('campaign_stop_cap'),
        'campaign_stop_cap': addendum.get('campaign_stop_cap'),
        'used_calls_at_migration': state.get('model_calls', 0) + state.get('bootstrap_invocations', 0) + state.get('prior_reserved_calls', 0),
        'cumulative_runtime_hard_cap_active': False,
        'old_stop_preserved': True,
    }
    if target_id in (REVISION_CODEX_LUNA, REVISION_GPT6_LUNA):
        pending_id = (state.get('pending_job') or {}).get('job_id')
        if state.get('worker_route_migration'):
            state.setdefault('worker_route_migration_history', []).append(state['worker_route_migration'])
        state['worker_route_migration'] = {
            'from': ({'provider': 'codex', 'model': 'gpt-5.6-luna', 'variant': 'max'}
                     if target_id == REVISION_GPT6_LUNA else
                     {'provider': 'opencode', 'model': 'opencode/muse-spark-1.3-contributor-free', 'variant': 'xhigh'}),
            'to': {'provider': 'codex', 'model': 'gpt-6-luna' if target_id == REVISION_GPT6_LUNA else 'gpt-5.6-luna',
                   'variant': 'max'},
            'pending_job_id': pending_id,
            'failed_muse_receipt_preserved': True,
            'retry_is_independent_trial': False,
            'used_calls_at_migration': state['runtime_budget_migration']['used_calls_at_migration'],
        }
        if pending_id == addendum.get('pending_job_id'):
            state['worker_route_migration_pending_execution'] = True
    return state, True


def apply_runtime_budget(profile, state):
    """Overlay only the runtime call cap; leave the financial profile pinned."""
    revision = state.get('runtime_policy_revision')
    if revision not in (REVISION_160, REVISION_1000, REVISION_CAMPAIGN_1000, REVISION_CODEX_LUNA, REVISION_GPT6_LUNA, REVISION_UNBOUNDED):
        return profile
    out = json.loads(json.dumps(profile))
    out['max_calls'] = 160 if revision == REVISION_160 else 1000
    out['campaign_stop_calls'] = _campaign_stop_for_revision(revision)
    out['runtime_budget_authority'] = {
        'revision_id': revision,
        'global_max_calls': out['max_calls'],
        'campaign_stop_cap': out['campaign_stop_calls'],
        'historical_profile_max_calls': 80,
        'effective_profile_sha256': profile.get('effective_profile_sha256'),
        'cumulative_runtime_is_reporting_only': True,
        'source': (ADDENDUM_160_JSON if revision == REVISION_160 else
                   AUTHORIZATION_1000_JSON if revision == REVISION_1000 else
                   AUTHORIZATION_CAMPAIGN_1000_JSON if revision == REVISION_CAMPAIGN_1000 else
                   AUTHORIZATION_UNBOUNDED_JSON if revision == REVISION_UNBOUNDED else
                   AUTHORIZATION_GPT6_LUNA_JSON if revision == REVISION_GPT6_LUNA else
                   AUTHORIZATION_CODEX_LUNA_JSON).as_posix(),
    }
    if revision in (REVISION_1000, REVISION_CAMPAIGN_1000, REVISION_CODEX_LUNA, REVISION_GPT6_LUNA, REVISION_UNBOUNDED):
        out['reasoning'] = 'xhigh'
        out['lead'] = 'gpt-6-astra'
        if revision in (REVISION_CODEX_LUNA, REVISION_GPT6_LUNA, REVISION_UNBOUNDED):
            out['worker_provider'] = 'codex'
            out['worker'] = 'gpt-6-luna' if revision in (REVISION_GPT6_LUNA, REVISION_UNBOUNDED) else 'gpt-5.6-luna'
            out['worker_variant'] = 'max'
        else:
            out['worker_provider'] = 'opencode'
            out['worker'] = 'opencode/muse-spark-1.3-contributor-free'
            out['worker_variant'] = 'xhigh'
        out['route_authority'] = {
            'lead_model': 'gpt-6-astra', 'lead_reasoning': 'xhigh',
            'worker_provider': out['worker_provider'],
            'worker_model': out['worker'],
            'worker_variant': out['worker_variant'],
            'source': (AUTHORIZATION_1000_JSON if revision == REVISION_1000 else
                       AUTHORIZATION_CAMPAIGN_1000_JSON if revision == REVISION_CAMPAIGN_1000 else
                       AUTHORIZATION_UNBOUNDED_JSON if revision == REVISION_UNBOUNDED else
                       AUTHORIZATION_GPT6_LUNA_JSON if revision == REVISION_GPT6_LUNA else
                       AUTHORIZATION_CODEX_LUNA_JSON).as_posix(),
        }
    return out


def effective_call_cap(profile):
    """Operational stop ceiling; global authority remains separately reported."""
    global_cap = int(profile.get('max_calls', 0))
    stop_cap = profile.get('campaign_stop_calls')
    return min(global_cap, int(stop_cap)) if stop_cap is not None else global_cap


def used_call_units(state):
    return sum(int(state.get(k, 0)) for k in ('model_calls', 'bootstrap_invocations', 'prior_reserved_calls'))


def remaining_call_units(state, profile):
    return max(0, effective_call_cap(profile) - used_call_units(state))


def global_remaining_call_units(state, profile):
    return max(0, int(profile.get('max_calls', 0)) - used_call_units(state))

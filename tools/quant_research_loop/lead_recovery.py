"""R04 authority overlay and durable return of local failures to the Lead."""
import copy
import hashlib
import json
from .gate_zero import ROOT, write
from .prepare import digest

REV = 'R04_FLOOR500_LEAD_RECOVERY_20260922'
BASE = ROOT / 'research/campaigns/PAYOUT_V1/revisions' / REV
REV5 = 'R05_TOTAL_CAP1000_DECISION_FIRST_20260922'
REVISION_1000 = 'R05_RUNTIME_ADDENDUM_20260922_1000_CALLS'
BASE5_REL = 'research/campaigns/PAYOUT_V1/revisions/' + REV5
REV6 = 'R06_CAMPAIGN_CAP1000_REVIEW_FIRST_20260923'
REVISION_CAMPAIGN_1000 = 'R06_RUNTIME_ADDENDUM_20260923_CAMPAIGN1000'
BASE6_REL = 'research/campaigns/PAYOUT_V1/revisions/' + REV6
REV7 = 'R07_CODEX_LUNA_MAX_EXECUTOR_20260923'
REVISION_CODEX_LUNA = 'R07_RUNTIME_ADDENDUM_20260923_CODEX_LUNA_MAX'
BASE7_REL = 'research/campaigns/PAYOUT_V1/revisions/' + REV7
REV8 = 'R08_CODEX_GPT6_LUNA_MAX_EXECUTOR_20260923'
REVISION_GPT6_LUNA = 'R08_RUNTIME_ADDENDUM_20260923_GPT6_LUNA_MAX'
BASE8_REL = 'research/campaigns/PAYOUT_V1/revisions/' + REV8
REV9 = 'R09_UNBOUNDED_ACTIVE_RUNTIME_20260923'
REVISION_UNBOUNDED = 'R09_RUNTIME_ADDENDUM_20260923_UNBOUNDED_ACTIVE'
BASE9_REL = 'research/campaigns/PAYOUT_V1/revisions/' + REV9


def _base_for_revision(revision):
    if revision == REV:
        return BASE
    if revision == REV5:
        return ROOT / BASE5_REL
    if revision == REV6:
        return ROOT / BASE6_REL
    if revision == REV7:
        return ROOT / BASE7_REL
    if revision == REV8:
        return ROOT / BASE8_REL
    if revision == REV9:
        return ROOT / BASE9_REL
    raise ValueError('Unknown recovery authorization revision')


def active_base(state):
    revision = state.get('lead_recovery_revision')
    return _base_for_revision(revision if revision in (REV, REV5, REV6, REV7, REV8, REV9) else REV)


def authority(revision=REV):
    base = _base_for_revision(revision)
    path = base / 'authorization.json'
    if not path.exists():
        return None
    a = json.loads(path.read_text(encoding='utf-8'))
    if revision == REV:
        valid = (a['revision'] == REV and a['source_sha256'] == digest(base / 'USER_AUTHORIZATION.txt')
                 and a['global_call_cap'] == 160 and a['economic_floor_usd'] == 500
                 and a['live'] == 'DISABLED')
    elif revision == REV5:
        valid = (a.get('revision') == REV5 and a.get('global_call_cap') == 1000
                 and a.get('campaign_stop_cap') == 320 and a.get('source') == 'USER_AUTHORIZATION.txt'
                 and a.get('live') == 'DISABLED' and a.get('lead_model') == 'gpt-6-astra'
                 and a.get('lead_reasoning') == 'xhigh'
                 and a.get('worker_model') == 'opencode/muse-spark-1.3-contributor-free'
                 and a.get('worker_variant') == 'xhigh'
                 and a.get('source_sha256') == digest(base / 'USER_AUTHORIZATION.txt'))
    elif revision == REV6:
        valid = (a.get('revision') == REV6 and a.get('global_call_cap') == 1000
                 and a.get('campaign_stop_cap') == 1000 and a.get('source') == 'USER_AUTHORIZATION.txt'
                 and a.get('review_first_required') is True
                 and a.get('review_first_worker_dispatch_allowed') is False
                 and a.get('live') == 'DISABLED' and a.get('lead_model') == 'gpt-6-astra'
                 and a.get('lead_reasoning') == 'xhigh'
                 and a.get('worker_model') == 'opencode/muse-spark-1.3-contributor-free'
                  and a.get('worker_variant') == 'xhigh'
                  and a.get('source_sha256') == digest(base / 'USER_AUTHORIZATION.txt'))
    elif revision in (REV7, REV8, REV9):
        expected_model = 'gpt-6-luna' if revision in (REV8, REV9) else 'gpt-5.6-luna'
        valid = (a.get('revision') == revision and a.get('global_call_cap') == 1000
                 and a.get('campaign_stop_cap') == 1000 and a.get('source') == 'USER_AUTHORIZATION.txt'
                 and a.get('live') == 'DISABLED' and a.get('lead_model') == 'gpt-6-astra'
                 and a.get('lead_reasoning') == 'xhigh'
                 and a.get('worker_provider') == 'codex'
                 and a.get('worker_model') == expected_model
                 and a.get('worker_variant') == 'max'
                 and a.get('worker_reasoning') == 'max'
                 and a.get('worker_auth_class') == 'ChatGPT'
                 and a.get('worker_paid_fallback') is False
                 and a.get('source_sha256') == digest(base / 'USER_AUTHORIZATION.txt'))
        if revision == REV9:
            valid = valid and a.get('fixed_elapsed_timeouts_active') is False and a.get('inactivity_stall_seconds') == 3600
    else:
        valid = False
    if not valid:
        raise ValueError('R04 authorization mismatch')
    return a


def enabled(state):
    revision = state.get('lead_recovery_revision')
    return revision in (REV, REV5, REV6, REV7, REV8, REV9) and authority(revision) is not None


def apply(profile, state):
    if not state.get('generic_authority'):
        return profile
    if (state.get('runtime_policy_revision') == REVISION_UNBOUNDED
            and authority(REV9) is not None):
        revision = REV9
    elif (state.get('runtime_policy_revision') == REVISION_GPT6_LUNA
            and authority(REV8) is not None):
        revision = REV8
    elif (state.get('runtime_policy_revision') == REVISION_CODEX_LUNA
            and authority(REV7) is not None):
        revision = REV7
    elif (state.get('runtime_policy_revision') == REVISION_CAMPAIGN_1000
            and authority(REV6) is not None):
        revision = REV6
    elif (state.get('runtime_policy_revision') == REVISION_1000
            and authority(REV5) is not None):
        revision = REV5
    else:
        revision = REV
    a = authority(revision)
    if a is None:
        return profile
    p = copy.deepcopy(profile)
    path = BASE / 'EFFECTIVE_PROFILE.json'
    if digest(path) != a['effective_profile_sha256']:
        raise ValueError('Active economic profile hash mismatch')
    p['effective_profile'] = json.loads(path.read_text(encoding='utf-8'))
    p['effective_profile_path'] = path.relative_to(ROOT).as_posix()
    p['effective_profile_sha256'] = digest(path)
    p['objective_revision'] = revision
    if state.get('lead_recovery_revision') != revision:
        snapshot_path = _base_for_revision(revision) / 'state_before.json'
        state.setdefault('status_history', []).append({
            'status': state['status'], 'error': state.get('error'),
            'preserved_by_revision': revision,
            'snapshot': snapshot_path.relative_to(ROOT).as_posix()})
        state['lead_recovery_revision'] = revision
        state['objective_revision'] = revision
    state.setdefault('authorized_metadata_artifacts', {})[p['effective_profile_path']] = {
        'sha256': p['effective_profile_sha256'], 'partition': 'POLICY_OR_METADATA'}
    return p


def capture(controller, error):
    """Checkpoint once per occurrence; do not retry or reset local counters."""
    s = controller.state
    job = s.get('pending_job')
    if not enabled(s) or not job:
        return False
    text = str(error)
    if not text.startswith(('ENGINEERING_BLOCKED:', 'EXECUTION_', 'VERIFICATION_')):
        return False
    folder = ROOT / 'research/campaigns/PAYOUT_V1/runtime/jobs' / job['job_id']
    failures = []
    for path in sorted(folder.glob('failure_*.json')):
        value = json.loads(path.read_text(encoding='utf-8'))
        failures.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': digest(path), **value})
    artifacts = []
    for name in ('analysis.py', 'result.json'):
        path = folder / 'work' / name
        if path.exists():
            artifacts.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': digest(path),
                              'verified': False})
    from .runtime_policy import effective_call_cap, used_call_units
    context = {'job': job, 'error': text, 'failures': failures,
               'partial_artifacts': artifacts, 'repair_history': copy.deepcopy(s.get('blocker_repairs', {})),
               'remaining_calls': effective_call_cap(controller.profile) - used_call_units(s),
               'require_changed_hypothesis_before_retry': True,
               'independent_trial': False}
    history = s.setdefault('recovery_history', [])
    active_revision = s.get('lead_recovery_revision')
    revision_base = _base_for_revision(active_revision if active_revision in (REV, REV5, REV6, REV7, REV8, REV9) else REV)
    path = revision_base / 'recovery' / f'{len(history):03d}.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    write(path, context)
    history.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': digest(path), 'job_id': job['job_id']})
    s['recovery_context'] = context
    s['status'] = 'RUNNING'
    controller.save()
    return True


def validate_episode(spec, state):
    episode = spec.get('recovery_episode')
    if not episode:
        return
    if not enabled(state):
        raise ValueError('Recovery episode lacks authority')
    if episode['max_attempts'] not in (1, 2):
        raise ValueError('Unbounded repair episode')
    for key in ('component', 'root_cause', 'changed_approach', 'stop_condition'):
        if len(episode[key].strip()) < 20:
            raise ValueError('Recovery requires concrete component, cause, changed approach and stop')
    if not episode['evidence_refs']:
        raise ValueError('Recovery requires failure evidence')
    from .generic_jobs import relpath
    missing=[p for p in episode['evidence_refs'] if not relpath(p).is_file()]
    if missing:
        raise ValueError('Recovery evidence_refs require repository FILE PATHS, not hashes; missing: '+repr(missing))
    identity = hashlib.sha256(json.dumps({k: episode[k] for k in
        ('component', 'root_cause', 'changed_approach')}, sort_keys=True).encode()).hexdigest()
    for old in state.get('recovery_episodes', {}).values():
        if old['identity'] == identity and old['job_id'] != spec['job_id']:
            raise ValueError('Renamed repair episode is not new authority')


def reserve_episode(state, job, code, evidence):
    """New Lead episode is separately metered; historical repairs remain intact."""
    episode = state.get('lead_recovery_authorizations',{}).get(job['job_id'],{}).get('episode') or job.get('recovery_episode')
    if not episode or not enabled(state):
        return False
    validate_episode(dict(job,recovery_episode=episode), state)
    identity = hashlib.sha256(json.dumps({k: episode[k] for k in
        ('component', 'root_cause', 'changed_approach')}, sort_keys=True).encode()).hexdigest()
    record = state.setdefault('recovery_episodes', {}).setdefault(identity, {
        'identity': identity, 'job_id': job['job_id'], 'authorization': episode, 'attempts': []})
    if len(record['attempts']) >= episode['max_attempts']:
        raise RuntimeError('ENGINEERING_BLOCKED: Lead repair episode exhausted')
    if any(x['evidence'] == evidence for x in record['attempts']):
        raise RuntimeError('ENGINEERING_BLOCKED: unchanged recovery evidence')
    record['attempts'].append({'code': code, 'evidence': evidence})
    return True

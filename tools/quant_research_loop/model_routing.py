"""User-authorized model routing, separate from historical research authority.

The route revision cannot resume a campaign, reset accounting, or change the
financial mandate, data scope, or permissions of any role.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = 'gpt-6.1-sol'
REASONING = 'high'
LEAD_LABEL = 'Sol 6.1 High'
REVISION = 'R19_SOL61_HIGH_ROUTES_20261001'
BASE = Path('research/campaigns/PAYOUT_V1/revisions') / REVISION
AUTHORIZATION = BASE / 'AUTHORIZATION.json'
AUTONOMOUS_REVISION = 'R24_AUTONOMOUS_LEAD_20261003'
AUTONOMOUS_BASE = BASE.parent / AUTONOMOUS_REVISION
AUTONOMOUS_AUTHORIZATION = AUTONOMOUS_BASE / 'MODEL_AUTHORIZATION.json'
MODEL_CAPABILITY = AUTONOMOUS_BASE / 'MODEL_CAPABILITY.json'
LEAN_REVISION = 'R25_LEAN_PAYOUT_SOL61_MEDIUM_20261003'
LEAN_AUTHORIZATION = BASE.parent / LEAN_REVISION / 'MODEL_AUTHORIZATION.json'


def _exact(actual, expected):
    """JSON equality must not mistake true for 1 or false for a zero budget."""
    return (json.dumps(actual, sort_keys=True, allow_nan=False)
            == json.dumps(expected, sort_keys=True, allow_nan=False))


def _pinned_json(relative, sha256, label):
    path = ROOT / relative
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError(f'{label} path mismatch')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError(f'{label} hash mismatch')
    value = json.loads(raw.decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object')
    return value


def _lead_reasoning(value, required_effort=None):
    """Bind R24 to a no-turn native catalog/configuration capability receipt."""
    pin = value.get('model_capability')
    if not isinstance(pin, dict) or pin.get('path') != MODEL_CAPABILITY.as_posix():
        raise ValueError('Model capability identity mismatch')
    capability = _pinned_json(MODEL_CAPABILITY, pin.get('sha256'), 'Model capability')
    expected = {
        'schema_version': 1, 'revision_id': AUTONOMOUS_REVISION, 'model': MODEL,
        'provider': 'codex', 'login': 'chatgpt', 'model_call_units': 0,
        'no_thread_or_model_turn_sent': True, 'api_credentials_used': False,
        'sent_methods': ['initialize', 'initialized', 'model/list', 'config/read'],
    }
    if any(not _exact(capability.get(key), wanted) for key, wanted in expected.items()):
        raise ValueError('Model capability provenance mismatch')
    catalog, config = capability.get('model_catalog'), capability.get('native_config')
    if not isinstance(catalog, dict) or not isinstance(config, dict):
        raise ValueError('Model capability catalog/configuration missing')
    model = catalog.get('model')
    if (catalog.get('method') != 'model/list' or catalog.get('nextCursor') is not None
            or not isinstance(model, dict) or model.get('id') != MODEL or model.get('model') != MODEL):
        raise ValueError('Model capability catalog mismatch')
    entries = model.get('supportedReasoningEfforts')
    if (not isinstance(entries, list) or not entries
            or any(not isinstance(entry, dict) or not isinstance(entry.get('reasoningEffort'), str)
                   or not entry['reasoningEffort'] for entry in entries)):
        raise ValueError('Model capability reasoning catalog malformed')
    efforts = [entry['reasoningEffort'] for entry in entries]
    if len(set(efforts)) != len(efforts):
        raise ValueError('Model capability reasoning catalog ambiguous')
    if required_effort is not None and required_effort not in efforts:
        raise ValueError('Model capability executor reasoning unsupported')
    selected = 'ultra' if 'ultra' in efforts else 'max'
    # A native configuration rejection may justify the user's max fallback even
    # when a catalog advertises ultra. The exact error is retained in the pin.
    if selected == 'ultra' and config.get('observed') == 'max':
        rejection = config.get('ultra_rejection', {})
        if (not isinstance(rejection, dict) or rejection.get('reasoning') != 'ultra'
                or rejection.get('stage') != 'native_config'
                or not isinstance(rejection.get('error'), str) or not rejection['error'].strip()
                or rejection.get('no_thread_or_model_turn_sent') is not True):
            raise ValueError('Model capability ultra fallback lacks native rejection')
        selected = 'max'
    config_expected = {'key': 'model_reasoning_effort', 'requested': selected,
                       'observed': selected, 'initialize_completed': True, 'config_read_completed': True}
    if (selected not in efforts or capability.get('selected_reasoning') != selected
            or any(not _exact(config.get(key), wanted) for key, wanted in config_expected.items())):
        raise ValueError('Model capability selected reasoning mismatch')
    return selected


def authority(state):
    """Validate an explicit state pin; historical state keeps its own route."""
    ref = state.get('model_route_authority')
    if ref is None:
        return None
    if not isinstance(ref, dict):
        raise ValueError('Model route authorization identity mismatch')
    revision = ref.get('revision_id')
    if not isinstance(revision, str):
        raise ValueError('Model route authorization identity mismatch')
    relative = {REVISION: AUTHORIZATION, AUTONOMOUS_REVISION: AUTONOMOUS_AUTHORIZATION,
                LEAN_REVISION: LEAN_AUTHORIZATION}.get(revision)
    if relative is None or ref.get('path') != relative.as_posix():
        raise ValueError('Model route authorization identity mismatch')
    value = _pinned_json(relative, ref.get('sha256'), 'Model route authorization')
    reasoning = (_lead_reasoning(value, 'medium' if revision == LEAN_REVISION else None)
                 if revision in (AUTONOMOUS_REVISION, LEAN_REVISION) else REASONING)
    expected = {
        'revision_id': revision, 'campaign_id': 'PAYOUT_V1', 'approved': True,
        'source': 'USER_AUTHORIZATION.txt', 'provider': 'codex', 'login': 'chatgpt',
        'routes': {'lead': {'model': MODEL, 'reasoning': reasoning},
                   'qa': {'model': MODEL, 'reasoning': REASONING}},
        'executor': ({'provider': 'codex', 'model': MODEL, 'reasoning': 'medium'}
                     if revision == LEAN_REVISION else
                     {'provider': 'codex', 'model': 'gpt-6-luna', 'reasoning': 'max'}),
        'global_call_cap': 1000, 'spend_usd': 0, 'live': 'DISABLED',
        'resume_authorized': False, 'qa_tools_allowed': False,
    }
    if revision in (AUTONOMOUS_REVISION, LEAN_REVISION):
        expected['skill_engineering'] = {'model': MODEL, 'reasoning': REASONING}
    if any(not _exact(value.get(key), wanted) for key, wanted in expected.items()):
        raise ValueError('Model route or preserved permission mismatch')
    source = ROOT / relative.parent / value['source']
    if not source.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('Model route user authorization path mismatch')
    if hashlib.sha256(source.read_bytes()).hexdigest() != value.get('source_sha256'):
        raise ValueError('Model route user authorization hash mismatch')
    return value


def apply(profile, state):
    """Overlay approved future routes after all historical profile checks pass."""
    approved = authority(state)
    if approved is None:
        return profile
    out = deepcopy(profile)
    out['lead'] = approved['routes']['lead']['model']
    out['reasoning'] = approved['routes']['lead']['reasoning']
    if approved['revision_id'] == LEAN_REVISION:
        out['worker_provider'] = approved['executor']['provider']
        out['worker'] = approved['executor']['model']
        out['worker_variant'] = approved['executor']['reasoning']
    out['model_route_authority'] = deepcopy(state['model_route_authority'])
    out['route_authority'] = dict(out.get('route_authority', {}),
                                 lead_model=out['lead'], lead_reasoning=out['reasoning'],
                                 model_route_source=state['model_route_authority']['path'])
    if approved['revision_id'] == LEAN_REVISION:
        out['route_authority'].update(worker_provider=out['worker_provider'],
                                      worker_model=out['worker'], worker_reasoning=out['worker_variant'])
    return out

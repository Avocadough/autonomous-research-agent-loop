"""R24 technical-stop recovery checkpoints, never worker or trading authority.

The supervisor may ask ``recover`` for RESUME/BACKOFF/MAINTENANCE_REQUIRED/STOP. A RESUME only
requires a fresh Lead review; Controller still owns every metered invocation,
independent QA, changed worker episode, dispatch grant and scope check.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import time

from .gate_zero import ROOT, write
from .prepare import digest
from .runtime_policy import effective_call_cap, used_call_units

REVISION = 'R24_AUTONOMOUS_LEAD_20261003'
BASE = Path('research/campaigns/PAYOUT_V1/revisions') / REVISION
AUTHORIZATION = BASE / 'AUTHORIZATION.json'
RUNTIME = ROOT / 'research/campaigns/PAYOUT_V1/runtime'
EXPECTED = {
    'revision_id': REVISION, 'campaign_id': 'PAYOUT_V1', 'approved': True,
    'source': 'USER_AUTHORIZATION.txt', 'resume_authorized': True,
    'autonomous_technical_recovery': True,
    'lead_owns_changed_recovery_episode': True,
    'global_call_cap': 1000, 'spend_usd': 0, 'live': 'DISABLED',
    'preserve_user_pause_cancel': True, 'preserve_data_permissions': True,
    'preserve_account_permissions': True,
    'preserve_research_episode_budgets': True, 'preserve_call_accounting': True,
}


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def authority(state):
    ref = state.get('autonomous_recovery_authority')
    if not ref:
        return None
    if ref.get('revision_id') != REVISION or ref.get('path') != AUTHORIZATION.as_posix():
        raise ValueError('Autonomous recovery authorization identity mismatch')
    path = ROOT / AUTHORIZATION
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('Autonomous recovery authorization path mismatch')
    if digest(path) != ref.get('sha256'):
        raise ValueError('Autonomous recovery authorization hash mismatch')
    value = _read(path)
    from .model_routing import _exact
    if any(not _exact(value.get(key), expected) for key, expected in EXPECTED.items()):
        raise ValueError('Autonomous recovery permission mismatch')
    if digest(path.parent / value['source']) != value.get('source_sha256'):
        raise ValueError('Autonomous recovery user authorization changed')
    return value


def _protected(error):
    text = str(error).lower()
    protected = (
        'scope violation', 'scope check failed', 'scope incident', 'forbidden tool',
        'outside acknowledged', 'outside authorized', 'authority mismatch',
        'authorization mismatch', 'authorization identity', 'authorization hash',
        'authority changed', 'identity changed', 'hash mismatch', 'pin mismatch',
        'artifact changed', 'saved qa review changed', 'permission mismatch',
        'permission denied', 'access denied', 'not authorized', 'unauthorized',
        'test_reserved', 'reserved-test', 'reserved test', 'raw combined',
        'price reads', 'live trading', 'account purchase', 'brokerage', 'payment',
        'paid fallback', 'api key', 'authentication', 'login required',
        'pause requested', 'paused_user', 'cancel requested', 'cancelled by user',
        'resource_limit', 'budget exhausted', 'call cap', 'safety hold',
    )
    return any(token in text for token in protected)


def classify(error, stage='controller'):
    """Explicit technical signatures only; uncertain authority stays stopped."""
    text = str(error).lower()
    if _protected(text):
        return None
    # A Human Action Required envelope is never treated as a generic engine
    # exception. Exact incident acknowledgement is handled separately by root.
    if text.startswith('human_action_required:'):
        return None
    if any(token in text for token in ('model is at capacity', 'rate limit',
                                       'provider capacity', 'service unavailable',
                                       'too many requests', 'http 429', 'http 503')):
        return 'PROVIDER_CAPACITY'
    if any(token in text for token in ('transport', 'connection reset',
                                       'connection timed out', 'provider/session/output incomplete',
                                       'lead runtime unavailable', 'interrupted lead call',
                                       'input_too_large', 'input exceeds the maximum length')):
        return 'TRANSPORT'
    if text.startswith(('lead_dispatch_blocked:', 'lead_prerequisite:',
                        'technical_prerequisite:')):
        return 'PREREQUISITE'
    if any(token in text for token in ('invalid_json_schema', 'invalid schema for response_format',
                                       'format/permission validation exhausted',
                                       'qa has unresolved findings', 'qa requires correction',
                                       'qa pass conflicts', 'qa facets incomplete',
                                       'qa report identity mismatch')):
        return 'QA_OR_SCHEMA'
    if text.startswith(('engineering_blocked:', 'execution_', 'verification_',
                        'lead_selected_unregistered_job_requires_scoped_handler')):
        return 'ENGINEERING'
    return None


def _provider_permission_failure(raw):
    # Logs can contain a rejected Lead proposal discussing normal trading/data
    # restrictions. Only actual provider access failures block transport retry;
    # trusted controller error messages still use the broader protected gate.
    text = str(raw).lower()
    return any(token in text for token in (
        'authentication failed', 'authentication expired', 'login required',
        'invalid api key', 'unauthorized request', 'permission denied',
        'access denied', 'approval required',
    ))


def _pin(path):
    path = Path(path).resolve()
    root = ROOT.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Recovery evidence must be an existing workspace file')
    relative = path.relative_to(root).as_posix()
    allowed = ('tools/', '.codex/skills/payout-',
               'research/campaigns/PAYOUT_V1/runtime/',
               'research/campaigns/PAYOUT_V1/revisions/')
    if not relative.startswith(allowed) or path.name in ('state.json', 'pause.request', 'cancel.request'):
        raise ValueError('Recovery evidence is outside immutable engineering scope')
    return {'path': relative, 'sha256': digest(path)}


def _check_pin(ref):
    observed = _pin(ROOT / ref['path'])
    if observed != ref:
        raise ValueError('Autonomous recovery evidence identity changed')
    return ROOT / ref['path']


def _decision_boundary(state):
    """Only archive completed failed Lead folders; never infer missing turns."""
    index = int(state.get('decisions', 0))
    folder = RUNTIME / 'decisions' / f'{index:03d}'
    refs, diagnostics = [], []
    calls = sorted((p for p in folder.glob('call_*') if p.is_dir()),
                   key=lambda p: int(p.name.split('_')[-1]))
    if (folder / 'accepted.json').exists():
        raise ValueError('Accepted Lead decision requires state reconciliation')
    for call in calls:
        request, receipt_path = call / 'request.json', call / 'receipt.json'
        if not request.is_file() or not receipt_path.is_file():
            raise ValueError('Unreconciled Lead invocation; no completed receipt')
        receipt = _read(receipt_path)
        refs.extend([_pin(request), _pin(receipt_path)])
        call_text = ''
        for name in ('stderr.txt', 'stdout.jsonl'):
            path = call / name
            if path.is_file():
                refs.append(_pin(path))
                # Diagnostic strings are only classification hints. Full raw
                # receipts remain pinned and the Lead receives their paths.
                call_text += path.read_text(encoding='utf-8', errors='replace')[-65536:]
        diagnostics.append(call_text)
        suffix = call.name.split('_')[-1]
        output, rejection = folder / f'decision_{suffix}.json', folder / f'rejection_{suffix}.json'
        known_timeout = (receipt.get('exit_code') is None
                         and receipt.get('process_reaped') is True
                         and any(marker in call_text for marker in (
                             'CONTROLLER_TIMEOUT', 'CONTROLLER_INACTIVITY_STALL',
                             'NATIVE_TRANSPORT_TIMEOUT'))
                         and 'turn.completed' not in call_text and not output.exists())
        if ((type(receipt.get('exit_code')) is not int and not known_timeout)
                or receipt.get('process_reaped') is not True):
            raise ValueError('Unreconciled Lead invocation; process completion uncertain')
        if output.is_file():
            refs.append(_pin(output))
            if not rejection.is_file():
                raise ValueError('Completed Lead output requires validation before retry')
            refs.append(_pin(rejection))
        elif 'turn.completed' in call_text:
            raise ValueError('Completed Lead turn requires output reconciliation')
        elif receipt['exit_code'] == 0 and not any(token in call_text for token in (
                'turn.failed', 'invalid_json_schema', 'Invalid schema for response_format')):
            raise ValueError('Successful Lead process without output requires reconciliation')
    return refs, '\n'.join(diagnostics), index + 1 if calls else index


def _gate(controller):
    state = controller.state
    if state.get('autonomous_recovery', {}).get('review_required'):
        context(state)
    if authority(state) is None:
        return 'Autonomous technical recovery is not authorized'
    if any((RUNTIME / name).exists() for name in ('pause.request', 'cancel.request')):
        return 'User pause/cancel marker requires explicit reconciliation'
    if state.get('status') in ('PAUSED_USER', 'CANCELLED', 'CANCELED',
                               'READY_FOR_FORWARD', 'DATA_OR_RULES_BLOCKED',
                               'NO_QUALIFYING_CANDIDATE', 'RESOURCE_LIMIT', 'PAUSED_BUDGET'):
        return 'Current terminal or user stop must be preserved'
    if state.get('inflight_call'):
        return 'Unreconciled inflight invocation must be preserved'
    from .safety_hold import active, delegated_review_allowed
    if active(state) and not delegated_review_allowed(state):
        return 'Active safety hold requires its exact existing disposition'
    if used_call_units(state) >= min(1000, effective_call_cap(controller.profile)):
        return 'Global or effective call cap exhausted'
    return None


def _qa_failure(state):
    ref = state.get('latest_qa_review') or {}
    if ref.get('verdict') != 'INCOMPLETE':
        return None
    path = _check_pin({key: ref[key] for key in ('path', 'sha256')})
    if not path.resolve().is_relative_to((RUNTIME / 'qa').resolve()) or path.name != 'report.json':
        raise ValueError('QA retry report outside QA receipt directory')
    report = _read(path)
    if (report.get('limitations') != ['Controller-generated failure envelope; no accepted model QA verdict.']
            or any(report.get(key) for key in ('reviewed_files', 'facets', 'findings'))):
        return None  # An actual model verdict must be corrected through normal evidence.
    if _protected(report.get('summary', '')):
        return None
    call = path.parent / 'call_0'
    request, receipt_path = call / 'request.json', call / 'receipt.json'
    receipt = _read(receipt_path)
    refs = [_pin(path), _pin(request), _pin(receipt_path)]
    raw = ''
    for name in ('stdout.jsonl', 'stderr.txt'):
        log = call / name
        if log.is_file():
            refs.append(_pin(log))
            raw += log.read_text(encoding='utf-8', errors='replace')[-65536:]
    response = path.parent / 'response.json'
    known_timeout = (receipt.get('exit_code') is None and receipt.get('process_reaped') is True
                     and any(marker in raw for marker in ('CONTROLLER_TIMEOUT', 'CONTROLLER_INACTIVITY_STALL', 'NATIVE_TRANSPORT_TIMEOUT'))
                     and 'turn.completed' not in raw and not response.exists())
    if (receipt.get('process_reaped') is not True
            or (type(receipt.get('exit_code')) is not int and not known_timeout)):
        raise ValueError('Unreconciled QA invocation; process completion uncertain')
    if _provider_permission_failure(raw):
        return None
    if response.is_file():
        refs.append(_pin(response))
    elif 'turn.completed' in raw:
        raise ValueError('Completed QA turn requires output reconciliation')
    elif receipt.get('exit_code') == 0 and not any(token in raw for token in (
            'turn.failed', 'invalid_json_schema', 'Invalid schema for response_format')):
        raise ValueError('Successful QA process without output requires reconciliation')
    return {'job_id': report['job_id'], 'failed_review_key': report['review_key'],
            'category': classify(raw) or classify(report.get('summary', '')) or 'QA_OR_SCHEMA',
            'evidence_refs': refs, 'report': _pin(path)}


def prerequisite_inputs_key(state):
    metadata = state.get('operator_metadata_recovery') or {}
    qa = state.get('latest_qa_review') or {}
    inputs = {'pending_job': state.get('pending_job'),
              'pending_lead_evidence': state.get('pending_lead_evidence'),
              'qa': {key: qa.get(key) for key in ('path', 'sha256')},
              'metadata': {key: metadata.get(key) for key in ('authorization', 'reconciliation')}}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def capture(controller, error, stage='controller', evidence_refs=()):
    """Record one immutable incident, without calls, resets or stop release.

    Returns False for unknown/protected failures and unchanged incidents that
    already received a fresh Lead review opportunity.
    """
    if _gate(controller):
        return False
    state = controller.state
    category = classify(error, stage)
    if (category is None and state.get('status') == 'ENGINEERING_BLOCKED'
            and not _protected(error) and not str(error).startswith('HUMAN_ACTION_REQUIRED:')):
        category = 'ENGINEERING'
    if category is None:
        return False
    refs, diagnostic, next_index = _decision_boundary(state)
    if _provider_permission_failure(diagnostic):
        return False
    qa_failure = _qa_failure(state)
    if qa_failure:
        refs.extend(qa_failure['evidence_refs'])
        category = qa_failure['category']
    if category == 'TRANSPORT' and classify(diagnostic) == 'PROVIDER_CAPACITY':
        category = 'PROVIDER_CAPACITY'
    if category in ('TRANSPORT', 'PROVIDER_CAPACITY') and not refs:
        # No saved request/receipt means there is no demonstrated provider
        # failure to retry. The caller must supply separate reconciled evidence.
        return False
    for ref in evidence_refs:
        refs.append(_pin(ROOT / ref) if isinstance(ref, str) else _pin(_check_pin(ref)))
    job = state.get('pending_job') or {}
    job_id = job.get('job_id')
    if job_id:
        folder = (RUNTIME / 'jobs' / job_id).resolve()
        if not folder.is_relative_to((RUNTIME / 'jobs').resolve()):
            raise ValueError('Recovery job identity escaped job directory')
        refs.extend(_pin(path) for path in sorted(folder.glob('failure_*.json')))
    refs = sorted({ref['path']: ref for ref in refs}.values(), key=lambda ref: ref['path'])
    identity = {'error': str(error), 'stage': stage, 'category': category,
                'job_id': job_id, 'decision_index': int(state.get('decisions', 0)),
                'evidence_refs': refs}
    prerequisite_evidence = None
    if category == 'PREREQUISITE':
        # A rephrased PAUSE is not new information. Only changed pinned inputs
        # justify another Lead review of a prerequisite it already reviewed.
        prerequisite_evidence = prerequisite_inputs_key(state)
        reviewed = state.get('autonomous_recovery', {}).get('prerequisite_reviews', {}).get(prerequisite_evidence)
        if reviewed:
            path = _check_pin(reviewed)
            if _read(path).get('action') != 'PAUSE':
                raise ValueError('Prerequisite wait requires an actual accepted Lead PAUSE')
            return False
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    semantic = {key: value for key, value in identity.items() if key != 'decision_index'}
    semantic['evidence_refs'] = [ref for ref in refs if '/decisions/' not in ref['path']]
    semantic_fingerprint = hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()
    recovery = state.setdefault('autonomous_recovery', {})
    if recovery.get('current', {}).get('fingerprint') == fingerprint:
        context(state)  # validate existing pin before treating it as idempotent
        return recovery.get('phase') not in ('REVIEW_DISPATCHED', 'REVIEWED')
    record = dict(identity, revision_id=REVISION, semantic_fingerprint=semantic_fingerprint,
                  prerequisite_evidence=prerequisite_evidence,
                  qa_failure=qa_failure,
                  used_calls_at_capture=used_call_units(state),
                  next_decision_index=next_index,
                  prior_worker_dispatch_decision=deepcopy(state.get('worker_dispatch_decision')),
                  require_fresh_lead=True, require_changed_recovery_episode=True,
                  worker_dispatch_authorized=False, independent_trial=False,
                  requirements=[
                      'Diagnose the preserved failure and select a changed bounded technical episode.',
                      'Complete prerequisites and independent QA before a fresh explicit worker grant.',
                      'Preserve spent calls, worker attempts, datasets, permissions and stop conditions.',
                  ])
    path = RUNTIME / 'autonomous_recovery' / fingerprint / 'incident.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = _read(path)
        # An identical orphan created before state persistence can be recovered.
        # Never adopt changed routing, accounting or permission fields from it.
        if previous != record:
            raise ValueError('Autonomous recovery incident collision')
    else:
        write(path, record)
    pin = dict(_pin(path), fingerprint=fingerprint)
    recovery.setdefault('history', []).append(pin)
    counts = recovery.setdefault('semantic_attempts', {})
    counts[semantic_fingerprint] = int(counts.get(semantic_fingerprint, 0)) + 1
    recovery.update(current=pin, review_required=True, phase='CAPTURED')
    controller.save()
    return True


def context(state):
    """Return only hash-validated advisory context for the current Lead review."""
    if authority(state) is None:
        return None
    recovery = state.get('autonomous_recovery', {})
    ref = recovery.get('current')
    if not ref or not recovery.get('review_required'):
        return None
    if (len(ref.get('fingerprint', '')) != 64
            or any(character not in '0123456789abcdef' for character in ref['fingerprint'])):
        raise ValueError('Autonomous recovery incident fingerprint malformed')
    path = _check_pin({key: ref[key] for key in ('path', 'sha256')})
    expected_base = (RUNTIME / 'autonomous_recovery' / ref['fingerprint']).resolve()
    if path.resolve() != expected_base / 'incident.json':
        raise ValueError('Autonomous recovery incident path mismatch')
    record = _read(path)
    if record.get('revision_id') != REVISION or record.get('worker_dispatch_authorized') is not False:
        raise ValueError('Autonomous recovery incident permission mismatch')
    for evidence in record['evidence_refs']:
        _check_pin(evidence)
    return deepcopy(record)


def recover(controller, now=None):
    """Prepare a fresh Lead boundary, preserving all worker/research authority."""
    def result(action, reason, delay=0):
        return {'action': action, 'delay_seconds': delay, 'reason': reason}

    try:
        gate = _gate(controller)
        if gate:
            return result('STOP', gate)
        state = controller.state
        from .safety_hold import delegated_review_allowed
        # A failed QA transport needs the existing authenticated retry/cooldown
        # path. It must not reuse the cached failure envelope as a new review.
        qa_failure=_qa_failure(state) if delegated_review_allowed(state) else None
        if delegated_review_allowed(state) and qa_failure is None:
            packet=state['delegated_incident_review']
            prior=state.get('delegated_incident_wait') or {}
            if prior.get('repair_packet')==packet and prior.get('qa')==state.get('latest_qa_review') and state.get('status')!='RUNNING':
                # This needs trusted maintenance, not a provider cooldown. Yield
                # the writer to the existing heartbeat at this gated no-inflight
                # boundary; the handoff grants neither release nor a worker call.
                reason='Lead awaits changed internal repair evidence; trusted maintenance required'
                state['maintenance_handoff']={
                    'action':'MAINTENANCE_REQUIRED','reason':reason,
                    'repair_packet':deepcopy(packet),
                    'qa':deepcopy(state.get('latest_qa_review')),
                    'last_decision':state.get('last_decision'),
                    'worker_dispatch_authorized':False,
                }
                controller.save()
                return result('MAINTENANCE_REQUIRED',reason)
            state.pop('maintenance_handoff',None)
            state['delegated_incident_wait']={'repair_packet':packet,'qa':state.get('latest_qa_review')}
            state['delegated_hold_review_required']=True
            state['status']='RUNNING'
            controller.save()
            return result('RESUME','Explicit user delegation requires read-only incident QA and Lead disposition; no worker grant')
        error = state.get('error')
        # A current accepted R26 Lead prerequisite PAUSE needs maintenance even
        # after the incident was released. Re-reviewing identical inputs only
        # spends calls while the supervisor prevents that maintenance writer.
        if (state.get('status') in ('PAUSED_LEAD','ENGINEERING_BLOCKED')
                and str(error).startswith('LEAD_PREREQUISITE:')
                and not _protected(error)
                and state.get('lead_delegated_recovery_authority')):
            from .safety_hold import delegated_authority
            try:
                if delegated_authority(state) is None:
                    return result('STOP','Lead maintenance delegation is not authorized')
                ref=deepcopy(state.get('worker_dispatch_decision') or {})
                ref['path']=str(ref.get('path','')).replace('\\','/')
                path=_check_pin(ref)
                if (path.name!='accepted.json'
                        or not path.resolve().is_relative_to((RUNTIME/'decisions').resolve())
                        or ref['path']!=str(state.get('last_decision','')).replace('\\','/')):
                    return result('STOP','Maintenance requires the exact current accepted Lead decision')
                decision=_read(path)
                if (decision.get('action')!='PAUSE' or decision.get('next_job') is not None
                        or decision.get('worker_dispatch_ready') is not False
                        or state.get('pending_job')):
                    return result('STOP','Maintenance Lead pause cannot carry a worker grant')
            except RuntimeError as failure:
                return result('STOP','Lead maintenance authority requires reconciliation: '+str(failure))
            reason='Accepted Lead prerequisite PAUSE requires trusted maintenance before changed evidence review'
            state['maintenance_handoff']={
                'action':'MAINTENANCE_REQUIRED','reason':reason,
                'accepted_decision':ref,'last_decision':state.get('last_decision'),
                'repair_packet':deepcopy(state.get('delegated_incident_review')),
                'qa':deepcopy(state.get('latest_qa_review')),
                'metadata_recovery':deepcopy(state.get('operator_metadata_recovery')),
                'prerequisite_inputs_key':prerequisite_inputs_key(state),
                'worker_dispatch_authorized':False,
            }
            controller.save()
            return result('MAINTENANCE_REQUIRED',reason)
        if not error or not capture(controller, error):
            return result('STOP', 'No new demonstrated technical incident to recover')
        record = context(state)
        recovery = state['autonomous_recovery']
        now = time.time() if now is None else float(now)
        provider_failure = record['category'] in ('PROVIDER_CAPACITY', 'TRANSPORT')
        semantic_attempts = recovery['semantic_attempts'][record['semantic_fingerprint']]
        if provider_failure or semantic_attempts > 1:
            if recovery['phase'] == 'CAPTURED':
                streak = int(recovery.get('provider_failure_streak', 0)) + 1 if provider_failure else semantic_attempts - 1
                delay = min(1800, 60 * (2 ** min(streak - 1, 5)))
                recovery.update(phase='BACKOFF', not_before_unix=now + delay)
                if provider_failure:
                    recovery['provider_failure_streak'] = streak
                controller.save()
            remaining = math.ceil(recovery['not_before_unix'] - now)
            if remaining > 0:
                return result('BACKOFF', 'Technical failure cooldown before fresh Lead review', remaining)
        # Re-check exactly pinned evidence and the mutable user controls after
        # any cooldown. Only the current failed decision index can advance.
        if _gate(controller):
            return result('STOP', _gate(controller))
        context(state)
        if state.get('decisions', 0) != record['decision_index']:
            return result('STOP', 'Decision index changed since incident capture')
        target = RUNTIME / 'decisions' / f"{record['next_decision_index']:03d}"
        if record['next_decision_index'] != record['decision_index'] and target.exists():
            return result('STOP', 'Next Lead decision folder already exists; reconcile first')
        state.setdefault('error_history', []).append({
            'error': error, 'autonomous_recovery': deepcopy(recovery['current']),
            'counters_preserved': True,
        })
        state['decisions'] = record['next_decision_index']
        state['error'] = None
        state['status'] = 'RUNNING'
        state.pop('worker_dispatch_decision', None)
        if record.get('qa_failure'):
            failed = record['qa_failure']
            recovery.setdefault('qa_retries', {})[failed['job_id']] = {
                'generation': recovery['current']['fingerprint'],
                'incident': {key: recovery['current'][key] for key in ('path', 'sha256')},
                'failed_review_key': failed['failed_review_key'],
                'report': failed['report'],
            }
        recovery.update(phase='REVIEW_DISPATCHED', review_required=True)
        controller.save()
        return result('RESUME', 'Fresh Lead review required before any worker dispatch')
    except (OSError, ValueError, KeyError, TypeError) as error:
        return result('STOP', 'Recovery evidence requires reconciliation: ' + str(error))


def complete_review(controller, decision_path):
    """Pin a fresh accepted Lead decision; this does not grant a worker call."""
    state = controller.state
    recovery = state.get('autonomous_recovery', {})
    if not recovery.get('review_required'):
        return False
    record = context(state)
    path = Path(decision_path)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    folder = (RUNTIME / 'decisions' / f"{record['next_decision_index']:03d}").resolve()
    if path != folder / 'accepted.json' or str(state.get('last_decision', '')).replace('\\', '/') != path.relative_to(ROOT).as_posix():
        raise ValueError('Recovery completion requires the fresh accepted Lead decision')
    pin = _pin(path)
    if record.get('prerequisite_evidence') and _read(path).get('action') == 'PAUSE':
        recovery.setdefault('prerequisite_reviews', {})[record['prerequisite_evidence']] = pin
        # QA may have completed between recovery capture and the actual Lead
        # call. Deduplicate the inputs the Lead actually received as well.
        recovery['prerequisite_reviews'][prerequisite_inputs_key(state)] = pin
    recovery.update(phase='REVIEWED', review_required=False, decision=pin,
                    provider_failure_streak=0)
    controller.save()
    return True


def qa_retry_context(state, job_id):
    """Stable input to QA's cache key, only after a preserved transport failure."""
    if authority(state) is None:
        return None
    retry = state.get('autonomous_recovery', {}).get('qa_retries', {}).get(job_id)
    if not retry:
        return None
    generation = retry.get('generation', '')
    if len(generation) != 64 or any(character not in '0123456789abcdef' for character in generation):
        raise ValueError('QA recovery generation malformed')
    path = _check_pin(retry['incident'])
    if path.resolve() != (RUNTIME / 'autonomous_recovery' / generation / 'incident.json').resolve():
        raise ValueError('QA recovery generation does not match incident path')
    incident = _read(path)
    if incident.get('revision_id') != REVISION or incident.get('worker_dispatch_authorized') is not False:
        raise ValueError('QA recovery incident permission mismatch')
    failed = incident.get('qa_failure') or {}
    if (failed.get('job_id') != job_id or failed.get('report') != retry['report']
            or failed.get('failed_review_key') != retry['failed_review_key']):
        raise ValueError('QA recovery generation does not match preserved failure')
    for ref in failed['evidence_refs']:
        _check_pin(ref)
    return deepcopy(retry)

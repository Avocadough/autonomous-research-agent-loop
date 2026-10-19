"""One explicitly acknowledged incident; bounded probe before releasing its hold."""
import copy
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from .gate_zero import ROOT, write
from .prepare import digest
from .generic_jobs import read, relpath


def active(state):
    return bool(unresolved_holds(state.get('operator_safety_hold', {})))


def unresolved_holds(hold):
    result = []
    while hold:
        if hold.get('active'): result.append(hold)
        hold = hold.get('prior_unreleased_hold')
    return result


DELEGATED_REVISION = 'R26_LEAD_DELEGATED_RECOVERY_20261003'
GIT_CONTAINMENT_MODE = 'GIT_STORAGE_CONTAINMENT_V1'
_GIT_INCIDENT_BASE = 'research/campaigns/PAYOUT_V1/revisions/' + DELEGATED_REVISION + '/NR7_METADATA_STOP_20261003T143500Z/'
GIT_CONTAINMENT_INCIDENT = {'path': _GIT_INCIDENT_BASE + 'INCIDENT.json',
    'sha256': '5ebc78b17ab8f38ddbaf484686e9ccea97ce9639ee582f75aff14a0ae67521be'}
GIT_CONTAINMENT_PINS = {
    'lead_decision': {'path': 'research/campaigns/PAYOUT_V1/runtime/decisions/271/accepted.json',
        'sha256': 'be5edd1c09ffa96c87bd38f16e2335872b8020e96fa6aea4963839dbdb17105b'},
    'metadata_scope': {'path': _GIT_INCIDENT_BASE + 'ORIGINAL_METADATA_SCOPE_CHECK.json',
        'sha256': '99d85ec921865f62ee6c950c13142b7fe259a8131b6b744ff1ad5e37a01634e4'},
    'native_audit': {'path': _GIT_INCIDENT_BASE + 'NATIVE_AUDIT_SUMMARY.json',
        'sha256': '43e099b761d18f19b0285bf7a2aeb03db161c307fbee5288e2f7541e051779bd'},
    'original_preservation': {'path': _GIT_INCIDENT_BASE + 'PRESERVATION.json',
        'sha256': 'ec1e569d3997530275d1a3808015b06233e810e3bf8d3de34c31dc4e5863f76d'},
    'original_packet': {'path': _GIT_INCIDENT_BASE + 'INCIDENT_REVIEW_PACKET.json',
        'sha256': '64a64737e4ff81e204230f6224212ee1cd416269907a8b405c6f675a5dd573e3'}}


def incident_disposition(packet):
    """Containment is an exact incident exception, never historical restoration."""
    restoration = packet.get('history_restoration', {})
    disposition = packet.get('containment_disposition')
    if not disposition:
        return 'BYTE_EXACT_RESTORATION' if (restoration.get('passed') is True
            and restoration.get('byte_exact') is True) else None
    fail = lambda: RuntimeError('ENGINEERING_BLOCKED: Git containment evidence incomplete or changed')
    if (disposition.get('mode') != GIT_CONTAINMENT_MODE
            or packet.get('incident') != GIT_CONTAINMENT_INCIDENT
            or restoration.get('passed') is not False or restoration.get('byte_exact') is not False
            or restoration.get('status') != 'UNKNOWN'
            or packet.get('validation', {}).get('passed') is not True
            or type(packet.get('validation', {}).get('tests')) is not int
            or packet['validation']['tests'] <= 0):
        raise fail()
    required = list(GIT_CONTAINMENT_PINS.values()) + [disposition.get('object_integrity'), disposition.get('preservation')]
    if any(pin not in packet.get('proof_files', []) for pin in required):
        raise fail()
    proofs = {key: read(_delegated_pin(pin)) for key, pin in GIT_CONTAINMENT_PINS.items()}
    scope, audit, original = proofs['metadata_scope'], proofs['native_audit'], proofs['original_preservation']
    if (proofs['lead_decision'].get('action') != 'PAUSE'
            or GIT_CONTAINMENT_INCIDENT['path'] not in proofs['lead_decision'].get('artifact_refs', [])
            or packet.get('history_artifacts') != proofs['original_packet'].get('history_artifacts')
            or audit.get('write_policy_valid') is not True or audit.get('audit_error') is not None
            or any(audit.get(key) != [] for key in ('errors', 'protocol_errors', 'unparsed_tools'))
            or not audit.get('commands') or audit.get('edits_count') != 5):
        raise fail()
    _delegated_pin(audit['raw']); _delegated_pin(audit['pretool_policy'])
    before, after = (read(_delegated_pin(scope[key])) for key in ('before', 'after'))
    changes = {p: {'before': before.get(p), 'after': after.get(p)}
               for p in sorted(set(before) | set(after)) if before.get(p) != after.get(p)}
    job_pin = next((pin for pin in original.get('pins', []) if pin['path'].endswith('/job.json')), None)
    job = read(_delegated_pin(job_pin))
    folder = str(Path(job_pin['path']).parent).replace('\\', '/')
    allowed = set(job['allowed_file_scope']) | {'research/campaigns/PAYOUT_V1/runtime/state.json', scope['before']['path']}
    unexpected = [p for p in changes if p not in allowed and not p.startswith(folder + '/worker_call_0/')]
    if changes != scope.get('changes') or unexpected != scope.get('unexpected') or len(unexpected) != 770:
        raise fail()
    added = 0
    for name in unexpected:
        if not re.fullmatch(r'\.git/objects/(?:[0-9a-f]{2}/[0-9a-f]{38}|pack/pack-[0-9a-f]{40}\.pack)', name):
            raise fail()
        left, right = before.get(name), after.get(name)
        if not right or right[3] != 'FILE':
            raise fail()
        if left is None:
            if '/pack/' in name:
                raise fail()
            added += 1
        elif len(left) != 4 or any(left[i] != right[i] for i in (0, 2, 3)):
            raise fail()
    if added != 8:
        raise fail()
    integrity = read(_delegated_pin(disposition['object_integrity']))
    rows = integrity.get('rows', [])
    if (integrity.get('verdict') != 'CURRENT_CONTENT_IDENTITIES_VERIFIED'
            or integrity.get('historical_restoration') != 'UNKNOWN'
            or integrity.get('protected_metadata_changes') != []
            or len(rows) != 770 or {row.get('path') for row in rows} != set(unexpected)):
        raise fail()
    for row in rows:
        if (row.get('current_content_identity') != 'PASS'
                or row.get('saved_metadata_before') != before.get(row['path'])
                or row.get('saved_metadata_after') != after.get(row['path'])):
            raise fail()
        current_path = ROOT
        for part in Path(row['path']).parts:
            current_path = current_path / part
            if current_path.is_symlink() or current_path.is_junction():
                raise fail()
        _delegated_pin({'path': row['path'], 'sha256': row.get('file_sha256')}, ROOT / '.git/objects')
    for pin in original['pins']:
        _delegated_pin(pin)
    result_pin = next(pin for pin in original['pins'] if pin['path'].endswith('/work/result.json'))
    result = read(_delegated_pin(result_pin))
    preserved = read(_delegated_pin(disposition['preservation']))
    if (original.get('call_spent') is not True or original.get('result_accepted') is not False
            or original.get('new_worker_allowance') is not False or original.get('counter_changes') is not False
            or result.get('payload', {}).get('overall_status') != 'FAIL_RETURN_TO_LEAD'
            or any(preserved.get(key) is not False for key in
                   ('result_accepted', 'worker_allowance_added', 'counters_reset'))
            or preserved.get('call_spent') is not True or preserved.get('original_pins') != original['pins']):
        raise fail()
    return GIT_CONTAINMENT_MODE


def _delegated_pin(pin, base=None, verify_hash=True):
    if not isinstance(pin, dict) or not pin.get('path') or not pin.get('sha256'):
        raise RuntimeError('ENGINEERING_BLOCKED: delegated incident proof pin missing')
    path = relpath(pin['path'])
    if (base is not None and not path.is_relative_to(base.resolve())) or (verify_hash and digest(path) != pin['sha256']):
        raise RuntimeError('ENGINEERING_BLOCKED: delegated incident proof pin mismatch')
    return path


def delegated_authority(state):
    """Direct user delegation permits incident review, never a worker grant."""
    pin = state.get('lead_delegated_recovery_authority')
    if not pin:
        return None
    base = ROOT / 'research/campaigns/PAYOUT_V1/revisions' / DELEGATED_REVISION
    path = _delegated_pin(pin, base)
    if path != (base / 'AUTHORIZATION.json').resolve():
        raise RuntimeError('HUMAN_ACTION_REQUIRED: delegated authority location mismatch')
    value = read(path)
    expected = {'approved': True, 'campaign_id': 'PAYOUT_V1', 'revision_id': DELEGATED_REVISION,
                'lead_model': 'gpt-6.1-sol', 'lead_reasoning': 'ultra', 'global_call_cap': 1000,
                'spend_usd': 0, 'live': 'DISABLED', 'delegate_internal_incident_disposition': True,
                'preserve_user_stop': True, 'preserve_data_and_financial_permissions': True}
    if any(value.get(key) != wanted for key, wanted in expected.items()):
        raise RuntimeError('HUMAN_ACTION_REQUIRED: delegated authority scope mismatch')
    source = _delegated_pin({'path': value['source'], 'sha256': value['source_sha256']}, base)
    if source != (base / 'USER_AUTHORIZATION.txt').resolve() or not source.read_text(encoding='utf-8').strip():
        raise RuntimeError('HUMAN_ACTION_REQUIRED: delegated user authorization missing')
    return value


def _delegated_stopped(state):
    runtime = ROOT / 'research/campaigns/PAYOUT_V1/runtime'
    from .runtime_policy import used_call_units
    return (state.get('inflight_call')
            or state.get('status') in ('PAUSED_USER', 'CANCELLED', 'CANCELED', 'DATA_OR_RULES_BLOCKED',
                                       'RESOURCE_LIMIT', 'PAUSED_BUDGET', 'READY_FOR_FORWARD',
                                       'NO_QUALIFYING_CANDIDATE')
            or any((runtime / name).exists() for name in ('pause.request', 'cancel.request'))
            or used_call_units(state) >= 1000 or state.get('live', 'DISABLED') != 'DISABLED')


def delegated_review_packet(state, require_repaired=False):
    """Validate the controller's repaired internal incident evidence in place."""
    pin = state.get('delegated_incident_review')
    if not pin:
        raise RuntimeError('ENGINEERING_BLOCKED: delegated incident repair packet missing')
    packet = read(_delegated_pin(pin, ROOT / 'research/campaigns/PAYOUT_V1/revisions'))
    holds = unresolved_holds(state.get('operator_safety_hold', {}))
    incident = packet.get('incident', {})
    if (len(holds) != 1 or incident.get('path') != holds[0].get('incident')
            or incident.get('sha256') != holds[0].get('incident_sha256')
            or packet.get('scope') != 'INTERNAL_PROJECT_INCIDENT'
            or packet.get('protected_permissions_affected') is not False
            or packet.get('user_stop') is not False or packet.get('no_inflight') is not True):
        raise RuntimeError('HUMAN_ACTION_REQUIRED: delegated internal incident repair scope mismatch')
    _delegated_pin(incident)
    if not packet.get('source_hashes'):
        raise RuntimeError('ENGINEERING_BLOCKED: delegated incident repair proofs incomplete')
    if require_repaired and (packet.get('validation', {}).get('passed') is not True
            or incident_disposition(packet) is None
            or not packet.get('history_artifacts') or not packet.get('proof_files')):
        raise RuntimeError('ENGINEERING_BLOCKED: delegated incident repairs are not verified')
    for name, sha in packet['source_hashes'].items():
        _delegated_pin({'path': name, 'sha256': sha})
    for proof in packet.get('history_artifacts', []):
        # Read-only QA reports expected/actual history hashes without treating
        # unrepaired bytes as accepted evidence. Release always enforces them.
        _delegated_pin(proof, verify_hash=require_repaired)
    for proof in packet.get('proof_files', []):
        _delegated_pin(proof)
    if holds[0].get('restoration'):
        _delegated_pin(holds[0]['restoration'])
        if require_repaired and holds[0]['restoration'] not in packet.get('proof_files', []):
            raise RuntimeError('ENGINEERING_BLOCKED: exact hold restoration proof missing')
    return packet


def delegated_review_allowed(state):
    if not active(state) or delegated_authority(state) is None or _delegated_stopped(state):
        return False
    delegated_review_packet(state)
    guard_auxiliary(state)
    return True


def release_authorities(state, auth):
    """No nested incident disappears just because the outer hold is released."""
    primary = auth['incident']
    coverage = {primary['path']: {'incident': primary,
        'authorization': state['operator_safety_recovery']['authorization']}}
    for item in auth.get('acknowledged_prior_incidents', []):
        ref = item['authorization']; path = relpath(ref['path'])
        if digest(path) != ref['sha256']: raise RuntimeError('ENGINEERING_BLOCKED: prior acknowledgement changed')
        prior = read(path)
        incident = item['incident']
        if (prior.get('incident') != incident or prior.get('global_call_cap') != 1000
                or prior.get('live') != 'DISABLED' or prior.get('spend_usd') != 0
                or prior.get('resume_after_new_breach') is not False):
            raise RuntimeError('ENGINEERING_BLOCKED: prior acknowledgement does not cover incident')
        coverage[incident['path']] = item
    result=[]
    for hold in unresolved_holds(state['operator_safety_hold']):
        item = coverage.get(hold['incident'])
        if not item or digest(relpath(hold['incident'])) != item['incident']['sha256']:
            raise RuntimeError('ENGINEERING_BLOCKED: unresolved prior hold lacks exact acknowledgement')
        result.append(item)
    return result


def authority(state):
    if not active(state):
        return None
    ref = state.get('operator_safety_recovery')
    if not ref or ref.get('released') or ref.get('revoked_due_to'):
        raise RuntimeError('HUMAN_ACTION_REQUIRED: active operator safety hold')
    base = (ROOT / 'research/campaigns/PAYOUT_V1/revisions').resolve()
    values = []
    for key in ('authorization', 'preflight'):
        pin = ref[key]
        path = relpath(pin['path'])
        if not path.is_relative_to(base) or digest(path) != pin['sha256']:
            raise RuntimeError('HUMAN_ACTION_REQUIRED: safety recovery pin mismatch')
        values.append(read(path))
    auth, checks = values
    incident = auth['incident']
    if (incident['path'] != state['operator_safety_hold']['incident']
            or digest(relpath(incident['path'])) != incident['sha256']
            or checks.get('passed') is not True
            or checks.get('authorization_sha256') != ref['authorization']['sha256']
            or auth.get('global_call_cap') != 1000 or auth.get('live') != 'DISABLED'
            or auth.get('spend_usd') != 0 or auth.get('resume_after_new_breach') is not False):
        raise RuntimeError('HUMAN_ACTION_REQUIRED: safety recovery authority mismatch')
    incident_value = read(relpath(incident['path']))
    preserved = auth.get('preserve_artifacts')
    if preserved is None:
        preserved = [{'path': incident_value['actual_unexpected_path'],
                      'sha256': incident_value['actual_unexpected_sha256']}]
    for pin in preserved:
        if digest(relpath(pin['path'])) != pin['sha256']:
            raise RuntimeError('HUMAN_ACTION_REQUIRED: preserved incident artifact changed')
    for path, sha in checks['source_hashes'].items():
        if digest(relpath(path)) != sha:
            raise RuntimeError('HUMAN_ACTION_REQUIRED: recovery source changed after preflight')
    return auth


def validate_probe(state, job, auth):
    ref = state['operator_safety_recovery']
    allowed = set(auth['route_probe_inputs'])
    if (job.get('job_class') != 'DIAGNOSTIC' or job.get('implementation')
            or job.get('dependencies') or not job.get('inputs')
            or any(i['path'] not in allowed or i['partition'] != 'POLICY_OR_METADATA'
                   for i in job['inputs'])
            or ref.get('job_id') not in (None, job['job_id'])
            or auth.get('route_probe_job_id', job['job_id']) != job['job_id']
            or (job.get('recovery_episode') or {}).get('max_attempts') != 1):
        raise ValueError('Hold permits only its bounded nonmarket route probe')


def guard_worker(controller, job):
    guard_auxiliary(controller.state)
    auth = authority(controller.state)
    if auth is None:
        return
    validate_probe(controller.state, job, auth)
    ref = controller.state['operator_safety_recovery']
    ref['job_id'] = job['job_id']
    controller.save()


def guard_auxiliary(state):
    for key, incident in state.get('operator_auxiliary_incidents', {}).items():
        if incident.get('resolved') is not True:
            raise RuntimeError('HUMAN_ACTION_REQUIRED: unresolved auxiliary runtime scope incident ' + key)
        ref=incident.get('resolution') or {}
        if not ref.get('path') or digest(relpath(ref['path']))!=ref.get('sha256'):
            raise RuntimeError('HUMAN_ACTION_REQUIRED: auxiliary resolution receipt missing or changed')
        receipt=read(relpath(ref['path']))
        if (receipt.get('incident_id')!=key or receipt.get('verified_outcome') not in ('NO_WRITE','ACKNOWLEDGED_WRITE')
                or receipt.get('authorization_after_incident') is not True):
            raise RuntimeError('HUMAN_ACTION_REQUIRED: auxiliary incident lacks verified disposition and later authorization')


def guard_call(controller, folder):
    if delegated_review_allowed(controller.state):
        runtime = (ROOT / 'research/campaigns/PAYOUT_V1/runtime').resolve()
        relative = folder.resolve().relative_to(runtime)
        if relative.parts and relative.parts[0] in ('decisions', 'qa', 'skill_engineering'):
            return
    auth = authority(controller.state)
    if auth is None:
        return
    ref = controller.state['operator_safety_recovery']
    relative = folder.resolve().relative_to((ROOT / 'research/campaigns/PAYOUT_V1/runtime').resolve())
    if relative.parts[0] in ('decisions', 'qa'):
        return
    if relative.parts[0] == 'jobs' and folder.name.startswith('worker_call_'):
        guard_worker(controller, controller.state['pending_job'])
        cap = auth.get('max_route_worker_calls', 1)
        if cap not in (1, 2):
            raise RuntimeError('HUMAN_ACTION_REQUIRED: invalid scoped recovery call authority')
        episode_key = None
        if auth.get('lead_scoped_recovery_authorized') is True:
            # R16 delegates routine recovery to the real Lead while keeping the
            # existing episode and global ledgers. A new name is not a reset.
            approved = controller.state.get('lead_recovery_authorizations', {}).get(ref['job_id'], {})
            episode = approved.get('episode', {})
            if (approved.get('decision_index', -1) < auth['lead_recovery_min_decision_index']
                    or episode.get('max_attempts') not in (1, 2)):
                raise RuntimeError('ENGINEERING_BLOCKED: fresh bounded Lead recovery required')
            episode_key = hashlib.sha256(json.dumps({k: episode[k] for k in
                ('component', 'root_cause', 'changed_approach')}, sort_keys=True).encode()).hexdigest()
            reserved = controller.state.get('recovery_episodes', {}).get(episode_key, {})
            calls = ref.setdefault('lead_episode_worker_calls', {}).get(episode_key, 0)
            if (reserved.get('job_id') != ref['job_id'] or calls >= episode['max_attempts']
                    or len(reserved.get('attempts', [])) != calls + 1):
                raise RuntimeError('ENGINEERING_BLOCKED: route recovery episode not reserved or exhausted')
        if (relative.parts[1] != ref['job_id']
                or (ref.get('worker_calls_reserved', 0) >= cap and episode_key is None)):
            raise RuntimeError('ENGINEERING_BLOCKED: acknowledged route probe call exhausted; no blind retry')
        ref['worker_calls_reserved'] = ref.get('worker_calls_reserved', 0) + 1
        if episode_key is not None:
            ref['lead_episode_worker_calls'][episode_key] = calls + 1
        return
    raise RuntimeError('HUMAN_ACTION_REQUIRED: call outside acknowledged safety recovery')


def release_delegated_after_review(controller, decision):
    """Release only the exact repaired hold after accepted QA and Lead disposition."""
    state = controller.state
    if decision.get('action') != 'RELEASE_REPAIRED_HOLD' or not active(state):
        return False
    if (decision.get('next_job') is not None or state.get('pending_job')
            or decision.get('worker_dispatch_ready') is not False
            or not delegated_review_allowed(state)):
        raise RuntimeError('HUMAN_ACTION_REQUIRED: delegated release cannot dispatch or override a stop')
    packet = delegated_review_packet(state, require_repaired=True)
    packet_ref = state['delegated_incident_review']
    incident = packet['incident']
    qa = state.get('latest_qa_review') or {}
    report_path = _delegated_pin(qa, ROOT / 'research/campaigns/PAYOUT_V1/runtime/qa')
    report = read(report_path)
    incident_value = read(_delegated_pin(incident))
    facets = report.get('facets', [])
    from .qa_review import FACETS
    if (qa.get('incident_review') is not True or qa.get('incident') != incident
            or qa.get('repair_packet') != packet_ref or qa.get('verdict') != 'PASS'
            or qa.get('requested_model') != 'gpt-6.1-sol' or qa.get('requested_reasoning') != 'high'
            or report.get('verdict') != 'PASS' or report.get('job_id') != incident_value.get('incident_id')
            or qa.get('job_id') != report.get('job_id') or qa.get('review_key') != report.get('review_key')
            or sorted(x.get('facet', '') for x in facets) != sorted(FACETS)
            or any(x.get('status') != 'PASS' for x in facets)
            or any(x.get('severity') != 'minor' for x in report.get('findings', []))
            or packet_ref['path'] not in report.get('reviewed_files', [])):
        raise RuntimeError('ENGINEERING_BLOCKED: delegated incident lacks exact independent QA acceptance')
    ref = state.get('worker_dispatch_decision') or {}
    accepted_ref = dict(ref, path=str(ref.get('path', '')).replace('\\', '/'))
    accepted_path = _delegated_pin(accepted_ref, ROOT / 'research/campaigns/PAYOUT_V1/runtime/decisions')
    if (accepted_path.name != 'accepted.json'
            or ref['path'] != state.get('last_decision') or read(accepted_path) != decision
            or decision.get('evidence_id') != state.get('evidence_id')
            or not {incident['path'], packet_ref['path'], qa['path']}.issubset(decision.get('artifact_refs', []))):
        raise RuntimeError('ENGINEERING_BLOCKED: delegated disposition is not the current accepted Lead decision')
    # A saved accepted JSON is not sufficient without the successful native
    # request/output pair that the controller accepted for this decision.
    lead_proof = None
    auth = delegated_authority(state)
    for call in accepted_path.parent.glob('call_*'):
        suffix = call.name.removeprefix('call_')
        if not suffix.isdigit():
            continue
        request, receipt, output = call / 'request.json', call / 'receipt.json', accepted_path.parent / ('decision_' + suffix + '.json')
        if not all(p.is_file() for p in (request, receipt, output)):
            continue
        argv = read(request).get('argv', [])
        terminal = read(receipt)
        if ('-m' in argv and argv.index('-m') + 1 < len(argv)
                and argv[argv.index('-m') + 1] == auth['lead_model']
                and 'model_reasoning_effort="' + auth['lead_reasoning'] + '"' in argv
                and '-s' in argv and argv.index('-s') + 1 < len(argv)
                and argv[argv.index('-s') + 1] == 'read-only'
                and terminal.get('exit_code') == 0 and terminal.get('process_reaped') is True
                and read(output) == decision):
            lead_proof = [{'path': p.relative_to(ROOT).as_posix(), 'sha256': digest(p)}
                          for p in (request, receipt, output)]
            break
    if lead_proof is None:
        raise RuntimeError('ENGINEERING_BLOCKED: successful authorized Lead disposition receipt missing')
    previous = copy.deepcopy(state['operator_safety_hold'])
    from .runtime_policy import used_call_units
    path = accepted_path.parent / 'delegated_hold_release.json'
    if path.exists():
        raise RuntimeError('ENGINEERING_BLOCKED: delegated release receipt already exists')
    proof = {'revision_id': DELEGATED_REVISION, 'incident': incident, 'prior_hold': previous,
             'authorization': state['lead_delegated_recovery_authority'], 'repair_packet': packet_ref,
             'independent_qa': qa, 'lead_decision': ref, 'lead_call_proofs': lead_proof,
             'call_units_used': used_call_units(state), 'counters_reset': False,
             'historical_result_reclassified': False, 'worker_dispatch_authorized': False,
             'incident_disposition': incident_disposition(packet),
             'history_restoration': packet.get('history_restoration'),
             'live': 'DISABLED', 'spend_usd': 0,
             'recorded_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    write(path, proof)
    release = {'path': path.relative_to(ROOT).as_posix(), 'sha256': digest(path)}
    state.setdefault('operator_safety_hold_history', []).append(dict(previous, release=release))
    updated = copy.deepcopy(previous)
    node = updated
    while node:
        if node.get('active') and node.get('incident') == incident['path']:
            node['active'] = False
            node['release'] = release
        node = node.get('prior_unreleased_hold')
    state['operator_safety_hold'] = updated
    state['delegated_incident_review_release'] = release
    controller.save()
    return True


def release_after_review(controller, decision):
    state = controller.state
    # A real Lead stop must persist as that stop, not be masked by a failed
    # release attempt. Acceptance of negative evidence is not hold release.
    if decision.get('action') in ('HUMAN_ACTION_REQUIRED', 'PAUSE', 'RESOURCE_LIMIT'):
        return False
    auth = authority(state)
    if auth is None:
        return False
    ref = state['operator_safety_recovery']
    jid = ref.get('job_id')
    record = state.get('completed', {}).get(jid)
    if not record or decision.get('evidence_id') != jid:
        return False
    qa = state.get('latest_qa_review', {})
    if (qa.get('job_id') != jid or qa.get('verdict') != 'PASS'
            or state.get('reviewed_results', {}).get(jid) != record['sha256']
            or digest(relpath(record['path'])) != record['sha256']
            or digest(relpath(qa['path'])) != qa['sha256']):
        raise RuntimeError('ENGINEERING_BLOCKED: safety route probe lacks complete QA and Lead acceptance')
    folder = ROOT / 'research/campaigns/PAYOUT_V1/runtime/jobs' / jid
    import re
    match = re.fullmatch(r'verification_(\d+)\.json', Path(record['verification']).name)
    if not match:
        raise RuntimeError('ENGINEERING_BLOCKED: verified worker attempt identity missing')
    attempt = match[1]
    scope = read(folder / f'scope_check_{attempt}.json')
    receipt = read(folder / f'worker_call_{attempt}/receipt.json')
    verification = read(relpath(record['verification']))
    # Controller-owned references use native separators on Windows. Keep the
    # external contract path validator strict after canonicalizing this field.
    accepted = read(relpath(state['last_decision'].replace('\\', '/')))
    if auth.get('worker_command_policy') == 'SCOPED_LITERAL_V1':
        native = read(folder / f'worker_call_{attempt}/native_policy_and_commands.json')
        broad = read(folder / f'metadata_scope_check_{attempt}.json')
        if (native.get('errors') or native.get('unparsed_tools') or native.get('protocol_errors')
                or native.get('write_policy_valid') is not True or broad.get('unexpected') != []):
            raise RuntimeError('HUMAN_ACTION_REQUIRED: native policy or complete scope check failed')
        if state.get('native_worker_preturn_gate') and not native.get('pretool_policy'):
            raise RuntimeError('ENGINEERING_BLOCKED: native pre-tool policy proof missing')
    if (scope.get('unexpected') != [] or receipt.get('exit_code') != 0
            or verification.get('exit_code') != 0 or accepted != decision):
        raise RuntimeError('HUMAN_ACTION_REQUIRED: acknowledged roundtrip scope or receipt failed')
    guard_auxiliary(state)
    covered = release_authorities(state, auth)
    previous = copy.deepcopy(state['operator_safety_hold'])
    path = relpath(ref['authorization']['path']).parent / 'RECOVERY_RELEASE_RECEIPT.json'
    from .runtime_policy import used_call_units
    proof = {'incident': auth['incident'], 'prior_hold': previous,
             'authorization': ref['authorization'], 'preflight': ref['preflight'],
             'job_id': jid, 'verified_result': record, 'sol_qa': qa,
             'lead_decision': state['last_decision'], 'call_units_used': used_call_units(state),
             'counters_reset': False, 'live': 'DISABLED',
             'recorded_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
             'released_incidents': covered,
             'claim_limit': 'Current verified scoped roundtrip resolves acknowledged route conditions only. Earlier failed artifacts remain failed/incomplete; no retrospective result acceptance.'}
    write(path, proof)
    release = {'path': path.relative_to(ROOT).as_posix(), 'sha256': digest(path)}
    state.setdefault('operator_safety_hold_history', []).append(dict(previous, release=release))
    updated=copy.deepcopy(previous)
    node=updated
    while node:
        if node.get('active'):
            item=next(i for i in covered if i['incident']['path']==node['incident'])
            incident_path=path.with_name('RECOVERY_RELEASE_'+item['incident']['sha256'][:16]+'.json')
            write(incident_path,{'incident':item['incident'],'acknowledgement':item['authorization'],
                'current_recovery_authorization':ref['authorization'],'roundtrip_release':release,
                'prior_hold':copy.deepcopy(node),'historical_result_reclassified':False})
            node['active']=False
            node['release']={'path':incident_path.relative_to(ROOT).as_posix(),'sha256':digest(incident_path)}
        node=node.get('prior_unreleased_hold')
    state['operator_safety_hold'] = updated
    ref['released'] = True
    controller.save()
    return True

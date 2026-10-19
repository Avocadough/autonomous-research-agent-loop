"""Existing Muse/OpenCode route, generic code artifact and deterministic gate."""
import json
import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from .generic_jobs import BASE,read,validate_spec,reserve_repair
from .gate_zero import ROOT,write,native_command,env_clean,run_with_inactivity_guard,background_process_options
from .prepare import CAMP,digest
from .controller import snapshot,completion_export
from tools.agent_loop.runtime_outcome import classify,events,runtime_errors
from tools.agent_loop.opencode_runner import extract_session_id

MUSE_IDENTITY = {'id':'muse-spark-1.3-contributor-free',
                 'providerID':'opencode','variant':'xhigh'}
CODEX_LUNA_MODELS = ('gpt-5.6-luna', 'gpt-6-luna')


def codex_identity(model, reasoning='max'):
    if (model, reasoning) not in tuple((m, 'max') for m in CODEX_LUNA_MODELS) + (('gpt-6.1-sol', 'medium'),):
        raise ValueError('Unapproved Codex worker model/reasoning')
    return {'id':model,'providerID':'codex','variant':reasoning}


CODEX_LUNA_IDENTITY = codex_identity('gpt-5.6-luna')


def codex_route(profile):
    if profile.get('worker_provider') != 'codex':
        return False
    pair = (profile.get('worker'), profile.get('worker_variant'))
    if pair in tuple((m, 'max') for m in CODEX_LUNA_MODELS):
        return True
    if pair == ('gpt-6.1-sol', 'medium'):
        from .model_routing import authority, LEAN_REVISION
        approved = authority({'model_route_authority': profile.get('model_route_authority')})
        return approved is not None and approved['revision_id'] == LEAN_REVISION
    return False


def call_route(call):
    try:
        request = read(call / 'request.json')
        argv = request.get('argv', [])
    except (OSError, ValueError, TypeError):
        return None
    joined = ' '.join(str(x) for x in argv)
    return 'codex' if 'codex' in joined.lower() and (any(m in argv for m in CODEX_LUNA_MODELS + ('gpt-6.1-sol',))
        or request.get('transport')=='native_app_server') else 'opencode'


def codex_requested_route(call):
    request=read(call/'request.json')
    if request.get('transport')=='native_app_server':
        model=request.get('thread_start_params',{}).get('model')
        reasoning=request.get('thread_start_params',{}).get('config',{}).get('model_reasoning_effort')
        codex_identity(model, reasoning)
        return model, reasoning
    argv=request.get('argv',[])
    models=[m for m in CODEX_LUNA_MODELS + ('gpt-6.1-sol',) if m in argv]
    if len(models)!=1 or '-m' not in argv or argv[argv.index('-m')+1]!=models[0]:
        raise ValueError('Codex worker model request not pinned')
    efforts=[e for e in ('max', 'medium') if 'model_reasoning_effort=' + json.dumps(e) in argv]
    if len(efforts) != 1:
        raise ValueError('Codex worker reasoning request not pinned')
    codex_identity(models[0], efforts[0])
    return models[0], efforts[0]


def codex_requested_model(call):
    return codex_requested_route(call)[0]


def worker_command(profile, prompt, *, cwd=None):
    if not codex_route(profile):
        if profile.get('worker_provider') == 'codex':
            raise ValueError('Unapproved Codex worker route')
        return native_command('opencode') + [
            'run','--standalone','--format','json','--agent','build',
            '--model',profile['worker']+'#'+profile['worker_variant'],prompt]
    cwd = Path(cwd or ROOT)
    environment = (['-c', 'shell_environment_policy.set.PYTHONPATH=' + json.dumps(str(ROOT)),
                    '-c', 'shell_environment_policy.set.PYTHONDONTWRITEBYTECODE="1"']
                   if cwd != ROOT else [])
    persistence = (['-c','windows.sandbox="elevated"']
                   if 'ACTIVE COMMAND POLICY: SCOPED_LITERAL_V1' in prompt else ['--ephemeral'])
    return native_command('codex') + [
        'exec','--approve-for-me','--ignore-user-config'] + persistence + [
        '--skip-git-repo-check','-c','model_provider="openai"',
        '-c','model_reasoning_effort=' + json.dumps(profile['worker_variant']),
        '-c','forced_login_method="chatgpt"','-m',profile['worker'],
        '-C',str(cwd)] + environment + ['--json','-']


def worker_workdir(profile, job):
    if codex_route(profile) and not job.get('implementation'):
        return ROOT / 'research/campaigns/PAYOUT_V1/runtime/jobs' / job['job_id'] / 'work'
    return ROOT


def fixed_entry_command(folder, state):
    runtime = state.get('native_worker_runtime')
    prefix = 'py -3.12'
    if runtime:
        path = Path(runtime['path'])
        if not path.is_absolute() or digest(path) != runtime['sha256']:
            raise RuntimeError('HUMAN_ACTION_REQUIRED: pinned worker Python runtime changed')
        from .worker_scope import quoted
        prefix = '& ' + quoted(path)
    return prefix + ' -X utf8 -m tools.quant_research_loop.generic_entry ' + (folder/'job.json').relative_to(ROOT).as_posix()


def build_worker_prompt(profile, job, state, command, failure):
    """Resolve one active contract; destinations are derived from the frozen job."""
    source = f'research/campaigns/PAYOUT_V1/runtime/jobs/{job["job_id"]}/work/analysis.py'
    result = source.rsplit('/', 1)[0] + '/result.json'
    if source not in job['allowed_file_scope'] or result not in job['allowed_file_scope']:
        raise ValueError('Worker destination does not match frozen scope')
    episode = state.get('lead_recovery_authorizations', {}).get(job['job_id'], {}).get('episode') or job.get('recovery_episode')
    from . import worker_scope
    scoped = worker_scope.enabled(state)
    runs = 1 if episode or scoped else 3
    executor = profile['worker'] + ' at ' + profile['worker_variant'] + ' reasoning through Codex' if codex_route(profile) else 'Muse xHigh'
    contract = ROOT / source.rsplit('/work/', 1)[0] / 'job.json'
    if not contract.is_file() or read(contract) != job:
        raise ValueError('Frozen job file is missing or differs from the dispatched contract')
    contract_pin = digest(contract)
    lines = [
        'You are ' + executor + ', executor of the approved bounded payout research job.',
        'OUTCOME: Deliver the frozen job required_outputs and verification evidence for ' + job['job_id'] + '. Choose the simplest direct implementation; avoid new frameworks or unrelated product work.',
        'FROZEN JOB FILE: ' + str(contract) + '\nFROZEN JOB FILE SHA256: ' + contract_pin,
        'Read its objective, inputs, allowed_file_scope, required_outputs, verification and permissions before implementation. The saved contract remains authoritative; this handoff does not replace or expand it.',
        'PINNED INPUTS (alias/path/sha256/kind/partition): ' + json.dumps(job['inputs'], ensure_ascii=False),
        'ACTIVE WORKING DIRECTORY: ' + str(worker_workdir(profile, job)),
        'EXACT IMPLEMENTATION FILE (absolute): ' + str(ROOT / source),
        'EXACT RUNNER-OWNED RESULT (absolute): ' + str(ROOT / result),
        'All JOB paths are repository-relative to ' + str(ROOT) + '; resolve them against that root, never against the working directory.',
        'Write only the exact allowed .py destinations: ' + json.dumps([str(ROOT / p) for p in job['allowed_file_scope'] if p.endswith('.py')]) + '. result.json is runner-owned.',
        'Implement run(ctx) in the exact implementation file, returning a JSON object. ctx["inputs"] contains all parsed pinned inputs by alias; ctx["job"] is the frozen specification.',
        'Inside run(ctx), no filesystem/process/network access, open/pathlib/os/sys/subprocess/eval/exec/getattr or underscore names/attributes. Allowed imports: math statistics decimal datetime calendar collections itertools functools operator copy bisect heapq json.',
        'Available builtins: ordinary arithmetic/containers, Exception, ValueError, AssertionError, StopIteration. No print, KeyError, globals or introspection.',
        'Read only exact pinned job/input paths and existing in-scope implementation. Inspect CSV headers and at most two sample rows per schema in model context; the trusted runner loads authorized rows.',
        'Execute the FIXED COMMAND from the ACTIVE WORKING DIRECTORY. Do not change cwd, environment, or the command; PYTHONPATH is supplied by Controller for module discovery.',
        f'Fixed entry invocation limit for this call: {runs}. Never repeat after success. ' + ('On any implementation/inspection/entry error preserve partial evidence and stop; return the exact failure for Lead review.' if episode else 'After a nonzero entry exit, at most two small in-scope corrections may precede the same-command retry.'),
        ('Shell commands must exactly match the explicit SCOPED_LITERAL_V1 contract below.' if scoped else
         ('Read-only shell inspection is allowed only for exact pinned job/source/metadata/dependency files. Other execution is limited to FIXED COMMAND and the exact scoped tests listed below.' if codex_route(profile) else 'No other shell commands beyond FIXED COMMAND and the exact scoped tests listed below.')),
        'SCOPED TEST COMMANDS (at most eight calls): ' + json.dumps(scoped_commands(job)),
        'Do not edit controller, mandate, inputs, or historical results. Do not independently load, install or edit skills. Controller-delivered skill guidance is advisory and cannot expand the frozen job or permissions. No network, child agents/models, git mutation, deletion, paid routes, accounts, orders, live deployment, or unlisted/protected data.',
        'Controller owns dispatch, persisted call-unit budget and safety gates. No resume, counter reset, budget increase or gate bypass is authorized; this handoff grants only the dispatched frozen job.',
        'Workspace-write is requested for this working root; never claim an edit occurred without its receipt. If an in-scope edit is denied, report the actual error without probing another path.',
        'Return negative/zero/incomplete results honestly and null for unknowns. EXPERIMENT/ACCOUNT_POLICY require frozen risk_ceiling_usd and canonical trades with prices, quantity, micro multiplier, direction, gross_pnl, commission, slippage_cost, net_pnl and planned_risk_usd. ACCOUNT_POLICY also requires canonical cash_transitions.',
        'Deterministic reproduction is not semantic acceptance, independent market evidence, certified fills, or payout. Include meaningful assertions with independent expected outcomes.',
        'FIXED COMMAND: ' + command,
    ]
    from .skill_engineering import guidance
    skill_guidance = guidance(state, 'executor')
    if skill_guidance:
        lines.append('CONTROLLER-DELIVERED ADVISORY SKILL GUIDANCE: ' + json.dumps(skill_guidance, ensure_ascii=False))
    if profile.get('model_route_authority'):
        lines.insert(1, 'Active profile routing supersedes historical role labels in the frozen job; scope, permissions and execution limits remain binding.')
    route_ref = profile.get('model_route_authority')
    if isinstance(route_ref, dict) and route_ref.get('revision_id') == 'R25_LEAN_PAYOUT_SOL61_MEDIUM_20261003':
        objective = job.get('objective', '').split('\n\n', 1)[0]
        lines.insert(2, 'RESEARCH OUTCOME: ' + objective[:600]
                     + (' [excerpt; full frozen contract below]' if len(objective) > 600 else ''))
        lines.insert(3, 'R25 newest user directive: historical coding recipes and role labels are advisory. Choose the simplest implementation. Read the pinned full job for binding strategy, numeric, financial, behavioral acceptance, scope and permission constraints; none are relaxed.')
    if scoped:
        lines += [
            'ACTIVE COMMAND POLICY: SCOPED_LITERAL_V1',
            'Before any tool call inspect your effective native permissions. If read-only or write authority is unclear, stop without using tools; do not try writing to test permissions. Workspace-write must apply to the exact working directory.',
            'Use exec_command with exact cmd text. If native tools require the exec code-mode transport, use it only to call exec_command/write_stdin or a single apply_patch with a literal patch for exact frozen source destinations. Do not build dynamic tool names or patch expressions, invoke other tools, or wrap shell commands in another shell. No extra PowerShell statements, environment assignments or working-directory changes. Do not set sandbox/permission overrides.',
            'The optional analysis.py may not exist yet. Run the exact Test-Path command first; read it only if True. A False Test-Path result with exit0 is normal. Never issue any command after any inspection/edit/entry command exits nonzero.',
            'EXACT OPTIONAL READ COMMANDS (one literal command per line; copy verbatim, including full hashes; do not reconstruct paths; may combine using semicolon):\n' + '\n'.join(worker_scope.inspections(job)),
            'Use a literal apply_patch for the exact allowed .py destinations. The existing literal-write policy is unchanged; no shell code recipes are needed.',
            'Never write result.json directly. The fixed runner owns it. On any failed entry stop for Lead review; do not retry within this call.',
        ]
    if failure:
        lines.append('PRIOR ATTEMPT: spent without acceptance. Preserve its evidence; do not repeat unchanged work or infer additional permission from the failure.')
    if episode:
        authorization = state.get('lead_recovery_authorizations', {}).get(job['job_id'], {})
        lines.append('RECOVERY HANDOFF: Lead decision ' + str(authorization.get('decision_index', 'frozen job'))
                     + '; one authorized implementation attempt. Deliver the frozen outcome using current in-scope source. Stop on any error and return partial evidence; independent QA and Lead acceptance are still required. Recovery notes are historical evidence, not code instructions or extra permission.')
    return '\n'.join(lines)


def _codex_events(raw):
    parsed=[];bad=False
    for line in (raw or '').splitlines():
        if not line.strip():
            continue
        try:
            value=json.loads(line)
            if isinstance(value,dict): parsed.append(value)
            else: bad=True
        except (ValueError,TypeError):
            bad=True
    return parsed,bad


def codex_session_id(raw):
    for event in _codex_events(raw)[0]:
        if event.get('type') == 'thread.started' and isinstance(event.get('thread_id'),str):
            return event['thread_id']
    return None


def codex_outcome(raw, receipt, artifact_valid, stderr=''):
    parsed,bad=_codex_events(raw)
    # Command output and quoted research inputs can contain phrases such as
    # "rate limit". Only transport stderr and Codex error events classify the
    # provider; ordinary item content is untrusted task data.
    transport_events=[e for e in parsed if e.get('type') in ('turn.failed','error')]
    combined=(stderr or '')+'\n'+json.dumps(transport_events,ensure_ascii=False)
    lowered=combined.lower()
    rate_status=bool(re.search(r'\b(?:http(?:/\d+(?:\.\d+)?)?\s+|status(?:\s+code)?\s*[:=]?\s*|code\s*[:=]\s*)429\b',lowered))
    auth_status=bool(re.search(r'\b(?:http(?:/\d+(?:\.\d+)?)?\s+|status(?:\s+code)?\s*[:=]?\s*|code\s*[:=]\s*)401\b',lowered))
    if (rate_status or 'rate limit' in lowered or 'rate_limit' in lowered
            or 'too many requests' in lowered):
        return 'RATE_LIMIT'
    if (auth_status or 'authentication failed' in lowered or 'not logged in' in lowered
            or 'unauthorized' in lowered or 'invalid api key' in lowered):
        return 'AUTH'
    if any(e.get('type') in ('turn.failed','error') for e in parsed):
        return 'PROVIDER_ERROR'
    if receipt.get('exit_code') is None:
        return 'TIMEOUT_OR_INTERRUPTED'
    if receipt.get('exit_code') != 0:
        return 'PROCESS_FAILED'
    if bad:
        return 'MALFORMED_OR_MIXED_OUTPUT'
    if not codex_session_id(raw):
        return 'MISSING_SESSION_OUTCOME'
    if not any(e.get('type') == 'turn.completed' for e in parsed):
        return 'MISSING_TERMINAL_OUTCOME'
    if not artifact_valid:
        return 'INVALID_OR_MISSING_ARTIFACT'
    return 'SUCCESS'


def codex_completion(raw, receipt, call):
    requested_model,requested_reasoning=codex_requested_route(call)
    sid=codex_session_id(raw)
    outcome=codex_outcome(raw,receipt,True,
        (call/'stderr.txt').read_text(encoding='utf-8') if (call/'stderr.txt').exists() else '')
    succeeded=outcome in ('SUCCESS','INVALID_OR_MISSING_ARTIFACT')
    return {
        'info': {'id':sid,'model':codex_identity(requested_model,requested_reasoning),'cost':None,
                 'outcome':'succeeded' if succeeded else 'failed'},
        'messages':[{'type':'idle','outcome':'succeeded' if succeeded else 'failed'}],
        'identity_evidence': {
            'source':'controller_request_plus_terminal_codex_session',
            'provider_reported_model':False,
            'requested_model':requested_model,'requested_reasoning':requested_reasoning,
            'request_sha256':digest(call/'request.json'),
            'terminal_event_observed':any(
                e.get('type') == 'turn.completed' for e in _codex_events(raw)[0]),
        }}


def saved_native_configuration(call, exported, request, proof, state):
    """Match exact archived proof pins, without granting current dispatch rights."""
    if state is None:
        return False
    from .desktop_capture import recovery_context
    context = recovery_context(state)
    records = (context or {}).get('reconciliation', {}).get('saved_native_proofs', [])
    for record in records:
        request_pin = record.get('request', {})
        if request_pin.get('path') != (call/'request.json').relative_to(ROOT).as_posix():
            continue
        for pin in record.values():
            path = (ROOT/pin['path']).resolve()
            if not path.is_relative_to(ROOT.resolve()) or not path.is_file() or digest(path) != pin['sha256']:
                return False
        return (Path(request['preflight_path']).resolve() == (ROOT/record['preflight']['path']).resolve()
                and read(ROOT/record['preflight']['path']) == proof
                and read(ROOT/record['completion']['path']) == exported
                and exported.get('identity_evidence', {}).get('request_sha256') == digest(call/'request.json'))
    return False


def completion_matches(call, exported, state=None):
    if not exported:
        return False
    model=exported.get('info',{}).get('model')
    route=call_route(call)
    if route == 'codex':
        try:
            requested_model,requested_reasoning=codex_requested_route(call)
        except (OSError,ValueError,TypeError,IndexError):
            return False
        if model != codex_identity(requested_model,requested_reasoning):
            return False
        try:
            request=read(call/'request.json')
            argv=request.get('argv',[])
        except (OSError,ValueError,TypeError):
            return False
        joined=' '.join(str(x) for x in argv)
        if request.get('transport')=='native_app_server':
            from .native_preflight import validate_policy, thread_parameters, PARENT_IDENTITY_ENV
            try:
                proof=read(request['preflight_path'])
                sid=validate_policy(proof['response'],request['cwd'],requested_model,requested_reasoning)
                return (proof.get('passed') is True and proof.get('no_model_turn_sent') is True
                    and exported.get('identity_evidence',{}).get('request_sha256')==digest(call/'request.json')
                    and ((request['thread_start_params']==thread_parameters(request['cwd'],requested_model,requested_reasoning)
                          and proof['response'].get('thread',{}).get('originator')=='payout-worker'
                          and proof.get('parent_identity_environment_excluded')==list(PARENT_IDENTITY_ENV))
                         or saved_native_configuration(call,exported,request,proof,state))
                    and proof['thread_start_params']==request['thread_start_params']
                    and proof['argv']==argv and 'codex' in joined.lower() and 'app-server' in argv
                    and 'model_reasoning_effort=' + json.dumps(requested_reasoning) in argv
                    and sid==request.get('native_session_id')==exported.get('info',{}).get('id'))
            except (OSError,ValueError,KeyError,TypeError,RuntimeError): return False
        return (requested_model in argv and 'model_reasoning_effort=' + json.dumps(requested_reasoning) in argv
                and 'forced_login_method="chatgpt"' in argv
                and '--ignore-user-config' in argv
                and ('--ephemeral' in argv or 'ACTIVE COMMAND POLICY: SCOPED_LITERAL_V1' in (call/'prompt.txt').read_text(encoding='utf-8'))
                and '--approve-for-me' in argv and '--json' in argv
                and 'codex' in joined.lower())
    return model == MUSE_IDENTITY


def load_worker_completion(raw, receipt, call, export_path):
    if export_path.exists():
        return read(export_path)
    if call_route(call) == 'codex':
        exported=codex_completion(raw,receipt,call)
        write(export_path,exported)
        return exported
    sid=extract_session_id(raw)
    if not sid:
        return None
    exp=subprocess.run(
        native_command('opencode')+['session','export',sid,'--standalone','--sanitize'],
        cwd=ROOT,env=env_clean(),capture_output=True,text=True,encoding='utf-8',timeout=30,
        **background_process_options())
    if exp.returncode!=0:
        return None
    exported=completion_export(json.loads(exp.stdout));write(export_path,exported)
    return exported


def worker_session_id(raw, call):
    return codex_session_id(raw) if call_route(call) == 'codex' else extract_session_id(raw)


def worker_outcome(raw, receipt, sid, exported, artifact_valid, job, state, call):
    if call_route(call) == 'codex':
        stderr=(call/'stderr.txt').read_text(encoding='utf-8') if (call/'stderr.txt').exists() else ''
        return codex_outcome(raw,receipt,artifact_valid,stderr),[]
    return reconcile_optional_lookup(raw,receipt,sid,exported,artifact_valid,job,state)


def codex_commands(raw):
    commands=[]
    for event in _codex_events(raw)[0]:
        item=event.get('item',{})
        if event.get('type') == 'item.completed' and item.get('type') == 'command_execution':
            commands.append({'command':item.get('command',''),
                             'output':item.get('aggregated_output',''),
                             'exit_code':item.get('exit_code')})
    return commands


def validate_worker_history(raw, command, controller, call):
    if call_route(call) != 'codex':
        return validate_shell_history(raw,command,controller,str(call.relative_to(ROOT)))
    prompt_path=call/'prompt.txt'
    if prompt_path.exists() and 'ACTIVE COMMAND POLICY: SCOPED_LITERAL_V1' in prompt_path.read_text(encoding='utf-8'):
        from .worker_scope import native_session_receipt
        job=read(call.parent/'job.json')
        receipt=native_session_receipt(call,codex_session_id(raw),job,command,scoped_commands(job))
        if controller.state.get('native_worker_preturn_gate') and not receipt.get('pretool_policy'):
            raise RuntimeError('ENGINEERING_BLOCKED: native pre-tool policy receipt missing')
        return receipt['commands']
    commands=codex_commands(raw)
    normalized=lambda value:' '.join(str(value).replace('\\','/').split()).lower()
    fixed=normalized(command)
    fixed_calls=[entry for entry in commands if fixed in normalized(entry['command'])]
    if not fixed_calls:
        raise RuntimeError('HUMAN_ACTION_REQUIRED: Codex worker did not run the fixed command')
    prompt_path=call/'prompt.txt'
    one_entry=prompt_path.exists() and 'Fixed entry invocation limit for this call: 1.' in prompt_path.read_text(encoding='utf-8')
    if len(fixed_calls)>(1 if one_entry else 3):
        raise RuntimeError('HUMAN_ACTION_REQUIRED: Codex worker exceeded fixed-command repair limit')
    if any(entry['exit_code']==0 for entry in fixed_calls[:-1]):
        raise RuntimeError('HUMAN_ACTION_REQUIRED: Codex worker repeated successful fixed command')
    forbidden=('git push','git commit','git reset','git clean','curl ','wget ',
               'invoke-webrequest','start-bitstransfer','opencode ','codex exec',
               'remove-item',' del ',' rmdir ',' rm ')
    attempted=[entry['command'] for entry in commands
               if any(token in ' '+normalized(entry['command'])+' ' for token in forbidden)]
    if attempted:
        raise RuntimeError('HUMAN_ACTION_REQUIRED: forbidden Codex worker command '+repr(attempted))
    write(call.parent/('codex_command_history_'+call.name.rsplit('_',1)[-1]+'.json'),{
        'fixed_command':command,'fixed_command_calls':len(fixed_calls),
        'commands':commands,'forbidden_attempts':[]})
    return commands

def audit_worker_attempt(raw, command, controller, call, job, attempt):
    """Persist audited no-entry failures before returning control to the Lead.

    Without a durable failure disposition, the next accepted repair audits the
    same spent call again instead of reaching its separately metered attempt.
    Safety and unparsed-tool failures are deliberately not caught here.
    """
    from .worker_scope import ScopedPreEntryIncomplete
    try:
        return validate_worker_history(raw, command, controller, call)
    except ScopedPreEntryIncomplete as error:
        folder = call.parent
        if (folder / 'work/result.json').exists():
            raise
        if controller.state.get('native_worker_preturn_gate') and not error.receipt.get('pretool_policy'):
            raise RuntimeError('ENGINEERING_BLOCKED: native pre-tool policy receipt missing') from error
        path = folder / f'failure_{attempt}.json'
        if not path.exists():
            source = folder / 'work/analysis.py'
            write(path, {
                'code': 'SCOPED_PREENTRY_INCOMPLETE',
                'runtime_outcome': 'AUDITED_TURN_WITHOUT_FIXED_ENTRY',
                'details': str(error) + '; preserve this spent call and partial source. '
                           'A Lead-authorized, separately metered recovery is required; no result was accepted.',
                'attempt': attempt,
                'session_id': error.receipt['native_session_id'],
                'analysis_sha256': digest(source) if source.exists() else None,
                'input_hashes': {i['path']: i['sha256'] for i in job['inputs']},
                'native_policy_receipt': {'path': error.receipt_path.relative_to(ROOT).as_posix(),
                                          'sha256': digest(error.receipt_path)},
                'call_receipt': {'path': (call / 'receipt.json').relative_to(ROOT).as_posix(),
                                 'sha256': digest(call / 'receipt.json')},
                'new_independent_trial': False,
            })
        raise


def reconcile_optional_lookup(raw,receipt,sid,exported,artifact_valid,job,state):
    """A missing legacy work directory is not a failed calculation.

    Preserve the raw TOOL_FAILED classification. Reconcile only nonexistent,
    optional work-directory reads for already-completed jobs, followed by a
    successful terminal session. Required inputs and all other errors stay fatal.
    """
    original=classify(raw,receipt['exit_code'],session_id=sid,session_export=exported,artifact_valid=artifact_valid)
    if original!='TOOL_FAILED': return original,[]
    parsed,bad=events(raw)
    if bad: return original,[]
    required={(ROOT/i['path']).resolve() for i in job['inputs']}
    # The executor commonly probes its own analysis.py before creating it.
    # That optional missing read is harmless for both an already-completed
    # dependency and the current pending job; it must not downgrade an
    # otherwise authenticated Muse completion to TOOL_FAILED.
    allowed={(CAMP/'runtime/jobs'/jid/'work').resolve() for jid in state.get('completed',{})}
    allowed.add((CAMP/'runtime/jobs'/job['job_id']/'work').resolve())
    filtered=[];ignored=[]
    for event in parsed:
        part=event.get('part',{});toolstate=part.get('state',{})
        if event.get('type')=='tool_use' and toolstate.get('status')=='error':
            value=toolstate.get('input',{}).get('filePath',toolstate.get('input',{}).get('path'))
            path=Path(value).resolve() if isinstance(value,str) else None
            in_allowed_work = bool(path and any(path == root or root in path.parents for root in allowed))
            if part.get('tool')=='read' and in_allowed_work and path not in required and toolstate.get('error','').startswith('File not found:'):
                ignored.append({'tool':'read','path':str(path),'error':toolstate['error']});continue
        filtered.append(event)
    effective=classify('\n'.join(json.dumps(x) for x in filtered),receipt['exit_code'],session_id=sid,session_export=exported,artifact_valid=artifact_valid)
    return (effective,ignored) if effective=='SUCCESS' and ignored else (original,[])

def execution_failure(output):
    for line in reversed(output.splitlines()):
        stripped=line.strip()
        # Tracebacks often end with a bare exception name (for example
        # AssertionError) rather than ``NameError: message``. Preserve that
        # concrete blocker identity instead of collapsing every such failure
        # into the campaign-wide generic fixed-entry cap.
        if stripped.endswith('Error') and ':' not in stripped and stripped.isidentifier():
            return 'EXECUTION_'+stripped
        kind,sep,message=line.partition(':')
        if sep and kind.endswith(('Error','Exception')):
            return 'EXECUTION_'+kind+':'+message.split('Did you mean:',1)[0].strip()
    return 'EXECUTION_FIXED_ENTRY_FAILED'

def validate_shell_history(raw,command,controller,label):
    parsed,_=events(raw)
    shells=[];edit_history=[];edit_versions=[]
    for event in parsed:
        if event.get('type')!='tool_use': continue
        part=event.get('part',{});state=part.get('state',{})
        if part.get('tool') in ('write','edit','apply_patch'):
            edit_history.append(state.get('input',{}))
        if part.get('tool') in ('shell','bash','execute'):
            shells.append(state)
            edit_versions.append(hashlib.sha256(json.dumps(edit_history,sort_keys=True).encode()).hexdigest())
    job=controller.state.get('pending_job') or {}
    extra=scoped_commands(job)
    inspections=[x for x in shells if x.get('input',{}).get('command') in extra]
    if len(inspections)>8 or any(x.get('status')!='completed' for x in inspections):
        raise RuntimeError('ENGINEERING_BLOCKED: scoped inspection/test limit')
    shells=[x for x in shells if x not in inspections]
    if not 1<=len(shells)<=3 or any(x.get('input',{}).get('command')!=command or x.get('status')!='completed' for x in shells):
        raise RuntimeError('ENGINEERING_BLOCKED: fixed command history outside bounded contract')
    for index,failed in enumerate(shells[:-1]):
        code=failed.get('metadata',{}).get('metadata',{}).get('exit')
        if code is None or code==0:
            raise RuntimeError('ENGINEERING_BLOCKED: repeated fixed command after success or unknown exit')
        key=label+':'+str(index)
        if key not in controller.state.setdefault('internal_repair_receipts',{}):
            failure_code=execution_failure(failed.get('output',''))
            evidence=hashlib.sha256(json.dumps({'code':failure_code,'edit_version':edit_versions[index]},sort_keys=True).encode()).hexdigest()
            episode=controller.state.get('lead_recovery_authorizations',{}).get(job.get('job_id'),{})
            if not episode:
                reserve_repair(controller.state,'GENERIC_ARTIFACT',failure_code,evidence)
            controller.state['internal_repair_receipts'][key]={'code':failure_code,'evidence_sha256':evidence}
            controller.save()
    return shells


def scoped_commands(job):
    implementation=job.get('implementation')
    if not implementation:
        return []
    return ['py -3.12 -X utf8 -m unittest '+m for m in implementation['test_modules']]


def deterministic_command(controller, command, *, historical_timeout):
    from .runtime_policy import REVISION_UNBOUNDED
    if controller.state.get('runtime_policy_revision')!=REVISION_UNBOUNDED:
        return subprocess.run(command,cwd=ROOT,env=env_clean(),capture_output=True,
                              text=True,encoding='utf-8',timeout=historical_timeout,
                              **background_process_options())
    receipt,stdout,stderr=run_with_inactivity_guard(
        command,cwd=ROOT,env=env_clean(),
        inactivity_seconds=controller.state['runtime_policy']['inactivity_stall_seconds'],
        cancel_path=CAMP/'runtime/cancel.request')
    return subprocess.CompletedProcess(command,
        receipt['exit_code'] if receipt['exit_code'] is not None else 124,stdout,stderr)

def verify_result(controller,job,folder,label):
    command=['py','-3.12','-X','utf8','-m','tools.quant_research_loop.generic_entry',(folder/'job.json').relative_to(ROOT).as_posix(),'--verify']
    result=deterministic_command(controller,command,historical_timeout=95)
    path=folder/f'verification_{label}.json'
    write(path,{'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
    if result.returncode==0:
        resultpath=folder/'work/result.json';s=controller.state;jid=job['job_id']
        record={'path':resultpath.relative_to(ROOT).as_posix(),'sha256':digest(resultpath),'verification':path.relative_to(ROOT).as_posix()}
        s['completed'][jid]=record;s['evidence_id']=jid;s['pending_job']=None
        if jid in s.get('registered_experiments',{}): s['registered_experiments'][jid]['status']='VERIFIED'
        controller.save()
    return result

def reconciled_scope_is_clean(folder,attempt,call):
    """Accept a controller-owned scope reconciliation bound to raw worker files."""
    scope=folder/f'scope_check_{attempt}.json'
    if not scope.exists():
        return False
    receipt=read(scope)
    unexpected=receipt.get('unexpected',[])
    if not unexpected:
        return True
    reconciliation=folder/f'scope_reconciliation_{attempt}.json'
    if not reconciliation.exists():
        return False
    recovered=read(reconciliation)
    return (
        digest(scope)==recovered.get('overwritten_scope_check_sha256')
        and recovered.get('reconstructed_unexpected')==[]
        and recovered.get('worker_tool_mutations')==[]
        and digest(call/'stdout.jsonl')==recovered.get('worker_stream_sha256')
        and digest(call/'receipt.json')==recovered.get('worker_receipt_sha256')
    )

def require_saved_metadata_clean(folder):
    """A saved raw metadata failure cannot be reclassified during reuse."""
    for path in folder.glob('metadata_scope_check_*.json'):
        raw=read(path);unexpected=raw.get('unexpected',[])
        if not unexpected: continue
        attempt=path.stem.rsplit('_',1)[-1];call=folder/f'worker_call_{attempt}'
        storage=folder/f'metadata_storage_check_{attempt}.json'
        try:
            classified=read(storage)
            pins=[(path,classified['raw_metadata_scope']),
                  (call/'receipt.json',classified['worker_receipt']),
                  (call/'stdout.jsonl',classified['worker_stream']),
                  (call/'request.json',classified['worker_request'])]
            pins.extend((folder/f'metadata_scope_{label}_{attempt}.json',raw[label]) for label in ('before','after'))
            native=classified['native_permission_evidence'];native_path=ROOT/native['path']
            if native_path not in (call/'native_policy_and_commands.json',call/'native_policy_and_commands_parsed_v3.json'):
                raise ValueError('native proof path')
            pins.append((native_path,native))
            if any(pin['path']!=p.relative_to(ROOT).as_posix() or pin['sha256']!=digest(p) for p,pin in pins):
                raise ValueError('receipt pin')
            proof=read(native_path)
            pretool=proof['pretool_policy'];pretool_path=ROOT/pretool['path']
            if pretool_path!=folder/f'native_preflight_{attempt}.json':
                raise ValueError('native preflight path')
            if (proof.get('write_policy_valid') is not True or not proof.get('pretool_policy')
                    or any(proof.get(k)!=[] for k in ('errors','protocol_errors','unparsed_tools'))
                    or digest(pretool_path)!=pretool['sha256']
                    or proof.get('request')!=read(call/'request.json')
                    or proof.get('native_session_id')!=worker_session_id((call/'stdout.jsonl').read_text(encoding='utf-8'),call)
                    or classified.get('raw_unexpected')!=unexpected
                    or classified.get('remaining_unexpected')!=[]
                    or classified.get('rejected')!={}
                    or set(classified.get('validated_storage',{}))!=set(unexpected)):
                raise ValueError('incomplete classification')
            from .worker_scope import validated_git_storage
            current=validated_git_storage(raw['changes'],unexpected,proof,root=ROOT,allow_later_mtime=True)
            if current['remaining_unexpected'] or current['validated_storage']!=classified['validated_storage']:
                raise ValueError('storage identity changed')
        except (OSError,ValueError,KeyError,TypeError):
            raise RuntimeError('HUMAN_ACTION_REQUIRED: saved worker metadata failure has no original complete classification; no reuse or redispatch: '+path.name)


def recover_existing(controller,job,folder,command):
    """Reconcile authentic saved Muse output without another model invocation."""
    require_saved_metadata_clean(folder)
    resultpath=folder/'work/result.json'
    if not resultpath.exists(): return False
    value=read(resultpath)
    source_hash=hashlib.sha256((folder/'work/analysis.py').read_text(encoding='utf-8').encode()).hexdigest()
    if value.get('analysis_sha256')!=source_hash or value.get('job_sha256')!=digest(folder/'job.json'):
        raise RuntimeError('Saved result provenance mismatch')
    retryable_provider_failure=False
    for path in sorted(folder.glob('outcome_*.json')):
        attempt=path.stem.split('_')[-1]
        outcome=read(path);call=folder/f'worker_call_{attempt}'
        completion=folder/f'worker_completion_{attempt}.json'
        corrected_classifier=False
        effective_classification=outcome.get('classification')
        if (call_route(call)=='codex' and effective_classification in ('AUTH','RATE_LIMIT')
                and completion.exists() and (call/'receipt.json').exists()):
            existing_receipt=read(call/'receipt.json')
            existing_raw=(call/'stdout.jsonl').read_text(encoding='utf-8')
            existing_stderr=(call/'stderr.txt').read_text(encoding='utf-8')
            corrected=codex_outcome(existing_raw,existing_receipt,True,existing_stderr)
            if corrected=='SUCCESS':
                effective_classification=corrected
                corrected_classifier=True
        # A provider-side invalid-output/auth/rate event has an immutable
        # receipt but no authenticated artifact. Preserve it and let the
        # bounded execute loop decide whether a fresh call is permitted;
        # do not misclassify it as a shell-history failure.
        if outcome.get('classification')=='PROVIDER_ERROR' and not resultpath.exists() and completion.exists():
            retryable_provider_failure=True
            continue
        # A bounded Muse process can be reaped after the fixed entry command
        # has already completed.  Keep the raw timeout receipt immutable, but
        # accept the artifact only after authenticating the Muse export,
        # fixed-command history, scope receipt and deterministic reproduction.
        # This is recovery of one existing attempt, never a retry or a new
        # independent trial.
        if effective_classification not in ('SUCCESS','TOOL_FAILED','TIMEOUT_OR_INTERRUPTED') or not completion.exists(): continue
        exported=read(completion);receipt=read(call/'receipt.json');raw=(call/'stdout.jsonl').read_text(encoding='utf-8')
        if not completion_matches(call,exported,controller.state): continue
        timeout_artifact = (
            effective_classification=='TIMEOUT_OR_INTERRUPTED'
            and receipt.get('process_reaped') is True
            and receipt.get('exit_code') is None
            and resultpath.exists()
        )
        effective,ignored=worker_outcome(raw,receipt,outcome['session_id'],exported,True,job,controller.state,call)
        if effective!='SUCCESS' and not timeout_artifact: continue
        if ignored: write(folder/f'outcome_reconciled_{attempt}.json',{'original':outcome['classification'],'effective':effective,'optional_missing_directories':ignored,'raw_receipt_preserved':True})
        shells=validate_worker_history(raw,command,controller,call)
        scope=folder/f'scope_check_{attempt}.json'
        if not reconciled_scope_is_clean(folder,attempt,call): continue
        if corrected_classifier:
            correction=folder/f'outcome_classifier_reconciliation_{attempt}.json'
            if not correction.exists():
                write(correction,{
                    'original_outcome_sha256':digest(path),
                    'original_classification':outcome['classification'],
                    'corrected_classification':'SUCCESS',
                    'reason':'The numeric status token occurred in non-error transport text. The raw Codex stream has a terminal turn, zero exit, one authenticated fixed command, and a scoped artifact.',
                    'stdout_sha256':digest(call/'stdout.jsonl'),
                    'receipt_sha256':digest(call/'receipt.json'),
                    'scope_check_sha256':digest(scope),
                    'fixed_command_count':len([x for x in shells if command in x.get('command','')]),
                    'new_worker_call':False,'new_independent_trial':False,
                })
        if timeout_artifact:
            write(folder/f'outcome_reconciled_{attempt}.json',{
                'original':outcome['classification'],
                'effective':'SUCCESS_RECONCILED_AUTHENTIC_ARTIFACT',
                'reason':'The bounded Muse process was reaped after the fixed command completed; exact command/result/provenance/scope were authenticated and deterministically reproduced.',
                'raw_receipt_preserved':True,
                'worker_completion_model':exported['info']['model'],
                'receipt_process_reaped':True,
                'scope_check_sha256':digest(scope),
            })
        verified=verify_result(controller,job,folder,'recovery_'+attempt)
        if verified.returncode: raise RuntimeError('Saved artifact deterministic verification failed: '+verified.stderr[-2000:])
        return True
    if retryable_provider_failure:
        return False
    raise RuntimeError('Saved artifact has no authenticated scoped Muse completion; no redispatch')


def recovery_verification(controller, job, folder, command, episode):
    """Run one Lead-authorized Muse verification for a saved partial artifact.

    A provider transport error after a deterministic artifact was written is
    neither a valid result nor permission to rerun the market job.  The Lead
    may authorize one changed recovery episode; this route authenticates a
    fresh Muse xHigh session that runs only the trusted ``--verify`` command.
    The original call, analysis and result remain immutable and the episode is
    never counted as a new market trial.
    """
    require_saved_metadata_clean(folder)
    s, p = controller.state, controller.profile
    resultpath = folder / 'work' / 'result.json'
    if not resultpath.exists():
        raise RuntimeError('ENGINEERING_BLOCKED: recovery requires a saved artifact')
    call = folder / 'worker_call_recovery_0'
    if call.exists():
        raise RuntimeError('ENGINEERING_BLOCKED: recovery call already exists; no blind retry')

    from .skill_engineering import maybe_review, guidance
    maybe_review(controller)
    from .lead_recovery import reserve_episode
    failure_refs = []
    for path in sorted(folder.glob('failure_*.json')):
        failure_refs.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': digest(path)})
    evidence = hashlib.sha256(json.dumps({
        'kind': 'RECOVERY_VERIFICATION',
        'job_id': job['job_id'],
        'analysis_sha256': digest(folder / 'work' / 'analysis.py'),
        'result_sha256': digest(resultpath),
        'failures': failure_refs,
        'episode': episode,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    reserve_episode(s, job, 'RECOVERY_VERIFICATION', evidence)
    controller.save()

    executor_label=(p['worker']+' at '+p['worker_variant']+' reasoning through Codex'
                    if codex_route(p) else 'Muse xHigh')
    prompt = (
        'You are '+executor_label+' executing one Research Lead-authorized bounded recovery episode. '
        'The prior fixed experiment call and its analysis/result are immutable partial evidence. '
        'Do not edit analysis.py, result.json, job.json, controller, verifier, inputs or old results; '
        'do not read protected data, use network, call child models, or rerun the market job without '
        '--verify. Inspect the supplied failure/evidence only as needed. Execute exactly this one '
        'trusted deterministic verification command once, then report its actual exit status:\n' + command +
        '\nThis is acceptance verification of the saved artifact, not a new market trial or a parameter search. '
        'If the command fails, report the exact failure and stop.'
    )
    skill_guidance = guidance(s, 'executor')
    if skill_guidance:
        prompt += '\nADVISORY SKILL GUIDANCE (verification-only contract takes precedence): ' + json.dumps(skill_guidance, ensure_ascii=False)
    before = snapshot()
    scope_before = folder / 'scope_before_recovery_0.json'
    write(scope_before, before)
    cmd = worker_command(p,prompt)
    receipt, raw = controller.model_call(cmd, prompt, call, cwd=ROOT, timeout_seconds=900)
    after = snapshot()
    state_rel = controller.state_path.relative_to(ROOT).as_posix()
    call_prefix = call.relative_to(ROOT).as_posix() + '/'
    scope_before_rel = scope_before.relative_to(ROOT).as_posix()
    unexpected = sorted(
        n for n in set(before) | set(after)
        if before.get(n) != after.get(n)
        and n not in {state_rel, scope_before_rel}
        and not n.startswith(call_prefix)
    )
    scope_path = folder / 'scope_check_recovery_0.json'
    write(scope_path, {'unexpected': unexpected, 'command': command,
                       'worker_call': call.relative_to(ROOT).as_posix()})
    if unexpected:
        raise RuntimeError('HUMAN_ACTION_REQUIRED: recovery scope violation ' + str(unexpected))

    sid = worker_session_id(raw,call)
    export_path = folder / 'worker_completion_recovery_0.json'
    try:
        exported=load_worker_completion(raw,receipt,call,export_path)
    except (ValueError,TypeError):
        exported=None
    outcome,_ = worker_outcome(raw,receipt,sid,exported,True,job,s,call)
    write(folder / 'outcome_recovery_0.json', {
        'classification': outcome, 'session_id': sid,
        'requested_model': p['worker'], 'requested_variant': p['worker_variant'],
        'command': command, 'original_result_immutable': True,
    })
    if outcome != 'SUCCESS':
        raise RuntimeError('ENGINEERING_BLOCKED: recovery worker runtime ' + outcome)
    expected_model = codex_identity(p['worker'], p['worker_variant']) if codex_route(p) else MUSE_IDENTITY
    if not completion_matches(call,exported,controller.state):
        raise RuntimeError('ENGINEERING_BLOCKED: recovery worker identity not verified')
    validate_worker_history(raw,command,controller,call)
    verification = verify_result(controller, job, folder, 'recovery_verification_0')
    if verification.returncode:
        raise RuntimeError('ENGINEERING_BLOCKED: recovery deterministic verification failed: ' +
                           verification.stderr[-2000:])
    write(folder / 'recovery_verification_receipt.json', {
        'status': 'PASS', 'episode': episode,
        'worker_model': expected_model,
        'worker_call': call.relative_to(ROOT).as_posix(),
        'result_sha256': digest(resultpath),
        'new_independent_trial': False,
        'market_job_redispatched': False,
        'original_call_preserved': True,
    })
    return s['completed'][job['job_id']]

def classify_git_metadata(raw, command, controller, call, job, attempt, changes, unexpected):
    """Actual native audit precedes any current Git-storage classification."""
    from .worker_scope import validated_git_storage
    sid=worker_session_id(raw,call)
    if call_route(call)!='codex' or not sid:
        raise RuntimeError('HUMAN_ACTION_REQUIRED: worker metadata scope violation '+str(unexpected))
    commands=audit_worker_attempt(raw,command,controller,call,job,attempt)
    path=call/'native_policy_and_commands_parsed_v3.json'
    if not path.exists(): path=call/'native_policy_and_commands.json'
    proof=read(path)
    if proof.get('native_session_id')!=sid or proof.get('commands')!=commands:
        raise RuntimeError('HUMAN_ACTION_REQUIRED: Git storage classification native proof identity mismatch')
    classification=validated_git_storage(changes,unexpected,proof,root=ROOT)
    classification['native_permission_evidence']={'path':path.relative_to(ROOT).as_posix(),'sha256':digest(path)}
    return classification


def execute(controller,job):
    s,p=controller.state,controller.profile;jid=job['job_id']
    folder=CAMP/'runtime/jobs'/jid
    require_saved_metadata_clean(folder)
    if jid in s['completed']:
        record=s['completed'][jid]
        if digest(ROOT/record['path'])!=record['sha256']: raise RuntimeError('Completed result changed')
        return {'status':'ALREADY_VERIFIED'}
    validate_spec(job,p,s)
    work=folder/'work';work.mkdir(exist_ok=True)
    if job['job_class']=='EXPERIMENT' and not (folder/'engine_regression.json').exists():
        controller.check_budget()
        checked=deterministic_command(controller,
            ['py','-3.12','-X','utf8','-m','unittest','discover','-s','tools','-p','test_research_engine.py'],
            historical_timeout=120)
        write(folder/'engine_regression.json',{'exit_code':checked.returncode,'stdout':checked.stdout,'stderr':checked.stderr})
    if job['job_class']=='EXPERIMENT' and read(folder/'engine_regression.json')['exit_code']!=0:
        raise RuntimeError('ENGINEERING_BLOCKED: existing simulator regression failed before experiment implementation')
    if job['job_class'] in ('EXPERIMENT','ACCOUNT_POLICY') and jid not in s.setdefault('registered_experiments',{}):
        controller.check_budget()
        s['registered_experiments'][jid]={'candidate_id':job['candidate_id'],'status':'DISPATCH_RESERVED','job_sha256':digest(folder/'job.json')}
        s['trials']+=1;s['families'][job['candidate_id']]=s['families'].get(job['candidate_id'],0)+1;controller.save()
    command=fixed_entry_command(folder,s)
    episode=s.get('lead_recovery_authorizations',{}).get(jid,{}).get('episode') or job.get('recovery_episode')
    if episode and (folder / 'work' / 'result.json').exists():
        # A recovery episode is explicitly different from a normal worker
        # retry.  If a saved result exists, authenticate it through one fresh
        # Muse session running only the deterministic verifier.  Never enter
        # the ordinary market-command loop for this route.
        return recovery_verification(controller, job, folder, command + ' --verify', episode)
    if recover_existing(controller,job,folder,command): return s['completed'][jid]
    failure=''
    # Flat loop: each concrete blocker is capped by reserve_repair, all calls
    # also consume the unchanged global budget. Distinct failures do not share
    # an accidental per-job three-call limit.
    for attempt in range(p.get('max_calls',80)):
        native_audited = False
        call=folder/f'worker_call_{attempt}'
        if (folder/f'failure_{attempt}.json').exists():
            failure=read(folder/f'failure_{attempt}.json')['details']
            continue  # immutable failed invocation; no redispatch or scope rescan
        if call.exists():
            if not (call/'receipt.json').exists(): raise RuntimeError('ENGINEERING_BLOCKED: ambiguous worker call, no redispatch')
            receipt=read(call/'receipt.json');raw=(call/'stdout.jsonl').read_text(encoding='utf-8')
            prior_outcome_path=folder/f'outcome_{attempt}.json'
            if (codex_route(p) and call_route(call)=='opencode'
                    and prior_outcome_path.exists()
                    and read(prior_outcome_path).get('classification') in ('RATE_LIMIT','AUTH','PROVIDER_ERROR')
                    and not (work/'result.json').exists()):
                # R07 preserves the failed Muse receipt and advances to a new
                # attempt on the explicitly authorized Codex route. This is a
                # runtime-route retry of the same frozen job, never a new trial.
                failure_path=folder/f'failure_{attempt}.json'
                if not failure_path.exists():
                    write(failure_path,{
                        'code':'SUPERSEDED_MUSE_PROVIDER_'+read(prior_outcome_path)['classification'],
                        'runtime_outcome':read(prior_outcome_path)['classification'],
                        'details':'R07 preserved the failed Muse/OpenCode call and authorized the same frozen job through Codex gpt-5.6-luna max. No artifact was produced; this retry is not an independent trial.',
                        'attempt':attempt,
                        'session_id':read(prior_outcome_path).get('session_id'),
                        'analysis_sha256':None,
                        'input_hashes':{i['path']:i['sha256'] for i in job['inputs']},
                        'original_call_preserved':True,
                        'new_independent_trial':False,
                    })
                failure=read(failure_path)['details']
                continue
        else:
            # A parent Desktop turn may still own a Git snapshot. Wait before
            # reserving the episode/call; retain the strict post-worker check.
            from .desktop_capture import wait_before_worker
            wait_before_worker(controller, folder/f'desktop_capture_wait_{attempt}.json', root=ROOT)
            from .skill_engineering import maybe_review
            maybe_review(controller)
            if attempt:
                prior=read(folder/f'failure_{attempt-1}.json');code=prior['code']
                # G01's pending call was cut to ~7 seconds by the retired
                # cumulative-time gate. Preserve that raw failure, but treat
                # the policy-induced timeout as a distinct, newly actionable
                # plumbing blocker under the signed addendum. A normal
                # post-addendum timeout still uses the unchanged blocker key
                # and therefore remains subject to its existing two-attempt
                # cap.
                if code=='GENERIC_IMPLEMENTATION_TIMEOUT':
                    from .runtime_policy import cumulative_runtime_cap_bypassed
                    prior_receipt=call.parent/f'worker_call_{attempt-1}'/'receipt.json'
                    try:
                        prior_elapsed=read(prior_receipt).get('elapsed_s')
                    except (OSError,ValueError):
                        prior_elapsed=None
                    if (cumulative_runtime_cap_bypassed(s,p)
                            and isinstance(prior_elapsed,(int,float))
                            and prior_elapsed < 30):
                        legacy_code='GENERIC_IMPLEMENTATION_TIMEOUT_LEGACY_CUMULATIVE_GATE'
                        write(folder/f'repair_reclassification_{attempt-1}.json',{
                            'raw_code':code,'effective_code':legacy_code,
                            'receipt_elapsed_s':prior_elapsed,
                            'reason':'The prior call was bounded by the retired cumulative runtime cap; raw failure remains immutable and is not a research result.',
                            'authorization_revision':s.get('runtime_policy_revision'),
                        })
                        code=legacy_code
                evidence=hashlib.sha256(json.dumps({k:prior.get(k) for k in ('code','analysis_sha256','input_hashes')},sort_keys=True).encode()).hexdigest()
                episode=s.get('lead_recovery_authorizations',{}).get(jid,{}).get('episode') or job.get('recovery_episode')
                if not episode and code!='HOST_TURN_ABORTED_NO_RESULT':
                    reserve_repair(s,'GENERIC_ARTIFACT',code,evidence)
                controller.save()
                archive=folder/f'failed_work_{attempt-1}'
                if not archive.exists(): shutil.copytree(work,archive)
                if (work/'result.json').exists(): (work/'result.json').unlink()
            prompt=build_worker_prompt(p,job,s,command,failure)
            # Meter the first dispatched invocation as well as retries.
            # Reading an existing result above consumes no worker attempt.
            from .lead_recovery import reserve_episode
            dispatch_evidence=hashlib.sha256(json.dumps({
                'analysis_sha256':digest(work/'analysis.py') if (work/'analysis.py').exists() else None,
                'input_hashes':{i['path']:i['sha256'] for i in job['inputs']},
            },sort_keys=True).encode()).hexdigest()
            reserve_episode(s,job,'WORKER_INVOCATION',dispatch_evidence)
            controller.save()
            worker_cwd=worker_workdir(p,job)
            cmd=worker_command(p,prompt,cwd=worker_cwd)
            native_session=None
            if s.get('native_worker_preturn_gate'):
                from .native_preflight import prepare_session
                native_session=prepare_session(worker_cwd,p['worker'],folder/f'native_preflight_{attempt}.json',
                                               reasoning=p['worker_variant'])
            write(folder/f'scope_before_{attempt}.json',snapshot())
            from . import worker_scope
            if worker_scope.enabled(s):
                write(folder/f'metadata_scope_before_{attempt}.json',worker_scope.metadata_snapshot())
            try:
                receipt,raw=controller.model_call(cmd,prompt,call,cwd=worker_cwd,timeout_seconds=900,
                    **({'native_session':native_session} if native_session is not None else {}))
            finally:
                if native_session is not None: native_session.close()
            if worker_scope.enabled(s):
                before_path=folder/f'metadata_scope_before_{attempt}.json'
                after_path=folder/f'metadata_scope_after_{attempt}.json'
                before_meta=read(before_path)
                after_meta=worker_scope.metadata_snapshot()
                allowed_meta=set(job['allowed_file_scope'])|{controller.state_path.relative_to(ROOT).as_posix(),before_path.relative_to(ROOT).as_posix()}
                changes,unexpected_meta=worker_scope.metadata_diff(before_meta,after_meta,allowed_meta,call.relative_to(ROOT).as_posix()+'/')
                write(after_path,after_meta)
                write(folder/f'metadata_scope_check_{attempt}.json',{
                    'before':{'path':before_path.relative_to(ROOT).as_posix(),'sha256':digest(before_path),'entries':len(before_meta)},
                    'after':{'path':after_path.relative_to(ROOT).as_posix(),'sha256':digest(after_path),'entries':len(after_meta)},
                    'includes_ignored':True,'file_contents_read':False,'changes':changes,'unexpected':unexpected_meta,
                    'limitations':'Repository metadata comparison; no OS enforcement or outside-repository attestation. Transient create/delete with restored metadata may be invisible.'})
                if unexpected_meta:
                    # Storage classification cannot precede the existing exact
                    # native permission/tool audit. Preserve the raw comparison.
                    classification=classify_git_metadata(raw,command,controller,call,job,attempt,changes,unexpected_meta)
                    native_audited=True
                    classification.update(
                        raw_metadata_scope={'path':(folder/f'metadata_scope_check_{attempt}.json').relative_to(ROOT).as_posix(),
                                            'sha256':digest(folder/f'metadata_scope_check_{attempt}.json')},
                        raw_unexpected=unexpected_meta,
                        worker_receipt={'path':(call/'receipt.json').relative_to(ROOT).as_posix(),'sha256':digest(call/'receipt.json')},
                        worker_stream={'path':(call/'stdout.jsonl').relative_to(ROOT).as_posix(),'sha256':digest(call/'stdout.jsonl')},
                        worker_request={'path':(call/'request.json').relative_to(ROOT).as_posix(),'sha256':digest(call/'request.json')})
                    write(folder/f'metadata_storage_check_{attempt}.json',classification)
                    if classification['remaining_unexpected']:
                        raise RuntimeError('HUMAN_ACTION_REQUIRED: worker metadata scope violation '+str(classification['remaining_unexpected']))
        scope_path=folder/f'scope_check_{attempt}.json'
        if scope_path.exists():
            # A persisted call has already been scope-checked during its
            # original worker interval. Controller-side Lead decisions,
            # contract reconciliation, and runtime fixes created while
            # resuming are not worker side effects and must not be compared
            # against the old pre-call snapshot.
            scope_receipt=read(scope_path)
            unexpected=scope_receipt.get('unexpected',[])
            reconciliation=folder/f'scope_reconciliation_{attempt}.json'
            if unexpected and reconciliation.exists():
                recovered=read(reconciliation)
                # Accept a controller recovery receipt only when it binds the
                # exact overwritten scope receipt and raw Muse stream, and
                # independently records zero mutating worker tools.
                if (digest(scope_path)==recovered.get('overwritten_scope_check_sha256')
                        and recovered.get('reconstructed_unexpected')==[]
                        and recovered.get('worker_tool_mutations')==[]
                        and digest(call/'stdout.jsonl')==recovered.get('worker_stream_sha256')
                        and digest(call/'receipt.json')==recovered.get('worker_receipt_sha256')):
                    unexpected=[]
        else:
            before=read(folder/f'scope_before_{attempt}.json');after=snapshot()
            prefix=folder.relative_to(ROOT).as_posix()+'/'
            allowed=set(job['allowed_file_scope'])|{controller.state_path.relative_to(ROOT).as_posix()}
            allowed.add((folder/f'scope_before_{attempt}.json').relative_to(ROOT).as_posix())
            for name in ('metadata_scope_before','metadata_scope_after','metadata_scope_check','metadata_storage_check'):
                allowed.add((folder/f'{name}_{attempt}.json').relative_to(ROOT).as_posix())
            call_prefix=call.relative_to(ROOT).as_posix()+'/'
            unexpected=[n for n in set(before)|set(after) if before.get(n)!=after.get(n) and n not in allowed and not n.startswith(call_prefix)]
            write(scope_path,{'unexpected':unexpected})
        if unexpected: raise RuntimeError('HUMAN_ACTION_REQUIRED: worker scope violation '+str(unexpected))
        sid=worker_session_id(raw,call);export_path=folder/f'worker_completion_{attempt}.json'
        # Native permission/command evidence is mandatory even when no result
        # exists. A failed runner must not skip the audit of commands it ran.
        from .worker_scope import enabled as scoped_commands_enabled
        if scoped_commands_enabled(s) and sid and not native_audited:
            audit_worker_attempt(raw,command,controller,call,job,attempt)
        try:
            exported=load_worker_completion(raw,receipt,call,export_path)
        except (ValueError,TypeError):
            exported=None
        resultpath=work/'result.json'
        if call_route(call)=='codex':
            original=codex_outcome(raw,receipt,resultpath.exists(),
                (call/'stderr.txt').read_text(encoding='utf-8') if (call/'stderr.txt').exists() else '')
        else:
            original=classify(raw,receipt['exit_code'],session_id=sid,session_export=exported,artifact_valid=resultpath.exists())
        outcome,ignored=worker_outcome(raw,receipt,sid,exported,resultpath.exists(),job,s,call)
        outcome_path=folder/f'outcome_{attempt}.json'
        current_outcome={'classification':original,'session_id':sid,
            'requested_model':p['worker'],'requested_variant':p['worker_variant'],
            'provider':p.get('worker_provider','opencode')}
        if outcome_path.exists():
            prior_outcome=read(outcome_path)
            if prior_outcome != current_outcome:
                write(folder/f'outcome_classifier_reconciliation_{attempt}.json',{
                    'original_outcome_sha256':digest(outcome_path),
                    'original_classification':prior_outcome.get('classification'),
                    'corrected_classification':original,
                    'reason':'Codex command output mentioned rate limit; no provider error event or transport rate-limit stderr. The 900-second receipt has no terminal turn or artifact.',
                    'raw_call_preserved':True,'new_independent_trial':False})
        else:
            write(outcome_path,current_outcome)
        if ignored: write(folder/f'outcome_reconciled_{attempt}.json',{'original':original,'effective':outcome,'optional_missing_directories':ignored,'raw_receipt_preserved':True})
        if outcome=='TIMEOUT_OR_INTERRUPTED' and receipt.get('process_reaped') and not resultpath.exists():
            parsed,_=events(raw)
            ended=[e['part']['state'] for e in parsed if e.get('type')=='tool_use' and e.get('part',{}).get('tool') in ('shell','bash','execute') and e.get('part',{}).get('state',{}).get('status')=='completed']
            trace=ended[-1].get('output','')[-5000:] if ended else ''
            failed_entry=bool(ended and ended[-1].get('metadata',{}).get('metadata',{}).get('exit') not in (None,0))
            write(folder/f'failure_{attempt}.json',{'code':execution_failure(trace) if failed_entry else 'GENERIC_IMPLEMENTATION_TIMEOUT','runtime_outcome':outcome,'details':'The standalone process ended at its bounded timeout. Continue from saved analysis.py with the smallest correction. No completed artifact exists. Last completed entry output:\n'+trace,
                'attempt':attempt,'session_id':sid,'analysis_sha256':digest(work/'analysis.py') if (work/'analysis.py').exists() else None,'input_hashes':{i['path']:i['sha256'] for i in job['inputs']}})
            failure=read(folder/f'failure_{attempt}.json')['details']
            if call_route(call)=='codex' and not (work/'analysis.py').exists():
                raise RuntimeError('ENGINEERING_BLOCKED: Codex worker timed out before producing an implementation; return failure evidence to Lead')
            continue
        if outcome=='PROVIDER_ERROR' and not resultpath.exists():
            errors=runtime_errors(raw,sid)
            if errors and all(e.get('type')=='provider.invalid-request' and 'reasoning item' in e.get('message','') and 'expired' in e.get('message','') for e in errors):
                failure='Interrupted session contains an expired provider reasoning reference. A fresh bounded session must implement the unchanged job. No implementation or result was produced by this failed provider call.'
                write(folder/f'failure_{attempt}.json',{'code':'PROVIDER_EXPIRED_REASONING_REFERENCE','details':failure,'attempt':attempt,'session_id':sid,'analysis_sha256':digest(work/'analysis.py') if (work/'analysis.py').exists() else None,'input_hashes':{i['path']:i['sha256'] for i in job['inputs']}})
                continue
            provider_type=(errors[0].get('type','provider-error') if errors else 'provider-error')
            provider_code='PROVIDER_'+str(provider_type).split('.')[-1].replace('-','_').upper()
            failure='Provider-side failure before the fixed command produced an authenticated artifact; preserve the raw receipt and retry only within the existing bounded repair/call policy.\n'+json.dumps(errors,ensure_ascii=False)
            write(folder/f'failure_{attempt}.json',{'code':provider_code,'runtime_outcome':outcome,'details':failure,
                'attempt':attempt,'session_id':sid,'analysis_sha256':digest(work/'analysis.py') if (work/'analysis.py').exists() else None,
                'input_hashes':{i['path']:i['sha256'] for i in job['inputs']}})
            failure=read(folder/f'failure_{attempt}.json')['details']
            continue
        if outcome in ('AUTH','RATE_LIMIT','PERMISSION_DENIED','SESSION_MISMATCH','TIMEOUT_OR_INTERRUPTED','MISSING_TERMINAL_OUTCOME'):
            raise RuntimeError('HUMAN_ACTION_REQUIRED: worker runtime '+outcome)
        if (outcome in ('INVALID_OR_MISSING_ARTIFACT','PROCESS_FAILED') and call_route(call)=='codex'
                and not resultpath.exists()):
            stderr=(call/'stderr.txt').read_text(encoding='utf-8') if (call/'stderr.txt').exists() else ''
            blocked=('read-only sandbox' in stderr.lower() or 'blocked by policy' in stderr.lower())
            argument_conflict=('cannot be used with' in stderr.lower()
                               and '--approve-for-me' in stderr
                               and '--sandbox' in stderr)
            code=('CODEX_WORKSPACE_APPROVAL_ROUTE_MISCONFIGURATION' if blocked else
                  'CODEX_CLI_ARGUMENT_CONFLICT' if argument_conflict else
                  'CODEX_MISSING_ARTIFACT')
            failure=('Codex terminal session produced no result artifact. '
                     + ('The first R07 adapter used a non-interactive mode that was reduced to read-only; use the bounded auto-review workspace-write route on the next attempt. '
                        if blocked else '')
                     + ('The auto-review flag already selects workspace-write and cannot be combined with an explicit sandbox flag; remove only the duplicate sandbox argument. '
                        if argument_conflict else '')
                     + stderr[-3000:])
            write(folder/f'failure_{attempt}.json',{
                'code':code,'runtime_outcome':outcome,'details':failure,
                'attempt':attempt,'session_id':sid,
                'analysis_sha256':digest(work/'analysis.py') if (work/'analysis.py').exists() else None,
                'input_hashes':{i['path']:i['sha256'] for i in job['inputs']},
                'new_independent_trial':False})
            continue
        if not completion_matches(call,exported,controller.state):
            raise RuntimeError('HUMAN_ACTION_REQUIRED: worker runtime identity not verified')
        shells=validate_worker_history(raw,command,controller,call)
        if outcome=='SUCCESS':
            verification=verify_result(controller,job,folder,attempt)
            if verification.returncode==0:
                return s['completed'][jid]
            failure=verification.stderr[-5000:];code='VERIFICATION_'+failure.strip().splitlines()[-1][:200]
        else:
            failure='\n'.join(str(x.get('output',''))[-4000:] for x in shells);code='EXECUTION_'+outcome
            code=execution_failure(failure)
        write(folder/f'failure_{attempt}.json',{'code':code,'details':failure,'attempt':attempt,'session_id':sid,'analysis_sha256':digest(work/'analysis.py') if (work/'analysis.py').exists() else None,'input_hashes':{i['path']:i['sha256'] for i in job['inputs']}})
    controller.check_budget()
    raise RuntimeError('ENGINEERING_BLOCKED: bounded invocation history exhausted')

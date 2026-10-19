"""Persisted, single-writer PAYOUT_V1 controller for registered evidence jobs.

Lead calls are mandatory. Unregistered proposals are saved and fail closed;
they cannot silently replace a frozen job or gain dataset/write permission.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import time
import jsonschema

from .gate_zero import ROOT, BASE, native_command, env_clean, invoke, write, run_with_inactivity_guard, background_process_options
from .prepare import CAMP, digest
from .replay_job import load_job, calculate
from tools.agent_loop.opencode_runner import extract_text, extract_tool_calls
from tools.agent_loop.runtime_outcome import classify

RUNTIME=CAMP/'runtime'
AUDIT_MANIFEST=CAMP/'continuations/G01_GENERIC_JOBS/AUDIT_SUPPLEMENT_20260922.json'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _lean_payout_context(state, profile, now=None):
    """Pinned R27 decision evidence; does not replace historical job contracts."""
    from .r03 import lean_payout_authority, lean_payout_profile, lean_payout_deadline_elapsed, LEAN_AUTHORIZATION
    authority = lean_payout_authority(state)
    if authority is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    expected_base = LEAN_AUTHORIZATION.parent
    values = {}
    for key, suffix in (('lean_payout_profile', 'EFFECTIVE_PROFILE.json'),
                        ('lean_payout_business_report', 'business_screen/report.json'),
                        ('lean_payout_business_registration', 'business_screen/REGISTRATION.json')):
        ref = state.get(key) or {}
        path = (ROOT / ref.get('path', '')).resolve()
        if (path != (ROOT / expected_base / suffix).resolve() or not path.is_file()
                or digest(path) != ref.get('sha256')):
            raise ValueError('Lean payout context missing/changed pin: ' + key)
        values[key] = read(path)
    prospective = lean_payout_profile(state, profile['effective_profile'])
    if values['lean_payout_profile'] != prospective:
        raise ValueError('Lean payout profile differs from prospective authority')
    registration = values['lean_payout_business_registration']
    for ref in registration.get('pins', []):
        path = (ROOT / ref['path']).resolve()
        if not path.is_relative_to(ROOT.resolve()) or digest(path) != ref['sha256']:
            raise ValueError('Lean payout business source pin mismatch')
    report = values['lean_payout_business_report']
    view = {k:v for k,v in report.items() if k not in
            ('records', 'reconciliation', 'source_correction_comparison',
             'source_correction_changed_saved_days', 'quarterly_metrics_unchanged')}
    view['records'] = [{k:v for k,v in row.items() if k not in ('weekly', 'rounded_reward_risk')}
                       for row in report.get('records', [])]
    business_qa = None
    if state.get('lean_payout_business_qa'):
        ref = state['lean_payout_business_qa']
        path = (ROOT / ref['path']).resolve()
        if not path.is_relative_to(ROOT.resolve()) or digest(path) != ref['sha256']:
            raise ValueError('Lean payout business QA hash mismatch')
        business_qa = {'ref':ref, 'report':read(path), 'full_research_acceptance':False}
    return {'authority_ref': state['lean_payout_authority'], 'authority': authority,
            'prospective_profile_ref':state['lean_payout_profile'], 'prospective_profile':prospective,
            'business_report_ref':state['lean_payout_business_report'], 'business_report':view,
            'business_registration_ref':state['lean_payout_business_registration'],
            'independent_business_qa':business_qa,
            'source_refs':registration.get('pins', []), 'now_utc':now.isoformat(),
            'decision_due_utc':authority['decision_due_utc'],
            'deadline_elapsed':lean_payout_deadline_elapsed(state, now),
            'research_accepted':False, 'worker_allowance_added':False,
            'material_static_qa_conflict':'generic_entry Timer(90, os._exit(124)) imposes a fixed 90-second execution limit contrary to the frozen recovery_episode.stop_condition (no fixed elapsed ceiling under R09 report-only authority); unresolved semantic correctness blocks acceptance.',
            'shadow_clock':'28 days begin only at actual capture start; no backdated or synthetic elapsed credit.'}


def _guard_lean_payout_route(state, job, now=None):
    """Deadline blocks further NR7 repair/worker grants, not a Lead disposition."""
    from .r03 import lean_payout_deadline_elapsed
    if not lean_payout_deadline_elapsed(state, now):
        return
    route = ' '.join(str(job.get(k, '')) for k in ('job_id', 'candidate_id', 'objective', 'dependencies'))
    source_job = 'PAYOUT_R26_NR7_CORRECTED_2025_VALIDATION_V1'
    exact_dependency = source_job in json.dumps(job, ensure_ascii=False)
    repair = (job.get('job_class') in ('ENGINEERING', 'DIAGNOSTIC', 'VERIFY')
              or re.search(r'repair|recovery|transport|correct', route, re.IGNORECASE))
    # The deadline parks this blocked route. It does not ban unchanged NR7
    # rules on a genuinely new, separately authorized evidence allocation.
    if exact_dependency or (re.search(r'NR7', route, re.IGNORECASE) and repair):
        raise ValueError('R27_DEADLINE: park blocked NR7 route; no further repair or worker dispatch')


def _recover_codex_decision(stdout):
    """Recover a completed structured Lead response after CLI flush timeout.

    The Codex CLI can emit ``item.completed`` and ``turn.completed`` in its
    JSONL stream just before the wrapper's fixed 300-second timeout kills the
    process, leaving the ``-o`` file unwritten.  This parser accepts only the
    final agent-message JSON from a stream that contains ``turn.completed``;
    normal schema/evidence validation still runs at the call site.  It never
    turns prose or a partial turn into a decision.
    """
    texts=[]
    turn_completed=False
    for line in (stdout or '').splitlines():
        try:
            event=json.loads(line)
        except (TypeError,ValueError):
            continue
        if not isinstance(event,dict):
            continue
        if event.get('type')=='turn.completed':
            turn_completed=True
        if event.get('type')!='item.completed':
            continue
        item=event.get('item')
        if isinstance(item,dict) and item.get('type')=='agent_message' and isinstance(item.get('text'),str):
            texts.append(item['text'])
    if not turn_completed:
        return None
    decoder=json.JSONDecoder()
    for text in reversed(texts):
        candidate=text.strip()
        if candidate.startswith('```'):
            candidate=candidate.strip('`').strip()
            if candidate.startswith('json'):
                candidate=candidate[4:].lstrip()
        starts=[i for i,ch in enumerate(candidate) if ch=='{']
        for start in starts:
            try:
                value,_=decoder.raw_decode(candidate[start:])
            except (TypeError,ValueError):
                continue
            if isinstance(value,dict) and {'campaign_id','evidence_id','action','artifact_refs','next_job'}.issubset(value):
                return value
    return None


def p_remaining(profile,state):
    from .runtime_policy import effective_call_cap, used_call_units
    return effective_call_cap(profile)-used_call_units(state)


def _lead_compact_value(value, depth=0):
    """Bound controller-delivered Lead context without changing persisted evidence."""
    if depth > 3:
        return {'_omitted': 'nested_context'}
    if isinstance(value, dict):
        out={}
        for key,item in value.items():
            encoded=json.dumps(item,ensure_ascii=False,separators=(',',':'))
            if len(encoded)>12000 and isinstance(item,(dict,list)):
                if isinstance(item,list):
                    out[key]={'_count':len(item),'_omitted':'large_list'}
                else:
                    out[key]={'_keys':list(item)[:40],'_omitted':'large_object'}
            elif len(encoded)>12000:
                out[key]=encoded[:12000]+'...[truncated]'
            else:
                out[key]=_lead_compact_value(item,depth+1)
        return out
    if isinstance(value,list):
        if len(value)>40:
            return {'_count':len(value),'_sample':[_lead_compact_value(x,depth+1) for x in value[:2]],'_omitted':'large_list'}
        return [_lead_compact_value(x,depth+1) for x in value]
    if isinstance(value,str) and len(value)>12000:
        return value[:12000]+'...[truncated]'
    return value


def _compact_lead_bundle(bundle):
    compact=dict(bundle)
    evidence=bundle.get('evidence')
    compact['evidence']=_lead_compact_value(evidence)
    if len(json.dumps(bundle.get('decision_view'),ensure_ascii=False))>50000:
        # Metadata diagnostics can embed historical state in their input view.
        # Keep current directive/QA/authority intact; bound that duplicated history.
        compact['decision_view']=_lead_compact_value(bundle['decision_view'])
    profile=bundle.get('profile')
    if isinstance(profile,dict):
        profile_compact={k:v for k,v in profile.items() if k not in ('jobs','source_inventory')}
        if isinstance(profile.get('jobs'),list):
            profile_compact['jobs']=[{k:j.get(k) for k in ('job_id','job_class','candidate_id','operation') if k in j}
                                    for j in profile['jobs']]
        compact['profile']=profile_compact
    # Supplemental audits are advisory and remain hash-pinned on disk.  Keep
    # a bounded excerpt in the provider transport so an old audit cannot make
    # the Astra call spend its entire timeout loading prose instead of making
    # the persisted campaign decision.
    audit=bundle.get('supplemental_audit')
    if isinstance(audit,dict) and isinstance(audit.get('files'),list):
        audit_compact=dict(audit)
        files=[]
        for item in audit['files']:
            if not isinstance(item,dict):
                files.append(item)
                continue
            copied=dict(item)
            content=copied.get('content')
            if isinstance(content,str) and len(content)>12000:
                copied['content']=(content[:6000]+'\n...[audit transport excerpt omitted; '
                                   'read the hash-pinned file from the supplied path if needed]...\n'
                                   +content[-6000:])
                copied['content_transport_truncated']=True
            elif isinstance(content,(dict,list)):
                copied['content']=_lead_compact_value(content)
                copied['content_transport_compacted']=True
            files.append(copied)
        audit_compact['files']=files
        audit_compact['_transport_note']='Full audit files remain persisted and hash-pinned.'
        compact['supplemental_audit']=audit_compact
    # Recovery history is durable audit state, not a prompt-sized research
    # corpus.  Keep the identities, bounded root-cause/approach/stop notes and
    # attempt receipts needed for the next decision while leaving the full
    # hash-pinned authorizations on disk.
    recoveries=bundle.get('lead_recovery_authorizations')
    if isinstance(recoveries,dict):
        recovery_compact={}
        for job_id,item in recoveries.items():
            if not isinstance(item,dict):
                recovery_compact[job_id]=item
                continue
            episode=item.get('episode') if isinstance(item.get('episode'),dict) else {}
            attempts=item.get('attempts') if isinstance(item.get('attempts'),list) else []
            recovery_compact[job_id]={
                'decision_index':item.get('decision_index'),
                'episode':{
                    'component':str(episode.get('component',''))[:500],
                    'root_cause':str(episode.get('root_cause',''))[:1000],
                    'changed_approach':str(episode.get('changed_approach',''))[:1000],
                    'stop_condition':str(episode.get('stop_condition',''))[:800],
                    'evidence_refs':episode.get('evidence_refs',[]),
                    'max_attempts':episode.get('max_attempts'),
                },
                'instruction':str(item.get('instruction',''))[:1000],
                'attempts':[
                    {'code':a.get('code'),'evidence':a.get('evidence')}
                    for a in attempts if isinstance(a,dict)
                ],
            }
        compact['lead_recovery_authorizations']=recovery_compact
    compact['_context_note']='Full evidence remains persisted in the controller evidence file; large arrays were compacted only for transport-size safety.'
    return compact


_DECISION_MAJOR_KEYS={
    'records','case_metrics','case_summaries','coverage_matrix','final_selection',
    'unresolved_questions','limitations','reconciliation','policy_snapshot',
    'contract_check','daily','weekly','bound_comparison','frozen_starts',
    'defects','unresolved','synthetic_checks','normalized_daily_checks',
    'concentration_review','paths','qualifier','qualification','lifecycle',
    'cash_transition','target_gaps','unknowns','verdicts','actual_cash_received',
    'ready_for_forward','independent_market_evidence'
}
_DECISION_OMIT_KEYS={
    'input_hashes','daily_rows','mark_rows','trade_rows','daily_checks','weeks',
    'trades','marks','daily','saved_transitions','independent_transitions',
    'stage_transitions','retained_paths','source_inventory','allowed_paths'
}


def _decision_view_value(value, key='', depth=0):
    """Keep decision-bearing scalars while bounding transport-only payloads.

    The full pinned artifact remains on disk and is hash-checked.  This view is
    a deterministic read-only adapter for the Lead: candidate/scenario
    identities, metrics, gaps, coverage, unknowns and verdicts survive even
    when the raw ledger payload is larger than the provider input limit.
    """
    if isinstance(value,(str,int,float,bool)) or value is None:
        return value
    if depth>5:
        if isinstance(value,dict):
            return {k:v for k,v in value.items() if isinstance(v,(str,int,float,bool)) or v is None}
        return {'_count':len(value)} if isinstance(value,list) else str(value)
    if isinstance(value,list):
        if key in _DECISION_OMIT_KEYS:
            return {'_count':len(value),'_omitted':'transport_detail'}
        if len(value)>48:
            sample=value[:3]+value[-3:]
            return {'_count':len(value),'_sample':[_decision_view_value(x,key,depth+1) for x in sample],
                    '_omitted':'middle_items'}
        return [_decision_view_value(x,key,depth+1) for x in value]
    out={}
    for k,v in value.items():
        if k in _DECISION_OMIT_KEYS:
            if isinstance(v,(list,dict)):
                out[k]={'_count':len(v),'_omitted':'transport_detail'}
            continue
        if (depth<=1 or k in _DECISION_MAJOR_KEYS or
                isinstance(v,(str,int,float,bool)) or v is None):
            out[k]=_decision_view_value(v,k,depth+1)
    return out


def _decision_view(job, state):
    """Read only exact, hash-pinned JSON inputs for the pending Lead review."""
    if not job:
        return {}
    items=[]
    for item in job.get('inputs',[]):
        path=ROOT/item['path']
        if digest(path)!=item['sha256']:
            raise ValueError('Pinned Lead input changed: '+item['path'])
        if item.get('kind')!='json':
            items.append({'alias':item['alias'],'path':item['path'],'sha256':item['sha256'],
                          'partition':item['partition'],'kind':item.get('kind'),
                          'view':'non-JSON input retained by exact pin; no arbitrary retrieval'})
            continue
        raw=read(path)
        payload=raw.get('payload',raw) if isinstance(raw,dict) else raw
        items.append({'alias':item['alias'],'path':item['path'],'sha256':item['sha256'],
                      'partition':item['partition'],'kind':item.get('kind'),
                      'evidence_id':raw.get('evidence_id') if isinstance(raw,dict) else None,
                      'job_class':raw.get('job_class') if isinstance(raw,dict) else None,
                      'view':_decision_view_value(payload)})
    return {'job_id':job.get('job_id'),'source':'exact pending_job input hashes',
            'full_artifacts_persisted':True,'inputs':items}


def _user_research_directive(state):
    """Deliver a user-authored, hash-pinned scope without altering research gates."""
    ref = state.get('user_research_directive')
    if not ref:
        return None
    relative = Path(ref['path'])
    path = (ROOT / relative).resolve()
    base = (ROOT / 'research/campaigns/PAYOUT_V1/revisions').resolve()
    if relative.is_absolute() or not path.is_relative_to(base):
        raise ValueError('User directive path outside campaign revisions')
    if digest(path) != ref['sha256']:
        raise ValueError('User directive identity mismatch')
    content = read(path)
    if content.get('revision_id') != ref['revision_id']:
        raise ValueError('User directive revision mismatch')
    return dict(ref, content=content,
                authority='Explicit user research scope; no additional data, risk, spend or live permission')


def _supplemental_audit():
    """Load only the two user-supplied GPT Pro audits through pinned paths."""
    if not AUDIT_MANIFEST.exists():
        return None
    manifest=read(AUDIT_MANIFEST)
    out=[]
    for item in manifest.get('source_files',[]):
        if item.get('role')!='SUPPLEMENTAL_AUDIT':
            continue
        path=ROOT/item['path']
        if digest(path)!=item['sha256']:
            raise ValueError('Supplemental audit hash changed: '+item['path'])
        content=path.read_text(encoding='utf-8')
        if path.suffix.lower()=='.json':
            content=json.loads(content)
        out.append({'path':item['path'],'sha256':item['sha256'],'bytes':item['bytes'],'content':content})
    return {'kind':'SUPPLEMENTAL_AUDIT_EVIDENCE','manifest_path':AUDIT_MANIFEST.relative_to(ROOT).as_posix(),
            'manifest_sha256':digest(AUDIT_MANIFEST),'authority':'advisory evidence only; no mandate or permission change',
            'files':out}


def completion_export(original):
    """Retain terminal/error provenance while dropping transcript content.

    The shared classifier requires matching info AND an actual idle outcome.
    An info-only minimizer cannot establish completion and must not be used.
    """
    info=original.get('info',{})
    result={'info':{k:info[k] for k in ('id','model','cost','outcome','time','status') if k in info},'messages':[]}
    for message in original.get('messages',[]):
        if message.get('type')=='idle':
            result['messages'].append({k:message[k] for k in ('type','outcome') if k in message})
        elif message.get('type')=='assistant' and message.get('error') is not None:
            error=message['error']
            result['messages'].append({'type':'assistant','error':
                {k:error[k] for k in ('type','status') if k in error} if isinstance(error,dict) else {}})
    return result


def snapshot(root=ROOT):
    names=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=root,
                                  **background_process_options()).decode().split('\0')
    owned_locks={'research/campaigns/PAYOUT_V1/runtime/writer.lock',
                 'research/campaigns/PAYOUT_V1/runtime/controller.lock'}
    # Advisory lock bytes are not an experiment input and Windows forbids
    # reading the locked byte. Preserve and hash every other file as before.
    return {n:digest(root/n) for n in names if n and n not in owned_locks and (root/n).is_file()}


@contextlib.contextmanager
def writer_lock(path):
    # OS-released advisory lock survives neither process exit nor crash.
    import msvcrt
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as f:
        if path.stat().st_size==0:
            f.write(b'0'); f.flush()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            raise RuntimeError('One controller writer already active')
        try:
            yield
        finally:
            f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)


SCHEMA={'type':'object','additionalProperties':False,'properties':{
    'campaign_id':{'type':'string','enum':['PAYOUT_V1']},
    'evidence_id':{'type':'string'},
    'action':{'type':'string','enum':['NEW_CANDIDATE','RUN_BASELINE','RUN_VARIANT',
        'REPLAY_ACCOUNT_POLICY','FIX_EXPERIMENT','READY_FOR_FORWARD','PAUSE']},
    'rationale':{'type':'string'},
    'artifact_refs':{'type':'array','items':{'type':'string'}},
    'next_job':{'anyOf':[{'type':'null'},{'type':'object','additionalProperties':False,
        'properties':{'job_id':{'type':'string'}},'required':['job_id']}]},
    'missing_information':{'type':'array','items':{'type':'string'}},
    'worker_dispatch_ready':{'type':'boolean'},
    'pause_category':{'type':'string','enum':['NONE','DATA_OR_RULES_BLOCKED','PAUSED_LEAD','PAUSED_BUDGET']}}}
SCHEMA['required']=list(SCHEMA['properties'])


def _generic_contract_instruction():
    """The same exact validator contract applies to concise and legacy prompts."""
    from .generic_jobs import PROHIBITED
    return (
        'Contract constants: prohibited_actions must be EXACTLY '+json.dumps(PROHIBITED)+
        ', with no extra entries; put job-specific restrictions in objective/verification. '
        'permissions={"network":"none","spend_usd":0,"live":false}. '
        'Every new contract includes risk_ceiling_usd. Inputs have unique aliases, exact authorized path/sha256/partition and kind json/csv/text; allowed_data equals exactly those input paths. '
        'allowed_file_scope and required_outputs each contain exactly research/campaigns/PAYOUT_V1/runtime/jobs/JOB_ID/work/analysis.py and .../work/result.json, plus declared ENGINEERING write_paths only. '
        'verification.method=reproduce_json; required_keys name payload keys; min_records checks payload.records. '
        'ENGINEERING implementation.write_paths are exact tools/NAME.py; pin existing source as RESEARCH_CODE inputs. '
        'test_modules are tools.test_NAME, each pinned or authored in write_paths; include write_paths in both allowed_file_scope and required_outputs. '
        'Financial trades retain gross_pnl,commission,slippage_cost,net_pnl,planned_risk_usd,instrument. '
    )


def _lean_work_order(decision, profile):
    """Keep new R25 handoffs brief; historical frozen contracts stay intact."""
    from .model_routing import LEAN_REVISION
    if profile.get('model_route_authority', {}).get('revision_id') != LEAN_REVISION:
        return
    job = decision.get('next_job') or {}
    fields = []
    if 'job_class' in job:
        fields.append(('objective', job.get('objective', ''), 2200))
    episode = job.get('recovery_episode') or {}
    if episode:
        fields.append(('changed_approach', episode.get('changed_approach', ''), 1200))
    for name, text, limit in fields:
        if len(text) > limit or '```' in text or re.search(r'(?m)^\s*(?:def |class |import |from \S+ import |function )', text):
            raise ValueError(f'Lean handoff: {name} must be a brief outcome, not embedded code or an implementation recipe')


def validate_decision(decision,state,profile):
    from .generic_jobs import authorized,validate_spec,LEAD_SPEC
    schema=json.loads(json.dumps(SCHEMA))
    if authorized(state,profile):
        schema['properties']['next_job']['anyOf'].append(LEAD_SPEC)
        schema['properties']['action']['enum']+=['NO_QUALIFYING_CANDIDATE','HUMAN_ACTION_REQUIRED']
    from .safety_hold import delegated_review_allowed
    delegated_review=delegated_review_allowed(state)
    if delegated_review:
        schema['properties']['action']['enum'].append('RELEASE_REPAIRED_HOLD')
    jsonschema.validate(decision,schema)
    _lean_work_order(decision, profile)
    if decision['evidence_id']!=state['evidence_id']:
        raise ValueError('Stale evidence decision')
    if not decision['rationale'].strip() or not decision['artifact_refs']:
        raise ValueError('No evidence-backed rationale')
    if decision['worker_dispatch_ready'] != bool(decision['next_job']):
        raise ValueError('Unmet dispatch prerequisites require PAUSE with no next_job; terminal decisions cannot authorize a worker')
    if delegated_review and (decision['next_job'] is not None or decision['worker_dispatch_ready']):
        raise ValueError('Delegated incident review cannot dispatch a worker before hold release')
    if decision['action']=='RELEASE_REPAIRED_HOLD':
        qa=state.get('latest_qa_review') or {}
        required_refs={state['operator_safety_hold']['incident'],state['delegated_incident_review']['path'],qa.get('path')}
        if (qa.get('verdict')!='PASS' or qa.get('incident_review') is not True
                or qa.get('repair_packet')!=state['delegated_incident_review']
                or not required_refs.issubset(set(decision['artifact_refs']))):
            raise ValueError('Delegated release requires current incident QA PASS and exact repair evidence refs')
    if decision['action'] in ('PAUSE','READY_FOR_FORWARD','NO_QUALIFYING_CANDIDATE','HUMAN_ACTION_REQUIRED','RELEASE_REPAIRED_HOLD'):
        if decision['next_job'] is not None:
            raise ValueError('Terminal decision contains a job')
    elif decision['next_job'] is None:
        raise ValueError('Nonterminal decision missing job')
    if decision['next_job']:
        _guard_lean_payout_route(state, decision['next_job'])
        jobid=decision['next_job']['job_id']
        if 'job_class' in decision['next_job'] and authorized(state,profile):
            validate_spec(decision['next_job'],profile,state)
            return decision
        if authorized(state,profile) and jobid in state.get('generic_registry',{}):
            frozen_path=RUNTIME/'jobs'/jobid/'job.json'
            if digest(frozen_path)!=state['generic_registry'][jobid]['sha256']:
                raise ValueError('Registered generic job hash mismatch')
            validate_spec(read(frozen_path),profile,state)
            _guard_lean_payout_route(state, read(frozen_path))
            return decision
        from .model_routing import LEAN_REVISION
        if profile.get('model_route_authority', {}).get('revision_id') == LEAN_REVISION:
            raise ValueError('Lean handoff: select a generic job for the authorized Sol executor route')
        if jobid not in {j['job_id'] for j in profile['jobs']}:
            raise ValueError('Unregistered proposal requires scoped implementation before execution')
        if jobid in state['completed']:
            raise ValueError('Completed deterministic job cannot become a new trial')
        job=next(j for j in profile['jobs'] if j['job_id']==jobid)
        family=job['candidate_id']
        if (family not in state.get('families',{}) and
            len(state.get('families',{}))>=profile.get('max_families',6)):
            raise ValueError('Candidate family budget exhausted')
        if state.get('families',{}).get(family,0)>=profile.get('max_per_family',3):
            raise ValueError('Baseline plus two variants exhausted')
    if decision['action']=='READY_FOR_FORWARD':
        if not state.get('qualification_verified',False):
            raise ValueError('External qualification gates not passed')
    return decision


def guard_lead_dispatch(state, job):
    """Require an explicit, pinned Lead dispatch grant; prose is not a grant."""
    try:
        _guard_lean_payout_route(state, job)
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    ref = state.get('worker_dispatch_decision') or {}
    path = (ROOT / ref.get('path', '')).resolve()
    base = (RUNTIME / 'decisions').resolve()
    if (not path.is_relative_to(base) or path.name != 'accepted.json'
            or ref.get('path') != state.get('last_decision')
            or not path.is_file() or digest(path) != ref.get('sha256')):
        raise RuntimeError('LEAD_DISPATCH_BLOCKED: missing or changed explicit Lead dispatch grant')
    decision = read(path)
    if (decision.get('worker_dispatch_ready') is not True
            or (decision.get('next_job') or {}).get('job_id') != job['job_id']
            or decision.get('action') in ('PAUSE', 'READY_FOR_FORWARD',
                                        'NO_QUALIFYING_CANDIDATE', 'HUMAN_ACTION_REQUIRED')):
        raise RuntimeError('LEAD_DISPATCH_BLOCKED: Lead has not authorized this worker to start')


class Controller:
    def __init__(self):
        if (RUNTIME/'active_profile.json').exists():
            from .r03 import active_profile
            self.profile=active_profile()
        else:
            self.profile=read(RUNTIME/'profile.json')
            if digest(RUNTIME/'profile.json')!=read(RUNTIME/'profile.sha256.json')['sha256']:
                raise ValueError('Protected profile changed')
        p=self.profile
        from .model_routing import MODEL, REASONING
        if (p['lead'],p['worker'],p['spend_authorized'],p['live']) not in (
            ('gpt-6-astra','opencode/muse-spark-1.3-contributor-free',0,'DISABLED'),
            (MODEL,'opencode/muse-spark-1.3-contributor-free',0,'DISABLED')):
            raise ValueError('Runtime route/financial permission mismatch')
        if ((p['lead'] == MODEL and p.get('reasoning') != REASONING)
                or (p['lead'] == 'gpt-6-astra' and p.get('reasoning') not in ('medium', 'xhigh'))
                or p.get('worker_variant') not in ('xhigh',)):
            raise ValueError('Runtime reasoning/worker variant mismatch')
        gate=read(BASE/'latest_gate.json')
        if gate['status']!='GATE_ZERO_PASSED':
            raise ValueError('Real Lead-worker gate missing')
        self.state_path=RUNTIME/'state.json'
        self.state=read(self.state_path) if self.state_path.exists() else dict(
            campaign_id='PAYOUT_V1',run_id='PAYOUT_V1_C001',status='INITIALIZED',
            evidence_id='PAYOUT_V1_INVENTORY',completed={},pending_job=None,
            model_calls=gate['model_calls'],active_invocation_s=gate['active_invocation_s'],
            prior_reserved_calls=p['prior_reserved_calls'],prior_reserved_seconds=p['prior_reserved_seconds'],
            bootstrap_invocations=1, trials=0,families={},decisions=0,
            transport_retries=0,implementation_repairs=0,integration_fix_cycles=gate['integration_fix_cycles'],
            qualification_verified=False,live='DISABLED',human_forwarding_count=0)
        if self.state_path.exists() and self.state.get('generic_authority'):
            # Apply the explicitly authorized G01 runtime revision while
            # retaining the historical PAUSED_BUDGET receipt in state. This
            # only changes the cumulative-time gate; all other permissions
            # remain validated by the existing profile and generic contract.
            from .runtime_policy import migrate_state,apply_runtime_budget
            self.state,_=migrate_state(self.state,p)
            # Runtime call authority is a separate overlay.  The pinned R03
            # financial/effective profile and its hash remain unchanged.
            self.profile=apply_runtime_budget(self.profile,self.state)
        from .lead_recovery import apply
        self.profile=apply(self.profile,self.state)
        effective_route=(self.profile.get('lead'), self.profile.get('reasoning'),
                         self.profile.get('worker_provider','opencode'),
                         self.profile.get('worker'), self.profile.get('worker_variant'),
                         self.profile.get('spend_authorized'), self.profile.get('live'))
        from .runtime_policy import REVISION_CODEX_LUNA, REVISION_GPT6_LUNA, REVISION_UNBOUNDED
        expected_route=(
            ('gpt-6-astra','xhigh','codex','gpt-6-luna','max',0,'DISABLED')
            if self.state.get('runtime_policy_revision') in (REVISION_GPT6_LUNA, REVISION_UNBOUNDED) else
            ('gpt-6-astra','xhigh','codex','gpt-5.6-luna','max',0,'DISABLED')
            if self.state.get('runtime_policy_revision') == REVISION_CODEX_LUNA else
            (p['lead'],self.profile.get('reasoning'),'opencode',
             'opencode/muse-spark-1.3-contributor-free','xhigh',0,'DISABLED'))
        if effective_route != expected_route:
            raise ValueError('Effective runtime route/financial permission mismatch')
        # Historical authorities above retain their original model identities.
        # The separately pinned user revision changes future Lead/QA calls only.
        from .model_routing import apply as apply_model_route
        self.profile=apply_model_route(self.profile,self.state)
        self.save()

    def save(self):
        write(self.state_path,self.state)

    def check_budget(self):
        s,p=self.state,self.profile
        from .r03 import activation_authorized
        from .generic_jobs import accounted_seconds
        from .runtime_policy import cumulative_runtime_cap_bypassed
        runtime_revision=cumulative_runtime_cap_bypassed(s,p)
        from .runtime_policy import effective_call_cap, global_remaining_call_units, used_call_units
        used=used_call_units(s)
        operational_cap=effective_call_cap(p)
        calls_exhausted=(used>=operational_cap)
        global_calls_exhausted=(used>=int(p['max_calls']))
        trials_exhausted=s['trials']>=p['max_configs']
        # Do not turn the report-only cumulative clock into a stop gate under
        # the validated addendum. We still compute it for the legacy path.
        time_exhausted=(False if runtime_revision else accounted_seconds(s)>=p['max_active_seconds'])
        if calls_exhausted or trials_exhausted or (time_exhausted and not runtime_revision):
            if runtime_revision and (calls_exhausted or trials_exhausted):
                s['status']='RESOURCE_LIMIT'
                reason=('global call-unit cap' if global_calls_exhausted else
                        ('campaign call stop cap' if calls_exhausted else 'global trial cap'))
                s['error']='RESOURCE_LIMIT: '+reason
                self.save()
                raise RuntimeError('RESOURCE_LIMIT')
            s['status']='PAUSED_BUDGET'; self.save()
            raise RuntimeError('PAUSED_BUDGET')
        from .generic_jobs import authorized
        if s.get('integration_fix_cycles',0)>=p.get('max_integration_fixes',3) and not (activation_authorized(s,p) or authorized(s,p)):
            s['status']='ENGINEERING_BLOCKED'; self.save()
            raise RuntimeError('ENGINEERING_FIX_ALLOWANCE_EXHAUSTED')

    def model_call(self,cmd,prompt,folder,*,cwd=None,timeout_seconds=300,native_session=None):
        # A skill checkpoint can finish after the operator requests a pause.
        # Respect that request before charging or dispatching the next call.
        if any((RUNTIME/name).exists() for name in ('pause.request', 'cancel.request')):
            self.state['status']='PAUSED_USER'
            self.save()
            raise RuntimeError('PAUSED_USER: operator stop requested before model dispatch')
        self.check_budget()
        from .qa_review import cycle_reserve
        if folder.name.startswith('worker_call_') and p_remaining(self.profile,self.state)<cycle_reserve(self.state):
            self.state['status']='RESOURCE_LIMIT'
            self.state['error']='RESOURCE_LIMIT: worker, QA and Lead cycle cannot be reserved'
            self.save()
            raise RuntimeError(self.state['error'])
        from .safety_hold import guard_call
        guard_call(self, folder)
        self.state['model_calls']+=1
        self.state['inflight_call']=str(folder.relative_to(ROOT))
        self.save()  # reserve before dispatch; crash never makes a call free
        from .generic_jobs import accounted_seconds
        from .runtime_policy import cumulative_runtime_cap_bypassed, REVISION_UNBOUNDED
        if self.state.get('runtime_policy_revision')==REVISION_UNBOUNDED:
            timeout=None
        elif cumulative_runtime_cap_bypassed(self.state,self.profile):
            # The addendum removes only cumulative-time-based shortening. The
            # explicit per-call timeout supplied by Lead/worker remains active.
            timeout=max(1,int(timeout_seconds))
        else:
            remaining=self.profile['max_active_seconds']-accounted_seconds(self.state)
            # Before the addendum, generic calls were bounded by the remaining
            # cumulative allowance; keep that historical behavior for states
            # without the validated revision.
            timeout=max(1,min(timeout_seconds,int(remaining/2)))
        started_utc=dt.datetime.now(dt.timezone.utc).isoformat()
        if cwd is None:
            result,raw=invoke(cmd,prompt,folder,timeout=timeout)
        else:
            folder.mkdir(parents=True,exist_ok=False)
            (folder/'prompt.txt').write_text(prompt,encoding='utf-8')
            if native_session is None:
                write(folder/'request.json',{'argv':cmd,'cwd':str(cwd),'started_utc':started_utc})
            started=time.monotonic()
            stdin_prompt = prompt if cmd and cmd[-1] == '-' else ''
            if native_session is not None:
                result,raw=native_session.invoke(prompt,folder,
                    inactivity_seconds=self.state['runtime_policy']['inactivity_stall_seconds'],
                    cancel_path=RUNTIME/'cancel.request',timeout=timeout)
            elif timeout is None:
                result,raw,stderr=run_with_inactivity_guard(
                    cmd,input_text=stdin_prompt,cwd=cwd,env=env_clean(),
                    stdout_path=folder/'stdout.jsonl',stderr_path=folder/'stderr.txt',
                    inactivity_seconds=self.state['runtime_policy']['inactivity_stall_seconds'],
                    cancel_path=RUNTIME/'cancel.request')
            else:
                try:
                    proc=subprocess.run(cmd,input=stdin_prompt,capture_output=True,text=True,
                        encoding='utf-8',errors='replace',cwd=cwd,env=env_clean(),timeout=timeout,
                        **background_process_options())
                    result={'exit_code':proc.returncode,'elapsed_s':time.monotonic()-started,'process_reaped':True}
                    raw=proc.stdout
                    (folder/'stderr.txt').write_text(proc.stderr,encoding='utf-8')
                except subprocess.TimeoutExpired as e:
                    # subprocess.run kills and waits for its direct child before
                    # raising. Preserve partial events, never invent completion.
                    result={'exit_code':None,'elapsed_s':time.monotonic()-started,'process_reaped':True}
                    raw=e.stdout or ''
                    if isinstance(raw,bytes): raw=raw.decode('utf-8','replace')
                    err=e.stderr or ''
                    if isinstance(err,bytes): err=err.decode('utf-8','replace')
                    (folder/'stderr.txt').write_text(err+'\nTIMEOUT: interrupted call outcome unknown',encoding='utf-8')
            if timeout is not None:
                (folder/'stdout.jsonl').write_text(raw,encoding='utf-8')
        result.update(started_utc=started_utc,finished_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                      timing_basis='observed_utc_with_monotonic_elapsed')
        write(folder/'receipt.json',result)
        self.state['active_invocation_s']+=result['elapsed_s']
        self.state.pop('inflight_call',None);self.save()
        return result,raw

    def lead(self):
        s,p=self.state,self.profile
        index=s['decisions']
        folder=RUNTIME/'decisions'/f'{index:03d}'
        if p.get('model_route_authority'):
            # Never label a retry of an old-model call as an unchanged route.
            # Reconcile saved calls before spending QA/Lead units on this index.
            for saved_call in folder.glob('call_*'):
                if not saved_call.is_dir():
                    continue
                request_path=saved_call/'request.json'
                if not request_path.is_file():
                    raise ValueError('Lead saved route missing; reconcile before retry')
                argv=read(request_path).get('argv', [])
                if ('-m' not in argv or argv.index('-m')+1 >= len(argv)
                        or argv[argv.index('-m')+1] != p['lead']
                        or f'model_reasoning_effort="{p["reasoning"]}"' not in argv):
                    raise ValueError('Lead saved route differs from authorized route; reconcile before retry')
        from .safety_hold import authority as recovery_authority, delegated_review_allowed
        delegated_review=delegated_review_allowed(s)
        held_recovery = None if delegated_review else recovery_authority(s)
        from .skill_engineering import maybe_review, guidance
        maybe_review(self)
        from .qa_review import review as qa_review, RequiredEvidenceIncomplete, preflight_for_lead
        qa_preflight = None
        try:
            qa=qa_review(self)
        except RequiredEvidenceIncomplete as error:
            qa_preflight = preflight_for_lead(s, error)
            qa = None  # Never substitute historical PASS or a fabricated QA verdict.
        if (s.get('autonomous_recovery_authority') and qa and
                'Controller-generated failure envelope; no accepted model QA verdict.'
                in qa.get('report', {}).get('limitations', [])):
            raise RuntimeError('QA provider/session/output incomplete; inspect preserved call receipt')
        maybe_review(self)
        folder.mkdir(parents=True,exist_ok=True)
        evidence=(read(ROOT/s['completed'][s['evidence_id']]['path'])
                  if s['evidence_id'] in s['completed'] else {
                    'status':'INVENTORY','saved_run':'B002_20260912T225535Z',
                    'verification':'PASS in preserved verification.json; fixed-one baseline chosen before payout results',
                    'price_reads_authorized':False,'registered_first_job':p['jobs'][0]})
        if p.get('mandate_revision') and s['evidence_id'] not in s['completed']:
            evidence=read(ROOT/p['activation_evidence_path'])
        bundle={'campaign_id':'PAYOUT_V1','evidence_id':s['evidence_id'],
                'profile':p,'evidence':evidence,'rules':read(ROOT/p.get('rules_path','research/campaigns/PAYOUT_V1/runtime/rules.json'))}
        lean_context = _lean_payout_context(s, p)
        if lean_context is not None:
            bundle['lean_payout_decision'] = lean_context
        from .qa_review import current_incident_disposition
        disposition, disposition_proofs = current_incident_disposition(s)
        bundle['current_incident_disposition'] = disposition
        bundle['current_incident_disposition_proofs'] = disposition_proofs
        recovery=s.get('operator_lead_capacity_recovery')
        if recovery and recovery.get('applied'):
            ref=recovery['authorization']
            if digest(ROOT/ref['path'])!=ref['sha256']:
                raise ValueError('Lead capacity recovery authorization changed')
            bundle['operator_transport_reconciliation']=read(ROOT/ref['path'])
        if p.get('model_route_authority'):
            from .model_routing import authority as model_route_authority
            bundle['active_model_routes']=model_route_authority(s)
        if s.get('autonomous_recovery_authority'):
            from .autonomous_recovery import authority as autonomy_authority, context as autonomy_context
            bundle['autonomous_technical_recovery'] = {
                'authority': autonomy_authority(s), 'incident': autonomy_context(s),
                'review_required': s.get('autonomous_recovery', {}).get('review_required', False)}
        if qa is not None:
            bundle['independent_sol_qa']=qa
        skill_guidance = guidance(s, 'lead')
        if skill_guidance:
            bundle['skill_guidance'] = skill_guidance
        directive = _user_research_directive(s)
        if directive is not None:
            bundle['user_research_directive'] = directive
        from .desktop_capture import recovery_context as metadata_recovery_context
        metadata_recovery = metadata_recovery_context(s)
        if metadata_recovery is not None:
            bundle['acknowledged_metadata_incident'] = metadata_recovery
        if held_recovery is not None:
            bundle['operator_safety_recovery'] = {
                'authorization': held_recovery,
                'progress': s['operator_safety_recovery'],
                'instruction': ('Active hold permits only its authorized DIAGNOSTIC route probe with the exact '
                    'route_probe_inputs, no dependencies, no job.implementation or production-source writes, and one recovery '
                    'attempt per initial episode. The current pinned authorization controls cumulative route calls '
                    'and whether real Lead-selected materially different bounded recovery episodes are permitted; '
                    'never reset counters or retry an unchanged exhausted episode. Implement a small synthetic fixture through the fixed runner, not a call-graph '
                    'report. Preserve V25 INCOMPLETE and V26 failure. Once this probe is verified and Sol '
                    'PASS, review its evidence and select the NR7 production correction/control job. '
                    'Controller will release only this incident after that real roundtrip; no market '
                    'job may dispatch before release.')}
        if s.get('lead_recovery_revision'):
            from .lead_recovery import active_base
            revision_dir=active_base(s)
            bundle['active_authorization']=(revision_dir/'USER_AUTHORIZATION.txt').read_text(encoding='utf-8')
            bundle['recovery_context']=s.get('recovery_context')
            bundle['lead_recovery_authorizations']=s.get('lead_recovery_authorizations',{})
            bundle['latest_result_decision_view']=_decision_view_value(evidence.get('payload',evidence))
            bundle['historical_profiles_are_not_current_objective']=True
        from .generic_jobs import authorized,authority_catalog,LEAD_SPEC,PROHIBITED
        generic=authorized(s,p)
        schema=json.loads(json.dumps(SCHEMA))
        if delegated_review:
            schema['properties']['action']['enum'].append('RELEASE_REPAIRED_HOLD')
            bundle['lead_delegated_incident_review']={
                'authority':s['lead_delegated_recovery_authority'],
                'repair_packet':s['delegated_incident_review'],
                'incident':s['operator_safety_hold']['incident'],
                'qa':qa,
                'worker_allowed':False}
        if generic:
            from .generic_jobs import accounted_seconds
            from .runtime_policy import cumulative_runtime_cap_bypassed
            runtime_revision=cumulative_runtime_cap_bypassed(s,p)
            pending_runtime={}
            if s.get('pending_job'):
                pending_id=s['pending_job']['job_id']
                pending_folder=RUNTIME/'jobs'/pending_id
                failures=[]
                for failure_path in sorted(pending_folder.glob('failure_*.json')):
                    try:
                        failure=read(failure_path)
                        failures.append({'path':failure_path.relative_to(ROOT).as_posix(),'code':failure.get('code'),'attempt':failure.get('attempt')})
                    except (OSError,ValueError):
                        failures.append({'path':failure_path.relative_to(ROOT).as_posix(),'code':'UNREADABLE_FAILURE'})
                pending_runtime={
                    'job_id':pending_id,
                    'analysis_exists':(pending_folder/'work/analysis.py').exists(),
                    'result_exists':(pending_folder/'work/result.json').exists(),
                    'failures':failures,
                    'repair_ledger':s.get('blocker_repairs',{}),
                    'partial_timeout_has_no_research_meaning':True,
                }
                from .qa_review import worker_terminal_evidence
                pending_runtime['worker_terminal_evidence']=worker_terminal_evidence(pending_folder)
            schema['properties']['next_job']['anyOf'].append(LEAD_SPEC)
            schema['properties']['action']['enum']+=['NO_QUALIFYING_CANDIDATE','HUMAN_ACTION_REQUIRED']
            from .runtime_policy import effective_call_cap, global_remaining_call_units
            bundle.update(input_authority_catalog=authority_catalog(p,s),completed_job_ids=list(s['completed']),
                pending_proposal=s.get('pending_proposal'),pending_job=s.get('pending_job'),pending_job_runtime=pending_runtime,
                generic_contract=LEAD_SPEC,
                remaining_call_units=effective_call_cap(p)-s['model_calls']-s['bootstrap_invocations']-s['prior_reserved_calls'],
                global_remaining_call_units=global_remaining_call_units(s,p),
                campaign_stop_cap=p.get('campaign_stop_calls'),
                global_call_cap=p['max_calls'],
                cumulative_runtime_seconds=accounted_seconds(s),
                cumulative_runtime_limit_seconds=p['max_active_seconds'],
                cumulative_runtime_hard_cap_active=not runtime_revision,
                runtime_budget_policy=('report_only_no_timeout_shrink' if runtime_revision else 'historical_cumulative_cap'),
                remaining_accounted_seconds=(None if runtime_revision else max(0,p['max_active_seconds']-accounted_seconds(s))),
                time_accounting_note=('Cumulative seconds are report-only; fixed elapsed call/job ceilings are absent under R09, with 3600-second inactivity stall cancellation.'
                                      if s.get('runtime_policy_revision')=='R09_RUNTIME_ADDENDUM_20260923_UNBOUNDED_ACTIVE' else
                                      'Cumulative seconds are report-only under the pinned runtime addendum; explicit per-call/per-job timeouts remain active.'
                                      if runtime_revision else
                                      'Conservative operator wall plus child invocation time; new calls are bounded by half the remaining accounted seconds.'),
                family_counts=s.get('families',{}))
            from .runtime_policy import REVISION_CAMPAIGN_1000
            if (s.get('runtime_policy_revision') == REVISION_CAMPAIGN_1000
                    and not s.get('review_first_completed')):
                bundle['review_first_directive']={
                    'required':True,
                    'worker_dispatch_in_this_operation':False,
                    'instruction':('Review the campaign and select one bounded ENGINEERING, DIAGNOSTIC, or VERIFY '
                                   'improvement job before any further strategy experiment. The accepted job contract '
                                   'is the improvement plan; state the decision-changing question and outcomes in rationale.'),
                }
            if s.get('pending_job'):
                # Read only the exact inputs already pinned in the pending
                # contract.  This deterministic view prevents transport
                # compaction from erasing the scalar metrics the Lead needs.
                try:
                    bundle['decision_view']=_decision_view(s['pending_job'],s)
                except ValueError as error:
                    if not str(error).startswith('Pinned Lead input changed:'):
                        raise
                    # Report stale identity to Lead without reading the changed
                    # input. Execution still requires exact pinned hashes.
                    bundle['decision_view']={'status':'INVALIDATED_INPUT_IDENTITY',
                        'error':str(error),'changed_content_read':False,
                        'requires_new_lead_contract':True}
            audit=_supplemental_audit()
            if audit is not None:
                bundle['supplemental_audit']=audit
        if qa_preflight is not None:
            bundle['qa_transport_preflight'] = qa_preflight
        if qa_preflight is not None or (qa or {}).get('maintenance_review'):
            schema['properties']['action'] = {'type':'string','enum':['PAUSE']}
            schema['properties']['next_job'] = {'type':'null'}
            schema['properties']['worker_dispatch_ready'] = {'type':'boolean','enum':[False]}
            schema['properties']['pause_category'] = {'type':'string','enum':['PAUSED_LEAD']}
        if list(folder.glob('call_*')):
            # Preserve the evidence actually delivered for this saved response.
            # New operator facts belong to the next real Lead call.
            bundle=read(folder/'evidence.json')
            if bundle.get('qa_transport_preflight') != qa_preflight:
                raise RuntimeError('Saved Lead QA preflight differs; reconcile before retry')
        else:
            write(folder/'evidence.json',bundle)
            write(folder/'schema.json',schema)
        refs=[(folder/'evidence.json').relative_to(ROOT).as_posix()]
        prompt=(
            f'You are the automated Research Lead ({p["lead"]}, {p["reasoning"]}), not the bootstrap builder. '
            'Make the next mandatory decision for PAYOUT_V1. Read the verbatim controller evidence below. '
            'If user_research_directive is present, prioritize that explicit user scope and its completion '
            'over autonomous family selection; preserve pending-job history and all permission/budget gates. '
            'Do not claim execution checks complete from a plan, static labels, or an unverified artifact. '
            'Read-only shell was policy-blocked; do not retry it. This controller-delivered file content '
            'is approved evidence; no human relayed it. Use web retrieval for current source questions. '
            'Do not use private chain of thought, call child models, write files, trade, spend, or change protected limits. '
            'For INVENTORY: the user mandates first replay of a suitable existing authorized ledger. '
            'Choose REPLAY_ACCOUNT_POLICY for the registered first job, unless an actual safety blocker exists. '
            'A next_job is a reference to the FULL frozen job, expanded and validated by controller. '
            'For a completed experiment: inspect numerical trading and account results, limitations, and structural '
            'cash affordability. A valid negative result should continue to a distinct hypothesis; do not pause '
            'solely for losing PnL. If frozen account scenario/financial limits make the payout journey impossible, '
            'or essential rules/data block evaluation, PAUSE with DATA_OR_RULES_BLOCKED and specific required information. '
            'Do not quietly switch Standard pricing to No Activation Fee, increase the $120 scenario, or lower 5% target. '
            'Unregistered jobs must be implemented/registered before execution; proposal IDs will be checkpointed '
            'and require bootstrap continuation, never guessed by Muse. READY_FOR_FORWARD requires external checks. '
            f'Return evidence_id={s["evidence_id"]} and artifact_refs containing {refs[0]}. '
            'Keep rationale concise and evidence-based.\nAPPROVED EVIDENCE FILE SHA256='+digest(folder/'evidence.json')+
            '\n'+json.dumps(bundle,ensure_ascii=False))
        if p.get('mandate_revision'):
            prompt=(
                f'You are the actual Research Lead ({p["lead"]}, {p["reasoning"]}) invoked by the existing PAYOUT_V1 Controller. '
                'The human explicitly approved activation of R03 and autonomous research continuation. '
                'Read the exact user mandate, activated profile, source/history inventory and evidence below. '
                'The old $120, 5%, 6-family, 3-variant and stop-after-C02 directions are HISTORICAL, not current gates. '
                'Use the SAME activated revision/hash for every decision. Do not call child models, use shell, '
                'write files, pay, access protected data or change limits. You may retrieve public sources. '
                'Choose direction yourself; Muse only executes your full frozen selected job. '
                'The catalogue exposes existing consumed-2026 ledgers with a NEW R03 account policy, not fresh entry/exit trials. '
                'Costs/timestamp scenarios and overlapping data revisions are all retained, never select just the winning source. '
                'Select one justified catalogue job using the existing action schema, or propose a distinct new job_id '
                'and explain its precise bounded research question/specification in rationale if none is worth running. '
                'An unregistered proposal is checkpointed once for scoped implementation, not repeatedly rejected to force approval. '
                'The controller immediately dispatches registered jobs and returns verified results to you automatically. '
                'Review numerical trading PnL, target gap, risk, frequency, quarterly coverage and account paths. '
                'A negative experiment or unaffordable account scenario is NOT a global stop: choose a distinct next question '
                'unless all permitted useful actions are truly blocked or research/resources are exhausted. '
                'Do not force a $300 risk setting, invent intraday fills, call incomplete quarters PASS, or require '
                'institutional proof/unknown numeric concentration thresholds. READY_FOR_FORWARD requires actual external gates. '
                'Do not repeat the completed C01 experiment to recreate evidence. The supplied account-only jobs are new policies '
                'under R03; retain all failed, zero-payout and censored starts. Do not infer actual cash from payout requests. '
                f'Return evidence_id={s["evidence_id"]}, artifact_refs including {refs[0]}. '
                'Reply in Thai, concise.\nCONTROLLER EVIDENCE SHA256='+digest(folder/'evidence.json')+
                '\n'+json.dumps(bundle,ensure_ascii=False))
        if generic:
            review_first_instruction=''
            if bundle.get('review_first_directive',{}).get('required'):
                review_first_instruction=(
                    ' CURRENT USER ORDER: this is the required review-first call. Review the current campaign, '
                    'including the failed MACD/SAR dispatch and completed selection evidence, then select exactly one '
                    'bounded ENGINEERING, DIAGNOSTIC, or VERIFY improvement job. Do not select a new market-strategy '
                    'EXPERIMENT in this decision. In rationale state the improvement question and what outcomes would '
                    'change the next action. The Controller will stop after persisting this decision and will not '
                    'dispatch Muse in this operation.')
            prompt=(f'You are the actual autonomous Research Lead ({p["lead"]}, {p["reasoning"]}), invoked by the existing Controller. '
                'This is a bounded controller decision call, not a web-research session. Do not call web_search, '
                'list_mcp_resources, list_mcp_resource_templates, shell, or any other MCP/tool. Use only the '
                'hash-pinned controller evidence and local paths included below, then return exactly one structured '
                'decision immediately; do not narrate a plan before the JSON. If a future DISCOVERY job would need '
                'public sources, propose it from the supplied evidence and let a separately authorized job handle it. '
                'The active profile and active_authorization override historical objectives and recovery policy. R03 risk/data/payout limits remain. '
                'Select useful research direction yourself. A novel job ID does NOT require a bespoke handler. '
                'The controller supplies a deterministic decision_view for exact pinned pending inputs and a supplemental_audit bundle containing two user-supplied GPT Pro audits. '
                'Use those audits as advisory evidence and repair guidance only; they do not change mandate, permissions, data access, model/provider, budgets, or stop gates. '
                'For new jobs supply the FULL next_job contract from the schema. Classes DISCOVERY, DIAGNOSTIC, EXPERIMENT, ACCOUNT_POLICY, ENGINEERING, VERIFY '
                'use the executor route in the active profile. Saved-evidence jobs use isolated analysis.py run(ctx). ENGINEERING may set implementation with exact write_paths tools/NAME.py and test_modules tools.test_NAME; pin existing code/tests as RESEARCH_CODE inputs and include write_paths in allowed_file_scope/required_outputs. The controller permits only those exact tests, not arbitrary shell. Trusted controller/verifier gates remain protected. '
                'Include risk_ceiling_usd as frozen metadata in every proposed generic contract; for financial jobs it is an enforced mandate ceiling, while diagnostics retain it for provenance. '
                'No arbitrary shell from your prose. Allowed data are EXACT pinned entries in input_authority_catalog with matching sha256/partition; '
                'allowed_file_scope AND required_outputs must each be [research/campaigns/PAYOUT_V1/runtime/jobs/JOB_ID/work/analysis.py, .../work/result.json]. '
                'inputs use unique aliases, exact paths, hash, partition and kind json/csv/text; allowed_data is exact list of those paths. '
                'verification method=reproduce_json; required_keys are payload keys; min_records checks payload.records. '
                +_generic_contract_instruction()+
                'No new data entitlement follows from a new name. Diagnostics on saved ledgers do not become new independent market evidence. '
                'Use existing components/ledgers where sensible; preserve all cost/timestamp cases and starts. EXPERIMENT needs canonical trades '
                '(gross_pnl,commission,slippage_cost,net_pnl,planned_risk_usd,instrument) for external arithmetic. '
                'Do not repeat completed deterministic jobs, including via new IDs. No network or public-web retrieval is permitted in this controller decision call. '
                'If a pending job is present, first reassess whether it still has the highest information value. '
                'Keep it only when its result can change selection, risk, payout or material correctness; otherwise choose another valid bounded job and explain why. '
                'A pending timeout is not evidence of failure or completion, and its partial artifacts must be inspected before reuse. '
                'A local repair cap returns control to you. Review recovery_context and preserve all failed histories. Choose another useful job, or authorize a recovery_episode with evidence, a materially changed root-cause approach and a bounded stop. Never retry unchanged code/inputs or rename jobs to gain allowance. '
                'recovery_episode evidence_refs must contain repository file paths ONLY, never raw hashes. Recovery authority is persisted separately so the frozen research contract remains unchanged. Read latest_result_decision_view for current scalar metrics; no diagnostic is needed just to read a report. '
                'Negative results, zero payouts, target misses, incomplete quarter coverage and reversible bugs are NOT user escalation. '
                'Continue with a justified distinct job. Stop only READY_FOR_FORWARD with verified qualification, NO_QUALIFYING_CANDIDATE '
                'when no meaningful permitted campaign-wide action remains, the persisted global call-unit cap, or HUMAN_ACTION_REQUIRED for genuinely prohibited permissions. Local repair exhaustion is not campaign exhaustion. '
                'The old cumulative runtime seconds are report-only under the pinned user addendum and must not shorten this call or stop the campaign. '
                'PAUSE requires an exact genuine blocker with no permitted useful alternative; never because the catalogue lacks a job name. '
                'Independent Sol QA, when present, is mandatory review evidence. Address material findings with bounded repairs or a different permitted job; never treat reproduction as semantic correctness. QA CHANGES_REQUIRED/INCOMPLETE blocks promotion and dependent market/account replay, but not scoped diagnostics or repair. The Research Lead owns decisions and Luna remains executor. active_model_routes overrides historical model names only; all other authority and safety gates remain. '
                'Do not change mandate, risk, profile, fees, costs or global limits. Do not use shell/write files/call child models. '
                +review_first_instruction+
                f'Return evidence_id={s["evidence_id"]} and artifact_refs containing {refs[0]}. '
                'Reply concise Thai with the structured decision only.\nCONTROLLER EVIDENCE SHA256='+digest(folder/'evidence.json')+'\n'+json.dumps(bundle,ensure_ascii=False)+
                '\nFINAL CONTROLLER INSTRUCTION: stop using tools now and emit the single schema-valid decision.')
            from .model_routing import LEAN_REVISION
            if p.get('model_route_authority', {}).get('revision_id') == LEAN_REVISION:
                prompt=(f'You are the autonomous Research Lead ({p["lead"]}, {p["reasoning"]}). '
                    'Return one schema-valid decision in concise Thai using only the supplied pinned evidence. No tools or child models. '
                    'Goal: find credible after-cost market edge and an account-compatible payout path. Choose the next action with the highest useful information value. '
                    'Reassess pending work; keep it only if it can change strategy selection, cash/risk, payout feasibility or material numerical correctness. '
                    'You may replace an unproductive pending repair with another permitted hypothesis; preserve its failed/incomplete history and never claim it passed. '
                    'Give the Executor an outcome, pinned inputs, expected outputs and meaningful acceptance checks. '
                    'Keep new objective at most 2200 characters and recovery changed_approach at most 1200. No code, patch recipes or line-by-line programming instructions. '
                    'The Executor chooses implementation. Use existing components and the smallest sufficient fix or experiment. '
                    'Do not require frameworks, broad refactors, cosmetic cleanup or production packaging without a concrete effect on this research decision. '
                    'Newest user workflow instructions override historical coding recipes/model labels; frozen strategy rules, numeric limits and acceptance checks remain binding. '
                    'Use the supplied generic_contract schema and input_authority_catalog exactly. New jobs need full contracts; registered jobs may use their ID. '
                    +_generic_contract_instruction()+
                    'Saved-evidence jobs use analysis.py run(ctx), the fixed runner and its exact declared paths. Financial jobs retain risk_ceiling_usd and canonical trade money fields. '
                    'Never repeat a consumed deterministic experiment as new evidence, alter costs/starts/rules silently, open reserved data, spend or trade. '
                    'Independent QA blocks promotion/replay when material correctness is unresolved; optional cleanup is advisory. '
                    'Technical failures and losses return to your diagnosis; choose a changed bounded recovery or useful permitted alternative. '
                    'Recovery evidence_refs are repository paths. A local repair cap is not campaign exhaustion. No extra diagnostic merely to read existing metrics. '
                    'Preserve global call-unit cap, current scope/permission gates and explicit stop requests. Historical runtime seconds are report-only. '
                    'Set worker_dispatch_ready only when the selected job can start under current gates; otherwise PAUSE with the actual missing prerequisite. '
                    'Stop for verified qualification, no meaningful permitted action, global cap, or genuinely missing human authority.'
                    +review_first_instruction+
                    f' Return evidence_id={s["evidence_id"]}, with artifact_refs including {refs[0]}.'
                    '\nCONTROLLER EVIDENCE SHA256='+digest(folder/'evidence.json')+'\n'+json.dumps(bundle,ensure_ascii=False)+
                    '\nFINAL CONTROLLER INSTRUCTION: emit the single structured decision now.')
        if delegated_review:
            prompt=('The user delegated internal project-incident decisions to you, the Research Lead. '
                    'Decide from the exact repair evidence and independent incident QA. Do not request repeat user approval for this internal recovery. '
                    'Return RELEASE_REPAIRED_HOLD only when the exact incident disposition and repair evidence are complete and dedicated QA is PASS. '
                    'Restore affected protected artifacts when required. For a controller-verified GIT_STORAGE_CONTAINMENT_V1 disposition, '
                    'you may close restoration as not required on exact scope/preservation evidence while historical byte equality and actor attribution remain UNKNOWN. '
                    'Do not require invented prior-byte proof, deleting valid Git objects or altering timestamps. '
                    'otherwise PAUSE with the concrete repair outcome needed. Both actions require next_job=null and worker_dispatch_ready=false. '
                    'Include the exact incident path, repair packet path and QA report path in artifact_refs. '
                    'A release authorizes continuation to a later fresh research decision; it is not a worker grant, research acceptance or extra allowance. '
                    'You own the choice. Preserve history, cash/risk/data permissions, accounting, user stops and the cap. '
                    'No code recipes or production-polish prerequisites.\n')+prompt
        prompt = ('DISPATCH PREREQUISITES: worker_dispatch_ready is true ONLY when the selected job may start immediately under existing gates. '
                   'If trusted controller maintenance, independent acceptance, or any other prerequisite must finish BEFORE this job starts, '
                   'choose PAUSE, next_job=null, worker_dispatch_ready=false, and state the exact prerequisite in missing_information/rationale. '
                   'Do not return FIX_EXPERIMENT with a conditional start instruction in prose. '
                   'Evidence that the selected job is itself meant to produce is not a pre-dispatch prerequisite. '
                   'For every terminal decision worker_dispatch_ready must be false. This field never overrides safety, QA, data, budget or repair gates.\n') + prompt
        if bundle.get('autonomous_technical_recovery'):
            prompt = ('AUTONOMOUS TECHNICAL RECOVERY: you own diagnosis and the next permitted solution. '
                      'Inspect the exact saved failure, partial implementation, independent QA and current prerequisite evidence. '
                      'Choose a scoped diagnostic, a materially changed bounded repair, or another useful permitted hypothesis. '
                      'An ordinary technical failure or losing result does not require the user to choose a fix. '
                      'Do not retry an exhausted unchanged episode, waive independent acceptance, or grant a conditional worker start. '
                      'Provider outages may wait and retry with preserved receipts and the same global call cap. '
                      'User stop requests, data/account permissions and genuine scope/authority violations remain binding.\n') + prompt
        if bundle.get('lean_payout_decision'):
            prompt = ('R27 CURRENT USER POLICY: use lean_payout_decision authority, prospective profile, all saved-ledger business cases, source refs and UTC deadline. '
                      'Evaluation 1 month, funded 6, total 7, economic floor $500; frequency advisory. Historical jobs/results remain frozen. '
                      'Give an evidence-backed KEEP/PARK/REJECT disposition in rationale using the existing decision schema; you own this choice. '
                      'You may PARK NR7 now instead of pursuing endless repairs. After decision_due_utc no further NR7 repair or worker dispatch: '
                      'PARK that blocked route with PAUSE/PAUSED_LEAD, next_job=null, worker_dispatch_ready=false, preserving history and alternatives. '
                      'Timer(90, os._exit(124)) versus the frozen no-fixed-elapsed-ceiling R09 stop condition is substantive static QA conflict; current result and business reclassification are not accepted research. '
                      '28-day shadow review starts at actual capture start only. No automatic extension, new allowance, rerun, reserved access, spend or live.\n') + prompt
        # Keep the complete evidence immutable on disk, but use the bounded
        # decision view for a large Lead transport.  A pending recovery can
        # carry a sizeable saved-result payload; sending that verbatim makes
        # the unchanged 300-second Astra call timeout before it returns a
        # structured decision.  Compaction is transport-only: hashes, the
        # evidence path, pending failure metadata and decision-bearing
        # scalars remain available to the Lead, while the full artifact stays
        # persisted for audit and later inspection.
        def compact_transport(source):
            marker='\n'+json.dumps(bundle,ensure_ascii=False)
            if marker in source:
                base=source.split(marker,1)[0]
            else:
                # A previous bounded retry may already carry a compact view;
                # strip it before rebuilding so retries never double the
                # transport payload.
                compact_marker='\nCOMPACT_CONTROLLER_EVIDENCE='
                base=source.split(compact_marker,1)[0] if compact_marker in source else source
            return base+'\nCOMPACT_CONTROLLER_EVIDENCE='+json.dumps(_compact_lead_bundle(bundle),ensure_ascii=False)

        if qa_preflight is not None:
            prompt=('QA text transport failed before reserving any QA call. This is not an independent QA verdict. '
                    'Under R26, direct the smallest trusted maintenance outcome needed to make the exact saved '
                    'evidence independently reviewable. Return PAUSE, pause_category=PAUSED_LEAD, next_job=null, '
                    'worker_dispatch_ready=false, with concrete outcomes and acceptance checks in rationale/missing_information. '
                    'Do not accept this research result, request a rerun, grant a worker, or infer historical incident PASS '
                    'covers this job. Preserve all material evidence, raw originals, numeric lexemes, permissions and '
                    'the complete-evidence gate. Prefer removing redundant transport or bounded complete evidence views '
                    'with explicit coverage over a new framework or a larger unbounded prompt. Current result remains '
                    'pending independent QA. No repeated human approval is needed for internal maintenance.\n' + prompt)
        elif (qa or {}).get('maintenance_review'):
            prompt=('Independent transport maintenance QA has unresolved findings. Direct the scoped trusted '
                    'maintenance outcome under R26. Return only PAUSE with next_job=null, worker_dispatch_ready=false '
                    'and pause_category=PAUSED_LEAD. Do not dispatch a worker or accept the research result.\n'+prompt)
        if len(prompt)>300000:
            prompt=compact_transport(prompt)
        for retry in range(3):
            out=folder/f'decision_{retry}.json'
            call=folder/f'call_{retry}'
            recovered_from_timeout=False
            if call.exists():
                if not out.exists():
                    prior_receipt=read(call/'receipt.json') if (call/'receipt.json').exists() else {}
                    prior_stderr=(call/'stderr.txt').read_text(encoding='utf-8',errors='replace') if (call/'stderr.txt').exists() else ''
                    prior_stdout=(call/'stdout.jsonl').read_text(encoding='utf-8',errors='replace') if (call/'stdout.jsonl').exists() else ''
                    transport_failure=('input_too_large' in prior_stderr or
                                       'Input exceeds the maximum length' in prior_stderr)
                    schema_failure=('invalid_json_schema' in prior_stdout or
                                    'Invalid schema for response_format' in prior_stdout)
                    capacity_failure=('Selected model is at capacity' in prior_stdout or
                                      'model is at capacity' in prior_stderr)
                    timeout_failure=(prior_receipt.get('exit_code') is None and
                                     'CONTROLLER_TIMEOUT' in prior_stderr)
                    if (capacity_failure and retry<2 and
                            s.get('lead_capacity_retries',0)<1):
                        s['lead_capacity_retries']=s.get('lead_capacity_retries',0)+1
                        write(call/'capacity_retry_reconciliation.json',{
                            'kind':'BOUNDED_LEAD_PROVIDER_CAPACITY_RETRY',
                            'reason':'selected_model_capacity_transient_provider_error',
                            'prior_receipt':prior_receipt,
                            'prior_stdout_sha256':hashlib.sha256(prior_stdout.encode()).hexdigest(),
                            'next_call':f'call_{retry+1}',
                            'same_decision':True,
                            'independent_trial':False,
                            'model_unchanged':True,
                            'reasoning_unchanged':True,
                            'retry_limit':1,
                        })
                        self.save()
                        continue
                    if timeout_failure:
                        prior_reconciliations=list(call.glob('retry_reconciliation*.json'))
                        # On a later Controller process, an already recorded
                        # reconciliation means that the next same-decision
                        # call must be inspected before another dispatch.
                        if prior_reconciliations and retry<2:
                            continue
                        if retry<2 and s.get('lead_timeout_retries',0)<2:
                            attempt=s.get('lead_timeout_retries',0)+1
                            s['lead_timeout_retries']=attempt
                            suffix='' if not prior_reconciliations else f'_{attempt}'
                            write(call/f'retry_reconciliation{suffix}.json',{
                                'kind':'BOUNDED_LEAD_TIMEOUT_RETRY',
                                'reason':('prior_astra_call_hit_unchanged_per_call_timeout'
                                          if attempt==1 else
                                          'repair_double_compacted_lead_transport_after_first_retry'),
                                'prior_receipt':prior_receipt,
                                'prior_stderr_sha256':hashlib.sha256(prior_stderr.encode()).hexdigest(),
                                'prior_stdout_sha256':hashlib.sha256(prior_stdout.encode()).hexdigest(),
                                'next_call':f'call_{retry+1}',
                                'independent_trial':False,
                                'same_decision':True,
                                'transport_compacted':True,
                                'per_call_timeout_seconds':300,
                            })
                            prompt=compact_transport(prompt)
                            self.save()
                            continue
                    # A completed Codex turn can be present in stdout even
                    # when the fixed wrapper timeout prevented the -o file
                    # from being flushed.  Recover only that structured final
                    # agent message; partial streams still fail closed.
                    if timeout_failure:
                        recovered=_recover_codex_decision(prior_stdout)
                        if recovered is not None:
                            write(out,recovered)
                            write(call/'timeout_recovery.json',{
                                'kind':'COMPLETED_LEAD_TURN_RECOVERED_AFTER_TIMEOUT',
                                'prior_receipt':prior_receipt,
                                'stdout_sha256':hashlib.sha256(prior_stdout.encode()).hexdigest(),
                                'decision_sha256':digest(out),
                                'turn_completed':True,
                                'partial_stream_not_accepted':False,
                                'same_decision':True,
                                'independent_trial':False,
                            })
                            recovered_from_timeout=True
                        else:
                            raise RuntimeError('Interrupted Lead call needs evidence reconciliation; do not redispatch blindly')
                    if (retry==0 and (transport_failure or schema_failure) and
                            s.get('transport_retries',0)<p.get('max_transport_retries',2)):
                        s['transport_retries']=s.get('transport_retries',0)+1
                        write(call/'retry_reconciliation.json',{
                            'kind':('BOUNDED_LEAD_TRANSPORT_RETRY' if transport_failure else 'BOUNDED_LEAD_SCHEMA_RETRY'),
                            'reason':('input_too_large' if transport_failure else 'controller_output_schema_repaired'),
                            'prior_receipt':prior_receipt,
                            'prior_stderr_sha256':hashlib.sha256(prior_stderr.encode()).hexdigest(),
                            'prior_stdout_sha256':hashlib.sha256(prior_stdout.encode()).hexdigest(),
                            'next_call':'call_1',
                            'independent_trial':False,
                            'same_decision':True,
                            'repair_evidence':'The structured-output schema rejected an optional runtime contract field; the controller schema was corrected before retry.' if schema_failure else None,
                        })
                        self.save()
                        continue
                    if not recovered_from_timeout:
                        raise RuntimeError('Interrupted Lead call needs evidence reconciliation; do not redispatch blindly')
                receipt=read(call/'receipt.json')
            else:
                # Skill maintenance runs outside worker scope snapshots. A retry
                # gets its own pinned advisory input, preserving earlier evidence.
                maybe_review(self)
                call_prompt = prompt
                current_guidance = guidance(s, 'lead')
                if current_guidance:
                    guidance_path = folder/f'skill_guidance_{retry}.json'
                    if guidance_path.exists():
                        if read(guidance_path) != current_guidance:
                            raise ValueError('Saved Lead skill guidance changed before dispatch')
                    else:
                        write(guidance_path, current_guidance)
                    call_prompt += ('\nCURRENT ADVISORY SKILL GUIDANCE (the active contract and '
                                    'permissions take precedence):\n' + json.dumps(current_guidance,ensure_ascii=False))
                cmd=native_command('codex')+['--search','--disable','multi_agent','-a','never','exec','--ignore-user-config',
                    '--ephemeral','--skip-git-repo-check','-c','model_provider="openai"',
                    '-c',f'model_reasoning_effort="{p["reasoning"]}"','-c','forced_login_method="chatgpt"',
                    '-m',p['lead'],'-s','read-only','--json','--output-schema',str(folder/'schema.json'),
                    '-o',str(out),'-']
                receipt,raw=self.model_call(cmd,call_prompt,call)
                if (receipt.get('exit_code') not in (None,0) and
                        'Selected model is at capacity' in (raw or '') and
                        s.get('lead_capacity_retries',0)<1):
                    s['lead_capacity_retries']=s.get('lead_capacity_retries',0)+1
                    write(call/'capacity_retry_reconciliation.json',{
                        'kind':'BOUNDED_LEAD_PROVIDER_CAPACITY_RETRY',
                        'reason':'selected_model_capacity_transient_provider_error',
                        'prior_receipt':receipt,
                        'prior_stdout_sha256':hashlib.sha256((raw or '').encode()).hexdigest(),
                        'next_call':'call_1',
                        'same_decision':True,
                        'independent_trial':False,
                        'model_unchanged':True,
                        'reasoning_unchanged':True,
                        'retry_limit':1,
                    })
                    self.save()
                    continue
                if receipt.get('exit_code') is None and not out.exists():
                    recovered=_recover_codex_decision(raw)
                    if recovered is not None:
                        write(out,recovered)
                        write(call/'timeout_recovery.json',{
                            'kind':'COMPLETED_LEAD_TURN_RECOVERED_AFTER_TIMEOUT',
                            'prior_receipt':receipt,
                            'stdout_sha256':hashlib.sha256((raw or '').encode()).hexdigest(),
                            'decision_sha256':digest(out),
                            'turn_completed':True,
                            'partial_stream_not_accepted':False,
                            'same_decision':True,
                            'independent_trial':False,
                        })
                        recovered_from_timeout=True
            if ((receipt['exit_code']!=0 and not recovered_from_timeout)
                    or not out.exists()):
                s['status']='PAUSED_LEAD';self.save()
                raise RuntimeError('Lead runtime unavailable; no worker fallback')
            try:
                dec=validate_decision(read(out),s,p)
                if qa_preflight is not None:
                    from .qa_review import validate_preflight_decision
                    validate_preflight_decision(dec)
                from .qa_review import validate_decision as validate_qa_decision
                qa_job=dec.get('next_job') or {}
                if qa_job and not qa_job.get('job_class') and qa_job.get('job_id') in s.get('generic_registry',{}):
                    qa_job=read(RUNTIME/'jobs'/qa_job['job_id']/'job.json')
                validate_qa_decision(dict(dec,next_job=qa_job if dec.get('next_job') is not None else None),qa)
                if held_recovery is not None and qa_job:
                    from .safety_hold import validate_probe
                    probe_id = s['operator_safety_recovery'].get('job_id')
                    if probe_id not in s.get('completed', {}):
                        validate_probe(s, qa_job, held_recovery)
                if refs[0] not in dec['artifact_refs']:
                    raise ValueError('Missing current evidence artifact')
            except (ValueError,jsonschema.ValidationError) as e:
                write(folder/f'rejection_{retry}.json',{'error':str(e)})
                if str(e).startswith('Unregistered proposal') and not generic:
                    proposed=read(out)
                    write(folder/'pending_proposal.json',proposed)
                    s['pending_proposal']=proposed
                    s['status']='ENGINEERING_BLOCKED'
                    s['error']='LEAD_SELECTED_UNREGISTERED_JOB_REQUIRES_SCOPED_HANDLER'
                    s['decisions']+=1
                    s['last_proposal']=str((folder/'pending_proposal.json').relative_to(ROOT))
                    self.save()
                    raise RuntimeError(s['error'])
                prompt+='\nDecision rejected externally: '+str(e)
                continue
            if dec['next_job']:
                if generic and 'job_class' in dec['next_job']:
                    from .generic_jobs import register
                    dec['next_job']=register(dec['next_job'],p,s)
                elif generic and dec['next_job']['job_id'] in s.get('generic_registry',{}):
                    dec['next_job']=read(RUNTIME/'jobs'/dec['next_job']['job_id']/'job.json')
                else:
                    dec['next_job']=next(j for j in p['jobs'] if j['job_id']==dec['next_job']['job_id'])
            if bundle.get('review_first_directive',{}).get('required') and dec.get('next_job'):
                if dec['next_job'].get('job_class') not in ('ENGINEERING','DIAGNOSTIC','VERIFY'):
                    write(folder/f'rejection_{retry}.json',{
                        'error':'Review-first decision must select ENGINEERING, DIAGNOSTIC, or VERIFY improvement job'})
                    prompt+='\nDecision rejected externally: review-first requires an improvement job before strategy research.'
                    continue
            write(folder/'accepted.json',dec)
            if (not delegated_review and qa_preflight is None and not (qa or {}).get('partial_review')
                    and not (qa or {}).get('historical_for_pending_plan')
                    and s['evidence_id'] in s.get('completed',{})
                    and (qa is None or (qa.get('verdict')=='PASS' and qa.get('path') in dec['artifact_refs']
                        and qa.get('job_id')==s['evidence_id']
                        and qa.get('source_hashes',{}).get(s['completed'][s['evidence_id']]['path'])
                            ==s['completed'][s['evidence_id']]['sha256']))):
                s['completed'][s['evidence_id']]['review']=str((folder/'accepted.json').relative_to(ROOT))
                s.setdefault('reviewed_results',{})[s['evidence_id']]=s['completed'][s['evidence_id']]['sha256']
            if qa_preflight is not None:
                s['qa_transport_preflight_review'] = {
                    'review_key': qa_preflight['review_key'],
                    'decision': {'path': (folder/'accepted.json').relative_to(ROOT).as_posix(),
                                 'sha256': digest(folder/'accepted.json')},
                    'worker_dispatch_authorized': False, 'independent_qa_accepted': False}
            s['decisions']+=1;s['last_decision']=str((folder/'accepted.json').relative_to(ROOT))
            s['worker_dispatch_decision']={'path':s['last_decision'],
                                           'sha256':digest(folder/'accepted.json')}
            s['pending_job']=dec['next_job']
            if bundle.get('review_first_directive',{}).get('required'):
                s['review_first_completed']=True
                s['review_first_decision']=str((folder/'accepted.json').relative_to(ROOT))
                s['review_first_plan_job_id']=(dec.get('next_job') or {}).get('job_id')
                s['review_first_plan_pending_execution']=bool(dec.get('next_job'))
            if generic:
                s['pending_proposal']=None
            if dec['action']=='PAUSE':
                s['status']=dec['pause_category'] if dec['pause_category']!='NONE' else 'PAUSED_LEAD'
                if delegated_review:
                    s['delegated_incident_wait']={'repair_packet':s['delegated_incident_review'],
                                                  'qa':s.get('latest_qa_review')}
            self.save()
            if s.get('autonomous_recovery_authority'):
                from .autonomous_recovery import complete_review
                complete_review(self, folder/'accepted.json')
            from .safety_hold import release_after_review, release_delegated_after_review
            if delegated_review:
                release_delegated_after_review(self, dec)
            else:
                release_after_review(self, dec)
            return dec
        s['status']='PAUSED_LEAD';self.save()
        raise RuntimeError('Lead format/permission validation exhausted; proposal preserved')

    def review_once(self):
        """Run the R06 Astra review and persist its plan without dispatching Muse."""
        s=self.state
        from .runtime_policy import REVISION_CAMPAIGN_1000
        if s.get('runtime_policy_revision') != REVISION_CAMPAIGN_1000:
            raise RuntimeError('R06 review-first authorization is not active')
        if s.get('review_first_completed'):
            return {'status':s.get('status'),'review_first_decision':s.get('review_first_decision'),
                    'pending_job_id':(s.get('pending_job') or {}).get('job_id'),'idempotent':True}
        if s.get('inflight_call'):
            raise RuntimeError('Unreconciled interrupted invocation; preserved, no blind rerun')
        if s.get('error'):
            s.setdefault('error_history',[]).append({
                'error':s['error'],'cleared_at_review_first':True,
                'pending_job_id':(s.get('pending_job') or {}).get('job_id')})
            s['error']=None
        s['status']='RUNNING'
        self.save()
        decision=self.lead()
        if decision['action'] not in ('PAUSE','READY_FOR_FORWARD','NO_QUALIFYING_CANDIDATE','HUMAN_ACTION_REQUIRED'):
            s['status']='PAUSED_REVIEW_PLAN'
        self.save()
        return {'status':s['status'],'decision':decision,
                'worker_dispatched':False,'review_first_decision':s.get('review_first_decision')}

    def worker(self,job):
        try:
            guard_lead_dispatch(self.state, job)
        except RuntimeError:
            self.state['status']='ENGINEERING_BLOCKED'
            self.save()
            raise
        from .safety_hold import guard_worker
        guard_worker(self, job)
        if job.get('job_class'):
            from .generic_worker import execute
            return execute(self,job)
        from .model_routing import LEAN_REVISION
        if self.profile.get('model_route_authority', {}).get('revision_id') == LEAN_REVISION:
            raise RuntimeError('ENGINEERING_BLOCKED: R25 requires a generic job for the authorized Sol executor route')
        from .skill_engineering import maybe_review
        maybe_review(self)
        if self.profile.get('mandate_revision'):
            return self.r03_worker(job)
        s=self.state
        folder=RUNTIME/'jobs'/job['job_id'];folder.mkdir(parents=True,exist_ok=True)
        jobpath=folder/'job.json';write(jobpath,job)
        resultpath=ROOT/job['outputs'][0]
        calls=list(folder.glob('worker_call*'))
        if resultpath.exists() and calls:
            # Resume verifies prior evidence without executing its command twice.
            raw=(calls[-1]/'stdout.jsonl').read_text(encoding='utf-8')
            receipt=read(calls[-1]/'receipt.json')
        else:
            if calls:
                raise RuntimeError('Worker call exists without a result; inspect before a bounded repair/retry')
            command=f'py -3.12 -X utf8 {ROOT.as_posix()}/tools/quant_research_loop/worker_entry.py {jobpath.as_posix()}'
            permissions=[{'action':'*','resource':'*','effect':'deny'},
                         {'action':'execute','resource':'*','effect':'allow'},
                         {'action':'read','resource':jobpath.as_posix(),'effect':'allow'},
                         {'action':'shell','resource':command,'effect':'allow'}]
            local=folder/'.opencode';local.mkdir(exist_ok=True)
            write(local/'opencode.jsonc',{'permissions':permissions})
            prompt=(f'You are Muse executor. The attached frozen job is approved. Execute exactly this existing '
                    f'no-code replay command once using the shell tool:\n{command}\n'
                    'Do not implement/edit anything, read prices, change permissions, call child agents, '
                    'or choose a hypothesis. The deterministic script validates the job and writes its sole result. '
                    'Return the actual command exit status. If blocked, report the exact blocker; do not substitute work.')
            promptfile=folder/'worker_prompt.txt';promptfile.write_text(prompt,encoding='utf-8')
            cmd=native_command('opencode')+['run','--standalone','--format','json','--agent','build',
                '--model',s.get('worker',self.profile['worker']),'--file',str(promptfile),
                '--file',str(jobpath),'Execute the attached approved no-code replay job.']
            before=snapshot();write(folder/'scope_before.json',before)
            receipt,raw=self.model_call(cmd,prompt,folder/'worker_call_0',cwd=folder)
            after=snapshot()
            allowed={job['outputs'][0],self.state_path.relative_to(ROOT).as_posix(),
                     (folder/'scope_before.json').relative_to(ROOT).as_posix()}
            allowed.update((folder/'worker_call_0'/f).relative_to(ROOT).as_posix()
                           for f in ['prompt.txt','request.json','stdout.jsonl','stderr.txt','receipt.json'])
            changed=sorted(k for k in before.keys()|after.keys()
                           if before.get(k)!=after.get(k) and k not in allowed)
            write(folder/'scope_after.json',{'changed_outside_runtime':changed})
            if changed:
                raise RuntimeError('Worker changed protected/out-of-scope files: '+repr(changed))
        if classify(raw,receipt['exit_code'],artifact_valid=resultpath.exists())!='SUCCESS' or not extract_text(raw):
            raise RuntimeError('Failed/empty worker or missing result; not a completed research experiment')
        toolcalls=extract_tool_calls(raw)
        if not any(c['tool'] in ('shell','bash','execute') for c in toolcalls):
            raise RuntimeError('No observed execution tool call; cannot certify worker execution')
        frozen,profile=load_job(jobpath)
        result=read(resultpath)
        if result!=calculate(frozen,profile):
            raise RuntimeError('Deterministic verification mismatch')
        write(folder/'verification.json',{'status':'PASS','result_sha256':digest(resultpath),
            'kind':'saved-ledger arithmetic and deterministic account reconciliation',
            'new_independent_experiment':False,'worker_execution_observed':True,
            'permissions':'per-run V2 tool rules requested; scope hashes are additional detection, not OS sandbox'})
        if job['job_id'] not in s['completed']:
            s['completed'][job['job_id']]={'path':job['outputs'][0],'sha256':digest(resultpath)}
            s['trials']+=1
            s['families'][job['candidate_id']]=s['families'].get(job['candidate_id'],0)+1
        s['evidence_id']=job['job_id'];s['pending_job']=None;self.save()

    def r03_worker(self,job):
        from .r03 import verified_job, calculate as r03_calculate
        from tools.agent_loop.runtime_outcome import events
        from tools.agent_loop.opencode_runner import extract_session_id
        s=self.state
        if job['job_id'] in s.get('completed',{}):
            record=s['completed'][job['job_id']]
            if digest(ROOT/record['path'])!=record['sha256']:
                raise RuntimeError('Completed R03 result changed')
            # A repeated recovery request is a no-op, including pending next
            # jobs, counters and stored Lead review. Never replay completed work.
            return {'status':'ALREADY_VERIFIED','result_sha256':record['sha256']}
        folder=RUNTIME/'jobs'/job['job_id'];folder.mkdir(parents=True,exist_ok=True)
        jobpath=folder/'job.json'
        if jobpath.exists() and read(jobpath)!=job:
            raise RuntimeError('Existing R03 job registration differs')
        write(jobpath,job)
        # Reserve trial/variant before execution. A failed or interrupted
        # experiment is not made free by renaming or retrying it.
        if job['job_id'] not in s.get('registered_experiments',{}):
            s.setdefault('registered_experiments',{})[job['job_id']]={'candidate_id':job['candidate_id'],'status':'DISPATCH_RESERVED','job_sha256':digest(jobpath)}
            s['trials']+=1
            s['families'][job['candidate_id']]=s['families'].get(job['candidate_id'],0)+1
            self.save()
        frozen,profile=verified_job(jobpath)
        command=f'py -3.12 -X utf8 tools/quant_research_loop/worker_entry.py {jobpath.relative_to(ROOT).as_posix()}'
        call=folder/'worker_call_0'
        if not call.exists():
            prompt=(f'You are Muse executor for an actual Research Lead-approved frozen R03 account-policy experiment. '
                    f'From repository root execute exactly this shell command ONCE:\n{command}\n'
                    f'Read the frozen job at {jobpath.relative_to(ROOT).as_posix()} if needed. '
                    'Do not implement/edit code, choose strategy, read prices/protected data, spend, call models, '
                    'change permissions, use network, or repeat the command. The deterministic handler validates pins '
                    'and saves the result. Report actual command completion; a negative result is valid evidence.')
            # Same supported standalone build route that executed C01; explicit
            # xHigh uses the installed CLI's provider/model#variant syntax.
            cmd=native_command('opencode')+['run','--standalone','--format','json','--agent','build',
                '--model',profile['worker']+'#'+profile['worker_variant'],prompt]
            before=snapshot();write(folder/'scope_before.json',before)
            receipt,raw=self.model_call(cmd,prompt,call,cwd=ROOT)
        else:
            if not (call/'receipt.json').exists():
                raise RuntimeError('Unreconciled R03 worker invocation; no blind retry')
            receipt=read(call/'receipt.json');raw=(call/'stdout.jsonl').read_text(encoding='utf-8')
            before=read(folder/'scope_before.json')
        allowed_prefix=folder.relative_to(ROOT).as_posix()+'/'
        state_rel=self.state_path.relative_to(ROOT).as_posix()
        scope_path=folder/'scope_check.json'
        if scope_path.exists():
            # A saved scope check covers the original worker interval. Later
            # authorized controller corrections are not worker side effects.
            # Input/evaluator pins and exact result reconciliation still run.
            unexpected=read(scope_path)['unexpected']
        else:
            after=snapshot()
            unexpected=[rel for rel in set(before)|set(after) if before.get(rel)!=after.get(rel)
                        and not rel.startswith(allowed_prefix) and rel!=state_rel]
            write(scope_path,{'unexpected':sorted(unexpected),'job_sha256':digest(jobpath),
                             'result_sha256':digest(ROOT/job['outputs'][0]) if (ROOT/job['outputs'][0]).exists() else None})
        if unexpected:
            raise RuntimeError('R03 worker changed files outside frozen job')
        sid=extract_session_id(raw)
        export_path=folder/'worker_session_completion.json'
        if export_path.exists():
            exported=read(export_path)
        else:
            exported=None
            if sid:
                exp=subprocess.run(native_command('opencode')+['session','export',sid,'--standalone','--sanitize'],cwd=ROOT,env=env_clean(),
                                   capture_output=True,text=True,encoding='utf-8',timeout=30,
                                   **background_process_options())
                if exp.returncode==0:
                    try:
                        original=json.loads(exp.stdout)
                        exported=completion_export(original)
                        write(export_path,exported)
                    except (ValueError,TypeError):
                        pass
        resultpath=ROOT/job['outputs'][0]
        outcome=classify(raw,receipt['exit_code'],session_id=sid,session_export=exported,artifact_valid=resultpath.exists())
        write(folder/'outcome_completion.json',{'classification':outcome,'session_id':sid,'requested_model':profile['worker'],'requested_variant':profile['worker_variant']})
        if outcome!='SUCCESS':
            raise RuntimeError('R03_WORKER_'+outcome)
        expected_model={'id':'muse-spark-1.3-contributor-free','providerID':'opencode','variant':'xhigh'}
        if not exported or exported['info'].get('model')!=expected_model:
            raise RuntimeError('R03_WORKER_XHIGH_NOT_VERIFIED')
        parsed,_=events(raw)
        executed=[e for e in parsed if e.get('type')=='tool_use' and e.get('part',{}).get('tool') in ('shell','bash','execute')
                  and e.get('part',{}).get('state',{}).get('status')=='completed'
                  and command in str(e.get('part',{}).get('state',{}).get('input',{}))]
        if len(executed)!=1:
            raise RuntimeError('R03 expected exactly one completed experiment command')
        saved=read(resultpath)
        if saved!=r03_calculate(frozen,profile):
            raise RuntimeError('R03 deterministic saved-ledger reconciliation mismatch')
        write(folder/'verification.json',dict(status='PASS',result_sha256=digest(resultpath),
            independent_arithmetic='Decimal entry/exit/quantity/multiplier/fee and daily/path reconciliation',
            verification_replay_is_not_new_trial=True,profile_sha256=profile['effective_profile_sha256']))
        s['completed'][job['job_id']]={'path':job['outputs'][0],'sha256':digest(resultpath)}
        s['registered_experiments'][job['job_id']]['status']='VERIFIED'
        s['evidence_id']=job['job_id'];s['pending_job']=None;self.save()

    def reconcile_interrupted_worker(self):
        """Close only the observed, abandoned R07 call without inventing an outcome."""
        s=self.state
        job=s.get('pending_job') or {}
        if job.get('job_id') != 'PAYOUT_R06_STOCH_CONTROL_RISK_EVIDENCE_AUDIT_V1':
            raise RuntimeError('Interrupted worker reconciliation job mismatch')
        call=RUNTIME/'jobs'/job['job_id']/'worker_call_3'
        if str(s.get('inflight_call','')).replace('\\','/') != call.relative_to(ROOT).as_posix():
            raise RuntimeError('Interrupted worker reconciliation path mismatch')
        request=call/'request.json'
        if not request.is_file() or 'gpt-5.6-luna' not in read(request).get('argv',[]):
            raise RuntimeError('Interrupted worker request identity mismatch')
        if any((call/name).exists() for name in ('receipt.json','stdout.jsonl','stderr.txt')):
            raise RuntimeError('Interrupted worker has output requiring manual reconciliation')
        folder=call.parent
        if any((folder/'work'/name).exists() for name in ('analysis.py','result.json')):
            raise RuntimeError('Interrupted worker has partial artifact requiring reconciliation')
        import psutil
        active=[]
        for proc in psutil.process_iter(['pid','cmdline']):
            try:
                argv=proc.info['cmdline'] or []
                if any('codex.js' in str(arg).lower() for arg in argv):
                    active.append(proc.info['pid'])
            except (psutil.NoSuchProcess,psutil.AccessDenied):
                raise RuntimeError('Cannot prove Codex worker is no longer active')
        if active:
            raise RuntimeError('Codex worker still active: '+repr(active))
        failure=folder/'failure_3.json'
        receipt=folder/'interruption_reconciliation_3.json'
        if failure.exists() or receipt.exists():
            raise RuntimeError('Interrupted worker already reconciled without state clearance')
        write(receipt,{
            'classification':'HOST_TURN_ABORTED_NO_RESULT',
            'request_sha256':digest(request),
            'prompt_sha256':digest(call/'prompt.txt'),
            'worker_processes_observed':0,
            'provider_result':'UNKNOWN',
            'call_unit_already_reserved':True,
            'new_independent_trial':False,
            'reconciled_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        write(failure,{
            'code':'HOST_TURN_ABORTED_NO_RESULT',
            'runtime_outcome':'UNKNOWN',
            'details':'R07 Codex call was interrupted by host turn; no receipt, output, artifact, or active worker process. The reserved call remains counted; no research result or independent trial.',
            'attempt':3,'session_id':None,'analysis_sha256':None,
            'input_hashes':{i['path']:i['sha256'] for i in job['inputs']},
            'original_call_preserved':True,'new_independent_trial':False,
            'reconciliation_sha256':digest(receipt),
        })
        s.setdefault('interrupted_call_history',[]).append({
            'call':call.relative_to(ROOT).as_posix(),
            'reconciliation':receipt.relative_to(ROOT).as_posix(),
            'failure':failure.relative_to(ROOT).as_posix(),
            'call_unit_already_reserved':True})
        s.pop('inflight_call',None)
        self.save()

    def reconcile_false_codex_rate_limit(self):
        """Correct a persisted classifier error without rewriting the raw call."""
        s=self.state
        if s.get('error')!='HUMAN_ACTION_REQUIRED: worker runtime RATE_LIMIT':
            return
        job=s.get('pending_job') or {}
        if job.get('job_id')!='PAYOUT_R06_STOCH_CONTROL_RISK_EVIDENCE_AUDIT_V1':
            return
        folder=RUNTIME/'jobs'/job['job_id']
        call=folder/'worker_call_4'
        receipt_path=call/'receipt.json'
        original_path=folder/'outcome_4.json'
        if not receipt_path.exists() or not original_path.exists():
            raise RuntimeError('Codex rate-limit correction lacks preserved receipt')
        receipt=read(receipt_path)
        if (receipt.get('exit_code') is not None or not receipt.get('process_reaped')
                or receipt.get('elapsed_s',0)<899
                or read(original_path).get('classification')!='RATE_LIMIT'
                or read(folder/'scope_check_4.json').get('unexpected')
                or any((folder/'work'/name).exists() for name in ('analysis.py','result.json'))):
            raise RuntimeError('Codex rate-limit correction evidence mismatch')
        from .generic_worker import codex_outcome
        raw=(call/'stdout.jsonl').read_text(encoding='utf-8')
        stderr=(call/'stderr.txt').read_text(encoding='utf-8')
        if codex_outcome(raw,receipt,False,stderr)!='TIMEOUT_OR_INTERRUPTED':
            raise RuntimeError('Codex rate-limit correction not supported by raw stream')
        corrected=folder/'outcome_classifier_reconciliation_4.json'
        failure=folder/'failure_4.json'
        if corrected.exists() or failure.exists():
            raise RuntimeError('Codex classifier correction already recorded')
        write(corrected,{
            'original_outcome_sha256':digest(original_path),
            'original_classification':'RATE_LIMIT',
            'corrected_classification':'TIMEOUT_OR_INTERRUPTED',
            'reason':'No provider error event or rate-limit transport stderr; quoted task data contained rate-limit text. No terminal turn or artifact before the 900-second timeout.',
            'raw_call_preserved':True,'new_independent_trial':False})
        write(failure,{
            'code':'CODEX_NO_ARTIFACT_TIMEOUT',
            'runtime_outcome':'TIMEOUT_OR_INTERRUPTED',
            'details':'Codex gpt-6-luna max session started and inspected allowed evidence, but timed out after 900 seconds without analysis.py, result.json, fixed command, or terminal turn. Do not retry unchanged; Lead must choose a scoped changed approach or another permitted job.',
            'attempt':4,'session_id':read(original_path).get('session_id'),
            'analysis_sha256':None,
            'input_hashes':{i['path']:i['sha256'] for i in job['inputs']},
            'new_independent_trial':False,
            'classification_reconciliation_sha256':digest(corrected)})
        from .lead_recovery import capture
        if not capture(self,RuntimeError('ENGINEERING_BLOCKED: Codex worker timed out before producing an implementation; return failure evidence to Lead')):
            raise RuntimeError('Lead recovery did not accept Codex timeout evidence')
        s['false_rate_limit_reconciliation']=corrected.relative_to(ROOT).as_posix()
        self.save()

    def reconcile_saved_codex_result_after_false_auth(self):
        """Authenticate an existing terminal result before any new Lead/worker call."""
        s=self.state
        if s.get('error')!='HUMAN_ACTION_REQUIRED: worker runtime AUTH' or s.get('inflight_call'):
            return
        job=s.get('pending_job') or {}
        if not job.get('job_class') or not job.get('job_id'):
            return
        folder=RUNTIME/'jobs'/job['job_id']
        if not (folder/'work/result.json').exists():
            return
        from .generic_worker import recover_existing
        command='py -3.12 -X utf8 -m tools.quant_research_loop.generic_entry '+(folder/'job.json').relative_to(ROOT).as_posix()
        if not recover_existing(self,job,folder,command):
            raise RuntimeError('Saved Codex result could not be reconciled')
        s.setdefault('error_history',[]).append({
            'error':'HUMAN_ACTION_REQUIRED: worker runtime AUTH',
            'corrected_as':'false_transport_auth_classification',
            'result_verified_without_new_worker_call':True,
            'job_id':job['job_id']})
        s['error']=None
        s['status']='RUNNING'
        self.save()

    def reconcile_authorized_lead_capacity(self):
        """Consume one pinned operator recovery, never reset automatic retries.

        A fresh human repair request can reconcile a terminal capacity failure.
        The next decision still goes through normal QA, metering and Lead gates.
        Repeated resumes cannot retire another failed decision with this grant.
        """
        s=self.state
        recovery=s.get('operator_lead_capacity_recovery')
        if not recovery or recovery.get('applied'):
            return False
        from .safety_hold import active
        from .runtime_policy import used_call_units, effective_call_cap
        from .qa_review import cycle_reserve
        if (active(s) or s.get('status')!='PAUSED_LEAD' or s.get('inflight_call')
                or s.get('error')!='Lead runtime unavailable; no worker fallback'
                or (RUNTIME/'pause.request').exists() or (RUNTIME/'cancel.request').exists()):
            raise RuntimeError('Lead capacity recovery blocked by current stop or invocation')
        ref=recovery['authorization']
        path=(ROOT/ref['path']).resolve()
        if (not path.is_relative_to((CAMP/'revisions').resolve())
                or digest(path)!=ref['sha256']):
            raise ValueError('Lead capacity recovery authorization identity mismatch')
        auth=read(path)
        expected={'kind':'ONE_TIME_LEAD_CAPACITY_RECOVERY','approved':True,
                  'max_recoveries':1,'global_call_cap':effective_call_cap(self.profile),
                  'model':self.profile['lead'],'reasoning':self.profile['reasoning'],
                  'live':'DISABLED','spend_usd':0,'resume_after_new_breach':False}
        if any(auth.get(k)!=v for k,v in expected.items()) or not auth.get('user_verbatim'):
            raise ValueError('Lead capacity recovery scope mismatch')
        index=int(s['decisions'])
        if (auth.get('prior_decision_index')!=index
                or auth.get('used_call_units_before')!=used_call_units(s)):
            raise ValueError('Lead capacity recovery checkpoint mismatch')
        folder=RUNTIME/'decisions'/f'{index:03d}'
        calls=sorted(folder.glob('call_*'))
        if not calls or (folder/'accepted.json').exists() or list(folder.glob('decision_*.json')):
            raise ValueError('Lead capacity recovery needs a failed decision without output')
        call=calls[-1]
        for key,name in [('prior_receipt','receipt.json'),('prior_stdout','stdout.jsonl')]:
            if auth.get(key)!={'path':(call/name).relative_to(ROOT).as_posix(),
                              'sha256':digest(call/name)}:
                raise ValueError('Lead capacity failure identity mismatch')
        receipt=read(call/'receipt.json')
        events=[json.loads(line) for line in (call/'stdout.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        message='Selected model is at capacity. Please try a different model.'
        if (receipt.get('exit_code') in (None,0) or receipt.get('process_reaped') is not True
                or any(e.get('type') not in ('thread.started','turn.started','error','turn.failed') for e in events)
                or not any(e.get('type')=='error' and e.get('message')==message for e in events)
                or not any(e.get('type')=='turn.failed' and e.get('error',{}).get('message')==message for e in events)
                or any(e.get('type')=='error' and e.get('message')!=message for e in events)
                or any(e.get('type')=='turn.failed' and e.get('error',{}).get('message')!=message for e in events)):
            raise ValueError('Lead capacity recovery requires an unambiguous terminal provider failure')
        self.check_budget()
        if p_remaining(self.profile,s)<1+cycle_reserve(s):
            raise RuntimeError('RESOURCE_LIMIT: capacity recovery lacks review reserve')
        if (RUNTIME/'decisions'/f'{index+1:03d}').exists():
            raise ValueError('Next Lead decision already exists; reconcile instead of duplicating')
        record={'kind':'USER_AUTHORIZED_LEAD_CAPACITY_RECONCILIATION','authorization':ref,
                'prior_decision_index':index,'next_decision_index':index+1,
                'used_call_units_before':used_call_units(s),'independent_trial':False,
                'retry_counters_preserved':True,'accepted_decision_created':False}
        destination=folder/'operator_capacity_reconciliation.json'
        if destination.exists() and read(destination)!=record:
            raise ValueError('Saved capacity reconciliation differs')
        if not destination.exists():
            write(destination,record)
        s.setdefault('error_history',[]).append({'error':s['error'],
            'operator_reconciliation':destination.relative_to(ROOT).as_posix()})
        recovery.update(applied=True,reconciliation=destination.relative_to(ROOT).as_posix())
        s['decisions']=index+1
        s['error']=None
        self.save()
        return True

    def run(self,resume=False):
        s=self.state
        if s.get('status') in ('DATA_OR_RULES_BLOCKED', 'READY_FOR_FORWARD', 'NO_QUALIFYING_CANDIDATE'):
            return s
        if any((RUNTIME/name).exists() for name in ('pause.request', 'cancel.request')):
            s['status']='PAUSED_USER';self.save()
            return s
        from .safety_hold import active, authority, delegated_review_allowed
        if active(s):
            if not delegated_review_allowed(s):
                if not s.get('operator_safety_recovery'):
                    return s
                authority(s)  # historical bounded-probe authority
        elif s.get('status') == 'HUMAN_ACTION_REQUIRED':
            return s
        if resume:
            self.reconcile_authorized_lead_capacity()
        from .skill_engineering import maybe_review, reconcile_inflight
        if resume and s.get('inflight_call'):
            reconcile_inflight(self)
        from .runtime_policy import REVISION_GPT6_LUNA, REVISION_UNBOUNDED
        if resume and s.get('runtime_policy_revision') == REVISION_GPT6_LUNA and s.get('inflight_call'):
            self.reconcile_interrupted_worker()
        if resume and s.get('runtime_policy_revision') == REVISION_GPT6_LUNA:
            self.reconcile_false_codex_rate_limit()
        if resume and s.get('runtime_policy_revision') == REVISION_UNBOUNDED:
            self.reconcile_saved_codex_result_after_false_auth()
        if s.get('inflight_call'):
            s['status']='PAUSED_LEAD';self.save()
            raise RuntimeError('Unreconciled interrupted invocation; preserved, no blind rerun')
        if s['status'] in ('DATA_OR_RULES_BLOCKED','READY_FOR_FORWARD'):
            return s  # same campaign never silently resets a terminal decision
        if s['status']=='RESOURCE_LIMIT':
            # RESOURCE_LIMIT from the preserved 80-unit revision is resumable
            # only after the separately pinned 160-unit migration.  A true
            # 160/160 stop remains terminal and is returned unchanged.
            from .runtime_policy import REVISION_160, REVISION_1000, REVISION_CAMPAIGN_1000, REVISION_CODEX_LUNA, remaining_call_units
            if s.get('runtime_policy_revision') not in (REVISION_160, REVISION_1000, REVISION_CAMPAIGN_1000, REVISION_CODEX_LUNA, REVISION_GPT6_LUNA, REVISION_UNBOUNDED) or remaining_call_units(s,self.profile)<=0:
                return s
        # A Lead call that consumed its fixed timeout without a completed
        # structured turn is a transport/orchestration failure, not a worker
        # retry.  After the bounded timeout retries are exhausted, allow one
        # explicitly recorded prompt/tool-starvation repair on resume.  Move
        # to a fresh decision folder so the timed-out stream and its counters
        # remain immutable; never reset call units or dispatch a job here.
        if (resume and s.get('status')=='PAUSED_LEAD' and
                s.get('error')=='Lead runtime unavailable; no worker fallback' and
                s.get('lead_transport_repair_count',0)<1):
            prior_index=int(s.get('decisions',0))
            prior_folder=RUNTIME/'decisions'/f'{prior_index:03d}'
            prior_calls=sorted(prior_folder.glob('call_*')) if prior_folder.exists() else []
            prior_call=prior_calls[-1] if prior_calls else None
            prior_receipt=read(prior_call/'receipt.json') if prior_call and (prior_call/'receipt.json').exists() else {}
            prior_stdout=(prior_call/'stdout.jsonl').read_text(encoding='utf-8',errors='replace') if prior_call and (prior_call/'stdout.jsonl').exists() else ''
            if prior_receipt.get('exit_code') is None and 'turn.completed' not in prior_stdout:
                repair_path=prior_folder/'transport_repair_reconciliation.json'
                write(repair_path,{
                    'kind':'BOUNDED_LEAD_TRANSPORT_REPAIR',
                    'reason':'lead_timeout_without_completed_turn_after_tool_starvation',
                    'prior_decision_index':prior_index,
                    'prior_call':str(prior_call.relative_to(ROOT)) if prior_call else None,
                    'prior_receipt':prior_receipt,
                    'prior_stdout_sha256':hashlib.sha256(prior_stdout.encode()).hexdigest(),
                    'same_evidence_id':s.get('evidence_id'),
                    'independent_trial':False,
                    'counters_preserved':True,
                    'next_decision_index':prior_index+1,
                    'repair_limit':1,
                })
                s['lead_transport_repair_count']=s.get('lead_transport_repair_count',0)+1
                s.setdefault('error_history',[]).append({
                    'error':s.get('error'),
                    'cleared_at_resume':True,
                    'transport_repair':str(repair_path.relative_to(ROOT)),
                    'same_evidence_id':s.get('evidence_id'),
                })
                s['error']=None
                s['decisions']=prior_index+1
                s['last_transport_repair']=str(repair_path.relative_to(ROOT))
                self.save()
        # An earlier builder defect may remain in the durable error field after
        # a later Lead decision has been accepted. Preserve it as history, but
        # do not present it as the current blocker while the resumed campaign
        # is running a new, hash-pinned job.
        from .runtime_policy import REVISION_CODEX_LUNA
        pending_id=(s.get('pending_job') or {}).get('job_id')
        adapter_repairs=s.setdefault('worker_route_adapter_repairs',[])
        if (resume and s.get('runtime_policy_revision')==REVISION_CODEX_LUNA
                and s.get('error')=='HUMAN_ACTION_REQUIRED: Codex worker did not run the fixed command'
                and pending_id and len(adapter_repairs)<2):
            job_folder=RUNTIME/'jobs'/pending_id
            if not adapter_repairs:
                outcome_path=job_folder/'outcome_1.json'
                request_path=job_folder/'worker_call_1/request.json'
            else:
                outcome_path=job_folder/'outcome_2.json'
                request_path=job_folder/'worker_call_2/request.json'
            if (not adapter_repairs and outcome_path.exists() and request_path.exists()
                    and read(outcome_path).get('classification')=='INVALID_OR_MISSING_ARTIFACT'
                    and 'never' in read(request_path).get('argv',[])):
                adapter_repairs.append({
                    'job_id':pending_id,
                    'failed_call':str((job_folder/'worker_call_1').relative_to(ROOT)),
                    'failure':'workspace-write request was reduced to read-only under approval=never',
                    'changed_approach':'Codex --approve-for-me with workspace-write; model, reasoning, job and timeout unchanged',
                    'max_attempts':1,
                    'independent_trial':False,
                })
                s['worker_route_adapter_repair_pending_execution']=True
            elif (len(adapter_repairs)==1 and outcome_path.exists() and request_path.exists()
                    and read(outcome_path).get('classification')=='PROCESS_FAILED'
                    and '--approve-for-me' in read(request_path).get('argv',[])
                    and 'workspace-write' in read(request_path).get('argv',[])):
                adapter_repairs.append({
                    'job_id':pending_id,
                    'failed_call':str((job_folder/'worker_call_2').relative_to(ROOT)),
                    'failure':'Codex CLI rejected duplicate auto-review and explicit sandbox flags before session start',
                    'changed_approach':'retain --approve-for-me implicit workspace-write and remove only the conflicting explicit -s option',
                    'max_attempts':1,
                    'independent_trial':False,
                })
                s['worker_route_adapter_repair_pending_execution']=True
        if s.get('error') and s.get('status') not in ('RESOURCE_LIMIT', 'PAUSED_BUDGET'):
            s.setdefault('error_history', []).append({
                'error': s['error'], 'cleared_at_resume': True,
                'pending_job_id': (s.get('pending_job') or {}).get('job_id')})
            s['error']=None
        s['status']='RUNNING';self.save()
        reviewed_plan_pending=bool(resume and s.get('pending_job') and (
            s.pop('review_first_plan_pending_execution',False)
            or s.pop('worker_route_migration_pending_execution',False)
            or s.pop('worker_route_adapter_repair_pending_execution',False)))
        review_pending=bool(s.get('delegated_hold_review_required') or
                            s.get('autonomous_recovery', {}).get('review_required') or
                            (resume and s.get('pending_job') and not reviewed_plan_pending))
        if s.get('pending_job'):
            try:
                _guard_lean_payout_route(s, s['pending_job'])
            except ValueError as error:
                if not str(error).startswith('R27_DEADLINE:'):
                    raise
                review_pending=True  # Real Lead disposition before any expired route dispatch.
        if reviewed_plan_pending:
            self.save()
        try:
            while True:
                if any((RUNTIME/name).exists() for name in ('pause.request', 'cancel.request')):
                    s['status']='PAUSED_USER';break
                self.check_budget()
                maybe_review(self)
                if review_pending:
                    # A persisted pending job from an older stopped run must
                    # be reviewed by the real Lead before any Muse retry.
                    review_pending=False
                    decision=self.lead()
                    if decision['action']=='PAUSE':
                        s['status']=decision['pause_category'] if decision['pause_category']!='NONE' else 'PAUSED_LEAD'
                        if s.get('autonomous_recovery_authority') and s['status']=='PAUSED_LEAD':
                            s['error']='LEAD_PREREQUISITE: '+json.dumps({
                                'rationale':decision['rationale'],
                                'missing_information':decision['missing_information']},ensure_ascii=False)
                        break
                    if decision['action']=='READY_FOR_FORWARD':
                        s['status']='READY_FOR_FORWARD';break
                    if decision['action'] in ('NO_QUALIFYING_CANDIDATE','HUMAN_ACTION_REQUIRED'):
                        s['status']=decision['action'];break
                    continue
                if s['pending_job']:
                    from .lead_recovery import capture, enabled
                    from .qa_review import cycle_reserve
                    if enabled(s) and p_remaining(self.profile,s)<cycle_reserve(s):
                        s['status']='RESOURCE_LIMIT'
                        s['error']='RESOURCE_LIMIT: cannot reserve worker plus mandatory QA/Lead review'
                        break
                    try:
                        self.worker(s['pending_job'])
                    except Exception as error:
                        if not capture(self,error):
                            raise
                        if s.get('autonomous_recovery_authority'):
                            from .autonomous_recovery import capture as autonomous_capture
                            autonomous_capture(self, error, stage='worker')
                        review_pending=True
                    continue
                decision=self.lead()
                if decision['action']=='PAUSE':
                    s['status']=decision['pause_category'] if decision['pause_category']!='NONE' else 'PAUSED_LEAD'
                    if s.get('autonomous_recovery_authority') and s['status']=='PAUSED_LEAD':
                        s['error']='LEAD_PREREQUISITE: '+json.dumps({
                            'rationale':decision['rationale'],
                            'missing_information':decision['missing_information']},ensure_ascii=False)
                    break
                if decision['action']=='READY_FOR_FORWARD':
                    s['status']='READY_FOR_FORWARD';break
                if decision['action'] in ('NO_QUALIFYING_CANDIDATE','HUMAN_ACTION_REQUIRED'):
                    s['status']=decision['action'];break
                s['pending_job']=decision['next_job'];self.save()
        except Exception as e:
            s['error']=str(e)
            if str(e).startswith('HUMAN_ACTION_REQUIRED:'):
                s['status']='HUMAN_ACTION_REQUIRED'
                if active(s) and s.get('operator_safety_recovery'):
                    s['operator_safety_recovery']['revoked_due_to']=str(e)
            elif s['status']=='RUNNING':
                s['status']='ENGINEERING_BLOCKED'
        self.save();return s


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['start','status','pause','resume','review'])
    args=parser.parse_args()
    if args.operation=='status':
        print(json.dumps(read(RUNTIME/'state.json') if (RUNTIME/'state.json').exists() else {'status':'NOT_STARTED'},indent=2));return
    if args.operation=='pause':
        write(RUNTIME/'pause.request',{'requested_utc':dt.datetime.now(dt.timezone.utc).isoformat()})
        print('Pause requested; controller checkpoints at next call boundary');return
    with writer_lock(RUNTIME/'writer.lock'):
        c=Controller()
        result=c.review_once() if args.operation=='review' else c.run(resume=args.operation=='resume')
        print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()

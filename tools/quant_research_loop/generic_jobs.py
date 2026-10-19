"""Bounded research artifacts; new identities do not create new authority."""
import copy
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
import jsonschema
from .gate_zero import ROOT, write
from .prepare import CAMP, digest

BASE=CAMP/'continuations/G01_GENERIC_JOBS'
CLASSES=['DISCOVERY','DIAGNOSTIC','EXPERIMENT','ACCOUNT_POLICY','ENGINEERING','VERIFY']
PROHIBITED=['spend','paid_fallback','accounts_or_resets','orders','live_deployment','protected_data',
            'change_mandate','increase_global_budgets','destructive_git','commit_or_push','child_models']

def obj(properties):
    return dict(type='object',additionalProperties=False,properties=properties,required=list(properties))

STR={'type':'string'}
STRINGS={'type':'array','items':STR}
SPEC=obj(dict(job_id=STR,job_class={'type':'string','enum':CLASSES},objective=STR,candidate_id=STR,
    inputs={'type':'array','items':obj(dict(alias=STR,path=STR,sha256=STR,kind={'type':'string','enum':['json','csv','text']},partition=STR))},
    allowed_data=STRINGS,allowed_file_scope=STRINGS,required_outputs=STRINGS,
    verification=obj(dict(method={'type':'string','enum':['reproduce_json']},required_keys=STRINGS,
                          min_records={'type':'integer','minimum':0,'maximum':100000},description=STR)),
    dependencies=STRINGS,prohibited_actions=STRINGS,
    permissions=obj(dict(network={'type':'string','enum':['none']},spend_usd={'type':'number'},live={'type':'boolean'})),
    effective_profile_sha256=STR))
# A bounded risk ceiling is part of the job identity when a job emits a
# financial ledger.  It is optional for diagnostics/verification jobs so old
# frozen contracts remain readable; EXPERIMENT and ACCOUNT_POLICY validation
# below requires it for new dispatches.
SPEC['properties']['risk_ceiling_usd']={'type':'number','exclusiveMinimum':0}
SPEC['properties']['recovery_episode']={'anyOf':[{'type':'null'},obj(dict(
    component=STR,root_cause=STR,changed_approach=STR,stop_condition=STR,
    evidence_refs=STRINGS,max_attempts={'type':'integer','minimum':1,'maximum':2}))]}
SPEC['properties']['implementation']={'anyOf':[{'type':'null'},obj(dict(
    write_paths=STRINGS,test_modules=STRINGS))]}
# The runtime validator accepts this optional metadata field so preserved
# diagnostic/verification contracts without it remain valid.  The strict Lead
# response schema below requires it for every newly proposed generic job.
LEAD_SPEC=copy.deepcopy(SPEC)
LEAD_SPEC['required'].append('risk_ceiling_usd')
LEAD_SPEC['required'].append('recovery_episode')
LEAD_SPEC['required'].append('implementation')

def read(p): return json.loads(Path(p).read_text(encoding='utf-8'))

def relpath(value):
    if not isinstance(value,str) or '\\' in value or ':' in value or value.startswith('/'):
        raise ValueError('HUMAN_ACTION_REQUIRED: non-repository path')
    p=Path(value)
    if '..' in p.parts or not p.parts: raise ValueError('Unsafe path traversal')
    full=(ROOT/p).resolve()
    if not full.is_relative_to(ROOT.resolve()): raise ValueError('Escaping repository')
    return full

def authority_catalog(profile,state):
    """Approved ledger artifacts + verified prior outputs, never a price glob."""
    catalog={}
    for job in profile['jobs']:
        for path,sha in job['input_hashes'].items():
            catalog[path]={'sha256':sha,'partition':job['data_partition']}
        for path,sha in job.get('code_hashes',{}).items():
            catalog[path]={'sha256':sha,'partition':'RESEARCH_CODE'}
    for path in [profile['rules_path'],profile['effective_profile_path'],profile['activation_evidence_path']]:
        catalog[path]={'sha256':digest(ROOT/path),'partition':'POLICY_OR_METADATA'}
    for jid,record in state.get('completed',{}).items():
        path=record['path'].replace('\\','/')
        catalog[path]={'sha256':record['sha256'],'partition':'CONSUMED_RESEARCH_EVIDENCE'}
        jobpath=f'research/campaigns/PAYOUT_V1/runtime/jobs/{jid}/job.json'
        if (ROOT/jobpath).exists(): catalog[jobpath]={'sha256':digest(ROOT/jobpath),'partition':'POLICY_OR_METADATA'}
        codepath=f'research/campaigns/PAYOUT_V1/runtime/jobs/{jid}/work/analysis.py'
        if (ROOT/codepath).exists(): catalog[codepath]={'sha256':digest(ROOT/codepath),'partition':'RESEARCH_CODE'}
    # Explicit partition files are added only from a separately validated data
    # capability receipt. Unknown local paths are not silently authorized.
    for path,record in state.get('authorized_data_artifacts',{}).items():
        if record['partition'] not in ('train','validation','CONSUMED_2026_DEVELOPMENT_PROXY'):
            raise ValueError('HUMAN_ACTION_REQUIRED: protected data partition')
        catalog[path]=record
    for path,record in state.get('authorized_metadata_artifacts',{}).items():
        if record['partition'] not in ('POLICY_OR_METADATA','RESEARCH_CODE'):
            raise ValueError('Invalid metadata capability')
        catalog[path]=record
    from .lead_recovery import enabled
    if enabled(state):
        # Code/schema access does not grant any additional price access.
        for path in sorted((ROOT/'tools').glob('*.py')):
            catalog[path.relative_to(ROOT).as_posix()]={'sha256':digest(path),'partition':'RESEARCH_CODE'}
        for path in sorted((ROOT/'tools/quant_research_loop').glob('*.py')):
            catalog[path.relative_to(ROOT).as_posix()]={'sha256':digest(path),'partition':'RESEARCH_CODE'}
    return catalog

def fingerprint(spec):
    value={k:v for k,v in spec.items() if k not in ('job_id','allowed_file_scope','required_outputs')}
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

def validate_spec(spec,profile,state,*,check_files=True):
    jsonschema.validate(spec,SPEC)
    jid=spec['job_id']
    if not re.fullmatch('[A-Z][A-Z0-9_]{4,95}',jid): raise ValueError('Invalid job identity')
    if jid in state.get('completed',{}): raise ValueError('Completed job cannot be rerun')
    from .lead_recovery import enabled,validate_episode
    validate_episode(spec,state)
    historical_frozen=(enabled(state) and state.get('generic_registry',{}).get(jid,{}).get('sha256')==
        (digest(ROOT/f'research/campaigns/PAYOUT_V1/runtime/jobs/{jid}/job.json') if (ROOT/f'research/campaigns/PAYOUT_V1/runtime/jobs/{jid}/job.json').exists() else None))
    if spec['effective_profile_sha256']!=profile['effective_profile_sha256'] and not historical_frozen: raise ValueError('HUMAN_ACTION_REQUIRED: mandate change')
    if spec.get('risk_ceiling_usd',0)>profile.get('effective_profile',{}).get('risk',{}).get('per_trade_usd_ceiling',300):
        raise ValueError('HUMAN_ACTION_REQUIRED: risk ceiling exceeds mandate')
    if spec['permissions']!={'network':'none','spend_usd':0,'live':False}: raise ValueError('HUMAN_ACTION_REQUIRED: prohibited permission')
    if set(spec['prohibited_actions'])!=set(PROHIBITED): raise ValueError('Explicit prohibited actions missing')
    if len(spec['objective'].strip())<20 or len(spec['verification']['description'].strip())<15: raise ValueError('Incomplete objective/verification')
    if len({x['alias'] for x in spec['inputs']})!=len(spec['inputs']): raise ValueError('Duplicate input alias')
    if not spec['inputs']: raise ValueError('No pinned input authority')
    catalog=authority_catalog(profile,state)
    for item in spec['inputs']:
        p=relpath(item['path'])
        if item['path'] not in catalog: raise ValueError('HUMAN_ACTION_REQUIRED: input lacks authorized data capability '+item['path'])
        cap=catalog[item['path']]
        if item['sha256']!=cap['sha256'] or item['partition']!=cap['partition']: raise ValueError('Input hash/partition capability mismatch')
        if check_files and digest(p)!=item['sha256']: raise ValueError('Pinned input changed')
    if set(spec['allowed_data'])!={i['path'] for i in spec['inputs']}: raise ValueError('Allowed data must equal exact pinned inputs')
    prefix=f'research/campaigns/PAYOUT_V1/runtime/jobs/{jid}/work/'
    expected=[prefix+'analysis.py',prefix+'result.json']
    implementation=spec.get('implementation')
    if implementation:
        if not enabled(state) or spec['job_class']!='ENGINEERING':
            raise ValueError('Scoped implementation requires R04 ENGINEERING authority')
        for path in implementation['write_paths']:
            relpath(path)
            # Trusted dispatch, financial verification and permissions remain
            # outside worker writes; technical research components are scoped.
            if not re.fullmatch(r'tools/[A-Za-z][A-Za-z0-9_]*\.py',path):
                raise ValueError('Implementation write outside technical component scope')
            if (ROOT/path).exists() and path not in spec['allowed_data']:
                raise ValueError('Existing implementation source must be pinned')
        for module in implementation['test_modules']:
            if not re.fullmatch(r'tools\.test_[A-Za-z0-9_]+',module):
                raise ValueError('Invalid scoped test module')
            path=module.replace('.','/')+'.py'
            if path not in spec['allowed_data'] and path not in implementation['write_paths']:
                raise ValueError('Scoped test is neither pinned nor authored')
        expected+=implementation['write_paths']
    if set(spec['allowed_file_scope'])!=set(expected) or set(spec['required_outputs'])!=set(expected):
        raise ValueError('HUMAN_ACTION_REQUIRED: write scope outside isolated research code and result')
    for path in expected: relpath(path)
    for dep in spec['dependencies']:
        if dep not in state.get('completed',{}): raise ValueError('Dependency not completed: '+dep)
    if spec['job_class'] in ('EXPERIMENT','ACCOUNT_POLICY'):
        if 'risk_ceiling_usd' not in spec:
            raise ValueError('Financial job missing frozen risk ceiling')
        family=spec['candidate_id']
        if not family: raise ValueError('Experiment family missing')
        if family not in state.get('families',{}) and len(state.get('families',{}))>=profile['max_families']: raise ValueError('NO_QUALIFYING_CANDIDATE: family budget exhausted')
        if state.get('families',{}).get(family,0)>=profile['max_per_family']: raise ValueError('NO_QUALIFYING_CANDIDATE: family variant budget exhausted')
    for old in state.get('generic_registry',{}).values():
        if old['fingerprint']==fingerprint(spec) and old['job_id']!=jid: raise ValueError('Duplicate specification under new ID')
    return spec

def register(spec,profile,state):
    validate_spec(spec,profile,state)
    if spec.get('recovery_episode'):
        # Recovery is new Lead execution authority, not a rewrite of the
        # frozen research contract. Persist it before restoring that contract.
        state.setdefault('lead_recovery_authorizations',{})[spec['job_id']] = {
            'episode':copy.deepcopy(spec['recovery_episode']),
            'decision_index':state.get('decisions'),
            'instruction':spec['objective']}
    folder=CAMP/'runtime/jobs'/spec['job_id'];folder.mkdir(parents=True,exist_ok=True)
    path=folder/'job.json'
    if path.exists() and read(path)!=spec:
        existing=read(path)
        # A Lead review may restate the question while retaining a pending
        # job ID. Resume must use the already frozen contract verbatim; the
        # restatement cannot silently alter inputs, outputs, permissions or
        # verification. The proposed spec was still validated above so any
        # prohibited capability remains rejected before this reconciliation.
        registered_recovery = (spec.get('recovery_episode') and
            state.get('generic_registry', {}).get(spec['job_id'], {}).get('sha256') == digest(path)
            and all(spec.get(k) == existing.get(k) for k in set(spec) | set(existing)
                    if k not in ('objective', 'recovery_episode')))
        if (state.get('pending_job') or {}).get('job_id')==spec['job_id'] or registered_recovery:
            write(folder/'contract_reconciliation.json',{
                'job_id':spec['job_id'],'mode':'REUSE_FROZEN_REGISTERED_RECOVERY' if registered_recovery else 'REUSE_FROZEN_PENDING_CONTRACT',
                'proposed_spec_sha256':hashlib.sha256(json.dumps(spec,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                'frozen_spec_sha256':digest(path),
                'differences_preserved_as_non_authoritative':sorted(k for k in set(spec)|set(existing) if spec.get(k)!=existing.get(k)),
                'permission_and_input_validation_applied_before_reuse':True,
                'new_job_or_trial':False,
            })
            spec=existing
        else:
            raise ValueError('Frozen job identity conflict')
    if not path.exists(): write(path,spec)
    state.setdefault('generic_registry',{})[spec['job_id']]={'job_id':spec['job_id'],'sha256':digest(path),'fingerprint':fingerprint(spec)}
    return spec

def repair_key(stage,code):
    # Caller supplies stable error class, never a job id or volatile trace text.
    return hashlib.sha256((stage+':'+code).encode()).hexdigest()[:20]

def reserve_repair(state,stage,code,evidence_hash):
    key=repair_key(stage,code)
    record=state.setdefault('blocker_repairs',{}).setdefault(key,{'stage':stage,'code':code,'attempts':0,'evidence':[]})
    if record['attempts']>=2: raise RuntimeError('ENGINEERING_BLOCKED: same blocker repair cap')
    if evidence_hash in record['evidence']: raise RuntimeError('ENGINEERING_BLOCKED: unchanged blocker without new evidence')
    record['attempts']+=1;record['evidence'].append(evidence_hash)
    return key

def repair_evidence(failure):
    # Attempt numbers and new filenames are not new evidence.
    return hashlib.sha256(json.dumps({k:failure.get(k) for k in ('code','analysis_sha256','input_hashes')},sort_keys=True).encode()).hexdigest()

def authorized(state,profile):
    auth=state.get('generic_authority')
    return bool(auth and profile.get('mandate_revision') and digest(ROOT/auth['path'])==auth['sha256'])

def accounted_seconds(state):
    g=state.get('generic_execution')
    if not g:
        from .r03 import accounted_seconds as historical
        return historical(state)
    elapsed=g.get('elapsed_wall_seconds',0)
    if g.get('active_epoch_utc'):
        elapsed+=max(0,(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(g['active_epoch_utc'])).total_seconds())
    return g['prior_accounted_seconds']+elapsed+max(0,state['active_invocation_s']-g['child_seconds_at_start'])

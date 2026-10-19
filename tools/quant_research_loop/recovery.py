"""C02: validate immutable historical evidence and review it, never replay it."""
import copy
import datetime as dt
import json
import jsonschema
from .controller import Controller, RUNTIME, SCHEMA, read, writer_lock
from .gate_zero import ROOT, write, lead_command, lead_route
from .model_routing import MODEL, REASONING, LEAD_LABEL
from .prepare import CAMP, digest
from tools.agent_loop.runtime_outcome import classify, events

BASE=CAMP/'continuations/C02_CLASSIFIER_RECOVERY'
C01=CAMP/'continuations/C01_FIRST_REAL_REPLAY'
JOB='PAYOUT_V1_J001_D1_R1'
SESSION='ses_f40e3b260ffeoi0J0jhS0lk2ql'
PATCHED_HISTORICAL_SOURCE={'tools/quant_research_loop/continuation.py','tools/quant_research_loop/controller.py'}

def validate_saved():
    baseline=read(BASE/'baseline.json')['files']
    # Every original C01 artifact remains immutable, including its mistaken status.
    prefix=C01.relative_to(ROOT).as_posix()+'/'
    for rel,sha in baseline.items():
        if rel.startswith(prefix) and digest(ROOT/rel)!=sha: raise ValueError('C01 evidence changed: '+rel)
    job=read(C01/'job.json'); result=read(C01/'result.json')
    if job['job_id']!=JOB or result['run_id']!=JOB or result['evidence_id']!=JOB: raise ValueError('Job identity mismatch')
    if digest(C01/'job.json')!=read(C01/'job.sha256.json')['sha256']: raise ValueError('Job hash mismatch')
    pin=read(BASE/'parent_before.json')['unreviewed_executions'][JOB]
    if pin['sha256']!=digest(C01/'result.json'): raise ValueError('Original result hash mismatch')
    if result['lineage']!=job['protected_hashes']: raise ValueError('Result lineage mismatch')
    for rel,sha in job['protected_hashes'].items():
        source=BASE/'original_source'/rel.split('/')[-1] if rel in PATCHED_HISTORICAL_SOURCE else ROOT/rel
        if digest(source)!=sha:
            # R03 activates a new evaluator without rebasing old evidence onto
            # new source hashes. Only exact, preserved pre-activation bytes can
            # satisfy the historical job's original pin.
            preserved=CAMP/'revisions/R03_PAYOUT_FIRST_20260921/activation/before'/rel
            if not preserved.is_file() or digest(preserved)!=sha:
                raise ValueError('Historical source/profile/input mismatch: '+rel)
    if read(C01/'worker_scope_check.json')['unexpected']: raise ValueError('Original scope violation')
    audit=read(C01/'blocker_audit.json')
    if not audit['deterministic_result_reconciliation'] or audit['actual_worker_session']!=SESSION:
        raise ValueError('Original verification missing')
    export=read(C01/'actual_worker_sanitized.json')
    if export['info']['model']!={'id':'muse-spark-1.3-contributor-free','providerID':'opencode','variant':'default'}:
        raise ValueError('Worker identity mismatch')
    raw=(C01/'worker_call/stdout.jsonl').read_text(encoding='utf-8')
    receipt=read(C01/'worker_call/receipt.json')
    outcome=classify(raw,receipt['exit_code'],session_id=SESSION,session_export=export,artifact_valid=True)
    if outcome!='SUCCESS': raise ValueError('Execution outcome: '+outcome)
    ev,_=events(raw)
    commands=[e['part']['state'] for e in ev if e.get('sessionID')==SESSION and e.get('type')=='tool_use' and e.get('part',{}).get('tool')=='shell']
    if len(commands)!=1 or commands[0]['input']['command']!='py -3.12 -X utf8 -m tools.quant_research_loop.continuation_replay' or commands[0]['status']!='completed' or 'REPLAY_COMPLETED' not in commands[0]['output']:
        raise ValueError('Missing actual one-time worker execution')
    if result['account_profile_sha256']!=job['account_profile_hash'] or result['ledger_sha256']!=next(iter(job['input_hashes'].values())):
        raise ValueError('Result manifest mismatch')
    approved=read(C01/'lead_before_fix2/accepted.json')['next_job']
    if approved['job_id']!=JOB or [x['scenario_id'] for x in result['scenarios']]!=approved['scenario_ids']:
        raise ValueError('Scenario authorization mismatch')
    return dict(status='VALIDATED_EXISTING_RESULT',job_id=JOB,result_sha256=pin['sha256'],
        worker_session=SESSION,old_classification='WORKER_ACCESS_BLOCKED',corrected_classification='SUCCESS',
        historical_manifest_unchanged=True,new_calculation_executions=0,
        source_correction_separate_from_execution=list(sorted(PATCHED_HISTORICAL_SOURCE)),
        basis='Original pins, saved execution outcome, original scope check and prior reconciliation; no recalculation in C02')

def decision_schema():
    schema=copy.deepcopy(SCHEMA)
    schema['properties']['result_sha256']={'type':'string'}
    schema['required'].append('result_sha256')
    return schema

def validate_review(dec,sha,ref):
    jsonschema.validate(dec,decision_schema())
    if dec['evidence_id']!=JOB or dec['result_sha256']!=sha or ref not in dec['artifact_refs'] or JOB not in dec['rationale']:
        raise ValueError('Review does not bind exact saved evidence')
    if not dec['rationale'].strip(): raise ValueError('Missing rationale')
    if dec['action']=='READY_FOR_FORWARD': raise ValueError('Qualification not authorized or verified')
    if dec['action']=='PAUSE' and dec['next_job'] is not None: raise ValueError('Pause cannot dispatch a job')
    if dec['action']!='PAUSE' and dec['next_job'] is None: raise ValueError('Missing pending proposal')

class Recovery(Controller):
    def __init__(self):
        self.profile=read(RUNTIME/'profile.json')
        if digest(RUNTIME/'profile.json')!=read(RUNTIME/'profile.sha256.json')['sha256']: raise ValueError('Profile changed')
        p=self.profile
        if (p['lead'],p['reasoning'],p['worker'],p['spend_authorized'],p['live'])!=('gpt-6-astra','medium','opencode/muse-spark-1.3-contributor-free',0,'DISABLED'):
            raise ValueError('Protected route/financial permission changed')
        self.state_path=RUNTIME/'state.json'; self.state=read(self.state_path)

    def check_budget(self):
        c=read(BASE/'state.json');s=self.state;p=self.profile
        now=dt.datetime.now(dt.timezone.utc)
        used=c['lead_calls']-int(getattr(self,'_reserved',False))
        if used>=2 or (dt.datetime.fromisoformat(c['deadline_utc'])-now).total_seconds()<300: raise RuntimeError('C02_BUDGET_EXHAUSTED')
        elapsed=(now-dt.datetime.fromisoformat(c['start_utc'])).total_seconds()
        prior_builder=read(C01/'state.json')['elapsed_wall_seconds']
        if s['model_calls']+s['bootstrap_invocations']+s['prior_reserved_calls']>=p['max_calls'] or s['active_invocation_s']+s['prior_reserved_seconds']+prior_builder+elapsed>=p['max_active_seconds']:
            raise RuntimeError('PARENT_BUDGET_EXHAUSTED')
        if s.get('inflight_call'): raise RuntimeError('AMBIGUOUS_PRIOR_CALL')

    def review_call(self,evidence):
        folder=BASE/'lead_review';folder.mkdir(exist_ok=True)
        ref=(folder/'evidence.json').relative_to(ROOT).as_posix()
        if (folder/'accepted.json').exists():
            dec=read(folder/'accepted.json');validate_review(dec,evidence['validation']['result_sha256'],ref);return dec
        if (folder/'call').exists():
            # No blind duplicate after a crash: accept only an explicit completed turn.
            if not (folder/'call/receipt.json').exists() or not (folder/'decision.json').exists(): raise RuntimeError('AMBIGUOUS_LEAD_COMPLETION')
            request=read(folder/'call/request.json') if (folder/'call/request.json').exists() else {}
            argv=request.get('argv',[])
            if ('-m' not in argv or argv[argv.index('-m')+1:argv.index('-m')+2] != [MODEL]
                    or f'model_reasoning_effort="{REASONING}"' not in argv):
                raise RuntimeError('LEAD_ROUTE_RECONCILIATION_REQUIRED: historical call preserved without relabeling')
            receipt=read(folder/'call/receipt.json');raw=(folder/'call/stdout.jsonl').read_text(encoding='utf-8')
        else:
            self.check_budget()
            write(folder/'evidence.json',evidence);write(folder/'schema.json',decision_schema())
            prompt=(f'You are the fresh automated Research Lead ({LEAD_LABEL}). The controller delivers immutable saved evidence; no human forwarded it. '
                'C02 fixes a false access-denial classifier only. The actual Muse worker succeeded earlier. Independently review the original '
                'job/result, limitations, financial targets and validation. No shell/tools, files, new calculations, worker, purchase, or orders. '
                'Do not infer payout success or full autonomous readiness from software recovery. Preserve $120 simulation and all targets. '
                'Choose a schema-valid next decision; a negative decision or PAUSE is acceptable. Any next_job is queued only, not dispatched, '
                'and unregistered IDs remain proposals requiring registration. C02 stops after this review. Cite exact run_id in rationale, '
                'return exact result_sha256 and evidence_id and reference '+ref+'.\nCONTROLLER EVIDENCE:\n'+json.dumps(evidence,ensure_ascii=False))
            cmd=lead_command(folder/'schema.json',folder/'decision.json')
            write(folder/'lead_route.json',lead_route())
            # Reserve C02 count before dispatch; parent adapter separately reserves its call.
            c=read(BASE/'state.json');c['lead_calls']+=1;write(BASE/'state.json',c)
            self._reserved=True
            try: receipt,raw=super().model_call(cmd,prompt,folder/'call')
            finally: self._reserved=False
            c=read(BASE/'state.json');c['active_child_seconds']+=receipt['elapsed_s'];write(BASE/'state.json',c)
        parsed,bad=events(raw)
        if receipt['exit_code']!=0 or bad or not any(e.get('type')=='turn.completed' for e in parsed) or any(e.get('type') in ('error','turn.failed') for e in parsed):
            raise RuntimeError('LEAD_COMPLETION_NOT_CONFIRMED')
        if any(e.get('type')=='item.completed' and e.get('item',{}).get('type') in ('command_execution','mcp_tool_call','web_search') for e in parsed):
            raise RuntimeError('LEAD_EXCEEDED_READ_ONLY_EVIDENCE_SCOPE')
        dec=read(folder/'decision.json');validate_review(dec,evidence['validation']['result_sha256'],ref)
        write(folder/'accepted.json',dec)
        write(folder/'session.json',dict(thread_id=next(e['thread_id'] for e in parsed if e.get('type')=='thread.started'),
             **lead_route(),receipt=receipt))
        return dec

    def recover(self):
        validation=validate_saved()
        c=read(BASE/'state.json')
        if c['status']=='REAL_REPLAY_COMPLETE':
            dec=read(BASE/'lead_review/accepted.json')
            validate_review(dec,validation['result_sha256'],(BASE/'lead_review/evidence.json').relative_to(ROOT).as_posix())
            if self.state.get('reviewed_results',{}).get(JOB)!=validation['result_sha256']: raise ValueError('Recovery checkpoint mismatch')
            return dict(status='ALREADY_REVIEWED',decision=dec,result_sha256=validation['result_sha256'])
        if 'C02' not in self.state.get('recovery_builders',[]):
            self.state.setdefault('recovery_builders',[]).append('C02')
            self.state['bootstrap_invocations']+=1
            self.save()
        write(BASE/'validation.json',validation)
        evidence=dict(evidence_id=JOB,validation=validation,job=read(C01/'job.json'),result=read(C01/'result.json'),
            original_approval=read(C01/'lead_before_fix2/accepted.json'),profile=self.profile,
            recovery_note='False positive from benign numbers in tool data; old trace/classification retained. No worker or simulator run in C02.')
        dec=self.review_call(evidence)
        if self.state.get('reviewed_results',{}).get(JOB)!=validation['result_sha256']:
            if self.state['trials']!=read(BASE/'parent_before.json')['trials']: raise ValueError('Trial count drift')
            record=self.state['unreviewed_executions'].pop(JOB)
            self.state['completed'][JOB]=dict(path=record['path'],sha256=record['sha256'],review=str((BASE/'lead_review/accepted.json').relative_to(ROOT)))
            self.state.setdefault('reviewed_results',{})[JOB]=validation['result_sha256']
            self.state['decisions']+=1;self.state['evidence_id']=JOB
            self.state['pending_job']=None
            self.state['pending_proposal']=dec['next_job']
            self.state['continuation_status']='REVIEW_STORED_STOP_AFTER_REVIEW'
            self.state['last_decision']=str((BASE/'lead_review/accepted.json').relative_to(ROOT))
            self.save()
        c=read(BASE/'state.json');c.update(status='REAL_REPLAY_COMPLETE',result_sha256=validation['result_sha256'],
             stop_after_review=True,pending_proposal=dec['next_job'],completed_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        write(BASE/'state.json',c)
        return dict(status='REVIEW_STORED',decision=dec,result_sha256=validation['result_sha256'])

def main():
    with writer_lock(RUNTIME/'controller.lock'):
        print(json.dumps(Recovery().recover(),ensure_ascii=False,indent=2))

if __name__=='__main__': main()

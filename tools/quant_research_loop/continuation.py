"""One authorized real replay, supervised by real Lead and executed by Muse."""
import copy
import datetime as dt
import json
import time
import subprocess
import jsonschema
from .controller import Controller, RUNTIME, SCHEMA, read, snapshot, writer_lock
from .gate_zero import ROOT, native_command, write, lead_command, lead_route
from .model_routing import LEAD_LABEL
from .prepare import CAMP, digest
from .continuation_replay import BASE, evaluate
from tools.agent_loop.opencode_runner import extract_tool_calls
from tools.agent_loop.runtime_outcome import classify

CMD='py -3.12 -X utf8 -m tools.quant_research_loop.continuation_replay'
JOB='PAYOUT_V1_J001_D1_R1'

def scope_snapshot():
    names=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT).decode().split('\0')
    lock=(RUNTIME/'controller.lock').relative_to(ROOT).as_posix()
    return {n:digest(ROOT/n) for n in names if n and n!=lock and (ROOT/n).is_file()}

def budget_guard(s,p,c,now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    if p['spend_authorized']!=0 or p['live']!='DISABLED': raise RuntimeError('SAFETY_BLOCKED')
    if c['child_calls']>=12 or c['change_sets_used']>2 or now>=dt.datetime.fromisoformat(c['deadline_utc']):
        raise RuntimeError('CONTINUATION_BUDGET_EXHAUSTED')
    elapsed=(now-dt.datetime.fromisoformat(c['start_utc'])).total_seconds()
    if s['model_calls']+s['bootstrap_invocations']+s['prior_reserved_calls']>=p['max_calls'] or s['active_invocation_s']+s['prior_reserved_seconds']+elapsed>=p['max_active_seconds']:
        raise RuntimeError('PARENT_BUDGET_EXHAUSTED')

def worker_status(receipt,raw,exists,*,session_export=None):
    outcome=classify(raw,receipt['exit_code'],session_id=receipt.get('session_id'),
                     session_export=session_export,artifact_valid=exists)
    if outcome=='AUTH': return 'WORKER_ACCESS_BLOCKED'
    if outcome=='SUCCESS': return 'RESULT_PRESENT'
    return 'WORKER_'+outcome

class Continuation(Controller):
    def check_budget(self):
        c=read(BASE/'state.json')
        if getattr(self,'_reserved',False): c['child_calls']-=1
        budget_guard(self.state,self.profile,c)
        if (dt.datetime.fromisoformat(c['deadline_utc'])-dt.datetime.now(dt.timezone.utc)).total_seconds()<300:
            raise RuntimeError('INSUFFICIENT_CONTINUATION_TIME_FOR_BOUNDED_CALL')

    def model_call(self,cmd,prompt,folder,**kwargs):
        self.check_budget()
        c=read(BASE/'state.json'); c['child_calls']+=1; c['inflight_call']=str(folder); write(BASE/'state.json',c)
        # Parent's pre-dispatch guard sees the increment; 12th dispatch is reserved here.
        self._reserved=True
        try:
            receipt,raw=super().model_call(cmd,prompt,folder,**kwargs)
        finally:
            self._reserved=False
        c=read(BASE/'state.json'); c.pop('inflight_call',None); c['active_child_seconds']+=receipt['elapsed_s']; write(BASE/'state.json',c)
        return receipt,raw

    def decide(self,stage,evidence):
        folder=BASE/stage
        if (folder/'accepted.json').exists(): return read(folder/'accepted.json')
        if (folder/'call').exists(): raise RuntimeError('LEAD_CALL_RECONCILIATION_REQUIRED')
        folder.mkdir(exist_ok=True)
        schema=copy.deepcopy(SCHEMA)
        schema['properties']['next_job']={'anyOf':[{'type':'null'},{'type':'object','additionalProperties':False,
            'properties':{'job_id':{'type':'string','enum':[JOB]},'scenario_ids':{'type':'array','items':{'type':'string','enum':['STANDARD_50K','NO_ACTIVATION_50K']}}},'required':['job_id','scenario_ids']}]}
        write(folder/'schema.json',schema); write(folder/'evidence.json',evidence)
        ref=(folder/'evidence.json').relative_to(ROOT).as_posix()
        prompt=(f'You are the actual automated Research Lead ({LEAD_LABEL}). This is a controller invocation; no human forwards evidence. '
                'Read the supplied evidence and decide independently. No shell, no tools, no writes, no child models. '
                'The user expressly authorizes ONE diagnostic saved-ledger replay and its review. $120 simulation unchanged; '
                'actual spending zero, live DISABLED, targets and data boundaries unchanged. Original J001 and its Standard '
                'affordability pause remain preserved. Standard affordability is a PATH result, not a global engineering halt. '
                f'Before replay, select REPLAY_ACCOUNT_POLICY with versioned successor {JOB} and scenario_ids '
                'including STANDARD_50K; optionally include NO_ACTIVATION_50K (95 monthly/0 activation, same Standard XFA). '
                'Or PAUSE for a concrete safety/data blocker. After the actual replay, cite its run_id and concrete numerical '
                'results in rationale, then PAUSE with next_job null because this continuation ends at review. Explain '
                'negative/unknown findings and exact next requirements; never claim full loop or payout success. '
                f'Return evidence_id={evidence["evidence_id"]}, artifact_refs including {ref}.\n'+json.dumps(evidence))
        out=folder/'decision.json'
        cmd=lead_command(folder/'schema.json',out)
        write(folder/'lead_route.json',lead_route())
        receipt,_=self.model_call(cmd,prompt,folder/'call')
        if receipt['exit_code']!=0 or not out.exists(): raise RuntimeError('LEAD_ACCESS_BLOCKED')
        dec=read(out); jsonschema.validate(dec,schema)
        if dec['evidence_id']!=evidence['evidence_id'] or ref not in dec['artifact_refs']: raise ValueError('Stale Lead evidence')
        if stage.startswith('lead_before') and dec['action']=='REPLAY_ACCOUNT_POLICY':
            ids=dec['next_job']['scenario_ids']
            if 'STANDARD_50K' not in ids or len(ids)!=len(set(ids)): raise ValueError('Required preserved scenario missing')
        elif dec['action']!='PAUSE' or dec['next_job'] is not None: raise ValueError('Scope exceeded')
        if stage=='lead_after' and JOB not in dec['rationale']: raise ValueError('Lead did not cite actual run')
        write(folder/'accepted.json',dec)
        return dec

    def run_once(self):
        c=read(BASE/'state.json')
        if c['status']=='REAL_REPLAY_COMPLETE':
            if read(BASE/'result.json')!=evaluate(): raise ValueError('Resume evidence mismatch')
            return c
        if not (BASE/'job.json').exists():
            job=copy.deepcopy(self.profile['jobs'][0]); job['job_id']=JOB; job['supersedes_for_diagnostic_only']='PAYOUT_V1_J001'
            ledger=ROOT/next(iter(job['input_hashes']))
            protected=[ledger,ledger.with_name('daily.csv'),ledger.with_name('marks.csv.gz'),RUNTIME/'profile.json',RUNTIME/'rules.json',
                ROOT/job['allowed_paths'][1],ROOT/job['allowed_paths'][2],BASE/'sources/manifest.json',BASE/'scenarios.json']
            protected+=list((ROOT/'tools/quant_research_loop').glob('*.py'))
            job['protected_hashes']={p.relative_to(ROOT).as_posix():digest(p) for p in protected}
            job['outputs']=[(BASE/'result.json').relative_to(ROOT).as_posix()]
            job['verification']=['Controller independently recomputes continuation_replay.evaluate and compares exact persisted JSON; no dispatch of original J001 verification command.']
            write(BASE/'job.json',job); write(BASE/'job.sha256.json',{'sha256':digest(BASE/'job.json')})
        first=self.decide('lead_before_fix2',dict(evidence_id='C01_REGISTERED_DIAGNOSTIC_FIX2',job=read(BASE/'job.json'),
            corrective_change='Owned OS lock excluded from file content hash; initial D1 never dispatched; initial job and real Lead decision preserved. D1_R1 binds corrected code.',
            original_pause=read(RUNTIME/'decisions/000/accepted.json'),scenarios=read(BASE/'scenarios.json'),
            source_manifest=read(BASE/'sources/manifest.json'),profile=self.profile,
            data='Saved consumed train trades/daily/OHLC intraday marks; proxy execution uncertain; no price reads.'))
        if first['action']=='PAUSE': raise RuntimeError('PAUSED_BY_LEAD_BEFORE_REPLAY')
        call=BASE/'worker_call'
        if not call.exists():
            if (BASE/'result.json').exists(): raise RuntimeError('UNATTRIBUTED_RESULT')
            before=scope_snapshot(); write(BASE/'worker_scope_before.json',before)
            prompt=('You are Muse, the actual experiment worker for an authorized saved-ledger payout replay. '
                    'The automated Research Lead approved the pinned job in '+str(BASE/'lead_before_fix2/accepted.json')+'. '
                    'Execute exactly this one shell command from the repository root: '+CMD+'. '
                    'Do not edit code or inputs, read raw prices, access reserved data, call other models, spend, or place orders. '
                    'The deterministic handler reads only pinned saved ledgers and writes its one result. '
                    'After command completion report its output. If denied or failed stop; no workaround or retry.')
            cmd=native_command('opencode')+['run','--standalone','--format','json','--model',self.profile['worker'],'--agent','build',prompt]
            receipt,raw=self.model_call(cmd,prompt,call,cwd=ROOT)
        else:
            receipt=read(call/'receipt.json'); raw=(call/'stdout.jsonl').read_text(encoding='utf-8'); before=read(BASE/'worker_scope_before.json')
        after=scope_snapshot(); allowed_prefix=BASE.relative_to(ROOT).as_posix()+'/'
        changed=[n for n in set(before)|set(after) if before.get(n)!=after.get(n)]
        allowed={allowed_prefix+'state.json',allowed_prefix+'result.json','research/campaigns/PAYOUT_V1/runtime/state.json'}
        bad=[n for n in changed if n not in allowed and not n.startswith(allowed_prefix+'worker_call/') and n!=allowed_prefix+'worker_scope_before.json']
        write(BASE/'worker_scope_check.json',dict(changed=changed,unexpected=bad))
        if bad: raise RuntimeError('WORKER_SCOPE_VIOLATION')
        status=worker_status(receipt,raw,(BASE/'result.json').exists())
        if status!='RESULT_PRESENT': raise RuntimeError(status)
        calls=extract_tool_calls(raw)
        if not any(CMD in str(x['args']) for x in calls): raise RuntimeError('NO_WORKER_EXECUTION_EVIDENCE')
        result=read(BASE/'result.json')
        if result!=evaluate(): raise RuntimeError('DETERMINISTIC_VERIFICATION_FAILED')
        write(BASE/'verification.json',dict(status='PASS',result_sha256=digest(BASE/'result.json'),worker_calls=calls,
              reproduction_is_not_new_trial=True,protected_inputs_verified=True))
        if JOB not in self.state['completed']:
            self.state['completed'][JOB]={'path':str((BASE/'result.json').relative_to(ROOT)),'sha256':digest(BASE/'result.json')}
            self.state['trials']+=1; self.save()
        decision=self.decide('lead_after',dict(evidence_id=JOB,result=result,verification=read(BASE/'verification.json'),profile=self.profile))
        c=read(BASE/'state.json'); c.update(status='REAL_REPLAY_COMPLETE',pending_job=None,lead_next_decision=decision,
            human_message_forwarding_count=0,completed_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        write(BASE/'state.json',c); return c

def main():
    with writer_lock(RUNTIME/'controller.lock'):
        controller=Continuation()
        try: result=controller.run_once()
        except Exception as exc:
            c=read(BASE/'state.json'); c['status']=str(exc); write(BASE/'state.json',c); raise
        print(json.dumps(result,indent=2))

if __name__=='__main__': main()

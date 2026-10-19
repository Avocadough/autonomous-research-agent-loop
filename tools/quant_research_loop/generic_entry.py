"""Fixed command for bounded pure-Python research artifacts. No arbitrary shell.

Code receives only parsed pinned inputs; no filesystem/network/process builtins.
AST checks reduce accidental authority escape, not an OS security sandbox.
"""
import ast
import builtins
import csv
import gzip
import importlib
import json
import math
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from .generic_jobs import read,relpath
from .gate_zero import ROOT,write
from .prepare import digest

MODULES={'math','statistics','decimal','datetime','calendar','collections','itertools','functools','operator','copy','bisect','heapq','json'}
DENIED={'open','eval','exec','compile','getattr','setattr','delattr','globals','locals','vars','dir','type','object','super','help','input','breakpoint',
        'os','sys','io','pathlib','subprocess','socket','requests','urllib','importlib','builtins','modules','codecs','loader','mro'}

def check_source(source):
    tree=ast.parse(source)
    for n in ast.walk(tree):
        if isinstance(n,(ast.Import,ast.ImportFrom)):
            names=[x.name.split('.')[0] for x in n.names] if isinstance(n,ast.Import) else [n.module]
            if any(name not in MODULES for name in names) or getattr(n,'level',0): raise ValueError('Forbidden import')
        if isinstance(n,ast.Name) and (n.id in DENIED or n.id.startswith('_')): raise ValueError('Forbidden name: '+n.id)
        if isinstance(n,ast.Attribute) and (n.attr.startswith('_') or n.attr in DENIED): raise ValueError('Forbidden attribute: '+n.attr)
    return tree

def run_analysis(job,source):
    check_source(source)
    inputs={}
    for item in job['inputs']:
        p=relpath(item['path'])
        if digest(p)!=item['sha256']: raise ValueError('Input hash changed')
        if item['kind']=='json': value=read(p)
        elif item['kind']=='csv':
            open_csv = gzip.open if p.suffix.lower() == '.gz' else open
            with open_csv(p,'rt',encoding='utf-8-sig',newline='') as f: value=list(csv.DictReader(f))
        else: value=p.read_text(encoding='utf-8')
        inputs[item['alias']]=value
    def importer(name,globals=None,locals=None,fromlist=(),level=0):
        if name not in MODULES or level: raise ValueError('Import outside capability')
        return importlib.import_module(name)
    safe={n:getattr(builtins,n) for n in ['abs','all','any','bool','dict','enumerate','float','int','isinstance','len','list','map','max','min','next','range','reversed','round','set','sorted','str','sum','tuple','zip','Exception','ValueError','AssertionError','StopIteration']}
    safe['__import__']=importer
    ns={'__builtins__':safe}
    exec(compile(source,'<bounded research analysis>','exec'),ns)
    payload=ns['run']({'job':job,'inputs':inputs})
    if not isinstance(payload,dict): raise ValueError('Result must be an object')
    for key in job['verification']['required_keys']:
        if key not in payload: raise ValueError('Missing required output key '+key)
    if len(payload.get('records',[]))<job['verification']['min_records']: raise ValueError('Missing required records')
    financial_verification=_verify_financial_payload(job,payload)
    json.dumps(payload,allow_nan=False)
    return dict(campaign_id='PAYOUT_V1',evidence_id=job['job_id'],run_id=job['job_id'],job_class=job['job_class'],
                effective_profile_sha256=job['effective_profile_sha256'],job_sha256=digest(ROOT/f'research/campaigns/PAYOUT_V1/runtime/jobs/{job["job_id"]}/job.json'),
                input_hashes={i['path']:i['sha256'] for i in job['inputs']},analysis_sha256=__import__('hashlib').sha256(source.encode()).hexdigest(),
                payload=payload,financial_verification=financial_verification,actual_cash_received=0,ready_for_forward=False,claim_limit='Development research; deterministic reproduction is not independent market validation')


def _decimal(value,label):
    try:
        value=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):
        raise ValueError('Invalid financial number: '+label)
    if not value.is_finite():
        raise ValueError('Non-finite financial number: '+label)
    return value


def _direction(value):
    if isinstance(value,(int,float)) and value in (-1,1): return Decimal(str(value))
    if isinstance(value,str):
        value=value.strip().lower()
        if value in ('long','buy','1','+1'): return Decimal('1')
        if value in ('short','sell','-1'): return Decimal('-1')
    raise ValueError('Invalid trade direction')


def _risk_ceiling(job,payload):
    cap=job.get('risk_ceiling_usd')
    if cap is None and isinstance(payload,dict): cap=payload.get('risk_ceiling_usd')
    if cap is None: return None
    cap=_decimal(cap,'risk_ceiling_usd')
    if cap<=0: raise ValueError('Risk ceiling must be positive')
    return cap


def _verify_financial_payload(job,payload):
    """Independent arithmetic checks for generic financial artifacts.

    Generated code supplies observations; this function derives price P&L,
    costs, aggregates and account cash transitions in the trusted runner.
    Jobs without a financial ledger (DIAGNOSTIC/VERIFY/ENGINEERING) are
    explicitly labelled as such rather than receiving a misleading PASS.
    """
    kind=job['job_class']
    trades=payload.get('trades') if isinstance(payload,dict) else None
    if kind not in ('EXPERIMENT','ACCOUNT_POLICY'):
        return {'scope':'no_financial_ledger_required','trade_arithmetic':'NOT_APPLICABLE',
                'account_cash_transition':'NOT_APPLICABLE'}
    if not isinstance(trades,list):
        raise ValueError('Financial job requires canonical trade ledger')
    cap=_risk_ceiling(job,payload)
    if cap is None: raise ValueError('Financial job missing risk ceiling authority')
    total=Decimal('0'); positive=0; negative=0
    for index,t in enumerate(trades):
        if not isinstance(t,dict): raise ValueError('Trade row must be an object')
        qty=_decimal(t.get('quantity',t.get('qty')),'trade quantity')
        multiplier=_decimal(t.get('multiplier'),'trade multiplier')
        if qty<=0 or qty!=qty.to_integral_value() or multiplier<=0:
            raise ValueError('Invalid trade quantity/multiplier')
        direction=_direction(t.get('direction'))
        entry=t.get('entry_price',t.get('entry'))
        exit=t.get('exit_price',t.get('exit'))
        gross=_decimal(t.get('gross_pnl'),'gross_pnl')
        commission=_decimal(t.get('commission',0),'commission')
        slippage=_decimal(t.get('slippage_cost',0),'slippage_cost')
        net=_decimal(t.get('net_pnl'),'net_pnl')
        if commission<0 or slippage<0: raise ValueError('Negative trading cost')
        if entry is not None and exit is not None:
            derived=(_decimal(exit,'exit_price')-_decimal(entry,'entry_price'))*direction*qty*multiplier
            if abs(derived-gross)>Decimal('.01'):
                raise ValueError('Gross P&L not reconciled to prices')
        if abs(gross-commission-slippage-net)>Decimal('.01'):
            raise ValueError('Independent trade cost arithmetic failed')
        risk=_decimal(t.get('planned_risk_usd'),'planned_risk_usd')
        if risk<=0 or risk>cap:
            raise ValueError('Risk/micro-contract mandate violation')
        if t.get('instrument') not in ('MNQ','MES','MGC','MCL'):
            raise ValueError('Risk/micro-contract mandate violation')
        total+=net
        positive+=1 if net>0 else 0
        negative+=1 if net<0 else 0
    # If a summary is emitted, independently reconcile the trade population.
    summary=payload.get('summary') or payload.get('metrics') or {}
    if isinstance(summary,dict):
        if 'trade_count' in summary and int(summary['trade_count'])!=len(trades):
            raise ValueError('Summary/trade count mismatch')
        if 'net_pnl' in summary and abs(_decimal(summary['net_pnl'],'summary net_pnl')-total)>Decimal('.01'):
            raise ValueError('Summary/ledger net P&L mismatch')
    if not trades and isinstance(summary,dict) and 'net_pnl' in summary and _decimal(summary['net_pnl'],'summary net_pnl')!=0:
        raise ValueError('Nonzero summary with empty trade ledger')
    transition_status='NOT_APPLICABLE'
    if kind=='ACCOUNT_POLICY':
        transitions=payload.get('cash_transitions')
        if not isinstance(transitions,list) or not transitions:
            raise ValueError('ACCOUNT_POLICY requires trusted cash transitions')
        for row in transitions:
            if not isinstance(row,dict): raise ValueError('Cash transition row must be an object')
            start=_decimal(row.get('starting_cash'),'starting_cash')
            end=_decimal(row.get('ending_cash'),'ending_cash')
            net=_decimal(row.get('net_pnl',0),'cash net_pnl')
            costs=_decimal(row.get('account_costs',0),'account_costs')
            if costs<0: raise ValueError('Negative account cost')
            if abs((start+net-costs)-end)>Decimal('.01'):
                raise ValueError('Trusted cash transition arithmetic failed')
        transition_status='PASS_INDEPENDENT_CONTROLLER'
    return {'scope':'trusted_controller_financial_checks','trade_count':len(trades),
            'trade_arithmetic':'PASS_INDEPENDENT_CONTROLLER','gross_price_reconciliation':'PASS' if any(t.get('entry_price',t.get('entry')) is not None for t in trades) else 'NOT_PRESENT',
            'aggregate_reconciliation':'PASS','risk_ceiling_usd':float(cap),
            'positive_trades':positive,'negative_trades':negative,
            'account_cash_transition':transition_status}

def _saved_verification_state(job,jobpath,out,state):
    """Validate a pinned completed artifact without granting dispatch authority."""
    jid=job['job_id']
    record=state.get('completed',{}).get(jid)
    if record is None:
        return state
    expected=ROOT/f'research/campaigns/PAYOUT_V1/runtime/jobs/{jid}/job.json'
    if jobpath!=expected.resolve(): raise ValueError('Completed verification requires exact registered job path')
    if state.get('generic_registry',{}).get(jid,{}).get('sha256')!=digest(jobpath):
        raise ValueError('No controller registration')
    if relpath(record.get('path'))!=out.resolve():
        raise ValueError('Completed result path mismatch')
    if not out.is_file(): raise ValueError('Completed saved result missing')
    if record.get('sha256')!=digest(out): raise ValueError('Completed saved result hash changed')
    if read(out).get('job_sha256')!=digest(jobpath): raise ValueError('Completed result job pin mismatch')
    # Only this local read-only validator view omits the dispatch rejection.
    # The persisted completed registry, accounting and ordinary entry stay intact.
    view=dict(state,completed=dict(state['completed']))
    del view['completed'][jid]
    return view


def main():
    jobpath=relpath(sys.argv[1]);job=read(jobpath)
    from .controller import RUNTIME
    state=read(RUNTIME/'state.json')
    from .r03 import active_profile
    from .generic_jobs import validate_spec
    from .lead_recovery import apply
    folder=jobpath.parent/'work';out=folder/'result.json'
    validation_state=_saved_verification_state(job,jobpath,out,state) if '--verify' in sys.argv else state
    validate_spec(job,apply(active_profile(),state),validation_state)
    if state['generic_registry'][job['job_id']]['sha256']!=digest(jobpath): raise ValueError('No controller registration')
    source=(folder/'analysis.py').read_text(encoding='utf-8')
    if '--verify' in sys.argv:
        if job['job_id'] in state.get('completed',{}):
            source_sha256=__import__('hashlib').sha256(source.encode()).hexdigest()
            if read(out).get('analysis_sha256')!=source_sha256:
                raise ValueError('Completed saved analysis hash changed')
        if read(out)!=run_analysis(job,source): raise ValueError('Deterministic reproduction mismatch')
        print('VERIFIED_EXISTING_RESULT');return
    if out.exists(): print('EXISTING_RESULT_REUSED; controller verifies without another worker run');return
    value=run_analysis(job,source);write(out,value);print('GENERIC_JOB_COMPLETED '+job['job_id'])

def entrypoint():
    """Keep Timer90 only for historical routes; validate R09 before bypass."""
    import threading,os
    from .controller import RUNTIME
    from .r03 import active_profile
    from .runtime_policy import REVISION_UNBOUNDED,cumulative_runtime_cap_bypassed
    state=read(RUNTIME/'state.json')
    timer=None
    if state.get('runtime_policy_revision')==REVISION_UNBOUNDED:
        policy=state.get('runtime_policy') or {}
        if (policy.get('authorization_revision',{}).get('revision_id')!=REVISION_UNBOUNDED
                or policy.get('per_call_timeout_mode')!='no_fixed_elapsed_cap_with_inactivity_guard'
                or policy.get('per_job_timeout_mode')!='no_fixed_elapsed_cap_with_inactivity_guard'
                or policy.get('inactivity_stall_seconds')!=3600
                or not cumulative_runtime_cap_bypassed(state,active_profile())):
            raise ValueError('R09 runtime authority integrity mismatch')
        # Cancellation and inactivity remain owned by the existing parent guard.
    else:
        timer=threading.Timer(90,lambda:os._exit(124));timer.daemon=True;timer.start()
    try:
        main()
    finally:
        if timer is not None: timer.cancel()


if __name__=='__main__':
    entrypoint()

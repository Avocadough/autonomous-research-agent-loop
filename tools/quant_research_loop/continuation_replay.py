"""Pinned saved-ledger adapter; never reads prices or selects a strategy."""
import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path
from .gate_zero import ROOT, write
from .prepare import CAMP, digest
from .replay_job import calculate
from .lifecycle import money

BASE = CAMP/'continuations/C01_FIRST_REAL_REPLAY'

def read(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))

def rows(p, compressed=False):
    with (gzip.open(p,'rt',encoding='utf-8-sig',newline='') if compressed else p.open(encoding='utf-8-sig',newline='')) as f:
        return list(csv.DictReader(f))

def evaluate():
    job=read(BASE/'job.json')
    if digest(BASE/'job.json') != read(BASE/'job.sha256.json')['sha256']:
        raise ValueError('Job pin mismatch')
    decision=read(BASE/'lead_before_fix2/accepted.json')
    if decision['action']!='REPLAY_ACCOUNT_POLICY' or decision['next_job']['job_id']!=job['job_id']:
        raise ValueError('No real Lead authorization')
    for name,sha in job['protected_hashes'].items():
        if digest(ROOT/name)!=sha:
            raise ValueError('Protected input or code changed: '+name)
    profile=read(CAMP/'runtime/profile.json')
    ledger=ROOT/next(iter(job['input_hashes']))
    trades=rows(ledger); daily=rows(ledger.with_name('daily.csv'))
    marks=rows(ledger.with_name('marks.csv.gz'),True)
    byday=defaultdict(list); paths=defaultdict(list)
    for r in trades:
        if r['decision']=='trade': byday[r['date']].append(r)
    for r in marks: paths[r['date']].append(r)
    events=[]; seen=set()
    for r in daily:
        d=r['date']; ts=byday[d]; ps=paths[d]
        if d in seen: raise ValueError('Duplicate day')
        seen.add(d)
        net=money(r['net_pnl'])
        if len(ts)!=int(r['trades']) or abs(sum((money(t['net_pnl']) for t in ts),money(0))-net)>money('.01'):
            raise ValueError('Daily/trade mismatch')
        if ts and (not ps or abs(money(ps[-1]['pnl'])-net)>money('.01')):
            raise ValueError('Missing/mismatched saved intraday path')
        trough=min([money(0)]+[money(x['pnl']) for x in ps])
        if abs(trough-money(r['intraday_min_pnl']))>money('.01'):
            raise ValueError('Daily/path minimum mismatch')
        # All ledger positions close by 11 ET; evidence is modeled OHLC, not ticks.
        from datetime import datetime
        flat=all(datetime.fromisoformat(t['exit_time']).strftime('%H:%M')<='11:00' for t in ts)
        events.append(dict(date=d,net=float(net),n_trades=len(ts),max_minis=.1 if ts else 0,
                           min_intraday_net=float(trough),coverage='UNKNOWN',flat=flat,request_allowed=None))
    if set(byday)-seen or set(paths)-seen: raise ValueError('Unmatched ledger day')
    scenarios=[]; result=None
    for sid in decision['next_job']['scenario_ids']:
        rules=read(BASE/'scenarios.json')[sid]
        calc=calculate(job,profile,account_events=events,account_rules=rules)
        if result is None: result=calc
        for path in calc['paths']:
            path['intraday_breach_status']='ASSUMED_OHLC_PATH_SCREENING'
            path['remaining_balance_note']='Last posted close; not a liquidation fill if intraday breach'
        scenarios.append(dict(scenario_id=sid,rules=rules,affordability=calc['structural_budget_block'],paths=calc['paths']))
    result.pop('paths'); result.pop('structural_budget_block')
    result.update(run_id=job['job_id'],scenarios=scenarios,daily_rows=len(daily),intraday_marks=len(marks),
                  lineage=job['protected_hashes'],limitations=[
                      'Consumed train; preserved NQ-as-MNQ proxy, not fresh market evidence',
                      'OHLC assumed adverse-first paths are not observed tick paths or certified MNQ fills',
                      'Feed, roll, timestamp, full calendar, funded scaling, checkout tax and FX unresolved',
                      'No real payouts; dependent fixed-start scenarios are not probabilities'])
    return result

def main():
    out=BASE/'result.json'
    value=evaluate()
    if out.exists():
        if read(out)!=value: raise ValueError('Existing evidence mismatch')
        print('EXISTING_RESULT_REUSED; no new experiment')
    else:
        write(out,value); print('REPLAY_COMPLETED '+str(out))

if __name__=='__main__': main()

"""Allowlisted worker entry point and deterministic replay verification.

No source/price data is loaded. Only the exact preserved ledger in the frozen
job is readable here. Verification recomputation is not a new experiment.
"""
import argparse
import csv
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

from .gate_zero import ROOT, write
from .prepare import CAMP, digest
from .lifecycle import replay, money


def load_job(path):
    path = Path(path).resolve()
    expected = (CAMP/'runtime/jobs/PAYOUT_V1_J001/job.json').resolve()
    if path != expected:
        raise ValueError('Unregistered job path')
    profile = json.loads((CAMP/'runtime/profile.json').read_text(encoding='utf-8'))
    pin = json.loads((CAMP/'runtime/profile.sha256.json').read_text(encoding='utf-8'))
    if digest(CAMP/'runtime/profile.json') != pin['sha256']:
        raise ValueError('Frozen profile changed')
    job = json.loads(path.read_text(encoding='utf-8'))
    if job != profile['jobs'][0]:
        raise ValueError('Job differs from frozen registration')
    for p, expected_hash in job['input_hashes'].items():
        if digest(ROOT/p) != expected_hash:
            raise ValueError('Input ledger changed')
    if digest(CAMP/'runtime/rules.json') != job['account_profile_hash']:
        raise ValueError('Account profile changed')
    if digest(ROOT/job['allowed_paths'][1]) != job['cost_profile_hash']:
        raise ValueError('Original cost contract changed')
    return job, profile


def calculate(job, profile, *, account_events=None, account_rules=None):
    path = ROOT / next(iter(job['input_hashes']))
    with path.open(encoding='utf-8-sig',newline='') as f:
        rows = list(csv.DictReader(f))
    start, end = map(dt.date.fromisoformat, job['eval_interval'])
    daily, months, quarters = defaultdict(list), defaultdict(list), defaultdict(list)
    nets, entries, exits = [], [], []
    for row in rows:
        if row['decision'] != 'trade':
            continue
        day = dt.date.fromisoformat(row['date'])
        if not start <= day <= end:
            raise ValueError('Ledger trade outside partition interval')
        net = money(row['net_pnl'])
        expected = money(row['gross_pnl'])-money(row['commission'])-money(row['slippage_cost'])
        if abs(net-expected) > money('.01'):
            raise ValueError('Ledger net/gross/cost reconciliation failed')
        if int(row['quantity']) != 1:
            raise ValueError('Frozen fixed-one sizing mismatch')
        daily[day].append(net)
        months[day.strftime('%Y-%m')].append(net)
        quarters[f'{day.year}-Q{(day.month-1)//3+1}'].append(net)
        nets.append(net)
        entries.append(dt.datetime.fromisoformat(row['entry_time']).strftime('%H:%M'))
        exits.append(dt.datetime.fromisoformat(row['exit_time']).strftime('%H:%M'))
    monthly = []
    year, month = start.year, start.month
    while (year,month) <= (end.year,end.month):
        key=f'{year:04d}-{month:02d}'
        monthly.append({'month':key,'net':float(sum(months[key])),
                        'partial_boundary': (year,month) in [(start.year,start.month),(end.year,end.month)]})
        month += 1
        if month == 13:
            year,month=year+1,1
    for m in monthly:
        y,mm=map(int,m['month'].split('-'))
        quarters.setdefault(f'{y}-Q{(mm-1)//3+1}',[])
    qnets={k:float(sum(v)) for k,v in sorted(quarters.items())}
    positive=[x for x in qnets.values() if x>0]
    winning=[n for n in nets if n>0]
    losing=[n for n in nets if n<0]
    total=sum(nets)
    first_monday=start-dt.timedelta(days=start.weekday())
    last_monday=end-dt.timedelta(days=end.weekday())
    weeks=(last_monday-first_monday).days//7+1
    metrics=dict(trades=len(nets), win_rate_pct=100*len(winning)/len(nets) if nets else None,
                 planned_risk_reward='1:2 target with structural stop; time exits may differ',
                 realized_payoff=float((sum(winning)/len(winning))/abs(sum(losing)/len(losing))) if winning and losing else None,
                 realized_payoff_definition='average winning net-dollar trade / absolute average losing net-dollar trade',
                 net_pnl=float(total), basis='after modeled commission and slippage, preserved NQ-as-MNQ proxy',
                 partition='consumed train', evaluation_start=str(start),evaluation_end=str(end),
                 calendar_weeks=weeks, calendar_week_convention='Mon-Sun; includes zero-trade weeks and partial boundary weeks',
                 trades_per_calendar_week=len(nets)/weeks,
                 observed_entry_window_ET=[min(entries),max(entries)] if entries else None,
                 observed_exit_window_ET=[min(exits),max(exits)] if exits else None,
                 timezone='America/New_York DST-aware; observed ledger windows, not revised rules',
                 monthly_net=monthly,quarterly_trading_net=qnets,
                 mean_monthly_return=float(total)/(profile['fixed_nominal_reference']*len(monthly)),
                 monthly_return_denominator=profile['fixed_nominal_reference']*len(monthly),
                 zero_trade_months_included=True,
                 positive_quarter_concentration=max(positive)/sum(positive) if positive else None,
                 net_excluding_best_quarter=float(total)-max(qnets.values()) if qnets else None,
                 concentration_user_approved_cutoff=None, concentration_pass='UNKNOWN',
                 historical_frequency_is_not_future_guarantee=True)
    rules=account_rules if account_rules is not None else json.loads((CAMP/'runtime/rules.json').read_text(encoding='utf-8'))
    events=account_events if account_events is not None else [dict(date=str(d),net=float(sum(v)),n_trades=len(v),max_minis=.1,
                 coverage='UNKNOWN',flat=None,min_intraday_net=None,request_allowed=None)
            for d,v in sorted(daily.items())]
    paths=[]
    for s in profile['starts']:
        e=str(dt.date.fromisoformat(s)+dt.timedelta(days=profile['horizon_calendar_days']-1))
        paths.append(replay(events,rules,start=s,end=e,budget=profile['external_cash_budget']))
    return dict(campaign_id='PAYOUT_V1', evidence_id=job['job_id'],
                experiment_status='VALID_SAVED_LEDGER_SCREENING', market_edge_status='NOT_REEVALUATED',
                metrics=metrics,paths=paths,ledger_sha256=digest(path),
                account_profile_sha256=job['account_profile_hash'],
                evaluator_sha256=digest(Path(__file__).with_name('lifecycle.py')),
                actual_cash_received=0, ready_for_forward=False,
                monthly_target_met=metrics['mean_monthly_return']>=profile['monthly_target'],
                payout_target_met=False,
                structural_budget_block={'minimum_base_cost_to_funded':rules['monthly']+rules['activation'],
                    'budget':profile['external_cash_budget'],'taxes':'UNKNOWN',
                    'funded_activation_possible': rules['monthly']+rules['activation']<=profile['external_cash_budget']},
                limitations=['Saved-ledger replay is not a new entry/exit trial or fresh OOS',
                             'Unknown intraday path, feed/roll/timestamp semantics, calendar, and MNQ execution',
                             'No observed cash payouts; scenario outcomes are dependent, not probabilities'])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('job')
    args=parser.parse_args()
    job,profile=load_job(args.job)
    result=calculate(job,profile)
    out=ROOT/job['outputs'][0]
    if args.verify:
        if json.loads(out.read_text(encoding='utf-8')) != result:
            raise ValueError('Persisted result mismatch')
        print('VERIFIED: arithmetic, ledger identity, frozen account profile, deterministic replay')
    else:
        if out.exists():
            if json.loads(out.read_text(encoding='utf-8')) != result:
                raise ValueError('Existing result conflict; do not overwrite')
            print('EXISTING_RESULT_REUSED; no new trial')
        else:
            write(out,result)
            print('REPLAY_COMPLETED '+out.as_posix())


if __name__=='__main__':
    main()

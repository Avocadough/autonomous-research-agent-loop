"""R03 profile and saved-ledger adapter for the existing PAYOUT controller.

No price loader, model transport, or replacement action framework. New account
policies are evaluated against pinned existing ledgers; historical files remain
immutable and their consumed-data status follows them into every result.
"""
import calendar
import csv
import datetime as dt
import json
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from .gate_zero import ROOT, write
from .prepare import CAMP, digest
from .lifecycle import replay, anniversary, money

REV = CAMP / 'revisions/R03_PAYOUT_FIRST_20260921'
ACT = REV / 'activation'
RUNTIME = CAMP / 'runtime'
LEAN_REVISION = 'R27_LEAN_PAYOUT_DECISION_20261005'
LEAN_AUTHORIZATION = Path('research/campaigns/PAYOUT_V1/revisions') / LEAN_REVISION / 'AUTHORIZATION.json'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def lean_payout_authority(state):
    """Validate the prospective R27 policy without granting dispatch or resume."""
    ref = state.get('lean_payout_authority')
    if not ref:
        return None
    if ref.get('revision_id') != LEAN_REVISION or ref.get('path') != LEAN_AUTHORIZATION.as_posix():
        raise ValueError('Lean payout authority identity mismatch')
    path = (ROOT / ref['path']).resolve()
    if not path.is_relative_to(ROOT.resolve()) or digest(path) != ref.get('sha256'):
        raise ValueError('Lean payout authority hash mismatch')
    value = read(path)
    expected = dict(revision_id=LEAN_REVISION, evaluation_months=1, funded_months=6,
                    total_months=7, cash_target_usd=1000, buffer_target_usd=2000,
                    prop_budget_usd=300, economic_floor_usd=500, frequency_advisory=True,
                    shadow_review_days=28, skill_advisory_failure_deferral=True,
                    global_call_cap=1000, spend_usd=0, live='DISABLED', counter_reset=False,
                    historical_receipts_immutable=True, independent_qa_required=True,
                    new_worker_allowance=False, market_rerun_authorized=False)
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise ValueError('Lean payout policy or permission mismatch')
    started = dt.datetime.fromisoformat(value['started_at_utc'])
    due = dt.datetime.fromisoformat(value['decision_due_utc'])
    if (started.utcoffset() != dt.timedelta(0) or due.utcoffset() != dt.timedelta(0)
            or due - started != dt.timedelta(days=1)):
        raise ValueError('Lean payout decision deadline mismatch')
    return value


def lean_payout_profile(state, effective):
    """Return future policy only; never mutate a frozen profile or saved job."""
    auth = lean_payout_authority(state)
    if auth is None:
        raise ValueError('Lean payout profile requires pinned R27 authority')
    result = deepcopy(effective)
    result['targets'].update(evaluation_deadline_calendar_months=auth['evaluation_months'],
        funded_to_first_cash_receipt_deadline_calendar_months=auth['funded_months'],
        total_deadline_calendar_months=auth['total_months'],
        arithmetic_mean_monthly_net_trading_pnl_usd=auth['economic_floor_usd'])
    result['strategy']['frequency_advisory'] = auth['frequency_advisory']
    result['shadow_review_days'] = auth['shadow_review_days']
    result['lean_payout_revision'] = LEAN_REVISION
    return result


def lean_payout_deadline_elapsed(state, now=None):
    auth = lean_payout_authority(state)
    if auth is None:
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    return now >= dt.datetime.fromisoformat(auth['decision_due_utc'])


def lean_payout_job_profile(job, profile):
    """Opt in only a separately registered job with an exact prospective pin."""
    ref = job.get('lean_payout_profile')
    if ref is None:
        return profile
    path = (ROOT / ref['path']).resolve()
    expected = (ROOT / LEAN_AUTHORIZATION.parent / 'EFFECTIVE_PROFILE.json').resolve()
    if path != expected or digest(path) != ref.get('sha256'):
        raise ValueError('Lean payout prospective profile hash/identity mismatch')
    state = read(RUNTIME / 'state.json')
    effective = lean_payout_profile(state, profile['effective_profile'])
    if read(path) != effective:
        raise ValueError('Lean payout prospective profile differs from pinned authority')
    result = deepcopy(profile)
    result.update(effective_profile=effective, effective_profile_path=ref['path'],
                  effective_profile_sha256=ref['sha256'])
    return result


def active_profile():
    pointer = read(RUNTIME / 'active_profile.json')
    path = ROOT / pointer['path']
    if digest(path) != pointer['sha256']:
        raise ValueError('Active profile hash mismatch')
    p = read(path)
    if digest(ROOT / p['effective_profile_path']) != p['effective_profile_sha256']:
        raise ValueError('R03 effective mandate/profile mismatch')
    if read(ROOT / p['effective_profile_path']) != p['effective_profile']:
        raise ValueError('R03 embedded profile differs from pinned profile')
    return p


def accounted_seconds(state):
    a = state.get('r03_activation')
    if not a:
        return state['active_invocation_s'] + state['prior_reserved_seconds']
    elapsed = a.get('elapsed_wall_seconds', 0)
    if a.get('active_epoch_utc'):
        elapsed += max(0, (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(a['active_epoch_utc'])).total_seconds())
    # Deliberately conservative: child time within operator wall time may be
    # charged twice. Prior reservations are never released or reset.
    return a['prior_accounted_seconds'] + elapsed + max(0, state['active_invocation_s'] - a['child_seconds_at_start'])


def activation_authorized(state, profile):
    a = state.get('r03_activation', {})
    if profile.get('mandate_revision') != REV.name or not a:
        return False
    authorization = read(ROOT / a['authorization_path'])
    if (authorization.get('approved') is not True or authorization.get('max_correction_sets') != 2
            or a.get('max_sets') != 2 or not 0 < a.get('sets_used', 0) <= 2
            or authorization.get('global_max_calls') != 80 or authorization.get('global_max_seconds') != 21600):
        return False
    if digest(ACT / 'AUTHORIZATION.md') != authorization['authorization_sha256']:
        return False
    return a.get('status') in ('ACTIVATING', 'ACTIVE', 'CORRECTING')


def csv_rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def verified_job(path):
    path = Path(path).resolve()
    profile = active_profile()
    jobs = {j['job_id']: j for j in profile['jobs']}
    job = read(path)
    expected = RUNTIME / 'jobs' / job['job_id'] / 'job.json'
    if path != expected.resolve() or jobs.get(job['job_id']) != job:
        raise ValueError('Unregistered R03 job')
    if job['effective_profile_sha256'] != profile['effective_profile_sha256']:
        raise ValueError('Job/evaluator/controller revision mismatch')
    for rel, sha in job['input_hashes'].items():
        if not rel.startswith('research/runs/B017_20260914T161251Z/') or '..' in Path(rel).parts:
            raise ValueError('R03 saved-ledger path outside exact source allowlist')
        if digest(ROOT / rel) != sha:
            raise ValueError('Pinned ledger changed: ' + rel)
    for rel, sha in job['code_hashes'].items():
        if digest(ROOT / rel) != sha:
            raise ValueError('Frozen R03 evaluator source changed: ' + rel)
    if digest(ROOT / profile['rules_path']) != job['account_profile_hash']:
        raise ValueError('R03 account-rule pin mismatch')
    return job, profile


def ledger_metrics(trades, daily, marks, start, end):
    start, end = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    nets = [money(t['net_pnl']) for t in trades]
    wins, losses = [n for n in nets if n > 0], [n for n in nets if n < 0]
    daily_map = {r['date']: r for r in daily}
    if len(daily_map) != len(daily):
        raise ValueError('Duplicate saved day')
    byday, paths = defaultdict(list), defaultdict(list)
    for row in trades:
        day = dt.date.fromisoformat(row['date'])
        if not start <= day <= end:
            raise ValueError('Trade outside frozen interval')
        qty = int(row['contracts'])
        if qty <= 0 or qty > 50:
            raise ValueError('Invalid micro quantity')
        if dt.datetime.fromisoformat(row['exit_time']) < dt.datetime.fromisoformat(row['entry_time']):
            raise ValueError('Exit precedes entry')
        # Entry/exit already incorporate modeled shortfall. Never deduct it a
        # second time. Independent Decimal arithmetic checks each cash row.
        expected = money((Decimal(row['exit']) - Decimal(row['entry'])) * int(row['direction']) * 2 * qty - Decimal(row['commission']))
        if abs(expected - money(row['net_pnl'])) > money('.01'):
            raise ValueError('Trade-level independent cash reconciliation failed')
        byday[row['date']].append(row)
    for row in marks:
        paths[row['date']].append(row)
    if set(byday) - set(daily_map) or set(paths) - set(daily_map):
        raise ValueError('Unmatched saved day/path')
    events = []
    cumulative = peak = marked_peak = dd = marked_dd = money(0)
    for day, row in sorted(daily_map.items()):
        ds = byday[day]
        net = money(row['net_pnl'])
        if len(ds) != int(row['trades']) or abs(sum((money(t['net_pnl']) for t in ds), money(0)) - net) > money('.01'):
            raise ValueError('Daily/trade reconciliation failed')
        ps = sorted(paths[day], key=lambda x: int(x['i']))
        if ds and (not ps or abs(money(ps[-1]['day_pnl']) - net) > money('.01')):
            raise ValueError('Missing/mismatched minute-close path')
        for r in ps:
            equity = cumulative + money(r['day_pnl'])
            marked_peak = max(marked_peak, equity)
            marked_dd = max(marked_dd, marked_peak - equity)
        cumulative += net
        peak = max(peak, cumulative)
        dd = max(dd, peak - cumulative)
        trough = min([money(0)] + [money(r['day_pnl']) for r in ps])
        events.append(dict(date=day, net=float(net), n_trades=len(ds),
                           max_minis=max([int(t['contracts']) / 10 for t in ds] or [0]),
                           min_intraday_net=float(trough), coverage='UNKNOWN',
                           flat=all(dt.datetime.fromisoformat(t['exit_time']).strftime('%H:%M') <= '15:55' for t in ds),
                           request_allowed=None))
    monthly = []
    cursor = start.replace(day=1)
    while cursor <= end:
        last = cursor.replace(day=calendar.monthrange(cursor.year, cursor.month)[1])
        values = [money(t['net_pnl']) for t in trades if t['date'][:7] == cursor.strftime('%Y-%m')]
        monthly.append(dict(month=cursor.strftime('%Y-%m'), net=float(sum(values, money(0))),
                            partial_boundary=start > cursor or end < last,
                            coverage='SAVED_OBSERVED_SESSIONS_ONLY; feed/calendar completeness unverified'))
        cursor = anniversary(cursor, 1)
    quarters = []
    cursor = start.replace(month=((start.month - 1)//3)*3 + 1, day=1)
    while cursor <= end:
        last = anniversary(cursor, 3) - dt.timedelta(days=1)
        qnet = sum((money(t['net_pnl']) for t in trades if str(cursor) <= t['date'] <= str(last)), money(0))
        quarters.append(dict(quarter=f'{cursor.year}-Q{(cursor.month-1)//3+1}', net=float(qnet),
                             partial_boundary=start > cursor or end < last))
        cursor = anniversary(cursor, 3)
    weeks = (end - dt.timedelta(days=end.weekday()) - (start - dt.timedelta(days=start.weekday()))).days//7 + 1
    weekly = defaultdict(int)
    for t in trades:
        day = dt.date.fromisoformat(t['date'])
        weekly[str(day - dt.timedelta(days=day.weekday()))] += 1
    zero_weeks = longest = streak = 0
    for n in range(weeks):
        week = str(start - dt.timedelta(days=start.weekday()) + dt.timedelta(weeks=n))
        if weekly[week] == 0:
            zero_weeks += 1; streak += 1; longest = max(longest, streak)
        else:
            streak = 0
    loss_streak = current = 0
    closed_peak = closed_dd = closed_equity = money(0)
    for n in nets:
        current = current + 1 if n < 0 else 0
        loss_streak = max(loss_streak, current)
        closed_equity += n; closed_peak = max(closed_peak, closed_equity)
        closed_dd = max(closed_dd, closed_peak - closed_equity)
    full = [m['net'] for m in monthly if not m['partial_boundary']]
    positive_q = [q['net'] for q in quarters if q['net'] > 0]
    metrics = dict(trades=len(nets), resolved_trades=len(nets), unresolved_trades=0,
        wins=len(wins), losses=len(losses), breakeven=len(nets)-len(wins)-len(losses),
        win_rate_pct=100*len(wins)/len(nets) if nets else None,
        planned_risk_reward='N/A: trailing/structural stop and time exit, no fixed target',
        realized_payoff=float((sum(wins)/len(wins))/abs(sum(losses)/len(losses))) if wins and losses else None,
        realized_payoff_definition='average winning net-dollar trade / absolute average losing net-dollar trade',
        net_pnl=float(sum(nets, money(0))), commission=float(sum((money(t['commission']) for t in trades), money(0))),
        mean_monthly_net_full_calendar_months=sum(full)/len(full) if full else None,
        mean_monthly_net_including_partial=sum(m['net'] for m in monthly)/len(monthly),
        complete_calendar_months=len(full), monthly=monthly, quarterly=quarters,
        positive_quarters=sum(q['net'] > 0 for q in quarters),
        complete_quarters=sum(not q['partial_boundary'] for q in quarters),
        positive_quarter_concentration=max(positive_q)/sum(positive_q) if positive_q else None,
        net_excluding_best_quarter=float(sum(nets, money(0)))-max(q['net'] for q in quarters),
        max_drawdown_closed_trades=float(closed_dd), max_drawdown_daily_close=float(dd),
        max_drawdown_saved_minute_close=float(marked_dd), intrabar_drawdown_certified=False,
        losing_streak=loss_streak, calendar_weeks=weeks, trades_per_calendar_week=len(nets)/weeks,
        zero_trade_weeks=zero_weeks, longest_zero_calendar_week_streak=longest,
        calendar_week_basis='Mon-Sun, all intersecting weeks including zero and partial boundary weeks; missing sessions disclosed separately',
        evaluation_start=str(start), evaluation_end=str(end), partition='CONSUMED_2026_DEVELOPMENT_PROXY',
        intraday_entry_window='10:00-15:30, every 30 minutes', mandatory_exit='15:55',
        timezone='America/New_York; Thai +11h during EDT and +12h during EST',
        max_initial_risk=max([float(t['initial_fill_risk']) for t in trades] or [0]),
        worst_net_trade=float(min(nets)) if nets else None,
        basis='After preserved modeled commission $1.22/RT/MNQ and scenario execution prices; no second slippage deduction; current executable costs unverified')
    return metrics, events


def qualify_metrics(metrics, effective):
    target = effective['targets']['arithmetic_mean_monthly_net_trading_pnl_usd']
    mean = metrics['mean_monthly_net_full_calendar_months']
    return dict(monthly_target_usd=target, target_gap_usd=None if mean is None else max(0, target-mean),
        monthly_target_met=None if mean is None else mean >= target,
        quarterly_gate='INSUFFICIENT_COVERAGE' if metrics['complete_quarters'] < 4 else ('PASS_POSITIVE_COUNT_ONLY' if metrics['positive_quarters'] >= 2 else 'FAIL'),
        concentration_gate='LEAD_REVIEW_REQUIRED_NO_USER_NUMERIC_CUTOFF',
        frequency_met=metrics['trades_per_calendar_week'] >= effective['strategy']['min_trades_per_calendar_week'],
        no_trade_calendar_week_gate=metrics['longest_zero_calendar_week_streak'] <= effective['strategy']['max_no_trade_span_weeks'],
        **({'frequency_gate_binding':False} if effective['strategy'].get('frequency_advisory') else {}),
        max_no_trade_span_certified=False,
        risk_screen_met=metrics['max_initial_risk'] <= effective['risk']['per_trade_usd_ceiling'] and metrics['max_drawdown_saved_minute_close'] <= effective['risk']['max_drawdown_usd_ceiling'],
        classification='DEVELOPMENT_SCREEN_ONLY', full_target_met=False, ready_for_forward=False)


def calculate(job, profile):
    profile = lean_payout_job_profile(job, profile)
    effective = profile['effective_profile']
    rules = read(ROOT / profile['rules_path'])
    scenarios = []
    policy = dict(evaluation_months=effective['targets']['evaluation_deadline_calendar_months'],
                  funded_months=effective['targets']['funded_to_first_cash_receipt_deadline_calendar_months'],
                  total_months=effective['targets']['total_deadline_calendar_months'],
                  cash_target=effective['targets']['first_payout_cash_received_usd'],
                  buffer_target=effective['targets']['post_withdrawal_buffer_usd'],
                  daily_loss_ceiling=effective['risk']['daily_loss_usd_ceiling'])
    for case in job['cases']:
        folder = ROOT / case['path']
        metrics, events = ledger_metrics(csv_rows(folder/'trades.csv'), csv_rows(folder/'daily.csv'),
                                         csv_rows(folder/'marks.csv'), *case['eval_interval'])
        paths = []
        for start in case['starts']:
            desired_end = anniversary(dt.date.fromisoformat(start), policy['total_months']) - dt.timedelta(days=1)
            observed_end = min(desired_end, dt.date.fromisoformat(case['eval_interval'][1]))
            path = replay(events, rules, start=start, end=str(observed_end),
                          budget=effective['cash_budget']['total_all_accounts_usd'], policy=policy)
            path.update(planned_horizon_end=str(desired_end), data_censored=observed_end < desired_end,
                        source_intraday_path='SAVED_MINUTE_CLOSE_PROXY; not intrabar fills',
                        historical_rate_is_not_probability=True)
            paths.append(path)
        scenarios.append(dict(case=case, metrics=metrics, gates=qualify_metrics(metrics,effective), paths=paths))
    return dict(campaign_id='PAYOUT_V1', evidence_id=job['job_id'], run_id=job['job_id'],
                mandate_revision=profile['mandate_revision'], effective_profile_sha256=profile['effective_profile_sha256'],
                experiment_status='VALID_SAVED_LEDGER_ACCOUNT_POLICY_SCREEN',
                new_entry_exit_backtest=False, new_account_policy_evaluation=True,
                consumed_not_fresh_OOS=True, all_source_cost_timestamp_cases_retained=True,
                scenarios=scenarios, account_profile_sha256=job['account_profile_hash'],
                input_hashes=job['input_hashes'], code_hashes=job['code_hashes'],
                actual_cash_received=0, ready_for_forward=False, full_target_met=False,
                limitations=['Two overlapping source revisions are separate, not pooled independent samples',
                             'No trade deletion, re-sizing, new price read or invented intraday liquidation',
                             'Minute-close marks understate possible intrabar risk; saved source feed/roll/time/fills unresolved',
                             'Funding scaling, checkout tax, transfer timing and bank/FX fees may block individual payout claims',
                             'One-attempt baseline uses at most one failure and one account; does not claim three-attempt success odds',
                             'Failed and incomplete starts retained; no winner-only speed metric'])

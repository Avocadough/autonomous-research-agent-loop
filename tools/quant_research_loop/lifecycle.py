"""Decimal, chronological account/cash evaluator. Never places orders.

The original payout.py is retained as a superseded prototype, not imported for
account arithmetic. Values come from an explicit hashed rule/scenario profile.
Daily minimums are cumulative realized+open PnL relative to start of day.
Missing paths, calendars, and costs remain unknown; cash is always modeled.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING


def money(x):
    value = Decimal(str(x))
    if not value.is_finite():
        raise ValueError("Non-finite amount")
    return value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def anniversary(start, n):
    year, month = divmod(start.year * 12 + start.month - 1 + n, 12)
    return date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1]))


def replay(days, rules, *, start, end, budget=120, synthetic=False, operating_cost=0,
           policy=None):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    policy = policy or {}
    evaluation_deadline = (anniversary(start, policy['evaluation_months'])
                           if 'evaluation_months' in policy else None)
    total_deadline = (anniversary(start, policy['total_months'])
                     if 'total_months' in policy else None)
    if end < start or budget < 0 or operating_cost < 0 or not rules.get('rules_hash'):
        raise ValueError('Invalid horizon/budget/unpinned rules')
    rows = {}
    for row in days:
        d = date.fromisoformat(row['date'])
        if d in rows:
            raise ValueError('Duplicate daily event')
        money(row['net'])
        rows[d] = row
    balance, floor = money(50000), money(50000 - rules['mll'])
    spent, cash = money(0), money(0)
    total, best, cycle_profit = money(0), money(0), money(0)
    costs, payouts, transitions = [], [], []
    unknowns = set(rules.get('unknowns', []))
    if not synthetic:
        unknowns.update(['checkout_tax_unknown', 'bank_FX_personal_taxes_unknown',
                         'allocated_subscription_economic_cost_unknown'])
    status, stage = 'INCOMPLETE', 'EVALUATION'
    wins = traded = 0
    month = 1
    passed = failed = first_payout = last_trade = None

    def charge(d, kind, amount):
        nonlocal spent
        amount = money(amount)
        if amount < 0:
            raise ValueError('Negative cash charge')
        if spent + amount > money(budget):
            transitions.append({'date': str(d), 'event': 'CASH_BUDGET_BLOCK',
                                'needed': float(amount), 'remaining': float(money(budget) - spent)})
            return False
        spent += amount
        costs.append({'date': str(d), 'kind': kind, 'amount': float(amount)})
        return True

    if not charge(start, 'operating', operating_cost) or not charge(start, 'evaluation', rules['monthly']):
        status = 'CASH_BUDGET_BLOCKED'
    day = start
    while day <= end and status == 'INCOMPLETE':
        # Calendar deadlines are exclusive anniversary boundaries. Missing days
        # do not stop the clock, and a renewal cannot extend the declared goal.
        if total_deadline and day >= total_deadline:
            status = 'HORIZON_COMPLETE' if first_payout else 'PAYOUT_DEADLINE_MISSED'
            break
        if stage == 'EVALUATION' and evaluation_deadline and day >= evaluation_deadline:
            status = 'EVALUATION_DEADLINE_MISSED'
            break
        if stage == 'FUNDED' and not first_payout and policy.get('funded_months') and day >= anniversary(date.fromisoformat(passed), policy['funded_months']):
            status = 'PAYOUT_DEADLINE_MISSED'
            break
        if stage == 'EVALUATION' and day == anniversary(start, month):
            if not charge(day, 'renewal', rules['monthly']):
                status = 'CASH_BUDGET_BLOCKED'
                break
            month += 1
        row = rows.get(day)
        if row is None:
            unknowns.add('calendar_coverage_unknown')
            day += timedelta(days=1)
            continue
        if row.get('coverage') != 'COMPLETE':
            unknowns.add('data_coverage_unknown')
        net, count = money(row['net']), int(row['n_trades'])
        if count < 0 or (count == 0 and net != 0):
            raise ValueError('PnL/trade count mismatch')
        limit = rules['combine_max_minis']
        if stage == 'FUNDED':
            if rules.get('scaling') is None:
                status = 'RULES_UNCERTAIN'
                unknowns.add('funded_scaling_unknown')
                break
            limit = max((q for b, q in rules['scaling'] if balance >= money(b)), default=0)
            if last_trade and (day - last_trade).days > 30:
                status = 'RULES_UNCERTAIN'
                unknowns.add('discretionary_inactivity_closure')
                break
        qty = row.get('max_minis')
        if qty is None:
            unknowns.add('position_limit_unverified')
        elif qty > limit:
            status, failed = 'POSITION_LIMIT_VIOLATION', str(day)
            break
        if row.get('flat') is False:
            status, failed = 'FLATTEN_VIOLATION', str(day)
            break
        if row.get('flat') is not True:
            unknowns.add('flatten_time_unverified')
        trough = row.get('min_intraday_net')
        if trough is None:
            unknowns.add('intraday_path_unknown')
        else:
            trough = money(trough)
            if trough > min(money(0), net):
                raise ValueError('Minimum equity exceeds start/close')
            if balance + trough <= floor:
                status, failed = 'MLL_FAILED', str(day)
                transitions.append({'date': str(day), 'event': status, 'stage': stage,
                                    'floor': float(floor), 'equity': float(balance + trough)})
                break
            if rules.get('dll') is not None and trough <= -money(rules['dll']):
                status = 'UNKNOWN_DLL_LIQUIDATION_PATH'
                unknowns.add('missing_firm_liquidation_fill; not_strategy_stop')
                break
            if policy.get('daily_loss_ceiling') is not None and trough <= -money(policy['daily_loss_ceiling']):
                status = 'UNKNOWN_DAILY_STOP_LIQUIDATION_PATH'
                unknowns.add('daily_screen_ceiling_reached; no invented liquidation or post-entry deletion')
                break
        balance += net
        if balance <= floor:
            status, failed = 'MLL_FAILED', str(day)
            break
        if count:
            last_trade = day
        cap = money(50000 if stage == 'EVALUATION' else 0)
        floor = max(floor, min(cap, balance - money(rules['mll'])))
        if stage == 'EVALUATION':
            total += net
            best = max(best, net)
            traded += bool(count)
            target = max(money(rules['target']), best / Decimal(str(rules['consistency'])))
            if total >= target and traded >= rules['min_days']:
                passed = str(day)
                transitions.append({'date': passed, 'event': 'MODELED_PASS',
                                    'evaluation_profit_discarded': float(total)})
                if not charge(day, 'activation', rules['activation']):
                    status = 'ACTIVATION_BUDGET_BLOCKED'
                    break
                stage, balance, floor = 'FUNDED', money(0), -money(rules['mll'])
                last_trade = day
                transitions.append({'date': str(day), 'event': 'FUNDED_RESET', 'balance': 0})
        else:
            cycle_profit += net
            if count and net >= money(rules['winning_day']):
                wins += 1
            eligible = wins >= rules['winning_days'] and (not payouts or cycle_profit >= money(.01))
            if eligible and row.get('request_allowed') is not True:
                unknowns.add('payout_request_calendar_unknown')
            elif eligible:
                request = min(money(rules['payout_cap']), money(balance * Decimal(str(rules['payout_ratio']))))
                if policy.get('cash_target') is not None:
                    # Withdraw the smallest cent amount meeting the cash goal;
                    # do not use an early small payout or consume the buffer.
                    required = ((money(policy['cash_target']) + money(rules['withdrawal_fee'])) /
                                Decimal(str(rules['trader_split']))).quantize(Decimal('.01'), rounding=ROUND_CEILING)
                    allowed = min(request, balance - money(policy['buffer_target']))
                    request = required if allowed >= required else money(0)
                if request >= money(rules['min_payout']):
                    received = money(request * Decimal(str(rules['trader_split']))) - money(rules['withdrawal_fee'])
                    payouts.append({'date': str(day), 'eligible': True, 'requested': float(request),
                                    'modeled_approved': float(request), 'approval_is_assumed': True,
                                    'modeled_cash_after_split_and_fee': float(received),
                                    'withdrawal_fee': rules['withdrawal_fee'], 'actual_cash_received': 0})
                    cash += received
                    balance -= request
                    floor, wins, cycle_profit = money(0), 0, money(0)
                    first_payout = first_payout or day
                    if policy:
                        payouts[-1]['post_withdrawal_buffer'] = float(balance - floor)
                        payouts[-1]['cash_receipt_date'] = None
                        unknowns.add('cash_receipt_delay_and_approval_unknown; request_date_is_not_receipt_date')
                    if balance <= floor:
                        status, failed = 'POST_WITHDRAWAL_MLL_FAILED', str(day)
        day += timedelta(days=1)
    if policy and status == 'INCOMPLETE' and not first_payout:
        deadline = evaluation_deadline if stage == 'EVALUATION' else min(total_deadline, anniversary(date.fromisoformat(passed), policy['funded_months']))
        if end >= deadline - timedelta(days=1):
            status = 'EVALUATION_DEADLINE_MISSED' if stage == 'EVALUATION' else 'PAYOUT_DEADLINE_MISSED'
    result = {'status': status, 'stage': stage, 'start': str(start), 'end': str(end),
            'passed_on': passed, 'failed_on': failed, 'external_cash_spent': float(spent),
            'external_budget': budget, 'remaining_balance': float(balance), 'remaining_floor': float(floor),
            'modeled_cash_received': float(cash), 'actual_cash_received': 0,
            'net_cash_known_costs_only': float(cash - spent),
            'net_cash_all_costs': None if unknowns else float(cash - spent),
            'days_to_first_payout': (first_payout - start).days if first_payout else None,
            'post_withdrawal_survival': None if not payouts or unknowns else failed is None,
            'payouts': payouts, 'cost_events': costs, 'transitions': transitions,
            'unknowns': sorted(unknowns), 'synthetic': synthetic, 'rules_hash': rules['rules_hash'],
            'certified_payout_feasible': False, 'screening_only': True,
            'intraday_breach_status': 'UNKNOWN' if 'intraday_path_unknown' in unknowns else 'OBSERVED_TRACE_ONLY',
            'one_attempt_only': True}
    if policy:
        result.update(policy=policy, account_failures=int(failed is not None),
                      attempts=1, retry_policy='ONE_ATTEMPT; no healthy-account abandonment or reset',
                      costs_before_first_request=sum(x['amount'] for x in costs if not first_payout or date.fromisoformat(x['date']) <= first_payout),
                      days_to_first_payout_request=result['days_to_first_payout'],
                      days_to_first_cash_receipt=None, cash_receipt_deadline_met=None,
                      payout_target_eligibility_met=bool(payouts), actual_cash_received=0,
                      cash_received_label='CONDITIONAL_MODELED_AMOUNT; receipt date and approval unverified')
    return result

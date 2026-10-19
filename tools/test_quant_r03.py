import copy
import datetime as dt
import unittest
from unittest.mock import patch

from quant_research_loop.controller import Controller, completion_export
from tools.agent_loop.runtime_outcome import classify
from quant_research_loop.r03 import accounted_seconds, ledger_metrics, qualify_metrics
from quant_research_loop.lifecycle import replay
from test_quant_lifecycle import RULES, days


POLICY = dict(evaluation_months=1, funded_months=3, total_months=4,
              cash_target=1000, buffer_target=2000, daily_loss_ceiling=1000)
R03_RULES = {**RULES, 'monthly':95, 'activation':0, 'scaling':[[-2000,2],[1500,3]]}


def run(values, **kwargs):
    return replay(days(values), R03_RULES, start='2024-01-01',
                  end=kwargs.pop('end', str(dt.date(2024,1,1)+dt.timedelta(days=len(values)-1))),
                  budget=300, synthetic=True, policy=POLICY, **kwargs)


class R03PolicyTests(unittest.TestCase):
    def test_first_small_eligible_payout_is_not_goal(self):
        r=run([1500,1500]+[200]*5)
        self.assertEqual(r['payouts'],[])
        self.assertEqual(r['remaining_balance'],1000)
        self.assertFalse(r['payout_target_eligibility_met'])

    def test_cash_split_fee_buffer_independent_arithmetic(self):
        r=run([1500,1500]+[650]*5)
        self.assertEqual(len(r['payouts']),1)
        self.assertEqual(r['payouts'][0]['requested'],1144.45)
        self.assertEqual(r['modeled_cash_received'],1000.01)
        self.assertEqual(r['net_cash_known_costs_only'],905.01)
        self.assertEqual(r['payouts'][0]['post_withdrawal_buffer'],2105.55)
        self.assertEqual(r['external_cash_spent'],95)
        self.assertIsNone(r['days_to_first_cash_receipt'])
        self.assertIsNone(r['cash_receipt_deadline_met'])
        self.assertEqual(r['actual_cash_received'],0)

    def test_payout_cannot_consume_buffer(self):
        r=run([1500,1500]+[600]*5)
        self.assertEqual(r['remaining_balance'],3000)
        self.assertEqual(r['payouts'],[])

    def test_evaluation_deadline_not_subscription_renewal(self):
        r=run([0]*40)
        self.assertEqual(r['status'],'EVALUATION_DEADLINE_MISSED')
        self.assertEqual(r['external_cash_spent'],95)
        self.assertFalse(any(x['kind']=='renewal' for x in r['cost_events']))
        self.assertEqual(r['account_failures'],0)

    def test_end_on_last_evaluation_day_is_complete_failure(self):
        r=run([0]*31)
        self.assertEqual(r['status'],'EVALUATION_DEADLINE_MISSED')

    def test_missing_data_does_not_make_early_horizon_complete(self):
        r=run([0]*10)
        self.assertEqual(r['status'],'INCOMPLETE')
        self.assertFalse(r['certified_payout_feasible'])

    def test_daily_pause_is_not_failed_account_or_fabricated_fill(self):
        r=run([-1000,2000])
        self.assertEqual(r['status'],'UNKNOWN_DAILY_STOP_LIQUIDATION_PATH')
        self.assertEqual(r['account_failures'],0)
        self.assertEqual(r['remaining_balance'],50000)
        self.assertEqual(r['modeled_cash_received'],0)

    def test_mll_first_and_unrealized_loss_cannot_be_hidden_by_close(self):
        rows=days([1000]);rows[0]['min_intraday_net']=-2000
        r=replay(rows,R03_RULES,start='2024-01-01',end='2024-01-01',budget=300,synthetic=True,policy=POLICY)
        self.assertEqual(r['status'],'MLL_FAILED')
        self.assertEqual(r['account_failures'],1)

    def test_evaluation_profits_never_funded_cash(self):
        r=run([1500,1500])
        self.assertEqual(r['remaining_balance'],0)
        self.assertEqual(r['modeled_cash_received'],0)
        self.assertEqual(r['transitions'][0]['evaluation_profit_discarded'],3000)


class R03BudgetTests(unittest.TestCase):
    def controller(self):
        c=object.__new__(Controller)
        c.profile=dict(max_calls=80,max_active_seconds=21600,max_configs=3000,max_integration_fixes=3)
        c.state=dict(model_calls=13,bootstrap_invocations=5,prior_reserved_calls=10,
                     active_invocation_s=392.515,prior_reserved_seconds=3600,trials=1,integration_fix_cycles=3)
        c.save=lambda:None
        return c

    def test_explicit_r03_allowance_does_not_reset_old_counter(self):
        c=self.controller()
        with patch('quant_research_loop.r03.activation_authorized',return_value=True):
            c.check_budget()
        self.assertEqual(c.state['integration_fix_cycles'],3)

    def test_new_allowance_cannot_override_global_call_limit(self):
        c=self.controller();c.state['model_calls']=65
        with patch('quant_research_loop.r03.activation_authorized',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'PAUSED_BUDGET'):c.check_budget()

    def test_old_engineering_stop_has_distinct_name(self):
        c=self.controller()
        with patch('quant_research_loop.r03.activation_authorized',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'ENGINEERING_FIX_ALLOWANCE_EXHAUSTED'):c.check_budget()
        self.assertEqual(c.state['status'],'ENGINEERING_BLOCKED')

    def test_time_reserves_follow_into_revision(self):
        c=self.controller()
        c.state['r03_activation']=dict(prior_accounted_seconds=7538,elapsed_wall_seconds=100,
                                        child_seconds_at_start=300)
        self.assertAlmostEqual(accounted_seconds(c.state),7730.515)


class R03ReportingTests(unittest.TestCase):
    def test_zero_weeks_partial_quarters_and_2000_target(self):
        trades=[dict(date='2026-04-01',contracts='1',entry='100',exit='110',direction='1',commission='1.22',net_pnl='18.78',
                     entry_time='2026-04-01 10:00:00-04:00',exit_time='2026-04-01 15:55:00-04:00',initial_fill_risk='20')]
        daily=[dict(date='2026-04-01',trades='1',net_pnl='18.78')]
        marks=[dict(date='2026-04-01',i='385',day_pnl='18.78')]
        m,events=ledger_metrics(trades,daily,marks,'2026-04-01','2026-09-02')
        self.assertEqual(m['calendar_weeks'],23)
        self.assertEqual(m['zero_trade_weeks'],22)
        self.assertEqual(m['complete_quarters'],1)
        self.assertTrue(m['quarterly'][-1]['partial_boundary'])
        profile={'targets':{'arithmetic_mean_monthly_net_trading_pnl_usd':2000},
                 'strategy':{'min_trades_per_calendar_week':3,'max_no_trade_span_weeks':1},
                 'risk':{'per_trade_usd_ceiling':300,'max_drawdown_usd_ceiling':2500}}
        gates=qualify_metrics(m,profile)
        self.assertEqual(gates['monthly_target_usd'],2000)
        self.assertFalse(gates['monthly_target_met'])
        self.assertEqual(gates['quarterly_gate'],'INSUFFICIENT_COVERAGE')
        self.assertFalse(gates['ready_for_forward'])
        self.assertEqual(events[0]['coverage'],'UNKNOWN')
        self.assertIsNone(m['realized_payoff'])
        bad=copy.deepcopy(trades);bad[0]['net_pnl']='20'
        with self.assertRaisesRegex(ValueError,'cash reconciliation'):ledger_metrics(bad,daily,marks,'2026-04-01','2026-09-02')


class R03CompletionTests(unittest.TestCase):
    def fixture(self):
        import json
        original={'info':{'id':'same','outcome':'succeeded','model':{'variant':'xhigh'},'cost':0},
                  'messages':[{'type':'assistant','content':'not an outcome'}, {'type':'idle','outcome':'succeeded'}]}
        raw=json.dumps({'type':'text','sessionID':'same','part':{'text':'completed'}})
        return original,raw

    def test_minimizer_preserves_required_idle_without_transcript(self):
        original,raw=self.fixture()
        self.assertEqual(classify(raw,0,session_export={'info':original['info']}),'MISSING_TERMINAL_OUTCOME')
        minimal=completion_export(original)
        self.assertEqual(minimal['messages'],[{'type':'idle','outcome':'succeeded'}])
        self.assertEqual(classify(raw,0,session_export=minimal),'SUCCESS')

    def test_minimizer_cannot_hide_assistant_runtime_error(self):
        original,raw=self.fixture()
        original['messages'].insert(0,{'type':'assistant','error':{'type':'provider.auth','status':403,'message':'private detail'}})
        minimal=completion_export(original)
        self.assertNotIn('message',minimal['messages'][0]['error'])
        self.assertEqual(classify(raw,0,session_export=minimal),'AUTH')

    def test_info_only_mismatch_and_failed_idle_remain_rejected(self):
        original,raw=self.fixture()
        original['messages'][-1]['outcome']='failed'
        self.assertEqual(classify(raw,0,session_export=completion_export(original)),'MISSING_TERMINAL_OUTCOME')
        original['info']['id']='other'
        self.assertEqual(classify(raw,0,session_export=completion_export(original)),'SESSION_MISMATCH')


if __name__=='__main__':unittest.main()

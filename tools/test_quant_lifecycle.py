import unittest
from datetime import date, timedelta
from quant_research_loop.lifecycle import replay, anniversary


RULES = dict(rules_hash='SYNTHETIC_TEST_ONLY', monthly=49, activation=149,
             mll=2000, target=3000, consistency=.55, min_days=2,
             combine_max_minis=5, scaling=[[-2000, 2], [1500, 3]], dll=None,
             winning_day=150, winning_days=5, payout_ratio=.5, payout_cap=2000,
             trader_split=.9, min_payout=125, withdrawal_fee=30)


def days(values):
    return [dict(date=str(date(2024, 1, 1) + timedelta(days=i)), net=v,
                 n_trades=1, min_intraday_net=min(0, v), max_minis=1,
                 flat=True, coverage='COMPLETE', request_allowed=True)
            for i, v in enumerate(values)]


def run(values, **kwargs):
    rows = days(values)
    return replay(rows, kwargs.pop('rules', RULES), start='2024-01-01',
                  end=rows[-1]['date'], budget=kwargs.pop('budget', 1000), synthetic=True, **kwargs)


class LifecycleTests(unittest.TestCase):
    def test_reset_then_payout_hand_calculation(self):
        r = run([1500, 1500] + [200] * 5)
        self.assertEqual(r['passed_on'], '2024-01-02')
        self.assertEqual(r['remaining_balance'], 500)
        self.assertEqual(r['payouts'][0]['requested'], 500)
        self.assertEqual(r['modeled_cash_received'], 420)
        self.assertEqual(r['net_cash_known_costs_only'], 222)  # 450-30-49-149, fee once
        self.assertEqual(r['remaining_floor'], 0)
        self.assertEqual(r['actual_cash_received'], 0)

    def test_original_120_scenario_cannot_activate(self):
        r = run([1500, 1500, -10000], budget=120)
        self.assertEqual(r['status'], 'ACTIVATION_BUDGET_BLOCKED')
        self.assertEqual(r['external_cash_spent'], 49)
        self.assertEqual(r['payouts'], [])
        self.assertIsNone(r['failed_on'])

    def test_intraday_breach_positive_close(self):
        rows = days([1000])
        rows[0]['min_intraday_net'] = -2000
        r = replay(rows, RULES, start='2024-01-01', end='2024-01-01', synthetic=True)
        self.assertEqual(r['status'], 'MLL_FAILED')
        self.assertEqual(r['remaining_balance'], 50000)

    def test_floor_updates_eod_not_intraday_high(self):
        rows = days([1000, -1100])
        r = replay(rows, RULES, start='2024-01-01', end='2024-01-02', synthetic=True)
        self.assertEqual(r['status'], 'INCOMPLETE')
        self.assertEqual(r['remaining_floor'], 49000)

    def test_post_payout_floor_failure(self):
        r = run([1500,1500] + [200]*5 + [-500])
        self.assertEqual(r['status'], 'MLL_FAILED')
        self.assertEqual(r['modeled_cash_received'], 420)

    def test_payout_cap_and_cycle_counter_reset(self):
        r = run([1500,1500] + [1000]*5 + [200]*4)
        self.assertEqual(len(r['payouts']), 1)
        self.assertEqual(r['payouts'][0]['requested'], 2000)
        self.assertEqual(r['remaining_balance'], 3800)

    def test_next_payout_requires_positive_cycle(self):
        r = run([1500,1500] + [1000]*5 + [-1200] + [150]*5)
        self.assertEqual(len(r['payouts']), 1)

    def test_missing_path_not_certified(self):
        rows = days([1500,1500])
        rows[0].pop('min_intraday_net')
        r = replay(rows, RULES, start='2024-01-01', end='2024-01-02', synthetic=True)
        self.assertEqual(r['intraday_breach_status'], 'UNKNOWN')
        self.assertFalse(r['certified_payout_feasible'])

    def test_calendar_renewals_include_no_trade_days(self):
        r = replay([], RULES, start='2024-01-31', end='2024-03-31', budget=120, synthetic=True)
        self.assertEqual(r['status'], 'CASH_BUDGET_BLOCKED')
        self.assertEqual(r['external_cash_spent'], 98)
        self.assertEqual(r['cost_events'][-1]['date'], '2024-02-29')
        self.assertEqual(anniversary(date(2024,1,31),2), date(2024,3,31))
        self.assertIn('calendar_coverage_unknown', r['unknowns'])

    def test_no_renewals_after_pass(self):
        r = run([1500,1500] + [0]*40)
        self.assertFalse(any(x['kind']=='renewal' for x in r['cost_events']))

    def test_unknown_scaling_blocks_only_funded(self):
        r = run([1500,1500,200], rules={**RULES, 'scaling':None})
        self.assertEqual(r['status'], 'RULES_UNCERTAIN')
        self.assertEqual(r['remaining_balance'], 0)

    def test_dll_is_not_fake_stop_fill(self):
        r = run([-600], rules={**RULES,'dll':500})
        self.assertEqual(r['status'], 'UNKNOWN_DLL_LIQUIDATION_PATH')
        self.assertEqual(r['remaining_balance'], 50000)

    def test_duplicate_and_nonfinite_rejected(self):
        rows = days([1]) * 2
        with self.assertRaises(ValueError):
            replay(rows, RULES, start='2024-01-01', end='2024-01-02')
        with self.assertRaises(ValueError):
            run([float('nan')])

    def test_consistency_raise_not_false_pass(self):
        self.assertIsNone(run([2500,500])['passed_on'])

    def test_failure_stops_cash_charges_no_reset(self):
        r = run([-2000]+[1000]*40)
        self.assertEqual(r['external_cash_spent'],49)
        self.assertEqual(r['status'],'MLL_FAILED')


if __name__ == '__main__':
    unittest.main()

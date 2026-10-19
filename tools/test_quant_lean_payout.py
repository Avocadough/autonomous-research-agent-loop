import copy
import datetime as dt
import unittest
from unittest.mock import patch

from tools import test_quant_skill_engineering as fixtures
from tools.quant_research_loop import skill_engineering as S, r03 as R, controller as C


class LeanPayoutTests(unittest.TestCase):
    setUp = fixtures.SkillEngineeringTests.setUp

    def authorize(self):
        patch.object(R, 'ROOT', self.root).start()
        path = self.root / R.LEAN_AUTHORIZATION
        path.parent.mkdir(parents=True, exist_ok=True)
        auth = dict(revision_id=R.LEAN_REVISION, evaluation_months=1, funded_months=6,
                    started_at_utc='2026-10-05T09:32:21+00:00', decision_due_utc='2026-10-06T09:32:21+00:00',
                    total_months=7, cash_target_usd=1000, buffer_target_usd=2000,
                    prop_budget_usd=300, economic_floor_usd=500, frequency_advisory=True,
                    shadow_review_days=28, skill_advisory_failure_deferral=True,
                    global_call_cap=1000, spend_usd=0, live='DISABLED', counter_reset=False,
                    historical_receipts_immutable=True, independent_qa_required=True,
                    new_worker_allowance=False, market_rerun_authorized=False)
        S.write(path, auth)
        self.state['lean_payout_authority'] = dict(S.pin(path), revision_id=R.LEAN_REVISION)
        return path

    def blocked(self):
        patch.object(S, '_safe', return_value=True).start()
        self.proposal['selected_skills']['lead'] = [
            self.source_ref['path'] + ' | sha256=' + self.source_ref['sha256']]
        with self.assertRaisesRegex(ValueError, 'valid catalog member'):
            S.maybe_review(self.controller)
        folder = self.root / S.RUNTIME / 'skill_engineering/540'
        receipt = S.read(folder / 'call_proposal/receipt.json')
        receipt['process_reaped'] = True
        S.write(folder / 'call_proposal/receipt.json', receipt)
        return self.state['skill_engineering']['reviews']['540'], folder

    def test_spent_format_failure_defers_preserving_failure_and_accounting(self):
        record, folder = self.blocked()
        self.authorize()
        before = copy.deepcopy(record)
        counters = (self.state['model_calls'], self.state['active_invocation_s'])
        active = copy.deepcopy(self.state['skill_engineering']['active_skills'])
        with patch.object(self.controller, 'model_call', side_effect=AssertionError('No new call')):
            result = S.maybe_review(self.controller)
        self.assertEqual(result['status'], 'DEFERRED')
        self.assertEqual(record, before)
        self.assertEqual(record['phase'], 'BLOCKED')
        self.assertEqual((self.state['model_calls'], self.state['active_invocation_s']), counters)
        self.assertEqual(self.state['skill_engineering']['active_skills'], active)
        self.assertEqual(self.state['skill_engineering']['next_review_at'], 550)
        self.assertFalse((folder / 'apply_plan.json').exists())
        self.assertIsNone(S.maybe_review(self.controller))

    def test_unfinished_apply_and_security_error_are_not_deferred(self):
        record, folder = self.blocked()
        self.authorize()
        record['apply_plan'] = {'path': 'unfinished'}
        self.assertIsNone(S.defer_advisory_failure(self.controller, record))
        record.pop('apply_plan')
        record['error'] = 'Skill Engineering attempted forbidden tools'
        self.assertIsNone(S.defer_advisory_failure(self.controller, record))

    def test_uncertain_provider_and_pin_mismatch_still_block(self):
        record, folder = self.blocked()
        auth = self.authorize()
        receipt = S.read(folder / 'call_proposal/receipt.json')
        receipt['process_reaped'] = False
        S.write(folder / 'call_proposal/receipt.json', receipt)
        with self.assertRaisesRegex(ValueError, 'certain terminal'):
            S.defer_advisory_failure(self.controller, record)
        auth.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            S.defer_advisory_failure(self.controller, record)

    def test_prospective_profile_keeps_original_unchanged(self):
        self.authorize()
        original = dict(targets=dict(evaluation_deadline_calendar_months=1,
            funded_to_first_cash_receipt_deadline_calendar_months=3,
            total_deadline_calendar_months=4, arithmetic_mean_monthly_net_trading_pnl_usd=2000),
            strategy=dict(min_trades_per_calendar_week=3, max_no_trade_span_weeks=1),
            cash_budget=dict(total_all_accounts_usd=300), permissions=dict(live='DISABLED'))
        before = copy.deepcopy(original)
        result = R.lean_payout_profile(self.state, original)
        self.assertEqual(original, before)
        self.assertEqual(result['targets']['evaluation_deadline_calendar_months'], 1)
        self.assertEqual(result['targets']['funded_to_first_cash_receipt_deadline_calendar_months'], 6)
        self.assertEqual(result['targets']['total_deadline_calendar_months'], 7)
        self.assertEqual(result['targets']['arithmetic_mean_monthly_net_trading_pnl_usd'], 500)
        self.assertTrue(result['strategy']['frequency_advisory'])
        self.assertEqual(result['shadow_review_days'], 28)
        self.assertEqual(result['permissions'], original['permissions'])
        self.state['lean_payout_authority']['revision_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            R.lean_payout_profile(self.state, original)

    def test_deadline_blocks_nr7_without_adding_worker_allowance(self):
        self.authorize()
        job = {'job_id':'PAYOUT_NR7_REPAIR_V1', 'objective':'repair'}
        C._guard_lean_payout_route(self.state, job, dt.datetime(2026,10,6,9,32,20,tzinfo=dt.timezone.utc))
        with self.assertRaisesRegex(ValueError, 'R27_DEADLINE'):
            C._guard_lean_payout_route(self.state, job, dt.datetime(2026,10,6,9,32,21,tzinfo=dt.timezone.utc))
        C._guard_lean_payout_route(self.state, {'job_id':'OTHER'}, dt.datetime(2026,10,7,tzinfo=dt.timezone.utc))
        C._guard_lean_payout_route(self.state,
            {'job_id':'NR7_NEW_DATA','job_class':'EXPERIMENT','objective':'unchanged rules on new allocation'},
            dt.datetime(2026,10,7,tzinfo=dt.timezone.utc))
        with self.assertRaisesRegex(ValueError, 'R27_DEADLINE'):
            C._guard_lean_payout_route(self.state,
                {'job_id':'ALIAS','inputs':[{'path':'runtime/jobs/PAYOUT_R26_NR7_CORRECTED_2025_VALIDATION_V1/work/result.json'}]},
                dt.datetime(2026,10,7,tzinfo=dt.timezone.utc))

    def test_historical_profile_selection_and_gate_schema_unchanged(self):
        profile = {'effective_profile':{'old':True}}
        self.assertIs(R.lean_payout_job_profile({'job_id':'OLD'}, profile), profile)

    def test_future_job_selects_exact_prospective_profile_only(self):
        self.authorize()
        patch.object(R,'RUNTIME',self.root/'runtime').start()
        R.RUNTIME.mkdir()
        S.write(R.RUNTIME/'state.json',self.state)
        profile={'effective_profile':dict(targets={},strategy={}),
                 'effective_profile_path':'historical.json','effective_profile_sha256':'old'}
        before=copy.deepcopy(profile)
        path=self.root/R.LEAN_AUTHORIZATION.parent/'EFFECTIVE_PROFILE.json'
        S.write(path,R.lean_payout_profile(self.state,profile['effective_profile']))
        job={'job_id':'NEW','lean_payout_profile':S.pin(path)}
        future=R.lean_payout_job_profile(job,profile)
        self.assertEqual(profile,before)
        self.assertEqual(future['effective_profile']['targets']['funded_to_first_cash_receipt_deadline_calendar_months'],6)
        self.assertEqual(future['effective_profile_sha256'],S.digest(path))
        path.write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'hash/identity mismatch'):
            R.lean_payout_job_profile(job,profile)

    def test_context_retains_all_cases_and_fails_on_corrupt_pin(self):
        self.authorize()
        patch.object(C, 'ROOT', self.root).start()
        effective = dict(targets={}, strategy={})
        profile = {'effective_profile':effective}
        refs = [('lean_payout_profile','EFFECTIVE_PROFILE.json',R.lean_payout_profile(self.state,effective)),
                ('lean_payout_business_report','business_screen/report.json',
                 {'records':[dict(case=str(i),net_pnl=i,weekly=[1]*50) for i in range(15)]}),
                ('lean_payout_business_registration','business_screen/REGISTRATION.json',{'pins':[]})]
        for key,suffix,value in refs:
            path=self.root/R.LEAN_AUTHORIZATION.parent/suffix
            path.parent.mkdir(parents=True,exist_ok=True)
            S.write(path,value)
            self.state[key]=S.pin(path)
        context=C._lean_payout_context(self.state,profile)
        self.assertEqual(len(context['business_report']['records']),15)
        self.assertFalse(context['research_accepted'])
        self.assertNotIn('weekly',context['business_report']['records'][0])
        compact=C._compact_lead_bundle({'lean_payout_decision':context})
        self.assertEqual(compact['lean_payout_decision'],context)
        (self.root/self.state['lean_payout_business_report']['path']).write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'missing/changed pin'):
            C._lean_payout_context(self.state,profile)


if __name__ == '__main__':
    unittest.main()

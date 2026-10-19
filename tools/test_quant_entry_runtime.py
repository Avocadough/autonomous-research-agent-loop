"""Scoped Timer90/R09 entrypoint tests; no worker, native call or market data."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.quant_research_loop import generic_entry as E
from tools.quant_research_loop import runtime_policy as RP
from tools.quant_research_loop import r03
from tools.quant_research_loop.gate_zero import run_with_inactivity_guard


def r09_state():
    return {'runtime_policy_revision':RP.REVISION_UNBOUNDED,
            'runtime_policy':{'authorization_revision':{'revision_id':RP.REVISION_UNBOUNDED},
                              'per_call_timeout_mode':'no_fixed_elapsed_cap_with_inactivity_guard',
                              'per_job_timeout_mode':'no_fixed_elapsed_cap_with_inactivity_guard',
                              'inactivity_stall_seconds':3600},
            'model_calls':755,'completed':{'historical':{'sha256':'unchanged'}}}


class EntryRuntimeTests(unittest.TestCase):
    def test_validated_r09_omits_timer_preserves_state_and_existing_main(self):
        state=r09_state();before=copy.deepcopy(state);profile={'max_active_seconds':28800}
        with patch.object(E,'read',return_value=state),patch.object(r03,'active_profile',return_value=profile), \
                patch.object(RP,'cumulative_runtime_cap_bypassed',return_value=True) as validator, \
                patch('threading.Timer') as timer,patch.object(E,'main') as main:
            E.entrypoint()
            validator.assert_called_once_with(state,profile)
            timer.assert_not_called();main.assert_called_once_with()
        self.assertEqual(state,before)

    def test_non_r09_keeps_timer90_and_cleanup_on_main_error(self):
        for error in (None,ValueError('scope rejection')):
            with self.subTest(error=error),patch.object(E,'read',return_value={}), \
                    patch('threading.Timer') as timer,patch.object(E,'main',side_effect=error) as main, \
                    patch.object(RP,'cumulative_runtime_cap_bypassed') as validator:
                if error:
                    with self.assertRaisesRegex(ValueError,'scope rejection'):E.entrypoint()
                else:E.entrypoint()
                self.assertEqual(timer.call_args.args[0],90)
                self.assertTrue(timer.return_value.daemon)
                timer.return_value.start.assert_called_once_with()
                timer.return_value.cancel.assert_called_once_with()
                main.assert_called_once_with();validator.assert_not_called()

    def test_unvalidated_or_malformed_r09_fails_before_main(self):
        states=[r09_state(),r09_state(),r09_state(),r09_state()]
        states[1]['runtime_policy']['authorization_revision']['revision_id']=RP.REVISION_GPT6_LUNA
        states[2]['runtime_policy']['inactivity_stall_seconds']=0
        states[3]['runtime_policy']['per_job_timeout_mode']='existing_runner_limits'
        for state in states:
            with self.subTest(state=state),patch.object(E,'read',return_value=state), \
                    patch.object(r03,'active_profile',return_value={}), \
                    patch.object(RP,'cumulative_runtime_cap_bypassed',return_value=False), \
                    patch('threading.Timer') as timer,patch.object(E,'main') as main:
                with self.assertRaisesRegex(ValueError,'authority integrity mismatch'):E.entrypoint()
                timer.assert_not_called();main.assert_not_called()

    def test_r09_does_not_suppress_main_hash_scope_or_verification_errors(self):
        with patch.object(E,'read',return_value=r09_state()),patch.object(r03,'active_profile',return_value={}), \
                patch.object(RP,'cumulative_runtime_cap_bypassed',return_value=True), \
                patch.object(E,'main',side_effect=ValueError('Input hash changed')):
            with self.assertRaisesRegex(ValueError,'Input hash changed'):E.entrypoint()

    def test_observed_entrypoint_r09_progress_outlives_scaled_legacy_timer(self):
        # A scaled historical timer makes the actual branch effect observable
        # without a 90-second unit test. Real entrypoint and real OS exit used.
        template='''import threading,time
from unittest.mock import patch
from tools.quant_research_loop import generic_entry as E,runtime_policy as RP,r03
state=STATE
original_timer=threading.Timer
def short_timer(seconds,callback):
    assert seconds==90
    return original_timer(.05,callback)
def progressing():
    print('entry started',flush=True)
    for i in range(4):
        time.sleep(.06)
        print('progress '+str(i),flush=True)
    print('entry completed',flush=True)
with patch.object(E,'read',return_value=state),patch.object(r03,'active_profile',return_value={}),patch.object(RP,'cumulative_runtime_cap_bypassed',return_value=True),patch('threading.Timer',side_effect=short_timer),patch.object(E,'main',side_effect=progressing):
    E.entrypoint()
'''
        for state,code in ((r09_state(),0),({},124)):
            command=[sys.executable,'-u','-c',template.replace('STATE',repr(state))]
            receipt,stdout,stderr=run_with_inactivity_guard(command,inactivity_seconds=2)
            self.assertEqual(receipt['exit_code'],code,stderr)
            self.assertTrue(receipt['process_reaped'])
            self.assertIn('entry started',stdout)
            if code==0:self.assertIn('entry completed',stdout)
            else:self.assertNotIn('entry completed',stdout)

    def test_existing_parent_cancel_and_inactivity_still_reap(self):
        command=[sys.executable,'-u','-c','import time;print("started",flush=True);time.sleep(5)']
        with tempfile.TemporaryDirectory() as tmp:
            cancel=Path(tmp)/'cancel.request';cancel.write_text('synthetic cancellation')
            receipt,_,stderr=run_with_inactivity_guard(command,inactivity_seconds=2,cancel_path=cancel)
            self.assertEqual(receipt['stop_reason'],'CANCEL_REQUESTED')
            self.assertTrue(receipt['process_reaped']);self.assertIsNone(receipt['exit_code'])
            self.assertIn('CANCEL_REQUESTED',stderr)
        receipt,_,stderr=run_with_inactivity_guard(command,inactivity_seconds=.2)
        self.assertEqual(receipt['stop_reason'],'STALL_INACTIVITY')
        self.assertTrue(receipt['process_reaped']);self.assertIsNone(receipt['exit_code'])
        self.assertIn('STALL_INACTIVITY',stderr)


if __name__=='__main__':unittest.main()

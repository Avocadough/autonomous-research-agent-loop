"""Single-writer supervision of the existing, authorized PAYOUT controller.

Recovery owns evidence classification and fresh Lead review checkpoints. This
module never calls a model, grants permissions, clears operator requests, or
starts another process. Run with ``-m tools.quant_research_loop.supervisor``.
"""
import copy
import json
import math
import time

from .controller import Controller, RUNTIME, writer_lock
from .runtime_policy import remaining_call_units, used_call_units
from .safety_hold import active, delegated_review_allowed


TERMINAL_STATUSES = frozenset({
    'READY_FOR_FORWARD', 'NO_QUALIFYING_CANDIDATE', 'DATA_OR_RULES_BLOCKED',
    'RESOURCE_LIMIT', 'PAUSED_BUDGET', 'PAUSED_USER', 'CANCELLED',
})
MIN_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600
POLL_SECONDS = 1


def _stopped(controller, runtime):
    state = controller.state
    if any((runtime / name).exists() for name in ('pause.request', 'cancel.request')):
        if state.get('status') != 'PAUSED_USER':
            state['status'] = 'PAUSED_USER'
            controller.save()
        return True
    if state.get('status') in TERMINAL_STATUSES or (active(state) and not delegated_review_allowed(state)):
        return True
    if remaining_call_units(state, controller.profile) <= 0 or used_call_units(state) >= 1000:
        state['status'] = 'RESOURCE_LIMIT'
        controller.save()
        return True
    return False


def _wait(controller, runtime, seconds, sleep):
    """Keep the same writer lock while remaining responsive to operator stops."""
    while seconds > 0:
        if _stopped(controller, runtime):
            return False
        duration = min(POLL_SECONDS, seconds)
        sleep(duration)
        seconds -= duration
    return not _stopped(controller, runtime)


def run(controller, *, recover=None, runtime=None, sleep=None):
    """Run until a real terminal gate; caller must hold the campaign writer lock.

    ``recover(controller)`` returns RESUME, BACKOFF, MAINTENANCE_REQUIRED or STOP. BACKOFF rechecks
    recovery after waiting, never blindly reruns a failed Controller invocation.
    RESUME must follow a changed, persisted recovery checkpoint; Controller still
    enforces worker_dispatch_ready and its normal QA, budget and permission gates.
    Unknown exceptions remain visible and fail closed instead of becoming retries.
    """
    if recover is None:
        from .autonomous_recovery import recover
    runtime = RUNTIME if runtime is None else runtime
    sleep = time.sleep if sleep is None else sleep
    run_controller = (controller.state.get('status') == 'RUNNING'
                      and not controller.state.get('inflight_call'))
    backoffs = 0
    while not _stopped(controller, runtime):
        if run_controller:
            controller.run(resume=True)
            if _stopped(controller, runtime):
                break
            run_controller = False
        before = copy.deepcopy(controller.state)
        outcome = recover(controller)
        if _stopped(controller, runtime):
            break
        action = outcome.get('action')
        if action == 'STOP':
            break
        if action == 'MAINTENANCE_REQUIRED':
            handoff=controller.state.get('maintenance_handoff') or {}
            if (handoff.get('action')!='MAINTENANCE_REQUIRED'
                    or handoff.get('worker_dispatch_authorized') is not False
                    or controller.state.get('status')=='RUNNING'
                    or controller.state.get('inflight_call')):
                raise RuntimeError('Maintenance handoff requires a safe persisted checkpoint')
            # Returning releases main's writer lock so existing maintenance can
            # supply changed evidence. Keep all incident and stop gates intact.
            break
        if action == 'RESUME':
            if controller.state == before or controller.state.get('inflight_call'):
                raise RuntimeError('Recovery requested resume without a reconciled checkpoint')
            run_controller = True
            backoffs = 0
        elif action == 'BACKOFF':
            delay = float(outcome.get('delay_seconds', MIN_BACKOFF_SECONDS))
            if not math.isfinite(delay) or delay < 0:
                raise ValueError('Recovery returned an invalid backoff interval')
            delay = min(MAX_BACKOFF_SECONDS,
                        max(delay, MIN_BACKOFF_SECONDS * 2 ** min(backoffs, 6)))
            backoffs += 1
            if not _wait(controller, runtime, delay, sleep):
                break
        else:
            raise ValueError('Recovery returned an unknown supervisor action')
    return controller.state


def main():
    # Acquire before Controller construction, which can persist migrations.
    # Hold across transient backoff; yield on a persisted maintenance handoff.
    with writer_lock(RUNTIME / 'writer.lock'):
        controller = Controller()
        state = run(controller)
        print(json.dumps({'status': state.get('status'),
                          'used_call_units': used_call_units(state),
                          'error': state.get('error')}, ensure_ascii=False))


if __name__ == '__main__':
    main()

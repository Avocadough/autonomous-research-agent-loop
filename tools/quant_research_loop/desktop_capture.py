"""Wait before worker reservation while Desktop owns an active Git capture.

This avoids a known overlapping writer; it never exempts Git from scope checks.
A capture starting after this check is still caught by the existing post gate.
"""
import os
import json
import time
import stat
from pathlib import Path

from .gate_zero import ROOT, write
from .prepare import digest


def recovery_context(state):
    """Transport the exact acknowledged incident, never grant resume authority."""
    record = state.get('operator_metadata_recovery')
    if not record:
        return None
    result = {}
    base = (ROOT / 'research/campaigns/PAYOUT_V1/revisions').resolve()
    for key in ('authorization', 'reconciliation'):
        pin = record[key]
        path = (ROOT / pin['path']).resolve()
        if not path.is_relative_to(base) or digest(path) != pin['sha256']:
            raise RuntimeError('HUMAN_ACTION_REQUIRED: metadata recovery evidence changed')
        result[key] = json.loads(path.read_text(encoding='utf-8'))
    return result


def active_captures(root):
    root = Path(root).resolve()
    directory = root / '.git/refs/codex/turn-diffs/captures'
    # Do not follow a linked Git directory or another checkout's metadata.
    for path in [
            root / '.git', root / '.git/refs', root / '.git/refs/codex',
            root / '.git/refs/codex/turn-diffs', directory]:
        if path.is_symlink() or path.is_junction():
            raise RuntimeError('HUMAN_ACTION_REQUIRED: linked Desktop capture metadata')
    if (root / '.git').is_file():
        raise RuntimeError('HOST_CAPTURE_GUARD_UNSUPPORTED: linked Git worktree')
    # Only a positively observed absent directory is safe to treat as empty.
    # os.walk otherwise suppresses scandir failures (including nested ones).
    try:
        mode = directory.stat().st_mode
    except FileNotFoundError:
        return []
    except OSError as error:
        raise RuntimeError('HOST_CAPTURE_SCAN_FAILED: capture directory unreadable') from error
    if not stat.S_ISDIR(mode):
        raise RuntimeError('HOST_CAPTURE_SCAN_FAILED: capture path is not a directory')

    def scan_failed(error):
        # A disappearance during enumeration is also uncertain; a later
        # authorized check can establish absence without reserving this call.
        raise RuntimeError('HOST_CAPTURE_SCAN_FAILED: capture enumeration failed') from error

    found = []
    for parent, dirs, files in os.walk(directory, followlinks=False, onerror=scan_failed):
        for name in dirs + files:
            path = Path(parent) / name
            if path.is_symlink() or path.is_junction():
                raise RuntimeError('HUMAN_ACTION_REQUIRED: linked Desktop capture metadata')
        found.extend((Path(parent) / name).relative_to(root).as_posix() for name in files)
    return sorted(found)


def wait_before_worker(controller, receipt_path, *, root=ROOT):
    """No model call, episode reservation, source edit, or retry during this wait."""
    root = Path(root)
    runtime = root / 'research/campaigns/PAYOUT_V1/runtime'
    started = time.monotonic()
    timeout = controller.state.get('runtime_policy', {}).get('inactivity_stall_seconds', 3600)
    record = None
    while True:
        for name in ('pause.request', 'cancel.request'):
            if (runtime / name).exists():
                controller.state['status'] = 'PAUSED_USER'
                controller.save()
                raise RuntimeError('HOST_CAPTURE_WAIT_CANCELLED: ' + name)
        captures = active_captures(root)
        if not captures:
            if record is not None:
                record.update(status='CLEARED', elapsed_s=time.monotonic() - started)
                write(receipt_path, record)
                controller.state['desktop_capture_wait'] = record
                controller.save()
                print('Desktop capture cleared; ordinary worker gates remain active.', flush=True)
            return
        if record is None:
            record = {'status': 'WAITING', 'active_refs_at_start': captures,
                      'model_calls_reserved': 0, 'worker_attempts_reserved': 0,
                      'scope_exemptions': [], 'receipt_path': str(receipt_path)}
            write(receipt_path, record)
            controller.state['desktop_capture_wait'] = record
            controller.save()
            print('Waiting for Desktop Git capture before worker reservation.', flush=True)
        if time.monotonic() - started >= timeout:
            record.update(status='STALLED', elapsed_s=time.monotonic() - started,
                          remaining_refs=captures)
            write(receipt_path, record)
            controller.state['status'] = 'ENGINEERING_BLOCKED'
            controller.save()
            raise RuntimeError('HOST_CAPTURE_WAIT_STALLED: inactivity guard; no worker dispatched')
        time.sleep(1)

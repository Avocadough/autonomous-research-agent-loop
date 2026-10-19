"""OpenCode v2 runtime envelopes, never model prose or nested tool data.

The run stream can omit its final step_finish. A matching saved session export
with an idle outcome can establish completion; exit zero alone cannot.
"""
import json

def events(raw):
    parsed=[]; malformed=False
    for line in (raw or '').splitlines():
        if not line.strip(): continue
        try:
            value=json.loads(line)
            if not isinstance(value,dict): malformed=True
            else: parsed.append(value)
        except (ValueError,TypeError): malformed=True
    return parsed,malformed

def scoped_events(raw, session_id=None):
    parsed,bad=events(raw)
    ids={e.get('sessionID') for e in parsed if isinstance(e.get('sessionID'),str)}
    if session_id is None:
        if len(ids)>1: return [],True,None
        session_id=next(iter(ids),None)
    # Runtime events without session provenance cannot authenticate another session.
    selected=[e for e in parsed if e.get('sessionID')==session_id]
    return selected,bad,session_id

def error_kind(error):
    if not isinstance(error,dict): return 'runtime-error'
    kind=error.get('type','')
    status=error.get('status')
    if kind in ('permission.denied','permission.rejected'): return 'permission-denied'
    if isinstance(kind,str) and kind.startswith('provider.'):
        if kind=='provider.auth' or status in (401,403): return 'auth'
        if kind=='provider.rate-limit' or status==429: return 'rate-limit'
        return 'provider-error'
    return 'runtime-error'

def runtime_errors(raw, session_id=None):
    selected,_,_=scoped_events(raw,session_id)
    return [e['error'] for e in selected if e.get('type')=='error' and 'error' in e]

def classify(raw, exit_code, *, session_id=None, session_export=None,
             artifact_valid=True, process_failure=None):
    selected,bad,sid=scoped_events(raw,session_id)
    failures=[error_kind(e.get('error')) for e in selected if e.get('type')=='error']
    terminal=False
    if session_export is not None:
        info=session_export.get('info',{})
        if not sid or info.get('id')!=sid: return 'SESSION_MISMATCH'
        messages=session_export.get('messages',[])
        for m in messages:
            if m.get('type')=='assistant' and m.get('error') is not None:
                failures.append(error_kind(m['error']))
        idle=[m.get('outcome') for m in messages if m.get('type')=='idle']
        terminal=info.get('outcome')=='succeeded' and bool(idle) and idle[-1]=='succeeded'
        if info.get('outcome')=='failed' and not failures: failures.append('runtime-error')
    if failures:
        for category in ('auth','rate-limit','permission-denied','provider-error','runtime-error'):
            if category in failures: return category.upper().replace('-','_')
    if process_failure=='start': return 'PROCESS_START_FAILED'
    if process_failure=='timeout' or exit_code is None: return 'TIMEOUT_OR_INTERRUPTED'
    if exit_code!=0: return 'PROCESS_FAILED'
    if bad: return 'MALFORMED_OR_MIXED_OUTPUT'
    if not selected or not sid: return 'MISSING_SESSION_OUTCOME'
    for e in selected:
        if e.get('type')=='tool_use':
            state=e.get('part',{}).get('state',{})
            if state.get('status') in ('error','denied','rejected'):
                return 'PERMISSION_DENIED' if state.get('status') in ('denied','rejected') else 'TOOL_FAILED'
            if state.get('status') not in ('completed',): return 'PARTIAL_TOOL_OUTCOME'
    if session_export is None and selected[-1].get('type')=='step_finish' and selected[-1].get('part',{}).get('reason')=='stop': terminal=True
    if not terminal: return 'MISSING_TERMINAL_OUTCOME'
    if not artifact_valid: return 'INVALID_OR_MISSING_ARTIFACT'
    return 'SUCCESS'

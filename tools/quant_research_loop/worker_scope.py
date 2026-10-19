"""Scoped worker commands and native receipts; no market-content inspection."""
import hashlib
import json
import os
import re
import stat
import zlib
from pathlib import Path
from urllib.parse import unquote, urlparse

from .gate_zero import ROOT, write
from .prepare import digest

VERSION = 'SCOPED_LITERAL_V1'


class ScopedPreEntryIncomplete(RuntimeError):
    """An audited, permitted worker turn ended without invoking the runner."""

    def __init__(self, receipt, receipt_path):
        super().__init__('ENGINEERING_BLOCKED: scoped worker did not execute fixed entry')
        self.receipt = receipt
        self.receipt_path = receipt_path


def enabled(state):
    return state.get('worker_command_policy', {}).get('version') == VERSION


def quoted(path):
    return "'" + str(path).replace("'", "''") + "'"


def inspections(job):
    paths = [(ROOT / item['path'], item['kind']) for item in job['inputs']]
    folder = ROOT / 'research/campaigns/PAYOUT_V1/runtime/jobs' / job['job_id']
    paths += [(folder / 'job.json', 'json'), (folder / 'work/analysis.py', 'text')]
    result = []
    for path, kind in paths:
        if kind == 'csv':
            result.append('Get-Content -LiteralPath ' + quoted(path) + ' -TotalCount 3')
        else:
            result.append('Get-Content -Raw -LiteralPath ' + quoted(path))
    result.append('Test-Path -LiteralPath ' + quoted(folder / 'work/analysis.py'))
    return result


def command_kind(command, job, fixed, tests):
    """Classify actual native tool arguments, never shell-rendered display text.

    A literal here-string cannot execute its source payload. It must be one
    pipeline to one exact permitted implementation path, with no other syntax.
    """
    body = command.replace('\r\n', '\n').strip()
    if body == fixed:
        return 'fixed_entry'
    if body in tests:
        return 'scoped_test'
    allowed_reads = set(inspections(job))
    statements = [s.strip() for s in body.split(';') if s.strip()]
    if statements and all(s in allowed_reads for s in statements):
        return 'pinned_read'
    pattern = r"@'\n(?P<source>.*?)\n'@\s*\|\s*Set-Content -LiteralPath (?P<path>'(?:[^']|'')*') -Encoding utf8"
    match = re.fullmatch(pattern, body, re.DOTALL)
    allowed_edits = [p for p in job['allowed_file_scope'] if p.endswith('.py')]
    if match and not re.search(r"(?m)^\s*'@", match['source']):
        path = match['path'][1:-1].replace("''", "'")
        if path in [str(ROOT / p) for p in allowed_edits]:
            return 'literal_scoped_write'
    raise RuntimeError('HUMAN_ACTION_REQUIRED: worker command outside exact read/write/runner contract')


def metadata_snapshot(root=ROOT):
    """Enumerate all repository files, including ignored files, without opening them."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction():
                dirs.remove(name)
                st = path.lstat()
                result[path.relative_to(root).as_posix()] = [st.st_size, st.st_mtime_ns, st.st_ctime_ns, 'LINK_NOT_FOLLOWED']
        for name in files:
            path = Path(directory) / name
            st = path.lstat()
            result[path.relative_to(root).as_posix()] = [st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                                                       'LINK_NOT_FOLLOWED' if path.is_symlink() else 'FILE']
    return result


def metadata_diff(before, after, allowed, prefix):
    changes = {p: {'before': before.get(p), 'after': after.get(p)}
               for p in sorted(set(before) | set(after)) if before.get(p) != after.get(p)}
    unexpected = [p for p in changes if p not in allowed and not p.startswith(prefix)]
    return changes, unexpected


def git_storage_identity(path, oid, packed=False):
    """Hash Git storage only; never emit object payload or invoke Git."""
    identity, raw_hash = hashlib.sha1(), hashlib.sha256()
    with path.open('rb') as stream:
        if packed:
            size = path.stat().st_size
            header = stream.read(12)
            if size < 32 or header[:4] != b'PACK' or int.from_bytes(header[4:8], 'big') not in (2, 3):
                raise ValueError('Invalid pack header')
            identity.update(header); raw_hash.update(header)
            remaining = size - 32
            while remaining:
                data = stream.read(min(65536, remaining))
                if not data: raise ValueError('Pack shortened during read')
                identity.update(data); raw_hash.update(data); remaining -= len(data)
            trailer = stream.read(20); raw_hash.update(trailer)
            if len(trailer) != 20 or trailer.hex() != oid or stream.read(1):
                raise ValueError('Pack trailer or length mismatch')
        else:
            inflater, header, length, observed = zlib.decompressobj(), bytearray(), None, 0
            while raw := stream.read(65536):
                raw_hash.update(raw)
                pending = raw
                while pending:
                    data = inflater.decompress(pending, 65536); pending = inflater.unconsumed_tail
                    identity.update(data)
                    if length is None:
                        delimiter = data.find(b'\0')
                        header.extend(data if delimiter < 0 else data[:delimiter])
                        if len(header) > 64: raise ValueError('Invalid object header')
                        if delimiter >= 0:
                            match = re.fullmatch(rb'(blob|tree|commit|tag) (0|[1-9][0-9]*)', header)
                            if not match: raise ValueError('Invalid object header')
                            length = int(match[2]); observed += len(data) - delimiter - 1
                    else:
                        observed += len(data)
                if inflater.unused_data: raise ValueError('Trailing compressed data')
            if not inflater.eof or length is None or observed != length:
                raise ValueError('Incomplete object or declared length mismatch')
    if identity.hexdigest() != oid: raise ValueError('Content-addressed identity mismatch')
    return {'content_sha1': oid, 'storage_sha256': raw_hash.hexdigest()}


def validated_git_storage(changes, unexpected, native_receipt, root=ROOT, allow_later_mtime=False):
    """Separate narrowly verified storage activity; raw metadata remains strict."""
    if (native_receipt.get('write_policy_valid') is not True or not native_receipt.get('pretool_policy')
            or any(native_receipt.get(key) != [] for key in ('errors','protocol_errors','unparsed_tools'))):
        raise RuntimeError('HUMAN_ACTION_REQUIRED: Git storage classification requires clean native permission evidence')
    root = Path(root)
    validated, remaining, rejected = {}, [], {}
    for name in unexpected:
        try:
            loose = re.fullmatch(r'\.git/objects/([0-9a-f]{2})/([0-9a-f]{38})', name)
            pack = re.fullmatch(r'\.git/objects/pack/pack-([0-9a-f]{40})\.pack', name)
            if not loose and not pack: raise ValueError('Not exact content-addressed storage')
            before, after = changes[name]['before'], changes[name]['after']
            if pack and before is None: raise ValueError('New packs require explicit incident review')
            if (not after or len(after)!=4 or after[3]!='FILE'
                    or (before is not None and (len(before)!=4 or before[3]!='FILE'
                        or before[0]!=after[0] or before[2]!=after[2]))):
                raise ValueError('Not a regular-file addition or mtime-only change')
            path = root / name
            for ancestor in [path, *path.parents]:
                if ancestor == root: break
                if ancestor.is_symlink() or ancestor.is_junction(): raise ValueError('Linked storage path')
            def observed():
                value = path.lstat()
                if not stat.S_ISREG(value.st_mode): raise ValueError('Not a regular storage file')
                return [value.st_size,value.st_mtime_ns,value.st_ctime_ns,'FILE']
            current=observed()
            if (current!=after and (not allow_later_mtime or
                    [current[0],current[2],current[3]]!=[after[0],after[2],after[3]])):
                raise ValueError('Storage changed since metadata snapshot')
            oid = loose[1]+loose[2] if loose else pack[1]
            proof = git_storage_identity(path, oid, packed=bool(pack))
            if observed()!=current: raise ValueError('Storage changed during identity read')
            validated[name] = dict(proof, kind='LOOSE_OBJECT' if loose else 'PACK',
                                   change='ADDITION' if before is None else 'MTIME_ONLY')
        except (OSError,ValueError,zlib.error) as error:
            remaining.append(name); rejected[name]=str(error)
    return {'validated_storage':validated,'remaining_unexpected':remaining,'rejected':rejected,
            'claim_limit':'Current storage identities only; prior byte equality and actor attribution are unknown. '
                          'No Git-control/worktree exemption, worker permission change, historical scope PASS or result acceptance.'}


def native_command_events(events, job, fixed, tests, cwd):
    """Read executor-owned argv arrays, not model JS or display shell strings."""
    commands, errors = [], []
    for event in events:
        payload=event.get('payload',{});item=payload.get('item',{})
        if event.get('type')!='event_msg' or payload.get('type')!='item_completed' or item.get('type')!='CommandExecution':
            continue
        argv=item.get('command',[])
        if (not isinstance(argv,list) or len(argv)<3 or argv[-2].lower()!='-command'
                or Path(argv[0]).name.lower() not in ('pwsh.exe','powershell.exe')
                or any(a.lower()!='-noprofile' for a in argv[1:-2])):
            raise RuntimeError('ENGINEERING_BLOCKED: unsupported native executor argv format')
        actual_cwd=item.get('cwd','')
        if actual_cwd.startswith('file:'):
            actual_cwd=unquote(urlparse(actual_cwd).path).lstrip('/')
        if Path(actual_cwd).resolve()!=Path(cwd).resolve():
            errors.append('Executor changed frozen working directory')
        try:kind=command_kind(argv[-1],job,fixed,tests)
        except RuntimeError as error:errors.append(str(error));kind='REJECTED'
        commands.append({'call_id':item.get('id'),'argv':argv,'command':argv[-1],
                         'workdir':actual_cwd,'kind':kind,'exit_code':item.get('exit_code'),
                         'started_at_ms':payload.get('started_at_ms'),'completed_at_ms':payload.get('completed_at_ms'),
                         'basis':'native CommandExecution argv'})
    return commands,errors


def literal_patch_wrapper(code):
    """Decode only a complete literal wrapper; never evaluate worker JavaScript."""
    code = code.strip()
    assigned = re.match(r'const\s+([A-Za-z_]\w*)\s*=\s*', code)
    direct = re.match(r'text\(\s*await\s+tools\.apply_patch\(\s*', code)
    start = assigned.end() if assigned else direct.end() if direct else None
    if start is None or code[start:start + 1] != '"':
        raise ValueError('Unsupported nonliteral apply_patch wrapper')
    patch, end = json.JSONDecoder().raw_decode(code, start)
    if not isinstance(patch, str):
        raise ValueError('Patch payload is not a literal string')
    suffix = code[end:]
    if assigned:
        variable = re.escape(assigned.group(1))
        patterns = [
            r';\s*const\s+([A-Za-z_]\w*)\s*=\s*await\s+tools\.apply_patch\(\s*'
            + variable + r'\s*\)\s*;\s*text\(\s*\1\s*\)\s*;?',
            r';\s*text\(\s*await\s+tools\.apply_patch\(\s*'
            + variable + r'\s*\)\s*\)\s*;?',
        ]
    else:
        patterns = [r'\)\s*\)\s*;?']
    if not any(re.fullmatch(pattern, suffix) for pattern in patterns):
        raise ValueError('Unsupported statements in apply_patch wrapper')
    return patch


def patch_destinations(patch, job, cwd):
    if (not patch.startswith('*** Begin Patch\n') or not patch.rstrip().endswith('*** End Patch')
            or '*** Move to:' in patch or '*** Delete File:' in patch):
        raise ValueError('Disallowed patch operation')
    matches = re.findall(r'^\*\*\* (Add|Update) File: (.+)$', patch, re.MULTILINE)
    if not matches:
        raise ValueError('Patch has no explicit destinations')
    allowed = {(ROOT / path).resolve() for path in job['allowed_file_scope'] if path.endswith('.py')}
    destinations = {}
    for operation, target in matches:
        path = Path(target)
        path = path if path.is_absolute() else Path(cwd) / path
        resolved = path.resolve()
        if resolved not in allowed or resolved in destinations:
            raise ValueError('Patch destination outside frozen source scope or duplicated')
        destinations[resolved] = operation.lower()
    return destinations


def native_patch_events(events, job, cwd):
    """Bind a supported literal wrapper to its native edit/output interval."""
    edits, errors, unavailable, protocol_errors, wrappers = [], [], [], [], set()
    for index, event in enumerate(events):
        payload = event.get('payload', {})
        if (event.get('type') != 'response_item' or payload.get('type') != 'custom_tool_call'
                or payload.get('name', '').split('.')[-1] != 'exec'
                or not re.search(r'\btools\.apply_patch\b', payload.get('input', ''))):
            continue
        call_id = payload.get('call_id')
        wrappers.add(call_id)
        try:
            patch = literal_patch_wrapper(payload.get('input', ''))
            destinations = patch_destinations(patch, job, cwd)
        except (TypeError, ValueError) as error:
            errors.append(str(error))
            continue
        outputs = [(i, value.get('payload', {})) for i, value in enumerate(events)
                   if i > index and value.get('type') == 'response_item'
                   and value.get('payload', {}).get('type') == 'custom_tool_call_output'
                   and value['payload'].get('call_id') == call_id]
        if not call_id or len(outputs) != 1:
            unavailable.append('Literal patch wrapper lacks one exact native output')
            continue
        end, output = outputs[0]
        if any(value.get('type') == 'response_item' and value.get('payload', {}).get('type') in
               ('function_call', 'custom_tool_call') for value in events[index + 1:end]):
            unavailable.append('Overlapping tool calls prevent exact native patch attribution')
            continue
        native = [(i, value['payload']) for i, value in enumerate(events[index + 1:end], index + 1)
                  if value.get('type') == 'event_msg' and value.get('payload', {}).get('type') == 'item_completed'
                  and value['payload'].get('item', {}).get('type') == 'FileChange']
        texts = output.get('output', [])
        output_text = '\n'.join(x.get('text', '') for x in texts if isinstance(x, dict)) if isinstance(texts, list) else str(texts)
        failed = 'apply_patch verification failed:' in output_text
        if failed and not native:
            status = 'failed_before_change'
        elif len(native) == 1 and not failed:
            item = native[0][1]['item']
            changes = item.get('changes', {})
            if item.get('status') != 'completed' or not isinstance(changes, dict):
                errors.append('Native patch completion is not a completed path map')
                continue
            actual = {}
            for target, change in changes.items():
                path = Path(target)
                path = path if path.is_absolute() else Path(cwd) / path
                if not isinstance(change, dict) or change.get('move_path') is not None:
                    errors.append('Native patch attempted unsupported move/change')
                    continue
                actual[path.resolve()] = change.get('type')
            if actual != destinations:
                errors.append('Native FileChange paths/operations differ from literal patch')
                continue
            status = 'completed'
        else:
            unavailable.append('Literal patch wrapper lacks unambiguous native FileChange proof')
            continue
        if failed:
            for later in events[end + 1:]:
                lp = later.get('payload', {})
                if ((later.get('type') == 'response_item' and lp.get('type') in ('function_call', 'custom_tool_call'))
                        or (later.get('type') == 'event_msg' and lp.get('type') == 'item_completed'
                            and lp.get('item', {}).get('type') in ('CommandExecution', 'FileChange'))):
                    protocol_errors.append('Worker issued tools after a failed edit')
                    break
        edits.append({'call_id': call_id, 'patch': patch, 'status': status,
                      'destinations': [str(path) for path in destinations],
                      'native_file_change_ids': [entry['item'].get('id') for _, entry in native],
                      'basis': 'literal patch plus matching native FileChange/output interval'})
    return edits, errors, unavailable, protocol_errors, wrappers


def native_session_receipt(call, session_id, job, fixed, tests):
    """Extract exact command arguments and configured policy from this one session."""
    if not session_id or not re.fullmatch(r'[0-9a-f-]{36}', session_id):
        raise RuntimeError('ENGINEERING_BLOCKED: native session identity missing')
    session_root = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
    paths = list(session_root.rglob('*' + session_id + '*.jsonl'))
    if len(paths) != 1:
        raise RuntimeError('ENGINEERING_BLOCKED: exact persisted native session receipt unavailable')
    path = paths[0]
    contexts = []
    commands = []
    edits = []
    errors = []
    unavailable = []
    wrappers = []
    native_events=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    executed, execution_errors=native_command_events(native_events,job,fixed,tests,read_request_cwd(call))
    errors.extend(execution_errors)
    native_edits, edit_errors, edit_unavailable, edit_protocol_errors, patch_wrappers = native_patch_events(
        native_events, job, read_request_cwd(call))
    edits.extend(native_edits)
    errors.extend(edit_errors)
    unavailable.extend(edit_unavailable)
    for event in native_events:
        payload = event.get('payload', {})
        if event.get('type') == 'turn_context':
            contexts.append({k: payload.get(k) for k in ('cwd', 'approval_policy', 'sandbox_policy', 'model', 'effort')})
        if event.get('type') != 'response_item':
            continue
        if payload.get('type') == 'function_call':
            name = payload.get('name', '').split('.')[-1]
            try:
                args = json.loads(payload.get('arguments', '{}'))
            except (TypeError, ValueError):
                unavailable.append('Unparsed native arguments: ' + name)
                continue
            if name == 'exec_command':
                cmd = args.get('cmd', '')
                try:
                    kind = command_kind(cmd, job, fixed, tests)
                except RuntimeError as error:
                    errors.append(str(error));kind = 'REJECTED'
                commands.append({'call_id': payload.get('call_id'), 'command': cmd,
                                 'workdir': args.get('workdir'), 'kind': kind})
                expected_cwd = read_request_cwd(call)
                if args.get('workdir') and Path(args['workdir']).resolve() != Path(expected_cwd).resolve():
                    errors.append('Worker changed fixed working directory')
                if args.get('sandbox_permissions') not in (None, 'use_default'):
                    errors.append('Worker requested a permission override')
            elif name not in ('write_stdin',):
                unavailable.append('Unrecognized worker function: ' + name)
            elif args.get('chars', '').strip():
                errors.append('Worker sent interactive commands outside frozen contract')
        elif payload.get('type') == 'custom_tool_call':
            name = payload.get('name', '').split('.')[-1]
            if name == 'exec':
                code=payload.get('input','')
                wrappers.append({'call_id':payload.get('call_id'),'code':code})
                if payload.get('call_id') in patch_wrappers:
                    continue  # literal input and actual native edits were paired above
                names=set(re.findall(r'\btools\.([A-Za-z_][A-Za-z_0-9]*)\s*\(',code))
                if not executed or not names or names-{'exec_command','write_stdin'} or re.search(r'\btools\s*\[',code):
                    unavailable.append('Code-mode wrapper requires supported native execution receipts')
                if re.search(r'sandbox_permissions\s*:\s*[\"\']require_escalated',code):
                    errors.append('Worker requested a permission override')
                continue
            if name != 'apply_patch':
                unavailable.append('Unrecognized worker custom tool: ' + name)
                continue
            patch = payload.get('input', '')
            destinations = re.findall(r'^\*\*\* (?:Add|Update|Delete) File: (.+)$', patch, re.MULTILINE)
            if '*** Move to:' in patch or '*** Delete File:' in patch or not destinations:
                errors.append('Disallowed patch operation')
            for target in destinations:
                resolved = Path(target)
                if not resolved.is_absolute():
                    resolved = Path(contexts[-1]['cwd']) / target
                if resolved.resolve() not in [(ROOT / p).resolve() for p in job['allowed_file_scope'] if p.endswith('.py')]:
                    errors.append('Patch destination outside frozen source scope')
            edits.append({'call_id': payload.get('call_id'), 'patch': patch})
    if executed:
        commands=executed  # no double counting the model request and its execution
    request = json.loads((call / 'request.json').read_text(encoding='utf-8'))
    from .generic_worker import codex_requested_route
    # Old scope receipts without an explicit model request were Luna/max only.
    requested_model, requested_reasoning = (codex_requested_route(call)
        if request.get('transport') == 'native_app_server' or '-m' in request.get('argv', [])
        else ('gpt-6-luna', 'max'))
    protocol_errors=list(edit_protocol_errors)
    ordered=sorted(executed,key=lambda c:c.get('started_at_ms') or 0)
    for entry in ordered:
        if entry.get('exit_code') not in (None,0) and any(
                (later.get('started_at_ms') or 0)>(entry.get('completed_at_ms') or 0) for later in ordered):
            protocol_errors.append('Worker issued commands after a failed inspection/edit/entry')
    write_policy_valid=bool(contexts) and all(
        c.get('sandbox_policy',{}).get('type')=='workspace-write'
        and not c.get('sandbox_policy',{}).get('network_access',False)
        and Path(c.get('cwd','')).resolve()==Path(request['cwd']).resolve()
        and all(Path(p).resolve()==Path(request['cwd']).resolve() for p in c.get('sandbox_policy',{}).get('writable_roots',[]))
        for c in contexts)
    pretool_path = call.parent / ('native_preflight_' + call.name.rsplit('_', 1)[-1] + '.json')
    pretool = None
    if pretool_path.exists():
        import datetime as dt
        from .native_preflight import validate_policy
        pretool = json.loads(pretool_path.read_text(encoding='utf-8'))
        actual_id = validate_policy(pretool.get('response', {}), request['cwd'], requested_model, requested_reasoning)
        before_ms = dt.datetime.fromisoformat(pretool['recorded_utc']).timestamp() * 1000
        if (pretool.get('passed') is not True or not pretool.get('no_model_turn_sent')
                or actual_id != session_id or pretool.get('native_session_id') != session_id
                or any(c.get('started_at_ms') is None or c['started_at_ms'] <= before_ms for c in executed)):
            errors.append('Native pre-turn proof does not precede this session command history')
    receipt = {'parser_version':3,'native_session_id': session_id, 'native_journal_sha256': digest(path),
        'policy_contexts': contexts, 'request': request, 'commands': commands, 'edits': edits,
        'code_mode_transport':wrappers,'write_policy_valid':write_policy_valid,'protocol_errors':protocol_errors,
        'errors': errors, 'unparsed_tools': unavailable,
        'pretool_policy': {'path':pretool_path.relative_to(ROOT).as_posix(),'sha256':digest(pretool_path)} if pretool else None,
        'claim_limit': 'Native configured-policy and exact tool-call receipt, not an independent OS enforcement certificate. Session storage is outside the research workspace; no credential files were read.'}
    output=call/'native_policy_and_commands.json'
    if output.exists() and json.loads(output.read_text(encoding='utf-8'))!=receipt:
        output=call/'native_policy_and_commands_parsed_v3.json'
    write(output, receipt)
    if protocol_errors:
        raise RuntimeError('HUMAN_ACTION_REQUIRED: native worker stop-condition violation: '
                           + '; '.join(protocol_errors) + '; preserve artifact as incomplete')
    if not contexts or not commands:
        raise RuntimeError('ENGINEERING_BLOCKED: native journal lacks policy or executable tool arguments')
    if errors:
        raise RuntimeError('HUMAN_ACTION_REQUIRED: native worker tool-policy violation: ' + '; '.join(errors))
    if unavailable:
        raise RuntimeError('ENGINEERING_BLOCKED: native tool receipt parser incomplete: ' + '; '.join(unavailable))
    if not executed:
        raise RuntimeError('ENGINEERING_BLOCKED: native executed argv receipts missing')
    if not write_policy_valid:
        raise RuntimeError('ENGINEERING_BLOCKED: native effective scoped write policy not established')
    if any(c.get('model') != requested_model or c.get('effort') != requested_reasoning for c in contexts):
        raise RuntimeError('HUMAN_ACTION_REQUIRED: native worker model/reasoning mismatch')
    fixed_calls = [c for c in commands if c['kind'] == 'fixed_entry']
    if len(fixed_calls) > 1:
        raise RuntimeError('HUMAN_ACTION_REQUIRED: scoped native route requires one fixed invocation')
    if len([c for c in commands if c['kind'] == 'scoped_test']) > 8:
        raise RuntimeError('HUMAN_ACTION_REQUIRED: scoped test call limit exceeded')
    if not fixed_calls:
        raise ScopedPreEntryIncomplete(receipt, output)
    return receipt


def read_request_cwd(call):
    return json.loads((call / 'request.json').read_text(encoding='utf-8'))['cwd']

"""Pre-turn policy check and metered turn/start on the same live native session."""
import datetime as dt
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from .gate_zero import ROOT, native_command, env_clean, write, background_process_options


PARENT_IDENTITY_ENV = ('CODEX_INTERNAL_ORIGINATOR_OVERRIDE', 'CODEX_THREAD_ID')


def worker_environment():
    # A separate worker must not impersonate the desktop parent or inherit its
    # chat identity. Keep authentication and the existing permission policy.
    environment = env_clean()
    for name in PARENT_IDENTITY_ENV:
        environment.pop(name, None)
    return environment


def validate_policy(response, cwd, model, reasoning='max'):
    policy = response.get('sandbox', {})
    roots = response.get('runtimeWorkspaceRoots', [])
    if (response.get('model') != model or response.get('modelProvider') != 'openai'
            or response.get('reasoningEffort') != reasoning
            or Path(response.get('cwd', '')).resolve() != Path(cwd).resolve()
            or policy.get('type') != 'workspaceWrite' or policy.get('networkAccess') is not False
            or policy.get('writableRoots') != [] or not roots
            or any(Path(root).resolve() != Path(cwd).resolve() for root in roots)
            or response.get('approvalPolicy') != 'on-request'
            or response.get('approvalsReviewer') != 'auto_review'):
        raise RuntimeError('ENGINEERING_BLOCKED: native pre-turn policy does not match scoped worker')
    return response['thread']['id']


def thread_parameters(cwd, model, reasoning='max'):
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError('Native worker reasoning must be a non-empty string')
    return {'cwd': str(cwd), 'model': model, 'modelProvider': 'openai',
        'approvalPolicy': 'on-request', 'approvalsReviewer': 'auto_review',
        'sandbox': 'workspace-write', 'ephemeral': False,
        'config': {'windows.sandbox': 'elevated', 'model_reasoning_effort': reasoning,
            'forced_login_method': 'chatgpt', 'features.hooks': False,
            'features.plugins': False, 'features.memories': False, 'features.multi_agent': False,
            'mcp_servers.node_repl.enabled': False,
            'shell_environment_policy.set.PYTHONPATH': str(ROOT),
            'shell_environment_policy.set.PYTHONDONTWRITEBYTECODE': '1'}}


def canonical_event(message, sid):
    """Compatibility view only; the complete original native stream is retained."""
    method, params = message.get('method'), message.get('params', {})
    if params.get('threadId', sid) != sid: return None
    if method == 'thread/started' and params.get('thread', {}).get('id') == sid:
        return {'type': 'thread.started', 'thread_id': sid}
    if method == 'turn/started': return {'type': 'turn.started', 'turn_id': params['turn']['id']}
    if method == 'item/completed':
        item = dict(params.get('item', {}))
        if item.get('type') == 'agentMessage': item['type'] = 'agent_message'
        elif item.get('type') == 'commandExecution':
            item.update(type='command_execution', exit_code=item.get('exitCode'), aggregated_output=item.get('aggregatedOutput', ''))
        return {'type': 'item.completed', 'item': item, 'source': 'native_app_server'}
    if method == 'turn/completed':
        turn = params.get('turn', {})
        if turn.get('status') == 'completed': return {'type': 'turn.completed', 'turn_id': turn.get('id')}
        return {'type': 'turn.failed', 'error': turn.get('error'), 'native_status': turn.get('status')}
    if method == 'error': return {'type': 'error', 'error': params}
    return None


class NativeSession:
    def __init__(self, cwd, model, reasoning='max'):
        self.cwd, self.model, self.reasoning = Path(cwd), model, reasoning
        self.params = thread_parameters(cwd, model, reasoning)
        self.argv = native_command('codex') + ['app-server', '--disable', 'hooks', '--disable', 'plugins',
            '--disable', 'memories', '--disable', 'multi_agent',
            '-c', 'windows.sandbox="elevated"', '-c', 'model_provider="openai"',
            '-c', 'model_reasoning_effort=' + json.dumps(reasoning), '-c', 'forced_login_method="chatgpt"',
            '-c', 'mcp_servers.node_repl.enabled=false']
        self.process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', cwd=cwd, env=worker_environment(),
            **background_process_options())
        self.incoming, self.messages, self.errors = queue.Queue(), [], []
        self.closed = self.turn_sent = False
        self.turn_id = self.session_id = None
        self.last_activity = time.monotonic()
        threading.Thread(target=self._pump, daemon=True).start()
        self.error_thread = threading.Thread(target=self._pump_errors, daemon=True)
        self.error_thread.start()

    def _pump(self):
        for line in self.process.stdout:
            self.last_activity = time.monotonic()
            try: self.incoming.put(json.loads(line))
            except ValueError: self.incoming.put({'transport_error': line})
        self.incoming.put({'transport_error': 'app server closed stdout'})

    def _pump_errors(self):
        for line in self.process.stderr: self.errors.append(line)

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n'); self.process.stdin.flush()

    def receive(self, timeout):
        value = self.incoming.get(timeout=timeout); self.messages.append(value)
        if 'transport_error' in value: raise RuntimeError(value['transport_error'])
        if 'id' in value and 'method' in value:
            raise RuntimeError('ENGINEERING_BLOCKED: native server request requires review: ' + value['method'])
        return value

    def rpc(self, identifier, method, params):
        self.send({'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + 60
        while True:
            value = self.receive(max(.01, deadline - time.monotonic()))
            if value.get('id') == identifier:
                if 'error' in value: raise RuntimeError(json.dumps(value['error']))
                return value['result']
            if time.monotonic() >= deadline: raise TimeoutError('Native initialization timeout')

    def prepare(self, receipt_path):
        self.receipt_path = Path(receipt_path)
        if self.receipt_path.exists():
            self.close(); raise RuntimeError('ENGINEERING_BLOCKED: native preflight already exists; reconcile before retry')
        proof = {'passed': False, 'no_model_turn_sent': True, 'argv': self.argv, 'cwd': str(self.cwd),
            'transport': 'same_live_app_server', 'thread_start_params': self.params,
            'parent_identity_environment_excluded': list(PARENT_IDENTITY_ENV),
            'scope': 'Native configured policy before model dispatch, not OS enforcement attestation.'}
        try:
            self.rpc(1, 'initialize', {'clientInfo': {'name': 'payout-worker', 'version': '2'}, 'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
            response = self.rpc(2, 'thread/start', self.params)
            proof['response'] = response
            self.session_id = validate_policy(response, self.cwd, self.model, self.reasoning)
            if response['thread'].get('originator') != 'payout-worker':
                raise RuntimeError('Native worker inherited an unexpected parent identity')
            proof.update(native_session_id=self.session_id, passed=True)
        except Exception as error:
            proof['error'] = str(error); self.close()
            raise RuntimeError('ENGINEERING_BLOCKED: native pre-turn gate: ' + str(error)) from error
        finally:
            proof.update(recorded_utc=dt.datetime.now(dt.timezone.utc).isoformat(), messages=list(self.messages), stderr=''.join(self.errors))
            write(self.receipt_path, proof)
        return self

    def invoke(self, prompt, folder, inactivity_seconds, cancel_path, timeout=None):
        """Only the existing Controller calls this, after persisting call reservation."""
        if self.closed or self.turn_sent or not self.session_id: raise RuntimeError('Native session unavailable or already used')
        folder = Path(folder)
        write(folder/'request.json', {'argv': self.argv, 'cwd': str(self.cwd), 'transport': 'native_app_server',
            'thread_start_params': self.params, 'native_session_id': self.session_id, 'preflight_path': str(self.receipt_path)})
        started = self.last_activity = time.monotonic()
        result = {'exit_code': None, 'process_reaped': False, 'turn_completed': False, 'transport': 'native_app_server'}
        raw = []
        with (folder/'native_stream.jsonl').open('w', encoding='utf-8') as native, (folder/'stdout.jsonl').open('w', encoding='utf-8') as stdout:
            def record(value):
                native.write(json.dumps(value, ensure_ascii=False) + '\n'); native.flush()
                event = canonical_event(value, self.session_id)
                if event:
                    line = json.dumps(event, ensure_ascii=False) + '\n'; raw.append(line); stdout.write(line); stdout.flush()
            try:
                record({'method':'thread/started','params':{'thread':{'id':self.session_id}},'source':'validated_thread_start_response'})
                self.turn_sent = True
                self.send({'id':3,'method':'turn/start','params':{'threadId':self.session_id,'input':[{'type':'text','text':prompt,'text_elements':[]}]}})
                while True:
                    now = time.monotonic()
                    if Path(cancel_path).exists(): raise InterruptedError('CONTROLLER_CANCELLED')
                    if now-self.last_activity >= inactivity_seconds: raise TimeoutError('CONTROLLER_INACTIVITY_STALL')
                    if timeout is not None and now-started >= timeout: raise TimeoutError('CONTROLLER_TIMEOUT')
                    try: value = self.receive(.25)
                    except queue.Empty: continue
                    record(value)
                    if value.get('id') == 3:
                        if 'error' in value: raise RuntimeError(json.dumps(value['error']))
                        self.turn_id = value['result']['turn']['id']
                    if value.get('method') == 'turn/completed' and value.get('params',{}).get('threadId') == self.session_id:
                        turn = value['params']['turn']
                        result.update(exit_code=0 if turn['status']=='completed' else 1, turn_completed=turn['status']=='completed', native_turn=turn)
                        break
            except Exception as error:
                result['transport_error'] = str(error); self.errors.append('\n'+str(error))
                if self.turn_id:
                    try: self.send({'id':4,'method':'turn/interrupt','params':{'threadId':self.session_id,'turnId':self.turn_id}})
                    except (OSError, ValueError): pass
            finally:
                self.close()
                while not self.incoming.empty():
                    value=self.incoming.get_nowait()
                    if 'transport_error' not in value: record(value)
                result.update(elapsed_s=time.monotonic()-started, process_reaped=True, server_exit_code=self.process.returncode)
        (folder/'stderr.txt').write_text(''.join(self.errors), encoding='utf-8')
        return result, ''.join(raw)

    def close(self):
        if self.closed: return
        self.closed = True
        try:
            self.process.stdin.close(); self.process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
        self.error_thread.join(timeout=2)


def prepare_session(cwd, model, receipt_path, reasoning='max'):
    return NativeSession(cwd, model, reasoning).prepare(receipt_path)

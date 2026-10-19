"""OpenCode session runner (current CLI API, no screen automation).

Each phase uses a FRESH `opencode run` invocation, i.e. a fresh session and
fresh context. The controller never passes --continue/--session, so the
reviewer cannot inherit implementation context.

Tested against: opencode v2.0.10 (`opencode run --format json ...`).
JSON parsing is best-effort; the raw output is always preserved.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from .runtime_outcome import runtime_errors, error_kind


class BaseRunner:
    """Protocol: run_phase(phase, prompt, attachments=(), agent=None) -> dict."""

    def run_phase(
        self,
        phase: str,
        prompt: str,
        attachments: tuple[str, ...] = (),
        agent: str | None = None,
    ) -> dict:
        raise NotImplementedError


def _iter_events(stdout: str):
    """Yield one event per JSON value: whole-doc JSON or JSONL event stream."""
    raw = (stdout or "").strip()
    if not raw:
        return
    try:
        yield json.loads(raw)
        return
    except (json.JSONDecodeError, ValueError):
        pass
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue


def _texts_from_object(payload) -> list[str]:
    if isinstance(payload, dict):
        for key in ("text", "content", "message", "output", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return [value]
        for key in ("session", "response"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                for sub in ("text", "content", "message"):
                    if isinstance(nested.get(sub), str):
                        return [nested[sub]]
    if isinstance(payload, list):
        texts = [p.get("text", "") for p in payload if isinstance(p, dict)]
        return [t for t in texts if t]
    return []


def extract_session_id(stdout: str) -> str | None:
    """Best-effort session id from `opencode run --format json` output."""
    for event in _iter_events(stdout):
        if isinstance(event, dict):
            sid = event.get("sessionID") or event.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
    return None


def classify_provider_error(stdout: str, stderr: str) -> str | None:
    """Only top-level runtime errors; arbitrary stderr/prose is not provenance."""
    errors=runtime_errors(stdout)
    kinds={error_kind(e) for e in errors}
    for kind in ('auth','permission-denied','rate-limit','provider-error','runtime-error'):
        if kind in kinds: return kind
    return None


def extract_retry_after_s(stdout: str) -> float | None:
    """Provider-advised wait in seconds, if exposed. Capped at 300s."""
    for error in runtime_errors(stdout):
        if error_kind(error)!='rate-limit': continue
        for key,divisor in [('retryAfterMs',1000),('retry_after_ms',1000),('retryAfter',1),('retry_after',1)]:
            value=error.get(key)
            if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<value/divisor<=300:
                return value/divisor
    return None


def _input_strings(value, depth: int = 0) -> list[str]:
    """All string values inside a tool input mapping (paths, commands)."""
    found: list[str] = []
    if depth > 3:
        return found
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for item in value.values():
            found.extend(_input_strings(item, depth + 1))
    if isinstance(value, list):
        for item in value:
            found.extend(_input_strings(item, depth + 1))
    return found


def extract_tool_calls(stdout: str) -> list[dict]:
    """Observable tool invocations from the JSONL event stream.

    Returns [{tool, args}] where args are the input strings. Evidence-based:
    lets the controller block ATTEMPTED forbidden actions instead of
    scanning prose for keywords.
    """
    calls: list[dict] = []
    for event in _iter_events(stdout or ""):
        if not isinstance(event, dict) or event.get("type") != "tool_use":
            continue
        part = event.get("part", {})
        if not isinstance(part, dict):
            continue
        tool = part.get("tool")
        if not isinstance(tool, str) or not tool:
            continue
        state = part.get("state", {})
        raw_input = part.get("input")
        if raw_input is None and isinstance(state, dict):
            raw_input = state.get("input")
        calls.append({"tool": tool, "args": _input_strings(raw_input)})
    return calls


def extract_text(stdout: str) -> str:
    """Best-effort assistant text from `opencode run --format json`.

    Handles a single JSON object, a list shape, and the JSONL event stream
    (`{"type":"text","part":{"text":"..."}}` per line). Falls back to raw.
    """
    raw = (stdout or "").strip()
    if not raw:
        return ""
    texts: list[str] = []
    saw_event = False
    for event in _iter_events(raw):
        saw_event = True
        if isinstance(event, dict) and event.get("type") == "text":
            part = event.get("part", {})
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        else:
            texts.extend(_texts_from_object(event))
    joined = "\n".join(t for t in texts if t and t.strip()).strip()
    if joined:
        return joined
    # No substantive assistant text: "" for event streams (never raw noise,
    # so evidence shows "(empty plan)" instead of a stream dump); raw text
    # back when the output was not JSON at all.
    if saw_event:
        return ""
    return raw


class OpenCodeRunner(BaseRunner):
    """Real runner: shells out to `opencode run` (one fresh session per phase)."""

    def __init__(
        self,
        binary: str = "opencode",
        model: str | None = None,
        variant: str | None = None,
        timeout_s: int = 1800,
        cwd: str | None = None,
        standalone: bool = False,
    ) -> None:
        # Resolve the binary (Windows npm shims like `opencode` are .CMD/.PS1
        # files that CreateProcess cannot launch by bare name).
        self.binary = shutil.which(binary) or binary
        self.model = model
        self.variant = variant
        self.timeout_s = timeout_s
        self.cwd = cwd
        self.standalone = standalone

    @property
    def provider(self) -> str | None:
        # Fix 3: provider derived from the explicit model id (`provider/model`).
        if self.model and "/" in self.model:
            return self.model.split("/")[0] or None
        return None

    def effective_model(self) -> str | None:
        if self.model and self.variant and "#" not in self.model:
            return f"{self.model}#{self.variant}"
        return self.model

    def build_command(
        self,
        phase: str,
        run_id: str,
        prompt: str,
        attachments: tuple[str, ...] = (),
        agent: str | None = None,
    ) -> list[str]:
        cmd = [self.binary, "run"]
        if self.standalone:
            cmd += ["--standalone"]
        cmd += ["--format", "json"]
        cmd += ["--title", f"agent-loop:{run_id}:{phase}"]
        if agent:
            cmd += ["--agent", agent]
        effective = self.effective_model()
        if effective:
            cmd += ["--model", effective]
        for path in attachments:
            if path and Path(path).is_file():
                cmd += ["--file", str(path)]
        cmd += [prompt]
        return cmd

    def run_phase(self, phase, prompt, attachments=(), agent=None, run_id="n/a"):
        cmd = self.build_command(phase, run_id, prompt, attachments, agent)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                cwd=self.cwd,
            )
            return {
                "phase": phase,
                "agent": agent,
                "provider": self.provider,
                "model": self.model,
                "variant": self.variant,
                "session_id": extract_session_id(proc.stdout),
                "output": extract_text(proc.stdout),
                "raw_stdout": proc.stdout[-20000:],
                "raw_stderr": proc.stderr[-5000:],
                "exit_code": proc.returncode,
                "provider_error": classify_provider_error(proc.stdout, proc.stderr),
                "fresh_session": True,
                "command": cmd[:-1] + ["<prompt>"],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "phase": phase,
                "agent": agent,
                "provider": self.provider,
                "model": self.model,
                "variant": self.variant,
                "session_id": None,
                "output": "",
                "raw_stdout": "",
                "raw_stderr": f"[RUNNER FAILED] {exc}",
                "exit_code": None,
                "provider_error": None,
                "fresh_session": True,
                "command": [self.binary, "run", "<unavailable>"],
                "error": str(exc),
            }


class DryRunRunner(BaseRunner):
    """Harmless stand-in: no LLM, no subprocess, no file writes by the agent.

    Used for `--dry-run` demos and environments without OpenCode credentials.
    """

    CANNED = {
        "planner": (
            "# Plan (dry-run)\n\n1. Inspect the task scope.\n"
            "2. Make the minimal change.\n3. Run verification.\n\n"
            "Acceptance criteria map 1:1 to verification commands below. "
            "No credentials needed; scope and methodology unchanged."
        ),
        "implement": (
            "DRY-RUN: no files were modified. This placeholder stands in for "
            "the implementation session."
        ),
        "reviewer": (
            "Findings: dry-run placeholder; no code changed, "
            "nothing to flag. Evidence inspected: TASK.md, git diff (empty), "
            "verifier output."
        ),
        "repair": (
            "DRY-RUN: no repair performed (attempt placeholder, no file writes)."
        ),
    }

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run_phase(self, phase, prompt, attachments=(), agent=None, **kwargs):
        key = phase if phase in self.CANNED else "implement"
        self.calls.append(
            {
                "phase": phase,
                "agent": agent,
                "fresh_session": True,
                "attachments": list(attachments),
            }
        )
        return {
            "phase": phase,
            "agent": agent,
            "session_id": f"dry-run-{phase}-{len(self.calls)}",
            "output": self.CANNED[key],
            "raw_stdout": self.CANNED[key],
            "raw_stderr": "",
            "exit_code": 0,
            "fresh_session": True,
            "dry_run": True,
        }

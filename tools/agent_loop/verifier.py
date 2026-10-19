"""Deterministic external verifier (no LLM calls).

Runs repository-configured shell commands, records exit codes and output.
Refuses forbidden commands (push/deploy/secrets/test-weakening) before running.
"""

from __future__ import annotations

import re
import subprocess
import time

DEFAULT_TIMEOUT_S = 600
OUTPUT_CAP_CHARS = 20000


def is_forbidden(command: str, forbidden_patterns: list[str]) -> str | None:
    """Return the matching forbidden pattern, or None if the command is allowed."""
    for pattern in forbidden_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern
    return None


def run_one(command: str, cwd: str, timeout_s: int) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-OUTPUT_CAP_CHARS:],
            "stderr": proc.stderr[-OUTPUT_CAP_CHARS:],
            "timed_out": False,
            "duration_s": round(time.time() - started, 2),
            "blocked": False,
            "block_reason": None,
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return {
            "command": command,
            "exit_code": None,
            "stdout": out[-OUTPUT_CAP_CHARS:],
            "stderr": (err + f"\n[TIMEOUT after {timeout_s}s]")[-OUTPUT_CAP_CHARS:],
            "timed_out": True,
            "duration_s": round(time.time() - started, 2),
            "blocked": False,
            "block_reason": None,
        }
    except OSError as exc:
        return {
            "command": command,
            "exit_code": None,
            "stdout": "",
            "stderr": f"[LAUNCH FAILED] {exc}",
            "timed_out": False,
            "duration_s": round(time.time() - started, 2),
            "blocked": True,
            "block_reason": f"os-error: {exc}",
        }


def run_commands(
    commands: list[str],
    cwd: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    forbidden_patterns: list[str] | None = None,
    allowlist: list[str] | None = None,
) -> dict:
    """Run commands sequentially. Forbidden/allowlisted-out commands are refused.

    Returns a verification record: {"passed": bool, "results": [...], ...}.
    A refused forbidden command fails the run without being executed.
    """
    forbidden_patterns = forbidden_patterns or []
    results: list[dict] = []
    refused: list[dict] = []
    for command in commands:
        hit = is_forbidden(command, forbidden_patterns)
        if hit is not None:
            refused.append({"command": command, "pattern": hit})
            results.append(
                {
                    "command": command,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": f"[REFUSED] matches forbidden pattern: {hit}",
                    "timed_out": False,
                    "duration_s": 0.0,
                    "blocked": True,
                    "block_reason": f"forbidden: {hit}",
                }
            )
            continue
        if allowlist is not None and command not in allowlist:
            refused.append({"command": command, "pattern": "dry-run-allowlist"})
            results.append(
                {
                    "command": command,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "[REFUSED] not in dry-run allowlist; not executed.",
                    "timed_out": False,
                    "duration_s": 0.0,
                    "blocked": True,
                    "block_reason": "not-in-dry-run-allowlist",
                }
            )
            continue
        results.append(run_one(command, cwd, timeout_s))
    runnable = [r for r in results if not r["blocked"]]
    passed = bool(runnable) and all(r["exit_code"] == 0 for r in runnable)
    if refused:
        passed = False
    return {
        "passed": passed,
        "refused": refused,
        "results": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in runnable if r["exit_code"] == 0),
            "failed": sum(1 for r in runnable if r["exit_code"] not in (0, None)),
            "blocked_or_refused": len(results) - len(runnable),
        },
    }

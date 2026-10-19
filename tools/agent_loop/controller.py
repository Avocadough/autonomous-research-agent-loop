"""Explicit state-machine controller for the minimal local agent loop.

Flow: TASK.md -> planner -> implementation -> deterministic verifier
      -> independent read-only reviewer -> repair (max 2) -> final state.

States: PENDING PLANNING IMPLEMENTING VERIFYING REVIEWING REPAIRING
        DONE BLOCKED NEEDS_DECISION BUDGET_EXCEEDED
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import verifier as verifier_mod
from .opencode_runner import extract_retry_after_s, extract_tool_calls

STATES = (
    "PENDING",
    "PLANNING",
    "IMPLEMENTING",
    "VERIFYING",
    "REVIEWING",
    "REPAIRING",
    "DONE",
    "BLOCKED",
    "NEEDS_DECISION",
    "BUDGET_EXCEEDED",
)
TERMINAL = frozenset({"DONE", "BLOCKED", "NEEDS_DECISION", "BUDGET_EXCEEDED"})
MAX_REPAIRS_DEFAULT = 2
DIFF_CAP_CHARS = 300000

# Commands the loop must never execute (req 8). Checked before any run.
FORBIDDEN_DEFAULT = [
    r"git\s+push\b",
    r"git\s+publish\b",
    r"\bgh\s+release\b",
    r"npm\s+publish\b",
    r"kubectl\s+(apply|delete|create)\b",
    r"terraform\s+(apply|destroy)\b",
    r"fly\s+(deploy|launch)\b",
    r"vercel\b.*--prod\b",
    r"heroku\b.*\b(release|deploy|push)\b",
    r"docker\s+push\b",
    r"aws\b.*\b(deploy|publish|update-function-code)\b",
    r"\bdeploy\b",
    r"printenv\b",
    r"\bopencode\s+auth\b",
    r"\.env\b",
    r"AWS_SECRET|AZURE_|OPENAI_API_KEY|ANTHROPIC_API_KEY|GH_TOKEN|GITHUB_TOKEN",
    r"pytest\b.*--deselect\b",
    r"pytest\b.*-k\s+[\"']not\b",
    r"pytest\b.*--ignore\b",
    r"npm\s+test\b.*--.*updateSnapshot\b",
    r"-u\b.*snap",
]

# Change V0.3: the ONLY semantic text field the controller parses is the
# reviewer's explicit verdict token. Nothing else in any LLM prose gates state.
#
# V1: text-token verdicts are removed entirely. Control comes from validated
# native structured output (STRUCTURED_REVIEW) or, when the local OpenCode
# installation provably lacks that capability, from deterministic hard gates
# with the reviewer as advisory evidence only (ADVISORY_REVIEW).
REVIEW_STRUCTURED = "STRUCTURED_REVIEW"
REVIEW_ADVISORY = "ADVISORY_REVIEW"
STRUCTURED_VERDICTS = ("PASS", "CHANGES_REQUIRED", "BLOCKED")


def probe_structured_capability(repo_root: str, binary: str = "opencode") -> dict:
    """Static capability probe (no LLM calls).

    Native JSON-Schema structured output is supported only if the local
    OpenCode exposes it: a schema flag on `opencode run`, or a schema field
    in the SDK prompt API. On this installation neither exists, so the
    probe reports unsupported with concrete reasons.
    """
    import shutil as _shutil

    reasons: list[str] = []
    help_text = ""
    resolved = _shutil.which(binary) or binary
    try:
        proc = subprocess.run(
            [resolved, "run", "--help"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        help_text = proc.stdout or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        reasons.append(f"could not inspect opencode CLI: {exc}")
    if not re.search(r"schema|structured-output|response-format|json-schema",
                     help_text, re.IGNORECASE):
        reasons.append("`opencode run` exposes no structured-output/schema flag")
    sdk_hit = False
    sdk_base = Path(repo_root) / ".opencode" / "node_modules" / "@opencode-ai" / "sdk" / "dist"
    if sdk_base.is_dir():
        for path in sdk_base.rglob("*"):
            if not path.is_file() or not path.name.endswith(".d.ts"):
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"PromptInput\s*=\s*\{[^}]*schema", content):
                sdk_hit = True
                break
    if not sdk_hit:
        reasons.append("local @opencode-ai/sdk prompt API exposes no schema field")
    return {"supported": not reasons, "reasons": reasons}

REVIEWER_INSTRUCTIONS = """\
You are an INDEPENDENT, READ-ONLY reviewer with a FRESH context.
You have NOT seen the implementation session; do NOT trust any summary.

Rules:
- Do NOT modify any files. Do NOT run shell commands.
- Inspect TASK.md, the actual git diff, the listed source files, and the
  verifier output. An implementation summary is NOT evidence.
- Ground every finding in concrete file/line or diff-hunk evidence.

Write plain prose only: findings (with evidence), an acceptance-criteria
checklist, and unresolved limitations. No control tokens are required;
a deterministic controller evaluates your prose as advisory evidence only.
"""

IMPLEMENTATION_RULES = """\
Hard constraints (violations escalate to NEEDS_DECISION, never bypass):
- NEVER: git push, deploy, change secrets/credentials, or touch production.
- NEVER weaken, delete, or skip acceptance tests to obtain PASS.
- Stay within the Allowed Scope in TASK.md. If the scope, acceptance
  criteria, or methodology must change, or credentials/production access
  are required, STOP and say NEEDS_DECISION with reasons instead of coding.
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_failure_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"0x[0-9a-f]+", "0x?", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(:\d{2})?", "<ts>", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[-800:]


def failure_signature(verification: dict) -> str:
    parts = []
    for res in verification.get("results", []):
        if res.get("blocked") or res.get("exit_code") == 0:
            continue
        tail = normalize_failure_text(
            (res.get("stderr") or "") + "\n" + (res.get("stdout") or "")
        )
        parts.append(f"{res.get('command')}||exit={res.get('exit_code')}||{tail}")
    digest = hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]
    return f"failsig:{len(parts)}:{digest}"


def forbidden_action_attempts(stdout: str, forbidden_patterns: list[str]) -> list[dict]:
    """Observable forbidden ACTION attempts from tool_use events.

    Evidence-based safety: blocks what the agent TRIED to do (shell
    commands, file paths in tool inputs), never prose keywords.
    """
    hits: list[dict] = []
    for call in extract_tool_calls(stdout or ""):
        for arg in call.get("args", []):
            hit = verifier_mod.is_forbidden(arg, forbidden_patterns)
            if hit is not None:
                hits.append({"tool": call.get("tool"), "arg": arg[:300],
                             "pattern": hit})
    return hits


def split_task_sections(task_text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in task_text.splitlines():
        heading = re.match(r"^#+\s+(.+?)\s*$", line)
        if heading:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = heading.group(1).strip().lower()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def extract_verification_commands(task_text: str) -> list[str]:
    """Take command-looking lines from the TASK.md Verification section."""
    sections = split_task_sections(task_text)
    body = sections.get("verification", "")
    commands: list[str] = []
    for line in body.splitlines():
        stripped = re.sub(r"^[-*\d.)\s\[\]xX]+\s*", "", line).strip().strip("`")
        if not stripped or stripped.startswith("#"):
            continue
        looks_like_cmd = (
            stripped.startswith(("py ", "python ", "pytest ", "npm ", "node ", "git "))
            or re.match(r"^[a-z0-9_.-]+\s+\S", stripped, re.IGNORECASE)
        ) and len(stripped) < 500
        if looks_like_cmd:
            commands.append(stripped)
    # De-duplicate, keep order.
    return list(dict.fromkeys(commands))


def detect_test_weakening(diff_text: str) -> list[str]:
    findings: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-") and re.search(
            r"\bassert\b|def\s+test_|expect\(|\.should| Rt|it\(['\"]", line
        ):
            findings.append(f"removed test/assertion line: {line[1:].strip()[:160]}")
        if line.startswith("+") and re.search(
            r"--deselect|--ignore|-k\s+[\"']not|updateSnapshot|skip\s*\(|xit\(|xdescribe\(",
            line,
        ):
            findings.append(f"added test-weakening line: {line[1:].strip()[:160]}")
    return findings[:20]


def changed_files_from_status(status_short: str) -> list[str]:
    files: list[str] = []
    for line in (status_short or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            files.append(parts[-1].replace('"', ""))
    return sorted(set(files))


def collect_git_state(repo_root: str) -> dict:
    def _run(args: list[str]) -> str:
        try:
            proc = subprocess.run(
                ["git"] + args,
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            return proc.stdout if proc.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    commit = _run(["rev-parse", "--short", "HEAD"]).strip()
    # --untracked-files=all lists new files individually so run deltas,
    # scope checks, and reviewer attachments see them (not just top dirs).
    status = _run(["status", "--short", "--untracked-files=all"])
    diff = _run(["diff", "--", "."])
    if len(diff) > DIFF_CAP_CHARS:
        diff = diff[:DIFF_CAP_CHARS] + "\n[TRUNCATED]"
    return {"commit": commit or "n/a", "status_short": status, "diff": diff}


class RunStore:
    """Evidence writer for .agent-loop/runs/<run-id>/ (req 9)."""

    FILES = ("state.json", "plan.md", "verification.json", "review.md", "result.md")

    def __init__(self, runs_dir: str, run_id: str) -> None:
        self.dir = Path(runs_dir) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, content: str) -> str:
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    def write_json(self, name: str, payload: dict) -> str:
        return self.write(name, json.dumps(payload, indent=2, ensure_ascii=False))


class Controller:
    """Owns the state machine and all phase transitions."""

    def __init__(
        self,
        repo_root: str,
        task_path: str,
        run_id: str,
        runner,
        runs_dir: str | None = None,
        config: dict | None = None,
        max_repairs: int = MAX_REPAIRS_DEFAULT,
        dry_run: bool = False,
        capability_probe=None,
    ) -> None:
        self.repo_root = repo_root
        self.task_path = task_path
        self.run_id = run_id
        self.runner = runner
        self.config = config or {}
        self.max_repairs = max_repairs
        self.dry_run = dry_run
        self.forbidden = self.config.get("forbidden_patterns", FORBIDDEN_DEFAULT)
        self.agents = self.config.get(
            "agents",
            # Planning uses the read-only plan agent; implementation uses
            # build. Review defaults to plan as well: project subagents are
            # rejected by this provider tier (403), and every invocation is
            # still a fresh session with read-only instructions.
            {"planner": "plan", "implement": "build", "repair": "build",
             "reviewer": "plan"},
        )
        self.store = RunStore(runs_dir or str(Path(repo_root) / ".agent-loop" / "runs"), run_id)
        self.task_text = Path(task_path).read_text(encoding="utf-8")
        self.verification_commands = extract_verification_commands(self.task_text) or list(
            self.config.get("default_verification", [])
        )
        self.state = "PENDING"
        self.history: list[dict] = []
        self.repairs_used = 0
        self.last_signature: str | None = None
        self.plan_text = ""
        self.verification: dict = {}
        self.review_text = ""
        self.review_verdict: str | None = None
        self.git_state: dict = {"commit": "n/a", "status_short": "", "diff": ""}
        self.git_baseline: dict = {"commit": "n/a", "status_short": "", "diff": ""}
        self.git_after_plan: dict | None = None
        self.git_after_review: dict | None = None
        self._review_before_state: dict | None = None
        self.escalation: dict | None = None
        self.transport_retries: list[dict] = []
        # V1 review mode: "auto" (default) resolves via capability probe;
        # "structured" refuses to run when unsupported (no silent fallback).
        self.review_mode_config = str((config or {}).get("review_mode", "auto")).lower()
        self.capability_probe = capability_probe or (
            lambda: probe_structured_capability(self.repo_root,
                                               (config or {}).get("opencode_binary", "opencode")))
        self.review_mode: str | None = None
        self.review_capability: dict | None = None
        self.structured_verdict: str | None = None
        self.review_transport_failed = False
        self._forced_result: dict | None = None
        self._transition("PENDING", "run initialized")

    # -- state helpers -------------------------------------------------
    def _transition(self, to: str, reason: str) -> None:
        assert to in STATES, f"unknown state {to}"
        self.state = to
        self.history.append({"state": to, "at": utc_now(), "reason": reason})
        self._persist_state()

    def _persist_state(self) -> None:
        self.store.write_json(
            "state.json",
            {
                "run_id": self.run_id,
                "state": self.state,
                "repairs_used": self.repairs_used,
                "max_repairs": self.max_repairs,
                "task": self.task_path,
                "dry_run": self.dry_run,
                "history": self.history,
                "escalation": self.escalation,
                "transport_retries": self.transport_retries,
            },
        )

    def _transcript(self, phase: str, result: dict, prompt: str) -> None:
        # Fix 3: every LLM invocation records provider/model/variant/session.
        line = json.dumps(
            {
                "at": utc_now(),
                "phase": phase,
                "agent": result.get("agent"),
                "provider": result.get("provider")
                or getattr(self.runner, "provider", None),
                "model": result.get("model")
                or getattr(self.runner, "model", None),
                "variant": result.get("variant")
                or getattr(self.runner, "variant", None),
                "session_id": result.get("session_id"),
                "fresh_session": result.get("fresh_session", True),
                "prompt_chars": len(prompt),
                "output_chars": len(result.get("output") or ""),
                "exit_code": result.get("exit_code"),
                "provider_error": result.get("provider_error"),
                "transport_attempt": result.get("transport_attempt", 0),
                "stderr_tail": (result.get("raw_stderr") or "")[-500:],
            },
            ensure_ascii=False,
        )
        with open(self.store.dir / "transcript.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _phase(
        self, phase: str, prompt: str, attachments: tuple[str, ...] = (),
        agent: str | None = None,
    ) -> dict:
        kwargs: dict = {}
        if hasattr(self.runner, "run_phase"):
            import inspect as _inspect

            if "run_id" in _inspect.signature(self.runner.run_phase).parameters:
                kwargs["run_id"] = self.run_id
        # Fix 2: bounded transport retries for rate-limits. These NEVER
        # consume the implementation repair budget (repairs_used untouched).
        max_transport = int(self.config.get("transport_max_retries", 3))
        backoffs = list(self.config.get("transport_backoff_s", [30, 60, 120]))
        attempt = 0
        while True:
            result = self.runner.run_phase(phase, prompt, attachments, agent, **kwargs)
            result["transport_attempt"] = attempt
            self._transcript(phase, result, prompt)
            if result.get("provider_error") != "rate-limit":
                return result
            if attempt >= max_transport:
                result["transport_exhausted"] = True
                return result
            advised = extract_retry_after_s(result.get("raw_stdout") or "")
            wait = advised if advised is not None else (
                backoffs[min(attempt, len(backoffs) - 1)] if backoffs else 0)
            wait = max(0, min(wait, 300))
            self.transport_retries.append(
                {"phase": phase, "attempt": attempt + 1,
                 "wait_s": wait, "reason": "rate-limit",
                 "advised": advised is not None, "at": utc_now()})
            self._persist_state()
            time.sleep(wait)
            attempt += 1

    def _transport_blocked(self) -> dict:
        detail = (f"provider rate-limited after {len(self.transport_retries)} "
                  f"transport retries; implementation repair budget untouched "
                  f"({self.repairs_used}/{self.max_repairs})")
        self.escalation = {"category": "provider-rate-limited", "detail": detail}
        self._transition("BLOCKED", "provider rate-limit persisted after transport retries")
        return self._finish("BLOCKED")

    # -- prompts -------------------------------------------------------
    def _planner_prompt(self) -> str:
        # Change 2: the planner has ONE job -- a useful non-empty plan.
        # No control contract, no keyword gating; boundaries are enforced
        # externally (scope diff, verifier, reviewer, transport errors).
        return (
            "You are a PLANNER. Read TASK.md and produce a short, concrete "
            "implementation plan. Do not implement anything and do not modify "
            "any files.\n\n"
            "## TASK.md\n" + self.task_text + "\n\n"
            "Output markdown: steps, files to touch (must stay within the "
            "Allowed Scope in TASK.md), how each acceptance criterion will be "
            "verified, and risks/open questions."
        )

    def _implement_prompt(self) -> str:
        return (
            "You are the IMPLEMENTER. Execute the plan against TASK.md.\n\n"
            f"{IMPLEMENTATION_RULES}\n"
            "## Plan\n" + (self.plan_text or "(no plan)") + "\n\n## TASK.md\n"
            + self.task_text + "\n\nMake the minimal change that satisfies the "
            "acceptance criteria. Do not summarize as evidence; the deterministic "
            "verifier and git diff are the evidence."
        )

    def _repair_prompt(self, attempt: int) -> str:
        failures = json.dumps(self.verification, indent=2, ensure_ascii=False)[:12000]
        return (
            f"You are the REPAIRER (attempt {attempt}/{self.max_repairs}). Fix only "
            "the failures below; do not redesign.\n\n" f"{IMPLEMENTATION_RULES}\n"
            "## Verifier output (deterministic, authoritative)\n" + failures
            + "\n\n## Reviewer findings\n" + (self.review_text or "(none)")[:8000]
            + "\n\n## Plan\n" + (self.plan_text or "(none)")[:6000]
            + "\n\n## TASK.md\n" + self.task_text
            + "\n\nIf the same failure cannot be fixed with new evidence, or the fix "
            "needs scope/methodology/credential changes, output NEEDS_DECISION."
        )

    def _reviewer_prompt(self) -> str:
        files = self._delta_files()
        return (
            f"{REVIEWER_INSTRUCTIONS}\n## TASK.md\n" + self.task_text
            + "\n\n## Changed files\n" + (", ".join(files) or "(none)")
            + "\n\n## Git diff (authoritative implementation evidence)\n"
            + (self.git_state.get("diff") or "(empty diff)")[:60000]
            + "\n\n## Verifier output (deterministic, authoritative)\n"
            + json.dumps(self.verification, indent=2, ensure_ascii=False)[:20000]
            + "\n\nRelevant source files are attached; read them directly."
        )

    # -- main loop -----------------------------------------------------
    def run(self) -> dict:
        # Baseline worktree BEFORE any phase: scope/files-changed are deltas.
        self.git_baseline = collect_git_state(self.repo_root)
        self.git_state = self.git_baseline
        # PLANNING
        self._transition("PLANNING", "planner session started (fresh context)")
        planned = self._phase(
            "planner", self._planner_prompt(),
            attachments=(self.task_path,), agent=self.agents.get("planner"),
        )
        if planned.get("transport_exhausted"):
            return self._transport_blocked()
        # Change 2: planner prose never gates state. Non-empty plan proceeds;
        # a failed/empty planner call stops safely as BLOCKED.
        if not (planned.get("output") or "").strip():
            detail = (f"planner returned no usable plan "
                      f"(exit_code={planned.get('exit_code')}, "
                      f"provider_error={planned.get('provider_error')})")
            self.escalation = {"category": "planner-failed", "detail": detail,
                               "evidence": (planned.get("raw_stderr") or "")[-2000:]}
            self._transition("BLOCKED", "planner call failed or returned empty plan")
            return self._finish("BLOCKED")
        self.plan_text = planned.get("output", "")
        self.store.write("plan.md", self.plan_text or "(empty plan)")
        # Change 1: snapshot right after planning -- proves the planner
        # wrote nothing (planning delta must stay empty).
        self.git_after_plan = collect_git_state(self.repo_root)
        # V1 ADVISORY hard gate: planner-caused project-file delta blocks.
        planning_delta = self._planning_delta_files()
        if planning_delta:
            return self._finish_needs_decision(
                "planner-wrote-files",
                f"planner modified project files: {', '.join(planning_delta[:10])}",
                self.plan_text,
            )

        # IMPLEMENTING
        self._transition("IMPLEMENTING", "implementation session started (fresh context)")
        implemented = self._phase(
            "implement", self._implement_prompt(),
            attachments=(self.task_path,), agent=self.agents.get("implement"),
        )
        if implemented.get("transport_exhausted"):
            return self._transport_blocked()
        # V0.3: implementer prose never gates state. Only an OBSERVED
        # forbidden action attempt (tool evidence) escalates here.
        attempts = forbidden_action_attempts(
            implemented.get("raw_stdout", ""), self.forbidden)
        if attempts:
            return self._finish_needs_decision(
                "forbidden-action-attempted",
                f"implementer attempted forbidden action(s): {attempts[0]}",
                implemented.get("output", ""),
            )
        self.git_state = collect_git_state(self.repo_root)
        self.store.write("diff.patch", self.git_state["diff"] or "(empty diff)")
        scope_hit = self._scope_violation()
        if scope_hit:
            return self._finish_needs_decision(
                "scope-exceeded",
                f"changed files outside Allowed Scope: {scope_hit}",
                implemented.get("output", ""),
            )

        # VERIFY -> REVIEW -> (REPAIR?) loop
        first_verify = self._do_verify("initial implementation")
        if first_verify is not None:  # terminal escalation already handled
            return first_verify

        while True:
            reviewed = self._do_review()
            if reviewed is not None:
                return reviewed
            # _do_review returns None only when a repair was started; re-verify.
            reverified = self._do_verify(f"repair attempt {self.repairs_used}")
            if reverified is not None:
                return reverified

    def _verification_commands_for_run(self) -> list[str]:
        if self.dry_run:
            allowlist = self.config.get("dry_run_allowlist", [])
            return [c for c in self.verification_commands if c in allowlist] or list(
                allowlist[:2]
            )
        return self.verification_commands

    def _do_verify(self, reason: str):
        self._transition("VERIFYING", f"deterministic verifier: {reason}")
        commands = self._verification_commands_for_run()
        allowlist = self.config.get("dry_run_allowlist") if self.dry_run else None
        self.verification = verifier_mod.run_commands(
            commands,
            cwd=self.repo_root,
            timeout_s=int(self.config.get("verifier_timeout_s", 600)),
            forbidden_patterns=self.forbidden,
            allowlist=allowlist,
        )
        self.verification["commands_source"] = "TASK.md Verification" if \
            extract_verification_commands(self.task_text) else "repository config default"
        self.store.write_json("verification.json", self.verification)
        if self.verification.get("refused"):
            return self._finish_needs_decision(
                "forbidden-command",
                f"verifier refused forbidden command(s): {self.verification['refused']}",
                "",
            )
        if not self.verification.get("results"):
            self.escalation = {"category": "no-verification",
                               "detail": "no verification commands defined"}
            self._transition("BLOCKED", "no verification commands defined")
            return self._finish("BLOCKED")
        sig = failure_signature(self.verification)
        if not self.verification["passed"] and self.last_signature == sig:
            return self._finish_needs_decision(
                "same-failure-repeat",
                "same failure repeats without new evidence",
                sig,
            )
        self.last_signature = sig if not self.verification["passed"] else self.last_signature
        if self.verification["passed"]:
            self.last_signature = None
        return None

    def _resolve_review_mode(self) -> str | None:
        """Resolve STRUCTURED vs ADVISORY. Returns None when the run must stop.

        Forced "structured" with no local capability is an explicit refusal
        (BLOCKED), never a silent fallback to advisory.
        """
        if self.review_mode is not None:
            return self.review_mode
        self.review_capability = self.capability_probe()
        self.store.write_json("review_capability.json", {
            "run_id": self.run_id,
            "at": utc_now(),
            "configured": self.review_mode_config,
            "capability": self.review_capability,
        })
        if self.review_mode_config == "structured":
            if self.review_capability.get("supported"):
                self.review_mode = REVIEW_STRUCTURED
            else:
                self.escalation = {
                    "category": "review-mode-unavailable",
                    "detail": ("review_mode=structured was forced but this "
                               "installation exposes no native structured "
                               "output; refusing silent fallback. Reasons: "
                               f"{self.review_capability.get('reasons')}")}
                self._transition("BLOCKED", "forced structured review unsupported")
                self._forced_result = self._finish("BLOCKED")
                return None
        elif self.review_mode_config == "advisory":
            self.review_mode = REVIEW_ADVISORY
        else:  # auto
            self.review_mode = (REVIEW_STRUCTURED
                                if self.review_capability.get("supported")
                                else REVIEW_ADVISORY)
        return self.review_mode

    def _do_review(self):
        self._transition("REVIEWING", "independent read-only reviewer (fresh context)")
        mode = self._resolve_review_mode()
        if mode is None:  # forced-structured refusal already finished
            return self._forced_result
        git_before_review = collect_git_state(self.repo_root)
        review = self._phase(
            "reviewer", self._reviewer_prompt(),
            attachments=tuple(a for a in self._review_attachments() if a),
            agent=self.agents.get("reviewer"),
        )
        self.review_text = review.get("output", "")
        # Review-delta: proves the reviewer wrote nothing (mirrors planning delta).
        self.git_after_review = collect_git_state(self.repo_root)
        self._review_before_state = git_before_review
        review_delta = self._review_delta_files()
        if review_delta:
            # A reviewer write is a controller failure, not a decision request.
            self.escalation = {
                "category": "reviewer-wrote-files",
                "detail": f"reviewer modified project files: {', '.join(review_delta[:10])}",
                "evidence": self.review_text[:2000],
            }
            self._transition("BLOCKED", "reviewer wrote project files")
            return self._finish("BLOCKED")
        # Fresh-context + read-only audit trail: reviewer must be a new
        # session and must not be the implementing agent.
        self.store.write_json(
            "review_meta.json",
            {
                "agent": review.get("agent"),
                "fresh_session": review.get("fresh_session", True),
                "session_reused": bool(review.get("session_id") and review.get("session_id") == getattr(self, "_implement_session", None)),
                "attachments": list(self._review_attachments()),
                "review_mode": self.review_mode,
                "structured_verdict": self.structured_verdict,
                "review_transport_failed": self.review_transport_failed,
            },
        )
        self.store.write("review.md", self.review_text or "(empty review)")
        verify_ok = bool(self.verification.get("passed"))
        weakening = detect_test_weakening(self.git_state.get("diff", ""))
        if weakening and verify_ok:
            return self._finish_needs_decision(
                "test-weakening",
                f"verification PASS coincides with test weakening: {weakening[0]}",
                self.review_text,
            )
        if mode == REVIEW_STRUCTURED:
            return self._structured_review_decision(review, verify_ok)
        return self._advisory_review_decision(review)

    def _structured_review_decision(self, review: dict, verify_ok: bool):
        if review.get("transport_exhausted"):
            return self._transport_blocked()
        verdict = review.get("structured_verdict")
        if verdict not in STRUCTURED_VERDICTS:
            return self._finish_needs_decision(
                "reviewer-invalid-output",
                "structured reviewer verdict missing or invalid",
                self.review_text,
            )
        self.structured_verdict = verdict
        self.review_verdict = verdict
        if verdict == "PASS" and verify_ok:
            self._transition("DONE", "verifier PASS + structured reviewer PASS")
            return self._finish("DONE")
        if verdict == "BLOCKED":
            self.escalation = {"category": "reviewer-blocked",
                               "detail": self.review_text[:2000]}
            self._transition("BLOCKED", "structured reviewer returned BLOCKED")
            return self._finish("BLOCKED")
        # CHANGES_REQUIRED (or PASS-with-failing-verifier): repair if budget left.
        if self.repairs_used >= self.max_repairs:
            self.escalation = {"category": "budget-exceeded",
                               "detail": f"{self.max_repairs} automatic repairs exhausted"}
            self._transition("BUDGET_EXCEEDED",
                             f"max {self.max_repairs} automatic repairs exhausted")
            return self._finish("BUDGET_EXCEEDED")
        self.repairs_used += 1
        self._transition("REPAIRING",
                         f"repair attempt {self.repairs_used}/{self.max_repairs}")
        repaired = self._phase(
            "repair", self._repair_prompt(self.repairs_used),
            attachments=(self.task_path,), agent=self.agents.get("repair"),
        )
        if repaired.get("transport_exhausted"):
            return self._transport_blocked()
        attempts = forbidden_action_attempts(
            repaired.get("raw_stdout", ""), self.forbidden)
        if attempts:
            return self._finish_needs_decision(
                "forbidden-action-attempted",
                f"repairer attempted forbidden action(s): {attempts[0]}",
                repaired.get("output", ""),
            )
        self.git_state = collect_git_state(self.repo_root)
        self.store.write("diff.patch", self.git_state["diff"] or "(empty diff)")
        return None  # caller re-verifies

    def _advisory_review_decision(self, review: dict):
        # V1 ADVISORY: reviewer prose never controls state. Transport failure
        # of the advisory reviewer is recorded; hard gates decide.
        if review.get("transport_exhausted"):
            self.review_transport_failed = True
        verify_ok = bool(self.verification.get("passed"))
        if not verify_ok:
            # Deterministic failure drives repair (evidence, not prose).
            if self.repairs_used >= self.max_repairs:
                self.escalation = {"category": "budget-exceeded",
                                   "detail": f"{self.max_repairs} automatic repairs exhausted"}
                self._transition("BUDGET_EXCEEDED",
                                 f"max {self.max_repairs} automatic repairs exhausted")
                return self._finish("BUDGET_EXCEEDED")
            self.repairs_used += 1
            self._transition("REPAIRING",
                             f"repair attempt {self.repairs_used}/{self.max_repairs}")
            repaired = self._phase(
                "repair", self._repair_prompt(self.repairs_used),
                attachments=(self.task_path,), agent=self.agents.get("repair"),
            )
            if repaired.get("transport_exhausted"):
                return self._transport_blocked()
            attempts = forbidden_action_attempts(
                repaired.get("raw_stdout", ""), self.forbidden)
            if attempts:
                return self._finish_needs_decision(
                    "forbidden-action-attempted",
                    f"repairer attempted forbidden action(s): {attempts[0]}",
                    repaired.get("output", ""),
                )
            self.git_state = collect_git_state(self.repo_root)
            self.store.write("diff.patch", self.git_state["diff"] or "(empty diff)")
            return None  # caller re-verifies
        self._transition("DONE", "all advisory hard gates passed")
        return self._finish("DONE")

    def _delta_files(self) -> list[str]:
        """Files that appeared/changed since run start (pre-existing dirt excluded).

        The loop's own evidence dir is excluded: it is controller output,
        not implementation work, and must never trip the scope check.
        """
        return self._delta_between(
            self.git_baseline.get("status_short", ""),
            self.git_state.get("status_short", ""))

    def _delta_between(self, before_status: str, after_status: str) -> list[str]:
        before = set(changed_files_from_status(before_status))
        after = changed_files_from_status(after_status)
        own = Path(self.store.dir).as_posix()
        root = Path(self.repo_root).as_posix()
        delta = []
        for rel in sorted(set(after) - set(before)):
            if Path(f"{root}/{rel}").as_posix().startswith(own + "/"):
                continue
            delta.append(rel)
        return delta

    def _planning_delta_files(self) -> list[str]:
        """Files changed between run start and end of planning (must be none)."""
        if not self.git_after_plan:
            return ["(planning snapshot not recorded)"]
        return self._delta_between(
            self.git_baseline.get("status_short", ""),
            self.git_after_plan.get("status_short", ""))

    def _review_delta_files(self) -> list[str]:
        """Files changed during the review phase itself (must be none)."""
        if not self.git_after_review or not self._review_before_state:
            return []
        return self._delta_between(
            self._review_before_state.get("status_short", ""),
            self.git_after_review.get("status_short", ""))

    def _review_attachments(self) -> tuple[str, ...]:
        paths = [self.task_path, str(self.store.dir / "verification.json"),
                 str(self.store.dir / "diff.patch")]
        existing = [p for p in paths if Path(p).is_file()]
        files = self._delta_files() or changed_files_from_status(
            self.git_state.get("status_short", ""))
        for rel in files[:10]:
            candidate = str(Path(self.repo_root) / rel)
            if (Path(candidate).name == "TASK.md"
                    and Path(candidate) != Path(self.task_path)):
                continue  # never attach a different TASK.md next to the real one
            if Path(candidate).is_file() and candidate not in existing:
                existing.append(candidate)
        return tuple(existing)

    def _scope_violation(self) -> str:
        sections = split_task_sections(self.task_text)
        scope = sections.get("allowed scope", "")
        if not scope or "แก้ได้" in scope or len(scope) < 12:
            return ""  # template/empty scope: not enforceable
        files = self._delta_files()
        outside = [f for f in files if f not in scope]
        return ", ".join(outside[:10])

    def _finish_needs_decision(self, category: str, detail: str, evidence: str) -> dict:
        self.escalation = {"category": category, "detail": detail[:2000],
                           "evidence": (evidence or "")[:2000]}
        self._transition("NEEDS_DECISION", f"{category}: {detail[:160]}")
        return self._finish("NEEDS_DECISION")

    def _finish(self, status: str) -> dict:
        self.git_state = self.git_state if self.git_state.get("commit") else \
            collect_git_state(self.repo_root)
        files = self._delta_files()
        baseline = changed_files_from_status(self.git_baseline.get("status_short", ""))
        planned = self._verification_commands_for_run()
        executed = [r.get("command") for r in self.verification.get("results", [])
                    if not r.get("blocked")]
        result = {
            "run_id": self.run_id,
            "status": status,
            "task": self.task_path,
            "commit": self.git_state.get("commit"),
            "files_changed": files,
            "baseline_pre_existing_files": len(baseline),
            "model": {
                "provider": getattr(self.runner, "provider", None),
                "model": getattr(self.runner, "model", None),
                "variant": getattr(self.runner, "variant", None),
            },
            "transport_retries": list(self.transport_retries),
            "planning_delta_files": self._planning_delta_files(),
            "review_delta_files": self._review_delta_files(),
            "review_mode": self.review_mode,
            "review_capability": self.review_capability,
            "structured_verdict": self.structured_verdict,
            "review_transport_failed": self.review_transport_failed,
            "verification_planned": planned,
            "verification_executed": executed,
            "verification_commands": planned,
            "verification_passed": bool(self.verification.get("passed")),
            "verification_summary": self.verification.get("summary", {}),
            "reviewer_verdict": self.review_verdict,
            "repairs_used": self.repairs_used,
            "max_repairs": self.max_repairs,
            "escalation": self.escalation,
            "evidence_dir": str(self.store.dir),
        }
        self.store.write("result.md", render_result_md(
            result, self.verification, self.review_text, self.git_state))
        # Guarantee the full evidence set (req 9) even on early escalation.
        self._ensure_evidence_files()
        self._persist_state()
        return result

    def _ensure_evidence_files(self) -> None:
        defaults = {
            "plan.md": "(plan phase not reached)",
            "verification.json": {"passed": False, "refused": [], "results": [],
                                  "summary": {}, "note": "verifier phase not reached"},
            "review.md": "(review phase not reached)",
        }
        for name, fallback in defaults.items():
            if not (self.store.dir / name).is_file():
                if isinstance(fallback, dict):
                    self.store.write_json(name, fallback)
                else:
                    self.store.write(name, fallback)


def render_result_md(result: dict, verification: dict, review_text: str, git_state: dict) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# Agent-loop result: {result['run_id']}")
    add("")
    add(f"- status: **{result['status']}**")
    add(f"- task: `{result['task']}`")
    add(f"- commit/diff reference: `{result['commit']}` (see `diff.patch` in evidence dir)")
    add(f"- evidence dir: `{result['evidence_dir']}`")
    add(f"- repairs used: {result['repairs_used']}/{result['max_repairs']}")
    add(f"- reviewer verdict: {result['reviewer_verdict'] or 'n/a'}")
    model = result.get("model") or {}
    add(f"- model: provider=`{model.get('provider') or 'n/a'}` "
        f"model=`{model.get('model') or 'n/a'}` "
        f"variant=`{model.get('variant') or 'n/a'}` (explicit, no silent fallback)")
    add("")
    add("## Files changed")
    if result["files_changed"]:
        for path in result["files_changed"]:
            add(f"- {path}")
    else:
        add("- (none)")
    add("")
    add("## Files changed during planning (must be none)")
    planning_delta = result.get("planning_delta_files", [])
    if planning_delta:
        for path in planning_delta:
            add(f"- {path}")
    else:
        add("- (none -- planner wrote nothing)")
    add("")
    add("## Files changed during review (must be none)")
    review_delta = result.get("review_delta_files", [])
    if review_delta:
        for path in review_delta:
            add(f"- {path}")
    else:
        add("- (none -- reviewer wrote nothing)")
    add("")
    add("## Verification commands (planned)")
    planned = result.get("verification_planned", result.get("verification_commands", []))
    for cmd in planned:
        add(f"- `{cmd}`")
    if not planned:
        add("- (none defined)")
    add("")
    add("## Verification results (executed)")
    executed = result.get("verification_executed", [])
    not_executed = [c for c in planned if c not in executed]
    add(f"- overall: {'PASS' if result['verification_passed'] else 'FAIL'}")
    if not executed:
        add("- (no verification commands were executed)")
    if not_executed:
        add("- planned but NOT executed (do not treat as results):")
        for cmd in not_executed:
            add(f"  - `{cmd}`")
    for res in verification.get("results", []):
        add(f"### `{res.get('command')}`")
        add(f"- exit_code: {res.get('exit_code')}")
        add(f"- blocked: {res.get('blocked')}, timed_out: {res.get('timed_out')}, "
            f"duration_s: {res.get('duration_s')}")
        if res.get("block_reason"):
            add(f"- block_reason: {res.get('block_reason')}")
        stdout = (res.get("stdout") or "").strip().splitlines()
        stderr = (res.get("stderr") or "").strip().splitlines()
        if stdout:
            add(f"- stdout (last {min(10, len(stdout))} lines):")
            add("```")
            add("\n".join(stdout[-10:]))
            add("```")
        if stderr:
            add(f"- stderr (last {min(10, len(stderr))} lines):")
            add("```")
            add("\n".join(stderr[-10:]))
            add("```")
    if verification.get("refused"):
        add(f"- refused: {verification['refused']}")
    add("")
    add("## Review")
    add(f"- review_mode = **{result.get('review_mode') or 'n/a'}**")
    if result.get("structured_verdict"):
        add(f"- structured verdict: {result['structured_verdict']}")
    else:
        add("- structured verdict: n/a (advisory prose only; prose never controls state)")
    if result.get("review_transport_failed"):
        add("- advisory reviewer transport failed; continued on deterministic hard gates")
    cap = result.get("review_capability") or {}
    if cap and not cap.get("supported"):
        add(f"- structured-output capability: unsupported ({'; '.join(cap.get('reasons', []))})")
    if result.get("review_mode") == "ADVISORY_REVIEW":
        add("- ADVISORY_REVIEW is acceptable for reversible local/branch development "
            "only. It does NOT authorize deployment, production changes, secret "
            "modifications, or real-money actions.")
    add("")
    add("## Reviewer findings")
    add(review_text.strip() or "(no review recorded)")
    add("")
    add("## Transport retries (rate-limit only; never consume repair budget)")
    retries = result.get("transport_retries", [])
    if retries:
        for entry in retries:
            add(f"- phase={entry.get('phase')} attempt={entry.get('attempt')} "
                f"wait_s={entry.get('wait_s')} reason={entry.get('reason')} "
                f"advised={entry.get('advised')}")
    else:
        add("- none")
    add("")
    add("## Unresolved limitations")
    escalation = result.get("escalation")
    if escalation:
        add(f"- escalation: {escalation.get('category')}: {escalation.get('detail')}")
    elif result["status"] != "DONE":
        add(f"- loop ended in {result['status']} without full PASS; see verification + review above.")
    elif result.get("review_mode") == "ADVISORY_REVIEW":
        add("- none recorded; all deterministic hard gates passed; "
            "advisory review preserved as evidence only (prose never controlled state).")
    else:
        add("- none recorded; reviewer PASS + verifier PASS on the commands above.")
    add("")
    add("## Notes")
    add("- An implementation summary is not evidence; evidence is `diff.patch` + "
        "`verification.json`.")
    add("- Backtest-report fields (trade count, win rate, planned RR, realized payoff, "
        "partition, date range, session window, trades/week): N/A -- this loop run "
        "performed no market simulation.")
    return "\n".join(lines) + "\n"

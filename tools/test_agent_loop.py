"""Tests for the minimal agent-loop controller (no LLM, no network).

Run: py -3.12 -X utf8 -m unittest discover -s tools -p "test_*.py"
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent_loop import controller as C
from agent_loop import verifier as V

ROOT = Path(__file__).resolve().parent.parent


def make_task(directory: str, verification=None, scope="files that may change: tools/agent_loop/") -> str:
    lines = ["# Goal", "", "Do the thing.", "", "# Context", "", "Test fixture.",
             "", "# Acceptance Criteria", "", "- [ ] A", ""]
    if verification is not None:
        lines += ["# Verification", ""]
        lines += [f"- {cmd}" for cmd in verification]
        lines += [""]
    lines += ["# Constraints", "", "- Do not push.", "",
              "# Allowed Scope", "", scope, "",
              "# Stop Conditions", "", "Escalate if methodology needs to change."]
    path = Path(directory) / "TASK.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def canned_verification(passed: bool, command: str = "echo ok", stderr: str = "boom") -> dict:
    if passed:
        return {"passed": True, "refused": [],
                "results": [{"command": command, "exit_code": 0, "stdout": "ok",
                             "stderr": "", "timed_out": False, "duration_s": 0.01,
                             "blocked": False, "block_reason": None}],
                "summary": {"total": 1, "passed": 1, "failed": 0, "blocked_or_refused": 0}}
    return {"passed": False, "refused": [],
            "results": [{"command": command, "exit_code": 1, "stdout": "",
                         "stderr": stderr, "timed_out": False, "duration_s": 0.01,
                         "blocked": False, "block_reason": None}],
            "summary": {"total": 1, "passed": 0, "failed": 1, "blocked_or_refused": 0}}


class FakeRunner:
    """Scripted stand-in for OpenCode sessions. Records every invocation."""

    DEFAULTS = {"planner": "# Plan\n- step 1 (no credentials needed; "
                           "scope and methodology unchanged)",
                "implement": "implemented",
                "reviewer": "Findings: looks good. All criteria met.",
                "repair": "repaired"}

    def __init__(self, scripts=None):
        self.scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self.calls = []
        self.provider = None
        self.model = None
        self.variant = None

    def run_phase(self, phase, prompt, attachments=(), agent=None, **kwargs):
        self.calls.append({"phase": phase, "agent": agent, "prompt": prompt,
                           "attachments": list(attachments)})
        queue = self.scripts.get(phase, [])
        item = queue.pop(0) if queue else self.DEFAULTS.get(phase, "")
        if isinstance(item, dict):
            output = item.get("output", "")
            extra = {k: v for k, v in item.items() if k != "output"}
        else:
            output, extra = item, {}
        record = {"phase": phase, "agent": agent,
                  "session_id": f"fake-{phase}-{len(self.calls)}", "output": output,
                  "raw_stdout": output, "raw_stderr": "", "exit_code": 0,
                  "provider_error": None, "fresh_session": True}
        record.update(extra)
        return record


class ControllerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_verify = V.run_commands
        self._orig_git = C.collect_git_state

    def tearDown(self):
        V.run_commands = self._orig_verify
        C.collect_git_state = self._orig_git

    def stub_verify(self, payloads):
        queue = list(payloads)

        def _fake(commands, **kwargs):
            self.last_verify_commands = list(commands)
            if queue:
                return dict(queue.pop(0))
            return canned_verification(True)

        V.run_commands = _fake

    def stub_git(self, commit="abc1234", status="", diff="", then=None, seq=None):
        first = {"commit": commit, "status_short": status, "diff": diff}
        calls = {"n": 0}
        remaining = list(seq) if seq else None

        def _fake(repo_root):
            calls["n"] += 1
            if remaining:
                return dict(remaining.pop(0))
            if calls["n"] > 1 and then is not None:
                return dict(then)
            return dict(first)

        C.collect_git_state = _fake

    def make_controller(self, task_path, runner, config=None, **kwargs):
        params = {"repo_root": str(ROOT), "task_path": task_path,
                  "run_id": f"test-{self._testMethodName}",
                  "runner": runner,
                  "runs_dir": str(Path(self.tmp.name) / "runs"),
                  "config": config if config is not None else {},
                  "max_repairs": 2, "dry_run": False}
        params.update(kwargs)
        return C.Controller(**params)


class HappyPathTests(ControllerTestBase):
    def test_done_when_verifier_and_reviewer_pass(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git(status="", diff="")
        runner = FakeRunner()
        ctrl = self.make_controller(task, runner)
        result = ctrl.run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["repairs_used"], 0)
        for name in ("state.json", "plan.md", "verification.json", "review.md", "result.md"):
            self.assertTrue((Path(result["evidence_dir"]) / name).is_file(), name)

    def test_result_md_contains_required_sections(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git(status="", diff="")
        result = self.make_controller(task, FakeRunner()).run()
        body = (Path(result["evidence_dir"]) / "result.md").read_text(encoding="utf-8")
        for section in ("status", "commit/diff reference", "Files changed",
                        "Files changed during planning", "Files changed during review",
                        "Verification commands (planned)", "Verification results (executed)",
                        "Reviewer findings", "Unresolved limitations",
                        "Transport retries", "model:", "## Review", "review_mode"):
            self.assertIn(section, body)

    def test_state_history_order(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        ctrl = self.make_controller(task, FakeRunner())
        ctrl.run()
        states = [h["state"] for h in ctrl.history]
        for expected in ("PLANNING", "IMPLEMENTING", "VERIFYING", "REVIEWING", "DONE"):
            self.assertIn(expected, states)
        self.assertLess(states.index("PLANNING"), states.index("IMPLEMENTING"))
        self.assertLess(states.index("VERIFYING"), states.index("REVIEWING"))
        self.assertEqual(states[-1], "DONE")


class RepairBudgetTests(ControllerTestBase):
    def test_repair_then_done(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(False, stderr="first boom"),
                          canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"reviewer": ["Some findings on the diff.",
                                           "More findings after repair."]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["repairs_used"], 1)
        repairs = [c for c in runner.calls if c["phase"] == "repair"]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["agent"], "build")

    def test_budget_exceeded_after_two_repairs(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(False, stderr="boom-1"),
                          canned_verification(False, stderr="boom-2"),
                          canned_verification(False, stderr="boom-3")])
        self.stub_git()
        runner = FakeRunner({"reviewer": ["Findings round."] * 5})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "BUDGET_EXCEEDED")
        self.assertEqual(result["repairs_used"], 2)
        repairs = [c for c in runner.calls if c["phase"] == "repair"]
        self.assertEqual(len(repairs), 2, "must never attempt a third repair")

    def test_same_failure_repeat_escalates(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(False, stderr="identical boom"),
                          canned_verification(False, stderr="identical boom")])
        self.stub_git()
        runner = FakeRunner({"reviewer": ["Findings prose here."]})
        ctrl = self.make_controller(task, runner)
        result = ctrl.run()
        self.assertEqual(result["status"], "NEEDS_DECISION")
        self.assertEqual(result["escalation"]["category"], "same-failure-repeat")
        self.assertEqual(result["repairs_used"], 1)


class GuardrailTests(ControllerTestBase):
    def test_forbidden_push_command_is_refused_not_executed(self):
        task = make_task(self.tmp.name, verification=["git push origin main"])
        # Real verifier: must refuse before executing.
        self.stub_git()
        config = {"forbidden_patterns": C.FORBIDDEN_DEFAULT}
        result = self.make_controller(task, FakeRunner(), config=config).run()
        self.assertEqual(result["status"], "NEEDS_DECISION")
        self.assertEqual(result["escalation"]["category"], "forbidden-command")
        record = json.loads((Path(result["evidence_dir"]) / "verification.json")
                            .read_text(encoding="utf-8"))
        self.assertTrue(record["refused"])

    def test_reviewer_is_fresh_readonly_session(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner()
        config = {"agents": {"planner": "build", "implement": "build",
                             "repair": "build", "reviewer": "reviewer"}}
        self.make_controller(task, runner, config=config).run()
        reviews = [c for c in runner.calls if c["phase"] == "reviewer"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["agent"], "reviewer")
        implements = [c for c in runner.calls if c["phase"] == "implement"]
        self.assertNotEqual(reviews[0]["agent"], implements[0]["agent"])
        prompt = reviews[0]["prompt"]
        for needle in ("READ-ONLY", "git diff", "verifier", "summary is NOT evidence"):
            self.assertIn(needle, prompt)

    def test_reviewer_never_gets_a_different_task_md(self):
        task = make_task(self.tmp.name, verification=["echo ok"],
                         scope="files that may change: TASK.md tools/agent_loop/")
        self.stub_verify([canned_verification(True)])
        # Worktree contains a decoy TASK.md (the repo template); the run's
        # delta must not attach it next to the real task file.
        self.stub_git(status="?? TASK.md", diff="")
        runner = FakeRunner()
        self.make_controller(task, runner).run()
        reviews = [c for c in runner.calls if c["phase"] == "reviewer"]
        self.assertEqual(len(reviews), 1)
        for attachment in reviews[0]["attachments"]:
            if Path(attachment).name == "TASK.md":
                self.assertEqual(Path(attachment), Path(task))

    def test_test_weakening_blocks_done(self):
        task = make_task(self.tmp.name, verification=["echo ok"],
                         scope="files that may change: tools/foo.py")
        self.stub_verify([canned_verification(True)])
        clean = {"commit": "abc1234", "status_short": "", "diff": ""}
        dirty = {"commit": "abc1234", "status_short": " M tools/foo.py",
                 "diff": ("--- a/tools/foo.py\n+++ b/tools/foo.py\n"
                          "-    assert result.ok\n+    pass\n")}
        # baseline, after_plan clean; dirt appears only during implementation.
        self.stub_git(seq=[dict(clean), dict(clean), dict(dirty)])
        result = self.make_controller(task, FakeRunner()).run()
        self.assertEqual(result["status"], "NEEDS_DECISION")
        self.assertEqual(result["escalation"]["category"], "test-weakening")

    def test_scope_violation_escalates(self):
        task = make_task(self.tmp.name, verification=["echo ok"],
                         scope="files that may change: tools/allowed.py")
        self.stub_verify([canned_verification(True)])
        clean = {"commit": "abc1234", "status_short": "", "diff": ""}
        dirty = {"commit": "abc1234", "status_short": " M tools/other.py",
                 "diff": ""}
        self.stub_git(seq=[dict(clean), dict(clean), dict(dirty)])
        result = self.make_controller(task, FakeRunner()).run()
        self.assertEqual(result["status"], "NEEDS_DECISION")
        self.assertEqual(result["escalation"]["category"], "scope-exceeded")

    def test_reviewer_prose_never_gates_state(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"reviewer": ["Production credentials are required "
                                          "to proceed, methodology must change, "
                                          "out of scope, BLOCKED -- all just prose.\n"
                                          "REVIEW_VERDICT: PASS"]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["review_mode"], "ADVISORY_REVIEW")

    def test_legacy_verdict_format_is_ignored_prose(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"reviewer": ["Verdict: PASS\nOld format is just prose now."]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")

    def test_missing_reviewer_text_still_decided_by_gates(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"reviewer": [""]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")

    def test_planning_delta_blocks(self):
        task = make_task(self.tmp.name, verification=["echo ok"],
                         scope="files that may change: .agent-loop/smoke/adder/")
        self.stub_verify([canned_verification(True)])
        self.stub_git(status="", diff="",
                      then={"commit": "abc1234",
                            "status_short": "?? .agent-loop/smoke/adder/adder.py",
                            "diff": ""})
        result = self.make_controller(task, FakeRunner()).run()
        self.assertEqual(result["status"], "NEEDS_DECISION")
        self.assertEqual(result["escalation"]["category"], "planner-wrote-files")

    def test_pre_existing_worktree_dirt_is_not_scope_violation(self):
        task = make_task(self.tmp.name, verification=["echo ok"],
                         scope="files that may change: tools/allowed.py")
        self.stub_verify([canned_verification(True)])
        # Dirty before AND during the run (nothing new) -> no violation, DONE.
        self.stub_git(status=" M tools/other.py", diff="")
        result = self.make_controller(task, FakeRunner()).run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["files_changed"], [])

    def test_no_verification_commands_is_blocked(self):
        task = make_task(self.tmp.name, verification=None,
                         scope="files that may change: tools/agent_loop/")
        self.stub_git()
        result = self.make_controller(task, FakeRunner(), config={}).run()
        self.assertEqual(result["status"], "BLOCKED")


class SemanticGateRemovalTests(ControllerTestBase):
    """V0.3: prose is informational only; only evidence + verdict token gate."""

    def test_implementer_credentials_prose_does_not_escalate(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"implement": ["I did not access credentials. "
                                           "No secrets were touched. Done."]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")

    def test_no_methodology_change_prose_does_not_escalate(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"implement": ["No methodology change was required. Done."]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")

    def test_within_scope_text_does_not_replace_real_scope_check(self):
        task = make_task(self.tmp.name, verification=["echo ok"],
                         scope="files that may change: tools/allowed.py")
        self.stub_verify([canned_verification(True)])
        clean = {"commit": "abc1234", "status_short": "", "diff": ""}
        dirty = {"commit": "abc1234", "status_short": " M tools/other.py",
                 "diff": ""}
        self.stub_git(seq=[dict(clean), dict(clean), dict(dirty)])
        runner = FakeRunner({"implement": ["All changes remain within scope."]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "NEEDS_DECISION")
        self.assertEqual(result["escalation"]["category"], "scope-exceeded")

    def test_observed_forbidden_secret_access_blocks(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_git()
        tool_event = ('{"type":"tool_use","sessionID":"ses_x","part":'
                      '{"type":"tool","tool":"shell","state":{"status":"completed",'
                      '"input":{"command":"cat .env"}}}}')
        runner = FakeRunner({"implement": ["Working.\n" + tool_event]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "NEEDS_DECISION")
        self.assertEqual(result["escalation"]["category"], "forbidden-action-attempted")

    def test_benign_tool_calls_do_not_block(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        tool_event = ('{"type":"tool_use","sessionID":"ses_x","part":'
                      '{"type":"tool","tool":"shell","state":{"status":"completed",'
                      '"input":{"command":"py -3.12 -m unittest"}}}}')
        runner = FakeRunner({"implement": ["Ran tests.\n" + tool_event]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")


class VerifierUnitTests(unittest.TestCase):
    def test_records_real_exit_codes(self):
        record = V.run_commands(
            ['py -3.12 -X utf8 -c "import sys; sys.exit(0)"',
             'py -3.12 -X utf8 -c "import sys; sys.exit(3)"'],
            cwd=str(ROOT), timeout_s=120, forbidden_patterns=[])
        self.assertFalse(record["passed"])
        codes = [r["exit_code"] for r in record["results"]]
        self.assertEqual(codes, [0, 3])

    def test_forbidden_never_executes(self):
        import subprocess as sp
        orig_run = sp.run
        sp.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not execute"))
        try:
            record = V.run_commands(["git push origin main"], cwd=str(ROOT),
                                    timeout_s=60, forbidden_patterns=C.FORBIDDEN_DEFAULT)
        finally:
            sp.run = orig_run
        self.assertFalse(record["passed"])
        self.assertTrue(record["refused"])
        self.assertTrue(record["results"][0]["blocked"])

    def test_failure_signature_stable(self):
        first = canned_verification(False, stderr="boom  ")
        second = canned_verification(False, stderr="boom")
        self.assertEqual(C.failure_signature(first), C.failure_signature(second))
        other = canned_verification(False, stderr="different failure here xyz")
        self.assertNotEqual(C.failure_signature(first), C.failure_signature(other))

    def test_extract_verification_commands(self):
        text = "# Verification\n\n- py -3.12 -X utf8 -m unittest discover -s tools\n- npm test\n"
        self.assertEqual(C.extract_verification_commands(text),
                         ["py -3.12 -X utf8 -m unittest discover -s tools", "npm test"])


class RunnerUnitTests(unittest.TestCase):
    def test_binary_resolution(self):
        from unittest import mock
        from agent_loop import opencode_runner as R
        with mock.patch.object(R.shutil, "which", return_value="C:/x/opencode.CMD"):
            self.assertEqual(R.OpenCodeRunner(binary="opencode").binary,
                             "C:/x/opencode.CMD")
        with mock.patch.object(R.shutil, "which", return_value=None):
            self.assertEqual(R.OpenCodeRunner(binary="opencode").binary, "opencode")

    def test_extract_text_from_jsonl_event_stream(self):
        from agent_loop import opencode_runner as R
        stream = (
            '{"type":"step_start","sessionID":"ses_abc","part":{}}\n'
            '{"type":"text","sessionID":"ses_abc",'
            '"part":{"type":"text","text":"Verdict: PASS"}}\n'
            '{"type":"text","sessionID":"ses_abc",'
            '"part":{"type":"text","text":"All good."}}\n'
        )
        self.assertEqual(R.extract_text(stream), "Verdict: PASS\nAll good.")
        self.assertEqual(R.extract_session_id(stream), "ses_abc")

    def test_extract_text_error_stream_returns_empty(self):
        from agent_loop import opencode_runner as R
        err = ('{"type":"step_start","sessionID":"ses_x","part":{}}\n'
               '{"type":"error","sessionID":"ses_x",'
               '"error":{"type":"provider.rate-limit","message":"Provider returned error"}}\n')
        self.assertEqual(R.extract_text(err), "")
        self.assertEqual(R.extract_session_id(err), "ses_x")

    def test_extract_text_single_object_unchanged(self):
        from agent_loop import opencode_runner as R
        self.assertEqual(R.extract_text('{"text": "hello"}'), "hello")
        self.assertEqual(R.extract_text("plain output"), "plain output")
        self.assertEqual(R.extract_session_id('{"text": "hello"}'), None)

    def test_delta_excludes_own_evidence_dir(self):
        fake_root = Path(tempfile.mkdtemp())
        task = make_task(str(fake_root), verification=["echo ok"])
        ctrl = C.Controller(repo_root=str(fake_root), task_path=task,
                            run_id="evid-excl", runner=FakeRunner(),
                            runs_dir=str(fake_root / ".agent-loop" / "runs"),
                            config={}, max_repairs=2, dry_run=True)
        ctrl.git_baseline = {"commit": "x", "status_short": "", "diff": ""}
        ctrl.git_state = {"commit": "x",
                          "status_short": "?? .agent-loop/runs/evid-excl/state.json\n"
                                          "?? .agent-loop/smoke/adder/adder.py\n",
                          "diff": ""}
        self.assertEqual(ctrl._delta_files(), [".agent-loop/smoke/adder/adder.py"])


class PlanAgentTests(ControllerTestBase):
    def test_planner_uses_plan_agent_and_implement_uses_build(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner()
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")
        by_phase = {c["phase"]: c for c in runner.calls}
        self.assertEqual(by_phase["planner"]["agent"], "plan")
        self.assertEqual(by_phase["implement"]["agent"], "build")

    def test_planner_prose_keywords_do_not_alter_state(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        prose = ("# Plan\ncredentials methodology scope BLOCKED PROCEED "
                 "NEEDS_DECISION -- all just prose, proceed.")
        runner = FakeRunner({"planner": [prose]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")

    def test_empty_planner_stops_blocked_never_done(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_git()
        runner = FakeRunner({"planner": ["   \n  "]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["escalation"]["category"], "planner-failed")
        self.assertEqual(result["repairs_used"], 0)
        self.assertEqual([c for c in runner.calls if c["phase"] == "implement"], [])

    def test_planner_prose_is_not_implementation_evidence(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"planner": ["# Plan\nI created tools/ghost.py already."]})
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["files_changed"], [])
        self.assertEqual(result["planning_delta_files"], [])

    def test_reviewer_defaults_to_plan_agent_and_writes_nothing(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner()
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "DONE")
        reviews = [c for c in runner.calls if c["phase"] == "reviewer"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["agent"], "plan")
        self.assertEqual(result["review_delta_files"], [])


class ReviewModeTests(ControllerTestBase):
    SUPPORTED = {"supported": True, "reasons": []}
    UNSUPPORTED = {"supported": False, "reasons": ["no schema flag"]}

    def _reviewer(self, **extra):
        record = {"output": "Findings prose.", "raw_stdout": "Findings prose.",
                  "raw_stderr": "", "exit_code": 0, "provider_error": None}
        record.update(extra)
        return record

    def test_auto_selects_advisory_when_unsupported(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        ctrl = self.make_controller(task, FakeRunner(),
                                    capability_probe=lambda: dict(self.UNSUPPORTED))
        result = ctrl.run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["review_mode"], "ADVISORY_REVIEW")
        cap = json.loads((Path(result["evidence_dir"]) / "review_capability.json")
                         .read_text(encoding="utf-8"))
        self.assertFalse(cap["capability"]["supported"])

    def test_structured_pass_reaches_done(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"reviewer": [self._reviewer(structured_verdict="PASS")]})
        ctrl = self.make_controller(task, runner,
                                    capability_probe=lambda: dict(self.SUPPORTED))
        result = ctrl.run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["review_mode"], "STRUCTURED_REVIEW")
        self.assertEqual(result["structured_verdict"], "PASS")

    def test_structured_changes_required_repairs(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(False, stderr="boom"),
                          canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"reviewer": [self._reviewer(structured_verdict="CHANGES_REQUIRED"),
                                           self._reviewer(structured_verdict="PASS")]})
        ctrl = self.make_controller(task, runner,
                                    capability_probe=lambda: dict(self.SUPPORTED))
        result = ctrl.run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["repairs_used"], 1)

    def test_structured_blocked_blocks(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"reviewer": [self._reviewer(structured_verdict="BLOCKED")]})
        ctrl = self.make_controller(task, runner,
                                    capability_probe=lambda: dict(self.SUPPORTED))
        result = ctrl.run()
        self.assertEqual(result["status"], "BLOCKED")

    def test_structured_invalid_verdict_fails_safe(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"reviewer": [self._reviewer(structured_verdict="MAYBE")]})
        ctrl = self.make_controller(task, runner,
                                    capability_probe=lambda: dict(self.SUPPORTED))
        result = ctrl.run()
        self.assertEqual(result["status"], "NEEDS_DECISION")
        self.assertEqual(result["escalation"]["category"], "reviewer-invalid-output")

    def test_forced_structured_when_unsupported_blocks(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        ctrl = self.make_controller(task, FakeRunner(),
                                    config={"review_mode": "structured"},
                                    capability_probe=lambda: dict(self.UNSUPPORTED))
        result = ctrl.run()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["escalation"]["category"], "review-mode-unavailable")

    def test_advisory_reviewer_transport_failure_continues_on_gates(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        failed = self._reviewer(output="", raw_stdout="", provider_error="rate-limit",
                                exit_code=1)
        runner = FakeRunner({"reviewer": [failed]})
        config = {"transport_max_retries": 0, "transport_backoff_s": [0]}
        ctrl = self.make_controller(task, runner, config=config,
                                    capability_probe=lambda: dict(self.UNSUPPORTED))
        result = ctrl.run()
        self.assertEqual(result["status"], "DONE")
        self.assertTrue(result["review_transport_failed"])
        self.assertEqual(result["repairs_used"], 0)

    def test_reviewer_write_is_controller_failure(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        clean = {"commit": "abc1234", "status_short": "", "diff": ""}
        dirty = {"commit": "abc1234", "status_short": " M tools/sneaky.py", "diff": ""}
        # baseline, after_plan, after_implement, before_review, after_review
        self.stub_git(seq=[dict(clean), dict(clean), dict(clean),
                           dict(clean), dict(dirty)])
        result = self.make_controller(task, FakeRunner(),
                                      capability_probe=lambda: dict(self.UNSUPPORTED)).run()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["escalation"]["category"], "reviewer-wrote-files")


class TransportRetryTests(ControllerTestBase):
    RATE_LIMIT = {"output": "", "provider_error": "rate-limit", "exit_code": 1,
                  "raw_stdout": '{"type":"error","error":{"type":"provider.rate-limit"}}'}

    def test_rate_limit_retry_does_not_consume_repair_budget(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner({"planner": [dict(self.RATE_LIMIT),
                                         "# Plan after retry"]})
        config = {"transport_max_retries": 3, "transport_backoff_s": [0]}
        ctrl = self.make_controller(task, runner, config=config)
        result = ctrl.run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["repairs_used"], 0)
        self.assertEqual(len(result["transport_retries"]), 1)
        self.assertEqual(result["transport_retries"][0]["phase"], "planner")

    def test_persistent_rate_limit_blocks_without_repairs(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_git()
        runner = FakeRunner({"planner": [dict(self.RATE_LIMIT)] * 5})
        config = {"transport_max_retries": 2, "transport_backoff_s": [0]}
        ctrl = self.make_controller(task, runner, config=config)
        result = ctrl.run()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["escalation"]["category"], "provider-rate-limited")
        self.assertEqual(result["repairs_used"], 0)
        planners = [c for c in runner.calls if c["phase"] == "planner"]
        self.assertEqual(len(planners), 3)  # 1 initial + 2 retries, bounded

    def test_classify_and_retry_after(self):
        from agent_loop import opencode_runner as R
        self.assertEqual(R.classify_provider_error('{"type":"error","sessionID":"s","error":{"type":"provider.rate-limit"}}', ""), "rate-limit")
        self.assertIsNone(R.classify_provider_error("all good", ""))
        self.assertEqual(R.extract_retry_after_s('{"type":"error","sessionID":"s","error":{"type":"provider.rate-limit","retryAfterMs":45000}}'), 45.0)
        self.assertIsNone(R.extract_retry_after_s("no hints here"))


class ModelIdentityTests(ControllerTestBase):
    def test_explicit_model_in_build_command(self):
        from agent_loop import opencode_runner as R
        runner = R.OpenCodeRunner(model="opencode/muse-spark-1.3-contributor-free")
        cmd = runner.build_command("planner", "r1", "hi", (), "build")
        self.assertIn("--model", cmd)
        self.assertIn("opencode/muse-spark-1.3-contributor-free", cmd)
        self.assertEqual(runner.provider, "opencode")

    def test_variant_joined_with_hash(self):
        from agent_loop import opencode_runner as R
        runner = R.OpenCodeRunner(model="opencode/some-model", variant="thinking")
        cmd = runner.build_command("planner", "r1", "hi", (), "build")
        self.assertIn("opencode/some-model#thinking", cmd)

    def test_cli_refuses_live_run_without_explicit_model(self):
        from agent_loop import cli as CLI
        root = Path(tempfile.mkdtemp())
        (root / ".agent-loop").mkdir()
        (root / ".agent-loop" / "config.json").write_text("{}", encoding="utf-8")
        task = make_task(str(root), verification=["echo ok"])
        rc = CLI.main(["--task", task, "--run-id", "nomodel", "--repo-root", str(root)])
        self.assertEqual(rc, 1)

    def test_transcript_records_model_identity(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_verify([canned_verification(True)])
        self.stub_git()
        runner = FakeRunner()
        runner.provider = "opencode"
        runner.model = "opencode/muse-spark-1.3-contributor-free"
        runner.variant = None
        ctrl = self.make_controller(task, runner)
        result = ctrl.run()
        self.assertEqual(result["status"], "DONE")
        lines = (Path(result["evidence_dir"]) / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        self.assertEqual(first["model"], "opencode/muse-spark-1.3-contributor-free")
        self.assertEqual(first["provider"], "opencode")
        body = (Path(result["evidence_dir"]) / "result.md").read_text(encoding="utf-8")
        self.assertIn("opencode/muse-spark-1.3-contributor-free", body)


class EarlyExitReportingTests(ControllerTestBase):
    def test_planned_but_unexecuted_commands_are_labeled(self):
        task = make_task(self.tmp.name, verification=["echo ok"])
        self.stub_git()
        runner = FakeRunner({"planner": [""]})  # empty plan -> BLOCKED pre-verify
        result = self.make_controller(task, runner).run()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["verification_planned"], ["echo ok"])
        self.assertEqual(result["verification_executed"], [])
        body = (Path(result["evidence_dir"]) / "result.md").read_text(encoding="utf-8")
        self.assertIn("NOT executed", body)
        self.assertIn("(no verification commands were executed)", body)


if __name__ == "__main__":
    unittest.main()

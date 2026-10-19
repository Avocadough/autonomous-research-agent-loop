"""CLI entry: run one task through the agent loop.

Examples (PowerShell, repo root):
  py -3.12 -X utf8 tools/agent_loop/cli.py --task TASK.md --dry-run
  py -3.12 -X utf8 tools/agent_loop/cli.py --task TASK.md --run-id demo01 --dry-run
  py -3.12 -X utf8 tools/agent_loop/cli.py --task TASK.md --run-id real01
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_loop.controller import Controller, MAX_REPAIRS_DEFAULT  # noqa: E402
from agent_loop.opencode_runner import DryRunRunner, OpenCodeRunner  # noqa: E402

EXIT_BY_STATUS = {"DONE": 0, "BLOCKED": 1, "NEEDS_DECISION": 2, "BUDGET_EXCEEDED": 3}


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_config(repo_root: Path) -> dict:
    path = repo_root / ".agent-loop" / "config.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal local agent-loop controller.")
    parser.add_argument("--task", required=True, help="Path to TASK.md")
    parser.add_argument("--run-id", default=None, help="Run id (default: UTC timestamp)")
    parser.add_argument("--max-repairs", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate LLM phases; verifier runs allowlisted safe commands only.")
    parser.add_argument("--model", default=None, help="Override opencode model")
    parser.add_argument("--repo-root", default=str(Path.cwd()))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    config = load_config(repo_root)
    run_id = args.run_id or default_run_id()
    max_repairs = args.max_repairs if args.max_repairs is not None else int(
        config.get("max_repairs", MAX_REPAIRS_DEFAULT)
    )
    if args.dry_run:
        runner = DryRunRunner()
    else:
        # Fix 3: never silently fall back to a default model on live runs.
        if not (args.model or config.get("model")):
            print("refused: no explicit model configured "
                  "(.agent-loop/config.json: model). "
                  "Live runs require an explicit provider/model id.")
            return 1
        runner = OpenCodeRunner(
            binary=config.get("opencode_binary", "opencode"),
            model=args.model or config.get("model"),
            variant=config.get("model_variant"),
            timeout_s=int(config.get("opencode_timeout_s", 1800)),
            cwd=str(repo_root),
            standalone=bool(config.get("opencode_standalone", False)),
        )
    controller = Controller(
        repo_root=str(repo_root),
        task_path=str((repo_root / args.task).resolve()
                      if not Path(args.task).is_absolute() else Path(args.task)),
        run_id=run_id,
        runner=runner,
        runs_dir=str(repo_root / ".agent-loop" / "runs"),
        config=config,
        max_repairs=max_repairs,
        dry_run=args.dry_run,
    )
    result = controller.run()
    print(f"status: {result['status']}")
    print(f"run_id: {result['run_id']}")
    print(f"evidence: {result['evidence_dir']}")
    print(f"repairs: {result['repairs_used']}/{result['max_repairs']}")
    print(f"verification: {'PASS' if result['verification_passed'] else 'FAIL'}")
    print(f"reviewer: {result['reviewer_verdict'] or 'n/a'}")
    return EXIT_BY_STATUS.get(result["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())

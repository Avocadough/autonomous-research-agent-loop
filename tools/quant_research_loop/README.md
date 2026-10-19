# Current continuation

Start with `research/campaigns/PAYOUT_V1/runtime/state.json`, then its pinned
`user_research_directive`, `last_decision`, `latest_qa_review` and any
`maintenance_handoff`. Resolve routes from the state's `model_route_authority`
(R25 at this update); dated role labels below are historical. Reuse the existing
controller, supervisor and continuation heartbeat. Local tests or partial QA
do not grant worker dispatch, clear a stop, or authorize price access/live work.

New model-call receipts include UTC start/finish observations. Use `elapsed_s`
for invocation duration: it is monotonic and remains valid across wall-clock
changes. Missing times in historical receipts are unknown; do not rewrite them
or infer success from a finish timestamp.

# Historical model routes — 2026-10-01

New Research Lead and independent QA calls use `gpt-6.1-sol` with `high`
reasoning under the current model-route authorization. The executor remains
`gpt-6-luna` with `max` reasoning. This change does not resume the campaign,
clear a safety hold, expand any budget, or authorize research or live trading.
Use `runtime/state.json` and its referenced authorities for the current status.

R20 adds a separate `gpt-6.1-sol` / `high` Skill Engineering call every 10
cumulative call units. Activation is at 533; the first checkpoint is 540,
then 550, 560, and so on. The checkpoint runs at the next safe controller
boundary, outside a worker's scope snapshot; delayed thresholds are recorded.
Paused, held or interrupted work is not resumed by this role.

The role reviews current workflow evidence and the installed skill catalog,
selects bounded advisory guidance for Lead, Executor and QA, and may propose
project-owned `.codex/skills/payout-*/SKILL.md` updates. Global/plugin skills
and the Skill Engineering contract itself are read-only. Proposed edits need
an independent Sol 6.1 High review, path/hash validation and a durable apply
plan. A no-change review uses one call; a proposal with edits uses two. Both
count toward the unchanged 1,000-unit budget. Saved calls and edits reconcile
without blind retries, replacing historical evidence or refunding counters.
`runtime/skill_engineering/<threshold>/` retains evidence, reports and receipts.
Skill guidance cannot grant tools, data access, research or live permissions.

Original profiles, authorizations, decisions and receipts retain their original
model identities. Legacy fresh-call entrypoints use the new Lead route;
incompatible partially completed legacy calls require reconciliation and are
not relabeled or blindly dispatched again.

The bootstrap account below is historical evidence from 2026-09-20, not the
current campaign status or routing.

# Historical PAYOUT_V1 bootstrap: partial, stopped at the declared boundary

This is **not yet the complete automated research campaign implementation**.
The original `payout.py`, `schemas.py`, and Agent Loop are preserved unchanged.
Do not use the original payout prototype for financial decisions.

Verified on 2026-09-20:

- Controller `gate_zero.py` invoked three real Astra Lead sessions and Muse
  evidence analyses. Two distinct synthetic roundtrips completed without human
  forwarding. One additional Muse response was a plan-mode refusal, retained.
- Lead route: Codex CLI 0.155.1, official OpenAI provider, existing ChatGPT auth,
  `gpt-6-astra`, `medium`, isolated `--ignore-user-config`, read-only sandbox.
  Schema output and official web retrieval worked. Shell file reading was
  policy-blocked: exact approved file content and SHA256 were delivered through
  stdin instead. This does not claim a working shell-read capability.
- Muse route: OpenCode v2.0.10, built-in plan, requested
  `opencode/muse-spark-1.3-contributor-free`. Sanitized session export independently
  reports that model/provider, default variant, and cost 0 for the first worker.
- A later built-in build launcher call returned provider.auth HTTP 403:
  `OpenCode's free tier can only be used from within OpenCode`.
  The controller does not fall back to another provider or paid API.
- `lifecycle.py` is the new deterministic evaluator. Its tests cover cash and
  account transitions, EOD floor updates versus intraday monitoring, unknown
  paths, payout/reset/fees, calendar renewals, and the $120 scenario ceiling.
- `controller.py` persists exact registered jobs, Lead decisions, source and
  profile hashes, call accounting, single-writer locking, and pause/resume.
  Only the first preserved-ledger replay is currently registered.

Operations from the repo root:

```powershell
py -3.12 -X utf8 tools/quant_research_loop/launcher.py start
py -3.12 -X utf8 tools/quant_research_loop/launcher.py status
py -3.12 -X utf8 tools/quant_research_loop/launcher.py pause
py -3.12 -X utf8 tools/quant_research_loop/launcher.py resume
```

The repo-local Muse skill is `.opencode/skills/payout-research/SKILL.md`.
Direct start/status/resume were exercised. The real Muse launcher smoke attempt
failed at provider authorization before commands ran; do not call it certified.
Pause writes a request consumed at a safe invocation boundary. No service is
installed and survival across sleep/shutdown is not claimed.

## Stopped state

`research/campaigns/PAYOUT_V1/runtime/state.json` is authoritative.

- Setup: SETUP_BLOCKED (launcher provider 403).
- Bootstrap: PAUSED_BUDGET (3/3 integration-fix cycles; do not reset by renaming).
- Lead research decision: DATA_OR_RULES_BLOCKED. Standard 50K base evaluation
  plus activation is $49+$149=$198, above the preserved $120 external-cash
  scenario before unknown taxes. Lead paused before J001; zero real experiments
  and no payout replay were completed. This is not a strategy rejection.

Missing before full acceptance: supported free-worker launcher operation;
Agent Loop adapter with exact per-job scope and failed-process accounting;
automatic public discovery, new candidate registration/implementation and
multi-batch continuation; live worker execution/replay verification; complete
funded scaling/calendar/intratrade evidence. These are not implied by tests of
the currently implemented components. Dynamic unregistered jobs fail closed.

The original known Agent Loop weaknesses are documented in BOOTSTRAP_PLAN.md;
they were inspected, not silently declared repaired. No code-changing research
job was delegated through that layer during this bootstrap.

## Resume boundaries

Do not delete state or rerun completed gate jobs to create evidence. The fix
counter prevents further automatic work at its cap. Continuation needs explicit
authorization for an additional bounded engineering budget, and a versioned
resolution of the financial-profile conflict before research. Confirm supported
free-worker access through OpenCode's official route; do not bypass its 403.
No actual account, payment, order, deployment, commit, or push is authorized.

Accounting distinguishes observed child CLI invocations from unavailable prior
bootstrap/provider-level request totals. A conservative reserve is not a claim
that historical consumption was measured. See the final report for limits.


## R24 autonomous technical continuation — 2026-10-03

The direct user R24 revision selects Research Lead `gpt-6.1-sol` / `ultra`, supported by the pinned native model catalogue and configuration receipt; use `max` only when the validated capability authority requires fallback. Independent QA and Skill Engineering remain `gpt-6.1-sol` / `high`; the executor stays `gpt-6-luna` / `max`. Historical R19/R20 calls remain unchanged. The single-writer `python -m tools.quant_research_loop.supervisor` preserves pause/cancel controls and the 1,000 cumulative call-unit cap, waits on evidenced transient failures and requests a fresh Lead decision for technical recovery. Lead owns changed bounded repairs and diagnostics; a fresh explicit dispatch grant and existing independent QA/verifier gates remain mandatory. R24 acknowledges only the saved R23 operator-containment incident. Genuine permission/scope incidents, uncertain inflight calls, data/rules blocks, user stops and the cap remain stop conditions. No spending, accounts/orders/live, or protected dataset access is added. Current authority is runtime/state.json plus the R24 pinned authorization and receipts.

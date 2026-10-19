"""PAYOUT_V1 shared constants: allowed actions, paths, partitions."""
from __future__ import annotations

LEAD_ACTIONS = (
    "NEW_CANDIDATE",
    "RUN_BASELINE",
    "RUN_VARIANT",
    "REPLAY_ACCOUNT_POLICY",
    "FIX_EXPERIMENT",
    "READY_FOR_FORWARD",
    "PAUSE",
)

# Partitions the research engine may read (split contract). Anything else is refused.
AUTHORIZED_PARTITIONS = ("train", "validation", "synthetic_fixture")

# Path fragments that are NEVER readable/writable by experiment jobs.
PROTECTED_FRAGMENTS = (
    "test_reserved",
    "raw_combined",
    "combined_prices",
    "dataset/NQ_2026_candidate_getdata_v1",
    ".codex/auth",
    ".config/opencode",
)

DECISION_REQUIRED_FIELDS = (
    "campaign_id",
    "evidence_id",
    "action",
    "rationale",
    "artifact_refs",
    "next_job",
)

JOB_REQUIRED_FIELDS = (
    "job_id",
    "candidate_id",
    "candidate_version",
    "rules",
    "allowed_paths",
    "data_partition",
    "eval_interval",
    "cost_profile_hash",
    "account_profile_hash",
    "risk_policy",
    "outputs",
    "verification",
)

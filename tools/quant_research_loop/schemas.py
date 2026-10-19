"""Schema + permission validation for Lead decisions and jobs.

External, deterministic, and independent of any model prose: a decision is
accepted only if it is schema-valid AND references live campaign state
(campaign id, evidence id of a completed job, legal action, real paths).
"""
from __future__ import annotations

import os

from . import (
    AUTHORIZED_PARTITIONS,
    DECISION_REQUIRED_FIELDS,
    JOB_REQUIRED_FIELDS,
    LEAD_ACTIONS,
    PROTECTED_FRAGMENTS,
)


class ValidationError(ValueError):
    pass


def _norm(p: str) -> str:
    return os.path.normpath(p).replace("\\", "/")


def validate_decision(dec: dict, *, campaign_id: str, completed_evidence: set[str]) -> dict:
    if not isinstance(dec, dict):
        raise ValidationError("decision must be an object")
    missing = [f for f in DECISION_REQUIRED_FIELDS if f not in dec]
    if missing:
        raise ValidationError(f"missing fields: {missing}")
    if dec["campaign_id"] != campaign_id:
        raise ValidationError("stale campaign_id")
    if dec["evidence_id"] not in completed_evidence:
        raise ValidationError("unknown/stale evidence_id")
    if dec["action"] not in LEAD_ACTIONS:
        raise ValidationError(f"illegal action: {dec['action']!r}")
    if not isinstance(dec["rationale"], str) or not dec["rationale"].strip():
        raise ValidationError("rationale must be non-empty text")
    if not isinstance(dec.get("artifact_refs"), list):
        raise ValidationError("artifact_refs must be a list")
    nj = dec.get("next_job")
    if nj is not None and not isinstance(nj, dict):
        raise ValidationError("next_job must be object or null")
    return dec


def _check_path(p: str, repo_root: str) -> str:
    if not isinstance(p, str) or not p.strip():
        raise ValidationError(f"bad path: {p!r}")
    n = _norm(p)
    if n.startswith("/") or ".." in n.split("/") or (len(n) > 1 and n[1] == ":"):
        raise ValidationError(f"path traversal/absolute rejected: {p!r}")
    for frag in PROTECTED_FRAGMENTS:
        if frag in n:
            raise ValidationError(f"protected path rejected: {p!r}")
    full = os.path.normpath(os.path.join(repo_root, n))
    root = os.path.normpath(repo_root)
    if not (full == root or full.startswith(root + os.sep)):
        raise ValidationError(f"path escapes repo: {p!r}")
    return n


def validate_job(job: dict, *, repo_root: str) -> dict:
    if not isinstance(job, dict):
        raise ValidationError("job must be an object")
    missing = [f for f in JOB_REQUIRED_FIELDS if f not in job]
    if missing:
        raise ValidationError(f"missing fields: {missing}")
    if job["data_partition"] not in AUTHORIZED_PARTITIONS:
        raise ValidationError(f"unauthorized partition: {job['data_partition']!r}")
    if not isinstance(job["allowed_paths"], list) or not job["allowed_paths"]:
        raise ValidationError("allowed_paths must be a non-empty list")
    job["allowed_paths"] = [_check_path(p, repo_root) for p in job["allowed_paths"]]
    if not isinstance(job.get("outputs"), list) or not job["outputs"]:
        raise ValidationError("outputs must be a non-empty list")
    job["outputs"] = [_check_path(p, repo_root) for p in job["outputs"]]
    for out in job["outputs"]:
        if out not in job["allowed_paths"] and not any(
            out.startswith(a.rstrip("/") + "/") or out == a for a in job["allowed_paths"]
        ):
            raise ValidationError(f"output outside allowed_paths: {out!r}")
    if not isinstance(job.get("verification"), list) or not job["verification"]:
        raise ValidationError("verification must be a non-empty command list")
    for cmd in job["verification"]:
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
            raise ValidationError(f"verification must be typed argv arrays: {cmd!r}")
    return job

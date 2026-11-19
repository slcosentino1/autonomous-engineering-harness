"""Resolve the evidence fingerprints consumed by production stages."""

from __future__ import annotations

import hashlib
from typing import Any, Callable


def provide_stage_artifacts(
    stage: Any,
    state: dict[str, Any],
    target: Any,
    proposal_path: Any,
    fingerprint_repository: Callable[..., dict[str, Any]],
    read_bytes: Callable[[Any], bytes],
) -> dict[str, object]:
    """Return proposal and target fingerprints relevant to a stage's evidence."""
    artifacts: dict[str, object] = {}
    evidence_stages = {"implementation", "code_review", "correction", "verification", "complete"}
    if stage.stage_id in evidence_stages:
        proposal_bytes = (
            read_bytes(proposal_path)
            if proposal_path.exists() else str(state.get("proposal") or "").encode("utf-8")
        )
        artifacts["proposal_sha256"] = hashlib.sha256(proposal_bytes).hexdigest()
        if stage.stage_id == "implementation":
            execution_fingerprint = state.get("execution_fingerprint", {})
            initial = execution_fingerprint.get("initial", {}) if isinstance(execution_fingerprint, dict) else {}
            fingerprint = initial.get("target", {}) if isinstance(initial, dict) else {}
        else:
            fingerprint = fingerprint_repository(target)
        artifacts["target_commit_sha"] = fingerprint.get("commit_sha")
        artifacts["target_working_tree_sha256"] = fingerprint.get("working_tree_sha256")
    return artifacts

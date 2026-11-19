"""Safe stage-record transitions after explicit external recovery decisions."""

from __future__ import annotations

import copy
from typing import Any

from workflow.engine import StageStatus, _json_sha256
from workflow.errors import WorkflowError


def mark_running_stage_completed(
    execution: dict[str, Any],
    stage_id: str,
    output: dict[str, Any],
    *,
    resolution: str,
) -> None:
    """Apply a human-approved resolution to a running stage without saving it."""
    records = execution.get("stages")
    record = records.get(stage_id) if isinstance(records, dict) else None
    if not isinstance(record, dict) or record.get("status") != StageStatus.RUNNING.value:
        raise WorkflowError(f"Interrupted stage {stage_id!r} is not recoverable from its saved engine state.")
    attempts = record.get("attempts")
    if not isinstance(attempts, list) or not attempts or attempts[-1].get("status") != StageStatus.RUNNING.value:
        raise WorkflowError(f"Interrupted stage {stage_id!r} has no running engine attempt to resolve.")
    output_sha256 = _json_sha256(output)
    attempt = attempts[-1]
    attempt.update(
        status=StageStatus.COMPLETED.value,
        output=copy.deepcopy(output),
        output_sha256=output_sha256,
        recovery_resolution=resolution,
    )
    record.update(
        status=StageStatus.COMPLETED.value,
        output=copy.deepcopy(output),
        output_sha256=output_sha256,
        recovery_resolution=resolution,
    )
    record.pop("error", None)

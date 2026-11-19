"""Version 1 workflow state synthesis and stage compatibility mapping."""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Callable

from workflow.engine import StageStatus, WorkflowDefinition, WorkflowDefinitionError, definition_snapshot


def ensure_workflow_execution(
    state: dict[str, Any],
    definition: WorkflowDefinition,
    *,
    max_plan_reviews: int,
    clearly_no_review_findings: Callable[[str], bool],
    redact_error: Callable[[str], str],
) -> dict[str, Any]:
    """Synthesize engine records for a version 1 state without writing it."""
    snapshot = definition_snapshot(definition)
    existing = state.get("workflow_execution")
    if isinstance(existing, dict):
        if existing.get("definition") != snapshot:
            raise WorkflowDefinitionError("Saved execution snapshot does not match the pinned workflow definition.")
        return existing

    stage_ids = [stage.stage_id for stage in definition.stages]
    current_stage = state.get("stage")
    if state.get("human_feedback") and current_stage in {"plan_review_1", "plan_review_2"}:
        current_stage = "plan_review_2_after_revision"
    if current_stage not in stage_ids:
        if state.get("status") == "complete":
            current_index = len(stage_ids)
        else:
            raise WorkflowDefinitionError("Legacy workflow stage is not present in its compatibility definition.")
    else:
        current_index = stage_ids.index(current_stage)
    status = state.get("status")
    records: dict[str, dict] = {}
    review_records = state.get("plan_reviews", [])
    reviews_by_iteration = {
        item.get("iteration"): item for item in review_records
        if isinstance(item, dict) and item.get("valid") is True
    }
    decisions = state.get("plan_review_decisions", [])
    decisions_by_iteration = {
        item.get("iteration"): item for item in decisions if isinstance(item, dict)
    }
    correction_attempts = state.get("correction_attempts", [])
    correction_completed = any(
        isinstance(item, dict) and item.get("status") == "completed" for item in correction_attempts
    )
    verification_completed = bool(state.get("verification_report"))

    def legacy_output(stage_id: str) -> dict | None:
        if stage_id == "planning":
            return {"proposal_version": state.get("proposal_version", 0)}
        if stage_id in {"plan_review_1", "plan_review_2"}:
            iteration = int(stage_id.rsplit("_", 1)[1])
            return {"valid": iteration in reviews_by_iteration} if iteration in reviews_by_iteration else None
        if stage_id.startswith("plan_refine_"):
            iteration = int(stage_id.rsplit("_", 1)[1])
            if iteration == 1:
                second_review = (
                    2 in reviews_by_iteration
                    or current_stage in {"plan_review_2", "plan_refine_2", "human_revision"}
                )
                return {"second_review_needed": second_review, "revision_requested": False}
            return {"revision_requested": False}
        if stage_id == "revision_request":
            return {"requested": bool(state.get("human_feedback"))}
        if stage_id == "plan_review_2_request":
            first_review_count = len(reviews_by_iteration)
            review_limit = state.get("authorization", {}).get("claude_aily", {}).get(
                "max_plan_reviews", max_plan_reviews,
            )
            return {"requested": first_review_count < review_limit and (
                2 in reviews_by_iteration or current_stage in {"plan_review_2", "plan_refine_2"}
            )}
        if stage_id in {
            "revision_plan_review_request",
        }:
            review_limit = state.get("authorization", {}).get("claude_aily", {}).get(
                "max_plan_reviews", max_plan_reviews,
            )
            return {"requested": bool(state.get("human_feedback"))
                    and len(reviews_by_iteration) < review_limit}
        if stage_id == "plan_review_2_after_revision":
            current_reviews = [
                item for item in reviews_by_iteration.values()
                if item.get("proposal_version") == state.get("proposal_version")
                and isinstance(item.get("iteration"), int) and not isinstance(item.get("iteration"), bool)
            ]
            review = max(current_reviews, key=lambda item: item.get("iteration", 0), default=None)
            if state.get("human_feedback") and isinstance(review, dict):
                return {"valid": True}
            return None
        if stage_id == "plan_refine_2_after_revision":
            current_decisions = [
                item for item in decisions_by_iteration.values()
                if item.get("proposal_version") == state.get("proposal_version")
                and isinstance(item.get("iteration"), int) and not isinstance(item.get("iteration"), bool)
            ]
            decision = max(current_decisions, key=lambda item: item.get("iteration", 0), default=None)
            if state.get("human_feedback") and isinstance(decision, dict):
                return {"revision_requested": False}
            return None
        if stage_id == "human_revision":
            return {"revision_applied": True}
        if stage_id == "implementation":
            summary = state.get("implementation_summary")
            return {"summary_sha256": hashlib.sha256(str(summary or "").encode("utf-8")).hexdigest()}
        if stage_id == "code_review":
            has_findings = correction_completed or current_stage == "correction"
            report = state.get("code_review")
            if report and not has_findings:
                has_findings = not clearly_no_review_findings(str(report))
            return {"has_findings": has_findings} if report else None
        if stage_id == "correction":
            return {"performed": correction_completed} if correction_completed else None
        if stage_id == "verification":
            return {"verified": True} if verification_completed else None
        if stage_id == "complete":
            return {"delivered": status == "complete"}
        return None

    for index, stage in enumerate(definition.stages):
        stage_id = stage.stage_id
        if index < current_index or status == "complete":
            output = legacy_output(stage_id)
            stage_status = StageStatus.COMPLETED.value
            if stage_id == "plan_review_2" and 2 not in reviews_by_iteration:
                stage_status, output = StageStatus.NOT_APPLICABLE.value, None
            elif stage_id == "plan_refine_2" and 2 not in decisions_by_iteration:
                stage_status, output = StageStatus.NOT_APPLICABLE.value, None
            elif stage_id == "human_revision" and not state.get("human_feedback"):
                stage_status, output = StageStatus.NOT_APPLICABLE.value, None
            elif (stage_id == "revision_plan_review_request" and not state.get("human_feedback")):
                stage_status, output = StageStatus.NOT_APPLICABLE.value, None
            elif stage_id in {"plan_review_2_after_revision", "plan_refine_2_after_revision"} \
                    and output is None:
                stage_status, output = StageStatus.NOT_APPLICABLE.value, None
            elif stage_id == "correction" and not correction_completed:
                stage_status, output = StageStatus.NOT_APPLICABLE.value, None
            elif stage_id == "verification" and not verification_completed:
                stage_status, output = StageStatus.NOT_APPLICABLE.value, None
            record = {"status": stage_status, "attempts": [], "legacy_migrated": True}
            if output is not None:
                record["output"] = output
            records[stage_id] = record
        elif index == current_index:
            if status == "failed":
                record = {
                    "status": StageStatus.FAILED.value,
                    "error": redact_error(str(state.get("error") or "Legacy stage failed.")),
                    "attempts": [{
                        "attempt": 1, "execution_id": f"legacy-{stage_id}",
                        "status": StageStatus.FAILED.value,
                    }],
                }
            elif status in {"running", "recovery_required"}:
                record = {
                    "status": StageStatus.RUNNING.value,
                    "attempts": [{
                        "attempt": 1, "execution_id": f"legacy-{stage_id}",
                        "status": StageStatus.RUNNING.value,
                    }],
                }
            else:
                record = {"status": StageStatus.PENDING.value, "attempts": []}
            records[stage_id] = record
        else:
            records[stage_id] = {"status": StageStatus.PENDING.value, "attempts": []}
    execution = {"definition": snapshot, "status": "running", "stages": records}
    if status == "awaiting_approval":
        execution.update(status="paused", waiting_stage=current_stage, waiting_reason="Human approval is pending.")
    state["workflow_execution"] = execution
    state["engine_version"] = 2
    return execution

def legacy_stage_id(
    state: dict[str, Any],
    stage_id: str,
    valid_plan_reviews: Callable[[], list[dict]],
    saved_stage_definition: Callable[[dict[str, Any], str], dict[str, Any] | None],
) -> str:
    """Map a stable engine stage ID to the matching version 1 lifecycle ID."""
    allowed_adapters = {
        "plan_review_2_after_revision": {"plan_review_1", "plan_review_2"},
        "plan_refine_2_after_revision": {"plan_refine_1", "plan_refine_2"},
    }
    execution = state.get("workflow_execution")
    stage_records = execution.get("stages", {}) if isinstance(execution, dict) else {}
    record = stage_records.get(stage_id) if isinstance(stage_records, dict) else None
    if stage_id in allowed_adapters and isinstance(record, dict):
        legacy_id = record.get("legacy_stage_id")
        if (not record.get("invalidated") and isinstance(legacy_id, str)
                and legacy_id in allowed_adapters[stage_id]):
            return legacy_id
    if stage_id in {"plan_review_2_after_revision", "plan_refine_2_after_revision"}:
        if stage_id == "plan_refine_2_after_revision":
            prior_review = stage_records.get("plan_review_2_after_revision")
            prior_review_stage = prior_review.get("legacy_stage_id") if isinstance(prior_review, dict) else None
            if prior_review_stage in {"plan_review_1", "plan_review_2"}:
                return "plan_refine_" + prior_review_stage.rsplit("_", 1)[1]
        current_stage = state.get("stage")
        current_prefix = "plan_review_" if stage_id == "plan_review_2_after_revision" else "plan_refine_"
        if current_stage in {current_prefix + "1", current_prefix + "2"}:
            return current_stage
        reviewed_iterations = {
            item.get("iteration") for item in valid_plan_reviews()
            if isinstance(item.get("iteration"), int)
        }
        iteration = 1 if 1 not in reviewed_iterations else 2
        return f"{current_prefix}{iteration}"
    stage = saved_stage_definition(state, stage_id)
    if isinstance(stage, dict):
        adapter_id = stage.get("config", {}).get("legacy_stage")
        if isinstance(adapter_id, str):
            return adapter_id
    return stage_id

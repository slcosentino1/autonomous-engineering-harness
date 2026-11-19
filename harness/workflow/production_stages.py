"""Registry adapters and structured output mapping for production workflows."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from workflow.engine import StageInput, StageRegistry, StageResult, StageStatus
from workflow.errors import WorkflowError
from workflow.stage_services import ProductionStageServices
from workflow.stages.delivery import _execute_delivery
from workflow.stages.implementation import _execute_correction, _execute_implementation
from workflow.stages.planning import (
    _execute_human_revision,
    _execute_noop,
    _execute_plan_refinement,
    _execute_plan_review,
    _execute_planning,
)
from workflow.stages.review import _execute_code_review_or_verification


_STAGE_ACTIONS = {
    "revision_request": ("planning", _execute_noop),
    "plan_review_2_request": ("planning", _execute_noop),
    "revision_plan_review_request": ("planning", _execute_noop),
    "planning": ("planning", _execute_planning),
    "plan_review_1": ("planning", _execute_plan_review),
    "plan_review_2": ("planning", _execute_plan_review),
    "plan_refine_1": ("planning", _execute_plan_refinement),
    "plan_refine_2": ("planning", _execute_plan_refinement),
    "human_revision": ("planning", _execute_human_revision),
    "implementation": ("implementation", _execute_implementation),
    "code_review": ("review", _execute_code_review_or_verification),
    "verification": ("review", _execute_code_review_or_verification),
    "correction": ("implementation", _execute_correction),
    "complete": ("delivery", _execute_delivery),
}


class CallbackStage:
    """A registry entry that delegates a single action to the production stage layer."""

    def __init__(self, action):
        if not callable(action):
            raise TypeError("A production stage action must be callable.")
        self._action = action

    def execute(self, stage_input: StageInput) -> StageResult:
        return self._action(stage_input)


def build_stage_registry(actions: Mapping[str, Callable[[StageInput], StageResult]]) -> StageRegistry:
    """Build a fresh registry for the implementation IDs used by a definition."""
    registry = StageRegistry()
    for implementation, action in actions.items():
        registry.register(implementation, CallbackStage(action))
    return registry


def execute_stage_action(
    stage_input: StageInput,
    legacy_stage: str,
    services: ProductionStageServices,
) -> StageResult:
    """Execute one registered action and return its structured output to the engine."""
    implementation_id = stage_input.definition.implementation
    action = _STAGE_ACTIONS.get(implementation_id)
    if action is None:
        raise WorkflowError(f"Unknown production stage implementation: {implementation_id}")
    service_group, implementation = action
    implementation(stage_input, getattr(services, service_group), legacy_stage)
    output = workflow_stage_output(
        legacy_stage,
        services.state,
        services.valid_plan_reviews,
        services.max_plan_reviews,
        services.clearly_no_review_findings,
    )
    return StageResult(output=output)


def workflow_stage_output(
    stage_id: str,
    state: dict[str, Any],
    valid_plan_reviews: Callable[[], list[dict]],
    max_plan_reviews: int,
    clearly_no_review_findings: Callable[[str], bool],
) -> dict[str, Any]:
    """Build the structured result consumed by generic workflow conditions."""
    if stage_id == "planning":
        return {"proposal_version": state.get("proposal_version"),
                "proposal_sha256": state.get("proposal_artifact_sha256")}
    if stage_id in {"plan_review_1", "plan_review_2"}:
        iteration = int(stage_id.rsplit("_", 1)[1])
        return {"valid": any(
            item.get("iteration") == iteration and item.get("valid") is True
            for item in state.get("plan_reviews", []) if isinstance(item, dict)
        ), "proposal_sha256": state.get("proposal_artifact_sha256")}
    if stage_id.startswith("plan_refine_"):
        iteration = int(stage_id.rsplit("_", 1)[1])
        if iteration == 1:
            decision = next((item for item in reversed(state.get("plan_review_decisions", []))
                             if item.get("iteration") == iteration), {})
            return {"second_review_needed": decision.get("second_review_needed") is True,
                    "revision_requested": False,
                    "proposal_sha256": state.get("proposal_artifact_sha256")}
        return {"revision_requested": False, "proposal_sha256": state.get("proposal_artifact_sha256")}
    if stage_id == "revision_request":
        return {"requested": bool(state.get("human_feedback")),
                "proposal_sha256": state.get("proposal_artifact_sha256")}
    if stage_id == "plan_review_2_request":
        execution = state.get("workflow_execution", {})
        stage_records = execution.get("stages", {}) if isinstance(execution, dict) else {}
        first_refinement = stage_records.get("plan_refine_1", {})
        first_output = first_refinement.get("output", {}) if isinstance(first_refinement, dict) else {}
        revision = stage_records.get("human_revision", {})
        revision_applied = (
            isinstance(revision, dict)
            and revision.get("status") == StageStatus.COMPLETED.value
            and bool(revision.get("attempts"))
        )
        review_limit = state.get("authorization", {}).get("claude_aily", {}).get(
            "max_plan_reviews", max_plan_reviews,
        )
        return {"requested": len(valid_plan_reviews()) < review_limit
                and (first_output.get("second_review_needed") is True or revision_applied),
                "proposal_sha256": state.get("proposal_artifact_sha256")}
    if stage_id == "revision_plan_review_request":
        review_limit = state.get("authorization", {}).get("claude_aily", {}).get(
            "max_plan_reviews", max_plan_reviews,
        )
        return {"requested": len(valid_plan_reviews()) < review_limit,
                "proposal_sha256": state.get("proposal_artifact_sha256")}
    if stage_id == "human_revision":
        return {"revision_applied": True,
                "proposal_sha256": hashlib.sha256(str(state.get("proposal") or "").encode("utf-8")).hexdigest()}
    if stage_id == "implementation":
        return {"summary_sha256": hashlib.sha256(
            str(state.get("implementation_summary") or "").encode("utf-8"),
        ).hexdigest()}
    if stage_id == "code_review":
        campaign = state.get("review_campaign") or {}
        evidence = "\n\n".join([
            str(state.get("code_review") or ""),
            *(item.get("report", "") for item in campaign.get("part_reviews", [])
              if isinstance(item, dict)),
        ])
        return {"has_findings": not clearly_no_review_findings(evidence)}
    if stage_id == "correction":
        attempts = state.get("correction_attempts", [])
        return {"performed": bool(attempts and attempts[-1].get("status") == "completed")}
    if stage_id == "verification":
        result = state.get("integrity", {}).get("verification_result", {})
        return {"verified": isinstance(result, dict) and result.get("status") == "verified"}
    if stage_id == "complete":
        return {"delivered": state.get("status") == "complete"}
    return {}

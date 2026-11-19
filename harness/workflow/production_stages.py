"""Bind code-defined production actions directly into the stage registry."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from workflow.engine import StageInput, StageRegistry, StageResult
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


StageAction = Callable[[StageInput, object], StageResult]
StageRunner = Callable[[StageInput, StageAction, object], StageResult]


@dataclass(frozen=True)
class _ProductionImplementation:
    service_group: str
    action: StageAction


_IMPLEMENTATIONS = {
    "revision_request": _ProductionImplementation("planning", _execute_noop),
    "plan_review_2_request": _ProductionImplementation("planning", _execute_noop),
    "revision_plan_review_request": _ProductionImplementation("planning", _execute_noop),
    "planning": _ProductionImplementation("planning", _execute_planning),
    "plan_review_1": _ProductionImplementation("planning", _execute_plan_review),
    "plan_review_2": _ProductionImplementation("planning", _execute_plan_review),
    "plan_refine_1": _ProductionImplementation("planning", _execute_plan_refinement),
    "plan_refine_2": _ProductionImplementation("planning", _execute_plan_refinement),
    "human_revision": _ProductionImplementation("planning", _execute_human_revision),
    "implementation": _ProductionImplementation("implementation", _execute_implementation),
    "code_review": _ProductionImplementation("review", _execute_code_review_or_verification),
    "verification": _ProductionImplementation("review", _execute_code_review_or_verification),
    "correction": _ProductionImplementation("implementation", _execute_correction),
    "complete": _ProductionImplementation("delivery", _execute_delivery),
}


class ProductionStage:
    """A concrete registry entry bound to one stage action and its scoped services."""

    def __init__(
        self,
        action: StageAction,
        services: object,
        run_stage: StageRunner,
    ) -> None:
        if not callable(action) or not callable(run_stage):
            raise TypeError("Production stage actions and runners must be callable.")
        self._action = action
        self._services = services
        self._run_stage = run_stage

    def execute(self, stage_input: StageInput) -> StageResult:
        return self._run_stage(stage_input, self._action, self._services)


def build_stage_registry(
    implementation_ids: Iterable[str],
    services: ProductionStageServices,
    run_stage: StageRunner,
) -> StageRegistry:
    """Register the concrete action for each implementation used by a definition."""
    registry = StageRegistry()
    for implementation_id in dict.fromkeys(implementation_ids):
        implementation = _IMPLEMENTATIONS.get(implementation_id)
        if implementation is None:
            raise WorkflowError(f"Unknown production stage implementation: {implementation_id}")
        registry.register(
            implementation_id,
            ProductionStage(
                implementation.action,
                getattr(services, implementation.service_group),
                run_stage,
            ),
        )
    return registry

"""Code-defined, versioned workflow profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from workflow.engine import (
    StageCondition,
    StageDefinition,
    StageDependency,
    WorkflowDefinition,
    WorkflowDefinitionError,
    validate_definition,
)


def _stage(name: str, *dependencies: str, optional: bool = False, allow_skip: bool = False) -> StageDefinition:
    capabilities = {
        "planning": ("codex.execute", "documentation.read"),
        "plan_refine_1": ("codex.execute", "documentation.read"),
        "plan_refine_2": ("codex.execute", "documentation.read"),
        "human_revision": ("codex.execute", "documentation.read"),
        "implementation": ("codex.execute",),
        "correction": ("codex.execute",),
    }.get(name, ())
    return StageDefinition(
        stage_id=name,
        implementation=name,
        dependencies=tuple(StageDependency(dep) for dep in dependencies),
        capabilities=capabilities,
        optional=optional,
        allow_skip=allow_skip,
    )


FULL_DEVELOPMENT = WorkflowDefinition(
    name="full-development",
    description="Proposal, plan review, approval, implementation, review, verification, and delivery.",
    version=1,
    stages=(
        _stage("planning"),
        _stage("plan_review_1", "planning"),
        _stage("plan_refine_1", "plan_review_1"),
        StageDefinition(
            "plan_review_2", "plan_review_2", (StageDependency("plan_refine_1"),),
            run_when=StageCondition("plan_refine_1", ("second_review_needed",), "truthy"),
            optional=True, allow_skip=True,
        ),
        StageDefinition(
            "plan_refine_2", "plan_refine_2", (StageDependency("plan_review_2", accept_not_applicable=True),),
            run_when=StageCondition("plan_review_2", ("valid",), "truthy"),
            capabilities=("codex.execute", "documentation.read"),
        ),
        StageDefinition(
            "human_revision", "human_revision",
            (StageDependency("plan_refine_2", accept_not_applicable=True),),
            run_when=StageCondition("plan_refine_2", ("revision_requested",), "truthy"),
            optional=True, allow_skip=True,
            capabilities=("codex.execute", "documentation.read"),
        ),
        StageDefinition(
            "implementation", "implementation",
            (StageDependency("plan_refine_2", accept_not_applicable=True),
             StageDependency("human_revision", accept_not_applicable=True)),
            capabilities=("codex.execute",),
        ),
        _stage("code_review", "implementation"),
        StageDefinition(
            "correction", "correction", (StageDependency("code_review"),),
            run_when=StageCondition("code_review", ("has_findings",), "truthy"),
            capabilities=("codex.execute",),
        ),
        StageDefinition(
            "verification", "verification",
            (StageDependency("correction", accept_not_applicable=True),),
        ),
        _stage("complete", "verification"),
    ),
)

LEGACY_DEFAULT_WORKFLOW = WorkflowDefinition(
    name="legacy-default",
    description="In-memory compatibility definition for version 1 workflows using the existing lifecycle.",
    version=1,
    stages=FULL_DEVELOPMENT.stages,
)

BUG_FIX = WorkflowDefinition(
    name="bugfix",
    description="A shorter implementation flow that requires separate repository policy approval.",
    version=1,
    stages=(
        _stage("implementation"), _stage("code_review", "implementation"),
        _stage("verification", "code_review"), _stage("complete", "verification"),
    ),
)

QUICK_FIX = WorkflowDefinition(
    name="quick-fix",
    description="A minimal implementation flow that requires separate repository policy approval.",
    version=1,
    stages=(
        _stage("implementation"), _stage("verification", "implementation"),
        _stage("complete", "verification"),
    ),
)

IMPLEMENTATION_ONLY = WorkflowDefinition(
    name="implementation-only",
    description="An implementation flow without planning or review gates; disabled by current repository policy.",
    version=1,
    stages=(
        _stage("implementation"), _stage("complete", "implementation"),
    ),
)

BUILT_IN_WORKFLOWS: Mapping[str, WorkflowDefinition] = {
    definition.name: definition
    for definition in (FULL_DEVELOPMENT, BUG_FIX, QUICK_FIX, IMPLEMENTATION_ONLY)
}


@dataclass(frozen=True)
class RepositoryWorkflowConstraints:
    """Trusted required gates supplied by repository policy."""

    required_stages: tuple[str, ...] = (
        "planning", "plan_review_1", "plan_refine_1", "implementation", "code_review", "verification",
        "complete",
    )
    proposal_policy: str = "required"
    approval_policy: str = "always"


def resolve_workflow(name: str, constraints: RepositoryWorkflowConstraints) -> WorkflowDefinition:
    try:
        definition = BUILT_IN_WORKFLOWS[name]
    except KeyError as exc:
        raise WorkflowDefinitionError(f"Unknown workflow profile: {name}") from exc
    validate_definition(definition)
    included = {stage.stage_id for stage in definition.stages}
    missing = [stage for stage in constraints.required_stages if stage not in included]
    if missing:
        raise WorkflowDefinitionError(
            f"Workflow profile {name!r} conflicts with repository policy; required stages are missing: "
            + ", ".join(missing)
        )
    if definition.proposal_policy != constraints.proposal_policy:
        raise WorkflowDefinitionError(f"Workflow profile {name!r} conflicts with the repository proposal policy.")
    if definition.approval_policy != constraints.approval_policy:
        raise WorkflowDefinitionError(f"Workflow profile {name!r} conflicts with the repository approval policy.")
    return definition

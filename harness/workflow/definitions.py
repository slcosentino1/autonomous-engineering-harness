"""Code-defined, versioned workflow profiles."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
            "plan_review_2_request", "plan_review_2_request",
            (StageDependency("plan_refine_1"),),
        ),
        StageDefinition(
            "plan_review_2", "plan_review_2", (StageDependency("plan_review_2_request"),),
            run_when=StageCondition("plan_review_2_request", ("requested",), "truthy"),
        ),
        StageDefinition(
            "plan_refine_2", "plan_refine_2",
            (StageDependency("plan_review_2", accept_skipped=True, accept_not_applicable=True),),
            run_when=StageCondition("plan_review_2", ("valid",), "truthy"),
            capabilities=("codex.execute", "documentation.read"),
        ),
        StageDefinition(
            "revision_request", "revision_request",
            (StageDependency("plan_refine_2", accept_not_applicable=True),),
        ),
        StageDefinition(
            "human_revision", "human_revision", (StageDependency("revision_request"),),
            run_when=StageCondition("revision_request", ("requested",), "truthy"),
            capabilities=("codex.execute", "documentation.read"),
        ),
        StageDefinition(
            "revision_plan_review_request", "revision_plan_review_request",
            (StageDependency("human_revision", accept_not_applicable=True),),
            run_when=StageCondition("human_revision", ("revision_applied",), "truthy"),
        ),
        StageDefinition(
            "plan_review_2_after_revision", "plan_review_2",
            (StageDependency("revision_plan_review_request", accept_not_applicable=True),),
            config={"legacy_stage": "plan_review_2"},
            run_when=StageCondition("revision_plan_review_request", ("requested",), "truthy"),
        ),
        StageDefinition(
            "plan_refine_2_after_revision", "plan_refine_2",
            (StageDependency("plan_review_2_after_revision", accept_skipped=True, accept_not_applicable=True),),
            config={"legacy_stage": "plan_refine_2"},
            run_when=StageCondition("plan_review_2_after_revision", ("valid",), "truthy"),
            capabilities=("codex.execute", "documentation.read"),
        ),
        StageDefinition(
            "implementation", "implementation",
            (StageDependency("plan_refine_1"),),
            config={"requires_approval": True},
            capabilities=("codex.execute",),
        ),
        _stage("code_review", "implementation"),
        StageDefinition(
            "correction", "correction", (StageDependency("code_review"),),
            config={"requires_approval": True},
            run_when=StageCondition("code_review", ("has_findings",), "truthy"),
            capabilities=("codex.execute",),
        ),
        StageDefinition(
            "verification", "verification",
            (StageDependency("correction", accept_skipped=True, accept_not_applicable=True),),
            run_when=StageCondition("correction", ("performed",), "truthy"),
        ),
        StageDefinition(
            "complete", "complete", (StageDependency("verification", accept_not_applicable=True),),
        ),
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


def validate_persisted_definition(definition: WorkflowDefinition) -> None:
    """Ensure saved definitions only encode a trusted profile and supported overrides."""
    validate_definition(definition)
    if definition.name == LEGACY_DEFAULT_WORKFLOW.name:
        trusted = LEGACY_DEFAULT_WORKFLOW
    else:
        trusted = resolve_workflow(definition.name, RepositoryWorkflowConstraints())
    if (definition.description != trusted.description or definition.version != trusted.version
            or definition.failure_policy != trusted.failure_policy
            or definition.execution_policy != trusted.execution_policy
            or definition.approval_policy != trusted.approval_policy
            or definition.proposal_policy != trusted.proposal_policy
            or definition.recovery_policy != trusted.recovery_policy):
        raise WorkflowDefinitionError("Saved workflow definition differs from its trusted profile.")

    trusted_stages = {stage.stage_id: stage for stage in trusted.stages}
    indices: list[int] = []
    for stage in definition.stages:
        expected = trusted_stages.get(stage.stage_id)
        if expected is None:
            raise WorkflowDefinitionError("Saved workflow definition contains an unregistered stage.")
        indices.append(trusted.stages.index(expected))
        actual = stage
        if stage.config.get("skip_requested") is True:
            if not (expected.optional and expected.allow_skip):
                raise WorkflowDefinitionError(f"Required stage {stage.stage_id!r} cannot be skipped.")
            actual = replace(stage, config={key: value for key, value in stage.config.items()
                                            if key != "skip_requested"})
        if actual != expected:
            raise WorkflowDefinitionError("Saved workflow stage differs from its trusted profile.")
    if indices != sorted(set(indices)):
        raise WorkflowDefinitionError("Saved workflow stages are not an ordered profile subset.")

    included = {stage.stage_id for stage in definition.stages}
    constraints = RepositoryWorkflowConstraints()
    missing = [stage for stage in constraints.required_stages if stage not in included]
    if missing:
        raise WorkflowDefinitionError(
            "Saved workflow definition removes repository-required stages: " + ", ".join(missing)
        )


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

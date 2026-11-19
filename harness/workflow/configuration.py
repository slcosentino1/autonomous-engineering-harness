"""Resolve requested workflow profiles without granting runtime capabilities."""

from __future__ import annotations

from dataclasses import replace

from workflow.assessment import Assessment, assess_task
from workflow.definitions import RepositoryWorkflowConstraints, resolve_workflow
from workflow.engine import WorkflowDefinition, definition_snapshot, validate_definition
from workflow.policies import PolicyDecision, resolve_policies


def resolve_workflow_selection(
    name: str,
    task: str,
    *,
    skip_stages: tuple[str, ...] = (),
    selected_stages: tuple[str, ...] | None = None,
) -> tuple[WorkflowDefinition, dict, Assessment, PolicyDecision]:
    """Resolve a profile and reject overrides that weaken trusted repository gates."""
    if skip_stages and selected_stages is not None:
        raise ValueError("Use either --skip or --stages, not both.")
    if len(set(skip_stages)) != len(skip_stages):
        raise ValueError("A stage cannot be skipped more than once.")
    if selected_stages is not None and len(set(selected_stages)) != len(selected_stages):
        raise ValueError("Selected workflow stages must be unique.")

    constraints = RepositoryWorkflowConstraints()
    definition = resolve_workflow(name, constraints)
    stages_by_id = {stage.stage_id: stage for stage in definition.stages}
    if selected_stages is not None:
        unknown = [stage for stage in selected_stages if stage not in stages_by_id]
        if unknown:
            raise ValueError("Unknown selected stages: " + ", ".join(unknown))
        definition = replace(definition, stages=tuple(stages_by_id[stage] for stage in selected_stages))
    elif skip_stages:
        unknown = [stage for stage in skip_stages if stage not in stages_by_id]
        if unknown:
            raise ValueError("Unknown skipped stages: " + ", ".join(unknown))
        skipped = set(skip_stages)
        definition = replace(definition, stages=tuple(
            stage for stage in definition.stages if stage.stage_id not in skipped
        ))

    included = {stage.stage_id for stage in definition.stages}
    missing = [stage for stage in constraints.required_stages if stage not in included]
    if missing:
        raise ValueError(
            "Workflow override conflicts with repository policy; required stages are missing: "
            + ", ".join(missing)
        )
    validate_definition(definition)
    assessment = assess_task(task)
    policy = resolve_policies(
        proposal_policy=definition.proposal_policy,
        approval_policy=definition.approval_policy,
        assessment=assessment,
        constraints=constraints,
    )
    return definition, definition_snapshot(definition), assessment, policy

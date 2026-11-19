"""Small, sequential workflow execution contracts and engine."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol

DEFINITION_SCHEMA_VERSION = 2

class WorkflowDefinitionError(ValueError):
    """A workflow definition or registration is invalid."""


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_APPLICABLE = "not_applicable"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WorkflowContext:
    workflow_id: str
    task: str
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageDependency:
    stage_id: str
    accept_skipped: bool = False
    accept_not_applicable: bool = False


@dataclass(frozen=True)
class StageCondition:
    """A typed comparison against a prior stage's structured output."""

    stage_id: str
    path: tuple[str, ...]
    operator: str
    value: Any = None


@dataclass(frozen=True)
class StageDefinition:
    stage_id: str
    implementation: str
    dependencies: tuple[StageDependency, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    run_when: StageCondition | None = None
    optional: bool = False
    allow_skip: bool = False


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    description: str
    version: int
    stages: tuple[StageDefinition, ...]
    failure_policy: str = "fail"
    execution_policy: str = "sequential"
    approval_policy: str = "always"
    proposal_policy: str = "required"
    recovery_policy: str = "manual"


@dataclass(frozen=True)
class StageInput:
    context: WorkflowContext
    definition: StageDefinition
    predecessor_outputs: Mapping[str, Any]
    attempt: int
    execution_id: str
    configuration: Mapping[str, Any] = field(default_factory=dict)
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    services: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class StageResult:
    """JSON-compatible outcome; outputs must omit secrets and error text must be redacted."""

    status: StageStatus = StageStatus.COMPLETED
    output: Any = None
    error: str | None = None


class Stage(Protocol):
    def execute(self, stage_input: StageInput) -> StageResult: ...


class StageRegistry:
    """Maps stable implementation names to stage implementations."""

    def __init__(self) -> None:
        self._stages: dict[str, Stage] = {}

    def register(self, name: str, implementation: Stage) -> None:
        if not isinstance(name, str) or not name.strip():
            raise WorkflowDefinitionError("Stage implementation names must be non-empty strings.")
        if name in self._stages:
            raise WorkflowDefinitionError(f"Stage implementation is already registered: {name}")
        if not callable(getattr(implementation, "execute", None)):
            raise WorkflowDefinitionError(f"Stage implementation {name!r} must define execute().")
        self._stages[name] = implementation

    def resolve(self, name: str) -> Stage:
        try:
            return self._stages[name]
        except KeyError as exc:
            raise WorkflowDefinitionError(f"No implementation is registered for stage {name!r}.") from exc

    def validate_definition(self, definition: WorkflowDefinition) -> None:
        validate_definition(definition)
        for stage in definition.stages:
            self.resolve(stage.implementation)


def validate_definition(definition: WorkflowDefinition) -> None:
    if not isinstance(definition, WorkflowDefinition):
        raise WorkflowDefinitionError("A workflow definition is required.")
    if not isinstance(definition.name, str) or not definition.name.strip():
        raise WorkflowDefinitionError("Workflow names must be non-empty strings.")
    if not isinstance(definition.description, str) or type(definition.version) is not int or definition.version < 1:
        raise WorkflowDefinitionError("Workflow description and positive definition version are required.")
    if definition.execution_policy != "sequential":
        raise WorkflowDefinitionError("Only sequential workflow execution is currently supported.")
    if definition.recovery_policy not in {"manual", "operation_validated"}:
        raise WorkflowDefinitionError("Unsupported workflow recovery policy.")
    if definition.failure_policy not in {"fail", "warn", "ask"}:
        raise WorkflowDefinitionError("Unsupported workflow failure policy.")
    if definition.approval_policy not in {"always", "never", "auto"}:
        raise WorkflowDefinitionError("Unsupported workflow approval policy.")
    if definition.proposal_policy not in {"required", "optional", "disabled", "automatic"}:
        raise WorkflowDefinitionError("Unsupported workflow proposal policy.")
    if not definition.stages:
        raise WorkflowDefinitionError("Workflow definitions must contain at least one stage.")
    if not isinstance(definition.stages, tuple):
        raise WorkflowDefinitionError("Workflow stages must be an ordered tuple.")

    prior_ids: set[str] = set()
    all_ids: set[str] = set()
    for stage in definition.stages:
        if not isinstance(stage, StageDefinition):
            raise WorkflowDefinitionError("Workflow stages must use StageDefinition values.")
        if not isinstance(stage.stage_id, str) or not stage.stage_id.strip() or stage.stage_id in all_ids:
            raise WorkflowDefinitionError("Workflow stage IDs must be unique, non-empty strings.")
        if not isinstance(stage.implementation, str) or not stage.implementation.strip():
            raise WorkflowDefinitionError(f"Stage {stage.stage_id!r} has no implementation name.")
        if not isinstance(stage.dependencies, tuple):
            raise WorkflowDefinitionError(f"Stage {stage.stage_id!r} dependencies must be an ordered tuple.")
        if type(stage.optional) is not bool or type(stage.allow_skip) is not bool:
            raise WorkflowDefinitionError(f"Stage {stage.stage_id!r} skip policy must use boolean values.")
        if not isinstance(stage.config, Mapping):
            raise WorkflowDefinitionError(f"Stage {stage.stage_id!r} configuration must be a mapping.")
        if (not isinstance(stage.capabilities, tuple)
                or any(not isinstance(capability, str) or not capability for capability in stage.capabilities)
                or len(set(stage.capabilities)) != len(stage.capabilities)):
            raise WorkflowDefinitionError(f"Stage {stage.stage_id!r} capabilities must be unique non-empty strings.")
        if stage.config.get("skip_requested") is True and not (stage.optional and stage.allow_skip):
            raise WorkflowDefinitionError(f"Required stage {stage.stage_id!r} cannot be skipped by configuration.")
        for dependency in stage.dependencies:
            if (not isinstance(dependency, StageDependency) or not isinstance(dependency.stage_id, str)
                    or not dependency.stage_id.strip()
                    or type(dependency.accept_skipped) is not bool
                    or type(dependency.accept_not_applicable) is not bool):
                raise WorkflowDefinitionError(f"Stage {stage.stage_id!r} has an invalid dependency declaration.")
        dependency_ids = [dependency.stage_id for dependency in stage.dependencies]
        if len(set(dependency_ids)) != len(dependency_ids):
            raise WorkflowDefinitionError(f"Stage {stage.stage_id!r} declares a dependency more than once.")
        for dependency in stage.dependencies:
            if dependency.stage_id not in prior_ids:
                raise WorkflowDefinitionError(
                    f"Stage {stage.stage_id!r} depends on unknown or later stage {dependency.stage_id!r}."
                )
        if stage.run_when is not None:
            condition = stage.run_when
            if not isinstance(condition, StageCondition):
                raise WorkflowDefinitionError(f"Stage {stage.stage_id!r} has an invalid run_when condition.")
            if (not isinstance(condition.stage_id, str) or condition.stage_id not in prior_ids
                    or condition.stage_id not in dependency_ids
                    or not isinstance(condition.path, tuple) or not condition.path
                    or any(not isinstance(part, str) or not part for part in condition.path)
                    or not isinstance(condition.operator, str)
                    or condition.operator not in {"equals", "not_equals", "in", "truthy"}):
                raise WorkflowDefinitionError(f"Stage {stage.stage_id!r} has an invalid run_when condition.")
            if condition.operator == "in" and not isinstance(condition.value, (tuple, list, set, frozenset)):
                raise WorkflowDefinitionError("The 'in' condition requires a collection value.")
        prior_ids.add(stage.stage_id)
        all_ids.add(stage.stage_id)


def definition_snapshot(definition: WorkflowDefinition) -> dict[str, Any]:
    """Return a stable, JSON-compatible snapshot suitable for workflow state."""
    validate_definition(definition)
    stages = []
    for stage in definition.stages:
        condition = stage.run_when
        stages.append({
            "stage_id": stage.stage_id,
            "implementation": stage.implementation,
            "dependencies": [
                {
                    "stage_id": dependency.stage_id,
                    "accept_skipped": dependency.accept_skipped,
                    "accept_not_applicable": dependency.accept_not_applicable,
                }
                for dependency in stage.dependencies
            ],
            "config": _json_value(dict(stage.config)),
            "capabilities": list(stage.capabilities),
            "run_when": ({
                "stage_id": condition.stage_id,
                "path": list(condition.path),
                "operator": condition.operator,
                "value": _json_value(condition.value),
            } if condition else None),
            "optional": stage.optional,
            "allow_skip": stage.allow_skip,
        })
    snapshot = {
        "schema_version": DEFINITION_SCHEMA_VERSION,
        "name": definition.name,
        "description": definition.description,
        "version": definition.version,
        "stages": stages,
        "failure_policy": definition.failure_policy,
        "execution_policy": definition.execution_policy,
        "approval_policy": definition.approval_policy,
        "proposal_policy": definition.proposal_policy,
        "recovery_policy": definition.recovery_policy,
    }
    snapshot["sha256"] = hashlib.sha256(json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return snapshot


def definition_from_snapshot(snapshot: Mapping[str, Any]) -> WorkflowDefinition:
    """Reconstruct a trusted code-defined workflow from its validated persisted snapshot."""
    if not isinstance(snapshot, Mapping):
        raise WorkflowDefinitionError("Saved workflow definition is invalid.")
    value = _json_value(snapshot)
    if not isinstance(value, dict) or value.get("schema_version") != DEFINITION_SCHEMA_VERSION:
        raise WorkflowDefinitionError("Saved workflow definition uses an unsupported schema version.")
    supplied_digest = value.pop("sha256", None)
    expected_digest = hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()
    if supplied_digest != expected_digest:
        raise WorkflowDefinitionError("Saved workflow definition failed its integrity check.")
    required = {
        "schema_version", "name", "description", "version", "stages", "failure_policy",
        "execution_policy", "approval_policy", "proposal_policy", "recovery_policy",
    }
    if set(value) != required or not isinstance(value.get("stages"), list):
        raise WorkflowDefinitionError("Saved workflow definition has an invalid shape.")
    stages: list[StageDefinition] = []
    try:
        for item in value["stages"]:
            if not isinstance(item, dict) or set(item) != {
                "stage_id", "implementation", "dependencies", "config", "capabilities", "run_when",
                "optional", "allow_skip",
            }:
                raise WorkflowDefinitionError("Saved workflow stage has an invalid shape.")
            dependencies = item["dependencies"]
            if not isinstance(dependencies, list):
                raise WorkflowDefinitionError("Saved workflow stage dependencies are invalid.")
            parsed_dependencies = tuple(
                StageDependency(
                    dependency["stage_id"],
                    dependency.get("accept_skipped", False),
                    dependency.get("accept_not_applicable", False),
                )
                for dependency in dependencies
                if isinstance(dependency, dict)
                and set(dependency) == {"stage_id", "accept_skipped", "accept_not_applicable"}
            )
            if len(parsed_dependencies) != len(dependencies):
                raise WorkflowDefinitionError("Saved workflow stage dependency is invalid.")
            condition_value = item["run_when"]
            condition = None
            if condition_value is not None:
                if not isinstance(condition_value, dict) or set(condition_value) != {
                    "stage_id", "path", "operator", "value",
                } or not isinstance(condition_value.get("path"), list):
                    raise WorkflowDefinitionError("Saved workflow stage condition is invalid.")
                condition = StageCondition(
                    condition_value["stage_id"], tuple(condition_value["path"]),
                    condition_value["operator"], condition_value["value"],
                )
            if not isinstance(item["config"], dict) or not isinstance(item["capabilities"], list):
                raise WorkflowDefinitionError("Saved workflow stage configuration is invalid.")
            stages.append(StageDefinition(
                stage_id=item["stage_id"], implementation=item["implementation"],
                dependencies=parsed_dependencies, config=item["config"],
                capabilities=tuple(item["capabilities"]), run_when=condition,
                optional=item["optional"], allow_skip=item["allow_skip"],
            ))
        definition = WorkflowDefinition(
            name=value["name"], description=value["description"], version=value["version"],
            stages=tuple(stages), failure_policy=value["failure_policy"],
            execution_policy=value["execution_policy"], approval_policy=value["approval_policy"],
            proposal_policy=value["proposal_policy"], recovery_policy=value["recovery_policy"],
        )
        validate_definition(definition)
    except (KeyError, TypeError) as exc:
        raise WorkflowDefinitionError("Saved workflow definition is invalid.") from exc
    if definition_snapshot(definition) != {**value, "sha256": supplied_digest}:
        raise WorkflowDefinitionError("Saved workflow definition is not canonical.")
    return definition


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise WorkflowDefinitionError("Workflow configuration mapping keys must be strings.")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_json_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise WorkflowDefinitionError("Workflow configuration cannot contain non-finite numbers.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise WorkflowDefinitionError("Workflow configuration and conditions must be JSON-compatible.")


class WorkflowEngine:
    """Execute registered stages in definition order and persist transitions."""

    def __init__(self, registry: StageRegistry, save: Callable[[dict[str, Any]], None]):
        if not callable(save):
            raise WorkflowDefinitionError("WorkflowEngine requires a persistence callback.")
        self.registry = registry
        self.save = save

    def execute(
        self,
        definition: WorkflowDefinition,
        context: WorkflowContext,
        state: dict[str, Any] | None = None,
        *,
        retry_failed: bool = False,
        retry_allowed: Callable[[str, Mapping[str, Any]], bool] | None = None,
        rerun_allowed: Callable[[str, Mapping[str, Any]], bool] | None = None,
        resume_running_allowed: Callable[[str, Mapping[str, Any]], bool] | None = None,
        pause_before: Callable[[StageDefinition, Mapping[str, Any]], str | None] | None = None,
        artifact_provider: Callable[[StageDefinition, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        services: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.registry.validate_definition(definition)
        snapshot = definition_snapshot(definition)
        state = state if state is not None else {
            "definition": snapshot,
            "status": "running",
            "stages": {},
        }
        if state.get("definition") != snapshot:
            raise WorkflowDefinitionError("Saved workflow definition does not match the selected definition.")
        records = state.setdefault("stages", {})
        if not isinstance(records, dict):
            raise WorkflowDefinitionError("Saved stage execution state is invalid.")
        declared_ids = {stage.stage_id for stage in definition.stages}
        valid_statuses = {status.value for status in StageStatus}
        for stage_id, record in records.items():
            if stage_id not in declared_ids or not isinstance(record, dict):
                raise WorkflowDefinitionError("Saved stage execution state does not match the workflow definition.")
            if record.get("status") not in valid_statuses:
                raise WorkflowDefinitionError(f"Saved status for stage {stage_id!r} is invalid.")
            attempts = record.get("attempts", [])
            if not isinstance(attempts, list) or any(not isinstance(attempt, dict) for attempt in attempts):
                raise WorkflowDefinitionError(f"Saved attempts for stage {stage_id!r} are invalid.")
        retry_stage = self._prepare_retry(definition, state, retry_allowed) if retry_failed else None
        state["status"] = "running"
        state.pop("waiting_stage", None)
        state.pop("waiting_reason", None)

        for stage in definition.stages:
            record = records.setdefault(stage.stage_id, {"status": StageStatus.PENDING.value, "attempts": []})
            if not isinstance(record, dict):
                raise WorkflowDefinitionError(f"Saved state for stage {stage.stage_id!r} is invalid.")
            status = record.get("status")
            outputs = {
                dependency.stage_id: records.get(dependency.stage_id, {}).get("output")
                for dependency in stage.dependencies
                if isinstance(records.get(dependency.stage_id), dict)
            }
            completed_statuses = {
                StageStatus.COMPLETED.value, StageStatus.SKIPPED.value, StageStatus.NOT_APPLICABLE.value,
            }
            if status in completed_statuses and "input_artifacts" in record:
                artifacts = record["input_artifacts"]
            else:
                artifacts = artifact_provider(stage, outputs) if artifact_provider is not None else {}
            if not isinstance(artifacts, Mapping):
                raise WorkflowDefinitionError(f"Artifacts for stage {stage.stage_id!r} must be a mapping.")
            artifacts = _json_value(artifacts)
            input_sha256 = _stage_input_hash(stage, context, outputs, records, artifacts)
            if status in {StageStatus.COMPLETED.value, StageStatus.SKIPPED.value,
                          StageStatus.NOT_APPLICABLE.value}:
                if record.pop("legacy_migrated", False):
                    record["input_sha256"] = input_sha256
                    record["input_artifacts"] = copy.deepcopy(dict(artifacts))
                    self._save(state)
                    continue
                if status == StageStatus.SKIPPED.value and "input_sha256" not in record:
                    record["input_sha256"] = input_sha256
                    record["input_artifacts"] = copy.deepcopy(dict(artifacts))
                    self._save(state)
                    continue
                if "input_artifacts" not in record:
                    record["input_artifacts"] = copy.deepcopy(dict(artifacts))
                if record.get("input_sha256") == input_sha256:
                    continue
                try:
                    self.invalidate_dependents(definition, state, stage.stage_id)
                except WorkflowDefinitionError as exc:
                    state["status"] = "blocked"
                    state["recovery_error"] = str(exc)
                    self._save(state)
                    return state
                record = records[stage.stage_id]
                status = record.get("status")
            if (status == StageStatus.FAILED.value and not retry_failed
                    and not (definition.failure_policy == "warn" and stage.optional)):
                state["status"] = "failed"
                self._save(state)
                return state
            if (status == StageStatus.FAILED.value and definition.failure_policy == "warn"
                    and stage.optional and not retry_failed):
                record["warning_accepted"] = True
                warnings = state.setdefault("warnings", [])
                if stage.stage_id not in warnings:
                    warnings.append(stage.stage_id)
                continue
            if status == StageStatus.BLOCKED.value and record.get("rerun_required"):
                if rerun_allowed is not None and rerun_allowed(stage.stage_id, record):
                    record["status"] = StageStatus.PENDING.value
                    record.pop("error", None)
                    status = StageStatus.PENDING.value
                else:
                    state["status"] = "blocked"
                    self._save(state)
                    return state
            if status == StageStatus.RUNNING.value:
                if resume_running_allowed is not None and resume_running_allowed(stage.stage_id, record):
                    attempts = record.get("attempts", [])
                    if attempts and attempts[-1].get("status") == StageStatus.RUNNING.value:
                        attempts[-1]["status"] = "interrupted_recovered"
                    record["status"] = StageStatus.PENDING.value
                    status = StageStatus.PENDING.value
                    record["recovery_resolution"] = "existing_operation_guards_authorized_stage_replay"
                    self._save(state)
                else:
                    record["status"] = StageStatus.BLOCKED.value
                    record["error"] = record.get("error") or "Stage requires recovery before it can run again."
                    state["status"] = "blocked"
                    self._save(state)
                    return state
            if status == StageStatus.BLOCKED.value:
                record["status"] = StageStatus.BLOCKED.value
                record["error"] = record.get("error") or "Stage requires recovery before it can run again."
                state["status"] = "blocked"
                self._save(state)
                return state

            if record.get("rerun_required"):
                authorized = (
                    stage.stage_id == retry_stage
                    or (rerun_allowed is not None and rerun_allowed(stage.stage_id, record))
                )
                if not authorized:
                    record["status"] = StageStatus.BLOCKED.value
                    record["error"] = "Stale stage evidence requires an authorized rerun."
                    state["status"] = "blocked"
                    self._save(state)
                    return state
                record.pop("rerun_required", None)

            dependency_failure = self._dependency_failure(stage, records, definition)
            if dependency_failure:
                record.update(status=StageStatus.BLOCKED.value, error=dependency_failure)
                state["status"] = "blocked"
                self._save(state)
                return state
            if stage.run_when is not None and not _condition_matches(stage.run_when, outputs):
                record.update(
                    status=StageStatus.NOT_APPLICABLE.value,
                    output=None,
                    input_sha256=input_sha256,
                    input_artifacts=copy.deepcopy(dict(artifacts)),
                )
                self._save(state)
                continue

            if stage.config.get("skip_requested") is True:
                record.update(
                    status=StageStatus.SKIPPED.value,
                    output=None,
                    input_sha256=input_sha256,
                    input_artifacts=copy.deepcopy(dict(artifacts)),
                    skipped_by="workflow_override",
                )
                self._save(state)
                continue

            if pause_before is not None:
                pause_reason = pause_before(stage, state)
                if pause_reason is not None:
                    if not isinstance(pause_reason, str) or not pause_reason.strip():
                        raise WorkflowDefinitionError("A stage pause requires a non-empty reason.")
                    state.update(
                        status="paused",
                        waiting_stage=stage.stage_id,
                        waiting_reason=pause_reason.strip(),
                    )
                    record.update(status=StageStatus.PENDING.value, input_sha256=input_sha256)
                    self._save(state)
                    return state

            attempt = len(record.get("attempts", [])) + 1
            attempt_record = {
                "attempt": attempt,
                "execution_id": f"{stage.stage_id}#{attempt}-{uuid.uuid4().hex[:8]}",
                "status": StageStatus.RUNNING.value,
                "input_sha256": input_sha256,
            }
            record.setdefault("attempts", []).append(attempt_record)
            record["status"] = StageStatus.RUNNING.value
            record.pop("error", None)
            self._save(state)
            try:
                result = self.registry.resolve(stage.implementation).execute(
                    StageInput(
                        context=context,
                        definition=stage,
                        predecessor_outputs=outputs,
                        attempt=attempt,
                        execution_id=attempt_record["execution_id"],
                        configuration=copy.deepcopy(dict(stage.config)),
                        artifacts=copy.deepcopy(dict(artifacts)),
                        services=services,
                    ),
                )
                if not isinstance(result, StageResult):
                    raise WorkflowDefinitionError(
                        f"Stage implementation {stage.implementation!r} returned an invalid result."
                    )
                if not isinstance(result.status, StageStatus):
                    raise WorkflowDefinitionError("A stage result must use a valid StageStatus value.")
                if result.status in {StageStatus.PENDING, StageStatus.RUNNING, StageStatus.BLOCKED}:
                    raise WorkflowDefinitionError("A stage implementation returned an engine-owned status.")
                if result.error is not None and not isinstance(result.error, str):
                    raise WorkflowDefinitionError("Stage result errors must be strings when present.")
                _json_value(result.output)
            except Exception as exc:
                safe_error = f"Stage raised {type(exc).__name__}."
                record.update(status=StageStatus.FAILED.value, error=safe_error)
                attempt_record.update(status=StageStatus.FAILED.value, error=safe_error)
                state["status"] = "failed"
                self._save(state)
                return state

            output_sha256 = _json_sha256(result.output)
            record.pop("warning_accepted", None)
            if stage.stage_id in state.get("warnings", []):
                state["warnings"].remove(stage.stage_id)
            record.update(
                status=result.status.value,
                output=copy.deepcopy(result.output),
                input_sha256=input_sha256,
                output_sha256=output_sha256,
                input_artifacts=copy.deepcopy(dict(artifacts)),
            )
            attempt_record.update(
                status=result.status.value,
                output=copy.deepcopy(result.output),
                output_sha256=output_sha256,
            )
            if result.error:
                record["error"] = result.error
                attempt_record["error"] = result.error
            if result.status == StageStatus.FAILED:
                if definition.failure_policy == "warn" and stage.optional:
                    record["warning_accepted"] = True
                    warnings = state.setdefault("warnings", [])
                    if stage.stage_id not in warnings:
                        warnings.append(stage.stage_id)
                    self._save(state)
                    continue
                if definition.failure_policy == "ask":
                    record["status"] = StageStatus.BLOCKED.value
                    record["error"] = result.error or "Stage failure requires a user decision."
                    state["status"] = "blocked"
                    self._save(state)
                    return state
                state["status"] = "failed"
                self._save(state)
                return state
            if result.status == StageStatus.SKIPPED and not stage.allow_skip:
                record.update(status=StageStatus.FAILED.value, error="Stage is not allowed to be skipped.")
                attempt_record.update(status=StageStatus.FAILED.value, error=record["error"])
                state["status"] = "failed"
                self._save(state)
                return state
            self._save(state)

        state["status"] = "complete"
        self._save(state)
        return state

    def skip(
        self,
        definition: WorkflowDefinition,
        state: dict[str, Any],
        stage_id: str,
        context: WorkflowContext,
    ) -> dict[str, Any]:
        self.registry.validate_definition(definition)
        if state.get("definition") != definition_snapshot(definition):
            raise WorkflowDefinitionError("Saved workflow definition does not match the selected definition.")
        stages = {stage.stage_id: stage for stage in definition.stages}
        stage = stages.get(stage_id)
        if stage is None:
            raise WorkflowDefinitionError(f"Unknown stage: {stage_id}")
        if not stage.optional or not stage.allow_skip:
            raise WorkflowDefinitionError(f"Stage {stage_id!r} is required and cannot be skipped.")
        records = state.setdefault("stages", {})
        record = records.setdefault(stage_id, {"status": StageStatus.PENDING.value, "attempts": []})
        retryable_block = (
            record.get("status") == StageStatus.BLOCKED.value
            and bool(record.get("attempts"))
            and record["attempts"][-1].get("status") == StageStatus.FAILED.value
        )
        if record.get("status") not in {StageStatus.PENDING.value, StageStatus.FAILED.value} and not retryable_block:
            raise WorkflowDefinitionError("Only a pending or failed stage can be explicitly skipped.")
        outputs = {
            dependency.stage_id: records.get(dependency.stage_id, {}).get("output")
            for dependency in stage.dependencies
            if isinstance(records.get(dependency.stage_id), dict)
        }
        record.update(
            status=StageStatus.SKIPPED.value,
            skipped_by="policy_or_user",
            input_sha256=_stage_input_hash(stage, context, outputs, records),
            input_artifacts={},
        )
        self._save(state)
        return state

    def invalidate_dependents(
        self,
        definition: WorkflowDefinition,
        state: dict[str, Any],
        stage_id: str,
        *,
        authorized_failed_stage_retry: bool = False,
    ) -> set[str]:
        """Reset downstream evidence after a stage is explicitly rerun."""
        self.registry.validate_definition(definition)
        stages = {stage.stage_id: stage for stage in definition.stages}
        if stage_id not in stages:
            raise WorkflowDefinitionError(f"Unknown stage: {stage_id}")
        invalidated: set[str] = set()
        changed = True
        while changed:
            changed = False
            for stage in definition.stages:
                if stage.stage_id in invalidated:
                    continue
                if any(dep.stage_id == stage_id or dep.stage_id in invalidated for dep in stage.dependencies):
                    invalidated.add(stage.stage_id)
                    changed = True
        records = state.setdefault("stages", {})
        affected = {*invalidated, stage_id}
        for affected_id in affected:
            record = records.get(affected_id)
            if not isinstance(record, dict):
                continue
            status = record.get("status")
            retryable_block = (
                authorized_failed_stage_retry
                and affected_id == stage_id
                and status == StageStatus.BLOCKED.value
                and bool(record.get("attempts"))
                and record["attempts"][-1].get("status") == StageStatus.FAILED.value
            )
            if status == StageStatus.RUNNING.value or (
                status == StageStatus.BLOCKED.value
                and not record.get("rerun_required")
                and not retryable_block
            ):
                raise WorkflowDefinitionError(
                    f"Cannot invalidate stage {affected_id!r} before its recovery state is resolved."
                )
        for invalidated_id in affected:
            record = records.get(invalidated_id)
            if not isinstance(record, dict):
                record = {"attempts": []}
            previous_status = record.get("status")
            record["status"] = StageStatus.PENDING.value
            record["invalidated"] = True
            if previous_status in {StageStatus.COMPLETED.value, StageStatus.SKIPPED.value}:
                record["rerun_required"] = True
            record.pop("output", None)
            record.pop("error", None)
            record.pop("input_sha256", None)
            record.pop("output_sha256", None)
            record.pop("input_artifacts", None)
            records[invalidated_id] = record
        state["status"] = "running"
        self._save(state)
        return invalidated

    def continue_from(
        self,
        definition: WorkflowDefinition,
        state: dict[str, Any],
        stage_id: str,
        *,
        rerun_allowed: Callable[[str, Mapping[str, Any]], bool],
        requested_at: str,
    ) -> set[str]:
        """Invalidate a completed stage and its dependents for an explicit continuation."""
        self.registry.validate_definition(definition)
        if state.get("definition") != definition_snapshot(definition):
            raise WorkflowDefinitionError("Saved workflow definition does not match the selected definition.")
        records = state.get("stages")
        if not isinstance(records, dict):
            raise WorkflowDefinitionError("Saved stage execution state is invalid.")
        stage = next((item for item in definition.stages if item.stage_id == stage_id), None)
        if stage is None:
            raise WorkflowDefinitionError(f"Unknown stage: {stage_id}")
        stage_record = records.get(stage_id)
        allowed_start_statuses = {
            StageStatus.COMPLETED.value, StageStatus.SKIPPED.value,
            StageStatus.NOT_APPLICABLE.value, StageStatus.FAILED.value,
        }
        if not isinstance(stage_record, dict) or stage_record.get("status") not in allowed_start_statuses:
            raise WorkflowDefinitionError(
                "Continue-from requires a completed, skipped, not-applicable, or failed stage."
            )
        affected = {stage_id}
        changed = True
        while changed:
            changed = False
            for candidate in definition.stages:
                if candidate.stage_id not in affected and any(
                    dependency.stage_id in affected for dependency in candidate.dependencies
                ):
                    affected.add(candidate.stage_id)
                    changed = True
        for affected_id in affected:
            record = records.get(affected_id)
            if not isinstance(record, dict):
                continue
            status = record.get("status")
            if status in {StageStatus.RUNNING.value, StageStatus.BLOCKED.value}:
                raise WorkflowDefinitionError(
                    f"Cannot continue from {stage_id!r} while dependent stage {affected_id!r} "
                    "requires recovery."
                )
            if status in {
                StageStatus.COMPLETED.value, StageStatus.SKIPPED.value,
                StageStatus.NOT_APPLICABLE.value, StageStatus.FAILED.value,
            } and not rerun_allowed(affected_id, record):
                raise WorkflowDefinitionError(
                    f"Continue-from is not authorized to rerun stage {affected_id!r}."
                )
        previous = state.get("continuation_history", [])
        if not isinstance(previous, list):
            raise WorkflowDefinitionError("Saved continuation history is invalid.")
        state["continuation_history"] = [
            *previous,
            {
                "stage_id": stage_id,
                "invalidated_stages": [item.stage_id for item in definition.stages
                                       if item.stage_id in affected],
                "requested_at": requested_at,
            },
        ]
        try:
            self.invalidate_dependents(definition, state, stage_id)
        except Exception:
            if previous:
                state["continuation_history"] = previous
            else:
                state.pop("continuation_history", None)
            raise
        return affected

    @staticmethod
    def _dependency_failure(
        stage: StageDefinition,
        records: Mapping[str, Any],
        definition: WorkflowDefinition,
    ) -> str | None:
        stage_definitions = {item.stage_id: item for item in definition.stages}
        for dependency in stage.dependencies:
            dependency_record = records.get(dependency.stage_id, {})
            actual = dependency_record.get("status")
            accepted = {StageStatus.COMPLETED.value}
            if dependency.accept_skipped:
                accepted.add(StageStatus.SKIPPED.value)
            if dependency.accept_not_applicable:
                accepted.add(StageStatus.NOT_APPLICABLE.value)
            dependency_definition = stage_definitions[dependency.stage_id]
            if (definition.failure_policy == "warn" and dependency_definition.optional
                    and dependency_record.get("warning_accepted") is True):
                accepted.add(StageStatus.FAILED.value)
            if actual not in accepted:
                return f"Required dependency {dependency.stage_id!r} has status {actual!r}."
        return None

    def _prepare_retry(
        self,
        definition: WorkflowDefinition,
        state: dict[str, Any],
        retry_allowed: Callable[[str, Mapping[str, Any]], bool] | None,
    ) -> str:
        records = state["stages"]
        failed = [
            stage.stage_id for stage in definition.stages
            if records.get(stage.stage_id, {}).get("status") == StageStatus.FAILED.value
            or (
                records.get(stage.stage_id, {}).get("status") == StageStatus.BLOCKED.value
                and bool(records.get(stage.stage_id, {}).get("attempts"))
                and records[stage.stage_id]["attempts"][-1].get("status") == StageStatus.FAILED.value
            )
        ]
        if len(failed) != 1:
            raise WorkflowDefinitionError("Retry requires exactly one saved failed stage.")
        failed_record = records[failed[0]]
        if retry_allowed is None or not retry_allowed(failed[0], failed_record):
            raise WorkflowDefinitionError("Retry is blocked because stage recovery did not authorize it.")
        self.invalidate_dependents(
            definition, state, failed[0], authorized_failed_stage_retry=True,
        )
        records[failed[0]].pop("rerun_required", None)
        return failed[0]

    def _save(self, state: dict[str, Any]) -> None:
        self.save(state)


def _condition_matches(condition: StageCondition, outputs: Mapping[str, Any]) -> bool:
    missing = object()
    current = outputs.get(condition.stage_id, missing)
    for part in condition.path:
        if not isinstance(current, Mapping) or part not in current:
            current = missing
            break
        current = current[part]
    if current is missing:
        return False
    if condition.operator == "equals":
        return _typed_equal(current, condition.value)
    if condition.operator == "not_equals":
        return not _typed_equal(current, condition.value)
    if condition.operator == "in":
        return any(_typed_equal(current, candidate) for candidate in condition.value)
    if condition.operator == "truthy":
        return bool(current)
    raise WorkflowDefinitionError(f"Unsupported condition operator: {condition.operator}")


def _typed_equal(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _json_sha256(value: Any) -> str:
    normalized = _json_value(value)
    return hashlib.sha256(json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _stage_input_hash(
    stage: StageDefinition,
    context: WorkflowContext,
    outputs: Mapping[str, Any],
    records: Mapping[str, Any],
    artifacts: Mapping[str, Any] | None = None,
) -> str:
    dependencies = {
        dependency.stage_id: {
            "status": records.get(dependency.stage_id, {}).get("status"),
            "output": outputs.get(dependency.stage_id),
        }
        for dependency in stage.dependencies
    }
    return _json_sha256({
        "workflow_id": context.workflow_id,
        "task": context.task,
        "attributes": context.attributes,
        "stage_id": stage.stage_id,
        "implementation": stage.implementation,
        "config": stage.config,
        "dependencies": dependencies,
        "artifacts": artifacts or {},
    })

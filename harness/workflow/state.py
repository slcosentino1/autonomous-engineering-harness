"""Authoritative persisted state and operation journal for one workflow."""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from collections.abc import Iterator, MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow import authorization as _authorization
from workflow import persistence as _persistence


MAX_OPERATION_HISTORY = 100
MAX_PLANNING_FAILURE_OUTPUT_CHARS = 50_000
MAX_PLANNING_FAILURE_RECORDS = 3
TRACE_STAGES = frozenset({
    "planning", "plan_review_1", "plan_review_2", "plan_refine_1", "plan_refine_2",
    "human_revision", "implementation", "code_review", "correction", "verification", "complete",
})


class WorkflowError(RuntimeError):
    """An actionable workflow error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _redact(value: str) -> str:
    from claude_reviewer import redact

    return redact(value)


@dataclass(frozen=True)
class RecoveryFacts:
    operation_id: str | None
    operation_type: str | None
    operation_stage: str | None
    operation_status: str | None
    operation_sandbox: str | None
    process_presence: str
    target_change_paths: tuple[str, ...]
    target_changes_overlap_preexisting: bool
    target_fingerprint_status: str | None
    claude_budget_unknown: bool
    claude_budget_usd: object
    claude_spent_usd: object
    claude_call_cap_usd: object
    action: str | None
    verified_claude_cost_usd: float | None
    confirm_provider_inactive: bool
    confirm_process_inactive: bool
    confirm_target_reviewed: bool


@dataclass(frozen=True)
class RecoveryDecision:
    kind: str
    error: str | None = None
    resolution: str | None = None
    resolution_cost_usd: float | None = None


def decide_recovery(facts: RecoveryFacts) -> RecoveryDecision:
    """Apply existing recovery policy to explicit facts without inspecting or mutating state."""
    if facts.action is None:
        return RecoveryDecision("requires_recovery")
    if facts.operation_id is None:
        return RecoveryDecision("blocked", "Recovery has no verifiable operation record; leave the workflow blocked.")

    if facts.action == "accept-implementation":
        if not (facts.operation_type == "codex" and facts.operation_stage == "implementation"
                and facts.operation_status == "interrupted"):
            return RecoveryDecision(
                "blocked", "Only an interrupted Codex implementation can be accepted for independent review.",
            )
        if not facts.confirm_target_reviewed:
            return RecoveryDecision(
                "blocked",
                "Inspect the target changes and pass --confirm-target-reviewed before accepting an interrupted implementation.",
            )
        if not facts.target_change_paths:
            return RecoveryDecision("blocked", "No target changes were found; an interrupted implementation cannot be accepted.")
        if facts.target_changes_overlap_preexisting:
            return RecoveryDecision(
                "blocked",
                "Target changes overlap paths that were already changed before this workflow; "
                "their origin cannot be established safely. Leave the workflow blocked for manual inspection.",
            )
        if facts.target_fingerprint_status != "captured":
            return RecoveryDecision("blocked", "Could not capture the target fingerprint for review provenance.")
        return RecoveryDecision("accept-implementation", resolution="human_accepted_current_target_for_independent_review")

    if facts.action != "retry":
        return RecoveryDecision("blocked", f"Unsupported recovery action: {facts.action}.")
    if facts.operation_type == "claude":
        if not facts.claude_budget_unknown:
            return RecoveryDecision("blocked", "Claude recovery does not require unknown-spend resolution; resume normally.")
        if facts.verified_claude_cost_usd is None or not facts.confirm_provider_inactive:
            return RecoveryDecision(
                "blocked",
                "Before retrying Claude, verify the final charge and confirm the prior provider call is inactive; "
                "pass --verified-claude-cost-usd and --confirm-provider-inactive.",
            )
        try:
            cost = Decimal(str(facts.verified_claude_cost_usd))
            budget = Decimal(str(facts.claude_budget_usd))
            spent = Decimal(str(facts.claude_spent_usd))
            active_cap = Decimal(str(facts.claude_call_cap_usd))
        except Exception:
            return RecoveryDecision("blocked", "Verified Claude recovery cost is invalid.")
        if (not cost.is_finite() or cost < 0 or cost > active_cap or spent + cost > budget):
            return RecoveryDecision(
                "blocked", "Verified Claude recovery cost exceeds the active call cap or remaining workflow budget.",
            )
        return RecoveryDecision(
            "retry", resolution="user_verified_final_provider_cost_and_inactive_call",
            resolution_cost_usd=float(cost),
        )

    if facts.operation_type != "codex":
        return RecoveryDecision("blocked", "This recovery action is not supported for the interrupted operation.")
    if facts.operation_status not in {"interrupted", "failed", "unknown"}:
        return RecoveryDecision("blocked", "Only an unresolved Codex operation can be retried during recovery.")
    if not facts.confirm_process_inactive:
        return RecoveryDecision(
            "blocked",
            "Confirm that the prior Codex process is inactive with --confirm-process-inactive before retrying.",
        )
    if facts.operation_sandbox == "workspace-write" and not facts.confirm_target_reviewed:
        return RecoveryDecision(
            "blocked",
            "Inspect target changes and pass --confirm-target-reviewed before retrying a write operation.",
        )
    if facts.operation_sandbox == "workspace-write" and facts.target_fingerprint_status != "captured":
        return RecoveryDecision("blocked", "Could not capture target fingerprint for review provenance.")
    return RecoveryDecision("retry", resolution="user_approved_retry_after_target_inspection")


class WorkflowState(MutableMapping[str, Any]):
    """Owns the workflow document, its lock invariant, and durable operation order."""

    def __init__(
        self,
        state_path: Path,
        *,
        workflow_id: str,
        target: Path,
        default_limits: dict[str, int],
    ) -> None:
        self.path = state_path
        self.workflow_id = workflow_id
        self.target = target
        self.default_limits = dict(default_limits)
        self._data: dict[str, Any] = {}
        self.lock_held = False
        self._workflow_runtime_started_monotonic: float | None = None

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WorkflowState):
            return self._data == other._data
        if isinstance(other, MutableMapping):
            return self._data == dict(other)
        return NotImplemented

    def replace(self, value: dict[str, Any]) -> None:
        if not isinstance(value, dict):
            raise WorkflowError("Workflow state must be a JSON object.")
        self._data = value

    @property
    def document(self) -> dict[str, Any]:
        """Expose the existing JSON document to the command coordinator."""
        return self._data

    def load(self) -> dict[str, Any]:
        """Load this workflow's saved document without applying recovery policy."""
        try:
            _persistence.safe_artifact(self.path)
        except _persistence.PersistenceError as exc:
            raise WorkflowError(str(exc)) from exc
        try:
            self.replace(_persistence.read_json(self.path))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"Workflow state is missing or invalid: {self.path}") from exc
        if self._data.get("workflow_id") != self.workflow_id or self._data.get("target") != str(self.target):
            raise WorkflowError("Workflow state belongs to another target or workflow ID.")
        return self._data

    def migrate_legacy_resource_usage(self, authorization: dict) -> dict:
        """Fill the existing resource-usage fields for workflow states predating the counter."""
        usage = self._data.get("resource_usage")
        if not isinstance(usage, dict):
            operations = self._data.get("operation_history", [])
            claude_contexts = self._data.get("claude_contexts", [])
            usage = {
                "codex_calls": sum(
                    isinstance(item, dict) and item.get("operation_type") == "codex"
                    for item in operations
                ),
                "claude_calls": sum(
                    isinstance(item, dict) and item.get("call_state") != "validation_failed"
                    for item in claude_contexts
                ),
                "provider_runtime_seconds": round(sum(
                    (item.get("duration_ms", 0) or 0) / 1000
                    for item in operations if isinstance(item, dict)
                    and isinstance(item.get("duration_ms"), (int, float))
                ), 3),
                "workflow_runtime_seconds": 0.0,
                "correction_attempts": len(self._data.get("correction_attempts", [])),
            }
            if len(operations) >= MAX_OPERATION_HISTORY and "execution_limits" not in authorization:
                usage["codex_calls"] = max(usage["codex_calls"], self.execution_limits()["max_codex_calls"])
                usage["provider_runtime_seconds"] = max(
                    usage["provider_runtime_seconds"],
                    self.execution_limits()["max_provider_runtime_seconds"],
                )
            self._data["resource_usage"] = usage
        return self.resource_usage()

    def save(self) -> None:
        if not self.lock_held:
            raise WorkflowError("Workflow state writes require the workflow process lock.")
        self._data["updated_at"] = _now()
        self._data["workflow_status"] = self._data.get("status")
        current_stage = self._data.get("stage")
        for operation in self._data.get("operation_history", []):
            if (operation.get("status") == "completed" and operation.get("result_text") is not None
                    and operation.get("stage") != current_stage):
                operation["result_text"] = None
                operation["result_consumed"] = True
                operation["result_consumed_at"] = _now()
        authorization = self._data.get("authorization")
        if isinstance(authorization, dict):
            authorization["scope_sha256"] = _authorization.authorization_scope_digest(authorization)
        try:
            _persistence.write_json(self.path, self._data)
        except _persistence.PersistenceError as exc:
            raise WorkflowError(str(exc)) from exc

    def operation(self, operation_id: str | None) -> dict | None:
        if not operation_id:
            return None
        return next((
            item for item in reversed(self._data.get("operation_history", []))
            if item.get("operation_id") == operation_id
        ), None)

    def execution_limits(self) -> dict:
        saved = self._data.get("authorization", {}).get("execution_limits")
        if isinstance(saved, dict):
            return saved
        reviews = self._data.get("authorization", {}).get("claude_aily", {}).get(
            "max_code_reviews", self.default_limits["max_code_review_calls"],
        )
        max_claude_calls = _authorization.MAX_PLAN_REVIEWS + reviews
        return {
            "max_codex_calls": self.default_limits["max_codex_calls"],
            "codex_timeout_seconds": self.default_limits["codex_timeout_seconds"],
            "claude_timeout_seconds": self.default_limits["claude_timeout_seconds"],
            "workflow_timeout_seconds": self.default_limits["workflow_timeout_seconds"],
            "max_correction_attempts": _authorization.MAX_CORRECTION_ATTEMPTS,
            "max_claude_calls": max_claude_calls,
            "max_provider_runtime_seconds": (
                self.default_limits["max_codex_calls"] * self.default_limits["codex_timeout_seconds"]
                + max_claude_calls * self.default_limits["claude_timeout_seconds"]
            ),
        }

    def resource_usage(self) -> dict:
        usage = self._data.setdefault("resource_usage", {})
        if not isinstance(usage, dict):
            raise WorkflowError("Saved workflow resource usage is invalid.")
        usage.setdefault("codex_calls", 0)
        usage.setdefault("claude_calls", 0)
        usage.setdefault("provider_runtime_seconds", 0.0)
        usage.setdefault("workflow_runtime_seconds", 0.0)
        usage.setdefault("correction_attempts", 0)
        for key in ("codex_calls", "claude_calls", "correction_attempts"):
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise WorkflowError("Saved workflow resource usage is invalid.")
        for key in ("provider_runtime_seconds", "workflow_runtime_seconds"):
            value = usage.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or value < 0):
                raise WorkflowError("Saved workflow resource usage is invalid.")
        return usage

    def workflow_runtime_remaining(self) -> float:
        active = self._workflow_runtime_started_monotonic
        active_seconds = max(0.0, time.monotonic() - active) if active is not None else 0.0
        return (
            float(self.execution_limits()["workflow_timeout_seconds"])
            - float(self.resource_usage().get("workflow_runtime_seconds", 0.0))
            - active_seconds
        )

    def begin_workflow_runtime(self) -> None:
        if not self.lock_held:
            raise WorkflowError("Workflow runtime accounting requires the workflow process lock.")
        self._workflow_runtime_started_monotonic = time.monotonic()
        self._data["workflow_active_started_at"] = _now()
        self.save()

    def finish_workflow_runtime(self) -> None:
        started = self._workflow_runtime_started_monotonic
        if started is None:
            return
        elapsed = max(0.0, time.monotonic() - started)
        usage = self.resource_usage()
        usage["workflow_runtime_seconds"] = round(
            float(usage.get("workflow_runtime_seconds", 0.0)) + elapsed, 3,
        )
        self._data.pop("workflow_active_started_at", None)
        self._workflow_runtime_started_monotonic = None
        self.save()

    def provider_timeout(self, provider: str) -> float:
        limits = self.execution_limits()
        usage = self.resource_usage()
        timeout_key = "codex_timeout_seconds" if provider == "codex" else "claude_timeout_seconds"
        remaining_runtime = (
            float(limits["max_provider_runtime_seconds"])
            - float(usage.get("provider_runtime_seconds", 0.0))
        )
        remaining_workflow = self.workflow_runtime_remaining()
        if remaining_runtime <= 0:
            raise WorkflowError("The authorized workflow provider-runtime limit is exhausted.")
        if remaining_workflow <= 0:
            raise WorkflowError("The authorized workflow execution-time limit is exhausted.")
        return min(float(limits[timeout_key]), remaining_runtime, remaining_workflow)

    def record_operation_runtime(self, operation: dict, duration_ms: int | None = None) -> None:
        if operation.get("runtime_accounted") is True:
            return
        measured = duration_ms if isinstance(duration_ms, int) and duration_ms >= 0 else None
        if measured is None:
            timeout = operation.get("timeout_seconds", 0)
            measured = (
                round(float(timeout) * 1000)
                if operation.get("launch_state") != "not_started"
                and isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0
                else 0
            )
        operation["duration_ms"] = measured
        operation["runtime_accounted"] = True
        usage = self.resource_usage()
        usage["provider_runtime_seconds"] = round(
            float(usage.get("provider_runtime_seconds", 0.0)) + measured / 1000,
            3,
        )

    def new_operation(
        self,
        operation_type: str,
        *,
        input_sha256: str | None = None,
        **details: object,
    ) -> dict:
        history = self._data.setdefault("operation_history", [])
        operation = {
            "operation_id": uuid.uuid4().hex,
            "operation_type": operation_type,
            "stage": self._data.get("stage"),
            "status": "started",
            "started_at": _now(),
            "finished_at": None,
            "process_id": None,
            "owner_pid": os.getpid(),
            "launch_state": "not_started",
            "exit_code": None,
            "input_sha256": input_sha256,
            "result_sha256": None,
            "result_text": None,
            "result_consumed": False,
            **details,
        }
        if operation_type == "codex":
            operation.update({
                "workflow_id": self.workflow_id,
                "invocation_at": operation["started_at"],
                "duration_ms": None,
                "stdout_sha256": None,
                "stdout_bytes": None,
                "stdout_capture_status": "not_captured",
                "stdout_truncated": None,
                "stderr_sha256": None,
                "stderr_bytes": None,
                "stderr_capture_status": "not_captured",
                "stderr_truncated": None,
                "stdout_capture_representation": "unavailable",
                "stderr_capture_representation": "unavailable",
                "last_message_file_sha256": None,
                "last_message_file_bytes": None,
                "last_message_file_status": "not_captured",
                "jsonl_event_coverage": "not_captured",
                "jsonl_event_count": None,
                "jsonl_parse_error_count": None,
                "jsonl_unrecognized_event_count": None,
                "codex_cli_event_counts": {},
                "codex_cli_turn_status": "not_reported",
                "tool_invocations": [],
                "tool_event_coverage": "not_captured",
                "reported_usage": None,
                "usage_source": "not_reported",
                "reported_model": None,
                "reported_model_source": "not_reported",
            })
        history.append(operation)
        usage = self.resource_usage()
        if operation_type == "codex":
            usage["codex_calls"] = int(usage.get("codex_calls", 0)) + 1
        elif operation_type == "claude":
            usage["claude_calls"] = int(usage.get("claude_calls", 0)) + 1
        self._data["active_operation_id"] = operation["operation_id"]
        if len(history) > MAX_OPERATION_HISTORY:
            removable = [item for item in history if item is not operation
                         and item.get("status") != "started" and item.get("result_text") is None]
            remove_ids = {item["operation_id"] for item in removable[:len(history) - MAX_OPERATION_HISTORY]}
            self._data["operation_history"] = [
                item for item in history if item.get("operation_id") not in remove_ids
            ]
        return operation

    def operation_process_started(self, operation_id: str, process_id: int) -> None:
        operation = self.operation(operation_id)
        if operation is None or operation.get("status") != "started":
            raise WorkflowError("The tracked workflow operation is no longer active.")
        operation["process_id"] = process_id
        operation["launch_state"] = "launched"
        operation["process_started_at"] = _now()
        self.save()

    def operation_launching(self, operation_id: str) -> None:
        operation = self.operation(operation_id)
        if operation is None or operation.get("status") != "started":
            raise WorkflowError("The tracked workflow operation is no longer active.")
        operation["launch_state"] = "launching"
        operation["launch_attempted_at"] = _now()
        self.save()

    def finish_operation(
        self,
        operation_id: str,
        *,
        status: str,
        outcome: str,
        exit_code: int | None = None,
        error_category: str | None = None,
        result_text: str | None = None,
        result_sha256: str | None = None,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
        launch_state: str | None = None,
        evidence: dict | None = None,
    ) -> dict:
        operation = self.operation(operation_id)
        if operation is None:
            raise WorkflowError("The tracked workflow operation is missing from persisted state.")
        operation.update({
            "status": status,
            "finished_at": _now(),
            "outcome": _redact(outcome)[:500],
            "exit_code": exit_code,
            "error_category": error_category,
            "result_sha256": result_sha256,
            "result_text": result_text,
            "cost_usd": cost_usd,
        })
        if duration_ms is not None:
            operation["duration_ms"] = duration_ms
        if launch_state is not None:
            operation["launch_state"] = launch_state
        if evidence:
            operation.update(evidence)
        self.record_operation_runtime(operation, operation.get("duration_ms"))
        if self._data.get("active_operation_id") == operation_id:
            self._data["active_operation_id"] = None
        self.save()
        return operation

    def transition(self, fields: dict[str, Any]) -> None:
        """Apply only existing state fields after a stage has completed its durable work."""
        allowed = {"stage", "status", "error"}
        unknown = set(fields) - allowed
        if unknown:
            raise WorkflowError("Stage outcome contains unsupported fields: " + ", ".join(sorted(unknown)))
        self._data.update(fields)

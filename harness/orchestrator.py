#!/usr/bin/env python3
"""Run the plan, approval, implementation, review, and delivery workflow."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path
from typing import Callable

from workflow import workspace as _workspace

_workspace.load_harness_environment()

import claude_reviewer
from observability.tracer import (
    isolated_context,
    safe_workflow_reference,
)
from workflow import authorization as _authorization
from workflow.codex_executor import (
    READ_ONLY_WEB_ACCESS_INSTRUCTION,
    assert_no_project_mcp_servers as _assert_no_project_mcp_servers,
    build_codex_command,
    configured_codex_mcp_servers as _configured_codex_mcp_servers,
)
from workflow.planning_review import (
    ARCHITECTURAL_DECISION_OUTPUT, COMMITMENT_LEDGER_GUIDANCE, PLAN_REVIEW_GUIDANCE,
    PLANNING_PRINCIPLES,
)
from workflow.skills import MAX_PROMPT_FILE_INVENTORY_CHARS
from workflow.state import (
    MAX_PLANNING_FAILURE_OUTPUT_CHARS, MAX_PLANNING_FAILURE_RECORDS, TRACE_STAGES,
    RecoveryFacts, WorkflowError, WorkflowState, decide_recovery,
)
from workflow.workspace import MAX_EXECUTION_CHANGE_EXAMPLES
from workflow.execution import (
    ExecutionConfig,
    StageOutcome,
    WorkflowExecution,
    _clearly_no_review_findings,
    _file_inventory_section,
    _format_context_prompt,
    _guidance_for_claude,
    _guidance_for_codex,
    _planning_read_only_authorized_urls,
    _post_correction_dispositions,
    _sha256_text,
)
from workflow import cli as _cli
from workflow import persistence as _persistence
from workflow import process_locks as _process_locks
from workflow import process_control as _process_control
from workflow import planning_review as _planning_review
from workflow import traceability as _traceability
from workflow import github as _github
from workflow.codex_evidence import (
    MAX_CODEX_TOOL_NAMES,
    _CODEX_JSONL_EVENT_TYPES,
    _CODEX_NON_TOOL_ITEM_TYPES,
    _CODEX_TOOL_ITEM_TYPES,
    _CODEX_USAGE_FIELDS,
    _codex_jsonl_evidence,
    _codex_langfuse_metadata,
    _codex_stream_evidence,
    _json_line_message,
    _output_bytes,
)
from workflow.review import (
    PART_REVIEW_FIELDS,
    PART_REVIEW_SECTIONS,
    PART_REVIEW_SEVERITIES,
    PART_REVIEW_VALIDATOR_VERSION,
    CLAUDE_CODE_REVIEW_GUIDANCE,
    ReviewCampaign as _ReviewCampaign,
    ReviewCampaignError as _ReviewCampaignError,
    _safe_review_path,
    ReviewDiff,
    ReviewSegment,
    _integration_report_is_structured,
    _normalize_review_text,
    _part_coverage,
    _path_affinity,
    _review_patch_sha256,
    _review_partition_error,
    _safe_patch_boundaries,
    _validated_campaign_reports,
    build_review_diff as _review_diff,
    parse_part_review_report,
    render_review_feedback,
    recover_review_campaign as _recover_review_campaign,
    review_source_hash_matching_evidence as _review_source_hash_matching_evidence,
)
from workflow.review_input import (
    KNOWN_BINARY_SIGNATURES,
    OS_METADATA_NAMES,
    _baseline_blob,
    _binary_content_reason,
    _os_metadata_reason,
)
from workflow.workspace import (
    has_symlink_component as _has_symlink_component,
    is_sensitive as _is_sensitive,
    project_skills as _project_skills,
    read_instruction_files as _read_instruction_files,
    skill_metadata as _skill_metadata,
)


WORKFLOW_ROOT = _workspace.WORKFLOW_ROOT
WORKFLOW_LOCK_NAME_RE = _workspace.WORKFLOW_LOCK_NAME_RE
MAX_PLAN_REVIEWS = _authorization.MAX_PLAN_REVIEWS
DEFAULT_CLAUDE_BUDGET_USD = _cli.DEFAULT_CLAUDE_BUDGET_USD
DEFAULT_MAX_CODE_REVIEW_CALLS = _authorization.DEFAULT_MAX_CODE_REVIEW_CALLS
MAX_AUTHORIZED_CODE_REVIEW_CALLS = _authorization.MAX_AUTHORIZED_CODE_REVIEW_CALLS
DEFAULT_MAX_CODEX_CALLS = _authorization.DEFAULT_MAX_CODEX_CALLS
MAX_AUTHORIZED_CODEX_CALLS = _authorization.MAX_AUTHORIZED_CODEX_CALLS
DEFAULT_CODEX_TIMEOUT_SECONDS = _authorization.DEFAULT_CODEX_TIMEOUT_SECONDS
DEFAULT_CLAUDE_TIMEOUT_SECONDS = _authorization.DEFAULT_CLAUDE_TIMEOUT_SECONDS
DEFAULT_WORKFLOW_TIMEOUT_SECONDS = _authorization.DEFAULT_WORKFLOW_TIMEOUT_SECONDS
MAX_PROVIDER_TIMEOUT_SECONDS = _authorization.MAX_PROVIDER_TIMEOUT_SECONDS
MAX_WORKFLOW_TIMEOUT_SECONDS = _authorization.MAX_WORKFLOW_TIMEOUT_SECONDS
CODEX_EXECUTABLE = _cli.CODEX_EXECUTABLE
CLAUDE_MAX_PAYLOAD_ENV = claude_reviewer.MAX_REVIEW_PAYLOAD_ENV
WORKSPACE_OPERATIONS = _authorization.WORKSPACE_OPERATIONS
CODEX_STAGES = _authorization.CODEX_STAGES
CLAUDE_DATA_SCOPE = _authorization.CLAUDE_DATA_SCOPE
RESTRICTED_OPERATIONS = _authorization.RESTRICTED_OPERATIONS
SECRET_PATTERNS = claude_reviewer.SECRET_PATTERNS
SENSITIVE_NAMES = _workspace.SENSITIVE_NAMES


def _workspace_error(call):
    try:
        return call()
    except _workspace.WorkspaceError as exc:
        raise WorkflowError(str(exc)) from exc


def _persistence_call(call):
    try:
        return call()
    except _persistence.PersistenceError as exc:
        raise WorkflowError(str(exc)) from exc


def _git_env() -> dict[str, str]:
    return _workspace.git_env()


def git(target: Path, *args: str, check: bool = True) -> bytes:
    return _workspace_error(lambda: _workspace.git(target, *args, check=check, redactor=redact))


def canonical_target(value: str) -> Path:
    return _workspace_error(lambda: _workspace.canonical_target(value, redactor=redact))


def _changed_paths(target: Path, baseline: str) -> tuple[list[str], list[str]]:
    return _workspace_error(lambda: _workspace.changed_paths(target, baseline, redactor=redact))


def _protected_paths(target: Path, baseline: str) -> list[str]:
    return _workspace_error(lambda: _workspace.protected_paths(target, baseline, redactor=redact))


def _project_guidance(target: Path, task: str) -> dict:
    return _workspace_error(lambda: _workspace.project_guidance(target, task, redactor=redact))


def _safe_artifact(path: Path) -> None:
    _persistence_call(lambda: _persistence.safe_artifact(path))


def _atomic_text(path: Path, value: str) -> None:
    _persistence_call(lambda: _persistence.atomic_text(path, value))


def _write_json(path: Path, value: dict) -> None:
    _persistence_call(lambda: _persistence.write_json(path, value))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def redact(value: str) -> str:
    return claude_reviewer.redact(value)


def clean_markdown(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.strip().splitlines()).strip() + "\n"


def valid_workflow_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise WorkflowError("Workflow ID must contain only letters, numbers, dots, underscores, and hyphens.")
    return value


def _check_plain_directories(root: Path, relative: str, *, create: bool = False) -> Path:
    current = root
    parts = Path(relative).parts
    for part in parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise WorkflowError(f"Workflow path contains a symlink or non-directory: {current}")
        elif create:
            try:
                current.mkdir()
            except FileExistsError:
                if current.is_symlink() or not current.is_dir():
                    raise WorkflowError(f"Workflow path contains a symlink or non-directory: {current}")
    return current


@contextlib.contextmanager
def _workflow_process_lock(target: Path, workflow_id: str):
    """Exclusively coordinate mutating commands for one workflow across processes.

    The lock file intentionally remains after release. Ownership is determined by
    the kernel lock, never by the file's existence, so a process crash cannot leave
    a stale lock that blocks future recovery.
    """

    try:
        with _process_locks.named_process_lock(
            target, f".{workflow_id}.lock",
            busy_message=f"Workflow {workflow_id} is already being modified by another process.",
        ):
            yield
    except _process_locks.ProcessLockError as exc:
        raise WorkflowError(redact(str(exc))) from exc


@contextlib.contextmanager
def _target_process_lock(target: Path):
    try:
        with _process_locks.named_process_lock(
            target, ".target.lock", busy_message="Another workflow is modifying this target; retry later.",
        ):
            yield
    except _process_locks.ProcessLockError as exc:
        raise WorkflowError(redact(str(exc))) from exc




def new_workflow_authorization(
    target: Path, workflow_id: str, budget_usd: float,
    max_code_review_calls: int = DEFAULT_MAX_CODE_REVIEW_CALLS,
    tracing_enabled: bool = False,
    *, max_codex_calls: int = DEFAULT_MAX_CODEX_CALLS,
    codex_timeout_seconds: int = DEFAULT_CODEX_TIMEOUT_SECONDS,
    claude_timeout_seconds: int = DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    workflow_timeout_seconds: int = DEFAULT_WORKFLOW_TIMEOUT_SECONDS,
    github_publication: dict | None = None,
) -> dict:
    try:
        return _authorization.new_workflow_authorization(
            target, workflow_id, budget_usd, max_code_review_calls, tracing_enabled,
            max_codex_calls=max_codex_calls,
            codex_timeout_seconds=codex_timeout_seconds,
            claude_timeout_seconds=claude_timeout_seconds,
            workflow_timeout_seconds=workflow_timeout_seconds,
            github_publication=github_publication,
        )
    except _authorization.AuthorizationError as exc:
        raise WorkflowError(str(exc)) from exc


def authorization_scope_digest(authorization: dict) -> str:
    return _authorization.authorization_scope_digest(authorization)


def validate_workflow_authorization(state: dict, target: Path, workflow_id: str) -> dict:
    try:
        return _authorization.validate_workflow_authorization(state, target, workflow_id)
    except _authorization.AuthorizationError as exc:
        raise WorkflowError(str(exc)) from exc


def authorize_budget_change(
    authorization: dict, amount: str, operation: str, *, workflow_id: str, stage: str,
) -> dict:
    try:
        return _authorization.authorize_budget_change(
            authorization, amount, operation, workflow_id=workflow_id, stage=stage, timestamp=now(),
        )
    except _authorization.AuthorizationError as exc:
        raise WorkflowError(str(exc)) from exc


class Orchestrator:
    """A single-run stage controller with small, target-local JSON state."""

    def __init__(
        self,
        target: str | Path,
        *,
        task: str | None = None,
        budget_usd: float | None = DEFAULT_CLAUDE_BUDGET_USD,
        max_code_review_calls: int = DEFAULT_MAX_CODE_REVIEW_CALLS,
        max_codex_calls: int = DEFAULT_MAX_CODEX_CALLS,
        model: str | None = None,
        workflow_id: str | None = None,
        codex: str = CODEX_EXECUTABLE,
        aily: str = claude_reviewer.AILY_EXECUTABLE,
        timeout_seconds: int = DEFAULT_CODEX_TIMEOUT_SECONDS,
        claude_timeout_seconds: int = DEFAULT_CLAUDE_TIMEOUT_SECONDS,
        workflow_timeout_seconds: int = DEFAULT_WORKFLOW_TIMEOUT_SECONDS,
        process_runner: Callable = subprocess.run,
        claude_runner: Callable = claude_reviewer.review,
        tracing_enabled: bool = False,
        github_publication: dict | None = None,
    ):
        if (isinstance(max_code_review_calls, bool) or not isinstance(max_code_review_calls, int)
                or not 1 <= max_code_review_calls <= MAX_AUTHORIZED_CODE_REVIEW_CALLS):
            raise WorkflowError(
                f"Code review call authorization must be between 1 and {MAX_AUTHORIZED_CODE_REVIEW_CALLS}."
            )
        if (isinstance(max_codex_calls, bool) or not isinstance(max_codex_calls, int)
                or not 1 <= max_codex_calls <= MAX_AUTHORIZED_CODEX_CALLS):
            raise WorkflowError(
                f"Codex call authorization must be between 1 and {MAX_AUTHORIZED_CODEX_CALLS}."
            )
        for provider, timeout in (
            ("Codex", timeout_seconds), ("Claude", claude_timeout_seconds),
        ):
            if (isinstance(timeout, bool) or not isinstance(timeout, int)
                    or not 1 <= timeout <= MAX_PROVIDER_TIMEOUT_SECONDS):
                raise WorkflowError(
                    f"{provider} timeout must be between 1 and {MAX_PROVIDER_TIMEOUT_SECONDS} seconds."
                )
        if (isinstance(workflow_timeout_seconds, bool) or not isinstance(workflow_timeout_seconds, int)
                or not 1 <= workflow_timeout_seconds <= MAX_WORKFLOW_TIMEOUT_SECONDS):
            raise WorkflowError(
                f"Workflow timeout must be between 1 and {MAX_WORKFLOW_TIMEOUT_SECONDS} seconds."
            )
        self.target = canonical_target(str(target))
        self.task = task
        self.budget_usd = budget_usd
        self.max_code_review_calls = max_code_review_calls
        self.max_codex_calls = max_codex_calls
        if model is not None and not model.strip():
            raise WorkflowError("--model must name a Codex model when provided.")
        self.model = model.strip() if model is not None else None
        self.workflow_id = valid_workflow_id(workflow_id or self._new_workflow_id())
        self.codex = codex
        self.aily = aily
        self.timeout_seconds = timeout_seconds
        self.claude_timeout_seconds = claude_timeout_seconds
        self.workflow_timeout_seconds = workflow_timeout_seconds
        self.process_runner = process_runner
        self.claude_runner = claude_runner
        self.tracing_enabled = bool(tracing_enabled)
        self.github_publication = github_publication
        self._tracer = None
        self._active_trace_context = None
        self._trace_started_at: float | None = None
        self._active_codex_trace_context = None
        self._active_codex_operation_id: str | None = None
        self._active_codex_persisted_status: str | None = None
        self._codex_cli_version_checked = False
        self._codex_cli_version_cache: str | None = None
        self.workflow_dir = _check_plain_directories(self.target, f"{WORKFLOW_ROOT}/{self.workflow_id}")
        self.state_path = self.workflow_dir / "state.json"
        self.proposal_path = self.workflow_dir / "proposal.md"
        self.delivery_path = self.workflow_dir / "delivery.md"
        self.workflow_state = WorkflowState(
            self.state_path,
            workflow_id=self.workflow_id,
            target=self.target,
            default_limits={
                "max_code_review_calls": self.max_code_review_calls,
                "max_codex_calls": self.max_codex_calls,
                "codex_timeout_seconds": self.timeout_seconds,
                "claude_timeout_seconds": self.claude_timeout_seconds,
                "workflow_timeout_seconds": self.workflow_timeout_seconds,
            },
        )
        self.execution = WorkflowExecution(self.workflow_state, ExecutionConfig(
            target=self.target,
            workflow_id=self.workflow_id,
            codex=self.codex,
            aily=self.aily,
            model=self.model,
            budget_usd=self.budget_usd,
            max_code_review_calls=self.max_code_review_calls,
            max_codex_calls=self.max_codex_calls,
            timeout_seconds=self.timeout_seconds,
            claude_timeout_seconds=self.claude_timeout_seconds,
            workflow_timeout_seconds=self.workflow_timeout_seconds,
            process_runner=self.process_runner,
            claude_runner=self.claude_runner,
            recovery_required=self._require_recovery,
        ))

    @property
    def state(self) -> dict:
        workflow_state = getattr(self, "workflow_state", None)
        if isinstance(workflow_state, WorkflowState):
            return workflow_state.document
        return getattr(self, "_uninitialized_state", {})

    @state.setter
    def state(self, value: dict) -> None:
        workflow_state = getattr(self, "workflow_state", None)
        if isinstance(workflow_state, WorkflowState):
            workflow_state.replace(value)
        else:
            self._uninitialized_state = value

    @property
    def process_runner(self):
        return getattr(self, "_process_runner", subprocess.run)

    @process_runner.setter
    def process_runner(self, value) -> None:
        self._process_runner = value
        execution = getattr(self, "execution", None)
        if isinstance(execution, WorkflowExecution):
            execution.process_runner = value
            execution.codex_executor.process_runner = value

    @property
    def claude_runner(self):
        return getattr(self, "_claude_runner", claude_reviewer.review)

    @claude_runner.setter
    def claude_runner(self, value) -> None:
        self._claude_runner = value
        execution = getattr(self, "execution", None)
        if isinstance(execution, WorkflowExecution):
            execution.claude_runner = value

    @property
    def _tracer(self):
        execution = getattr(self, "execution", None)
        if isinstance(execution, WorkflowExecution):
            return execution._tracer
        return getattr(self, "_local_tracer", None)

    @_tracer.setter
    def _tracer(self, value) -> None:
        self._local_tracer = value
        execution = getattr(self, "execution", None)
        if isinstance(execution, WorkflowExecution):
            execution.set_tracer(value)

    @property
    def _process_lock_held(self) -> bool:
        return self.workflow_state.lock_held

    @_process_lock_held.setter
    def _process_lock_held(self, value: bool) -> None:
        self.workflow_state.lock_held = value

    @staticmethod
    def _new_workflow_id() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


    def _operation(self, operation_id: str | None) -> dict | None:
        workflow_state = getattr(self, "workflow_state", None)
        if isinstance(workflow_state, WorkflowState):
            return workflow_state.operation(operation_id)
        if not operation_id:
            return None
        return next((
            item for item in reversed(self.state.get("operation_history", []))
            if item.get("operation_id") == operation_id
        ), None)


    def _begin_workflow_runtime(self) -> None:
        self.workflow_state.begin_workflow_runtime()

    def _recovery_target_change_inventory(self) -> list[dict[str, str]]:
        return _workspace_error(lambda: _workspace.target_change_inventory(
            self.target,
            self.state.get("baseline_commit"),
            self.state.get("preexisting_paths", []),
            redactor=redact,
        ))

    def _recovery_target_fingerprint(self) -> dict:
        try:
            fingerprint = _workspace.repository_fingerprint(self.target, redactor=redact)
        except Exception as exc:
            return {
                "status": "unavailable",
                "error_category": type(exc).__name__,
                "error": "Fingerprint calculation failed; repository state is unavailable.",
            }
        return {"status": "captured", "captured_at": now(), **fingerprint}

    def _recovery_facts(
        self,
        operation: dict | None,
        *,
        action: str | None,
        verified_claude_cost_usd: float | None = None,
        confirm_provider_inactive: bool = False,
        confirm_process_inactive: bool = False,
        confirm_target_reviewed: bool = False,
        target_changes: list[dict[str, str]] | None = None,
        target_fingerprint: dict | None = None,
    ) -> RecoveryFacts:
        authorization = self.state.get("authorization", {}).get("claude_aily", {})
        operation = operation if isinstance(operation, dict) else {}
        target_changes = target_changes or []
        return RecoveryFacts(
            operation_id=operation.get("operation_id") if isinstance(operation.get("operation_id"), str) else None,
            operation_type=operation.get("operation_type") if isinstance(operation.get("operation_type"), str) else None,
            operation_stage=operation.get("stage") if isinstance(operation.get("stage"), str) else None,
            operation_status=operation.get("status") if isinstance(operation.get("status"), str) else None,
            operation_sandbox=operation.get("sandbox") if isinstance(operation.get("sandbox"), str) else None,
            process_presence=_process_control.process_presence(operation.get("process_id")),
            target_change_paths=tuple(
                item["path"] for item in target_changes
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            ),
            target_changes_overlap_preexisting=any(
                isinstance(item, dict) and item.get("preexisting") == "yes" for item in target_changes
            ),
            target_fingerprint_status=(
                target_fingerprint.get("status") if isinstance(target_fingerprint, dict) else None
            ),
            claude_budget_unknown=authorization.get("budget_unknown") is True,
            claude_budget_usd=authorization.get("budget_usd"),
            claude_spent_usd=authorization.get("spent_usd"),
            claude_call_cap_usd=operation.get("cap_usd"),
            action=action,
            verified_claude_cost_usd=verified_claude_cost_usd,
            confirm_provider_inactive=confirm_provider_inactive,
            confirm_process_inactive=confirm_process_inactive,
            confirm_target_reviewed=confirm_target_reviewed,
        )

    def _require_recovery(self, operation: dict | None, reason: str) -> None:
        if operation is not None and operation.get("status") == "started":
            operation["status"] = "interrupted"
            operation["finished_at"] = now()
            operation["outcome"] = "The workflow process stopped before recording a terminal result."
            operation["error_category"] = "Interrupted"
            if operation.get("operation_type") == "codex":
                operation.update({
                    "stdout_capture_status": "unavailable",
                    "stdout_truncated": None,
                    "stderr_capture_status": "unavailable",
                    "stderr_truncated": None,
                    "jsonl_event_coverage": "unavailable",
                    "tool_event_coverage": "unavailable",
                })
            if self.state.get("active_operation_id") == operation.get("operation_id"):
                self.state["active_operation_id"] = None
        if operation is not None:
            self.workflow_state.record_operation_runtime(operation)
        if operation is not None and operation.get("operation_type") == "claude":
            authorization = self.state.get("authorization", {}).get("claude_aily", {})
            authorization["budget_unknown"] = True
            authorization["active_call"] = None
            for context in reversed(self.state.get("claude_contexts", [])):
                if context.get("operation_id") == operation.get("operation_id"):
                    context["call_state"] = "interrupted_unknown_cost"
                    context["error"] = reason
                    break
        target_changes = self._recovery_target_change_inventory()
        facts = self._recovery_facts(operation, action=None, target_changes=target_changes)
        decision = decide_recovery(facts)
        self.state["recovery"] = {
            "operation_id": operation.get("operation_id") if operation else None,
            "operation_type": operation.get("operation_type") if operation else None,
            "stage": operation.get("stage") if operation else self.state.get("stage"),
            "operation_status": operation.get("status") if operation else "unknown",
            "process_presence": facts.process_presence,
            "detected_at": now(),
            "reason": redact(reason),
            "target_changes": target_changes,
        }
        self.state["status"] = "recovery_required" if decision.kind == "requires_recovery" else "failed"
        self.state["error"] = redact(reason)
        self.execution._capture_final_target_fingerprint()
        self.workflow_state.save()

    def _accept_current_target_for_review(self, *, confirm_target_reviewed: bool) -> None:
        """Bind an explicitly inspected failed-stage target to its independent review retry."""
        if not confirm_target_reviewed:
            raise WorkflowError(
                "Inspect the current target and pass --confirm-target-reviewed before accepting it for review."
            )
        if self.state.get("status") != "failed" or self.state.get("stage") != "code_review":
            raise WorkflowError("Only a failed code_review stage can accept the current target for review.")
        if self.state.get("active_operation_id") is not None:
            raise WorkflowError("A target cannot be accepted for review while an operation is active.")
        if self.state.get("recovery") is not None:
            raise WorkflowError("Resolve the pending interrupted operation before accepting a target for review.")
        operations = self.state.get("operation_history", [])
        implementation = next((
            item for item in reversed(operations)
            if isinstance(item, dict) and item.get("operation_type") == "codex"
            and item.get("stage") == "implementation"
        ), None) if isinstance(operations, list) else None
        if not isinstance(implementation, dict) or implementation.get("status") != "completed":
            raise WorkflowError("Current-target review acceptance requires a completed implementation operation.")

        baseline_commit = self.state.get("baseline_commit")
        execution_fingerprints = self.state.get("execution_fingerprint")
        initial_target = (
            execution_fingerprints.get("initial", {}).get("target", {})
            if isinstance(execution_fingerprints, dict) else {}
        )
        baseline_snapshot = self.state.get("baseline_snapshot")
        if not isinstance(baseline_commit, str) or not baseline_commit:
            raise WorkflowError("Current-target review acceptance has no saved baseline commit.")
        if (not isinstance(baseline_snapshot, dict)
                or not isinstance(baseline_snapshot.get("sha256"), str)):
            raise WorkflowError("Current-target review acceptance requires an intact baseline snapshot.")
        if (not isinstance(initial_target, dict) or initial_target.get("status") != "captured"
                or not isinstance(initial_target.get("working_tree_sha256"), str)):
            raise WorkflowError("Current-target review acceptance has no immutable initial target fingerprint.")
        if (baseline_snapshot.get("baseline_fingerprint_sha256")
                != initial_target.get("working_tree_sha256")):
            raise WorkflowError("Saved baseline snapshot does not match the immutable initial target fingerprint.")

        history = self.state.get("review_target_acceptances")
        if history is None:
            history = []
        elif not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
            raise WorkflowError("Saved target acceptance history is invalid; no acceptance was recorded.")
        else:
            history = [dict(item) for item in history]

        # Import the former single-record field into the append-only history before
        # recording a continuation. Its missing diff hash and path list stay explicit.
        legacy_acceptance = self.state.get("review_target_acceptance")
        if not history and isinstance(legacy_acceptance, dict):
            history.append({
                "accepted_at": legacy_acceptance.get("accepted_at"),
                "operation_id": legacy_acceptance.get("operation_id"),
                "previous_target_fingerprint": implementation.get("target_fingerprint_after"),
                "target_fingerprint": legacy_acceptance.get("target_fingerprint"),
                "changed_paths": None,
                "changed_path_count": legacy_acceptance.get("changed_path_count"),
                "baseline_commit": baseline_commit,
                "baseline_fingerprint_sha256": initial_target.get("working_tree_sha256"),
                "baseline_snapshot_sha256": baseline_snapshot.get("sha256")
                if isinstance(baseline_snapshot, dict) else None,
                "baseline_diff_sha256": None,
                "confirmation": legacy_acceptance.get("confirmation"),
                "legacy_record_without_diff_binding": True,
            })

        previous_fingerprint = (
            history[-1].get("target_fingerprint") if history
            else implementation.get("target_fingerprint_after")
        )
        if (not isinstance(previous_fingerprint, dict)
                or previous_fingerprint.get("status") != "captured"
                or previous_fingerprint.get("commit_sha") != baseline_commit
                or not isinstance(previous_fingerprint.get("working_tree_sha256"), str)):
            raise WorkflowError("Previous target fingerprint is unavailable or inconsistent with the baseline.")

        fingerprint = self._recovery_target_fingerprint()
        if (fingerprint.get("status") != "captured"
                or fingerprint.get("commit_sha") != baseline_commit):
            raise WorkflowError("Could not verify the current target against the saved workflow baseline.")
        if previous_fingerprint.get("working_tree_sha256") == fingerprint.get("working_tree_sha256"):
            raise WorkflowError("The current target does not differ from the previously accepted target.")

        changes = self._recovery_target_change_inventory()
        if not changes:
            raise WorkflowError("No reviewable target changes were found for explicit acceptance.")
        if any(item.get("preexisting") == "yes" for item in changes if isinstance(item, dict)):
            raise WorkflowError(
                "Current-target review acceptance is blocked because changes overlap pre-existing target paths."
            )

        review_diff = _review_diff(self.state, self.target)
        if not review_diff.included_paths and not review_diff.context_files:
            raise WorkflowError("Current target has no reviewable baseline-to-current diff to accept.")
        baseline_diff_sha256 = _ReviewCampaign._campaign_source_hash(review_diff)
        confirmed_fingerprint = self._recovery_target_fingerprint()
        if (confirmed_fingerprint.get("status") != "captured"
                or confirmed_fingerprint.get("commit_sha") != fingerprint.get("commit_sha")
                or confirmed_fingerprint.get("working_tree_sha256") != fingerprint.get("working_tree_sha256")):
            raise WorkflowError("Target changed while its review acceptance was being recorded; inspect and accept again.")

        acceptance = {
            "accepted_at": now(),
            "operation_id": implementation.get("operation_id"),
            "previous_target_fingerprint": previous_fingerprint,
            "target_fingerprint": confirmed_fingerprint,
            "changed_paths": changes,
            "changed_path_count": len(changes),
            "baseline_commit": baseline_commit,
            "baseline_fingerprint_sha256": initial_target.get("working_tree_sha256"),
            "baseline_snapshot_sha256": baseline_snapshot.get("sha256")
            if isinstance(baseline_snapshot, dict) else None,
            "baseline_diff_sha256": baseline_diff_sha256,
            "confirmation": "explicit_target_reviewed_for_independent_review",
        }
        history.append(acceptance)
        self.state["review_target_acceptances"] = history
        # Keep the original field as a latest-record compatibility alias. The
        # append-only history is authoritative and retains every acceptance.
        self.state["review_target_acceptance"] = acceptance
        self.workflow_state.save()

    def _resolve_recovery(
        self,
        action: str | None,
        *,
        verified_claude_cost_usd: float | None,
        confirm_provider_inactive: bool,
        confirm_process_inactive: bool,
        confirm_target_reviewed: bool,
    ) -> None:
        recovery = self.state.get("recovery") or {}
        operation = self.workflow_state.operation(recovery.get("operation_id"))
        if action is None:
            decision = decide_recovery(self._recovery_facts(
                operation,
                action=None,
                verified_claude_cost_usd=verified_claude_cost_usd,
                confirm_provider_inactive=confirm_provider_inactive,
                confirm_process_inactive=confirm_process_inactive,
                confirm_target_reviewed=confirm_target_reviewed,
            ))
            if decision.error:
                raise WorkflowError(decision.error)
            operation_id = recovery.get("operation_id") or "unavailable"
            detail = (
                "The interrupted Claude/Aily call has no recorded cost; verify its final charge and process state. "
                if operation is not None and operation.get("operation_type") == "claude"
                and self.state.get("authorization", {}).get("claude_aily", {}).get("budget_unknown") is True
                else ""
            )
            raise WorkflowError(
                f"Workflow requires recovery at stage {recovery.get('stage')!r} (operation {operation_id}). "
                f"{redact(str(recovery.get('reason') or (operation or {}).get('outcome') or ''))} "
                f"{detail}"
                "Inspect status and target changes, then resume with an explicit --recovery-action."
            )

        target_changes = self._recovery_target_change_inventory()
        needs_target_fingerprint = (
            action == "accept-implementation"
            or (action == "retry" and isinstance(operation, dict)
                and operation.get("operation_type") == "codex"
                and operation.get("sandbox") == "workspace-write")
        )
        target_fingerprint = self._recovery_target_fingerprint() if needs_target_fingerprint else None
        facts = self._recovery_facts(
            operation,
            action=action,
            verified_claude_cost_usd=verified_claude_cost_usd,
            confirm_provider_inactive=confirm_provider_inactive,
            confirm_process_inactive=confirm_process_inactive,
            confirm_target_reviewed=confirm_target_reviewed,
            target_changes=target_changes,
            target_fingerprint=target_fingerprint,
        )
        decision = decide_recovery(facts)
        if decision.error:
            raise WorkflowError(decision.error)
        if operation is None:
            raise WorkflowError("Recovery has no verifiable operation record; leave the workflow blocked.")

        if decision.kind == "accept-implementation":
            self.state["implementation_summary"] = (
                "Human-approved crash recovery: Codex was interrupted before its implementation summary was saved. "
                "The user inspected and accepted the current target changes for independent review. Changed paths: "
                + ", ".join(facts.target_change_paths)
            )
            operation["resolution"] = decision.resolution
            operation["human_accepted_target_fingerprint"] = target_fingerprint
            operation["resolved_at"] = now()
            self.state["stage"] = "code_review"
        elif operation.get("operation_type") == "claude":
            authorization = self.state["authorization"]["claude_aily"]
            cost = decision.resolution_cost_usd
            authorization["spent_usd"] = round(float(Decimal(str(authorization["spent_usd"])) + Decimal(str(cost))), 4)
            authorization["budget_unknown"] = False
            operation["resolution_cost_usd"] = cost
            operation["resolution"] = decision.resolution
            operation["resolved_at"] = now()
        else:
            if target_fingerprint is not None:
                operation["retry_reviewed_target_fingerprint"] = target_fingerprint
            operation["resolution"] = decision.resolution
            operation["resolved_at"] = now()

        self.state["recovery"] = None
        self.state["status"] = "running"
        self.state["error"] = None
        self.workflow_state.save()

    def _finish_workflow_runtime(self) -> None:
        self.workflow_state.finish_workflow_runtime()


    def _record_operation_runtime(self, operation: dict, duration_ms: int | None = None) -> None:
        self.workflow_state.record_operation_runtime(operation, duration_ms)


    def _capture_initial_fingerprints(self, baseline_commit: str) -> dict:
        harness_root = Path(__file__).resolve().parents[1]
        return {
            "captured_at": now(),
            "harness": self.execution._fingerprint_repository(harness_root),
            "target": self.execution._fingerprint_repository(self.target, expected_commit=baseline_commit),
        }


    def _require_current_approval_baseline(self) -> None:
        def anonymous_metadata_is_valid(value: object, path_count: int) -> bool:
            if not isinstance(value, list) or len(value) != path_count:
                return False
            for record in value:
                if (not isinstance(record, list) or len(record) != 5
                        or any(type(part) is not int or part < 0 for part in record)
                        or record[0] not in (0, 1)):
                    return False
            return True

        def sensitive_status_is_valid(value: object) -> bool:
            if not isinstance(value, dict) or value.get("status") != "captured":
                return False
            flag_names = ("staged_changes", "unstaged_changes", "untracked_files")
            if not all(type(value.get(name)) is bool for name in (*flag_names, "dirty")):
                return False
            path_count = value.get("path_count")
            if (type(path_count) is not int or path_count < 0
                    or value["dirty"] != any(value[name] for name in flag_names)):
                return False
            # Older persisted fingerprints predate anonymous metadata. Keep them readable, then
            # use their capture time as a conservative fallback during approval validation.
            return (
                "anonymous_path_metadata" not in value
                or anonymous_metadata_is_valid(value["anonymous_path_metadata"], path_count)
            )

        execution = self.state.get("execution_fingerprint")
        if not isinstance(execution, dict) or "initial" not in execution:
            raise WorkflowError(
                "The workflow has no initial target fingerprint; reconcile the target and start a new workflow."
            )
        initial = execution.get("initial")
        expected = initial.get("target") if isinstance(initial, dict) else None
        if (not isinstance(expected, dict) or expected.get("status") != "captured"
                or not isinstance(expected.get("commit_sha"), str)):
            raise WorkflowError(
                "The workflow has no usable initial target fingerprint; reconcile the target and start a new workflow."
            )

        expected_sensitive = expected.get("sensitive_path_status")
        if not sensitive_status_is_valid(expected_sensitive):
            raise WorkflowError(
                "The workflow has no usable initial sensitive-path status; reconcile the target and start a new workflow."
            )

        current = self.execution._fingerprint_repository(self.target, expected_commit=expected["commit_sha"])
        if current.get("status") != "captured":
            raise WorkflowError(
                "The target fingerprint is unavailable; approval was not granted. "
                "Reconcile the target and start a new workflow."
            )
        current_sensitive = current.get("sensitive_path_status")
        if not sensitive_status_is_valid(current_sensitive):
            raise WorkflowError(
                "Sensitive-path status is unavailable; approval was not granted. "
                "Reconcile the target and start a new workflow."
            )
        sensitive_flags = ("staged_changes", "unstaged_changes", "untracked_files")
        sensitive_state_changed = (
            any(expected_sensitive[key] != current_sensitive[key] for key in sensitive_flags)
            or expected_sensitive["path_count"] != current_sensitive["path_count"]
        )
        expected_metadata = expected_sensitive.get("anonymous_path_metadata")
        current_metadata = current_sensitive.get("anonymous_path_metadata")
        if "anonymous_path_metadata" in current_sensitive and not anonymous_metadata_is_valid(
                current_metadata, current_sensitive["path_count"]):
            raise WorkflowError(
                "Sensitive-path status is unavailable; approval was not granted. "
                "Reconcile the target and start a new workflow."
            )

        if expected_metadata is not None:
            sensitive_state_changed = sensitive_state_changed or expected_metadata != current_metadata
        elif expected_sensitive["path_count"]:
            # Legacy state has no initial stat records. Allow unchanged pre-existing files only
            # when their current change time is no later than the saved initial capture time.
            captured_at = expected.get("captured_at")
            try:
                captured_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
                if captured_time.tzinfo is None:
                    raise ValueError("capture timestamp has no timezone")
                elapsed = captured_time.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
                captured_ns = ((elapsed.days * 86400 + elapsed.seconds) * 1_000_000_000
                               + elapsed.microseconds * 1_000)
            except (AttributeError, TypeError, ValueError, OverflowError):
                captured_ns = -1
            if (captured_ns < 0 or not isinstance(current_metadata, list)
                    or any(record[0] != 1 or record[4] > captured_ns for record in current_metadata)):
                sensitive_state_changed = True

        if sensitive_state_changed:
            raise WorkflowError(
                "Sensitive files changed after this workflow started; approval was not granted. "
                "Clean or reconcile them, then start a new workflow."
            )
        stable_expected = {key: value for key, value in expected.items() if key != "captured_at"}
        stable_current = {key: value for key, value in current.items() if key != "captured_at"}
        if "anonymous_path_metadata" not in expected_sensitive:
            # The metadata is an additive fingerprint field. Ignore it for legacy snapshots after
            # the conservative approval check above has compared the available evidence.
            stable_current_sensitive = stable_current.get("sensitive_path_status")
            if isinstance(stable_current_sensitive, dict):
                stable_current_sensitive.pop("anonymous_path_metadata", None)
        if stable_current != stable_expected:
            raise WorkflowError(
                "The target changed after this workflow started; approval was not granted. "
                "Reconcile the target and start a new workflow."
            )


    @contextlib.contextmanager
    def _workflow_lock(self):
        if self._process_lock_held:
            raise WorkflowError("Workflow process locks cannot be nested.")
        with _workflow_process_lock(self.target, self.workflow_id):
            self._process_lock_held = True
            try:
                yield
            finally:
                self._process_lock_held = False

    def _read_state(self) -> dict:
        """Load persisted state and apply the saved authorization to runtime limits."""
        self.workflow_state.load()
        saved_model = self.state.get("codex_model")
        if saved_model is not None and (not isinstance(saved_model, str) or not saved_model.strip()):
            raise WorkflowError("Saved Codex model configuration is invalid.")
        if self.model is not None and self.model != saved_model:
            raise WorkflowError("Codex model is fixed when the workflow starts.")
        self.model = saved_model
        self.execution.model = saved_model

        authorization = validate_workflow_authorization(self.state, self.target, self.workflow_id)
        self.max_code_review_calls = authorization["claude_aily"]["max_code_reviews"]
        limits = authorization.get("execution_limits")
        if isinstance(limits, dict):
            self.max_codex_calls = limits["max_codex_calls"]
            self.timeout_seconds = limits["codex_timeout_seconds"]
            self.claude_timeout_seconds = limits["claude_timeout_seconds"]
            self.workflow_timeout_seconds = limits["workflow_timeout_seconds"]
        self.execution.max_code_review_calls = self.max_code_review_calls
        self.execution.max_codex_calls = self.max_codex_calls
        self.execution.timeout_seconds = self.timeout_seconds
        self.execution.claude_timeout_seconds = self.claude_timeout_seconds
        self.execution.workflow_timeout_seconds = self.workflow_timeout_seconds
        self.workflow_state.migrate_legacy_resource_usage(authorization)
        return authorization

    def _load(self, *, preflight_tracing: bool = False) -> None:
        """Load state for a locked mutation, recovering only a truly ownerless call."""
        if not self._process_lock_held:
            raise WorkflowError("Workflow state recovery requires the workflow process lock.")
        authorization = self._read_state()
        observability = authorization.get("observability")
        if (preflight_tracing and isinstance(observability, dict)
                and observability.get("enabled") is True):
            from observability.langfuse import LangfusePreflightError, preflight_langfuse_tracing

            try:
                preflight_langfuse_tracing()
            except LangfusePreflightError as exc:
                raise WorkflowError(str(exc)) from exc

        active_runtime_start = self.state.pop("workflow_active_started_at", None)
        if active_runtime_start is not None:
            usage = self.workflow_state.resource_usage()
            elapsed = 0.0
            if self.state.get("status") == "running":
                try:
                    started_at = datetime.fromisoformat(str(active_runtime_start).replace("Z", "+00:00"))
                    elapsed = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
                except (TypeError, ValueError):
                    # An invalid persisted start time cannot safely grant a fresh runtime allowance.
                    elapsed = float(self.workflow_state.execution_limits()["workflow_timeout_seconds"])
            usage["workflow_runtime_seconds"] = round(
                float(usage.get("workflow_runtime_seconds", 0.0)) + elapsed, 3,
            )
            self.workflow_state.save()

        active_operation = self._operation(self.state.get("active_operation_id"))
        if active_operation is None:
            active_operation = next((
                item for item in reversed(self.state.get("operation_history", []))
                if item.get("status") == "started"
            ), None)
        if active_operation is not None and active_operation.get("status") == "started":
            if active_operation.get("launch_state") == "not_started":
                active_operation.update({
                    "status": "failed",
                    "finished_at": now(),
                    "outcome": "The workflow stopped before launching this external operation; safe to retry.",
                    "error_category": "OperationNotLaunched",
                })
                if active_operation.get("operation_type") == "codex":
                    active_operation.update({
                        "stdout_capture_status": "not_started",
                        "stdout_truncated": False,
                        "stderr_capture_status": "not_started",
                        "stderr_truncated": False,
                        "jsonl_event_coverage": "unavailable",
                        "tool_event_coverage": "unavailable",
                    })
                if self.state.get("active_operation_id") == active_operation.get("operation_id"):
                    self.state["active_operation_id"] = None
                if active_operation.get("operation_type") == "claude":
                    external = authorization["claude_aily"]
                    active_call = external.get("active_call")
                    if isinstance(active_call, dict) and active_call.get("operation_id") == active_operation.get("operation_id"):
                        external["active_call"] = None
                    for context in reversed(self.state.get("claude_contexts", [])):
                        if context.get("operation_id") == active_operation.get("operation_id"):
                            context["call_state"] = "not_started"
                            context["error"] = active_operation["outcome"]
                            break
                self._record_operation_runtime(active_operation, 0)
                self.workflow_state.save()
            else:
                self._require_recovery(
                    active_operation,
                    "The prior workflow process released its lock while an external operation was launching or running. "
                    "Its outcome and child-process state cannot be established safely; no operation was repeated.",
                )
                return

        if self.state.get("status") == "running" and self.state.get("operation_tracking_version") != 1:
            self._require_recovery(
                None,
                "This legacy workflow was left running without operation records; the last external action cannot be established.",
            )
            return

        if self.state.get("status") == "failed":
            latest_operation = next(iter(reversed(self.state.get("operation_history", []))), None)
            if isinstance(latest_operation, dict):
                is_unresolved = latest_operation.get("status") in {"unknown", "interrupted"}
                codex_may_have_written = (
                    latest_operation.get("operation_type") == "codex"
                    and latest_operation.get("sandbox") == "workspace-write"
                    and latest_operation.get("status") == "failed"
                    and latest_operation.get("launch_state") != "not_started"
                )
                claude_spend_unknown = (
                    latest_operation.get("operation_type") == "claude"
                    and self.state.get("authorization", {}).get("claude_aily", {}).get("budget_unknown") is True
                )
                if is_unresolved or codex_may_have_written or claude_spend_unknown:
                    self._require_recovery(
                        latest_operation,
                        latest_operation.get("outcome")
                        or "The previous operation may have changed the target or incurred an unknown provider cost.",
                    )
                    return

        external = authorization["claude_aily"]
        if external.get("active_call"):
            external["budget_unknown"] = True
            external["active_call"] = None
            self.state["error"] = "A prior Claude call ended without a recorded result; further paid reviews are stopped."
            self.state["status"] = "failed"
            self.execution._capture_final_target_fingerprint()
            self.workflow_state.save()


    def start(self) -> dict:
        return self._execute_locked(self._start_locked)

    def _start_locked(self) -> dict:
        if not self.task or not self.task.strip():
            raise WorkflowError("A non-empty task is required to start a workflow.")
        if self.workflow_dir.exists() or self.workflow_dir.is_symlink():
            raise WorkflowError(f"Workflow already exists; use resume: {self.workflow_id}")
        try:
            budget = Decimal(str(self.budget_usd if self.budget_usd is not None else DEFAULT_CLAUDE_BUDGET_USD))
        except Exception as exc:
            raise WorkflowError("--budget-usd must be a finite amount with at most two decimal places.") from exc
        if not budget.is_finite() or budget < Decimal("0.01"):
            raise WorkflowError("--budget-usd must be at least 0.01.")
        if budget != budget.quantize(Decimal("0.01")):
            raise WorkflowError("--budget-usd must use at most two decimal places.")
        with _target_process_lock(self.target):
            baseline = git(self.target, "rev-parse", "HEAD").decode("ascii", "strict").strip()
            initial_fingerprints = self._capture_initial_fingerprints(baseline)
            preexisting_paths = _protected_paths(self.target, baseline)
            initial_target = initial_fingerprints.get("target", {})
            baseline_snapshot = None
            if initial_target.get("status") == "captured":
                captured_snapshot = _workspace.capture_preexisting_snapshot(
                    self.target, preexisting_paths, redactor=redact,
                )
                captured_snapshot["baseline_fingerprint_sha256"] = initial_target.get("working_tree_sha256")
                snapshot_body = {key: value for key, value in captured_snapshot.items() if key != "sha256"}
                captured_snapshot["sha256"] = hashlib.sha256(json.dumps(
                    snapshot_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                ).encode("utf-8")).hexdigest()
                confirmed_target = self.execution._fingerprint_repository(self.target, expected_commit=baseline)
                stable_initial = {key: value for key, value in initial_target.items() if key != "captured_at"}
                stable_confirmed = {key: value for key, value in confirmed_target.items() if key != "captured_at"}
                if confirmed_target.get("status") == "captured":
                    if stable_confirmed != stable_initial:
                        raise WorkflowError(
                            "Target changed while the workflow baseline was being captured; workflow was not started."
                        )
                    baseline_snapshot = captured_snapshot
        self.workflow_dir = _check_plain_directories(self.target, WORKFLOW_ROOT, create=True) / self.workflow_id
        if self.workflow_dir.exists() or self.workflow_dir.is_symlink():
            raise WorkflowError(f"Workflow already exists: {self.workflow_id}")
        self.workflow_dir.mkdir()
        self.state_path = self.workflow_dir / "state.json"
        self.proposal_path = self.workflow_dir / "proposal.md"
        self.delivery_path = self.workflow_dir / "delivery.md"
        self.state = {
            "version": 1,
            "workflow_id": self.workflow_id,
            "target": str(self.target),
            "task": self.task.strip(),
            "created_at": now(),
            "updated_at": now(),
            "status": "running",
            "stage": "planning",
            "codex_model": self.model,
            "baseline_commit": baseline,
            "baseline_snapshot": baseline_snapshot,
            "execution_fingerprint": {
                "version": _workspace.FINGERPRINT_VERSION,
                "algorithm": _workspace.FINGERPRINT_ALGORITHM,
                "excluded_internal_paths": _workspace.FINGERPRINT_EXCLUDED_INTERNAL_PATHS.copy(),
                "excluded_sensitive_paths": "known sensitive path components and .env.* basenames",
                "initial": initial_fingerprints,
            },
            "preexisting_paths": preexisting_paths,
            "project_guidance": _project_guidance(self.target, self.task.strip()),
            "proposal_version": 0,
            "traceability_required": True,
            "commitment_ledger_approval": None,
            "commitment_evidence": None,
            "commitment_assessment": None,
            "commitment_gate": None,
            "delivery_disposition": None,
            "planning_validation_failures": [],
            "planning_validation_failure_history_truncated": False,
            "authorization": new_workflow_authorization(
                self.target, self.workflow_id, float(budget), self.max_code_review_calls,
                tracing_enabled=self.tracing_enabled,
                max_codex_calls=self.max_codex_calls,
                codex_timeout_seconds=self.timeout_seconds,
                claude_timeout_seconds=self.claude_timeout_seconds,
                workflow_timeout_seconds=self.workflow_timeout_seconds,
                github_publication=self.github_publication,
            ),
            "plan_reviews": [],
            "claude_contexts": [],
            "plan_review_decisions": [],
            "proposal": None,
            "implementation_approved_at": None,
            "implementation_summary": None,
            "review_included_files": [],
            "review_exclusions": [],
            "review_files_not_reviewed": [],
            "code_review": None,
            "correction_summary": None,
            "error": None,
            "operation_tracking_version": 1,
            "operation_history": [],
            "active_operation_id": None,
            "resource_usage": {
                "codex_calls": 0,
                "claude_calls": 0,
                "provider_runtime_seconds": 0.0,
                "workflow_runtime_seconds": 0.0,
                "correction_attempts": 0,
            },
            "correction_attempts": [],
            "recovery": None,
        }
        self.workflow_state.save()
        self._begin_execution_trace("start")
        return self.run_pending()

    def approve(self) -> dict:
        state = self._execute_locked(self._approve_locked, target_lock=True)
        return self._maybe_publish_github_pr(state)

    def _approve_locked(self) -> dict:
        self._load(preflight_tracing=True)
        if self.state.get("status") != "awaiting_approval":
            raise WorkflowError("This workflow is not waiting for implementation approval.")
        self._require_current_approval_baseline()
        current_proposal = self.execution._read_proposal()
        if current_proposal != clean_markdown(self.state.get("proposal", "")):
            raise WorkflowError("The proposal file changed after review; use revise to review the change before approval.")
        ledger_snapshot = None
        if self.state.get("traceability_required") is True:
            try:
                ledger = _traceability.parse_commitment_ledger(current_proposal)
            except _traceability.TraceabilityError as exc:
                raise WorkflowError(f"Proposal approval requires a valid, user-reviewable commitment ledger: {exc}") from exc
            proposal_sha256 = hashlib.sha256(_persistence.read_bytes(self.proposal_path)).hexdigest()
            ledger_snapshot = {
                "schema_version": _traceability.LEDGER_SCHEMA_VERSION,
                "proposal_version": self.state.get("proposal_version"),
                "proposal_sha256": proposal_sha256,
                "ledger": ledger,
                "ledger_sha256": _traceability.canonical_sha256(ledger),
                "approved_at": now(),
            }
        self._begin_execution_trace("approve")
        self.state["implementation_approved_at"] = {"time": now(), "method": "explicit approve command"}
        if ledger_snapshot is not None:
            self.state["commitment_ledger_approval"] = ledger_snapshot
        self.state["status"] = "running"
        self.state["stage"] = "implementation"
        self.state["error"] = None
        self.workflow_state.save()
        return self.run_pending()

    def revise(self, feedback: str) -> dict:
        return self._execute_locked(lambda: self._revise_locked(feedback))

    def _revise_locked(self, feedback: str) -> dict:
        self._load(preflight_tracing=True)
        if self.state.get("status") != "awaiting_approval":
            raise WorkflowError("This workflow is not waiting for proposal feedback.")
        if not feedback.strip():
            raise WorkflowError("Proposal feedback must not be empty.")
        self._begin_execution_trace("revise")
        self._invalidate_unverified_plan_reviews()
        self.state["proposal"] = self.execution._read_proposal()
        self.state["proposal_version"] = max(1, int(self.state.get("proposal_version", 1)))
        self.state["project_guidance"] = _project_guidance(self.target, self.state["task"])
        self.state["human_feedback"] = feedback.strip()
        self.state["status"] = "running"
        self.state["stage"] = "human_revision"
        self.state["error"] = None
        self.workflow_state.save()
        return self.run_pending()


    def _invalidate_unverified_plan_reviews(self) -> None:
        """Keep legacy review evidence, but do not count calls with no verifiable context."""
        for record in self.state.get("plan_reviews", []):
            if record.get("valid") is True and isinstance(record.get("context_manifest"), dict):
                continue
            record["valid"] = False
            record["invalidated_reason"] = (
                "Legacy review has no context manifest. The previous redaction helper truncated prompts at "
                "1,200 characters, so completeness cannot be established."
            )
            record["codex_disposition"] = (
                "Invalid review attempt; do not count its findings. Reassess the current proposal using complete context."
            )


    def reject(self, reason: str) -> dict:
        return self._execute_locked(lambda: self._reject_locked(reason))

    def _reject_locked(self, reason: str) -> dict:
        self._load()
        if self.state.get("status") != "awaiting_approval":
            raise WorkflowError("This workflow is not waiting for a proposal decision.")
        self._begin_execution_trace("reject")
        self.state["status"] = "rejected"
        self.state["rejection_reason"] = reason.strip() or "Rejected by the user."
        self.state["completed_at"] = now()
        self.execution._capture_final_target_fingerprint()
        self.workflow_state.save()
        return self.state

    def resume(
        self,
        *,
        recovery_action: str | None = None,
        verified_claude_cost_usd: float | None = None,
        confirm_provider_inactive: bool = False,
        confirm_process_inactive: bool = False,
        confirm_target_reviewed: bool = False,
        extend_budget: str | None = None,
        set_budget_limit: str | None = None,
    ) -> dict:
        state = self._execute_locked(lambda: self._resume_locked(
            recovery_action=recovery_action,
            verified_claude_cost_usd=verified_claude_cost_usd,
            confirm_provider_inactive=confirm_provider_inactive,
            confirm_process_inactive=confirm_process_inactive,
            confirm_target_reviewed=confirm_target_reviewed,
            extend_budget=extend_budget,
            set_budget_limit=set_budget_limit,
        ), target_lock="resume")
        return self._maybe_publish_github_pr(state)

    def _maybe_publish_github_pr(self, state: dict) -> dict:
        authorization = state.get("authorization") if isinstance(state, dict) else None
        if (isinstance(state, dict) and state.get("status") == "complete"
                and isinstance(authorization, dict)
                and isinstance(authorization.get("github_publication"), dict)):
            return self.publish_github_pr()
        return state

    def publish_github_pr(self, *, explicitly_authorized: bool = False) -> dict:
        if type(explicitly_authorized) is not bool:
            raise WorkflowError("Publication authorization must be an explicit boolean.")
        return self._execute_locked(
            lambda: self._publish_github_pr_locked(explicitly_authorized=explicitly_authorized),
            target_lock=True,
        )

    def _publish_github_pr_locked(self, *, explicitly_authorized: bool) -> dict:
        self._load()
        if self.state.get("status") != "complete":
            raise WorkflowError("Pull Request publication requires a completed workflow.")
        self._verify_delivery_integrity()
        self._verify_delivery_artifact_hash()
        gate = self.state.get("commitment_gate")
        if not isinstance(gate, dict) or gate.get("disposition") != "complete":
            raise WorkflowError("Pull Request publication is blocked because the commitment gate is not complete.")
        authorization = self.state.get("authorization", {})
        saved_scope = authorization.get("github_publication") if isinstance(authorization, dict) else None
        if saved_scope is None and not explicitly_authorized:
            raise WorkflowError(
                "This workflow authorization does not permit GitHub publication; rerun `github publish` with explicit authorization."
            )
        initial_target = (
            self.state.get("execution_fingerprint", {}).get("initial", {}).get("target", {})
            if isinstance(self.state.get("execution_fingerprint"), dict) else {}
        )
        if initial_target.get("working_tree_clean") is not True:
            raise WorkflowError(
                "Pull Request publication is blocked because the target had pre-existing uncommitted changes."
            )
        try:
            scope = _github.publication_scope_for_workflow(
                self.target, self.workflow_id, self.state.get("task", ""),
            )
        except _github.GithubError as exc:
            raise WorkflowError(str(exc)) from exc
        if saved_scope is not None and saved_scope != scope:
            raise WorkflowError("Saved GitHub publication authorization does not match the Issue correlation.")
        if saved_scope is None and not explicitly_authorized:
            raise WorkflowError("GitHub publication requires explicit authorization.")
        evidence = self.state.get("integrity", {}).get("review_evidence", {})
        if evidence.get("target_commit_sha") != self.state.get("baseline_commit"):
            raise WorkflowError(
                "Pull Request publication is blocked because the reviewed commit is not the saved workflow baseline."
            )
        review_diff = _review_diff(self.state, self.target)
        if _review_source_hash_matching_evidence(self.state, review_diff, evidence) is None:
            raise WorkflowError("Pull Request publication is blocked because review evidence no longer matches the source.")
        try:
            reviewed_files = _github.reviewed_files_from_diff(self.target, review_diff)
        except _github.GithubError as exc:
            raise WorkflowError(str(exc)) from exc
        publication = self.state.setdefault("github_publication", {})
        if not isinstance(publication, dict):
            raise WorkflowError("Saved GitHub publication state is invalid.")
        if saved_scope is None:
            publication.setdefault("authorization_source", "explicit_cli_confirmation")
            publication.setdefault("authorized_at", now())
        else:
            publication.setdefault("authorization_source", "workflow_authorization")
        try:
            _github.publish_pull_request(
                self.target,
                self.workflow_id,
                task=self.state.get("task", ""),
                scope=scope,
                baseline_commit=self.state.get("baseline_commit"),
                source_patch_sha256=evidence["source_patch_sha256"],
                reviewed_files=reviewed_files,
                publication=publication,
                save=self.workflow_state.save,
            )
        except Exception as exc:
            message = str(exc) if isinstance(exc, _github.GithubError) else "GitHub publication failed; retry the publication command."
            publication["status"] = "failed"
            publication["error"] = redact(message)
            publication["updated_at"] = now()
            self.workflow_state.save()
            raise WorkflowError(message) from exc
        return self.state

    def _resume_locked(
        self,
        *,
        recovery_action: str | None = None,
        verified_claude_cost_usd: float | None = None,
        confirm_provider_inactive: bool = False,
        confirm_process_inactive: bool = False,
        confirm_target_reviewed: bool = False,
        extend_budget: str | None = None,
        set_budget_limit: str | None = None,
    ) -> dict:
        budget_change_requested = extend_budget is not None or set_budget_limit is not None
        if extend_budget is not None and set_budget_limit is not None:
            raise WorkflowError("Choose only one of --extend-budget or --set-budget-limit.")
        self._load(preflight_tracing=True)
        if self.state.get("status") == "awaiting_approval":
            if budget_change_requested:
                raise WorkflowError("Budget changes are available only when resuming a saved failed stage.")
            return self.state
        if self.state.get("status") == "complete":
            self._verify_delivery_integrity()
            self._verify_delivery_artifact_hash()
            if budget_change_requested:
                raise WorkflowError("Budget changes are available only when resuming a saved failed stage.")
            return self.state
        recovery_resolved = False
        if self.state.get("status") == "recovery_required":
            self._resolve_recovery(
                recovery_action,
                verified_claude_cost_usd=verified_claude_cost_usd,
                confirm_provider_inactive=confirm_provider_inactive,
                confirm_process_inactive=confirm_process_inactive,
                confirm_target_reviewed=confirm_target_reviewed,
            )
            recovery_resolved = True
        elif self.state.get("status") == "running":
            if recovery_action is not None:
                raise WorkflowError("No recovery action is pending for this workflow.")
        elif self.state.get("status") != "failed":
            raise WorkflowError(f"Workflow cannot resume from status {self.state.get('status')!r}.")
        elif recovery_action == "accept-target-for-review":
            if (verified_claude_cost_usd is not None or confirm_provider_inactive
                    or confirm_process_inactive):
                raise WorkflowError("Current-target review acceptance cannot include crash-recovery confirmations.")
            self._accept_current_target_for_review(confirm_target_reviewed=confirm_target_reviewed)
        elif recovery_action is not None or verified_claude_cost_usd is not None:
            raise WorkflowError("No recovery action is pending for this workflow.")
        authorization = validate_workflow_authorization(self.state, self.target, self.workflow_id)
        if (authorization["claude_aily"]["budget_unknown"]
                and self.state.get("stage", "").startswith(("plan_review", "code_review", "verification"))):
            raise WorkflowError(self.state.get("error") or "Claude review stopped because spend is unknown.")
        if budget_change_requested:
            if not (self.state.get("status") == "failed" or recovery_resolved):
                raise WorkflowError("Budget changes are available only when resuming a saved failed stage.")
            external = authorization["claude_aily"]
            if external.get("budget_unknown") is True:
                raise WorkflowError(self.state.get("error") or "Claude review stopped because spend is unknown.")
            stage = self.state.get("stage")
            if not isinstance(stage, str):
                raise WorkflowError("The saved failed stage is invalid; no budget change was applied.")
            operation, amount = (
                ("extend", extend_budget) if extend_budget is not None else ("set_limit", set_budget_limit)
            )
            entry = authorize_budget_change(
                authorization, amount, operation, workflow_id=self.workflow_id, stage=stage,
            )
            self.workflow_state.save()
            print(
                f"Claude/Aily budget authorization saved: ${entry['previous_limit_usd']:.2f} to "
                f"${entry['new_limit_usd']:.2f}; remaining ${entry['remaining_usd']:.4f} "
                f"for workflow {self.workflow_id} at stage {stage}.",
                file=sys.stderr,
            )
            with localcontext() as context:
                context.prec = 1000
                remaining = (
                    Decimal(str(external["budget_usd"])) - Decimal(str(external["spent_usd"]))
                )
            with localcontext() as context:
                context.prec = 1000
                available = remaining.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if available < Decimal("0.01"):
                self.state["status"] = "failed"
                self.state["error"] = (
                    "The saved budget change was authorized, but less than USD 0.01 remains; "
                    "the failed stage was not retried."
                )
                self.workflow_state.save()
                raise WorkflowError(self.state["error"])
        self._begin_execution_trace("resume")
        self.state["status"] = "running"
        self.state["error"] = None
        self.workflow_state.save()
        return self.run_pending()

    def _execute_locked(self, operation: Callable[[], dict], *, target_lock: bool | str = False) -> dict:
        try:
            with self._workflow_lock():
                acquire_target = target_lock is True
                if target_lock == "resume":
                    saved = self._read_state()
                    planning_stages = {
                        "planning", "plan_review_1", "plan_review_2", "plan_refine_1", "plan_refine_2",
                        "human_revision",
                    }
                    acquire_target = (
                        saved.get("status") == "recovery_required"
                        or saved.get("stage") not in planning_stages
                    )
                if acquire_target:
                    with _target_process_lock(self.target):
                        result = operation()
                else:
                    result = operation()
        except BaseException as exc:
            self._finish_execution_trace(exc)
            raise
        self._finish_execution_trace(None)
        return result

    def _begin_execution_trace(self, command: str) -> None:
        authorization = self.state.get("authorization", {})
        scope = authorization.get("observability", {})
        if scope.get("enabled") is not True:
            return
        try:
            from observability.langfuse import create_langfuse_tracer

            self._tracer = create_langfuse_tracer(scope)
            workflow_ref = safe_workflow_reference(self.workflow_id)
            initial_stage = self.state.get("stage")
            if not isinstance(initial_stage, str) or initial_stage not in TRACE_STAGES:
                initial_stage = "unknown"
            self._trace_started_at = time.monotonic()
            self._active_trace_context = isolated_context(lambda: self._tracer.execution(
                name="workflow.execution",
                session_id=workflow_ref,
                metadata={
                    "command": command,
                    "session_id": workflow_ref,
                    "initial_stage": initial_stage,
                },
            ))
            self._active_trace_context.__enter__()
        except Exception:
            self._tracer = None
            self._active_trace_context = None
            self._trace_started_at = None
            print("Tracing warning: could not initialize tracing; continuing without it.", file=sys.stderr)


    def _finish_execution_trace(self, error: BaseException | None) -> None:
        context = self._active_trace_context
        tracer = self._tracer
        self._active_trace_context = None
        self._tracer = None
        started_at = self._trace_started_at
        self._trace_started_at = None
        if context is None:
            return
        final_stage = self.state.get("stage") if isinstance(self.state, dict) else None
        final_status = self.state.get("status") if isinstance(self.state, dict) else None
        if not isinstance(final_stage, str) or final_stage not in TRACE_STAGES:
            final_stage = "unknown"
        if final_status not in {
            "running", "awaiting_approval", "failed", "recovery_required", "complete", "rejected",
        }:
            final_status = "unknown"
        duration = max(0, round((time.monotonic() - started_at) * 1000)) if started_at is not None else 0
        context.update(
            final_stage=final_stage,
            final_status=final_status,
            duration_ms=duration,
            execution_result="success" if error is None else "failure",
            **({"error_category": type(error).__name__} if error is not None else {}),
        )
        context.__exit__(type(error) if error is not None else None, error, error.__traceback__ if error else None)
        try:
            tracer.flush()
        except Exception:
            print("Tracing warning: export failed; workflow result is unchanged.", file=sys.stderr)

    def status(self) -> dict:
        self._read_state()
        self.state.setdefault("workflow_status", self.state.get("status"))
        self.state.setdefault("delivery_disposition", None)
        recovery = self.state.get("recovery")
        if isinstance(recovery, dict):
            recovery["target_changes"] = self.execution._target_change_inventory()
            operation = self._operation(recovery.get("operation_id"))
            process_id = operation.get("process_id") if isinstance(operation, dict) else None
            if isinstance(process_id, int) and not isinstance(process_id, bool) and process_id > 0:
                try:
                    os.kill(process_id, 0)
                except ProcessLookupError:
                    recovery["process_presence"] = "not_running"
                except PermissionError:
                    recovery["process_presence"] = "present_unverified"
                except OSError:
                    recovery["process_presence"] = "unknown"
                else:
                    recovery["process_presence"] = "present_unverified"
        if self.state.get("status") == "complete":
            integrity = self.state.setdefault("integrity", {})
            try:
                self._verify_delivery_integrity()
                self._verify_delivery_artifact_hash()
            except WorkflowError as exc:
                integrity["delivery_artifact_status"] = "integrity_failure"
                integrity["delivery_artifact_error"] = redact(str(exc))
            else:
                integrity["delivery_artifact_status"] = "verified"
        return self.state

    def _verify_delivery_artifact_hash(self) -> None:
        integrity = self.state.get("integrity")
        expected = integrity.get("delivery_artifact_sha256") if isinstance(integrity, dict) else None
        try:
            actual = hashlib.sha256(_persistence.read_bytes(self.delivery_path)).hexdigest()
        except OSError as exc:
            raise WorkflowError("The delivered report is missing or unreadable.") from exc
        if not isinstance(expected, str) or actual != expected:
            raise WorkflowError("The delivered report changed after the workflow completed.")

    def _run_codex(self, prompt: str, *, sandbox: str) -> str:
        return self.execution._run_codex(prompt, sandbox=sandbox)

    def _call_claude(
        self,
        header: str,
        stage: str,
        sections: dict[str, str],
        *,
        metadata: dict | None = None,
    ) -> tuple[str, dict]:
        return self.execution._call_claude(header, stage, sections, metadata=metadata)

    def _verify_delivery_integrity(self) -> dict:
        return self.execution._verify_delivery_integrity()

    def _fail(self, exc: Exception) -> None:
        latest_operation = next(iter(reversed(self.state.get("operation_history", []))), None)
        if isinstance(latest_operation, dict):
            if (latest_operation.get("status") == "completed"
                    and latest_operation.get("stage") == self.state.get("stage")
                    and latest_operation.get("result_text") is not None):
                latest_operation["result_text"] = None
                latest_operation["result_consumed"] = True
                latest_operation["result_rejected_at"] = now()
            unsafe_codex_failure = (
                latest_operation.get("operation_type") == "codex"
                and latest_operation.get("sandbox") == "workspace-write"
                and latest_operation.get("status") in {"failed", "interrupted"}
                and latest_operation.get("launch_state") != "not_started"
            )
            if latest_operation.get("status") in {"unknown", "interrupted"} or unsafe_codex_failure:
                self._require_recovery(
                    latest_operation,
                    redact(str(exc)) or "The operation may have stopped after partially changing the target.",
                )
                return
        self.state["status"] = "failed"
        self.state["error"] = redact(str(exc))
        self.execution._capture_final_target_fingerprint()
        self.workflow_state.save()


    def run_pending(self) -> dict:
        if not self.workflow_state.lock_held:
            raise WorkflowError("Workflow execution requires the workflow process lock.")
        self.workflow_state.begin_workflow_runtime()
        try:
            return self._run_pending_stages()
        finally:
            self.workflow_state.finish_workflow_runtime()

    def _transition_after_stage(self, stage: str, outcome: StageOutcome) -> dict[str, str]:
        """Map stage results to saved workflow transitions in the coordinator."""
        result = outcome.result
        if stage == "planning" and result == "completed":
            return {"stage": "plan_review_1"}
        if stage in {"plan_review_1", "plan_review_2"} and result == "completed":
            return {"stage": f"plan_refine_{stage[-1]}"}
        if stage == "plan_refine_1":
            if result == "review_again":
                return {"stage": "plan_review_2"}
            if result == "awaiting_approval":
                return {"stage": "implementation", "status": "awaiting_approval"}
        if stage == "plan_refine_2" and result == "awaiting_approval":
            return {"stage": "implementation", "status": "awaiting_approval"}
        if stage == "human_revision":
            if result == "next_review":
                iteration = len(_planning_review.valid_plan_reviews(self.state)) + 1
                limit = self.state["authorization"]["claude_aily"]["max_plan_reviews"]
                if iteration > limit:
                    raise WorkflowError("Human revision requested a plan review beyond the authorized limit.")
                return {"stage": f"plan_review_{iteration}"}
            if result == "awaiting_approval":
                return {"stage": "implementation", "status": "awaiting_approval"}
        if stage == "implementation":
            if result == "implementation_not_approved":
                return {"status": "awaiting_approval"}
            if result == "implemented":
                return {"stage": "code_review"}
        if stage == "code_review":
            if result == "review_clean":
                return {"stage": "complete"}
            if result == "blocking_findings":
                return {"stage": "correction"}
        if stage == "correction" and result == "corrected":
            return {"stage": "verification"}
        if stage == "verification" and result == "review_clean":
            return {"stage": "complete"}
        if stage == "complete" and result == "completed":
            return {"status": "complete"}
        raise WorkflowError(f"Stage {stage} returned unsupported transition result: {result}.")


    def _run_pending_stages(self) -> dict:
        while self.state.get("status") == "running":
            stage = self.state["stage"]
            stage_label = stage if isinstance(stage, str) and stage in TRACE_STAGES else "unknown"
            stage_started = time.monotonic()
            stage_context = self.execution._trace_span("workflow.stage", {"stage": stage_label})
            stage_context.__enter__()
            try:
                if self.workflow_state.workflow_runtime_remaining() <= 0:
                    raise WorkflowError("The authorized workflow execution-time limit is exhausted.")
                outcome = self.execution._execute_stage(stage)
                self.workflow_state.transition(self._transition_after_stage(stage, outcome))
                self.workflow_state.save()
            except KeyboardInterrupt:
                if self.state.get("status") != "recovery_required":
                    operation = self.workflow_state.operation(self.state.get("active_operation_id"))
                    error = f"Workflow cancelled during stage {stage}; resume will retry only the saved stage."
                    if operation is not None and operation.get("status") == "started":
                        if operation.get("launch_state") == "not_started":
                            operation.update({
                                "status": "failed",
                                "finished_at": now(),
                                "outcome": "Workflow was cancelled before launching this external operation; safe to retry.",
                                "error_category": "CancelledBeforeLaunch",
                            })
                            if self.state.get("active_operation_id") == operation.get("operation_id"):
                                self.state["active_operation_id"] = None
                            if operation.get("operation_type") == "claude":
                                external = self.state.get("authorization", {}).get("claude_aily", {})
                                active_call = external.get("active_call")
                                if isinstance(active_call, dict) and active_call.get("operation_id") == operation.get("operation_id"):
                                    external["active_call"] = None
                                for context in reversed(self.state.get("claude_contexts", [])):
                                    if context.get("operation_id") == operation.get("operation_id"):
                                        context["call_state"] = "not_started"
                                        context["error"] = operation["outcome"]
                                        break
                            self.workflow_state.record_operation_runtime(operation, 0)
                            self.state["status"] = "failed"
                            self.state["error"] = error
                            self.state["cancellation"] = {"stage": stage, "cancelled_at": now(), "outcome": error}
                            self.workflow_state.save()
                        else:
                            self.state["cancellation"] = {
                                "stage": stage,
                                "cancelled_at": now(),
                                "outcome": "Process group was stopped locally; any accepted remote provider work may remain unknown.",
                                "requires_recovery": True,
                            }
                            self._require_recovery(operation, error)
                    else:
                        self.state["status"] = "failed"
                        self.state["error"] = error
                        self.state["cancellation"] = {"stage": stage, "cancelled_at": now(), "outcome": error}
                        self.execution._capture_final_target_fingerprint()
                        self.workflow_state.save()
                else:
                    self.state["cancellation"] = {
                        "stage": stage,
                        "cancelled_at": now(),
                        "outcome": "The active provider operation was interrupted and requires recovery.",
                        "requires_recovery": True,
                    }
                    self.workflow_state.save()
                raise
            except Exception as exc:
                self._fail(exc)
                raise WorkflowError(f"Workflow stopped at {stage}: {redact(str(exc))}") from exc
            finally:
                stage_context.update(duration_ms=max(0, round((time.monotonic() - stage_started) * 1000)))
                stage_context.__exit__(*sys.exc_info())
        return self.state



def recover_code_review(target: str | Path, workflow_id: str, *, apply: bool = False) -> dict:
    """Revalidate persisted part reports locally; never calls a provider."""
    runner = Orchestrator(target, workflow_id=workflow_id)
    if apply:
        with runner._workflow_lock():
            return _recover_code_review(runner, workflow_id, apply=True)
    return _recover_code_review(runner, workflow_id, apply=False)


def _recover_code_review(runner: Orchestrator, workflow_id: str, *, apply: bool) -> dict:
    _safe_artifact(runner.state_path)
    try:
        original_bytes, state = _persistence.read_json_bytes(runner.state_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Workflow state is missing or invalid: {runner.state_path}") from exc
    if state.get("workflow_id") != workflow_id or state.get("target") != str(runner.target):
        raise WorkflowError("Workflow state belongs to another target or workflow ID.")
    authorization = validate_workflow_authorization(state, runner.target, workflow_id)
    if authorization["claude_aily"].get("active_call") or authorization["claude_aily"].get("budget_unknown"):
        raise WorkflowError(
            "Local review recovery is unsafe while a Claude call is active or its cost is unknown."
        )
    if state.get("status") != "failed" or state.get("stage") != "code_review":
        raise WorkflowError("Local code-review recovery requires a failed workflow at the code_review stage.")
    campaign = state.get("review_campaign")
    if not isinstance(campaign, dict) or not campaign.get("campaign_id"):
        raise WorkflowError("No persisted code-review campaign is available to recover.")

    runner.state = state
    try:
        review_diff = _review_diff(state, runner.target)
    except Exception as exc:
        raise WorkflowError(f"Could not safely reconstruct the saved target diff: {redact(str(exc))}") from exc
    shared = runner.execution._code_review_shared_context(review_diff)
    result, proposed = WorkflowExecution._campaign_helper(lambda: _recover_review_campaign(
        state, workflow_id, runner.target, review_diff, shared, apply=apply,
    ))
    if proposed is None:
        return result
    if _persistence.read_bytes(runner.state_path) != original_bytes:
        raise WorkflowError("Workflow state changed during local recovery; no recovery changes were written.")
    proposed["updated_at"] = now()
    _write_json(runner.state_path, proposed)
    result["applied"] = True
    result["campaign_completed"] = proposed["review_campaign"].get("status") == "complete"
    result["workflow_status_preserved"] = proposed.get("status")
    result["workflow_stage_preserved"] = proposed.get("stage")
    return result

def _task_text(args: argparse.Namespace) -> str:
    return _cli.task_text(args, workflow_error=WorkflowError)


def _code_review_calls_arg(value: str) -> int:
    return _cli.code_review_calls_arg(value, maximum=MAX_AUTHORIZED_CODE_REVIEW_CALLS)


def _print_summary(state: dict) -> None:
    return _cli.print_summary(state, workflow_root=WORKFLOW_ROOT)


def main(argv: list[str] | None = None) -> int:
    return _cli.main(
        argv,
        runner_factory=Orchestrator,
        recover_review=recover_code_review,
        workflow_error=WorkflowError,
        workflow_root=WORKFLOW_ROOT,
        max_authorized_code_review_calls=MAX_AUTHORIZED_CODE_REVIEW_CALLS,
    )


if __name__ == "__main__":
    raise SystemExit(main())

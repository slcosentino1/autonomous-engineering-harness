"""Stage and provider execution for a single approved workflow."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path
from typing import Callable

import claude_reviewer
from tools.read_documentation_url import canonicalize_documentation_url
from observability.tracer import NoOpTracer, isolated_context, safe_workflow_reference
from workflow import authorization as _authorization
from workflow import persistence as _persistence
from workflow import planning_review as _planning_review
from workflow.planning_review import (
    ARCHITECTURAL_DECISION_OUTPUT, COMMITMENT_LEDGER_GUIDANCE, PLAN_REVIEW_GUIDANCE,
    PLANNING_PRINCIPLES, clean_markdown,
)
from workflow import skills as _skills
from workflow import traceability as _traceability
from workflow import workspace as _workspace
from workflow.codex_executor import (
    CodexExecutor,
    READ_ONLY_WEB_ACCESS_INSTRUCTION,
    assert_no_project_mcp_servers as _assert_no_project_mcp_servers,
    build_codex_command,
    configured_codex_mcp_servers as _configured_codex_mcp_servers,
)
from workflow.skills import (
    file_inventory_section as _file_inventory_section,
    guidance_for_claude as _guidance_for_claude,
    guidance_for_codex as _guidance_for_codex,
)
from workflow.state import MAX_PLANNING_FAILURE_OUTPUT_CHARS, MAX_PLANNING_FAILURE_RECORDS, TRACE_STAGES
from workflow.workspace import MAX_EXECUTION_CHANGE_EXAMPLES
from workflow.codex_evidence import (
    _codex_langfuse_metadata, _codex_stream_evidence, _json_line_message, _output_bytes,
)
from workflow.delivery import (
    render_delivery_report,
    verify_delivery_integrity as _delivery_verify_integrity,
)
from workflow.review import (
    CLAUDE_CODE_REVIEW_GUIDANCE,
    ReviewCampaign as _ReviewCampaign,
    ReviewCampaignError as _ReviewCampaignError,
    ReviewDiff,
    _clearly_no_review_findings,
    _post_correction_dispositions,
    _safe_review_path,
    _validated_campaign_reports,
    render_review_feedback,
    build_review_diff as _review_diff, record_review_snapshot as _record_review_snapshot,
)
from workflow.state import WorkflowError, WorkflowState


def _safe_artifact(path: Path) -> None:
    try:
        _persistence.safe_artifact(path)
    except _persistence.PersistenceError as exc:
        raise WorkflowError(str(exc)) from exc


def _atomic_text(path: Path, value: str) -> None:
    try:
        _persistence.atomic_text(path, value)
    except _persistence.PersistenceError as exc:
        raise WorkflowError(str(exc)) from exc


def _write_json(path: Path, value: dict) -> None:
    try:
        _persistence.write_json(path, value)
    except _persistence.PersistenceError as exc:
        raise WorkflowError(str(exc)) from exc


def validate_workflow_authorization(state: dict, target: Path, workflow_id: str) -> dict:
    try:
        return _authorization.validate_workflow_authorization(state, target, workflow_id)
    except _authorization.AuthorizationError as exc:
        raise WorkflowError(str(exc)) from exc

def _format_context_prompt(header: str, sections: dict[str, str]) -> str:
    blocks = [header.strip()]
    for name, value in sections.items():
        content = value if name == "Complete patch" else value.strip()
        blocks.append(f"## {name}\n{content}")
    return "\n\n".join(blocks).strip()

def _planning_read_only_authorized_urls(stage: str, task: str) -> list[str]:
    if stage not in {"planning", "plan_refine_1", "plan_refine_2", "human_revision"}:
        return []
    urls = re.findall(r"https://[^\s<>\]\)\"']+", task, re.IGNORECASE)
    authorized_urls = set()
    for url in urls:
        try:
            canonical_url = canonicalize_documentation_url(url.rstrip(".,;:!?"))
        except ValueError:
            continue
        authorized_urls.add(canonical_url)
    return sorted(authorized_urls)

def _project_guidance(target: Path, task: str) -> dict:
    return _workspace_error(lambda: _workspace.project_guidance(target, task, redactor=redact))

def _sha256_text(value: str) -> str | None:
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None

def _workspace_error(call):
    try:
        return call()
    except _workspace.WorkspaceError as exc:
        raise WorkflowError(str(exc)) from exc

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def redact(value: str) -> str:
    return claude_reviewer.redact(value)


CLAUDE_DATA_SCOPE = _authorization.CLAUDE_DATA_SCOPE

CLAUDE_MAX_PAYLOAD_ENV = claude_reviewer.MAX_REVIEW_PAYLOAD_ENV














@dataclass
class ExecutionConfig:
    target: Path
    workflow_id: str
    codex: str
    aily: str
    model: str | None
    budget_usd: float | None
    max_code_review_calls: int
    max_codex_calls: int
    timeout_seconds: int
    claude_timeout_seconds: int
    workflow_timeout_seconds: int
    process_runner: Callable
    claude_runner: Callable
    recovery_required: Callable[[dict | None, str], None]


@dataclass(frozen=True)
class StageOutcome:
    """A stage result that the workflow coordinator maps to a persisted transition."""

    result: str

    def __post_init__(self) -> None:
        allowed = {
            "completed", "review_again", "awaiting_approval", "next_review",
            "implementation_not_approved", "implemented", "review_clean",
            "blocking_findings", "corrected",
        }
        if not isinstance(self.result, str) or self.result not in allowed:
            raise WorkflowError(f"Stage outcome contains unsupported result: {self.result}.")


class WorkflowExecution:
    """Runs saved stages using explicit state and provider configuration."""

    def __init__(self, state: WorkflowState, config: ExecutionConfig) -> None:
        self.workflow_state = state
        self.target = config.target
        self.workflow_id = config.workflow_id
        self.codex = config.codex
        self.aily = config.aily
        self.model = config.model
        self.budget_usd = config.budget_usd
        self.max_code_review_calls = config.max_code_review_calls
        self.max_codex_calls = config.max_codex_calls
        self.timeout_seconds = config.timeout_seconds
        self.claude_timeout_seconds = config.claude_timeout_seconds
        self.workflow_timeout_seconds = config.workflow_timeout_seconds
        self.process_runner = config.process_runner
        self.claude_runner = config.claude_runner
        self.recovery_required = config.recovery_required
        self.workflow_dir = state.path.parent
        self.proposal_path = self.workflow_dir / "proposal.md"
        self.delivery_path = self.workflow_dir / "delivery.md"
        self.codex_executor = CodexExecutor(config.process_runner)
        self._tracer = None
        self._active_codex_trace_context = None
        self._active_codex_operation_id = None
        self._active_codex_persisted_status = None
        self._codex_cli_version_checked = False
        self._codex_cli_version_cache = None
        self._pending_stage_result: str | None = None
        self._stage_execution_active = False

    @property
    def state(self) -> WorkflowState:
        return self.workflow_state

    def _save(self) -> None:
        self.workflow_state.save()

    def set_tracer(self, tracer) -> None:
        self._tracer = tracer

    def _finish_operation(self, operation_id: str, **kwargs) -> None:
        operation = self.workflow_state.finish_operation(operation_id, **kwargs)
        if operation.get("operation_type") == "codex":
            self._update_active_codex_trace(operation)

    def _update_active_codex_trace(
        self, operation: dict, *, status: str | None = None, finished_at: str | None = None,
    ) -> None:
        trace_context = getattr(self, "_active_codex_trace_context", None)
        if trace_context is not None:
            trace_context.update(**_codex_langfuse_metadata(
                operation, status=status, finished_at=finished_at,
            ))
        self._active_codex_persisted_status = operation.get("status")

    def _target_change_inventory(self) -> list[dict[str, str]]:
        return _workspace_error(lambda: _workspace.target_change_inventory(
            self.target,
            self.state.get("baseline_commit"),
            self.state.get("preexisting_paths", []),
            redactor=redact,
        ))

    def _fingerprint_repository(self, repository: Path, *, expected_commit: str | None = None) -> dict:
        try:
            fingerprint = _workspace.repository_fingerprint(
                repository, redactor=redact, expected_commit=expected_commit,
            )
        except Exception as exc:
            return {
                "status": "unavailable",
                "error_category": type(exc).__name__,
                "error": "Fingerprint calculation failed; repository state is unavailable.",
            }
        return {"status": "captured", "captured_at": now(), **fingerprint}

    def _write_operation_start_fingerprint(self, stage: str) -> dict | None:
        """Ensure a new write operation begins from the exact approved or reviewed target state."""
        if not isinstance(self.state.get("baseline_snapshot"), dict):
            return None
        current = self._fingerprint_repository(self.target)
        if current.get("status") != "captured":
            raise WorkflowError("Could not capture the target fingerprint for review provenance.")
        if stage == "implementation":
            execution = self.state.get("execution_fingerprint", {})
            expected = execution.get("initial", {}).get("target", {}) if isinstance(execution, dict) else {}
            operations = self.state.get("operation_history", [])
            approved_retry = next((
                item for item in reversed(operations)
                if isinstance(item, dict) and item.get("operation_type") == "codex"
                and item.get("stage") == "implementation"
                and item.get("resolution") == "user_approved_retry_after_target_inspection"
            ), None) if isinstance(operations, list) else None
            if approved_retry is not None:
                expected = approved_retry.get("retry_reviewed_target_fingerprint", {})
            message = "Target changed after the workflow baseline and before implementation."
        elif stage == "correction":
            snapshot = self.state.get("implementation_snapshot", {})
            expected = snapshot.get("target_fingerprint", {}) if isinstance(snapshot, dict) else {}
            message = "Target changed after the initial review and before correction."
        else:
            return None
        expected_sensitive = expected.get("sensitive_path_status") if isinstance(expected, dict) else None
        comparison_current = current
        if (isinstance(expected_sensitive, dict)
                and "anonymous_path_metadata" not in expected_sensitive):
            # Old persisted fingerprints predate path metadata. Approval validation
            # already checks their available evidence conservatively; keep the same
            # compatibility rule for the write-start comparison.
            stable_current_sensitive = current.get("sensitive_path_status")
            if isinstance(stable_current_sensitive, dict):
                stable_current_sensitive = dict(stable_current_sensitive)
                stable_current_sensitive.pop("anonymous_path_metadata", None)
                comparison_current = {**current, "sensitive_path_status": stable_current_sensitive}
        stable_expected = {key: value for key, value in expected.items() if key != "captured_at"}
        stable_current = {key: value for key, value in comparison_current.items() if key != "captured_at"}
        if expected.get("status") != "captured" or stable_current != stable_expected:
            raise WorkflowError(message + " Reconcile the target and start a new workflow.")
        return current

    def _require_write_operation_target(self, purpose: str) -> None:
        acceptance_history = self.state.get("review_target_acceptances")
        legacy_acceptance = self.state.get("review_target_acceptance")
        baseline_snapshot = self.state.get("baseline_snapshot")
        if not isinstance(baseline_snapshot, dict):
            if purpose == "initial" and (acceptance_history is not None or isinstance(legacy_acceptance, dict)):
                raise WorkflowError("Accepted target cannot be verified because its baseline snapshot is unavailable.")
            return
        if purpose == "initial" and (acceptance_history is not None or isinstance(legacy_acceptance, dict)):
            if not isinstance(acceptance_history, list) or not acceptance_history:
                raise WorkflowError(
                    "Saved target acceptance has no baseline diff binding; inspect and accept the exact target again."
                )
            operations = self.state.get("operation_history", [])
            implementation = next((
                item for item in reversed(operations)
                if isinstance(item, dict) and item.get("operation_type") == "codex"
                and item.get("stage") == "implementation" and item.get("status") == "completed"
            ), None) if isinstance(operations, list) else None
            baseline_commit = self.state.get("baseline_commit")
            execution_fingerprints = self.state.get("execution_fingerprint")
            initial_target = (
                execution_fingerprints.get("initial", {}).get("target", {})
                if isinstance(execution_fingerprints, dict) else {}
            )
            baseline_snapshot = self.state.get("baseline_snapshot")
            snapshot_sha256 = baseline_snapshot.get("sha256") if isinstance(baseline_snapshot, dict) else None
            if (not isinstance(implementation, dict) or not isinstance(baseline_commit, str)
                    or not baseline_commit or not isinstance(initial_target, dict)
                    or initial_target.get("status") != "captured"
                    or not isinstance(initial_target.get("working_tree_sha256"), str)):
                raise WorkflowError("Saved target acceptance is not bound to a completed implementation and baseline.")
            if (baseline_snapshot.get("baseline_fingerprint_sha256")
                    != initial_target.get("working_tree_sha256")):
                raise WorkflowError("Saved baseline snapshot does not match the immutable initial target fingerprint.")

            expected_previous = implementation.get("target_fingerprint_after")
            if (not isinstance(expected_previous, dict) or expected_previous.get("status") != "captured"
                    or expected_previous.get("commit_sha") != baseline_commit
                    or not isinstance(expected_previous.get("working_tree_sha256"), str)):
                raise WorkflowError("Completed implementation fingerprint is unavailable for target acceptance history.")

            for index, acceptance in enumerate(acceptance_history):
                if not isinstance(acceptance, dict):
                    raise WorkflowError("Saved target acceptance history is invalid.")
                previous = acceptance.get("previous_target_fingerprint")
                accepted = acceptance.get("target_fingerprint")
                if (acceptance.get("operation_id") != implementation.get("operation_id")
                        or not isinstance(acceptance.get("accepted_at"), str)
                        or not acceptance.get("accepted_at")
                        or acceptance.get("confirmation") != "explicit_target_reviewed_for_independent_review"
                        or acceptance.get("baseline_commit") != baseline_commit
                        or acceptance.get("baseline_fingerprint_sha256") != initial_target.get("working_tree_sha256")
                        or acceptance.get("baseline_snapshot_sha256") != snapshot_sha256
                        or not isinstance(previous, dict) or previous.get("status") != "captured"
                        or not isinstance(accepted, dict) or accepted.get("status") != "captured"
                        or previous.get("commit_sha") != baseline_commit
                        or accepted.get("commit_sha") != baseline_commit
                        or previous.get("working_tree_sha256") != expected_previous.get("working_tree_sha256")
                        or not isinstance(accepted.get("working_tree_sha256"), str)):
                    raise WorkflowError("Saved target acceptance chain is inconsistent with the implementation baseline.")
                is_legacy = acceptance.get("legacy_record_without_diff_binding") is True
                if is_legacy:
                    if index != 0 or acceptance.get("baseline_diff_sha256") is not None:
                        raise WorkflowError("Legacy target acceptance is inconsistent with the append-only history.")
                elif (not re.fullmatch(r"[0-9a-f]{64}", str(acceptance.get("baseline_diff_sha256", "")))
                      or not isinstance(acceptance.get("changed_paths"), list)
                      or acceptance.get("changed_path_count") != len(acceptance["changed_paths"])
                      or any(not isinstance(item, dict) or not isinstance(item.get("path"), str)
                             for item in acceptance["changed_paths"])):
                    raise WorkflowError("Saved target acceptance is missing its changes or baseline diff hash.")
                expected_previous = accepted

            acceptance = acceptance_history[-1]
            if (isinstance(legacy_acceptance, dict) and legacy_acceptance != acceptance):
                raise WorkflowError("Latest target acceptance does not match its compatibility record.")
            expected = acceptance["target_fingerprint"]
            current = self._fingerprint_repository(self.target)
            if (current.get("status") != "captured"
                    or current.get("commit_sha") != expected.get("commit_sha")
                    or current.get("working_tree_sha256") != expected.get("working_tree_sha256")):
                raise WorkflowError(
                    "Target changed after explicit current-target acceptance; inspect and accept the exact target again."
                )
            review_diff = _review_diff(self.state, self.target)
            source_hash = _ReviewCampaign._campaign_source_hash(review_diff)
            if source_hash != acceptance.get("baseline_diff_sha256"):
                raise WorkflowError(
                    "Baseline-to-current review diff changed after acceptance; inspect and accept the exact target again."
                )
            if self._target_change_inventory() != acceptance.get("changed_paths"):
                raise WorkflowError(
                    "Accepted changed-path inventory no longer matches the target; inspect and accept it again."
                )
            confirmed = self._fingerprint_repository(self.target)
            if (confirmed.get("status") != "captured"
                    or confirmed.get("commit_sha") != expected.get("commit_sha")
                    or confirmed.get("working_tree_sha256") != expected.get("working_tree_sha256")):
                raise WorkflowError("Target changed while checking review provenance; accept the exact target again.")
            return
        expected_stage = "correction" if purpose == "verification" else "implementation"
        operations = self.state.get("operation_history", [])
        operation = next((
            item for item in reversed(operations)
            if isinstance(item, dict) and item.get("operation_type") == "codex"
            and item.get("stage") == expected_stage
            and (item.get("status") == "completed" or (
                purpose == "initial"
                and item.get("resolution") == "human_accepted_current_target_for_independent_review"
            ))
        ), None) if isinstance(operations, list) else None
        if isinstance(operation, dict) and operation.get("resolution") == "human_accepted_current_target_for_independent_review":
            expected = operation.get("human_accepted_target_fingerprint")
        else:
            expected = operation.get("target_fingerprint_after") if isinstance(operation, dict) else None
        current = self._fingerprint_repository(self.target)
        if (not isinstance(expected, dict) or expected.get("status") != "captured"
                or current.get("status") != "captured"):
            raise WorkflowError("Could not capture the target fingerprint for review provenance.")
        if (expected.get("commit_sha") != current.get("commit_sha")
                or expected.get("working_tree_sha256") != current.get("working_tree_sha256")):
            raise WorkflowError(
                "Target changed outside the completed write operation before independent review; attribution is inconclusive.",
            )

    def _execution_change_summary(self, initial_target: dict | None) -> dict:
        if not isinstance(initial_target, dict) or initial_target.get("status") != "captured":
            return {
                "status": "unavailable",
                "reason": "The initial target fingerprint was not captured; change attribution is unavailable.",
            }
        try:
            inventory = self._target_change_inventory()
        except Exception as exc:
            return {
                "status": "unavailable",
                "error_category": type(exc).__name__,
                "reason": "The target change inventory could not be calculated.",
            }

        counts = {"added": 0, "modified": 0, "deleted": 0, "untracked": 0, "type_changed": 0}
        attributed_counts = {"preexisting_or_overlapping": 0, "changed_after_initial_capture": 0}
        examples: dict[str, list[dict[str, str]]] = {
            "preexisting_or_overlapping": [], "changed_after_initial_capture": [],
        }
        truncated = {key: False for key in examples}
        for item in inventory:
            change = item.get("change", "modified")
            counts[change] = counts.get(change, 0) + 1
            category = (
                "preexisting_or_overlapping" if item.get("preexisting") == "yes"
                else "changed_after_initial_capture"
            )
            attributed_counts[category] += 1
            path = item.get("path", "")
            encoded_path = path.encode("utf-8", "surrogateescape")
            if _workspace.fingerprint_path_is_sensitive(encoded_path):
                continue
            if len(examples[category]) >= MAX_EXECUTION_CHANGE_EXAMPLES:
                truncated[category] = True
                continue
            safe_path = encoded_path.decode("utf-8", "replace")
            safe_path = "".join(character if character.isprintable() else "?" for character in safe_path)
            safe_path = redact(safe_path).replace("`", "'")[:240]
            examples[category].append({"path": safe_path, "change": change})

        return {
            "status": "captured",
            "counts_by_change": counts,
            "counts_by_attribution": attributed_counts,
            "examples_by_attribution": examples,
            "examples_truncated": truncated,
            "attribution_limit": (
                "Paths present in the final baseline comparison but absent from the starting pre-existing "
                "inventory are not proof of agent authorship or when they changed; concurrent edits cannot be "
                "excluded. Transient changes absent from the final snapshot are not recorded. Paths already "
                "changed at initialization are classified as preexisting or overlapping."
            ),
        }

    def _capture_final_target_fingerprint(self) -> None:
        execution = self.state.setdefault("execution_fingerprint", {})
        if not isinstance(execution, dict):
            execution = {}
            self.state["execution_fingerprint"] = execution
        initial = execution.get("initial") if isinstance(execution.get("initial"), dict) else {}
        execution["version"] = _workspace.FINGERPRINT_VERSION
        execution["algorithm"] = _workspace.FINGERPRINT_ALGORITHM
        execution["excluded_internal_paths"] = _workspace.FINGERPRINT_EXCLUDED_INTERNAL_PATHS.copy()
        execution["excluded_sensitive_paths"] = "known sensitive path components and .env.* basenames"
        execution["final"] = {
            "captured_at": now(),
            "target": self._fingerprint_repository(self.target),
            "change_summary": self._execution_change_summary(initial.get("target")),
        }

    def _record_unvalidated_planning_output(
        self, output: str, error: WorkflowError, *, stage: str, proposal_version: int,
    ) -> None:
        """Keep bounded, redacted diagnostics without making output approval eligible."""
        safe_output = redact(output)
        operation = next((item for item in reversed(self.state.get("operation_history", []))
                          if item.get("status") == "completed" and item.get("operation_type") == "codex"
                          and item.get("stage") == stage and item.get("result_text") is not None), None)
        if operation is not None and stage in {"plan_refine_1", "plan_refine_2"}:
            operation["result_text"] = None
            operation["result_consumed"] = True
            operation["result_rejected_at"] = now()
        records = self.state.setdefault("planning_validation_failures", [])
        records.append({
            "validation_status": "failed_unvalidated",
            "approval_eligible": False,
            "stage": stage,
            "proposal_version": proposal_version,
            "attempted_at": now(),
            "validation_error": redact(str(error)),
            "generated_output": safe_output[:MAX_PLANNING_FAILURE_OUTPUT_CHARS],
            "generated_output_characters": len(safe_output),
            "generated_output_sha256": hashlib.sha256(safe_output.encode("utf-8")).hexdigest(),
            "generated_output_truncated": len(safe_output) > MAX_PLANNING_FAILURE_OUTPUT_CHARS,
            "secrets_redacted": safe_output != output,
        })
        if len(records) > MAX_PLANNING_FAILURE_RECORDS:
            del records[:-MAX_PLANNING_FAILURE_RECORDS]
            self.state["planning_validation_failure_history_truncated"] = True
        self.workflow_state.save()

    def _read_proposal(self) -> str:
        _safe_artifact(self.proposal_path)
        try:
            proposal = _persistence.read_text(self.proposal_path)
        except OSError as exc:
            raise WorkflowError(f"Saved proposal is unavailable: {self.proposal_path}") from exc
        if not proposal.strip():
            raise WorkflowError("Saved proposal is empty.")
        return clean_markdown(proposal)

    def _write_proposal(self, proposal: str, *, validated_ledger: dict | None = None) -> None:
        if self.state.get("traceability_required") is True and validated_ledger is None:
            self._validate_proposal_ledger(proposal)
        _atomic_text(self.proposal_path, proposal)
        content = _persistence.read_bytes(self.proposal_path)
        digest = hashlib.sha256(content).hexdigest()
        version = str(self.state.get("proposal_version", 1))
        self.state.setdefault("proposal_version_hashes", {})[version] = digest
        self.state["proposal_artifact_sha256"] = digest

    def _validate_proposal_ledger(self, proposal: str) -> dict | None:
        if self.state.get("traceability_required") is not True:
            return None
        try:
            return _traceability.parse_commitment_ledger(proposal)
        except _traceability.TraceabilityError as exc:
            raise WorkflowError(
                f"Proposal must preserve a valid commitment ledger before review or approval: {exc}"
            ) from exc

    def _record_review_snapshot(self, campaign: dict, review_diff: ReviewDiff, *, purpose: str) -> dict:
        current_review_diff = _review_diff(self.state, self.target)
        fingerprint = self._fingerprint_repository(self.target)
        proposal_bytes = _persistence.read_bytes(self.proposal_path)
        proposal_text = self._read_proposal()
        try:
            record, binding_changed = _record_review_snapshot(
                self.state, campaign, review_diff, current_review_diff, fingerprint,
                proposal_bytes, proposal_text, clean_markdown(self.state.get("proposal", "")),
                purpose=purpose,
            )
        except _ReviewCampaignError as exc:
            raise WorkflowError(str(exc)) from exc
        if binding_changed:
            self.workflow_state.save()
        return record

    def _verify_delivery_integrity(self) -> dict:
        return _delivery_verify_integrity(
            self.state,
            target=self.target,
            proposal_path=self.proposal_path,
        )

    def _guidance(self) -> dict:
        return self.state.get("project_guidance") or _project_guidance(self.target, self.state["task"])

    def _claude_skill_content_authorized(self) -> bool:
        permitted = self.state.get("authorization", {}).get("claude_aily", {}).get("permitted_data", [])
        return isinstance(permitted, list) and "relevant_skill_instructions" in permitted

    def _trace_span(self, name: str, metadata: dict, *, as_type: str = "span"):
        tracer = self._tracer or NoOpTracer()
        details = dict(metadata)
        review_operations = {
            "review.campaign": "review_campaign",
            "review.part.provider_call": "review_part",
            "review.integration.provider_call": "review_integration",
        }
        if name in review_operations:
            details.setdefault("operation", review_operations[name])
        if isinstance(self.state, MutableMapping):
            stage = self.state.get("stage")
            if isinstance(stage, str) and stage in TRACE_STAGES:
                details.setdefault("stage", stage)
        workflow_id = getattr(self, "workflow_id", None)
        if isinstance(workflow_id, str):
            details.setdefault("session_id", safe_workflow_reference(workflow_id))
        factory = getattr(tracer, "generation", tracer.span) if as_type == "generation" else tracer.span
        return isolated_context(lambda: factory(name=name, metadata=details))

    def _run_codex(self, prompt: str, *, sandbox: str) -> str:
        metadata = {"provider": "codex", "operation": "codex_execution", "sandbox": sandbox}
        if isinstance(self.state.get("stage"), str):
            metadata["stage"] = self.state["stage"]
        if self.model:
            metadata["model"] = self.model
        trace_context = self._trace_span("provider.codex", metadata, as_type="generation")
        previous_trace_context = getattr(self, "_active_codex_trace_context", None)
        previous_operation_id = getattr(self, "_active_codex_operation_id", None)
        previous_persisted_status = getattr(self, "_active_codex_persisted_status", None)
        self._active_codex_trace_context = trace_context
        self._active_codex_operation_id = None
        self._active_codex_persisted_status = None
        try:
            try:
                with trace_context:
                    result = self._run_codex_impl(prompt, sandbox=sandbox)
            except BaseException as exc:
                operation = self.workflow_state.operation(self._active_codex_operation_id)
                if operation is not None:
                    persisted_status = self._active_codex_persisted_status
                    if persisted_status in {"completed", "failed", "interrupted"}:
                        trace_context.update(**_codex_langfuse_metadata(operation))
                    else:
                        trace_context.update(**_codex_langfuse_metadata(
                            operation,
                            status="unknown",
                            finished_at=now(),
                        ))
                        trace_context.update(error_category=type(exc).__name__)
                raise
            operation = self.workflow_state.operation(self._active_codex_operation_id)
            if operation is not None and self._active_codex_persisted_status is not None:
                trace_context.update(**_codex_langfuse_metadata(operation))
            return result
        finally:
            self._active_codex_trace_context = previous_trace_context
            self._active_codex_operation_id = previous_operation_id
            self._active_codex_persisted_status = previous_persisted_status

    def _codex_cli_version(self) -> str | None:
        """Read the configured CLI version without making a provider call; optional metadata only."""
        if self._codex_cli_version_checked:
            return self._codex_cli_version_cache
        self._codex_cli_version_checked = True
        if self.process_runner is not subprocess.run:
            return None
        try:
            with tempfile.TemporaryDirectory(prefix="harness-codex-version-") as isolated_config:
                result = subprocess.run(
                    [self.codex, "--version"],
                    cwd=str(self.target),
                    env=_workspace.provider_env(isolated_config),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if result.returncode != 0:
            return None
        version_bytes, _source = _output_bytes(result.stdout)
        if version_bytes is None:
            return None
        version = version_bytes.decode("utf-8", "replace").strip().splitlines()
        if not version or len(version[0]) > 96:
            return None
        if re.fullmatch(r"(?:codex-cli|codex)\s+v?[0-9][A-Za-z0-9.+-]*", version[0], re.IGNORECASE):
            self._codex_cli_version_cache = version[0]
        return self._codex_cli_version_cache

    def _run_process_with_pid(self, command: list[str], *, operation_id: str, **kwargs):
        return self.codex_executor.run(
            command,
            operation_id=operation_id,
            mark_launching=self.workflow_state.operation_launching,
            process_started=self.workflow_state.operation_process_started,
            **kwargs,
        )

    def _run_codex_impl(self, prompt: str, *, sandbox: str) -> str:
        with tempfile.TemporaryDirectory(prefix="harness-codex-result-") as output_dir:
            output_path = Path(output_dir) / "last-message.md"
            authorization = self.state["authorization"]
            authorization_context = (
                "Persistent workflow authorization is already active for target "
                f"{authorization['target']}. It covers: {', '.join(authorization['workspace_operations'])}. "
                "Do not request individual approval for those operations. It does not cover: "
                f"{', '.join(authorization['restricted_operations'])}. Do not perform those actions without separate "
                "explicit user authorization. The sandbox for this stage still applies; planning is read-only.\n\n"
            )
            stage = self.state.get("stage")
            task = self.state.get("task", "")
            read_only_authorized_urls = (
                _planning_read_only_authorized_urls(str(stage or ""), str(task))
                if sandbox == "read-only" else []
            )
            disabled_user_mcp_servers: list[str] = []
            if sandbox == "read-only":
                _assert_no_project_mcp_servers(self.target)
                disabled_user_mcp_servers = _configured_codex_mcp_servers()
            if read_only_authorized_urls:
                prompt = f"{READ_ONLY_WEB_ACCESS_INSTRUCTION}\n\n{prompt}"
            command = build_codex_command(
                self.codex, self.target, sandbox, output_path, self.model,
                read_only_authorized_urls=read_only_authorized_urls,
                disabled_user_mcp_servers=disabled_user_mcp_servers,
            )
            effective_prompt = authorization_context + prompt
            effective_prompt_bytes = effective_prompt.encode("utf-8")
            prompt_hash = hashlib.sha256(effective_prompt_bytes).hexdigest()
            prior_operations = [
                item for item in self.state.get("operation_history", [])
                if item.get("operation_type") == "codex" and item.get("stage") == stage
                and item.get("sandbox") == sandbox and not item.get("result_consumed")
            ]
            prior = prior_operations[-1] if prior_operations else None
            if prior is not None and prior.get("status") == "completed":
                if (prior.get("input_sha256") != prompt_hash
                        or not isinstance(prior.get("result_text"), str)):
                    self.recovery_required(
                        prior,
                        "A completed Codex result does not match the current stage input or was not persisted completely.",
                    )
                    raise WorkflowError("Codex result recovery is ambiguous; inspect the workflow status.")
                return prior["result_text"]

            write_start_fingerprint = (
                self._write_operation_start_fingerprint(str(stage))
                if sandbox == "workspace-write" and isinstance(stage, str) else None
            )

            usage = self.workflow_state.resource_usage()
            limits = self.workflow_state.execution_limits()
            if usage["codex_calls"] >= limits["max_codex_calls"]:
                raise WorkflowError("The authorized Codex invocation limit is exhausted.")
            call_timeout = self.workflow_state.provider_timeout("codex")

            cli_version = self._codex_cli_version()
            operation = self.workflow_state.new_operation(
                "codex",
                input_sha256=prompt_hash,
                sandbox=sandbox,
                model=self.model,
                model_source="workflow_configuration" if self.model is not None else "not_reported",
                reported_model=None,
                cli_version=cli_version,
                cli_version_source="codex --version" if cli_version is not None else "unavailable",
                input_characters=len(effective_prompt),
                input_bytes=len(effective_prompt_bytes),
                timeout_seconds=call_timeout,
                attempt=1 + sum(
                    item.get("operation_type") == "codex" and item.get("stage") == stage
                    for item in self.state.get("operation_history", [])
                ),
            )
            if write_start_fingerprint is not None:
                operation["target_fingerprint_before"] = write_start_fingerprint
            self._active_codex_operation_id = operation["operation_id"]
            self.workflow_state.save()
            operation["invocation_at"] = now()
            self.workflow_state.save()
            self._update_active_codex_trace(operation)
            invocation_started = time.monotonic()
            try:
                result = self._run_process_with_pid(
                    command,
                    operation_id=operation["operation_id"],
                    cwd=str(self.target),
                    input=effective_prompt_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=call_timeout,
                    check=False,
                    env=_workspace.provider_env(Path(output_dir) / "isolated-github-config"),
                )
            except subprocess.TimeoutExpired as exc:
                invocation_finished = time.monotonic()
                partial_stdout, stdout_representation = _output_bytes(exc.output)
                partial_stderr, stderr_representation = _output_bytes(exc.stderr)
                stream_evidence = _codex_stream_evidence(
                    partial_stdout, partial_stderr,
                    stdout_status="partial" if partial_stdout is not None else "unavailable",
                    stderr_status="partial" if partial_stderr is not None else "unavailable",
                    stdout_truncated=True if partial_stdout is not None else None,
                    stderr_truncated=True if partial_stderr is not None else None,
                )
                stream_evidence.update({
                    "stdout_capture_representation": stdout_representation,
                    "stderr_capture_representation": stderr_representation,
                    "duration_ms": max(0, round((invocation_finished - invocation_started) * 1000)),
                })
                self._finish_operation(
                    operation["operation_id"], status="interrupted",
                    outcome=f"Codex timed out after {call_timeout:g} seconds; target changes require inspection.",
                    error_category=type(exc).__name__, evidence=stream_evidence,
                )
                raise WorkflowError(f"Codex timed out after {call_timeout:g} seconds.") from exc
            except KeyboardInterrupt:
                invocation_finished = time.monotonic()
                not_launched = operation.get("launch_state") == "not_started"
                stream_evidence = _codex_stream_evidence(
                    None, None,
                    stdout_status="not_started" if not_launched else "unavailable",
                    stderr_status="not_started" if not_launched else "unavailable",
                    stdout_truncated=False if not_launched else None,
                    stderr_truncated=False if not_launched else None,
                )
                stream_evidence["duration_ms"] = max(0, round((invocation_finished - invocation_started) * 1000))
                self._finish_operation(
                    operation["operation_id"], status="failed" if not_launched else "interrupted",
                    outcome=(
                        "Codex invocation was cancelled before launch; it is safe to retry."
                        if not_launched else "Codex invocation was cancelled; target changes require inspection."
                    ),
                    error_category="CancelledBeforeLaunch" if not_launched else "Cancelled",
                    launch_state="not_started" if not_launched else None,
                    evidence=stream_evidence,
                )
                if not not_launched:
                    self.recovery_required(
                        operation,
                        "Codex was cancelled while running; inspect target changes before retrying.",
                    )
                raise
            except OSError as exc:
                invocation_finished = time.monotonic()
                stream_evidence = _codex_stream_evidence(
                    None, None, stdout_status="not_started", stderr_status="not_started",
                    stdout_truncated=False, stderr_truncated=False,
                )
                stream_evidence["duration_ms"] = max(0, round((invocation_finished - invocation_started) * 1000))
                self._finish_operation(
                    operation["operation_id"], status="failed",
                    outcome=f"Codex could not start: {redact(str(exc))}",
                    error_category=type(exc).__name__, launch_state="not_started", evidence=stream_evidence,
                )
                raise WorkflowError(f"Codex could not start: {redact(str(exc))}") from exc
            invocation_finished = time.monotonic()
            if sandbox == "workspace-write":
                operation["target_fingerprint_after"] = self._fingerprint_repository(self.target)
            stdout_raw, stdout_representation = _output_bytes(result.stdout)
            stderr_raw, stderr_representation = _output_bytes(result.stderr)
            stdout = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else str(result.stdout or "")
            stderr = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else str(result.stderr or "")
            stdout_status = "unavailable" if stdout_raw is None else "empty" if not stdout_raw else "complete"
            stderr_status = "unavailable" if stderr_raw is None else "empty" if not stderr_raw else "complete"
            stream_evidence = _codex_stream_evidence(
                stdout_raw, stderr_raw,
                stdout_status=stdout_status, stderr_status=stderr_status,
                stdout_truncated=False if stdout_raw is not None else None,
                stderr_truncated=False if stderr_raw is not None else None,
            )
            stream_evidence.update({
                "stdout_capture_representation": stdout_representation,
                "stderr_capture_representation": stderr_representation,
                "duration_ms": max(0, round((invocation_finished - invocation_started) * 1000)),
            })
            if output_path.exists():
                last_message_raw = output_path.read_bytes()
                stream_evidence.update({
                    "last_message_file_sha256": hashlib.sha256(last_message_raw).hexdigest(),
                    "last_message_file_bytes": len(last_message_raw),
                    "last_message_file_status": "empty" if not last_message_raw else "captured",
                })
            else:
                last_message_raw = None
                stream_evidence.update({
                    "last_message_file_sha256": None,
                    "last_message_file_bytes": None,
                    "last_message_file_status": "missing",
                })
            if result.returncode:
                detail = redact(stderr.strip() or _json_line_message(stdout) or "Codex returned no diagnostic output.")
                self._finish_operation(
                    operation["operation_id"], status="failed",
                    outcome=f"Codex exited with status {result.returncode}: {detail}",
                    exit_code=result.returncode, error_category="CodexExitError", evidence=stream_evidence,
                )
                raise WorkflowError(f"Codex exited with status {result.returncode}: {detail}")
            if stream_evidence.get("codex_cli_turn_status") == "failed":
                self._finish_operation(
                    operation["operation_id"], status="failed",
                    outcome="Codex JSONL reported a failed turn.",
                    exit_code=result.returncode, error_category="CodexReportedTurnFailure", evidence=stream_evidence,
                )
                raise WorkflowError("Codex JSONL reported a failed turn.")
            if last_message_raw is not None:
                message = last_message_raw.decode("utf-8").strip()
            else:
                # --json is always enabled. Only extract an explicit final-message event;
                # never persist the raw event stream as a recovery result.
                message = _json_line_message(stdout)
            if not message:
                self._finish_operation(
                    operation["operation_id"], status="failed", outcome="Codex returned an empty response.",
                    exit_code=result.returncode, error_category="EmptyCodexResponse", evidence=stream_evidence,
                )
                raise WorkflowError("Codex returned an empty response.")
            stream_evidence.update({
                "result_text_characters": len(message),
                "result_text_bytes": len(message.encode("utf-8")),
            })
            if stream_evidence.get("reported_model") is not None:
                if operation.get("model") is None:
                    operation["model"] = stream_evidence["reported_model"]
                    operation["model_source"] = "codex_jsonl_event"
            self._finish_operation(
                operation["operation_id"], status="completed", outcome="Codex returned a response.",
                exit_code=result.returncode, result_text=message,
                result_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(), launch_state="completed",
                evidence=stream_evidence,
            )
            return message

    def _call_claude(
        self,
        header: str,
        stage: str,
        sections: dict[str, str],
        *,
        metadata: dict | None = None,
    ) -> tuple[str, dict]:
        operation_metadata = {
            "provider": "claude",
            "operation": "plan_review" if stage.startswith("plan_review_") else "code_review",
            "stage": stage,
        }
        if stage.startswith("plan_review_"):
            operation_metadata["iteration"] = int(stage[-1])
        if stage == "code_review" and isinstance(metadata, dict):
            retry_count = metadata.get("retry_count")
            if isinstance(retry_count, int) and not isinstance(retry_count, bool) and retry_count >= 0:
                operation_metadata["retry_count"] = retry_count
            if metadata.get("review_kind") == "integration":
                operation_metadata["review_kind"] = "integration"
            else:
                operation_metadata["review_kind"] = "part"
                match = re.fullmatch(r"part-(\d+)", str(metadata.get("part_id", "")))
                if match:
                    operation_metadata["review_part_index"] = int(match.group(1))
        with self._trace_span("provider.claude_review", operation_metadata, as_type="generation") as span:
            try:
                report, manifest = self._call_claude_impl(header, stage, sections, metadata=metadata)
            except Exception as exc:
                cost = getattr(exc, "cost_usd", None)
                if cost is None:
                    cost = getattr(getattr(exc, "__cause__", None), "cost_usd", None)
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
                    span.update(cost_usd=float(cost))
                raise
            cost = manifest.get("cost_usd") if isinstance(manifest, dict) else None
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
                span.update(cost_usd=float(cost))
            attempt = manifest.get("campaign_call_number") if isinstance(manifest, dict) else None
            if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
                span.update(attempt=attempt)
            return report, manifest

    def _call_claude_impl(
        self,
        header: str,
        stage: str,
        sections: dict[str, str],
        *,
        metadata: dict | None = None,
    ) -> tuple[str, dict]:
        if not self.workflow_state.lock_held:
            raise WorkflowError("Claude review requires the workflow process lock.")
        authorization = validate_workflow_authorization(self.state, self.target, self.workflow_id)
        external = authorization["claude_aily"]
        if external["budget_unknown"]:
            raise WorkflowError("Claude review stopped because a previous call has no recorded cost.")
        if external["permitted_data"] not in (
            _authorization.LEGACY_CLAUDE_DATA_SCOPE, CLAUDE_DATA_SCOPE,
        ):
            raise WorkflowError("This Claude/Aily request is outside the workflow's persisted data authorization.")
        if stage.startswith("plan_review_") and len(_planning_review.valid_plan_reviews(self.state)) >= external["max_plan_reviews"]:
            raise WorkflowError("The authorized Claude plan-review limit is exhausted.")
        code_review_call_number = None
        review_purpose = None
        if stage == "code_review":
            current_campaign = self.state.get("review_campaign")
            if (isinstance(current_campaign, dict)
                    and current_campaign.get("campaign_id") == (metadata or {}).get("campaign_id")):
                review_purpose = current_campaign.get("purpose", "initial")
            code_review_calls = [
                context for context in self.state.get("claude_contexts", [])
                if context.get("stage") == "code_review" and context.get("call_state") != "validation_failed"
            ]
            if len(code_review_calls) >= external["max_code_reviews"]:
                raise WorkflowError(
                    "The authorized Claude code-review campaign call limit is exhausted "
                    f"({external['max_code_reviews']} calls)."
                )
            code_review_call_number = len(code_review_calls) + 1

        code_required = {
            "Review metadata", "Assignment", "Approved proposal", "Implementation summary",
            "Repository context", "Relevant skills", "Independent reviewer role",
            "Repository/module map and dependencies", "Engineering Judgment & Proportional Design",
            "Review file coverage",
        }
        if stage == "code_review":
            code_required.add(
                "Individual part reports" if (metadata or {}).get("review_kind") == "integration"
                else "Complete patch"
            )
            if review_purpose == "verification":
                code_required.add("Post-correction verification")
                code_required.add("Correction and validation summary (unverified agent claim)")

        required_sections = {
            "plan_review": {
                "Review metadata", "Assignment", "Current proposal", "Repository context",
                "Workflow constraints", "Engineering Judgment & Proportional Design", "Relevant skills",
                "Review duties", "Independence boundary",
            },
        }["plan_review"] if stage != "code_review" else code_required
        safe_header = redact(header)
        safe_sections = {
            name: redact(value) if name == "Complete patch" else redact(value).strip()
            for name, value in sections.items()
        }
        prompt = _format_context_prompt(safe_header, safe_sections)
        missing = sorted(name for name in required_sections if not safe_sections.get(name, "").strip())
        validation_errors: list[str] = []
        if missing:
            validation_errors.append(
                "Claude review context is incomplete; missing mandatory sections: " + ", ".join(missing) + "."
            )
        if stage.startswith("plan_review_") and not all(
                isinstance((metadata or {}).get(key), int) for key in ("iteration", "proposal_version")):
            validation_errors.append("Claude plan-review metadata must include the review iteration and proposal version.")
        if stage == "code_review" and not isinstance((metadata or {}).get("proposal_version"), int):
            validation_errors.append("Claude code-review metadata must include the proposal version.")
        for name in required_sections:
            value = safe_sections.get(name)
            block = f"## {name}\n{value}".rstrip() if value is not None else ""
            if not block or block not in prompt:
                validation_errors.append(f"Claude review context section {name!r} is not present in the payload.")

        try:
            configured_payload_limit = claude_reviewer.max_review_payload_chars()
            requested_payload_limit = (metadata or {}).get("payload_limit_chars")
            if requested_payload_limit is not None and (
                    isinstance(requested_payload_limit, bool) or not isinstance(requested_payload_limit, int)
                    or requested_payload_limit < 1):
                raise claude_reviewer.ReviewError("Saved code-review payload limit is invalid.")
            if requested_payload_limit is not None and configured_payload_limit < requested_payload_limit:
                raise claude_reviewer.ReviewError(
                    f"Current {CLAUDE_MAX_PAYLOAD_ENV} limit ({configured_payload_limit}) is below the "
                    f"saved review campaign limit ({requested_payload_limit}); no paid call was made."
                )
            payload_limit = requested_payload_limit or configured_payload_limit
        except claude_reviewer.ReviewError as exc:
            payload_limit = None
            validation_errors.append(str(exc))
        if payload_limit is not None:
            try:
                claude_reviewer.validate_review_invocation(prompt, self.aily, limit=payload_limit)
            except claude_reviewer.ReviewError as exc:
                validation_errors.append(str(exc))

        prompt_bytes: bytes | None
        try:
            prompt_bytes = prompt.encode("utf-8")
        except UnicodeEncodeError:
            prompt_bytes = None

        included_files = list((metadata or {}).get("included_files", []))
        excluded_files = list((metadata or {}).get("excluded_files", []))
        section_manifest = [
            {
                "name": name,
                "characters": len(value),
                "sha256": _sha256_text(value),
            }
            for name, value in safe_sections.items()
        ]

        def context_manifest(call_state: str, error: str | None = None) -> dict:
            manifest = {
                "stage": stage,
                "iteration": (metadata or {}).get("iteration"),
                "proposal_version": (metadata or {}).get("proposal_version"),
                "header_id": (metadata or {}).get("header_id", "claude-review-v1"),
                "header_characters": len(safe_header),
                "header_sha256": _sha256_text(safe_header),
                "characters": len(prompt),
                "utf8_bytes": len(prompt_bytes) if prompt_bytes is not None else None,
                "payload_limit_characters": payload_limit,
                "payload_limit_source": "environment" if CLAUDE_MAX_PAYLOAD_ENV in os.environ else "default",
                "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest() if prompt_bytes is not None else None,
                "complete": not missing and not validation_errors,
                "sections": section_manifest,
                "included_files": included_files,
                "excluded_files": excluded_files,
                "call_state": call_state,
            }
            for key in (
                "campaign_id", "review_kind", "part_id", "shared_context_sha256", "part_context_sha256",
                "part_patch_sha256",
            ):
                if (metadata or {}).get(key) is not None:
                    manifest[key] = (metadata or {})[key]
            if review_purpose is not None:
                manifest["review_purpose"] = review_purpose
            if code_review_call_number is not None:
                manifest["campaign_call_number"] = code_review_call_number
            if error is not None:
                manifest["error"] = redact(error)
            return manifest

        if validation_errors:
            detail = " ".join(validation_errors)
            contexts = self.state.setdefault("claude_contexts", [])
            contexts.append(context_manifest("validation_failed", detail))
            self.state["error"] = detail
            self.workflow_state.save()
            raise WorkflowError(detail + " No paid call was made.")

        assert payload_limit is not None and prompt_bytes is not None
        print(
            f"Claude {stage} context: {len(prompt)} characters ({len(prompt_bytes)} UTF-8 bytes); "
            f"configured limit {payload_limit} characters.",
            file=sys.stderr,
        )

        with localcontext() as context:
            context.prec = 1000
            remaining_amount = Decimal(str(external["budget_usd"])) - Decimal(str(external["spent_usd"]))
            remaining = float(remaining_amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        if remaining < 0.01:
            raise WorkflowError("The authorized Claude budget is exhausted.")
        usage = self.workflow_state.resource_usage()
        limits = self.workflow_state.execution_limits()
        if usage["claude_calls"] >= limits["max_claude_calls"]:
            raise WorkflowError("The authorized total Claude/Aily invocation limit is exhausted.")
        call_timeout = self.workflow_state.provider_timeout("claude")

        manifest = context_manifest("submitted")
        manifest["submitted_at"] = now()
        contexts = self.state.setdefault("claude_contexts", [])
        context_index = len(contexts)
        operation = self.workflow_state.new_operation(
            "claude",
            input_sha256=manifest["prompt_sha256"],
            cap_usd=remaining,
            campaign_id=(metadata or {}).get("campaign_id"),
            part_id=(metadata or {}).get("part_id"),
            iteration=(metadata or {}).get("iteration"),
            proposal_version=(metadata or {}).get("proposal_version"),
            context_index=context_index,
            timeout_seconds=call_timeout,
            review_purpose=review_purpose,
        )
        manifest["operation_id"] = operation["operation_id"]
        operation["call_number"] = code_review_call_number
        contexts.append(manifest)
        external["active_call"] = {
            "stage": stage,
            "started_at": now(),
            "cap_usd": remaining,
            "context_sha256": manifest["prompt_sha256"],
            "operation_id": operation["operation_id"],
        }
        self.workflow_state.save()
        self.workflow_state.operation_launching(operation["operation_id"])
        invocation_started = time.monotonic()
        try:
            runner_kwargs = {"budget_usd": remaining, "executable": self.aily}
            if self.claude_runner is claude_reviewer.review:
                runner_kwargs["timeout_seconds"] = call_timeout
                runner_kwargs["process_started"] = lambda process_id: self.workflow_state.operation_process_started(
                    operation["operation_id"], process_id,
                )
            result = self.claude_runner(prompt, **runner_kwargs)
        except claude_reviewer.ReviewError as exc:
            duration_ms = max(0, round((time.monotonic() - invocation_started) * 1000))
            if exc.cost_usd is None:
                external["budget_unknown"] = True
            else:
                external["spent_usd"] = round(external["spent_usd"] + exc.cost_usd, 4)
            external["active_call"] = None
            self.state["error"] = str(exc)
            contexts[context_index]["call_state"] = "failed"
            contexts[context_index]["error"] = redact(str(exc))
            operation_status = "unknown" if exc.cost_usd is None else "failed"
            self._finish_operation(
                operation["operation_id"], status=operation_status,
                outcome=f"Claude invocation failed: {redact(str(exc))}",
                error_category=type(exc).__name__, cost_usd=exc.cost_usd, duration_ms=duration_ms,
            )
            raise WorkflowError(str(exc)) from exc
        except KeyboardInterrupt:
            duration_ms = max(0, round((time.monotonic() - invocation_started) * 1000))
            external["budget_unknown"] = True
            external["active_call"] = None
            error = "Claude/Aily invocation was cancelled; provider outcome and final cost are unknown."
            self.state["error"] = error
            contexts[context_index]["call_state"] = "interrupted_unknown_cost"
            contexts[context_index]["error"] = error
            self._finish_operation(
                operation["operation_id"], status="interrupted", outcome=error,
                error_category="Cancelled", duration_ms=duration_ms,
            )
            self.recovery_required(operation, error)
            raise
        except Exception as exc:
            duration_ms = max(0, round((time.monotonic() - invocation_started) * 1000))
            external["budget_unknown"] = True
            external["active_call"] = None
            error = f"Claude invocation failed without recorded cost: {redact(str(exc))}"
            self.state["error"] = error
            contexts[context_index]["call_state"] = "failed_unknown_cost"
            contexts[context_index]["error"] = error
            self._finish_operation(
                operation["operation_id"], status="unknown", outcome=error,
                error_category=type(exc).__name__, duration_ms=duration_ms,
            )
            raise WorkflowError(error) from exc
        cost = result.cost_usd
        if cost is not None:
            external["spent_usd"] = round(external["spent_usd"] + cost, 4)
        else:
            external["budget_unknown"] = True
        external["active_call"] = None
        contexts[context_index]["call_state"] = "completed"
        contexts[context_index]["cost_usd"] = cost
        contexts[context_index]["report"] = result.text
        duration_ms = max(0, round((time.monotonic() - invocation_started) * 1000))
        contexts[context_index]["duration_ms"] = duration_ms
        self._finish_operation(
            operation["operation_id"], status="completed", outcome="Claude returned a report.",
            result_sha256=hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
            cost_usd=cost, launch_state="completed", duration_ms=duration_ms,
        )
        return result.text, manifest

    def _code_review_shared_context(self, review_diff: ReviewDiff, *, purpose: str = "initial") -> dict[str, str]:
        guidance = self._guidance()
        campaign = self._review_campaign()
        return campaign.build_shared_context(
            review_diff,
            repository_context=_planning_review.repository_context_for_claude(
                guidance, list(review_diff.included_paths),
            ),
            relevant_skills=_guidance_for_claude(
                guidance,
                stage="code_review",
                context="\n".join((
                    self.state.get("task", ""), self.state.get("proposal", ""),
                    self.state.get("implementation_summary", ""), *review_diff.included_paths,
                )),
                include_skill_content=self._claude_skill_content_authorized(),
            ),
            purpose=purpose,
        )

    def _review_campaign(self) -> _ReviewCampaign:
        authorization = self.state.get("authorization", {}).get("claude_aily", {})
        return _ReviewCampaign(
            self.state,
            save_state=self._save,
            request_review=self._call_claude,
            authorized_call_limit=authorization.get("max_code_reviews"),
            authorize_call_count=self._review_call_count_authorized,
            trace_span=self._trace_span,
        )

    def _review_call_count_authorized(self, required_calls: int) -> bool:
        limit = self.state["authorization"]["claude_aily"]["max_code_reviews"]
        used = sum(
            isinstance(context, dict) and context.get("stage") == "code_review"
            and context.get("call_state") != "validation_failed"
            for context in self.state.get("claude_contexts", [])
        )
        campaign = self.state.get("review_campaign")
        campaign_id = campaign.get("campaign_id") if isinstance(campaign, dict) else None
        current_campaign_calls = sum(
            isinstance(context, dict) and context.get("stage") == "code_review"
            and context.get("campaign_id") == campaign_id
            and context.get("call_state") != "validation_failed"
            for context in self.state.get("claude_contexts", [])
        )
        required_remaining = max(0, required_calls - current_campaign_calls)
        return used + required_remaining <= limit

    @staticmethod
    def _campaign_helper(call):
        try:
            return call()
        except _ReviewCampaignError as exc:
            raise WorkflowError(str(exc)) from exc

    def _prepare_review_campaign(
        self, review_diff: ReviewDiff, *, purpose: str = "initial",
    ) -> tuple[dict, dict[str, str], list[dict]]:
        shared = self._code_review_shared_context(review_diff, purpose=purpose)
        campaign, shared, parts = self._campaign_helper(
            lambda: self._review_campaign()._prepare_review_campaign(review_diff, shared),
        )
        saved_purpose = campaign.get("purpose")
        if saved_purpose is not None and saved_purpose != purpose:
            raise WorkflowError("Saved review campaign purpose changed during recovery.")
        campaign["purpose"] = purpose
        self.workflow_state.save()
        return campaign, shared, parts

    def _run_review_campaign(
        self, campaign: dict, shared: dict[str, str], parts: list[dict], review_diff: ReviewDiff,
    ) -> str:
        if campaign.get("purpose") == "initial" and self.state.get("review_target_acceptances"):
            # Recheck at the last local boundary before any provider call. The target
            # may have changed while the review context was being partitioned.
            self._require_write_operation_target("initial")
            acceptance = self.state["review_target_acceptances"][-1]
            source_hash = _ReviewCampaign._campaign_source_hash(review_diff)
            if (source_hash != acceptance.get("baseline_diff_sha256")
                    or campaign.get("source_patch_sha256") != source_hash):
                raise WorkflowError(
                    "Prepared Claude review context does not match the explicitly accepted baseline diff."
                )
            review_header = (
                _ReviewCampaign._legacy_code_review_header()
                if campaign.get("header_id") == "code-review-part-v1"
                else _ReviewCampaign._code_review_header()
            )
            shared_hash = _ReviewCampaign._context_sha256(review_header, shared)
            if shared_hash != campaign.get("shared_context_sha256"):
                raise WorkflowError("Prepared Claude shared context differs from the saved accepted review campaign.")
            if (campaign.get("header_id") == "code-review-part-v3"
                    and _ReviewCampaign._context_sha256(
                        _ReviewCampaign._code_review_header(),
                        _ReviewCampaign._part_review_context(shared),
                    ) != campaign.get("part_context_sha256")):
                raise WorkflowError("Prepared Claude part context differs from the saved accepted review campaign.")
            if _ReviewCampaign._campaign_part_manifest(parts) != campaign.get("parts"):
                raise WorkflowError("Prepared Claude patch parts differ from the accepted baseline review diff.")
        with self._trace_span("review.campaign", {"review_part_count": len(parts)}):
            return self._campaign_helper(lambda: self._review_campaign()._run_review_campaign(
                campaign, shared, parts, review_diff,
            ))

    def _execute_stage(self, stage: str) -> StageOutcome:
        if self._stage_execution_active:
            raise WorkflowError("Workflow stages cannot be nested.")
        self._stage_execution_active = True
        self._pending_stage_result = None
        try:
            self._dispatch_stage(stage)
            if self._pending_stage_result is None:
                raise WorkflowError(f"Workflow stage {stage} did not return a result.")
            return StageOutcome(self._pending_stage_result)
        finally:
            self._pending_stage_result = None
            self._stage_execution_active = False

    def _stage_result(self, result: str) -> None:
        if not self._stage_execution_active:
            raise WorkflowError("Stage results can only be returned while executing a stage.")
        if self._pending_stage_result is not None:
            raise WorkflowError("A workflow stage can return only one result.")
        self._pending_stage_result = StageOutcome(result).result

    def _dispatch_stage(self, stage: str) -> None:
        if stage == "planning":
            self._stage_planning(stage)
        elif stage in {"plan_review_1", "plan_review_2"}:
            self._stage_plan_reviews(stage)
        elif stage in {"plan_refine_1", "plan_refine_2"}:
            self._stage_plan_refinement(stage)
        elif stage == "human_revision":
            self._stage_human_revision(stage)
        elif stage == "implementation":
            self._stage_implementation(stage)
        elif stage in {"code_review", "verification"}:
            self._stage_code_review_or_verification(stage)
        elif stage == "correction":
            self._stage_correction(stage)
        elif stage == "complete":
            self._stage_complete(stage)
        else:
            raise WorkflowError(f"Unknown workflow stage: {stage}")
    def _stage_planning(self, stage: str) -> None:
        guidance = self._guidance()
        skill_guidance = _guidance_for_codex(
            guidance, stage=stage, context=self.state["task"],
        )
        prompt = (
            "Understand the assignment and this repository. Use the applicable instruction and skill "
            "content supplied below; inspect relevant target files. Do not merely list skills: apply "
            "relevant guidance and record each selected skill and its use. If none apply, say so. "
            "Make reasonable technical decisions, identify requirements, edge cases, and verification. "
            "Apply Engineering Judgment & Proportional Design. Separate must implement, should implement, "
            "future production considerations, and out of scope. Do not invent product behavior. "
            "Return a concise, readable Markdown implementation proposal with scope, approach, risks, "
            "a requirements-to-design mapping, material architectural decisions and their trade-offs, "
            "relevant skills and how they were applied, and concrete validation commands. Include the "
            "approved-design commitment classifications and the required machine-readable ledger below. "
            "Do not modify "
            "files or run tests.\n\n"
            f"{COMMITMENT_LEDGER_GUIDANCE}\n\n"
            f"{skill_guidance}\n\n"
            f"{PLANNING_PRINCIPLES}\n\nTarget: {self.target}\n\nAssignment:\n{self.state['task']}"
        )
        prompt = f"{ARCHITECTURAL_DECISION_OUTPUT}\n\n{prompt}"
        proposal = clean_markdown(self._run_codex(prompt, sandbox="read-only"))
        self.state["proposal"] = proposal
        self.state["proposal_version"] = 1
        self._write_proposal(proposal)
        self._stage_result("completed")
    def _stage_plan_reviews(self, stage: str) -> None:
        iteration = int(stage[-1])
        header, sections, metadata = self._plan_review_prompt(iteration)
        recoverable_context = next((
            context for context in reversed(self.state.get("claude_contexts", []))
            if context.get("stage") == stage
            and context.get("proposal_version") == metadata["proposal_version"]
            and context.get("call_state") == "completed"
            and isinstance(context.get("report"), str)
            and context.get("report")
            and not any(
                review.get("context_manifest", {}).get("prompt_sha256")
                == context.get("prompt_sha256")
                for review in self.state.get("plan_reviews", [])
            )
        ), None)
        if recoverable_context is not None:
            report, manifest = recoverable_context["report"], recoverable_context
        else:
            report, manifest = self._call_claude(header, stage, sections, metadata=metadata)
        report, _scorecard = _planning_review.normalize_scorecard(report)
        self.state["plan_reviews"].append({
            "attempt": len(self.state.get("plan_reviews", [])) + 1,
            "iteration": iteration,
            "proposal_version": metadata["proposal_version"],
            "report": report,
            "time": now(),
            "valid": True,
            "context_manifest": manifest,
        })
        self._stage_result("completed")
    def _stage_plan_refinement(self, stage: str) -> None:
        iteration = int(stage[-1])
        review = next((record for record in reversed(self.state.get("plan_reviews", []))
                       if record.get("valid") is True and record.get("iteration") == iteration), None)
        if review is None:
            raise WorkflowError(f"No valid Claude plan review exists for iteration {iteration}.")
        report = review["report"]
        guidance = self._guidance()
        skill_guidance = _guidance_for_codex(
            guidance, stage=stage,
            context="\n".join((self.state["task"], self.state["proposal"], report)),
        )
        prompt = (
            "Evaluate every distinct finding in this valid Claude plan-review report. For each, explicitly "
            "mark incorporated, rejected, or deferred and give a reason. Incorporate valid in-scope "
            "recommendations; do not turn optional production advice into assignment requirements. "
            "Return the complete refined proposal as readable Markdown, retaining requirements mapping, "
            "material architectural decisions and trade-offs, scope distinctions, and the relevant skills "
            "and their application. Preserve or update the machine-readable commitment ledger to reflect "
            "the refined proposal without converting optional or future advice into obligations. Put a section titled "
            "`## Claude finding dispositions` at the end of the proposal. Use one bullet for every finding "
            "in this format: `- Finding: ... | Decision: incorporated/rejected/deferred | Reason: ...`. "
            "If there are none, write `- No actionable findings.` Apply Engineering Judgment & "
            "Proportional Design. Add one final "
            "line exactly in this form: `Second plan review needed: yes` or `...: no`. Use yes only when "
            "a material plan change needs a second independent plan review.\n\n"
            f"{ARCHITECTURAL_DECISION_OUTPUT}\n\n{PLANNING_PRINCIPLES}\n\n{skill_guidance}\n\n"
            f"{COMMITMENT_LEDGER_GUIDANCE}\n\n"
            f"Assignment:\n{self.state['task']}\n\nCurrent proposal (version "
            f"{self.state.get('proposal_version', 1)}):\n{self.state['proposal']}\n\n"
            f"Claude review (iteration {iteration}, proposal version {review.get('proposal_version')}):\n{report}"
        )
        refined = self._run_codex(prompt, sandbox="read-only")
        next_proposal_version = int(self.state.get("proposal_version", 1)) + 1
        try:
            proposal, review_again, dispositions = _planning_review.refined_proposal(refined)
            validated_ledger = self._validate_proposal_ledger(proposal)
        except WorkflowError as exc:
            self._record_unvalidated_planning_output(
                refined, exc, stage=stage, proposal_version=next_proposal_version,
            )
            raise
        self.state["proposal"] = proposal
        self.state["proposal_version"] = next_proposal_version
        self.state.setdefault("plan_review_decisions", []).append({
            "iteration": iteration,
            "proposal_version": self.state["proposal_version"],
            "dispositions": dispositions,
        })
        self._write_proposal(proposal, validated_ledger=validated_ledger)
        if iteration == 1 and review_again:
            self._stage_result("review_again")
        else:
            self._stage_result("awaiting_approval")
    def _stage_human_revision(self, stage: str) -> None:
        guidance = self._guidance()
        skill_guidance = _guidance_for_codex(
            guidance, stage=stage,
            context="\n".join((self.state["task"], self.state["proposal"], self.state["human_feedback"])),
        )
        prompt = (
            "Revise the saved proposal to address the user's explicit feedback. Keep the original "
            "assignment and unrelated decisions intact. Apply Engineering Judgment & Proportional Design. "
            "Apply relevant skill instructions below, and record the skill name and how it informed the "
            "proposal; if there are no applicable skills, state that. Distinguish must implement, should "
            "implement, future production considerations, and out of scope. Preserve and update the "
            "machine-readable commitment ledger using the user's explicit feedback. Return the complete proposal "
            "in readable Markdown; do not implement code.\n\n"
            f"{ARCHITECTURAL_DECISION_OUTPUT}\n\n{PLANNING_PRINCIPLES}\n\n{COMMITMENT_LEDGER_GUIDANCE}\n\n{skill_guidance}\n\n"
            f"Assignment:\n{self.state['task']}\n\nCurrent proposal (version "
            f"{self.state.get('proposal_version', 1)}):\n{self.state['proposal']}\n\n"
            f"Prior review attempts:\n{_planning_review.review_history_for_prompt(self.state)}\n\n"
            f"User feedback:\n{self.state['human_feedback']}"
        )
        self.state["proposal"] = clean_markdown(self._run_codex(prompt, sandbox="read-only"))
        self.state["proposal_version"] = int(self.state.get("proposal_version", 1)) + 1
        self._write_proposal(self.state["proposal"])
        authorized_plan_limit = self.state["authorization"]["claude_aily"]["max_plan_reviews"]
        valid_reviews = _planning_review.valid_plan_reviews(self.state)
        if len(valid_reviews) < authorized_plan_limit:
            self._stage_result("next_review")
        else:
            self._stage_result("awaiting_approval")
    def _stage_implementation(self, stage: str) -> None:
        if not self.state.get("implementation_approved_at"):
            self._stage_result("implementation_not_approved")
            return
        guidance = self._guidance()
        skill_guidance = _guidance_for_codex(
            guidance, stage=stage,
            context="\n".join((self.state["task"], self.state["proposal"])),
        )
        validation_baseline = _traceability.commitment_validation_baseline(self.state, self.workflow_id)
        prompt = (
            "Implement the approved proposal in this repository. Follow repository instructions. "
            "Apply the relevant skill instructions supplied below; do not merely list skills. "
            "Use the persistent workflow authorization for ordinary target-workspace operations without "
            "individual approvals. Install dependencies only from project manifests using the project's "
            "package manager; do not use arbitrary download URLs or custom registries. Run relevant checks. "
            "Do not expose secrets or read unrelated repositories. Commit, push, merge, deploy, publish, "
            "shared-infrastructure changes, and access outside the target require separate explicit "
            "user authorization and are not included in this workflow authorization. "
            "Do not delete unrelated existing files. Follow Engineering Judgment & Proportional Design "
            "and do not invent product behavior. Return a concise summary with files changed, "
            "commands actually run, results, and known limitations. Do not use this summary to declare "
            "commitments fulfilled. For every required validation scenario, execute an actual pytest or "
            "unittest test that asserts the scenario acceptance condition; manual inspection and bare shell "
            "checks are not runner receipts. If no suitable existing test applies and adding an application "
            "test file would exceed scope, place a temporary unittest under the workflow verification "
            "directory inside `.engineering/workflows/` and run it from the target repository with "
            "`python3 -m unittest discover -v` so individual test IDs and outcomes are captured. "
            "For repository-scope scenarios, compare "
            "`git status --porcelain=v1 -z --untracked-files=all` with the supplied initial baseline; "
            "preserve pre-existing paths, exclude only the supplied Harness workflow-artifact prefix, "
            "verify each pre-existing path's supplied kind, mode, and SHA-256 state, and do not ignore "
            "other `.engineering/` paths or sensitive-path policy. If `preexisting_snapshot_complete` is "
            "false, do not claim the scope scenario passed. Include test IDs "
            "exactly as the runner reports them. Do not invent execution event IDs: set "
            "`execution_event_ids` to an empty array and the Harness will bind only observed receipts. "
            "Finish with one `harness-evidence` JSON block linking each approved commitment to source "
            "references and specific scenario/test IDs. Run all required scenarios after the final code edits, "
            "prefer one combined runner invocation, and make it the last tool action; do not edit code afterward. "
            "Do not include raw command output or secrets. Use exactly one fenced block labeled "
            "`harness-evidence` with this JSON shape, replacing example values:\n"
            "```harness-evidence\n"
            "{\"schema_version\":1,\"commitments\":["
            "{\"commitment_id\":\"C-001\",\"source_refs\":[{\"path\":\"src/file.py\","
            "\"symbol\":\"function_name\"}],\"validation_refs\":[{\"scenario_id\":\"scenario-1\","
            "\"test_ids\":[\"tests/test_file.py::test_case\"],\"execution_event_ids\":[]}],"
            "\"disposition\":null,\"reason\":null}]}\n```. Include only observed references.\n\n"
            f"Harness validation baseline (do not modify):\n{validation_baseline}\n\n"
            f"{COMMITMENT_LEDGER_GUIDANCE}\n\n{PLANNING_PRINCIPLES}\n\n{skill_guidance}\n\n"
            f"Assignment:\n{self.state['task']}\n\nApproved proposal:\n{self.state['proposal']}"
        )
        self.state["implementation_summary"] = self._run_codex(prompt, sandbox="workspace-write")
        _traceability.capture_commitment_evidence(
            self.state, self.target, self.state["implementation_summary"],
        )
        self._stage_result("implemented")
    def _stage_code_review_or_verification(self, stage: str) -> None:
        purpose = "verification" if stage == "verification" else "initial"
        self._require_write_operation_target(purpose)
        review_diff = _review_diff(self.state, self.target)
        if not review_diff.included_paths and not review_diff.context_files:
            self.state["review_included_files"] = []
            self.state["review_exclusions"] = [
                {"path": _safe_review_path(path), "reason": reason}
                for path, reason in review_diff.excluded_files
            ]
            self.state["review_files_not_reviewed"] = []
            self.workflow_state.save()
            reasons = "; ".join(
                f"{path}: {reason}" for path, reason in review_diff.excluded_files
            )
            detail = f"No reviewable text changes were found. Excluded changed files: {reasons or 'none'}"
            raise WorkflowError(detail)
        try:
            campaign, shared, parts = self._prepare_review_campaign(review_diff, purpose=purpose)
        except WorkflowError as exc:
            if not self.state.get("review_campaign"):
                self.state["review_included_files"] = list(review_diff.included_paths)
                self.state["review_exclusions"] = [
                    {"path": _safe_review_path(path), "reason": reason}
                    for path, reason in review_diff.excluded_files
                ]
                self.state["review_files_not_reviewed"] = [
                    {"path": path, "reason": redact(str(exc))}
                    for path in review_diff.included_paths
                ]
                self.workflow_state.save()
            raise
        self.state["review_scope_files"] = sorted({
            *review_diff.included_paths,
            *(path for path, _origin, _baseline, _final in review_diff.context_files),
        })
        self.state["review_workflow_paths"] = list(review_diff.workflow_paths)
        self.state["review_scope_sha256"] = _ReviewCampaign._campaign_source_hash(review_diff)
        if purpose == "initial" and not isinstance(self.state.get("implementation_snapshot"), dict):
            implementation_snapshot = _workspace.capture_preexisting_snapshot(
                self.target, review_diff.included_paths, redactor=redact,
            )
            implementation_snapshot["source_patch_sha256"] = self.state["review_scope_sha256"]
            implementation_snapshot["target_fingerprint"] = self._fingerprint_repository(self.target)
            snapshot_body = {
                key: value for key, value in implementation_snapshot.items() if key != "sha256"
            }
            implementation_snapshot["sha256"] = hashlib.sha256(json.dumps(
                snapshot_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            self.state["implementation_snapshot"] = implementation_snapshot
        self.workflow_state.save()
        report = self._run_review_campaign(
            campaign, shared, parts, review_diff,
        )
        campaign["purpose"] = purpose
        if purpose == "verification":
            self.state["verification_report"] = report
        else:
            self.state["code_review"] = report
        _traceability.capture_commitment_assessment(self.state, report, purpose=purpose)
        if purpose == "verification":
            self.state["verification_context_sha256"] = campaign.get("shared_context_sha256")
        else:
            self.state["code_review_context_sha256"] = campaign.get("shared_context_sha256")
        review_evidence = "\n\n".join([
            report,
            *(record.get("report", "") for record in campaign.get("part_reviews", [])),
        ])
        if campaign.get("status") != "complete" or campaign.get("files_not_reviewed"):
            raise WorkflowError("Review did not complete all required files and parts; delivery is blocked.")
        no_blocking_findings = _clearly_no_review_findings(review_evidence)
        if purpose == "initial" and no_blocking_findings:
            self.state["correction_summary"] = (
                "Claude reported no acceptance-blocking findings; no correction run was needed."
            )
        verification_dispositions = None
        if purpose == "verification" and no_blocking_findings:
            history = self.state.get("review_campaign_history", [])
            original_campaign = next((
                item for item in reversed(history)
                if isinstance(item, dict) and item.get("purpose") == "initial"
            ), {})
            verification_dispositions, disposition_error = _post_correction_dispositions(
                original_campaign, campaign,
            )
            if disposition_error is not None:
                self.state.setdefault("integrity", {})["verification_result"] = {
                    "status": "failed",
                    "campaign_id": campaign.get("campaign_id"),
                    "checked_at": now(),
                    "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
                    "finding": disposition_error,
                }
                self.workflow_state.save()
                raise WorkflowError(
                    "Independent post-correction verification is inconclusive; delivery is blocked. "
                    + disposition_error
                )
        self._record_review_snapshot(campaign, review_diff, purpose=purpose)
        if purpose == "verification":
            integrity = self.state.setdefault("integrity", {})
            if no_blocking_findings:
                integrity["verification_result"] = {
                    "status": "verified",
                    "campaign_id": campaign.get("campaign_id"),
                    "original_campaign_id": (self.state.get("correction_attempts") or [{}])[-1].get(
                        "original_campaign_id",
                    ),
                    "checked_at": now(),
                    "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
                    "finding": "No unresolved blocking findings were reported.",
                    "original_finding_dispositions": verification_dispositions,
                }
                self._stage_result("review_clean")
            else:
                integrity["verification_result"] = {
                    "status": "failed",
                    "campaign_id": campaign.get("campaign_id"),
                    "checked_at": now(),
                    "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
                    "finding": "The independent verification reports unresolved or new blocking findings.",
                }
                self.workflow_state.save()
                raise WorkflowError(
                    "Independent post-correction verification found unresolved or new blocking findings; delivery is blocked."
                )
        elif no_blocking_findings:
            self._stage_result("review_clean")
        else:
            self._stage_result("blocking_findings")
    def _stage_correction(self, stage: str) -> None:
        guidance = self._guidance()
        campaign = self.state.get("review_campaign") or {}
        if campaign.get("status") != "complete":
            raise WorkflowError("Correction is blocked because the initial review campaign is incomplete.")
        external = self.state["authorization"]["claude_aily"]
        if external.get("budget_unknown") is True:
            raise WorkflowError("Correction is blocked because Claude spend is unknown and verification cannot be authorized safely.")
        limits = self.workflow_state.execution_limits()
        usage = self.workflow_state.resource_usage()
        remaining_claude_calls = limits["max_claude_calls"] - usage["claude_calls"]
        minimum_verification_calls = max(1, int(campaign.get("required_calls", 1)))
        if remaining_claude_calls < minimum_verification_calls:
            raise WorkflowError(
                "Correction is blocked because the saved Claude call authorization cannot cover an independent verification campaign."
            )
        attempts = self.state.setdefault("correction_attempts", [])
        attempt = attempts[-1] if attempts and attempts[-1].get("status") == "in_progress" else None
        if attempt is None:
            if len(attempts) >= limits["max_correction_attempts"]:
                raise WorkflowError("The authorized Codex correction-attempt limit is exhausted.")
            attempt = {
                "attempt": len(attempts) + 1,
                "status": "in_progress",
                "started_at": now(),
                "original_campaign_id": campaign.get("campaign_id"),
                "original_report_sha256": hashlib.sha256(
                    str(self.state.get("code_review") or "").encode("utf-8"),
                ).hexdigest(),
            }
            attempts.append(attempt)
            usage["correction_attempts"] += 1
            self.workflow_state.save()
        individual_reports = "\n\n".join(
            f"### {record.get('part_id')}\n{record.get('report', '')}"
            for record in campaign.get("part_reviews", []) if record.get("report")
        ) or "No separate individual reports were recorded."
        skill_guidance = _guidance_for_codex(
            guidance, stage=stage,
            context="\n".join((
                self.state["task"], self.state["proposal"], self.state["code_review"], individual_reports,
            )),
        )
        validation_baseline = _traceability.commitment_validation_baseline(self.state, self.workflow_id)
        prompt = (
            "Evaluate the independent Claude code review. Treat its findings as the complete correction "
            "assignment. Fix valid in-scope defects and reject incorrect recommendations with a brief "
            "reason. Assess every distinct finding from individual part reports and the consolidated "
            "integration report. Optional improvements are not mandatory. Modify only files necessary to "
            "address valid findings; preserve behavior outside those findings. Do not perform unrelated "
            "refactoring, cleanup, or scope expansion. Add or update focused tests when needed. For every "
            "required validation scenario, execute an actual pytest or unittest test that asserts its "
            "acceptance condition; manual inspection and bare shell checks are not runner receipts. If no "
            "suitable existing test applies and adding an application test file would exceed scope, place a "
            "temporary unittest under this workflow's `.engineering/workflows/` verification directory and "
            "run it from the target repository with `python3 -m unittest discover -v` so individual test IDs "
            "and outcomes are captured. For repository-scope "
            "scenarios, compare `git status --porcelain=v1 -z --untracked-files=all` with the supplied initial "
            "baseline; preserve pre-existing paths, exclude only the supplied Harness workflow-artifact "
            "prefix, verify each pre-existing path's supplied kind, mode, and SHA-256 state, and do not ignore "
            "other `.engineering/` paths or sensitive-path policy. If `preexisting_snapshot_complete` is false, "
            "do not claim the scope scenario passed. Include test IDs "
            "exactly as the runner reports them. Set `execution_event_ids` to an empty array; the Harness "
            "will bind only observed receipts. Do not repeat the review. Return a concise correction and "
            "validation summary plus one complete `harness-evidence` JSON block for every approved "
            "commitment. Run all required scenarios after the final code edits, prefer one combined runner "
            "invocation, and make it the last tool action; do not edit code afterward. Do not include raw "
            "output or secrets.\n\n"
            f"Harness validation baseline (do not modify):\n{validation_baseline}\n\n"
            f"{COMMITMENT_LEDGER_GUIDANCE}\n\n"
            f"{PLANNING_PRINCIPLES}\n\n"
            f"{skill_guidance}\n\n"
            f"Assignment:\n{self.state['task']}\n\nApproved proposal:\n{self.state['proposal']}\n\n"
            f"Claude code review:\n{self.state['code_review']}\n\n"
            f"Individual Claude part reports:\n{individual_reports}\n\n"
            f"Prior implementation summary:\n{self.state['implementation_summary']}"
        )
        self.state["correction_summary"] = self._run_codex(prompt, sandbox="workspace-write")
        _traceability.capture_commitment_evidence(
            self.state, self.target, self.state["correction_summary"],
        )
        attempt["status"] = "completed"
        attempt["finished_at"] = now()
        attempt["correction_summary_sha256"] = hashlib.sha256(
            self.state["correction_summary"].encode("utf-8"),
        ).hexdigest()
        campaign["purpose"] = "initial"
        campaign_history = self.state.setdefault("review_campaign_history", [])
        if not any(item.get("campaign_id") == campaign.get("campaign_id") for item in campaign_history):
            campaign_history.append(copy.deepcopy(campaign))
        self.state["review_campaign"] = None
        self.state["review_campaign_id"] = None
        self.state["verification_report"] = None
        self._stage_result("corrected")
    def _stage_complete(self, stage: str) -> None:
        delivery_verification = self._verify_delivery_integrity()
        self._capture_final_target_fingerprint()
        final_target = self.state.get("execution_fingerprint", {}).get("final", {}).get("target", {})
        if (final_target.get("status") != "captured"
                or final_target.get("commit_sha") != delivery_verification.get("target_commit_sha")
                or final_target.get("working_tree_sha256") != delivery_verification.get("target_working_tree_sha256")):
            raise WorkflowError("Delivery is blocked because the target changed during final integrity checks.")
        prior_status = self.state.get("status")
        self.state["status"] = "complete"
        self.state["completed_at"] = now()
        _traceability.evaluate_commitment_delivery(self.state, self.target, self.proposal_path)
        if isinstance(self.state.get("commitment_gate"), dict):
            self.state["commitment_gate"]["workflow_status"] = "complete"
        _atomic_text(self.delivery_path, self._delivery_report())
        delivery_hash = hashlib.sha256(_persistence.read_bytes(self.delivery_path)).hexdigest()
        self.state.setdefault("integrity", {})["delivery_artifact_sha256"] = delivery_hash
        self.state["status"] = prior_status
        self._stage_result("completed")

    def _plan_review_prompt(self, iteration: int) -> tuple[str, dict[str, str], dict]:
        guidance = self._guidance()
        relevant_skills = _guidance_for_claude(
            guidance,
            stage="plan_review",
            context="\n".join((self.state.get("task", ""), self.state.get("proposal", ""))),
            include_skill_content=self._claude_skill_content_authorized(),
        )
        return _planning_review.build_plan_review_prompt(
            self.state,
            self.target,
            iteration,
            repository_context=_planning_review.repository_context_for_claude(guidance),
            relevant_skills=relevant_skills,
        )

    def _delivery_report(self) -> str:
        campaign = self.state.get("review_campaign")
        campaigns = [
            item for item in self.state.get("review_campaign_history", []) if isinstance(item, dict)
        ] if isinstance(self.state.get("review_campaign_history"), list) else []
        if isinstance(campaign, dict):
            campaigns.append(campaign)
        feedback_reports = [
            report for item in campaigns for report in _validated_campaign_reports(item)
        ]
        structured_review_feedback = render_review_feedback(feedback_reports) if feedback_reports else ""
        return render_delivery_report(
            self.state,
            workflow_id=self.workflow_id,
            target=self.target,
            fallback_completed_at=now(),
            valid_plan_review_count=len(_planning_review.valid_plan_reviews(self.state)),
            structured_review_feedback=structured_review_feedback,
            claude_max_payload_env=CLAUDE_MAX_PAYLOAD_ENV,
            default_claude_payload_chars=claude_reviewer.DEFAULT_MAX_REVIEW_PAYLOAD_CHARS,
        )

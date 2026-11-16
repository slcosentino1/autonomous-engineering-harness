"""Persistent workflow authorization construction and validation."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from observability.tracer import observability_scope, valid_observability_scope


DEFAULT_MAX_CODE_REVIEW_CALLS = 32
MAX_PLAN_REVIEWS = 2
MAX_AUTHORIZED_CODE_REVIEW_CALLS = 64
DEFAULT_MAX_CODEX_CALLS = 8
MAX_AUTHORIZED_CODEX_CALLS = 64
DEFAULT_CODEX_TIMEOUT_SECONDS = 3600
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 900
DEFAULT_WORKFLOW_TIMEOUT_SECONDS = 86400
MAX_PROVIDER_TIMEOUT_SECONDS = 86400
MAX_WORKFLOW_TIMEOUT_SECONDS = 604800
MAX_CORRECTION_ATTEMPTS = 1
WORKSPACE_OPERATIONS = [
    "read_create_edit_rename_delete_assignment_files_in_target",
    "run_development_commands_builds_tests_linters_and_static_analysis",
    "install_manifest_declared_dependencies_using_repository_package_manager",
    "run_codex_planning_implementation_and_correction",
    "save_workflow_state_logs_proposals_reports_and_artifacts",
]
CODEX_STAGES = ["planning", "implementation", "correction"]
LEGACY_CLAUDE_DATA_SCOPE = [
    "assignment",
    "implementation_proposal",
    "implementation_and_validation_summary",
    "relevant_redacted_code_changes",
]
CLAUDE_DATA_SCOPE = [*LEGACY_CLAUDE_DATA_SCOPE, "relevant_skill_instructions"]
RESTRICTED_OPERATIONS = [
    "access_outside_target_workspace",
    "disclose_secrets",
    "commit",
    "push",
    "merge",
    "deploy",
    "publish",
    "change_shared_infrastructure",
]


class AuthorizationError(RuntimeError):
    """A workflow authorization is invalid or unsupported."""


_RESUMABLE_STAGES = {
    "planning", "plan_review_1", "plan_review_2", "plan_refine_1", "plan_refine_2",
    "human_revision", "implementation", "code_review", "correction", "verification",
}
_BUDGET_CHANGE_FIELDS = {
    "timestamp", "operation", "previous_limit_usd", "requested_amount_usd", "new_limit_usd",
    "spent_usd", "remaining_usd", "workflow_id", "authorization_id", "command", "stage",
}
_BUDGET_AMOUNT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def parse_budget_amount(value: str) -> Decimal:
    """Parse a positive, cent-precision amount representable by saved JSON numbers."""
    if not isinstance(value, str) or not value.strip():
        raise AuthorizationError("Budget amount must be a positive finite amount with at most two decimal places.")
    value = value.strip()
    if not _BUDGET_AMOUNT_RE.fullmatch(value):
        raise AuthorizationError("Budget amount must be a positive finite amount with at most two decimal places.")
    try:
        amount = Decimal(value)
    except Exception as exc:
        raise AuthorizationError(
            "Budget amount must be a positive finite amount with at most two decimal places."
        ) from exc
    if not amount.is_finite() or amount <= 0:
        raise AuthorizationError("Budget amount must be positive, finite, and use at most two decimal places.")
    _json_money_number(amount)
    if not _has_cent_precision(amount):
        raise AuthorizationError("Budget amount must be positive, finite, and use at most two decimal places.")
    return amount


def _has_cent_precision(amount: Decimal) -> bool:
    if amount.as_tuple().exponent >= -2:
        return True
    try:
        with localcontext() as context:
            context.prec = 1000
            return amount == amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        return False


def _json_money_number(value: Decimal) -> float:
    """Use the existing JSON-number money representation without losing cent value."""
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise AuthorizationError("Budget amount cannot be represented as a finite JSON number.") from exc
    if not number == number or number in (float("inf"), float("-inf")):
        raise AuthorizationError("Budget amount cannot be represented as a finite JSON number.")
    if Decimal(str(number)) != value:
        raise AuthorizationError("Budget amount cannot be represented without losing its cent precision.")
    return number


def _json_decimal_number(value: Decimal) -> float:
    """Serialize a non-negative spend or balance without changing its decimal value."""
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise AuthorizationError("Budget history amount cannot be represented as a finite JSON number.") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise AuthorizationError("Budget history amount cannot be represented as a finite JSON number.")
    if Decimal(str(number)) != value:
        raise AuthorizationError("Budget history amount cannot be represented without rounding.")
    return number


def authorize_budget_change(
    authorization: dict,
    amount_text: str,
    operation: str,
    *,
    workflow_id: str,
    stage: str,
    timestamp: str,
) -> dict:
    """Append a budget authorization event and update the effective limit."""
    if operation not in {"extend", "set_limit"}:
        raise AuthorizationError("Unsupported budget authorization operation.")
    if stage not in _RESUMABLE_STAGES:
        raise AuthorizationError("Budget changes require a saved, resumable workflow stage.")
    external = authorization.get("claude_aily")
    if not isinstance(external, dict):
        raise AuthorizationError("Saved Claude/Aily authorization is missing.")
    amount = parse_budget_amount(amount_text)
    try:
        previous = Decimal(str(external.get("budget_usd")))
        spent = Decimal(str(external.get("spent_usd")))
    except Exception as exc:
        raise AuthorizationError("Saved Claude/Aily budget is invalid.") from exc
    if not previous.is_finite() or not spent.is_finite() or previous < Decimal("0.01") or spent < 0:
        raise AuthorizationError("Saved Claude/Aily budget or spending is invalid.")
    with localcontext() as context:
        context.prec = 1000
        new_limit = previous + amount if operation == "extend" else amount
        remaining = new_limit - spent
    if new_limit < spent:
        raise AuthorizationError("New Claude/Aily budget limit cannot be below accumulated spending.")
    _json_money_number(new_limit)
    spent_number = _json_decimal_number(spent)
    remaining_number = _json_decimal_number(remaining)
    changes = external.get("budget_changes", [])
    if not isinstance(changes, list):
        raise AuthorizationError("Saved Claude/Aily budget history is invalid.")
    entry = {
        "timestamp": timestamp,
        "operation": operation,
        "previous_limit_usd": _json_money_number(previous),
        "requested_amount_usd": _json_money_number(amount),
        "new_limit_usd": _json_money_number(new_limit),
        "spent_usd": spent_number,
        "remaining_usd": remaining_number,
        "workflow_id": workflow_id,
        "authorization_id": authorization.get("authorization_id"),
        "command": "resume",
        "stage": stage,
    }
    changes.append(entry)
    external["budget_changes"] = changes
    external["budget_usd"] = _json_money_number(new_limit)
    return entry


def _validate_budget_change_history(authorization: dict, external: dict, workflow_id: str) -> None:
    if "budget_changes" not in external:
        return
    changes = external.get("budget_changes")
    if not isinstance(changes, list) or not changes:
        raise AuthorizationError("Saved Claude/Aily budget history is invalid.")
    prior_limit: Decimal | None = None
    prior_spent: Decimal | None = None
    for entry in changes:
        if not isinstance(entry, dict) or set(entry) != _BUDGET_CHANGE_FIELDS:
            raise AuthorizationError("Saved Claude/Aily budget history is invalid.")
        try:
            timestamp = datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00"))
            previous = Decimal(str(entry["previous_limit_usd"]))
            requested = Decimal(str(entry["requested_amount_usd"]))
            new_limit = Decimal(str(entry["new_limit_usd"]))
            spent = Decimal(str(entry["spent_usd"]))
            remaining = Decimal(str(entry["remaining_usd"]))
        except Exception as exc:
            raise AuthorizationError("Saved Claude/Aily budget history is invalid.") from exc
        amounts = (previous, requested, new_limit, spent, remaining)
        if (timestamp.tzinfo is None or timestamp.utcoffset() is None
                or timestamp.utcoffset().total_seconds() != 0
                or any(not amount.is_finite() for amount in amounts)
                or previous < Decimal("0.01") or requested <= 0 or new_limit < Decimal("0.01")
                or spent < 0 or remaining < 0
                or any(not _has_cent_precision(amount) for amount in (previous, requested, new_limit))
                or entry.get("workflow_id") != workflow_id
                or entry.get("authorization_id") != authorization.get("authorization_id")
                or entry.get("command") != "resume"
                or not isinstance(entry.get("stage"), str)
                or entry.get("stage") not in _RESUMABLE_STAGES
                or not isinstance(entry.get("operation"), str)
                or entry.get("operation") not in {"extend", "set_limit"}):
            raise AuthorizationError("Saved Claude/Aily budget history is invalid.")
        if prior_limit is not None and previous != prior_limit:
            raise AuthorizationError("Saved Claude/Aily budget history limit chain is invalid.")
        if prior_spent is not None and spent < prior_spent:
            raise AuthorizationError("Saved Claude/Aily budget history spending decreased.")
        with localcontext() as context:
            context.prec = 1000
            expected_limit = previous + requested if entry["operation"] == "extend" else requested
            expected_remaining = new_limit - spent
        if new_limit != expected_limit or remaining != expected_remaining:
            raise AuthorizationError("Saved Claude/Aily budget history arithmetic is invalid.")
        prior_limit, prior_spent = new_limit, spent
    try:
        current_limit = Decimal(str(external.get("budget_usd")))
        current_spent = Decimal(str(external.get("spent_usd")))
    except Exception as exc:
        raise AuthorizationError("Saved Claude/Aily budget is invalid.") from exc
    if prior_limit != current_limit or prior_spent > current_spent:
        raise AuthorizationError("Saved Claude/Aily budget history does not match current authorization state.")


def _authorization_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_workflow_authorization(
    target: Path, workflow_id: str, budget_usd: float,
    max_code_review_calls: int = DEFAULT_MAX_CODE_REVIEW_CALLS,
    tracing_enabled: bool = False,
    *, max_codex_calls: int = DEFAULT_MAX_CODEX_CALLS,
    codex_timeout_seconds: int = DEFAULT_CODEX_TIMEOUT_SECONDS,
    claude_timeout_seconds: int = DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    workflow_timeout_seconds: int = DEFAULT_WORKFLOW_TIMEOUT_SECONDS,
) -> dict:
    """Create the single persistent authorization attached to a new workflow."""
    if (isinstance(max_code_review_calls, bool) or not isinstance(max_code_review_calls, int)
            or not 1 <= max_code_review_calls <= MAX_AUTHORIZED_CODE_REVIEW_CALLS):
        raise AuthorizationError(
            f"Code review call authorization must be between 1 and {MAX_AUTHORIZED_CODE_REVIEW_CALLS}."
        )
    if (isinstance(max_codex_calls, bool) or not isinstance(max_codex_calls, int)
            or not 1 <= max_codex_calls <= MAX_AUTHORIZED_CODEX_CALLS):
        raise AuthorizationError(
            f"Codex call authorization must be between 1 and {MAX_AUTHORIZED_CODEX_CALLS}."
        )
    for name, value in (
        ("Codex", codex_timeout_seconds), ("Claude", claude_timeout_seconds),
    ):
        if (isinstance(value, bool) or not isinstance(value, int)
                or not 1 <= value <= MAX_PROVIDER_TIMEOUT_SECONDS):
            raise AuthorizationError(
                f"{name} timeout must be between 1 and {MAX_PROVIDER_TIMEOUT_SECONDS} seconds."
            )
    if (isinstance(workflow_timeout_seconds, bool) or not isinstance(workflow_timeout_seconds, int)
            or not 1 <= workflow_timeout_seconds <= MAX_WORKFLOW_TIMEOUT_SECONDS):
        raise AuthorizationError(
            f"Workflow timeout must be between 1 and {MAX_WORKFLOW_TIMEOUT_SECONDS} seconds."
        )
    max_provider_runtime_seconds = (
        max_codex_calls * codex_timeout_seconds
        + (MAX_PLAN_REVIEWS + max_code_review_calls) * claude_timeout_seconds
    )
    authorization = {
        "version": 1,
        "authorization_id": uuid.uuid4().hex,
        "created_at": _authorization_now(),
        "target": str(target),
        "workflow_id": workflow_id,
        "workspace_operations": WORKSPACE_OPERATIONS.copy(),
        "codex_stages": CODEX_STAGES.copy(),
        "execution_limits": {
            "max_codex_calls": max_codex_calls,
            "codex_timeout_seconds": codex_timeout_seconds,
            "claude_timeout_seconds": claude_timeout_seconds,
            "workflow_timeout_seconds": workflow_timeout_seconds,
            "max_claude_calls": MAX_PLAN_REVIEWS + max_code_review_calls,
            "max_correction_attempts": MAX_CORRECTION_ATTEMPTS,
            "max_provider_runtime_seconds": max_provider_runtime_seconds,
        },
        "claude_aily": {
            "provider": "Claude through Aily",
            "permitted_data": CLAUDE_DATA_SCOPE.copy(),
            "max_plan_reviews": MAX_PLAN_REVIEWS,
            "max_code_reviews": max_code_review_calls,
            "code_review_policy": {
                "mode": "adaptive_multi_part",
                "max_calls": max_code_review_calls,
            },
            "budget_usd": float(budget_usd),
            "spent_usd": 0.0,
            "budget_unknown": False,
            "active_call": None,
        },
        "observability": observability_scope(tracing_enabled),
        "restricted_operations": RESTRICTED_OPERATIONS.copy(),
    }
    authorization["scope_sha256"] = authorization_scope_digest(authorization)
    return authorization


def authorization_scope_digest(authorization: dict) -> str:
    """Hash authorization and spend state so scope or budget edits are detected on resume."""
    terms = {key: value for key, value in authorization.items() if key != "scope_sha256"}
    encoded = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_workflow_authorization(state: dict, target: Path, workflow_id: str) -> dict:
    """Reject missing, mismatched, or broadened saved authorization."""
    authorization = state.get("authorization")
    if not isinstance(authorization, dict) or authorization.get("version") != 1:
        raise AuthorizationError("Workflow has no supported persistent authorization; start a newly authorized workflow.")
    if authorization.get("target") != str(target) or authorization.get("workflow_id") != workflow_id:
        raise AuthorizationError("Saved authorization belongs to a different target or workflow.")
    if not isinstance(authorization.get("authorization_id"), str) or not authorization["authorization_id"]:
        raise AuthorizationError("Saved workflow authorization has no ID.")
    if authorization.get("workspace_operations") != WORKSPACE_OPERATIONS:
        raise AuthorizationError("Saved workspace authorization is missing, changed, or broader than the supported scope.")
    if authorization.get("codex_stages") != CODEX_STAGES:
        raise AuthorizationError("Saved Codex authorization is missing, changed, or broader than the supported stages.")
    if authorization.get("restricted_operations") != RESTRICTED_OPERATIONS:
        raise AuthorizationError("Saved protected-operation boundaries are missing or changed.")
    observability = authorization.get("observability")
    if observability is not None:
        enabled = observability.get("enabled") if isinstance(observability, dict) else None
        endpoint_hash = observability.get("langfuse_base_url_sha256") if isinstance(observability, dict) else None
        valid_endpoint_hash = (
            isinstance(endpoint_hash, str) and re.fullmatch(r"[a-f0-9]{64}", endpoint_hash)
            if enabled is True else endpoint_hash is None
        )
        if (not isinstance(observability, dict)
                or set(observability) != {"version", "enabled", "permitted_data", "langfuse_base_url_sha256"}
                or not valid_observability_scope(observability)
                or type(enabled) is not bool
                or not valid_endpoint_hash):
            raise AuthorizationError("Saved observability scope is invalid or changed.")
    external = authorization.get("claude_aily")
    if not isinstance(external, dict):
        raise AuthorizationError("Saved Claude/Aily authorization is missing.")
    code_review_limit = external.get("max_code_reviews")
    policy = external.get("code_review_policy")
    legacy_single_review = (
        policy is None and isinstance(code_review_limit, int)
        and not isinstance(code_review_limit, bool) and code_review_limit == 1
    )
    configured_review_policy = (
        isinstance(policy, dict)
        and policy.get("mode") == "adaptive_multi_part"
        and policy.get("max_calls") == code_review_limit
        and isinstance(code_review_limit, int)
        and not isinstance(code_review_limit, bool)
        and 1 <= code_review_limit <= MAX_AUTHORIZED_CODE_REVIEW_CALLS
    )
    if (external.get("provider") != "Claude through Aily"
            or external.get("permitted_data") not in (LEGACY_CLAUDE_DATA_SCOPE, CLAUDE_DATA_SCOPE)
            or external.get("max_plan_reviews") != MAX_PLAN_REVIEWS
            or not (legacy_single_review or configured_review_policy)):
        raise AuthorizationError("Saved Claude/Aily scope or review limit is missing or changed.")
    limits = authorization.get("execution_limits")
    if limits is not None:
        max_codex_calls = limits.get("max_codex_calls") if isinstance(limits, dict) else None
        codex_timeout = limits.get("codex_timeout_seconds") if isinstance(limits, dict) else None
        claude_timeout = limits.get("claude_timeout_seconds") if isinstance(limits, dict) else None
        workflow_timeout = limits.get("workflow_timeout_seconds") if isinstance(limits, dict) else None
        max_claude_calls = limits.get("max_claude_calls") if isinstance(limits, dict) else None
        correction_attempts = limits.get("max_correction_attempts") if isinstance(limits, dict) else None
        runtime_limit = limits.get("max_provider_runtime_seconds") if isinstance(limits, dict) else None
        expected_runtime = (
            max_codex_calls * codex_timeout
            + (MAX_PLAN_REVIEWS + code_review_limit) * claude_timeout
            if all(isinstance(value, int) and not isinstance(value, bool) for value in (
                max_codex_calls, codex_timeout, claude_timeout, code_review_limit,
            )) else None
        )
        if (not isinstance(limits, dict)
                or set(limits) != {
                    "max_codex_calls", "codex_timeout_seconds", "claude_timeout_seconds",
                    "workflow_timeout_seconds", "max_claude_calls", "max_correction_attempts",
                    "max_provider_runtime_seconds",
                }
                or isinstance(max_codex_calls, bool) or not isinstance(max_codex_calls, int)
                or not 1 <= max_codex_calls <= MAX_AUTHORIZED_CODEX_CALLS
                or any(isinstance(value, bool) or not isinstance(value, int)
                       or not 1 <= value <= MAX_PROVIDER_TIMEOUT_SECONDS
                       for value in (codex_timeout, claude_timeout))
                or isinstance(workflow_timeout, bool) or not isinstance(workflow_timeout, int)
                or not 1 <= workflow_timeout <= MAX_WORKFLOW_TIMEOUT_SECONDS
                or isinstance(max_claude_calls, bool) or not isinstance(max_claude_calls, int)
                or max_claude_calls != MAX_PLAN_REVIEWS + code_review_limit
                or correction_attempts != MAX_CORRECTION_ATTEMPTS
                or isinstance(correction_attempts, bool)
                or not isinstance(runtime_limit, int) or isinstance(runtime_limit, bool)
                or runtime_limit != expected_runtime):
            raise AuthorizationError("Saved execution limits are invalid or changed.")
    try:
        budget = Decimal(str(external.get("budget_usd")))
        spent = Decimal(str(external.get("spent_usd")))
    except Exception as exc:
        raise AuthorizationError("Saved Claude/Aily budget is invalid.") from exc
    if (not budget.is_finite() or budget < Decimal("0.01")
            or not _has_cent_precision(budget)
            or not spent.is_finite() or spent < 0):
        raise AuthorizationError("Saved Claude/Aily budget or spending is invalid.")
    _validate_budget_change_history(authorization, external, workflow_id)
    if authorization.get("scope_sha256") != authorization_scope_digest(authorization):
        raise AuthorizationError("Saved workflow authorization changed after initialization.")
    return authorization

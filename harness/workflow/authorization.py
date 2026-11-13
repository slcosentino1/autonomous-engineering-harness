"""Persistent workflow authorization construction and validation."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from observability.tracer import observability_scope, valid_observability_scope


DEFAULT_MAX_CODE_REVIEW_CALLS = 16
MAX_PLAN_REVIEWS = 2
MAX_AUTHORIZED_CODE_REVIEW_CALLS = 64
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


def _authorization_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_workflow_authorization(
    target: Path, workflow_id: str, budget_usd: float,
    max_code_review_calls: int = DEFAULT_MAX_CODE_REVIEW_CALLS,
    tracing_enabled: bool = False,
) -> dict:
    """Create the single persistent authorization attached to a new workflow."""
    if (isinstance(max_code_review_calls, bool) or not isinstance(max_code_review_calls, int)
            or not 1 <= max_code_review_calls <= MAX_AUTHORIZED_CODE_REVIEW_CALLS):
        raise AuthorizationError(
            f"Code review call authorization must be between 1 and {MAX_AUTHORIZED_CODE_REVIEW_CALLS}."
        )
    authorization = {
        "version": 1,
        "authorization_id": uuid.uuid4().hex,
        "created_at": _authorization_now(),
        "target": str(target),
        "workflow_id": workflow_id,
        "workspace_operations": WORKSPACE_OPERATIONS.copy(),
        "codex_stages": CODEX_STAGES.copy(),
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
    try:
        budget = Decimal(str(external.get("budget_usd")))
        spent = Decimal(str(external.get("spent_usd")))
    except Exception as exc:
        raise AuthorizationError("Saved Claude/Aily budget is invalid.") from exc
    if (not budget.is_finite() or budget < Decimal("0.01")
            or budget != budget.quantize(Decimal("0.01"))
            or not spent.is_finite() or spent < 0):
        raise AuthorizationError("Saved Claude/Aily budget or spending is invalid.")
    if authorization.get("scope_sha256") != authorization_scope_digest(authorization):
        raise AuthorizationError("Saved workflow authorization changed after initialization.")
    return authorization

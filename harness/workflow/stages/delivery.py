from __future__ import annotations

import copy
import hashlib
import json

from workflow import planning_review as _planning_review
from workflow.errors import WorkflowError
from workflow.review import _safe_review_path
from workflow.engine import StageInput, StageResult


def _execute_delivery(stage_input: StageInput, services) -> StageResult:
    delivery_verification = services.verify_delivery_integrity()
    services.capture_final_target_fingerprint()
    final_target = services.state.get("execution_fingerprint", {}).get("final", {}).get("target", {})
    if (final_target.get("status") != "captured"
            or final_target.get("commit_sha") != delivery_verification.get("target_commit_sha")
            or final_target.get("working_tree_sha256") != delivery_verification.get("target_working_tree_sha256")):
        raise WorkflowError("Delivery is blocked because the target changed during final integrity checks.")
    services.evaluate_commitment_delivery()
    services.atomic_text(services.delivery_path, services.delivery_report())
    delivery_hash = hashlib.sha256(services.read_bytes(services.delivery_path)).hexdigest()
    services.state.setdefault("integrity", {})["delivery_artifact_sha256"] = delivery_hash
    services.save()
    return StageResult(output={"delivered": True})


def _execute_profile_delivery(stage_input: StageInput, services) -> StageResult:
    """Write a delivery artifact for profiles without proposal-ledger traceability."""
    if services.state.get("traceability_required") is True:
        return _execute_delivery(stage_input, services)
    verification = services.state.get("integrity", {}).get("verification_result", {})
    if not isinstance(verification, dict) or verification.get("status") != "verified":
        raise WorkflowError("Delivery is blocked because final verification has not passed.")
    services.capture_final_target_fingerprint()
    fingerprint = services.state.get("execution_fingerprint", {}).get("final", {}).get("target", {})
    if fingerprint.get("status") != "captured":
        raise WorkflowError("Delivery is blocked because the final target fingerprint is unavailable.")
    state = services.state
    content = "\n".join((
        "# Workflow delivery",
        "",
        f"- Workflow: {state.get('workflow_name')}",
        f"- Workflow ID: {state.get('workflow_id')}",
        f"- Target: {state.get('target')}",
        f"- Completed: {services.now()}",
        "",
        "## Assignment",
        "",
        str(state.get("task") or ""),
        "",
        "## Implementation summary",
        "",
        str(state.get("implementation_summary") or "No implementation stage ran."),
        "",
        "## Automated test evidence",
        "",
        "\n".join(
            f"- {item.get('runner')}: exit {item.get('exit_code')}; event {item.get('event_id')}"
            for item in state.get("test_evidence", []) if isinstance(item, dict)
        ) or "No separate automated test stage was configured.",
        "",
        "## Independent verification",
        "",
        str(state.get("verification_report") or "Verification passed."),
        "",
    ))
    services.atomic_text(services.delivery_path, content)
    services.state.setdefault("integrity", {})["delivery_artifact_sha256"] = hashlib.sha256(
        services.read_bytes(services.delivery_path),
    ).hexdigest()
    services.state["delivery_disposition"] = "complete"
    services.save()
    return StageResult(output={"delivered": True})


def _execute_review_report(stage_input: StageInput, services) -> StageResult:
    """Persist a read-only review result under workflow metadata, never in source files."""
    review = services.state.get("code_review")
    if not isinstance(review, str) or not review.strip():
        raise WorkflowError("Review report cannot be written because the review result is missing.")
    evidence = services.state.get("integrity", {}).get("review_evidence", {})
    fingerprint = services.fingerprint_repository(services.target)
    if (not isinstance(evidence, dict) or fingerprint.get("status") != "captured"
            or fingerprint.get("commit_sha") != evidence.get("target_commit_sha")
            or fingerprint.get("working_tree_sha256") != evidence.get("target_working_tree_sha256")):
        raise WorkflowError("Review report is blocked because the target changed during review.")
    services.capture_final_target_fingerprint()
    content = "\n".join((
        "# Review report",
        "",
        f"- Workflow ID: {services.state.get('workflow_id')}",
        f"- Target: {services.state.get('target')}",
        f"- Captured: {services.now()}",
        "",
        "## Assignment",
        "",
        str(services.state.get("task") or ""),
        "",
        "## Reviewed changes",
        "",
        "\n".join(f"- {path}" for path in services.state.get("review_included_files", [])) or "No files were included.",
        "",
        "## Findings",
        "",
        review,
        "",
    ))
    services.atomic_text(services.delivery_path, content)
    services.state.setdefault("integrity", {})["delivery_artifact_sha256"] = hashlib.sha256(
        services.read_bytes(services.delivery_path),
    ).hexdigest()
    services.state["delivery_disposition"] = "review_complete"
    services.save()
    return StageResult(output={"report_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()})

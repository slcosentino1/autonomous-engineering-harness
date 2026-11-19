from __future__ import annotations

import copy
import hashlib
import json

from workflow import planning_review as _planning_review
from workflow.errors import WorkflowError
from workflow.review import _safe_review_path
from workflow.engine import StageInput
from workflow.stage_services import ProductionStageServices


def _execute_code_review_or_verification(stage_input: StageInput, services, stage: str) -> None:
    purpose = "verification" if stage == "verification" else "initial"
    services.require_write_operation_target(purpose)
    review_diff = services.review_diff(services.state, services.target)
    if not review_diff.included_paths and not review_diff.context_files:
        services.state["review_included_files"] = []
        services.state["review_exclusions"] = [
            {"path": _safe_review_path(path), "reason": reason}
            for path, reason in review_diff.excluded_files
        ]
        services.state["review_files_not_reviewed"] = []
        services.save()
        reasons = "; ".join(
            f"{path}: {reason}" for path, reason in review_diff.excluded_files
        )
        detail = f"No reviewable text changes were found. Excluded changed files: {reasons or 'none'}"
        raise WorkflowError(detail)
    try:
        campaign, shared, parts = services.prepare_review_campaign(review_diff, purpose=purpose)
    except WorkflowError as exc:
        if not services.state.get("review_campaign"):
            services.state["review_included_files"] = list(review_diff.included_paths)
            services.state["review_exclusions"] = [
                {"path": _safe_review_path(path), "reason": reason}
                for path, reason in review_diff.excluded_files
            ]
            services.state["review_files_not_reviewed"] = [
                {"path": path, "reason": services.redact(str(exc))}
                for path in review_diff.included_paths
            ]
            services.save()
        raise
    services.state["review_scope_files"] = sorted({
        *review_diff.included_paths,
        *(path for path, _origin, _baseline, _final in review_diff.context_files),
    })
    services.state["review_workflow_paths"] = list(review_diff.workflow_paths)
    services.state["review_scope_sha256"] = services.campaign_source_hash(review_diff)
    if purpose == "initial" and not isinstance(services.state.get("implementation_snapshot"), dict):
        implementation_snapshot = services.capture_preexisting_snapshot(
            services.target, review_diff.included_paths, redactor=services.redact,
        )
        implementation_snapshot["source_patch_sha256"] = services.state["review_scope_sha256"]
        implementation_snapshot["target_fingerprint"] = services.fingerprint_repository(services.target)
        snapshot_body = {
            key: value for key, value in implementation_snapshot.items() if key != "sha256"
        }
        implementation_snapshot["sha256"] = hashlib.sha256(json.dumps(
            snapshot_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        services.state["implementation_snapshot"] = implementation_snapshot
    services.save()
    report = services.run_review_campaign(
        campaign, shared, parts, review_diff,
    )
    campaign["purpose"] = purpose
    if purpose == "verification":
        services.state["verification_report"] = report
    else:
        services.state["code_review"] = report
    services.capture_commitment_assessment(report, purpose=purpose)
    if purpose == "verification":
        services.state["verification_context_sha256"] = campaign.get("shared_context_sha256")
    else:
        services.state["code_review_context_sha256"] = campaign.get("shared_context_sha256")
    review_evidence = "\n\n".join([
        report,
        *(record.get("report", "") for record in campaign.get("part_reviews", [])),
    ])
    if campaign.get("status") != "complete" or campaign.get("files_not_reviewed"):
        raise WorkflowError("Review did not complete all required files and parts; delivery is blocked.")
    no_blocking_findings = services.clearly_no_review_findings(review_evidence)
    if purpose == "initial" and no_blocking_findings:
        services.state["correction_summary"] = (
            "Claude reported no acceptance-blocking findings; no correction run was needed."
        )
    verification_dispositions = None
    if purpose == "verification" and no_blocking_findings:
        history = services.state.get("review_campaign_history", [])
        original_campaign = next((
            item for item in reversed(history)
            if isinstance(item, dict) and item.get("purpose") == "initial"
        ), {})
        verification_dispositions, disposition_error = services.post_correction_dispositions(
            original_campaign, campaign,
        )
        if disposition_error is not None:
            services.state.setdefault("integrity", {})["verification_result"] = {
                "status": "failed",
                "campaign_id": campaign.get("campaign_id"),
                "checked_at": services.now(),
                "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
                "finding": disposition_error,
            }
            services.save()
            raise WorkflowError(
                "Independent post-correction verification is inconclusive; delivery is blocked. "
                + disposition_error
            )
    services.record_review_snapshot(campaign, review_diff, purpose=purpose)
    if purpose == "verification":
        integrity = services.state.setdefault("integrity", {})
        if no_blocking_findings:
            integrity["verification_result"] = {
                "status": "verified",
                "campaign_id": campaign.get("campaign_id"),
                "original_campaign_id": (services.state.get("correction_attempts") or [{}])[-1].get(
                    "original_campaign_id",
                ),
                "checked_at": services.now(),
                "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
                "finding": "No unresolved blocking findings were reported.",
                "original_finding_dispositions": verification_dispositions,
            }
            services.state["stage"] = "complete"
        else:
            integrity["verification_result"] = {
                "status": "failed",
                "campaign_id": campaign.get("campaign_id"),
                "checked_at": services.now(),
                "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
                "finding": "The independent verification reports unresolved or new blocking findings.",
            }
            services.save()
            raise WorkflowError(
                "Independent post-correction verification found unresolved or new blocking findings; delivery is blocked."
            )
    elif no_blocking_findings:
        services.state["stage"] = "complete"
    else:
        services.state["stage"] = "correction"
    services.save()

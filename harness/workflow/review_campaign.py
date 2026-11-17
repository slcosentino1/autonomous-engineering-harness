"""Pure ReviewCampaign snapshot evolution and next-action decisions."""

from __future__ import annotations

import copy
import hashlib
import json

from workflow.review_models import (
    CampaignInput, CampaignPart, CampaignSnapshot, CampaignTransition, PreviousResult,
)
from workflow.review_partitioning import campaign_part_manifest, review_patch_sha256
from workflow.review_requests import build_integration_request, build_part_request
from workflow.review_validation import (
    PART_REVIEW_VALIDATOR_VERSION, _integration_report_is_structured,
    _review_sha256_text, validate_part_review_report,
)


def make_campaign_input(
    campaign: dict,
    shared_context: dict[str, str],
    parts: list[dict],
    excluded_files: tuple[tuple[str, str], ...],
    *,
    proposal_version: int,
    transition_at: str,
    part_header: str | None = None,
) -> CampaignInput:
    from workflow.review_requests import review_header

    return CampaignInput(
        campaign_id=campaign["campaign_id"],
        purpose=campaign.get("purpose", "initial"),
        source_patch_sha256=campaign.get("source_patch_sha256", ""),
        shared_context_sha256=campaign.get("shared_context_sha256", ""),
        parts=tuple(CampaignPart(part["part_id"], tuple(part["segments"])) for part in parts),
        shared_context=tuple(shared_context.items()),
        excluded_files=excluded_files,
        payload_limit_characters=campaign.get("payload_limit_characters", 1),
        proposal_version=proposal_version,
        report_contract_version=campaign.get("report_contract_version", PART_REVIEW_VALIDATOR_VERSION),
        part_header=review_header() if part_header is None else part_header,
        transition_at=transition_at,
    )


def create_campaign_snapshot(
    *,
    campaign_id: str,
    created_at: str,
    payload_limit_characters: int,
    source_patch_sha256: str,
    shared_context_sha256: str,
    required_calls: int,
    authorized_call_limit: int | None,
    parts: list[dict],
    integration_required: bool,
    included_paths: tuple[str, ...],
    excluded_files: tuple[tuple[str, str], ...],
    report_contract_version: str,
) -> dict:
    """Build the existing persisted campaign mapping from explicit values."""
    return {
        "campaign_id": campaign_id,
        "header_id": "code-review-part-v2",
        "report_contract_version": report_contract_version,
        "status": "planned",
        "strategy": "multi_part_with_integration" if integration_required else "single_call",
        "created_at": created_at,
        "payload_limit_characters": payload_limit_characters,
        "partition_limit_characters": payload_limit_characters,
        "source_patch_sha256": source_patch_sha256,
        "shared_context_sha256": shared_context_sha256,
        "required_calls": required_calls,
        "authorized_call_limit": authorized_call_limit,
        "parts": copy.deepcopy(parts),
        "part_reviews": [
            {"part_id": part["part_id"], "status": "pending", "report": None, "calls": []}
            for part in parts
        ],
        "integration_review": {
            "required": integration_required,
            "status": "pending" if integration_required else "not_required",
            "report": None,
            "calls": [],
        },
        "files_reviewed": [],
        "files_not_reviewed": [
            {"path": path, "reason": "assigned to a pending review part"}
            for path in included_paths
        ],
        "excluded_files": [
            {"path": _safe_path(path), "reason": reason} for path, reason in excluded_files
        ],
        "consolidated_report": None,
    }


def block_campaign_for_authorization(campaign: dict, included_paths: tuple[str, ...]) -> dict:
    result = copy.deepcopy(campaign)
    result["status"] = "blocked_authorization"
    result["files_not_reviewed"] = [
        {"path": path, "reason": "campaign exceeds its persisted Claude code-review call authorization"}
        for path in included_paths
    ]
    return result


def sync_campaign_coverage(campaign: dict, excluded_files: tuple[tuple[str, str], ...]) -> dict:
    """Return an updated campaign coverage snapshot without mutating the supplied mapping."""
    result = copy.deepcopy(campaign)
    reviewed_segments = {
        (
            segment.get("path"), segment.get("patch_line_start"),
            segment.get("patch_line_end"), segment.get("patch_sha256"),
        )
        for part in result["part_reviews"] if part.get("status") == "reviewed"
        for segment in part.get("segments", [])
    }
    if not any(item.get("segments") for item in result["part_reviews"]):
        by_id = {item["part_id"]: item for item in result["part_reviews"]}
        for part in result["parts"]:
            by_id[part["part_id"]]["segments"] = copy.deepcopy(part["segments"])
        reviewed_segments = {
            (
                segment.get("path"), segment.get("patch_line_start"),
                segment.get("patch_line_end"), segment.get("patch_sha256"),
            )
            for part in result["part_reviews"] if part.get("status") == "reviewed"
            for segment in part.get("segments", [])
        }
    expected_by_path: dict[str, set[tuple[str, int, int, str]]] = {}
    for part in result["parts"]:
        for segment in part["segments"]:
            identity = (
                segment["path"], segment["patch_line_start"],
                segment["patch_line_end"], segment["patch_sha256"],
            )
            expected_by_path.setdefault(segment["path"], set()).add(identity)
    result["files_reviewed"] = sorted(
        path for path, expected in expected_by_path.items() if expected <= reviewed_segments
    )
    result["files_not_reviewed"] = [
        {
            "path": path,
            "reason": next((
                f"review part {part['part_id']} is {part['status']}"
                for part in result["part_reviews"]
                if any(segment["path"] == path for segment in part.get("segments", []))
                and part["status"] != "reviewed"
            ), "one or more patch segments have not been reviewed"),
        }
        for path in sorted(expected_by_path) if path not in result["files_reviewed"]
    ]
    result["excluded_files"] = [
        {"path": _safe_path(path), "reason": reason} for path, reason in excluded_files
    ]
    return result


def _safe_path(path: str) -> str:
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(path, ensure_ascii=True)
    return path


def _part_by_id(parts: tuple[CampaignPart, ...], part_id: str) -> CampaignPart | None:
    return next((item for item in parts if item.part_id == part_id), None)


def _validate_snapshot(snapshot: dict, campaign_input: CampaignInput) -> str | None:
    if snapshot.get("campaign_id") != campaign_input.campaign_id:
        return "campaign ID does not match the immutable campaign input"
    if (campaign_input.source_patch_sha256
            and snapshot.get("source_patch_sha256") != campaign_input.source_patch_sha256):
        return "campaign source hash does not match the immutable campaign input"
    if snapshot.get("shared_context_sha256") != campaign_input.shared_context_sha256:
        return "campaign shared-context hash does not match the immutable campaign input"
    if snapshot.get("parts") != campaign_part_manifest([
        {"part_id": part.part_id, "segments": list(part.segments)} for part in campaign_input.parts
    ]):
        return "campaign part manifest does not match the immutable campaign input"
    expected_contract = snapshot.get("report_contract_version")
    if expected_contract and expected_contract != campaign_input.report_contract_version:
        return "campaign report contract does not match the immutable campaign input"
    if (snapshot.get("purpose") is not None
            and snapshot.get("purpose") != campaign_input.purpose):
        return "campaign purpose does not match the immutable campaign input"
    if (snapshot.get("payload_limit_characters") is not None
            and snapshot.get("payload_limit_characters") != campaign_input.payload_limit_characters):
        return "campaign payload limit does not match the immutable campaign input"
    checkpoints = snapshot.get("part_reviews")
    if not isinstance(checkpoints, list):
        return "campaign part checkpoints must be a list"
    checkpoint_ids = [
        item.get("part_id") for item in checkpoints if isinstance(item, dict)
    ]
    if len(checkpoint_ids) != len(checkpoints) or any(
        not isinstance(part_id, str) or not part_id.strip() for part_id in checkpoint_ids
    ):
        return "campaign contains a malformed part checkpoint"
    if len(checkpoint_ids) != len(set(checkpoint_ids)):
        return "campaign contains duplicate part checkpoint IDs"
    expected_ids = [part.part_id for part in campaign_input.parts]
    if len(checkpoint_ids) != len(expected_ids) or set(checkpoint_ids) != set(expected_ids):
        return "campaign part checkpoints do not exactly match the immutable part manifest"
    integration = snapshot.get("integration_review")
    if not isinstance(integration, dict):
        return "campaign integration checkpoint must be an object"
    return None


def _append_call(record: dict, result: PreviousResult) -> bool:
    call = {
        "prompt_sha256": result.prompt_sha256,
        "cost_usd": result.cost_usd,
        "call_state": result.call_state,
        "campaign_call_number": result.campaign_call_number,
    }
    calls = record.setdefault("calls", [])
    if result.recovered:
        same_attempt = [
            item for item in calls
            if isinstance(item, dict) and item.get("campaign_call_number") == result.campaign_call_number
        ]
        if same_attempt:
            return len(same_attempt) == 1 and same_attempt[0] == call
    calls.append(call)
    return True


def _apply_result(
    campaign: dict, campaign_input: CampaignInput, result: PreviousResult,
    expected_identity,
) -> tuple[str | None, tuple[str, ...]]:
    if result.request_identity != expected_identity:
        return "invalid_result", ("provider result identity does not match the outstanding review request",)
    if result.part_id != expected_identity.part_id:
        return "invalid_result", ("provider result part ID does not match its request identity",)
    if not result.recovered and result.prompt_sha256 != expected_identity.prompt_sha256:
        return "invalid_result", ("provider result prompt hash does not match its request identity",)
    if not result.recovered and (not isinstance(result.operation_id, str) or not result.operation_id):
        return "invalid_result", ("provider result has no execution operation identity",)
    if result.outcome == "completed":
        if result.call_state != "completed" or not isinstance(result.report, str):
            return "invalid_result", ("completed provider result has no terminal report",)
        report_hash = hashlib.sha256(result.report.encode("utf-8")).hexdigest()
        if result.result_sha256 != report_hash:
            return "invalid_result", ("provider result hash does not match its report",)
    if result.outcome == "uncertain":
        return "uncertain_result", ("provider outcome is uncertain; the capability cannot authorize a replay",)
    if result.part_id == "integration":
        record = campaign.get("integration_review")
        if not isinstance(record, dict):
            return "invalid_result", ("integration result has no matching campaign record",)
        if not _append_call(record, result):
            return "invalid_result", ("recovered integration attempt conflicts with its saved call checkpoint",)
        if result.outcome != "completed" or result.report is None:
            record["status"] = "call_failed"
            return "provider_failed", ()
        record["report"] = result.report
        record["context_sha256"] = result.prompt_sha256
        record["status"] = "reviewed" if _integration_report_is_structured(result.report) else "invalid_report"
        if record["status"] == "invalid_report":
            return "invalid_report", ("Claude returned an incomplete integration report.",)
        return None, ()

    part = _part_by_id(campaign_input.parts, result.part_id)
    record = next((item for item in campaign.get("part_reviews", []) if item.get("part_id") == result.part_id), None)
    if part is None or not isinstance(record, dict):
        return "invalid_result", ("part result does not match the immutable campaign manifest",)
    if not _append_call(record, result):
        return "invalid_result", ("recovered part attempt conflicts with its saved call checkpoint",)
    if result.outcome != "completed" or result.report is None:
        record["status"] = "call_failed"
        record["segments"] = campaign_part_manifest([{"part_id": part.part_id, "segments": list(part.segments)}])[0]["segments"]
        return "provider_failed", ()
    record["report"] = result.report
    record["context_sha256"] = result.prompt_sha256
    validation = validate_part_review_report(result.report, part.part_id, list(part.segments))
    record["status"] = "reviewed" if validation.valid else "invalid_report"
    record.setdefault("validation_history", []).append({
        "validator_version": PART_REVIEW_VALIDATOR_VERSION,
        "validated_at": result.received_at,
        "valid": validation.valid,
        "errors": list(validation.errors),
        "report_sha256": _review_sha256_text(result.report),
    })
    record["segments"] = campaign_part_manifest([{"part_id": part.part_id, "segments": list(part.segments)}])[0]["segments"]
    if not validation.valid:
        return "invalid_report", validation.errors
    return None, ()


def _revalidate_saved_reports(campaign: dict, campaign_input: CampaignInput) -> tuple[str | None, tuple[str, ...]]:
    for part in campaign_input.parts:
        record = next((item for item in campaign.get("part_reviews", []) if item.get("part_id") == part.part_id), None)
        if not isinstance(record, dict) or record.get("status") == "reviewed":
            continue
        if record.get("status") not in {"report_saved", "invalid_report"}:
            return None, ()
        report = record.get("report")
        if not report:
            return None, ()
        validation = validate_part_review_report(report, part.part_id, list(part.segments))
        previous_status = record["status"]
        record["status"] = "reviewed" if validation.valid else "invalid_report"
        record.setdefault("validation_history", []).append({
            "validator_version": PART_REVIEW_VALIDATOR_VERSION,
            "validated_at": campaign_input.transition_at,
            "previous_status": previous_status,
            "valid": validation.valid,
            "errors": list(validation.errors),
            "report_sha256": _review_sha256_text(report),
        })
        record["segments"] = campaign_part_manifest([{"part_id": part.part_id, "segments": list(part.segments)}])[0]["segments"]
        return None, ()
    integration = campaign.get("integration_review")
    if isinstance(integration, dict) and integration.get("status") == "report_saved" and integration.get("report"):
        integration["status"] = (
            "reviewed" if _integration_report_is_structured(integration["report"]) else "invalid_report"
        )
    return None, ()


def advance(
    snapshot: CampaignSnapshot,
    campaign_input: CampaignInput,
    previous_result: PreviousResult | None = None,
) -> CampaignTransition:
    """Evolve one campaign snapshot and express its next deterministic action."""
    if snapshot.contract_version != 1:
        return CampaignTransition(
            snapshot, "blocked", reason="unsupported_snapshot_version",
            findings=(f"unsupported campaign snapshot contract version: {snapshot.contract_version}",),
        )
    campaign = snapshot.to_mapping()
    mismatch = _validate_snapshot(campaign, campaign_input)
    if mismatch:
        return CampaignTransition(snapshot, "blocked", reason="input_mismatch", findings=(mismatch,))

    if previous_result is not None:
        expected = _outstanding_request(campaign, campaign_input)
        if expected is None:
            return CampaignTransition(
                snapshot, "blocked", reason="invalid_result",
                findings=("campaign has no outstanding request for the provider result",),
            )
        reason, findings = _apply_result(campaign, campaign_input, previous_result, expected.identity)
        if reason == "uncertain_result":
            return CampaignTransition(snapshot, "blocked", reason=reason, findings=findings)
        if reason == "invalid_result":
            return CampaignTransition(snapshot, "blocked", reason=reason, findings=findings)
        campaign = sync_campaign_coverage(campaign, campaign_input.excluded_files)
        evolved = CampaignSnapshot.from_mapping(campaign)
        if reason:
            return CampaignTransition(evolved, "blocked", part_id=previous_result.part_id,
                                      reason=reason, findings=findings)
        snapshot = evolved
    else:
        campaign = copy.deepcopy(campaign)
        _revalidate_saved_reports(campaign, campaign_input)
        campaign = sync_campaign_coverage(campaign, campaign_input.excluded_files)
        snapshot = CampaignSnapshot.from_mapping(campaign)

    campaign = snapshot.to_mapping()
    for part in campaign_input.parts:
        record = next((item for item in campaign.get("part_reviews", []) if item.get("part_id") == part.part_id), None)
        if not isinstance(record, dict):
            return CampaignTransition(snapshot, "blocked", reason="invalid_snapshot",
                                      findings=(f"campaign has no checkpoint for {part.part_id}",))
        if record.get("status") == "reviewed":
            continue
        request = build_part_request(
            campaign_id=campaign_input.campaign_id, part=part, purpose=campaign_input.purpose,
            proposal_version=campaign_input.proposal_version,
            payload_limit_characters=campaign_input.payload_limit_characters,
            report_contract_version=campaign_input.report_contract_version,
            source_patch_sha256=campaign_input.source_patch_sha256,
            shared_context_sha256=campaign_input.shared_context_sha256,
            shared_context=dict(campaign_input.shared_context),
            excluded_count=len(campaign_input.excluded_files), header=campaign_input.part_header,
        )
        return CampaignTransition(snapshot, "request_part", request=request, part_id=part.part_id)

    if campaign.get("files_not_reviewed"):
        return CampaignTransition(
            snapshot, "blocked", reason="coverage_incomplete",
            findings=("campaign cannot advance while changed files remain unreviewed",),
        )
    integration = campaign.get("integration_review", {})
    if not integration.get("required"):
        campaign["status"] = "complete"
        campaign["integration_review"]["status"] = "not_required"
        campaign["consolidated_report"] = campaign["part_reviews"][0].get("report") if campaign["part_reviews"] else None
        completed = CampaignSnapshot.from_mapping(campaign)
        return CampaignTransition(completed, "complete")
    if integration.get("status") == "reviewed":
        campaign["status"] = "complete"
        campaign["consolidated_report"] = integration.get("report")
        completed = CampaignSnapshot.from_mapping(campaign)
        return CampaignTransition(completed, "complete")
    request = build_integration_request(
        campaign=campaign, parts=campaign_input.parts,
        shared_context=dict(campaign_input.shared_context), purpose=campaign_input.purpose,
        proposal_version=campaign_input.proposal_version,
        payload_limit_characters=campaign_input.payload_limit_characters,
        report_contract_version=campaign_input.report_contract_version,
        source_patch_sha256=campaign_input.source_patch_sha256,
        shared_context_sha256=campaign_input.shared_context_sha256,
    )
    return CampaignTransition(snapshot, "request_integration", request=request, part_id="integration")


def _outstanding_request(campaign: dict, campaign_input: CampaignInput):
    """Reconstruct the one request a result is permitted to answer."""
    checkpoint_by_id = {item["part_id"]: item for item in campaign.get("part_reviews", [])}
    for part in campaign_input.parts:
        record = checkpoint_by_id[part.part_id]
        if record.get("status") == "reviewed":
            continue
        return build_part_request(
            campaign_id=campaign_input.campaign_id, part=part, purpose=campaign_input.purpose,
            proposal_version=campaign_input.proposal_version,
            payload_limit_characters=campaign_input.payload_limit_characters,
            report_contract_version=campaign_input.report_contract_version,
            source_patch_sha256=campaign_input.source_patch_sha256,
            shared_context_sha256=campaign_input.shared_context_sha256,
            shared_context=dict(campaign_input.shared_context),
            excluded_count=len(campaign_input.excluded_files), header=campaign_input.part_header,
        )
    integration = campaign.get("integration_review", {})
    if (integration.get("required") and integration.get("status") != "reviewed"
            and not campaign.get("files_not_reviewed")
            and all(checkpoint_by_id[part.part_id].get("status") == "reviewed"
                    for part in campaign_input.parts)):
        return build_integration_request(
            campaign=campaign, parts=campaign_input.parts,
            shared_context=dict(campaign_input.shared_context), purpose=campaign_input.purpose,
            proposal_version=campaign_input.proposal_version,
            payload_limit_characters=campaign_input.payload_limit_characters,
            report_contract_version=campaign_input.report_contract_version,
            source_patch_sha256=campaign_input.source_patch_sha256,
            shared_context_sha256=campaign_input.shared_context_sha256,
        )
    return None

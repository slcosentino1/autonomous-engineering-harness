"""Review contracts, prompt helpers, code-review campaigns, and local recovery."""

from __future__ import annotations

import hashlib
import copy
import json
import re
import uuid
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import claude_reviewer

from workflow.review_models import (
    CampaignInput, CampaignSnapshot, CampaignTransition, PreviousResult,
    ReviewCampaignError, ReviewDiff, ReviewSegment,
)
from workflow.review_campaign import (
    advance as _advance_review_campaign,
    block_campaign_for_authorization as _block_campaign_for_authorization,
    create_campaign_snapshot as _create_campaign_snapshot,
    make_campaign_input as _make_campaign_input,
    sync_campaign_coverage as _sync_campaign_coverage,
)
from workflow.review_requests import (
    format_context_prompt as _format_context_prompt, part_coverage as _part_coverage,
    redact_review_text as _review_redact,
    legacy_review_header, part_sections, review_header, serialized_review_fits,
)
from workflow.review_partitioning import (
    campaign_part_manifest as _campaign_part_manifest,
    campaign_source_hash as _campaign_source_hash,
    path_affinity as _path_affinity,
    review_partition_error as _review_partition_error,
    review_patch_sha256 as _review_patch_sha256,
    safe_patch_boundaries as _safe_patch_boundaries,
    validate_review_partition as _validate_review_partition,
    review_segment as _review_segment,
    split_oversized_segment as _split_oversized_segment,
    partition_review_segments as _partition_review_segments,
)
from workflow.review_validation import (
    PART_REVIEW_FIELDS, PART_REVIEW_SECTIONS, PART_REVIEW_SEVERITIES,
    PART_REVIEW_VALIDATOR_VERSION, _integration_report_is_structured, _normalize_review_text,
    _review_sha256_text, _validated_campaign_reports, parse_part_review_report,
    render_review_feedback,
)



CLAUDE_MAX_PAYLOAD_ENV = claude_reviewer.MAX_REVIEW_PAYLOAD_ENV




def _review_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_review_path(relative: str) -> str:
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(relative, ensure_ascii=True)
    return relative






CLAUDE_CODE_REVIEW_GUIDANCE = """Independent review roles and duties
Act as a Senior Software Architect, Senior Backend Engineer, Product Engineer, and independent senior reviewer.
Do not assume the approved proposal is correct merely because it was approved. Verify the actual implementation
against the assignment and challenge assumptions where evidence warrants it.

Evaluate requirements and product behavior; architecture and boundaries; design and maintainability; engineering
judgment and proportionality; relevant reliability and operational behavior; security; and test quality. Evaluate
the architecture that exists in context. Do not prefer Clean, Hexagonal, Microservices, CQRS, or another style by
default. Ask what concrete problem each significant decision solves, whether that problem exists here, what trade-off
it creates, and whether a simpler design would satisfy the requirement. Do not demand lengthy justification for
trivial details. Treat timeout, retry, concurrency, recovery, and scale concerns as findings only when relevant to
the actual requirements and execution model.

Use explicit requirements as the acceptance baseline. Separate confirmed defects from risks needing verification
and optional improvements. Do not report a style preference as a defect. Every finding must explain what is wrong,
why it matters in this assignment, the affected requirement or engineering property, and a concrete correction.
Classify severity as Must fix, Should fix, or Optional improvement; optional improvements do not block acceptance.
Use category Requirements, Architecture, Design, Correctness, Reliability, Security, Testing, or Integration.

For individual parts, assess local responsibilities, cohesion, coupling, dependency direction, contracts, tests, and
failure handling. Put concerns that depend on another part under Cross-component concerns and state what evidence
the integration review should verify. For final integration, synthesize all part reports; inspect boundaries,
contracts, data flow, assumptions, error/state behavior, duplicate responsibilities, and cross-component defects.
Do not merely concatenate reports. Consolidate duplicates while preserving references and introduce new integration
findings only when supported by the shared context and part reports.

Return the requested structured report. Each finding must include severity, category, file and location when known,
description, assignment-specific impact and reasoning, and recommended correction. Finish with a concise overall
assessment of requirement satisfaction, architecture/design quality, main risks, proportionality, and whether any
finding blocks acceptance. Only Must fix findings or an explicit CHANGES_REQUESTED decision should trigger the
correction stage; Should fix and Optional improvements must be reasoned about and remain advisory unless the report
explicitly identifies an acceptance-blocking defect."""


CLAUDE_CODE_REVIEW_GUIDANCE = """Independent review roles and duties
Act as a Senior Software Architect, Senior Backend Engineer, Product Engineer, and independent senior reviewer.
Do not assume the approved proposal is correct merely because it was approved. Verify the actual implementation
against the assignment and challenge assumptions where evidence warrants it.

Evaluate requirements and product behavior; architecture and boundaries; design and maintainability; engineering
judgment and proportionality; relevant reliability and operational behavior; security; and test quality. Evaluate
the architecture that exists in context. Do not prefer Clean, Hexagonal, Microservices, CQRS, or another style by
default. Ask what concrete problem each significant decision solves, whether that problem exists here, what trade-off
it creates, and whether a simpler design would satisfy the requirement. Do not demand lengthy justification for
trivial details. Treat timeout, retry, concurrency, recovery, and scale concerns as findings only when relevant to
the actual requirements and execution model.

Use explicit requirements as the acceptance baseline. Separate confirmed defects from risks needing verification
and optional improvements. Do not report a style preference as a defect. Every finding must explain what is wrong,
why it matters in this assignment, the affected requirement or engineering property, and a concrete correction.
Classify severity as Must fix, Should fix, or Optional improvement; optional improvements do not block acceptance.
Use category Requirements, Architecture, Design, Correctness, Reliability, Security, Testing, or Integration.

For individual parts, assess local responsibilities, cohesion, coupling, dependency direction, contracts, tests, and
failure handling. Put concerns that depend on another part under Cross-component concerns and state what evidence
the integration review should verify. For final integration, synthesize all part reports; inspect boundaries,
contracts, data flow, assumptions, error/state behavior, duplicate responsibilities, and cross-component defects.
Do not merely concatenate reports. Consolidate duplicates while preserving references and introduce new integration
findings only when supported by the shared context and part reports.

Return the requested structured report. Each finding must include severity, category, file and location when known,
description, assignment-specific impact and reasoning, and recommended correction. Finish with a concise overall
assessment of requirement satisfaction, architecture/design quality, main risks, proportionality, and whether any
finding blocks acceptance. Only Must fix findings or an explicit CHANGES_REQUESTED decision should trigger the
correction stage; Should fix and Optional improvements must be reasoned about and remain advisory unless the report
explicitly identifies an acceptance-blocking defect."""

class ReviewCampaign:
    """Compatibility runner for campaign persistence and provider callbacks.

    Deterministic requests, validation, coverage, and snapshot transitions live in
    the dedicated pure review modules. This adapter retains the current workflow
    save and provider ordering until the Coordinator recovery extraction phase.
    """

    def __init__(
        self,
        state: dict,
        *,
        save_state: Callable[[], None] | None = None,
        request_review: Callable[..., tuple[str, dict]] | None = None,
        authorized_call_limit: int | None = None,
        authorize_call_count: Callable[[int], bool] | None = None,
        trace_span: Callable[[str, dict], object] | None = None,
    ):
        self.state = state
        self._save = save_state
        self._call_claude = request_review
        self.authorized_call_limit = authorized_call_limit
        self._authorize_call_count_callback = authorize_call_count
        self._trace_span_callback = trace_span

    def _trace_context(self, name: str, metadata: dict):
        if self._trace_span_callback is None:
            return nullcontext()
        try:
            return self._trace_span_callback(name, metadata)
        except Exception:
            return nullcontext()

    def _authorize_call_count(self, required_calls: int) -> bool:
        if self._authorize_call_count_callback is None:
            raise ReviewCampaignError("Review campaign authorization was not supplied by the workflow coordinator.")
        return self._authorize_call_count_callback(required_calls)

    def _require_save(self) -> None:
        if self._save is None:
            raise ReviewCampaignError("Review campaign persistence was not supplied by the workflow coordinator.")

    def _require_review_call(self) -> None:
        if self._call_claude is None:
            raise ReviewCampaignError("Claude review execution was not supplied by the workflow coordinator.")

    @staticmethod
    def _code_review_header() -> str:
        return review_header()

    @staticmethod
    def _legacy_code_review_header() -> str:
        return legacy_review_header()

    @staticmethod
    def _review_segment(
        path: str, patch: str, start: int = 1, end: int | None = None, total: int | None = None,
    ) -> ReviewSegment:
        return _review_segment(path, patch, start, end, total)

    @staticmethod
    def _part_coverage(segments: list[ReviewSegment], *, excluded_count: int) -> str:
        return _part_coverage(segments, excluded_count=excluded_count)

    def _part_sections(
        self, shared: dict[str, str], segments: list[ReviewSegment], *, campaign_id: str,
        part_id: str, excluded_count: int,
    ) -> dict[str, str]:
        return part_sections(
            shared, segments, campaign_id=campaign_id, part_id=part_id,
            proposal_version=self.state.get("proposal_version", 1), excluded_count=excluded_count,
        )

    @staticmethod
    def _serialized_review_fits(header: str, sections: dict[str, str], limit: int) -> bool:
        return serialized_review_fits(header, sections, limit)

    def _split_oversized_segment(
        self, path: str, patch: str, *, shared: dict[str, str], campaign_id: str,
        limit: int, excluded_count: int, header: str | None = None,
    ) -> list[ReviewSegment]:
        return _split_oversized_segment(
            path, patch, shared=shared, campaign_id=campaign_id,
            proposal_version=self.state.get("proposal_version", 1), limit=limit,
            excluded_count=excluded_count, header=header,
        )

    def _partition_review_segments(
        self, review_diff: ReviewDiff, shared: dict[str, str], *, campaign_id: str,
        limit: int, header: str | None = None,
    ) -> tuple[list[dict], bool]:
        return _partition_review_segments(
            review_diff, shared, campaign_id=campaign_id,
            proposal_version=self.state.get("proposal_version", 1), limit=limit, header=header,
        )

    @staticmethod
    def _validate_review_partition(review_diff: ReviewDiff, parts: list[dict]) -> None:
        _validate_review_partition(review_diff, parts)

    @staticmethod
    def _campaign_source_hash(
        review_diff: ReviewDiff, *, preserved_exclusions: tuple[tuple[str, str], ...] = (),
    ) -> str:
        return _campaign_source_hash(review_diff, preserved_exclusions=preserved_exclusions)

    @staticmethod
    def _campaign_part_manifest(parts: list[dict]) -> list[dict]:
        return _campaign_part_manifest(parts)

    def _prepare_review_campaign(self, review_diff: ReviewDiff, shared: dict[str, str]) -> tuple[dict, dict[str, str], list[dict]]:
        existing = self.state.get("review_campaign")
        authorization_limit = self.authorized_call_limit
        campaign_limit = None
        if existing:
            campaign_id = existing["campaign_id"]
            partition_limit = existing.get(
                "partition_limit_characters", existing["payload_limit_characters"],
            )
            campaign_limit = existing["payload_limit_characters"]
            try:
                current_limit = claude_reviewer.max_review_payload_chars()
            except claude_reviewer.ReviewError as exc:
                raise ReviewCampaignError(str(exc)) from exc
            if current_limit < campaign_limit:
                raise ReviewCampaignError(
                    f"Current {CLAUDE_MAX_PAYLOAD_ENV} limit ({current_limit}) is below the saved review "
                    f"campaign limit ({campaign_limit}); restore the saved limit to resume safely."
                )
            limit = partition_limit
        else:
            try:
                limit = claude_reviewer.max_review_payload_chars()
            except claude_reviewer.ReviewError as exc:
                raise ReviewCampaignError(str(exc)) from exc
            campaign_id = uuid.uuid4().hex

        saved_headers = [
            context.get("header_id") for context in self.state.get("claude_contexts", [])
            if context.get("stage") == "code_review"
            and context.get("campaign_id") == campaign_id
            and context.get("review_kind") == "part"
        ]
        legacy_campaign = bool(existing) and (
            existing.get("header_id") == "code-review-part-v1"
            or (not existing.get("header_id") and "code-review-part-v1" in saved_headers)
        )
        review_header = self._legacy_code_review_header() if legacy_campaign else self._code_review_header()
        shared_hash = _review_patch_sha256(_format_context_prompt(
            _review_redact(review_header),
            {name: _review_redact(value).strip() for name, value in shared.items()},
        ))
        source_hash = self._campaign_source_hash(review_diff)
        try:
            parts, multipart = self._partition_review_segments(
                review_diff, shared, campaign_id=campaign_id, limit=limit, header=review_header,
            )
        except ReviewCampaignError as exc:
            if not existing:
                self.state["review_included_files"] = list(review_diff.included_paths)
                self.state["review_exclusions"] = [
                    {"path": _safe_review_path(path), "reason": reason}
                    for path, reason in review_diff.excluded_files
                ]
                self.state["review_files_not_reviewed"] = [
                    {"path": path, "reason": _review_redact(str(exc))}
                    for path in review_diff.included_paths
                ]
                self.state["review_partition_failure"] = {
                    "campaign_id": campaign_id,
                    "source_patch_sha256": source_hash,
                    "shared_context_sha256": shared_hash,
                    "payload_limit_characters": limit,
                    "error": _review_redact(str(exc)),
                }
                self.state["review_campaign_id"] = campaign_id
                self._require_save()
                self._save()
            raise
        part_manifest = self._campaign_part_manifest(parts)
        if existing:
            if (existing.get("source_patch_sha256") != source_hash
                    or existing.get("shared_context_sha256") != shared_hash
                    or existing.get("parts") != part_manifest):
                raise ReviewCampaignError(
                    "The target diff, shared review context, or partition changed after the review campaign began. "
                    "No remaining review calls were made; start a newly authorized workflow for the changed review."
                )
            if current_limit > campaign_limit:
                existing["payload_limit_history"] = existing.get("payload_limit_history", []) + [
                    {"characters": current_limit, "updated_at": _review_now()},
                ]
                existing["payload_limit_characters"] = current_limit
                self._require_save()
                self._save()
            if not self._authorize_call_count(existing.get("required_calls", 0)):
                raise ReviewCampaignError(
                    f"This saved review campaign requires {existing.get('required_calls')} Claude calls, but its "
                    f"persisted authorization allows {authorization_limit}. "
                    "The existing authorization was not expanded."
                )
            return existing, shared, parts

        required_calls = len(parts) + (1 if multipart else 0)
        authorization_limit = self.authorized_call_limit
        campaign = _create_campaign_snapshot(
            campaign_id=campaign_id,
            created_at=_review_now(),
            payload_limit_characters=limit,
            source_patch_sha256=source_hash,
            shared_context_sha256=shared_hash,
            required_calls=required_calls,
            authorized_call_limit=authorization_limit,
            parts=part_manifest,
            integration_required=multipart,
            included_paths=review_diff.included_paths,
            excluded_files=review_diff.excluded_files,
            report_contract_version=PART_REVIEW_VALIDATOR_VERSION,
        )
        self.state["review_campaign"] = campaign
        self.state["review_campaign_id"] = campaign_id
        self.state["review_shared_context_sha256"] = shared_hash
        self.state["review_included_files"] = list(review_diff.included_paths)
        self.state["review_exclusions"] = campaign["excluded_files"]
        self.state["review_files_not_reviewed"] = campaign["files_not_reviewed"]
        self.state.pop("review_partition_failure", None)
        self._require_save()
        self._save()
        if not self._authorize_call_count(required_calls):
            campaign.update(_block_campaign_for_authorization(campaign, review_diff.included_paths))
            self._require_save()
            self._save()
            raise ReviewCampaignError(
                f"This review requires {required_calls} Claude calls (parts plus integration), but this workflow "
                f"authorizes {authorization_limit}. Existing authorization was not expanded."
            )
        campaign["status"] = "in_progress"
        self._require_save()
        self._save()
        return campaign, shared, parts

    def _sync_review_coverage(self, campaign: dict, review_diff: ReviewDiff) -> None:
        updated = _sync_campaign_coverage(campaign, review_diff.excluded_files)
        campaign.clear()
        campaign.update(updated)
        self.state["review_files_not_reviewed"] = campaign["files_not_reviewed"]

    @staticmethod
    def _part_report_validation(report: str, part_id: str, segments: list[ReviewSegment]) -> dict:
        return parse_part_review_report(report, part_id, segments)

    @staticmethod
    def _part_report_is_structured(report: str, part_id: str, segments: list[ReviewSegment]) -> bool:
        return parse_part_review_report(report, part_id, segments)["valid"]

    @staticmethod
    def _integration_report_is_structured(report: str) -> bool:
        return _integration_report_is_structured(report)

    def _run_review_campaign(
        self, campaign: dict, shared: dict[str, str], parts: list[dict], review_diff: ReviewDiff,
    ) -> str:
        campaign["status"] = "in_progress"
        self._sync_review_coverage(campaign, review_diff)
        self._require_save()
        self._save()

        def campaign_input() -> CampaignInput:
            return _make_campaign_input(
                campaign, shared, parts, review_diff.excluded_files,
                proposal_version=self.state.get("proposal_version", 1), transition_at=_review_now(),
            )

        snapshot = CampaignSnapshot.from_mapping(campaign)

        def accept(transition: CampaignTransition, *, save: bool) -> None:
            nonlocal snapshot
            campaign.clear()
            campaign.update(transition.snapshot.to_mapping())
            snapshot = transition.snapshot
            self.state["review_files_not_reviewed"] = campaign.get("files_not_reviewed", [])
            if save:
                self._require_save()
                self._save()

        transition = _advance_review_campaign(snapshot, campaign_input())
        if transition.decision == "blocked":
            raise ReviewCampaignError("; ".join(transition.findings) or "Review campaign transition is blocked.")
        if transition.snapshot != snapshot:
            accept(transition, save=True)
        part_indexes = {part["part_id"]: index for index, part in enumerate(parts, start=1)}

        while transition.decision == "request_part":
            request = transition.request
            if request is None or transition.part_id is None:
                raise ReviewCampaignError("Review campaign requested a part without a request value.")
            part = next(item for item in parts if item["part_id"] == transition.part_id)
            record = next(item for item in campaign["part_reviews"] if item["part_id"] == transition.part_id)
            recoverable = next((
                context for context in reversed(self.state.get("claude_contexts", []))
                if context.get("campaign_id") == campaign["campaign_id"]
                and context.get("part_id") == part["part_id"]
                and context.get("call_state") == "completed"
                and context.get("report") is not None
                and not any(call.get("prompt_sha256") == context.get("prompt_sha256")
                            for call in record.get("calls", []))
            ), None)
            if recoverable is not None:
                transition = _advance_review_campaign(snapshot, campaign_input(), PreviousResult(
                    part_id=part["part_id"],
                    outcome="completed",
                    call_state=recoverable["call_state"],
                    report=recoverable["report"],
                    prompt_sha256=recoverable.get("prompt_sha256"),
                    cost_usd=recoverable.get("cost_usd"),
                    campaign_call_number=recoverable.get("campaign_call_number"),
                    received_at=_review_now(), recovered=True,
                ))
                accept(transition, save=True)
                if transition.decision == "blocked":
                    raise ReviewCampaignError(
                        f"Persisted Claude report for {part['part_id']} is invalid: "
                        + "; ".join(transition.findings)
                    )
                transition = _advance_review_campaign(snapshot, campaign_input())
                if transition.decision == "blocked":
                    raise ReviewCampaignError("; ".join(transition.findings) or "Review campaign transition is blocked.")
                if transition.snapshot != snapshot:
                    accept(transition, save=True)
                continue

            retry_count = len(record.get("calls", [])) if isinstance(record.get("calls", []), list) else 0
            part_trace = self._trace_context("review.part.provider_call", {
                "review_part_index": part_indexes[part["part_id"]],
                "review_part_count": len(parts),
                "retry_count": retry_count,
            })
            try:
                self._require_review_call()
                with part_trace:
                    report, manifest = self._call_claude(
                        request.header, "code_review", request.section_mapping(),
                        metadata={
                            "proposal_version": self.state.get("proposal_version", 1),
                            "header_id": "code-review-part-v2",
                            "review_kind": "part",
                            "campaign_id": campaign["campaign_id"],
                            "part_id": part["part_id"],
                            "retry_count": retry_count,
                            "shared_context_sha256": campaign["shared_context_sha256"],
                            "part_patch_sha256": request.part_patch_sha256,
                            "payload_limit_chars": campaign["payload_limit_characters"],
                            "included_files": sorted({segment.path for segment in part["segments"]}),
                            "excluded_files": campaign["excluded_files"],
                        },
                    )
            except Exception:
                context = next((
                    item for item in reversed(self.state.get("claude_contexts", []))
                    if item.get("campaign_id") == campaign["campaign_id"]
                    and item.get("part_id") == part["part_id"]
                ), None)
                if context is not None and not any(
                        call.get("prompt_sha256") == context.get("prompt_sha256") for call in record["calls"]):
                    record["calls"].append({
                        "prompt_sha256": context.get("prompt_sha256"), "cost_usd": context.get("cost_usd"),
                        "call_state": context.get("call_state"),
                        "campaign_call_number": context.get("campaign_call_number"),
                    })
                if context is not None and context.get("call_state") == "completed" and context.get("report"):
                    record["report"] = context["report"]
                    record["context_sha256"] = context.get("prompt_sha256")
                    record["status"] = "report_saved"
                else:
                    record["status"] = "call_failed"
                record["segments"] = self._campaign_part_manifest([part])[0]["segments"]
                self._sync_review_coverage(campaign, review_diff)
                self._require_save()
                self._save()
                raise
            transition = _advance_review_campaign(snapshot, campaign_input(), PreviousResult(
                part_id=part["part_id"], outcome="completed",
                call_state=manifest["call_state"], report=report,
                prompt_sha256=manifest["prompt_sha256"], cost_usd=manifest.get("cost_usd"),
                campaign_call_number=manifest.get("campaign_call_number"), received_at=_review_now(),
            ))
            accept(transition, save=True)
            if hasattr(part_trace, "update"):
                updated_part = next(
                    item for item in campaign["part_reviews"] if item["part_id"] == part["part_id"]
                )
                part_trace.update(review_result=updated_part["status"])
            if transition.decision == "blocked":
                raise ReviewCampaignError(
                    f"Claude report for {part['part_id']} is invalid: "
                    + "; ".join(transition.findings)
                    + ". The report remains preserved and the part is unreviewed."
                )
            transition = _advance_review_campaign(snapshot, campaign_input())
            if transition.decision == "blocked":
                raise ReviewCampaignError("; ".join(transition.findings) or "Review campaign transition is blocked.")
            if transition.snapshot != snapshot:
                accept(transition, save=True)

        if transition.decision == "blocked":
            if transition.reason == "coverage_incomplete" and len(parts) == 1:
                raise ReviewCampaignError(
                    "The single-part review cannot complete while changed files remain unreviewed."
                )
            raise ReviewCampaignError(
                "; ".join(transition.findings) or "The review campaign cannot complete while required work remains."
            )

        if transition.decision == "request_integration":
            request = transition.request
            if request is None:
                raise ReviewCampaignError("Review campaign requested integration without a request value.")
            integration = campaign["integration_review"]
            recoverable = next((
                context for context in reversed(self.state.get("claude_contexts", []))
                if context.get("campaign_id") == campaign["campaign_id"]
                and context.get("part_id") == "integration"
                and context.get("call_state") == "completed"
                and context.get("report") is not None
                and not any(call.get("prompt_sha256") == context.get("prompt_sha256")
                            for call in integration.get("calls", []))
            ), None)
            if recoverable is not None:
                transition = _advance_review_campaign(snapshot, campaign_input(), PreviousResult(
                    part_id="integration", outcome="completed", call_state=recoverable["call_state"],
                    report=recoverable["report"], prompt_sha256=recoverable.get("prompt_sha256"),
                    cost_usd=recoverable.get("cost_usd"),
                    campaign_call_number=recoverable.get("campaign_call_number"),
                    received_at=_review_now(), recovered=True,
                ))
                accept(transition, save=True)
                if transition.decision == "blocked":
                    raise ReviewCampaignError("Persisted Claude integration output is not a complete structured report.")
            else:
                retry_count = len(integration.get("calls", [])) if isinstance(integration.get("calls", []), list) else 0
                integration_trace = self._trace_context("review.integration.provider_call", {
                    "review_part_count": len(parts),
                    "retry_count": retry_count,
                })
                try:
                    self._require_review_call()
                    with integration_trace:
                        report, manifest = self._call_claude(
                            request.header, "code_review", request.section_mapping(),
                            metadata={
                                "proposal_version": self.state.get("proposal_version", 1),
                                "header_id": "code-review-integration-v1",
                                "review_kind": "integration",
                                "campaign_id": campaign["campaign_id"],
                                "part_id": "integration",
                                "retry_count": retry_count,
                                "shared_context_sha256": campaign["shared_context_sha256"],
                                "payload_limit_chars": campaign["payload_limit_characters"],
                                "included_files": campaign["files_reviewed"],
                                "excluded_files": campaign["excluded_files"],
                            },
                        )
                except Exception:
                    context = next((
                        item for item in reversed(self.state.get("claude_contexts", []))
                        if item.get("campaign_id") == campaign["campaign_id"]
                        and item.get("part_id") == "integration"
                    ), None)
                    if context is not None and not any(
                            call.get("prompt_sha256") == context.get("prompt_sha256")
                            for call in integration["calls"]):
                        integration["calls"].append({
                            "prompt_sha256": context.get("prompt_sha256"), "cost_usd": context.get("cost_usd"),
                            "call_state": context.get("call_state"),
                            "campaign_call_number": context.get("campaign_call_number"),
                        })
                    if context is not None and context.get("call_state") == "completed" and context.get("report"):
                        integration["report"] = context["report"]
                        integration["context_sha256"] = context.get("prompt_sha256")
                        integration["status"] = "report_saved"
                    else:
                        integration["status"] = "call_failed"
                    self._require_save()
                    self._save()
                    raise
                transition = _advance_review_campaign(snapshot, campaign_input(), PreviousResult(
                    part_id="integration", outcome="completed",
                    call_state=manifest["call_state"], report=report,
                    prompt_sha256=manifest["prompt_sha256"], cost_usd=manifest.get("cost_usd"),
                    campaign_call_number=manifest.get("campaign_call_number"), received_at=_review_now(),
                ))
                accept(transition, save=True)
                if hasattr(integration_trace, "update"):
                    integration_trace.update(review_result=campaign["integration_review"]["status"])
                if transition.decision == "blocked":
                    raise ReviewCampaignError(
                        "Claude returned an incomplete integration report; campaign remains incomplete."
                    )

        if transition.decision != "complete":
            raise ReviewCampaignError("The review campaign cannot complete while required work remains.")
        if campaign["files_not_reviewed"] or any(
                item["status"] != "reviewed" for item in campaign["part_reviews"]
        ):
            raise ReviewCampaignError("The review campaign cannot complete while any required part remains unreviewed.")
        if len(parts) == 1:
            self.state["review_campaign_call_count"] = sum(
                call.get("call_state") != "validation_failed"
                for call in campaign["part_reviews"][0].get("calls", [])
            )
        else:
            self.state["review_campaign_call_count"] = sum(
                sum(call.get("call_state") != "validation_failed" for call in record.get("calls", []))
                for record in campaign["part_reviews"]
            ) + sum(
                call.get("call_state") != "validation_failed"
                for call in campaign["integration_review"].get("calls", [])
            )
        self._require_save()
        self._save()
        return campaign["consolidated_report"]

def recover_review_campaign(
    state: dict,
    workflow_id: str,
    target: Path,
    review_diff: ReviewDiff,
    shared: dict[str, str],
    *,
    apply: bool,
) -> tuple[dict, dict | None]:
    """Revalidate saved reports locally and return a proposed state when valid."""
    campaign = state.get("review_campaign")
    if not isinstance(campaign, dict) or not campaign.get("campaign_id"):
        raise ReviewCampaignError("No persisted code-review campaign is available to recover.")
    errors: list[str] = []
    source_hash = ReviewCampaign._campaign_source_hash(review_diff)
    if source_hash != campaign.get("source_patch_sha256"):
        errors.append("current target diff or exclusions do not match the saved campaign source hash")

    context_records = [
        item for item in state.get("claude_contexts", [])
        if item.get("stage") == "code_review" and item.get("campaign_id") == campaign["campaign_id"]
        and item.get("review_kind") == "part"
    ]
    header_ids = {
        item.get("header_id") for item in context_records
        if item.get("call_state") == "completed" and isinstance(item.get("report"), str)
    }
    if header_ids == {"code-review-part-v1"}:
        header = ReviewCampaign._legacy_code_review_header()
    elif header_ids == {"code-review-part-v2"}:
        header = ReviewCampaign._code_review_header()
    else:
        errors.append("saved code-review contexts do not identify one supported prompt contract")
        header = ReviewCampaign._legacy_code_review_header()

    shared_hash = _review_patch_sha256(_format_context_prompt(
        _review_redact(header), {name: _review_redact(value).strip() for name, value in shared.items()},
    ))
    if shared_hash != campaign.get("shared_context_sha256"):
        errors.append("current assignment, proposal, implementation summary, repository context, or review guidance "
                      "does not match the saved shared-context hash")

    patch_by_path = dict(review_diff.file_patches)
    parts: list[dict] = []
    for saved_part in campaign.get("parts", []):
        segments: list[ReviewSegment] = []
        for item in saved_part.get("segments", []):
            path = item.get("path")
            patch = patch_by_path.get(path)
            if patch is None:
                errors.append(f"saved part {saved_part.get('part_id')} references missing changed path {path!r}")
                continue
            lines = patch.splitlines(keepends=True)
            start, end, count = (item.get("patch_line_start"), item.get("patch_line_end"),
                                 item.get("patch_line_count"))
            if (not isinstance(start, int) or not isinstance(end, int) or not isinstance(count, int)
                    or start < 1 or end < start or end > len(lines) or count != len(lines)):
                errors.append(f"saved patch range is invalid for {path!r}")
                continue
            text = "".join(lines[start - 1:end])
            digest = _review_patch_sha256(text)
            if digest != item.get("patch_sha256"):
                errors.append(f"saved patch segment hash no longer matches {path!r}")
                continue
            segments.append(ReviewSegment(path, text, start, end, count, digest))
        parts.append({"part_id": saved_part.get("part_id"), "segments": segments})
    try:
        ReviewCampaign._validate_review_partition(review_diff, parts)
    except ReviewCampaignError as exc:
        errors.append(str(exc))
    if ReviewCampaign._campaign_part_manifest(parts) != campaign.get("parts"):
        errors.append("reconstructed patch partition does not match the saved campaign manifest")

    reviews_by_part = {item.get("part_id"): item for item in campaign.get("part_reviews", [])}
    reports_by_part: dict[str, list[dict]] = {}
    validations: list[dict] = []
    for part in parts:
        part_id = part["part_id"]
        record = reviews_by_part.get(part_id)
        part_contexts = [item for item in context_records if item.get("part_id") == part_id]
        contexts = [item for item in part_contexts if item.get("call_state") == "completed"]
        if not contexts:
            if record:
                saved_calls = Counter(
                    (item.get("prompt_sha256"), item.get("cost_usd"), item.get("call_state"),
                     item.get("campaign_call_number")) for item in record.get("calls", [])
                )
                expected_calls = Counter(
                    (item.get("prompt_sha256"), item.get("cost_usd"), item.get("call_state"),
                     item.get("campaign_call_number")) for item in part_contexts
                )
                if expected_calls != saved_calls:
                    errors.append(f"{part_id} campaign call metadata does not preserve each saved provider attempt")
            continue
        sections = ReviewCampaign(state)._part_sections(
            shared, part["segments"], campaign_id=campaign["campaign_id"], part_id=part_id,
            excluded_count=len(review_diff.excluded_files),
        )
        safe_header = _review_redact(header)
        safe_sections = {
            name: _review_redact(value) if name == "Complete patch" else _review_redact(value).strip()
            for name, value in sections.items()
        }
        prompt = _format_context_prompt(safe_header, safe_sections)
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        section_manifest = [
            {"name": name, "characters": len(value), "sha256": _review_sha256_text(value)}
            for name, value in safe_sections.items()
        ]
        patch_hash = _review_patch_sha256(sections["Complete patch"])
        expected_files = sorted({segment.path for segment in part["segments"]})
        for context in contexts:
            call_number = context.get("campaign_call_number")
            metadata_errors = []
            if context.get("prompt_sha256") != prompt_sha256:
                metadata_errors.append("prompt hash does not match the saved report request")
            if context.get("header_sha256") != _review_sha256_text(safe_header):
                metadata_errors.append("header hash does not match the saved prompt contract")
            if context.get("sections") != section_manifest:
                metadata_errors.append("section hashes do not match the reconstructed prompt")
            if context.get("part_patch_sha256") != patch_hash:
                metadata_errors.append("part patch hash does not match the current target diff")
            if context.get("shared_context_sha256") != campaign.get("shared_context_sha256"):
                metadata_errors.append("saved shared-context metadata does not match the campaign")
            if context.get("included_files") != expected_files:
                metadata_errors.append("included-file metadata does not match this part")
            if context.get("excluded_files") != campaign.get("excluded_files"):
                metadata_errors.append("excluded-file metadata does not match the campaign")
            if context.get("characters") != len(prompt) or context.get("utf8_bytes") != len(prompt.encode("utf-8")):
                metadata_errors.append("saved prompt length metadata does not match the reconstructed prompt")
            if context.get("complete") is not True:
                metadata_errors.append("saved prompt manifest was not marked complete")
            if context.get("report") is None or not isinstance(context.get("report"), str):
                metadata_errors.append("completed provider context has no persisted report text")
            cost = context.get("cost_usd")
            if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
                metadata_errors.append("completed provider context has no valid recorded cost")
            if metadata_errors:
                errors.extend(f"{part_id} call {call_number}: {item}" for item in metadata_errors)
            report = context.get("report") if isinstance(context.get("report"), str) else ""
            validation = parse_part_review_report(report, part_id, part["segments"])
            attempt = {
                "part_id": part_id,
                "attempt": call_number,
                "submitted_at": context.get("submitted_at"),
                "prompt_sha256": context.get("prompt_sha256"),
                "cost_usd": cost,
                "report": report,
                "validation": validation,
                "errors": metadata_errors + validation["errors"],
            }
            validations.append(attempt)
            reports_by_part.setdefault(part_id, []).append(attempt)
            if not validation["valid"]:
                errors.extend(f"{part_id} call {call_number}: {item}" for item in validation["errors"])
        if record:
            if not record.get("report"):
                errors.append(f"{part_id} has completed report contexts but no campaign report text")
            elif contexts and record.get("report") != contexts[-1].get("report"):
                errors.append(f"{part_id} campaign report is not the latest persisted provider response")
            calls = record.get("calls", [])
            expected_calls = Counter(
                (item.get("prompt_sha256"), item.get("cost_usd"), item.get("call_state"),
                 item.get("campaign_call_number")) for item in part_contexts
            )
            saved_calls = Counter(
                (item.get("prompt_sha256"), item.get("cost_usd"), item.get("call_state"),
                 item.get("campaign_call_number")) for item in calls
            )
            if expected_calls != saved_calls:
                errors.append(f"{part_id} campaign call metadata does not preserve each saved provider attempt")

    missing_parts = [part["part_id"] for part in parts if not reports_by_part.get(part["part_id"])]
    campaign_complete = not missing_parts and all(
        all(attempt["validation"]["valid"] and not attempt["errors"] for attempt in attempts)
        for attempts in reports_by_part.values()
    ) and not errors
    integration = campaign.get("integration_review", {})
    integration_complete = not integration.get("required") or integration.get("status") == "reviewed"
    campaign_complete = campaign_complete and integration_complete

    original_reports = [attempt for attempts in reports_by_part.values() for attempt in attempts]
    digest_material = [
        {"part_id": item["part_id"], "attempt": item["attempt"],
         "report_sha256": _review_sha256_text(item["report"]), "prompt_sha256": item["prompt_sha256"]}
        for item in original_reports
    ]
    recovery_digest = _review_sha256_text(json.dumps(digest_material, sort_keys=True, separators=(",", ":")))
    already_applied = any(
        item.get("validator_version") == PART_REVIEW_VALIDATOR_VERSION
        and item.get("recovery_digest") == recovery_digest
        for item in campaign.get("recovery_history", [])
    )
    result = {
        "workflow_id": workflow_id,
        "target": str(target),
        "mode": "apply" if apply else "dry-run",
        "provider_invocations": 0,
        "source_patch_sha256": source_hash,
        "shared_context_sha256": shared_hash,
        "persisted_reports": len(validations),
        "valid_reports": sum(item["validation"]["valid"] and not item["errors"] for item in validations),
        "invalid_reports": sum(not item["validation"]["valid"] or bool(item["errors"]) for item in validations),
        "missing_parts": missing_parts,
        "integration_review_status": integration.get("status"),
        "campaign_would_be_complete": campaign_complete,
        "errors": errors,
        "report_validations": [
            {key: item[key] for key in ("part_id", "attempt", "submitted_at", "cost_usd", "validation", "errors")}
            for item in validations
        ],
        "recovery_digest": recovery_digest,
        "already_applied": already_applied,
        "applied": False,
        "claude_spent_usd": state.get("authorization", {}).get("claude_aily", {}).get("spent_usd"),
    }
    if not apply or already_applied:
        return result, None
    if errors:
        result["apply_error"] = "Recovery was not applied because source or report validation failed."
        return result, None
    if not validations:
        result["apply_error"] = "Recovery was not applied because no persisted completed part reports were found."
        return result, None

    proposed = copy.deepcopy(state)
    proposed_campaign = proposed["review_campaign"]
    proposed_records = {item["part_id"]: item for item in proposed_campaign.get("part_reviews", [])}
    for part_id, attempts in reports_by_part.items():
        record = proposed_records[part_id]
        previous_status = record.get("status")
        validated_reports = record.setdefault("validated_reports", [])
        known = {(item.get("prompt_sha256"), item.get("campaign_call_number")) for item in validated_reports}
        for attempt in attempts:
            identity = (attempt.get("prompt_sha256"), attempt.get("attempt"))
            if identity in known:
                continue
            validated_reports.append({
                "report": attempt["report"],
                "prompt_sha256": attempt["prompt_sha256"],
                "campaign_call_number": attempt["attempt"],
                "submitted_at": attempt["submitted_at"],
                "cost_usd": attempt["cost_usd"],
                "call_state": "completed",
                "validator_version": PART_REVIEW_VALIDATOR_VERSION,
                "validation": attempt["validation"],
            })
        latest = attempts[-1]
        record["report"] = latest["report"]
        record["context_sha256"] = latest["prompt_sha256"]
        record["status"] = "reviewed" if all(
            item["validation"]["valid"] and not item["errors"] for item in attempts
        ) else "invalid_report"
        record["segments"] = next(
            (item["segments"] for item in proposed_campaign.get("parts", []) if item["part_id"] == part_id), []
        )
        record.setdefault("validation_history", []).append({
            "validator_version": PART_REVIEW_VALIDATOR_VERSION,
            "validated_at": _review_now(),
            "previous_status": previous_status,
            "valid": record["status"] == "reviewed",
            "errors": [error for item in attempts for error in item["errors"]],
            "report_sha256s": [_review_sha256_text(item["report"]) for item in attempts],
            "source_patch_sha256": source_hash,
        })

        ReviewCampaign(proposed)._sync_review_coverage(proposed_campaign, review_diff)
    all_parts_reviewed = all(item.get("status") == "reviewed"
                             for item in proposed_campaign.get("part_reviews", []))
    full_coverage = not proposed_campaign.get("files_not_reviewed")
    if all_parts_reviewed and full_coverage and integration_complete:
        proposed_campaign["status"] = "complete"
        formatted = render_review_feedback(_validated_campaign_reports(proposed_campaign))
        proposed_campaign["consolidated_report"] = formatted
        proposed["code_review"] = formatted
        proposed["code_review_context_sha256"] = proposed_campaign.get("shared_context_sha256")
    else:
        proposed_campaign["status"] = "in_progress"

    proposed_campaign["header_id"] = next(iter(header_ids)) if len(header_ids) == 1 else proposed_campaign.get("header_id")
    proposed_campaign["report_contract_version"] = PART_REVIEW_VALIDATOR_VERSION
    proposed_campaign.setdefault("recovery_history", []).append({
        "recovered_at": _review_now(),
        "validator_version": PART_REVIEW_VALIDATOR_VERSION,
        "recovery_digest": recovery_digest,
        "source_patch_sha256": source_hash,
        "shared_context_sha256": shared_hash,
        "old_validator_statuses": {
            item["part_id"]: reviews_by_part[item["part_id"]].get("status")
            for item in proposed_campaign.get("part_reviews", []) if item["part_id"] in reviews_by_part
        },
        "reports_revalidated": len(validations),
        "campaign_completed": proposed_campaign.get("status") == "complete",
        "workflow_status_preserved": proposed.get("status"),
        "workflow_stage_preserved": proposed.get("stage"),
    })
    proposed["review_files_not_reviewed"] = proposed_campaign.get("files_not_reviewed", [])
    proposed["review_campaign_call_count"] = state.get("review_campaign_call_count", sum(
        call.get("call_state") != "validation_failed"
        for record in proposed_campaign.get("part_reviews", []) for call in record.get("calls", [])
    ))

    return result, proposed

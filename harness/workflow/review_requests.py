"""Deterministic code-review prompt and request construction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from workflow import review_text

from workflow.review_models import CampaignPart, ReviewCampaignError, ReviewRequest, ReviewSegment


def review_header() -> str:
    return (
        "Perform an independent, read-only code review. Treat the implementation and approved proposal as claims "
        "to verify against the original assignment. Apply all shared review criteria and the specific scope for "
        "this part. Do not implement changes. Return exactly one `Decision: APPROVED` or "
        "`Decision: CHANGES_REQUESTED` line, followed by exactly one `Part ID: <id>` line. "
        "Use the sections `Files reviewed`, `Findings`, `Cross-component concerns`, and `Overall assessment` "
        "in that order. Markdown heading markers are allowed on the Part ID and section lines. "
        "Represent every finding in its own uniquely labeled `### Finding F1` block. Include exactly one "
        "non-empty field for each of: "
        "`Severity`, `Category`, `File/location`, `Description`, `Impact and reasoning`, and "
        "`Recommended correction`. Use severity `Must fix`, `Should fix`, or `Optional improvement`. "
        "Do not place findings outside their blocks. Write `Findings: None` when there are no findings. "
        "Write one of `APPROVED` or `CHANGES_REQUESTED` only. Do not claim approval when a Must fix finding "
        "exists; use `Decision: CHANGES_REQUESTED` for acceptance-blocking defects. Give a substantive "
        "overall assessment including positive observations and limitations where relevant."
    )


def legacy_review_header() -> str:
    """Return the exact v1 header so persisted requests remain reconstructible."""
    return (
        "Perform an independent, read-only code review. Treat the implementation and approved proposal as claims "
        "to verify against the original assignment. Apply all shared review criteria and the specific scope for "
        "this part. Do not implement changes. Return a structured report with exactly these headings: `Part ID: "
        "<id>`, `Files reviewed`, `Findings`, `Cross-component concerns`, and `Overall assessment`. Format each "
        "finding with fields `Severity`, `Category`, `File/location`, `Description`, `Impact and reasoning`, and "
        "`Recommended correction`. Write `Findings: None` when there are none. Start with `Decision: APPROVED` "
        "unless an acceptance-blocking defect warrants `Decision: CHANGES_REQUESTED`."
    )


def integration_review_header() -> str:
    return (
        "Perform the final independent integration review for this multi-part campaign. Synthesize the "
        "individual reports and shared architecture context. Examine contracts, boundaries, data flow, "
        "inter-part assumptions, inconsistent error/state behavior, duplicated responsibilities, and defects "
        "that only appear across components. Deduplicate findings while preserving references. Add new "
        "integration findings only with evidence in the reports/context. Do not repeat local reviews."
        " Return `Decision: APPROVED` or `Decision: CHANGES_REQUESTED`, then use the exact headings "
        "`Consolidated findings`, `Integration findings`, and `Overall assessment`. Preserve original finding "
        "references and severity; state explicitly when a section has none."
    )


def redact_review_text(text: str) -> str:
    return review_text.redact(text)


def format_context_prompt(header: str, sections: Mapping[str, str]) -> str:
    blocks = [header.strip()]
    for name, value in sections.items():
        content = value if name == "Complete patch" else value.strip()
        blocks.append(f"## {name}\n{content}")
    return "\n\n".join(blocks).strip()


def part_coverage(segments: Sequence[ReviewSegment], *, excluded_count: int) -> str:
    lines = ["Every patch line listed below is included in this review request; no file content was truncated."]
    for segment in segments:
        line_description = (
            f"patch lines {segment.patch_line_start}-{segment.patch_line_end} of {segment.patch_line_count}"
        )
        if segment.patch_line_start == 1 and segment.patch_line_end == segment.patch_line_count:
            line_description = "complete patch"
        lines.append(f"- `{segment.path}` — {line_description}")
    lines.append(f"Changed metadata/binary/protected paths explicitly excluded and recorded: {excluded_count}.")
    return "\n".join(lines)


def part_sections(
    shared: Mapping[str, str],
    segments: Sequence[ReviewSegment],
    *,
    campaign_id: str,
    part_id: str,
    proposal_version: int,
    excluded_count: int,
) -> dict[str, str]:
    patch_lines = []
    for segment in segments:
        patch_lines.extend([
            f"FILE: {json.dumps(segment.path, ensure_ascii=True)}; patch lines "
            f"{segment.patch_line_start}-{segment.patch_line_end} of {segment.patch_line_count}; "
            f"segment SHA-256 {segment.patch_sha256}",
            segment.text,
        ])
    sections = {
        "Review metadata": (
            f"Code review campaign: {campaign_id}\nPart ID: {part_id}\n"
            f"Proposal version: {proposal_version}. This request contains all assigned patch lines. "
            "Return one structured individual-part report using the required format."
        ),
        **dict(shared),
        "Review file coverage": part_coverage(segments, excluded_count=excluded_count),
        "Complete patch": "\n".join(patch_lines),
    }
    return sections


def serialized_review_fits(header: str, sections: Mapping[str, str], limit: int) -> bool:
    safe_header = redact_review_text(header)
    safe_sections = {
        name: redact_review_text(value) if name == "Complete patch" else redact_review_text(value).strip()
        for name, value in sections.items()
    }
    prompt = format_context_prompt(safe_header, safe_sections)
    try:
        review_text.validate_review_invocation(prompt, "aily", limit=limit)
        return True
    except review_text.ReviewTextError as exc:
        if "above the configured" in str(exc):
            return False
        raise ReviewCampaignError(str(exc)) from exc


def _request(
    *,
    kind: str,
    campaign_id: str,
    part_id: str,
    purpose: str,
    proposal_version: int,
    payload_limit_characters: int,
    report_contract_version: str,
    header_id: str,
    source_patch_sha256: str,
    shared_context_sha256: str,
    part_patch_sha256: str | None,
    segment_sha256s: tuple[str, ...],
    header: str,
    sections: Mapping[str, str],
) -> ReviewRequest:
    safe_header = redact_review_text(header)
    safe_sections = {
        name: redact_review_text(value) if name == "Complete patch" else redact_review_text(value).strip()
        for name, value in sections.items()
    }
    prompt = format_context_prompt(safe_header, safe_sections)
    prompt_bytes = prompt.encode("utf-8")
    return ReviewRequest(
        kind=kind, campaign_id=campaign_id, part_id=part_id, purpose=purpose,
        proposal_version=proposal_version,
        proposal_sha256=hashlib.sha256(sections.get("Approved proposal", "").encode("utf-8")).hexdigest(),
        payload_limit_characters=payload_limit_characters,
        report_contract_version=report_contract_version, header_id=header_id,
        source_patch_sha256=source_patch_sha256, shared_context_sha256=shared_context_sha256,
        part_patch_sha256=part_patch_sha256, segment_sha256s=segment_sha256s, header=safe_header,
        sections=tuple(safe_sections.items()), prompt=prompt,
        prompt_sha256=hashlib.sha256(prompt_bytes).hexdigest(),
    )


def build_part_request(
    *,
    campaign_id: str,
    part: CampaignPart,
    purpose: str,
    proposal_version: int,
    payload_limit_characters: int,
    report_contract_version: str,
    source_patch_sha256: str,
    shared_context_sha256: str,
    shared_context: Mapping[str, str],
    excluded_count: int,
    header: str | None = None,
) -> ReviewRequest:
    selected_header = review_header() if header is None else header
    sections = part_sections(
        shared_context, part.segments, campaign_id=campaign_id, part_id=part.part_id,
        proposal_version=proposal_version, excluded_count=excluded_count,
    )
    patch_hash = hashlib.sha256(sections["Complete patch"].encode("utf-8")).hexdigest()
    return _request(
        kind="part", campaign_id=campaign_id, part_id=part.part_id, purpose=purpose,
        proposal_version=proposal_version, payload_limit_characters=payload_limit_characters,
        report_contract_version=report_contract_version, header_id="code-review-part-v2",
        source_patch_sha256=source_patch_sha256, shared_context_sha256=shared_context_sha256,
        part_patch_sha256=patch_hash, segment_sha256s=tuple(segment.patch_sha256 for segment in part.segments),
        header=selected_header, sections=sections,
    )


def build_integration_request(
    *,
    campaign: Mapping[str, object],
    parts: Sequence[CampaignPart],
    shared_context: Mapping[str, str],
    purpose: str,
    proposal_version: int,
    payload_limit_characters: int,
    report_contract_version: str,
    source_patch_sha256: str,
    shared_context_sha256: str,
) -> ReviewRequest:
    part_reviews = {
        item.get("part_id"): item for item in campaign.get("part_reviews", [])
        if isinstance(item, dict)
    }
    part_reports = "\n\n".join(
        f"### Part {part.part_id} report\n{part_reviews[part.part_id]['report']}" for part in parts
    )
    files_reviewed = campaign.get("files_reviewed", [])
    files_not_reviewed = campaign.get("files_not_reviewed", [])
    coverage = "\n".join(f"- `{path}` reviewed across its assigned patch segment(s)" for path in files_reviewed)
    coverage += "\nFiles not reviewed: " + (
        "; ".join(f"{item['path']}: {item['reason']}" for item in files_not_reviewed)
        if files_not_reviewed else "none"
    )
    sections = {
        "Review metadata": (
            f"Code review campaign {campaign['campaign_id']}; final integration review; "
            f"{len(parts)} individual parts; shared context SHA-256 {campaign['shared_context_sha256']}."
        ),
        **dict(shared_context),
        "Review file coverage": coverage,
        "Individual part reports": part_reports,
    }
    sections["Individual part reports"] += (
        "\n\nShared module/dependency map:\n" + shared_context["Repository/module map and dependencies"]
    )
    return _request(
        kind="integration", campaign_id=str(campaign["campaign_id"]), part_id="integration",
        purpose=purpose, proposal_version=proposal_version,
        payload_limit_characters=payload_limit_characters,
        report_contract_version=report_contract_version, header_id="code-review-integration-v1",
        source_patch_sha256=source_patch_sha256,
        shared_context_sha256=shared_context_sha256, part_patch_sha256=None,
        segment_sha256s=tuple(segment.patch_sha256 for part in parts for segment in part.segments),
        header=integration_review_header(), sections=sections,
    )


def shared_context_sha256(header: str, shared: Mapping[str, str]) -> str:
    prompt = format_context_prompt(
        redact_review_text(header),
        {name: redact_review_text(value).strip() for name, value in shared.items()},
    )
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

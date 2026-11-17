"""Deterministic parsing, validation, and rendering for review reports."""

from __future__ import annotations

import hashlib
import re
from collections import Counter

from workflow.review_models import ReportValidation, ReviewFinding, ReviewSegment

PART_REVIEW_VALIDATOR_VERSION = "part-review-v2"


def _review_sha256_text(value: str) -> str | None:
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None


PART_REVIEW_FIELDS = (
    "Severity", "Category", "File/location", "Description", "Impact and reasoning",
    "Recommended correction",
)


PART_REVIEW_SEVERITIES = ("Must fix", "Should fix", "Optional improvement")


PART_REVIEW_SECTIONS = (
    "Files reviewed", "Findings", "Cross-component concerns", "Overall assessment",
)


def _normalize_review_text(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^(?:\*\*|__|`)", "", value)
    value = re.sub(r"(?:\*\*|__|`)\s*$", "", value)
    return value.strip()


def parse_part_review_report(report: str, expected_part_id: str, segments: list[ReviewSegment]) -> dict:
    """Parse and substantively validate one independent part-review report."""
    errors: list[str] = []
    if not isinstance(report, str) or not report.strip():
        return {"valid": False, "errors": ["report is empty"], "findings": []}

    lines = report.splitlines()

    def values_for(pattern: str, source_lines: list[str] | None = None) -> list[tuple[int, str]]:
        candidates = lines if source_lines is None else source_lines
        return [(i, m.group(1).strip()) for i, line in enumerate(candidates)
                if (m := re.match(pattern, line, re.I))]

    section_re = re.compile(
        r"^\s{0,3}(?P<markdown>#{1,6}[ \t]+)?(?:\*\*)?"
        r"(?P<section>Files reviewed|Findings|Cross-component concerns|Overall assessment)"
        r"(?:\*\*)?(?:[ \t]*:[ \t]*(?:\*\*)?(?P<inline>.*?)|[ \t]*)$", re.I,
    )
    canonical = {name.casefold(): name for name in PART_REVIEW_SECTIONS}
    section_headings: list[tuple[int, str, str]] = []
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        fence_match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            if (fence_match and fence_match.group(1)[0] == fence[0]
                    and len(fence_match.group(1)) >= fence[1]):
                fence = None
            continue
        if fence_match:
            marker = fence_match.group(1)
            fence = (marker[0], len(marker))
            continue

        match = section_re.match(line)
        if not match:
            continue
        name = canonical[match.group("section").casefold()]
        inline = (match.group("inline") or "").strip()
        is_markdown_heading = match.group("markdown") is not None
        if (not is_markdown_heading and name == "Findings" and section_headings
                and section_headings[-1][1] == "Findings"
                and not any(item.strip() for item in lines[section_headings[-1][0] + 1:index])
                and re.fullmatch(r"(?i)(?:none|no findings\.?|n/?a)", inline)):
            # A repeated label with an explicit empty marker is common beneath `## Findings`.
            # Keep it as body content; a second actual section heading remains an error.
            continue
        section_headings.append((index, name, inline))

    section_starts = [index for index, _name, _inline in section_headings]
    header_lines = lines[:min(section_starts)] if section_starts else lines

    decisions = values_for(
        r"^\s*(?:#{1,6}\s*)?(?:\*\*)?Decision(?:\*\*)?\s*:\s*(?:\*\*)?(.*?)\s*(?:\*\*)?\s*$",
        header_lines,
    )
    if len(decisions) != 1:
        errors.append(f"expected exactly one Decision field; found {len(decisions)}")
        decision = None
    else:
        decision = _normalize_review_text(decisions[0][1]).upper()
        if decision not in {"APPROVED", "CHANGES_REQUESTED"}:
            errors.append("Decision must be APPROVED or CHANGES_REQUESTED")

    part_ids = values_for(
        r"^\s*(?:#{1,6}\s*)?(?:\*\*)?Part ID(?:\*\*)?\s*:\s*(?:\*\*)?(.*?)\s*(?:\*\*)?\s*$",
        header_lines,
    )
    if len(part_ids) != 1:
        errors.append(f"expected exactly one Part ID field; found {len(part_ids)}")
    else:
        actual_part_id = _normalize_review_text(part_ids[0][1])
        if actual_part_id != expected_part_id:
            errors.append(f"Part ID is {actual_part_id!r}; expected {expected_part_id!r}")

    heading_positions: dict[str, list[tuple[int, str]]] = {name: [] for name in PART_REVIEW_SECTIONS}
    for index, name, inline in section_headings:
        heading_positions[name].append((index, inline))
    for name, positions in heading_positions.items():
        if len(positions) != 1:
            errors.append(f"expected exactly one {name!r} section; found {len(positions)}")
    if all(len(heading_positions[name]) == 1 for name in PART_REVIEW_SECTIONS):
        ordered = [heading_positions[name][0][0] for name in PART_REVIEW_SECTIONS]
        if ordered != sorted(ordered):
            errors.append("required sections are out of order")
        if decisions and part_ids and not (decisions[0][0] < part_ids[0][0] < ordered[0]):
            errors.append("Decision and Part ID must appear before the report sections")

    sections: dict[str, str] = {}
    for name, positions in heading_positions.items():
        if len(positions) != 1:
            continue
        start, inline = positions[0]
        next_positions = [items[0][0] for section, items in heading_positions.items()
                          if items and items[0][0] > start]
        end = min(next_positions) if next_positions else len(lines)
        body = "\n".join(lines[start + 1:end]).strip()
        if name == "Findings":
            repeated_empty_marker = re.fullmatch(
                r"(?is)Findings\s*:\s*(None|No findings\.?|N/?A)", body,
            )
            if repeated_empty_marker:
                body = repeated_empty_marker.group(1)
        sections[name] = "\n".join(part for part in (inline, body) if part).strip()
        if not sections[name]:
            errors.append(f"{name!r} section is empty")

    files_section = sections.get("Files reviewed", "")
    listed_paths: set[str] = set()
    for line in files_section.splitlines():
        item = re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*?)\s*$", line)
        if not item:
            continue
        entry = item.group(1)
        quoted = re.findall(r"`([^`]+)`", entry)
        if quoted:
            listed_paths.update(quoted)
        else:
            listed_paths.add(entry.split(" — ", 1)[0].strip())
    assigned_paths = {segment.path for segment in segments}
    missing_paths = sorted(assigned_paths - listed_paths)
    if missing_paths:
        errors.append("Files reviewed is missing assigned path(s): " + ", ".join(missing_paths))

    findings_text = sections.get("Findings", "").strip()
    empty_findings = bool(re.fullmatch(r"(?is)(?:none|no findings\.?|n/?a)\s*", findings_text))
    findings: list[dict] = []
    if not findings_text:
        errors.append("Findings section must explicitly say None or contain finding blocks")
    elif not empty_findings:
        block_start = re.compile(
            r"^\s*(?:#{1,6}\s+(?:Finding\s+)?(F\d+|\d+)(?:\s*[-:.)].*)?"
            r"|\*\*Finding\s+(F\d+|\d+)\*\*|Finding\s+(F\d+|\d+)\s*:?)\s*$", re.I,
        )
        finding_starts = []
        for i, line in enumerate(findings_text.splitlines()):
            match = block_start.match(line)
            if match:
                identifier = next((value for value in match.groups() if value), None)
                if identifier:
                    finding_starts.append((i, identifier.upper()))
        finding_lines = findings_text.splitlines()
        if not finding_starts:
            errors.append("Findings must be exactly an explicit empty marker or contain individually headed finding blocks")
        else:
            if "\n".join(finding_lines[:finding_starts[0][0]]).strip():
                errors.append("unexpected text appears before the first finding block")
            ids = [identifier for _, identifier in finding_starts]
            if len(set(ids)) != len(ids):
                errors.append("finding block identifiers must be unique")
            for position, (start, identifier) in enumerate(finding_starts):
                end = finding_starts[position + 1][0] if position + 1 < len(finding_starts) else len(finding_lines)
                block = finding_lines[start + 1:end]
                fields: dict[str, list[list[str]]] = {name: [] for name in PART_REVIEW_FIELDS}
                current_field: str | None = None
                label_re = re.compile(
                    r"^\s*(?:[-*+]\s*)?(?:\*\*)?(Severity|Category|File/location|Description|"
                    r"Impact and reasoning|Recommended correction)(?:\*\*)?\s*:\s*(?:\*\*)?(.*?)"
                    r"\s*(?:\*\*)?\s*$", re.I,
                )
                field_names = {name.casefold(): name for name in PART_REVIEW_FIELDS}
                for body_line in block:
                    label = label_re.match(body_line)
                    if label:
                        current_field = field_names[label.group(1).casefold()]
                        fields[current_field].append([label.group(2).strip()])
                    elif current_field and body_line.strip():
                        fields[current_field][-1].append(body_line.strip())
                    elif body_line.strip():
                        errors.append(f"finding {identifier} has text outside a recognized field")
                finding: dict[str, str] = {"id": identifier}
                for name, values in fields.items():
                    if not values:
                        errors.append(f"finding {identifier} is missing required field {name!r}")
                        continue
                    if len(values) > 1:
                        errors.append(f"finding {identifier} repeats field {name!r}")
                    value = _normalize_review_text("\n".join(values[0]))
                    if not value:
                        errors.append(f"finding {identifier} has an empty {name!r} field")
                    finding[name] = value
                severity = finding.get("Severity", "")
                if severity.casefold() not in {item.casefold() for item in PART_REVIEW_SEVERITIES}:
                    errors.append(
                        f"finding {identifier} has unsupported severity {severity!r}; expected "
                        + ", ".join(PART_REVIEW_SEVERITIES)
                    )
                findings.append(finding)

    if decision == "APPROVED" and any(
            item.get("Severity", "").casefold() == "must fix" for item in findings):
        errors.append("Decision APPROVED contradicts a Must fix finding")
    if decision == "CHANGES_REQUESTED" and not any(
            item.get("Severity", "").casefold() == "must fix" for item in findings):
        errors.append("Decision CHANGES_REQUESTED requires at least one Must fix finding")

    return {
        "valid": not errors,
        "errors": errors,
        "decision": decision,
        "part_id": expected_part_id if len(part_ids) == 1 else None,
        "findings": findings,
        "sections": sections,
    }


def validate_part_review_report(
    report: str, expected_part_id: str, segments: list[ReviewSegment],
) -> ReportValidation:
    """Return a typed validation result while retaining the legacy dictionary parser."""
    parsed = parse_part_review_report(report, expected_part_id, segments)
    findings = tuple(
        ReviewFinding(
            identifier=item.get("id", ""), severity=item.get("Severity", ""),
            category=item.get("Category", ""), location=item.get("File/location", ""),
            description=item.get("Description", ""),
            impact_and_reasoning=item.get("Impact and reasoning", ""),
            recommended_correction=item.get("Recommended correction", ""),
        )
        for item in parsed.get("findings", [])
    )
    return ReportValidation(
        valid=bool(parsed.get("valid")), errors=tuple(parsed.get("errors", [])),
        decision=parsed.get("decision"), part_id=parsed.get("part_id"), findings=findings,
        sections=tuple((name, value) for name, value in parsed.get("sections", {}).items()),
    )


def _validated_campaign_reports(campaign: dict) -> list[dict]:
    reports: list[dict] = []
    for part in campaign.get("part_reviews", []):
        segments = [
            ReviewSegment(
                segment.get("path", ""), "", segment.get("patch_line_start", 1),
                segment.get("patch_line_end", 1), segment.get("patch_line_count", 1),
                segment.get("patch_sha256", ""),
            )
            for segment in part.get("segments", [])
        ]
        attempts = part.get("validated_reports") or ([{
            "report": part.get("report"),
            "prompt_sha256": part.get("context_sha256"),
            "campaign_call_number": None,
            "submitted_at": None,
        }] if part.get("status") == "reviewed" and part.get("report") else [])
        for attempt in attempts:
            raw_report = attempt.get("report")
            if not isinstance(raw_report, str):
                continue
            parsed = attempt.get("validation") or parse_part_review_report(
                raw_report, part.get("part_id", ""), segments,
            )
            reports.append({
                "part_id": part.get("part_id"),
                "attempt": attempt.get("campaign_call_number"),
                "submitted_at": attempt.get("submitted_at"),
                "report": raw_report,
                "validation": parsed,
            })
    return reports


def render_review_feedback(reports: list[dict]) -> str:
    """Surface validated feedback while retaining every source report verbatim."""
    if not reports:
        return "No validated code-review reports were recorded."
    lines = ["## Review decisions", ""]
    decisions = []
    for item in reports:
        validation = item.get("validation") or {}
        decision = validation.get("decision") or "unavailable"
        label = f"{item.get('part_id', 'unknown part')}"
        if item.get("attempt") is not None:
            label += f", call {item['attempt']}"
        decisions.append(f"- {label}: **{decision}**")
    lines.extend(decisions)

    grouped: dict[str, list[tuple[dict, dict]]] = {severity: [] for severity in PART_REVIEW_SEVERITIES}
    for item in reports:
        validation = item.get("validation") or {}
        for finding in validation.get("findings", []):
            severity = next((name for name in PART_REVIEW_SEVERITIES
                             if name.casefold() == finding.get("Severity", "").casefold()), None)
            if severity:
                grouped[severity].append((item, finding))
    for severity, heading in (("Must fix", "Blocking findings"), ("Should fix", "Non-blocking findings"),
                              ("Optional improvement", "Suggestions")):
        lines.extend(["", f"## {heading}", ""])
        items = grouped[severity]
        if not items:
            lines.append("None reported.")
            continue
        for item, finding in items:
            attempt = f", call {item['attempt']}" if item.get("attempt") is not None else ""
            lines.extend([
                f"### {finding.get('id', 'Finding')} — {item.get('part_id', 'unknown part')}{attempt}",
                "",
                f"- Category: {finding.get('Category', '')}",
                f"- Location: {finding.get('File/location', '')}",
                f"- Description: {finding.get('Description', '')}",
                f"- Why it matters: {finding.get('Impact and reasoning', '')}",
                f"- Recommended correction: {finding.get('Recommended correction', '')}",
                "",
            ])
    lines.extend(["## Reviewer observations and limits", ""])
    for item in reports:
        sections = (item.get("validation") or {}).get("sections", {})
        label = item.get("part_id", "unknown part")
        if item.get("attempt") is not None:
            label += f", call {item['attempt']}"
        lines.extend([
            f"### {label}", "",
            "Cross-component concerns:",
            sections.get("Cross-component concerns", "Not available."),
            "",
            "Overall assessment (including positive observations and limitations):",
            sections.get("Overall assessment", "Not available."),
            "",
        ])
    lines.extend(["## Original reviewer reports", ""])
    for item in reports:
        label = item.get("part_id", "unknown part")
        if item.get("attempt") is not None:
            label += f", call {item['attempt']}"
        lines.extend([f"### {label}", "", item["report"].rstrip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def _integration_report_is_structured(report: str) -> bool:
    return bool(
        re.search(r"(?im)^\s*Decision:\s*(?:APPROVED|CHANGES_REQUESTED)\b", report)
        and re.search(r"(?im)^\s*(?:#+\s*)?Consolidated findings\s*:?\s*(?:.*)?$", report)
        and re.search(r"(?im)^\s*(?:#+\s*)?Integration findings\s*:?\s*(?:.*)?$", report)
        and re.search(r"(?im)^\s*(?:#+\s*)?Overall assessment\s*:?\s*(?:.*)?$", report)
    )

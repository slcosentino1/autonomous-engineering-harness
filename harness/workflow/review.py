"""Pure review report contracts, parsing, rendering, and patch helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReviewDiff:
    """Complete text patch plus an auditable account of files filtered from it."""

    text: str
    included_paths: tuple[str, ...]
    excluded_files: tuple[tuple[str, str], ...]
    file_patches: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReviewSegment:
    """One complete file patch or an explicitly bounded, lossless patch segment."""

    path: str
    text: str
    patch_line_start: int
    patch_line_end: int
    patch_line_count: int
    patch_sha256: str


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

    section_line_re = re.compile(
        r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*)?(?:Files reviewed|Findings|Cross-component concerns|"
        r"Overall assessment)(?:\*\*)?(?:[ \t]*:.*|[ \t]*)$", re.I,
    )
    section_starts = [i for i, line in enumerate(lines) if section_line_re.match(line)]
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
    section_re = re.compile(
        r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*)?(Files reviewed|Findings|Cross-component concerns|Overall assessment)"
        r"(?:\*\*)?\s*:?\s*(?:\*\*)?[ \t]*(.*?)\s*$", re.I,
    )
    canonical = {name.casefold(): name for name in PART_REVIEW_SECTIONS}
    for index, line in enumerate(lines):
        match = section_re.match(line)
        if match:
            heading_positions[canonical[match.group(1).casefold()]].append((index, match.group(2).strip()))
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


def _review_patch_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_patch_boundaries(lines: list[str], path: str) -> list[int]:
    """Return conservative split positions at hunk, declaration, or prose boundaries."""
    boundaries: set[int] = set()
    declaration = re.compile(
        r"^(?:async\s+)?(?:def|class|function|interface|type|enum|struct|trait|impl|func|fn)\b"
        r"|^(?:export\s+)?(?:class|function|interface|type|enum)\b"
    )
    prose_file = Path(path).suffix.lower() in {".md", ".markdown", ".rst", ".txt", ".adoc"}
    for index in range(1, len(lines)):
        current = lines[index]
        previous = lines[index - 1]
        if current.startswith("@@") and any(line.startswith("@@") for line in lines[:index]):
            boundaries.add(index)
            continue
        if current[:1] in {"+", "-", " "}:
            content = current[1:].rstrip("\r\n")
        else:
            content = current.rstrip("\r\n")
        indentation = len(content) - len(content.lstrip(" \t"))
        if indentation == 0 and declaration.match(content):
            boundaries.add(index)
            continue
        previous_content = previous[1:].strip() if previous[:1] in {"+", "-", " "} else previous.strip()
        if not previous_content:
            is_heading = prose_file and content.lstrip().startswith("#")
            if is_heading or (prose_file and content.strip()):
                boundaries.add(index)
    return sorted(boundary for boundary in boundaries if 0 < boundary < len(lines))


def _path_affinity(left: str, right: str) -> tuple[int, str]:
    """Rank paths by actual shared structure without assuming an architectural style."""
    left_parts = Path(left).parts
    right_parts = Path(right).parts
    common_parent = 0
    for a, b in zip(left_parts[:-1], right_parts[:-1]):
        if a != b:
            break
        common_parent += 1
    left_stem = Path(left).stem.lower()
    right_stem = Path(right).stem.lower()
    left_test = re.sub(r"^(?:test_|tests?_)", "", left_stem)
    right_test = re.sub(r"^(?:test_|tests?_)", "", right_stem)
    stem_match = left_test == right_test or left_stem == right_test or right_stem == left_test
    score = common_parent * 10 + (8 if stem_match else 0)
    if Path(left).suffix.lower() == Path(right).suffix.lower():
        score += 1
    return score, right


def _part_coverage(segments: list[ReviewSegment], *, excluded_count: int) -> str:
    lines = ["Every patch line listed below is included in this review request; no file content was truncated."]
    for segment in segments:
        line_description = (
            f"patch lines {segment.patch_line_start}-{segment.patch_line_end} of {segment.patch_line_count}"
        )
        if segment.patch_line_start == 1 and segment.patch_line_end == segment.patch_line_count:
            line_description = "complete patch"
        lines.append(
            f"- `{segment.path}` — {line_description}; SHA-256 `{segment.patch_sha256}`"
        )
    lines.append(f"Changed metadata/binary/protected paths explicitly excluded and recorded: {excluded_count}.")
    return "\n".join(lines)


def _integration_report_is_structured(report: str) -> bool:
    return bool(
        re.search(r"(?im)^\s*Decision:\s*(?:APPROVED|CHANGES_REQUESTED)\b", report)
        and re.search(r"(?im)^\s*(?:#+\s*)?Consolidated findings\s*:?\s*(?:.*)?$", report)
        and re.search(r"(?im)^\s*(?:#+\s*)?Integration findings\s*:?\s*(?:.*)?$", report)
        and re.search(r"(?im)^\s*(?:#+\s*)?Overall assessment\s*:?\s*(?:.*)?$", report)
    )


def _review_partition_error(review_diff: ReviewDiff, parts: list[dict]) -> str | None:
    """Return the first lossless-coverage violation, without workflow side effects."""
    original = dict(review_diff.file_patches)
    assigned: dict[str, list[ReviewSegment]] = {path: [] for path in original}
    for part in parts:
        for segment in part["segments"]:
            if segment.path not in assigned:
                return f"Review partition contains an unknown changed file: {segment.path}."
            assigned[segment.path].append(segment)
    for path, patch in original.items():
        lines = patch.splitlines(keepends=True)
        segments = sorted(assigned[path], key=lambda item: item.patch_line_start)
        next_line = 1
        reconstructed = []
        for segment in segments:
            if (segment.patch_line_start != next_line
                    or segment.patch_line_count != len(lines)
                    or segment.patch_line_end < segment.patch_line_start
                    or segment.patch_line_end > len(lines)):
                return (
                    f"Review partition has a gap, overlap, or invalid range for {path}; "
                    "the file cannot be claimed as completely reviewed."
                )
            expected = "".join(lines[segment.patch_line_start - 1:segment.patch_line_end])
            if segment.text != expected or _review_patch_sha256(segment.text) != segment.patch_sha256:
                return f"Review partition content hash does not match the source patch for {path}."
            reconstructed.append(segment.text)
            next_line = segment.patch_line_end + 1
        if next_line != len(lines) + 1 or "".join(reconstructed) != patch:
            return f"Review partition does not cover the complete changed file patch: {path}."
    return None

"""Review contracts, prompt helpers, code-review campaigns, and local recovery."""

from __future__ import annotations

import hashlib
import copy
import json
import re
import uuid
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import claude_reviewer


PART_REVIEW_VALIDATOR_VERSION = "part-review-v2"
CLAUDE_MAX_PAYLOAD_ENV = claude_reviewer.MAX_REVIEW_PAYLOAD_ENV


class ReviewCampaignError(RuntimeError):
    """A code-review campaign could not safely proceed."""


def _review_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _review_redact(value: str) -> str:
    return claude_reviewer.redact(value)


def _safe_review_path(relative: str) -> str:
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(relative, ensure_ascii=True)
    return relative


def _format_context_prompt(header: str, sections: dict[str, str]) -> str:
    blocks = [header.strip()]
    for name, value in sections.items():
        content = value if name == "Complete patch" else value.strip()
        blocks.append(f"## {name}\n{content}")
    return "\n\n".join(blocks).strip()


def _review_sha256_text(value: str) -> str | None:
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None


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
    """Code-review campaign operations over workflow state and narrow callbacks."""

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

    @staticmethod
    def _legacy_code_review_header() -> str:
        """Return the exact v1 header so persisted requests can be verified without another provider call."""
        return (
            "Perform an independent, read-only code review. Treat the implementation and approved proposal as claims "
            "to verify against the original assignment. Apply all shared review criteria and the specific scope for "
            "this part. Do not implement changes. Return a structured report with exactly these headings: `Part ID: "
            "<id>`, `Files reviewed`, `Findings`, `Cross-component concerns`, and `Overall assessment`. Format each "
            "finding with fields `Severity`, `Category`, `File/location`, `Description`, `Impact and reasoning`, and "
            "`Recommended correction`. Write `Findings: None` when there are none. Start with `Decision: APPROVED` "
            "unless an acceptance-blocking defect warrants `Decision: CHANGES_REQUESTED`."
        )

    @staticmethod
    def _review_segment(
        path: str, patch: str, start: int = 1, end: int | None = None, total: int | None = None,
    ) -> ReviewSegment:
        lines = patch.splitlines(keepends=True)
        end = len(lines) if end is None else end
        total = len(lines) if total is None else total
        return ReviewSegment(path, patch, start, end, total, _review_patch_sha256(patch))

    @staticmethod
    def _part_coverage(segments: list[ReviewSegment], *, excluded_count: int) -> str:
        return _part_coverage(segments, excluded_count=excluded_count)

    def _part_sections(
        self,
        shared: dict[str, str],
        segments: list[ReviewSegment],
        *,
        campaign_id: str,
        part_id: str,
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
                f"Proposal version: {self.state.get('proposal_version', 1)}. This request contains all assigned patch lines. "
                "Return one structured individual-part report using the required format."
            ),
            **shared,
            "Review file coverage": self._part_coverage(segments, excluded_count=excluded_count),
            "Complete patch": "\n".join(patch_lines),
        }
        return sections

    @staticmethod
    def _serialized_review_fits(header: str, sections: dict[str, str], limit: int) -> bool:
        safe_header = _review_redact(header)
        safe_sections = {
            name: _review_redact(value) if name == "Complete patch" else _review_redact(value).strip()
            for name, value in sections.items()
        }
        prompt = _format_context_prompt(safe_header, safe_sections)
        try:
            claude_reviewer.validate_review_invocation(prompt, "aily", limit=limit)
            return True
        except claude_reviewer.ReviewError as exc:
            if "above the configured" in str(exc):
                return False
            raise ReviewCampaignError(str(exc)) from exc

    def _split_oversized_segment(
        self,
        path: str,
        patch: str,
        *,
        shared: dict[str, str],
        campaign_id: str,
        limit: int,
        excluded_count: int,
        header: str | None = None,
    ) -> list[ReviewSegment]:
        lines = patch.splitlines(keepends=True)
        safe_boundaries = _safe_patch_boundaries(lines, path)
        safe_boundaries.append(len(lines))
        safe_boundaries = sorted(set(safe_boundaries))
        chunks: list[tuple[int, int, str]] = []
        start = 0
        while start < len(lines):
            fitting_end = None
            for end in reversed([boundary for boundary in safe_boundaries if boundary > start]):
                candidate_text = "".join(lines[start:end])
                candidate = self._review_segment(path, candidate_text, start + 1, end, len(lines))
                if self._serialized_review_fits(
                    header or self._code_review_header(),
                    self._part_sections(
                        shared, [candidate], campaign_id=campaign_id, part_id="part-000001",
                        excluded_count=excluded_count,
                    ),
                    limit,
                ):
                    fitting_end = end
                    break
            if fitting_end is None:
                raise ReviewCampaignError(
                    f"Changed file {path} cannot be split at a safe patch boundary to fit the configured "
                    f"{limit}-character payload. Its full relevant patch remains unreviewed; no source was truncated."
                )
            chunks.append((start + 1, fitting_end, "".join(lines[start:fitting_end])))
            start = fitting_end
        total = len(lines)
        return [
            self._review_segment(path, text, first, last, total)
            for first, last, text in chunks
        ]

    def _partition_review_segments(
        self,
        review_diff: ReviewDiff,
        shared: dict[str, str],
        *,
        campaign_id: str,
        limit: int,
        header: str | None = None,
    ) -> tuple[list[dict], bool]:
        header = header or self._code_review_header()
        probe = self._review_segment("review-context-probe.txt", "x")
        probe_sections = self._part_sections(
            shared, [probe], campaign_id=campaign_id, part_id="part-000001",
            excluded_count=len(review_diff.excluded_files),
        )
        if not self._serialized_review_fits(header, probe_sections, limit):
            raise ReviewCampaignError(
                f"Required shared code-review context cannot fit within the configured {limit}-character "
                "payload even with a minimal patch. No files were truncated or omitted."
            )
        complete_segments = [self._review_segment(path, patch) for path, patch in review_diff.file_patches]
        full_sections = self._part_sections(
            shared, complete_segments, campaign_id=campaign_id, part_id="part-000001",
            excluded_count=len(review_diff.excluded_files),
        )
        if self._serialized_review_fits(header, full_sections, limit):
            parts = [{"part_id": "part-000001", "segments": complete_segments}]
            self._validate_review_partition(review_diff, parts)
            return parts, False

        segments: list[ReviewSegment] = []
        for path, patch in review_diff.file_patches:
            complete = self._review_segment(path, patch)
            singleton_sections = self._part_sections(
                shared, [complete], campaign_id=campaign_id, part_id="part-000001",
                excluded_count=len(review_diff.excluded_files),
            )
            if self._serialized_review_fits(header, singleton_sections, limit):
                segments.append(complete)
            else:
                segments.extend(self._split_oversized_segment(
                    path, patch, shared=shared, campaign_id=campaign_id, limit=limit,
                    excluded_count=len(review_diff.excluded_files), header=header,
                ))

        pending = sorted(segments, key=lambda item: (item.path, item.patch_line_start))
        planned: list[dict] = []
        while pending:
            seed = pending.pop(0)
            group = [seed]
            candidates = sorted(
                pending,
                key=lambda item: (
                    -max(_path_affinity(current.path, item.path)[0] for current in group),
                    item.path,
                    item.patch_line_start,
                ),
            )
            for candidate in candidates:
                trial = [*group, candidate]
                sections = self._part_sections(
                    shared, trial, campaign_id=campaign_id, part_id="part-000001",
                    excluded_count=len(review_diff.excluded_files),
                )
                if self._serialized_review_fits(header, sections, limit):
                    group.append(candidate)
                    pending.remove(candidate)
            part_id = f"part-{len(planned) + 1:06d}"
            planned.append({"part_id": part_id, "segments": group})

        for part in planned:
            sections = self._part_sections(
                shared, part["segments"], campaign_id=campaign_id, part_id=part["part_id"],
                excluded_count=len(review_diff.excluded_files),
            )
            if not self._serialized_review_fits(header, sections, limit):
                raise ReviewCampaignError(
                    f"Review part {part['part_id']} exceeds the configured payload after serialization; "
                    "the complete review was not submitted."
                )
        self._validate_review_partition(review_diff, planned)
        return planned, True

    @staticmethod
    def _validate_review_partition(review_diff: ReviewDiff, parts: list[dict]) -> None:
        error = _review_partition_error(review_diff, parts)
        if error is not None:
            raise ReviewCampaignError(error)

    @staticmethod
    def _campaign_source_hash(review_diff: ReviewDiff) -> str:
        content = {
            "patches": list(review_diff.file_patches),
            "exclusions": list(review_diff.excluded_files),
        }
        encoded = json.dumps(content, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _campaign_part_manifest(parts: list[dict]) -> list[dict]:
        return [
            {
                "part_id": part["part_id"],
                "segments": [
                    {
                        "path": segment.path,
                        "patch_line_start": segment.patch_line_start,
                        "patch_line_end": segment.patch_line_end,
                        "patch_line_count": segment.patch_line_count,
                        "patch_sha256": segment.patch_sha256,
                    }
                    for segment in part["segments"]
                ],
            }
            for part in parts
        ]

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
        campaign = {
            "campaign_id": campaign_id,
            "header_id": "code-review-part-v2",
            "report_contract_version": PART_REVIEW_VALIDATOR_VERSION,
            "status": "planned",
            "strategy": "multi_part_with_integration" if multipart else "single_call",
            "created_at": _review_now(),
            "payload_limit_characters": limit,
            "partition_limit_characters": limit,
            "source_patch_sha256": source_hash,
            "shared_context_sha256": shared_hash,
            "required_calls": required_calls,
            "authorized_call_limit": authorization_limit,
            "parts": part_manifest,
            "part_reviews": [
                {"part_id": part["part_id"], "status": "pending", "report": None, "calls": []}
                for part in parts
            ],
            "integration_review": {
                "required": multipart,
                "status": "pending" if multipart else "not_required",
                "report": None,
                "calls": [],
            },
            "files_reviewed": [],
            "files_not_reviewed": [
                {"path": path, "reason": "assigned to a pending review part"}
                for path in review_diff.included_paths
            ],
            "excluded_files": [
                {"path": _safe_review_path(path), "reason": reason}
                for path, reason in review_diff.excluded_files
            ],
            "consolidated_report": None,
        }
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
            campaign["status"] = "blocked_authorization"
            campaign["files_not_reviewed"] = [
                {"path": path, "reason": "campaign exceeds its persisted Claude code-review call authorization"}
                for path in review_diff.included_paths
            ]
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
        reviewed_segments = {
            (
                segment.get("path"), segment.get("patch_line_start"),
                segment.get("patch_line_end"), segment.get("patch_sha256"),
            )
            for part in campaign["part_reviews"] if part.get("status") == "reviewed"
            for segment in part.get("segments", [])
        }
        # Segment ownership is copied from the immutable part manifest at campaign creation.
        if not any(item.get("segments") for item in campaign["part_reviews"]):
            by_id = {item["part_id"]: item for item in campaign["part_reviews"]}
            for part in campaign["parts"]:
                by_id[part["part_id"]]["segments"] = part["segments"]
            reviewed_segments = {
                (
                    segment.get("path"), segment.get("patch_line_start"),
                    segment.get("patch_line_end"), segment.get("patch_sha256"),
                )
                for part in campaign["part_reviews"] if part.get("status") == "reviewed"
                for segment in part.get("segments", [])
            }
        expected_by_path: dict[str, set[tuple[str, int, int, str]]] = {}
        for part in campaign["parts"]:
            for segment in part["segments"]:
                identity = (
                    segment["path"], segment["patch_line_start"],
                    segment["patch_line_end"], segment["patch_sha256"],
                )
                expected_by_path.setdefault(segment["path"], set()).add(identity)
        campaign["files_reviewed"] = sorted(
            path for path, expected in expected_by_path.items() if expected <= reviewed_segments
        )
        campaign["files_not_reviewed"] = [
            {
                "path": path,
                "reason": next((
                    f"review part {part['part_id']} is {part['status']}"
                    for part in campaign["part_reviews"]
                    if any(segment["path"] == path for segment in part.get("segments", []))
                    and part["status"] != "reviewed"
                ), "one or more patch segments have not been reviewed"),
            }
            for path in sorted(expected_by_path) if path not in campaign["files_reviewed"]
        ]
        campaign["excluded_files"] = [
            {"path": _safe_review_path(path), "reason": reason}
            for path, reason in review_diff.excluded_files
        ]
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
        part_records = {record["part_id"]: record for record in campaign["part_reviews"]}
        for part_index, part in enumerate(parts, start=1):
            record = part_records[part["part_id"]]
            if record["status"] == "reviewed":
                continue
            if record["status"] in {"report_saved", "invalid_report"} and record.get("report"):
                validation = self._part_report_validation(
                    record["report"], part["part_id"], part["segments"],
                )
                previous_status = record["status"]
                record["status"] = "reviewed" if validation["valid"] else "invalid_report"
                record.setdefault("validation_history", []).append({
                    "validator_version": PART_REVIEW_VALIDATOR_VERSION,
                    "validated_at": _review_now(),
                    "previous_status": previous_status,
                    "valid": validation["valid"],
                    "errors": validation["errors"],
                    "report_sha256": _review_sha256_text(record["report"]),
                })
                record["segments"] = self._campaign_part_manifest([part])[0]["segments"]
                self._sync_review_coverage(campaign, review_diff)
                self._require_save()
                self._save()
                if record["status"] == "reviewed":
                    continue
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
                report = recoverable["report"]
                record["calls"].append({
                    "prompt_sha256": recoverable["prompt_sha256"], "cost_usd": recoverable.get("cost_usd"),
                    "call_state": recoverable["call_state"],
                    "campaign_call_number": recoverable.get("campaign_call_number"),
                })
                record["report"] = report
                record["context_sha256"] = recoverable["prompt_sha256"]
                validation = self._part_report_validation(report, part["part_id"], part["segments"])
                record["status"] = "reviewed" if validation["valid"] else "invalid_report"
                record.setdefault("validation_history", []).append({
                    "validator_version": PART_REVIEW_VALIDATOR_VERSION,
                    "validated_at": _review_now(),
                    "valid": validation["valid"],
                    "errors": validation["errors"],
                    "report_sha256": _review_sha256_text(report),
                })
                record["segments"] = self._campaign_part_manifest([part])[0]["segments"]
                self._sync_review_coverage(campaign, review_diff)
                self._require_save()
                self._save()
                if record["status"] != "reviewed":
                    raise ReviewCampaignError(
                        f"Persisted Claude report for {part['part_id']} is invalid: "
                        + "; ".join(validation["errors"])
                    )
                continue
            sections = self._part_sections(
                shared, part["segments"], campaign_id=campaign["campaign_id"],
                part_id=part["part_id"], excluded_count=len(review_diff.excluded_files),
            )
            patch_hash = _review_patch_sha256(sections["Complete patch"])
            part_trace = self._trace_context("review.part.provider_call", {
                "review_part_index": part_index,
                "review_part_count": len(parts),
                "retry_count": len(record.get("calls", [])) if isinstance(record.get("calls", []), list) else 0,
            })
            try:
                self._require_review_call()
                with part_trace:
                    report, manifest = self._call_claude(
                        self._code_review_header(), "code_review", sections,
                        metadata={
                            "proposal_version": self.state.get("proposal_version", 1),
                            "header_id": "code-review-part-v2",
                            "review_kind": "part",
                            "campaign_id": campaign["campaign_id"],
                            "part_id": part["part_id"],
                            "retry_count": len(record.get("calls", [])) if isinstance(record.get("calls", []), list) else 0,
                            "shared_context_sha256": campaign["shared_context_sha256"],
                            "part_patch_sha256": patch_hash,
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
            record["calls"].append({
                "prompt_sha256": manifest["prompt_sha256"], "cost_usd": manifest.get("cost_usd"),
                "call_state": manifest["call_state"], "campaign_call_number": manifest.get("campaign_call_number"),
            })
            record["report"] = report
            record["context_sha256"] = manifest["prompt_sha256"]
            validation = self._part_report_validation(report, part["part_id"], part["segments"])
            record["status"] = "reviewed" if validation["valid"] else "invalid_report"
            record.setdefault("validation_history", []).append({
                "validator_version": PART_REVIEW_VALIDATOR_VERSION,
                "validated_at": _review_now(),
                "valid": validation["valid"],
                "errors": validation["errors"],
                "report_sha256": _review_sha256_text(report),
            })
            record["segments"] = self._campaign_part_manifest([part])[0]["segments"]
            self._sync_review_coverage(campaign, review_diff)
            self._require_save()
            self._save()
            if hasattr(part_trace, "update"):
                part_trace.update(review_result=record["status"])
            if record["status"] != "reviewed":
                raise ReviewCampaignError(
                    f"Claude report for {part['part_id']} is invalid: "
                    + "; ".join(validation["errors"])
                    + ". The report remains preserved and the part is unreviewed."
                )

        if len(parts) == 1:
            campaign["integration_review"]["status"] = "not_required"
            if campaign["files_not_reviewed"]:
                raise ReviewCampaignError("The single-part review cannot complete while changed files remain unreviewed.")
            campaign["status"] = "complete"
            campaign["consolidated_report"] = part_records[parts[0]["part_id"]]["report"]
            self.state["review_campaign_call_count"] = sum(
                call.get("call_state") != "validation_failed"
                for call in part_records[parts[0]["part_id"]].get("calls", [])
            )
            self._require_save()
            self._save()
            return campaign["consolidated_report"]

        integration = campaign["integration_review"]
        if integration["status"] == "report_saved" and integration.get("report"):
            integration["status"] = (
                "reviewed" if self._integration_report_is_structured(integration["report"])
                else "invalid_report"
            )
            self._require_save()
            self._save()
        if integration["status"] != "reviewed":
            part_reports = "\n\n".join(
                f"### Part {part['part_id']} report\n{part_records[part['part_id']]['report']}"
                for part in parts
            )
            coverage = "\n".join(
                f"- `{path}` reviewed across its assigned patch segment(s)"
                for path in campaign["files_reviewed"]
            )
            coverage += "\nFiles not reviewed: " + (
                "; ".join(f"{item['path']}: {item['reason']}" for item in campaign["files_not_reviewed"])
                if campaign["files_not_reviewed"] else "none"
            )
            sections = {
                "Review metadata": (
                    f"Code review campaign {campaign['campaign_id']}; final integration review; "
                    f"{len(parts)} individual parts; shared context SHA-256 {campaign['shared_context_sha256']}."
                ),
                **shared,
                "Review file coverage": coverage,
                "Individual part reports": part_reports,
            }
            integration_header = (
                "Perform the final independent integration review for this multi-part campaign. Synthesize the "
                "individual reports and shared architecture context. Examine contracts, boundaries, data flow, "
                "inter-part assumptions, inconsistent error/state behavior, duplicated responsibilities, and defects "
                "that only appear across components. Deduplicate findings while preserving references. Add new "
                "integration findings only with evidence in the reports/context. Do not repeat local reviews."
                " Return `Decision: APPROVED` or `Decision: CHANGES_REQUESTED`, then use the exact headings "
                "`Consolidated findings`, `Integration findings`, and `Overall assessment`. Preserve original finding "
                "references and severity; state explicitly when a section has none."
            )
            # The integration payload is itself reviewed against the exact configured size before submission.
            integration_sections = {
                **sections,
                "Individual part reports": (
                    sections["Individual part reports"] + "\n\nShared module/dependency map:\n"
                    + shared["Repository/module map and dependencies"]
                ),
            }
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
                report = recoverable["report"]
                integration["calls"].append({
                    "prompt_sha256": recoverable["prompt_sha256"], "cost_usd": recoverable.get("cost_usd"),
                    "call_state": recoverable["call_state"],
                    "campaign_call_number": recoverable.get("campaign_call_number"),
                })
                integration["report"] = report
                integration["context_sha256"] = recoverable["prompt_sha256"]
                integration["status"] = (
                    "reviewed" if self._integration_report_is_structured(report) else "invalid_report"
                )
                self._require_save()
                self._save()
                if integration["status"] != "reviewed":
                    raise ReviewCampaignError("Persisted Claude integration output is not a complete structured report.")
            else:
                integration_trace = self._trace_context(
                    "review.integration.provider_call", {
                        "review_part_count": len(parts),
                    "retry_count": len(integration.get("calls", []))
                    if isinstance(integration.get("calls", []), list) else 0,
                    },
                )
                try:
                    self._require_review_call()
                    with integration_trace:
                        report, manifest = self._call_claude(
                            integration_header, "code_review", integration_sections,
                            metadata={
                                "proposal_version": self.state.get("proposal_version", 1),
                                "header_id": "code-review-integration-v1",
                                "review_kind": "integration",
                                "campaign_id": campaign["campaign_id"],
                                "part_id": "integration",
                                "retry_count": len(integration.get("calls", []))
                                if isinstance(integration.get("calls", []), list) else 0,
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
                integration["calls"].append({
                    "prompt_sha256": manifest["prompt_sha256"], "cost_usd": manifest.get("cost_usd"),
                    "call_state": manifest["call_state"],
                    "campaign_call_number": manifest.get("campaign_call_number"),
                })
                integration["report"] = report
                integration["context_sha256"] = manifest["prompt_sha256"]
                integration["status"] = (
                    "reviewed" if self._integration_report_is_structured(report) else "invalid_report"
                )
                self._require_save()
                self._save()
                if hasattr(integration_trace, "update"):
                    integration_trace.update(review_result=integration["status"])
                if integration["status"] != "reviewed":
                    raise ReviewCampaignError(
                        "Claude returned an incomplete integration report; campaign remains incomplete."
                    )

        if campaign["files_not_reviewed"] or any(
                part_records[part["part_id"]]["status"] != "reviewed" for part in parts):
            raise ReviewCampaignError("The review campaign cannot complete while any required part remains unreviewed.")
        campaign["consolidated_report"] = integration["report"]
        campaign["status"] = "complete"
        self.state["review_campaign_call_count"] = sum(
            sum(call.get("call_state") != "validation_failed" for call in record.get("calls", []))
            for record in campaign["part_reviews"]
        ) + sum(
            call.get("call_state") != "validation_failed" for call in integration.get("calls", [])
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

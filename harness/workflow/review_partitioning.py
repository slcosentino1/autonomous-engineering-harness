"""Lossless review partitioning and source manifest calculations."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Sequence

from workflow.review_models import ReviewCampaignError, ReviewDiff, ReviewSegment
from workflow.review_requests import part_sections, review_header, serialized_review_fits


def review_patch_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def review_segment(
    path: str, patch: str, start: int = 1, end: int | None = None, total: int | None = None,
) -> ReviewSegment:
    lines = patch.splitlines(keepends=True)
    resolved_end = len(lines) if end is None else end
    resolved_total = len(lines) if total is None else total
    return ReviewSegment(path, patch, start, resolved_end, resolved_total, review_patch_sha256(patch))


def safe_patch_boundaries(lines: Sequence[str], path: str) -> list[int]:
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


def path_affinity(left: str, right: str) -> tuple[int, str]:
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


def review_partition_error(review_diff: ReviewDiff, parts: Sequence[dict]) -> str | None:
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
            if segment.text != expected or review_patch_sha256(segment.text) != segment.patch_sha256:
                return f"Review partition content hash does not match the source patch for {path}."
            reconstructed.append(segment.text)
            next_line = segment.patch_line_end + 1
        if next_line != len(lines) + 1 or "".join(reconstructed) != patch:
            return f"Review partition does not cover the complete changed file patch: {path}."
    return None


def validate_review_partition(review_diff: ReviewDiff, parts: Sequence[dict]) -> None:
    error = review_partition_error(review_diff, parts)
    if error is not None:
        raise ReviewCampaignError(error)


def campaign_source_hash(
    review_diff: ReviewDiff, *, preserved_exclusions: tuple[tuple[str, str], ...] = (),
) -> str:
    content = {
        "patches": list(review_diff.file_patches),
        "exclusions": list(review_diff.excluded_files),
    }
    if preserved_exclusions:
        content["exclusions"] = sorted(set(review_diff.excluded_files) | set(preserved_exclusions))
    if (review_diff.workflow_paths or review_diff.context_files or review_diff.context_exclusions
            or review_diff.operation_diffs):
        content.update({
            "workflow_paths": list(review_diff.workflow_paths),
            "context_files": list(review_diff.context_files),
            "context_exclusions": list(review_diff.context_exclusions),
            "operation_diffs": list(review_diff.operation_diffs),
        })
    encoded = json.dumps(content, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def campaign_part_manifest(parts: Sequence[dict]) -> list[dict]:
    part_ids = [part.get("part_id") for part in parts if isinstance(part, dict)]
    if len(part_ids) != len(parts) or any(
        not isinstance(part_id, str) or not part_id.strip() for part_id in part_ids
    ):
        raise ReviewCampaignError("Review campaign parts must have non-empty part IDs.")
    if len(part_ids) != len(set(part_ids)):
        raise ReviewCampaignError("Review campaign part IDs must be unique.")
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


def split_oversized_segment(
    path: str,
    patch: str,
    *,
    shared: dict[str, str],
    campaign_id: str,
    proposal_version: int,
    limit: int,
    excluded_count: int,
    header: str | None = None,
) -> list[ReviewSegment]:
    lines = patch.splitlines(keepends=True)
    safe_boundaries = safe_patch_boundaries(lines, path)
    safe_boundaries.append(len(lines))
    safe_boundaries = sorted(set(safe_boundaries))
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(lines):
        fitting_end = None
        for end in reversed([boundary for boundary in safe_boundaries if boundary > start]):
            candidate_text = "".join(lines[start:end])
            candidate = review_segment(path, candidate_text, start + 1, end, len(lines))
            sections = part_sections(
                shared, [candidate], campaign_id=campaign_id, part_id="part-000001",
                proposal_version=proposal_version, excluded_count=excluded_count,
            )
            if serialized_review_fits(header or review_header(), sections, limit):
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
    return [review_segment(path, text, first, last, total) for first, last, text in chunks]


def partition_review_segments(
    review_diff: ReviewDiff,
    shared: dict[str, str],
    *,
    campaign_id: str,
    proposal_version: int,
    limit: int,
    header: str | None = None,
) -> tuple[list[dict], bool]:
    selected_header = header or review_header()
    excluded_count = len(review_diff.excluded_files)
    probe = review_segment("review-context-probe.txt", "x")
    probe_sections = part_sections(
        shared, [probe], campaign_id=campaign_id, part_id="part-000001",
        proposal_version=proposal_version, excluded_count=excluded_count,
    )
    if not serialized_review_fits(selected_header, probe_sections, limit):
        raise ReviewCampaignError(
            f"Required shared code-review context cannot fit within the configured {limit}-character "
            "payload even with a minimal patch. No files were truncated or omitted."
        )
    complete_segments = [review_segment(path, patch) for path, patch in review_diff.file_patches]
    full_sections = part_sections(
        shared, complete_segments, campaign_id=campaign_id, part_id="part-000001",
        proposal_version=proposal_version, excluded_count=excluded_count,
    )
    if serialized_review_fits(selected_header, full_sections, limit):
        parts = [{"part_id": "part-000001", "segments": complete_segments}]
        validate_review_partition(review_diff, parts)
        return parts, False

    segments: list[ReviewSegment] = []
    for path, patch in review_diff.file_patches:
        complete = review_segment(path, patch)
        singleton_sections = part_sections(
            shared, [complete], campaign_id=campaign_id, part_id="part-000001",
            proposal_version=proposal_version, excluded_count=excluded_count,
        )
        if serialized_review_fits(selected_header, singleton_sections, limit):
            segments.append(complete)
        else:
            segments.extend(split_oversized_segment(
                path, patch, shared=shared, campaign_id=campaign_id,
                proposal_version=proposal_version, limit=limit,
                excluded_count=excluded_count, header=selected_header,
            ))

    pending = sorted(segments, key=lambda item: (item.path, item.patch_line_start))
    planned: list[dict] = []
    while pending:
        seed = pending.pop(0)
        group = [seed]
        candidates = sorted(
            pending,
            key=lambda item: (
                -max(path_affinity(current.path, item.path)[0] for current in group),
                item.path,
                item.patch_line_start,
            ),
        )
        for candidate in candidates:
            trial = [*group, candidate]
            sections = part_sections(
                shared, trial, campaign_id=campaign_id, part_id="part-000001",
                proposal_version=proposal_version, excluded_count=excluded_count,
            )
            if serialized_review_fits(selected_header, sections, limit):
                group.append(candidate)
                pending.remove(candidate)
        part_id = f"part-{len(planned) + 1:06d}"
        planned.append({"part_id": part_id, "segments": group})

    for part in planned:
        sections = part_sections(
            shared, part["segments"], campaign_id=campaign_id, part_id=part["part_id"],
            proposal_version=proposal_version, excluded_count=excluded_count,
        )
        if not serialized_review_fits(selected_header, sections, limit):
            raise ReviewCampaignError(
                f"Review part {part['part_id']} exceeds the configured payload after serialization; "
                "the complete review was not submitted."
            )
    validate_review_partition(review_diff, planned)
    return planned, True

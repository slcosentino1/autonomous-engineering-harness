"""Immutable values shared by the deterministic review capabilities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal


class ReviewCampaignError(RuntimeError):
    """A code-review campaign could not safely proceed."""


@dataclass(frozen=True)
class ReviewDiff:
    """Workflow diff plus commitment-selected baseline/final context and exclusions."""

    text: str
    included_paths: tuple[str, ...]
    excluded_files: tuple[tuple[str, str], ...]
    file_patches: tuple[tuple[str, str], ...]
    workflow_paths: tuple[str, ...] = ()
    context_files: tuple[tuple[str, str, str, str], ...] = ()
    context_exclusions: tuple[tuple[str, str], ...] = ()
    operation_diffs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ReviewSegment:
    """One complete file patch or an explicitly bounded, lossless patch segment."""

    path: str
    text: str
    patch_line_start: int
    patch_line_end: int
    patch_line_count: int
    patch_sha256: str


@dataclass(frozen=True)
class CampaignPart:
    part_id: str
    segments: tuple[ReviewSegment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.segments, tuple):
            raise TypeError("CampaignPart.segments must be an immutable tuple.")


@dataclass(frozen=True)
class CampaignSnapshot:
    """Immutable JSON snapshot retaining the existing persisted campaign shape."""

    serialized: str
    contract_version: int = 1

    @classmethod
    def from_mapping(cls, campaign: dict) -> CampaignSnapshot:
        return cls(json.dumps(campaign, ensure_ascii=True, separators=(",", ":"), sort_keys=True))

    def to_mapping(self) -> dict:
        value = json.loads(self.serialized)
        if not isinstance(value, dict):
            raise ValueError("A campaign snapshot must contain a JSON object.")
        return value


@dataclass(frozen=True)
class CampaignInput:
    campaign_id: str
    purpose: str
    source_patch_sha256: str
    shared_context_sha256: str
    parts: tuple[CampaignPart, ...]
    shared_context: tuple[tuple[str, str], ...]
    excluded_files: tuple[tuple[str, str], ...]
    payload_limit_characters: int
    proposal_version: int
    report_contract_version: str
    part_header: str
    transition_at: str

    def __post_init__(self) -> None:
        for name in ("parts", "shared_context", "excluded_files"):
            if not isinstance(getattr(self, name), tuple):
                raise TypeError(f"CampaignInput.{name} must be an immutable tuple.")
        if any(not isinstance(item, tuple) for item in self.shared_context + self.excluded_files):
            raise TypeError("CampaignInput name/value pairs must be immutable tuples.")
        part_ids = [part.part_id for part in self.parts]
        if any(not isinstance(part_id, str) or not part_id.strip() for part_id in part_ids):
            raise ValueError("CampaignInput part IDs must be non-empty text values.")
        if len(part_ids) != len(set(part_ids)):
            raise ValueError("CampaignInput part IDs must be unique.")


@dataclass(frozen=True)
class RequestIdentity:
    """Stable identity for the exact review request a provider result answers."""

    campaign_id: str
    kind: Literal["part", "integration"]
    part_id: str
    purpose: str
    proposal_version: int
    proposal_sha256: str
    report_contract_version: str
    header_id: str
    source_patch_sha256: str
    shared_context_sha256: str
    part_patch_sha256: str | None
    segment_sha256s: tuple[str, ...]
    prompt_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.segment_sha256s, tuple):
            raise TypeError("RequestIdentity.segment_sha256s must be an immutable tuple.")


@dataclass(frozen=True)
class ReviewRequest:
    kind: Literal["part", "integration"]
    campaign_id: str
    part_id: str
    purpose: str
    proposal_version: int
    proposal_sha256: str
    payload_limit_characters: int
    report_contract_version: str
    header_id: str
    source_patch_sha256: str
    shared_context_sha256: str
    part_patch_sha256: str | None
    segment_sha256s: tuple[str, ...]
    header: str
    sections: tuple[tuple[str, str], ...]
    prompt: str
    prompt_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.sections, tuple) or not isinstance(self.segment_sha256s, tuple):
            raise TypeError("ReviewRequest sections and segment hashes must be immutable tuples.")

    def section_mapping(self) -> dict[str, str]:
        return dict(self.sections)

    @property
    def identity(self) -> RequestIdentity:
        return RequestIdentity(
            campaign_id=self.campaign_id,
            kind=self.kind,
            part_id=self.part_id,
            purpose=self.purpose,
            proposal_version=self.proposal_version,
            proposal_sha256=self.proposal_sha256,
            report_contract_version=self.report_contract_version,
            header_id=self.header_id,
            source_patch_sha256=self.source_patch_sha256,
            shared_context_sha256=self.shared_context_sha256,
            part_patch_sha256=self.part_patch_sha256,
            segment_sha256s=self.segment_sha256s,
            prompt_sha256=self.prompt_sha256,
        )


@dataclass(frozen=True)
class PreviousResult:
    part_id: str
    outcome: Literal["completed", "failed", "uncertain"]
    call_state: str
    report: str | None
    prompt_sha256: str | None
    cost_usd: float | None
    campaign_call_number: int | None
    received_at: str
    request_identity: RequestIdentity
    operation_id: str | None
    result_sha256: str | None
    recovered: bool = False


@dataclass(frozen=True)
class ReviewFinding:
    identifier: str
    severity: str
    category: str
    location: str
    description: str
    impact_and_reasoning: str
    recommended_correction: str


@dataclass(frozen=True)
class ReportValidation:
    valid: bool
    errors: tuple[str, ...]
    decision: str | None
    part_id: str | None
    findings: tuple[ReviewFinding, ...]
    sections: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CampaignTransition:
    snapshot: CampaignSnapshot
    decision: Literal["request_part", "request_integration", "complete", "blocked"]
    request: ReviewRequest | None = None
    part_id: str | None = None
    reason: str | None = None
    findings: tuple[str, ...] = ()

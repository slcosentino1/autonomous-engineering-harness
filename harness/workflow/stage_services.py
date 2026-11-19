"""Explicit, stage-scoped capabilities exposed to production implementations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class PlanningStageServices:
    state: dict[str, Any]
    target: Path
    call_claude: Callable[..., Any]
    guidance: Callable[..., Any]
    guidance_for_codex: Callable[..., str]
    guidance_for_claude: Callable[..., str]
    claude_skill_content_authorized: Callable[[], bool]
    repository_context_for_claude: Callable[..., str]
    run_codex: Callable[..., str]
    save: Callable[..., Any]
    write_proposal: Callable[..., Any]
    reassess_after_planning: Callable[..., Any]
    record_unvalidated_planning_output: Callable[..., Any]
    validate_proposal_ledger: Callable[..., Any]
    clean_markdown: Callable[..., str]
    valid_plan_reviews: Callable[..., list[dict]]
    now: Callable[[], str]
    architectural_decision_output: str
    commitment_ledger_guidance: str
    planning_principles: str
    plan_review_guidance: str


@dataclass(frozen=True)
class ImplementationStageServices:
    state: dict[str, Any]
    target: Path
    approval_required: Callable[[], bool]
    capture_commitment_evidence: Callable[..., Any]
    commitment_validation_baseline: Callable[..., str]
    guidance: Callable[..., Any]
    guidance_for_codex: Callable[..., str]
    run_codex: Callable[..., str]
    save: Callable[..., Any]
    execution_limits: Callable[..., Any]
    resource_usage: Callable[..., Any]
    now: Callable[[], str]
    commitment_ledger_guidance: str
    planning_principles: str


@dataclass(frozen=True)
class ReviewStageServices:
    state: dict[str, Any]
    target: Path
    capture_commitment_assessment: Callable[..., Any]
    require_write_operation_target: Callable[..., Any]
    review_diff: Callable[..., Any]
    prepare_review_campaign: Callable[..., Any]
    campaign_source_hash: Callable[..., str]
    capture_preexisting_snapshot: Callable[..., Any]
    fingerprint_repository: Callable[..., Any]
    run_review_campaign: Callable[..., str]
    record_review_snapshot: Callable[..., Any]
    clearly_no_review_findings: Callable[[str], bool]
    post_correction_dispositions: Callable[..., Any]
    now: Callable[[], str]
    redact: Callable[[str], str]
    save: Callable[..., Any]


@dataclass(frozen=True)
class DeliveryStageServices:
    state: dict[str, Any]
    target: Path
    fingerprint_repository: Callable[..., dict[str, Any]]
    delivery_path: Path
    verify_delivery_integrity: Callable[..., Any]
    capture_final_target_fingerprint: Callable[..., Any]
    evaluate_commitment_delivery: Callable[..., Any]
    delivery_report: Callable[..., str]
    atomic_text: Callable[..., Any]
    read_bytes: Callable[..., bytes]
    save: Callable[..., Any]
    now: Callable[[], str]


@dataclass(frozen=True)
class ProductionStageServices:
    """Grouped runtime services so each stage receives only its own capabilities."""

    planning: PlanningStageServices
    implementation: ImplementationStageServices
    review: ReviewStageServices
    delivery: DeliveryStageServices

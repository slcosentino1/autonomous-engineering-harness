"""Deterministic task assessment; recommendations never grant permissions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Mapping


_LEVELS = {"low": 0, "medium": 1, "high": 2}
_HIGH_RISK_TERMS = (
    "security", "credential", "secret", "migration", "delete", "production", "deploy", "infrastructure",
    "public api", "authentication", "authorization", "payment",
)
_MEDIUM_TERMS = ("integrat", "architecture", "multiple components", "concurrency", "external service")
_PLAN_HIGH_PATTERNS = (
    re.compile(r"\b(?:will|must|requires? to|plans? to) (?:perform|introduce|execute|apply)\b.{0,60}\bmigration\b", re.I),
    re.compile(r"\b(?:will|must|requires? to|plans? to) (?:change|modify|replace)\b.{0,60}\b(?:authentication|authorization|credentials|production infrastructure|payment flow)\b", re.I),
    re.compile(r"\b(?:production deployment|destructive data change) is required\b", re.I),
)
_PLAN_MEDIUM_PATTERNS = (
    re.compile(r"\b(?:will|must|requires? to|plans? to) (?:add|introduce|integrate)\b.{0,60}\b(?:external service|provider integration|concurrent execution)\b", re.I),
)


@dataclass(frozen=True)
class Assessment:
    complexity: str
    risk: str
    confidence: str
    reasons: tuple[str, ...]
    recommended_workflow: str
    proposal_recommended: bool
    approval_recommended: bool
    review_intensity: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess_task(task: str, signals: Mapping[str, Any] | None = None) -> Assessment:
    """Classify only explicit task text and supplied structured repository signals."""
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Assessment requires a non-empty task description.")
    signals = signals or {}
    text = task.casefold()
    reasons: list[str] = []
    high = [term for term in _HIGH_RISK_TERMS if term in text]
    medium = [term for term in _MEDIUM_TERMS if term in text]
    word_count = len(task.split())
    complexity = "high" if word_count > 160 else "medium" if word_count > 45 else "low"
    if word_count > 160:
        reasons.append("Task description exceeds 160 words.")
    elif word_count > 45:
        reasons.append("Task description exceeds 45 words.")
    risk = "high" if high else "medium" if medium else "low"
    if high:
        reasons.append("Task mentions risk-sensitive topics: " + ", ".join(high) + ".")
    if medium:
        reasons.append("Task mentions integration or architectural scope: " + ", ".join(medium) + ".")
    affected = signals.get("affected_components")
    if isinstance(affected, int) and not isinstance(affected, bool):
        if affected >= 4:
            complexity = "high"
            reasons.append("Repository context reports four or more affected components.")
        elif affected >= 2 and _LEVELS[complexity] < _LEVELS["medium"]:
            complexity = "medium"
            reasons.append("Repository context reports multiple affected components.")
    if not reasons:
        reasons.append("No configured risk-sensitive task signals were detected.")
    confidence = "medium" if len(task.split()) >= 12 else "low"
    if high or medium or (isinstance(affected, int) and not isinstance(affected, bool)):
        confidence = "high" if len(task.split()) >= 12 else "medium"
    return Assessment(
        complexity=complexity,
        risk=risk,
        confidence=confidence,
        reasons=tuple(reasons),
        recommended_workflow="full-development" if risk == "high" or complexity == "high" else "bugfix",
        proposal_recommended=risk != "low" or complexity != "low",
        approval_recommended=risk == "high" or confidence == "low",
        review_intensity="thorough" if risk == "high" or complexity == "high" else "standard",
    )


def assessment_from_dict(value: Any) -> Assessment:
    """Validate a persisted assessment before using it for policy decisions."""
    required = {
        "complexity", "risk", "confidence", "reasons", "recommended_workflow",
        "proposal_recommended", "approval_recommended", "review_intensity",
    }
    if not isinstance(value, Mapping) or not required.issubset(value):
        raise ValueError("Saved assessment is incomplete.")
    if (value["complexity"] not in _LEVELS or value["risk"] not in _LEVELS
            or value["confidence"] not in _LEVELS
            or not isinstance(value["reasons"], (list, tuple))
            or any(not isinstance(reason, str) for reason in value["reasons"])
            or not isinstance(value["recommended_workflow"], str)
            or type(value["proposal_recommended"]) is not bool
            or type(value["approval_recommended"]) is not bool
            or not isinstance(value["review_intensity"], str)):
        raise ValueError("Saved assessment is invalid.")
    return Assessment(
        complexity=value["complexity"], risk=value["risk"], confidence=value["confidence"],
        reasons=tuple(value["reasons"]), recommended_workflow=value["recommended_workflow"],
        proposal_recommended=value["proposal_recommended"], approval_recommended=value["approval_recommended"],
        review_intensity=value["review_intensity"],
    )


def reassess_task(
    previous: Assessment,
    task: str,
    signals: Mapping[str, Any] | None = None,
    *,
    plan_text: str = "",
) -> Assessment:
    """Escalate from task signals and explicit planned operations, ignoring plan length."""
    updated = assess_task(task, signals)
    complexity = max((previous.complexity, updated.complexity), key=_LEVELS.__getitem__)
    plan_reasons: list[str] = []
    plan_risk = "low"
    if any(pattern.search(plan_text) for pattern in _PLAN_HIGH_PATTERNS):
        plan_risk = "high"
        plan_reasons.append("The plan describes an explicit high-impact operation.")
    elif any(pattern.search(plan_text) for pattern in _PLAN_MEDIUM_PATTERNS):
        plan_risk = "medium"
        plan_reasons.append("The plan describes an explicit external integration or concurrency change.")
    risk = max((previous.risk, updated.risk, plan_risk), key=_LEVELS.__getitem__)
    confidence = min((previous.confidence, updated.confidence), key=_LEVELS.__getitem__)
    return Assessment(
        complexity=complexity,
        risk=risk,
        confidence=confidence,
        reasons=tuple(dict.fromkeys((*previous.reasons, *updated.reasons, *plan_reasons))),
        recommended_workflow=("full-development" if risk == "high" or complexity == "high"
                              else updated.recommended_workflow),
        proposal_recommended=previous.proposal_recommended or updated.proposal_recommended,
        approval_recommended=previous.approval_recommended or updated.approval_recommended,
        review_intensity=("thorough" if risk == "high" or complexity == "high" else updated.review_intensity),
    )

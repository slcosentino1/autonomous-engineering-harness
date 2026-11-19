"""Explainable task assessment; recommendations never grant permissions."""

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
    task_type: str = "engineering"
    production_impact: bool = False
    security_impact: bool = False
    architectural_impact: bool = False
    requires_investigation: bool = False
    ambiguous: bool = False
    documentation_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess_task(task: str, signals: Mapping[str, Any] | None = None) -> Assessment:
    """Classify task text plus explicit structured repository signals."""
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Assessment requires a non-empty task description.")
    signals = signals or {}
    text = task.casefold()
    reasons: list[str] = []
    high = [term for term in _HIGH_RISK_TERMS if term in text]
    medium = [term for term in _MEDIUM_TERMS if term in text]
    word_count = len(task.split())
    documentation_only = bool(re.search(r"\b(docs?|documentation|readme|typo|spelling|wording)\b", text))
    review_only = bool(re.search(r"\b(review|code review|audit|inspect)\b", text))
    bugfix = bool(re.search(r"\b(fix|bug|defect|regression|broken)\b", text))
    feature = bool(re.search(r"\b(implement|add|build|create|introduce|new feature)\b", text))
    task_type = "review" if review_only else "documentation" if documentation_only else (
        "bugfix" if bugfix else "feature" if feature else "engineering"
    )
    production_impact = any(term in text for term in ("production", "deploy", "payment", "migration"))
    security_impact = any(term in text for term in ("security", "credential", "secret", "authentication", "authorization"))
    architectural_impact = any(term in text for term in ("architecture", "multiple components", "concurrency"))
    ambiguous = bool(re.search(r"\b(improve it|fix it|make it better|something is wrong|clean this up)\b", text))
    complexity = "high" if word_count > 160 or architectural_impact else "medium" if word_count > 45 else "low"
    if word_count > 160 or architectural_impact:
        reasons.append("Task scope indicates substantial or architectural work.")
    elif word_count > 45:
        reasons.append("Task description spans multiple requirements or components.")
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
    if signals.get("security_impact") is True:
        security_impact, risk = True, "high"
        reasons.append("Repository context reports security-sensitive scope.")
    if signals.get("production_impact") is True:
        production_impact, risk = True, "high"
        reasons.append("Repository context reports production impact.")
    if signals.get("architectural_impact") is True:
        architectural_impact, complexity = True, "high"
        reasons.append("Repository context reports architectural impact.")
    if signals.get("ambiguous") is True:
        ambiguous = True
        reasons.append("Repository context reports unresolved task ambiguity.")
    if not reasons:
        reasons.append("No configured risk-sensitive task signals were detected.")
    confidence = "low" if ambiguous or word_count < 4 else "medium"
    if task_type in {"review", "documentation", "bugfix", "feature"} and not ambiguous:
        confidence = "high"
    if high or medium or (isinstance(affected, int) and not isinstance(affected, bool)):
        confidence = "high" if word_count >= 12 and not ambiguous else "medium"
    if ambiguous:
        confidence = "low"
    recommended = (
        "review-only" if review_only else
        "full-development" if risk == "high" or complexity == "high" or architectural_impact or feature else
        "quick-fix" if documentation_only or (bugfix and word_count <= 25) else
        "bugfix"
    )
    return Assessment(
        complexity=complexity,
        risk=risk,
        confidence=confidence,
        reasons=tuple(reasons),
        recommended_workflow=recommended,
        proposal_recommended=(risk != "low" or complexity != "low" or feature or architectural_impact),
        approval_recommended=(risk == "high" or confidence == "low"),
        review_intensity="thorough" if risk == "high" or complexity == "high" else "standard",
        task_type=task_type,
        production_impact=production_impact,
        security_impact=security_impact,
        architectural_impact=architectural_impact,
        requires_investigation=(task_type == "bugfix" or risk != "low" or ambiguous),
        ambiguous=ambiguous,
        documentation_only=documentation_only,
    )


def assessment_from_dict(value: Any) -> Assessment:
    """Validate persisted assessment values while reading older state formats."""
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
    booleans = (
        "production_impact", "security_impact", "architectural_impact",
        "requires_investigation", "ambiguous", "documentation_only",
    )
    if any(key in value and type(value[key]) is not bool for key in booleans):
        raise ValueError("Saved assessment is invalid.")
    return Assessment(
        complexity=value["complexity"], risk=value["risk"], confidence=value["confidence"],
        reasons=tuple(value["reasons"]), recommended_workflow=value["recommended_workflow"],
        proposal_recommended=value["proposal_recommended"], approval_recommended=value["approval_recommended"],
        review_intensity=value["review_intensity"],
        task_type=value.get("task_type", "engineering"),
        **{key: value.get(key, False) for key in booleans},
    )


def reassess_task(
    previous: Assessment,
    task: str,
    signals: Mapping[str, Any] | None = None,
    *,
    plan_text: str = "",
) -> Assessment:
    """Escalate risk from explicit plan operations without treating plan length as complexity."""
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
        proposal_recommended=previous.proposal_recommended or updated.proposal_recommended or plan_risk != "low",
        approval_recommended=previous.approval_recommended or updated.approval_recommended or plan_risk == "high",
        review_intensity=("thorough" if risk == "high" or complexity == "high" else updated.review_intensity),
        task_type=updated.task_type,
        production_impact=previous.production_impact or updated.production_impact or plan_risk == "high",
        security_impact=previous.security_impact or updated.security_impact,
        architectural_impact=previous.architectural_impact or updated.architectural_impact or plan_risk == "medium",
        requires_investigation=previous.requires_investigation or updated.requires_investigation,
        ambiguous=previous.ambiguous or updated.ambiguous,
        documentation_only=previous.documentation_only or updated.documentation_only,
    )

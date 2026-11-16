"""Helpers for evidence-based architectural proposal reviews."""

from __future__ import annotations

import re


SCORECARD_CRITERIA = (
    "Requirement understanding",
    "Architectural decision completeness",
    "Alternatives analysis",
    "Trade-off reasoning",
    "Operational and failure-mode thinking",
    "Assumption management",
    "Review effectiveness",
)

_SCORECARD_HEADING = re.compile(r"(?im)^\s*#{1,6}\s+Planning quality scorecard\s*#*\s*$")
_SCORE_ROW = re.compile(
    r"^\s*[-*]\s*(?P<criterion>[^:]+):\s*(?P<score>\d+)\s*/\s*5\s*[—–-]\s*"
    r"Evidence:\s*(?P<evidence>.*?)\s*$",
    re.IGNORECASE,
)


def build_plan_review_input(
    *,
    iteration: int,
    max_plan_reviews: int,
    proposal_version: int,
    assignment: str,
    proposal: str,
    target: str,
    repository_context: str,
    planning_principles: str,
    relevant_skills: str,
    review_guidance: str,
) -> tuple[str, dict[str, str], dict]:
    """Build deterministic plan-review sections without workflow state or provider access."""
    disposition_heading = re.search(
        r"(?im)^\s*##\s+Claude finding dispositions\s*$", proposal,
    )
    proposal_for_review = proposal
    if disposition_heading:
        proposal_for_review = proposal[:disposition_heading.start()].rstrip()
    header = (
        "Act as an independent Senior Software Architect reviewing a proposal. Inspect the complete assignment, "
        "current proposal, repository context, workflow constraints, and listed skill guidance. "
        "Do not rely on prior review conclusions or Codex dispositions; they are withheld. Give evidence-based "
        "findings and a scorecard. Do not implement code."
    )
    sections = {
        "Review metadata": (
            f"Plan review iteration {iteration} of at most {max_plan_reviews}; proposal version {proposal_version}; "
            "all required context sections below are complete."
        ),
        "Assignment": assignment,
        "Current proposal": proposal_for_review,
        "Repository context": repository_context,
        "Workflow constraints": (
            f"Target: {target}. The proposal is awaiting human approval; implementation is not authorized "
            "until the user approves it. The workflow does not authorize commits, pushes, merges, deployments, "
            "publication, shared-infrastructure changes, or access to unrelated repositories. Preserve the "
            "existing assignment and workflow budget."
        ),
        "Engineering Judgment & Proportional Design": planning_principles,
        "Relevant skills": relevant_skills,
        "Review duties": review_guidance,
        "Independence boundary": (
            "Prior Claude reports and Codex finding dispositions are deliberately withheld from this review. "
            "Base the assessment on the assignment and current proposal supplied here. The proposal's revision "
            "number is context only; do not infer that earlier conclusions were correct."
        ),
    }
    metadata = {
        "iteration": iteration,
        "proposal_version": proposal_version,
        "header_id": "plan-review-v2",
    }
    return header, sections, metadata


def normalize_scorecard(report: str) -> tuple[str, dict]:
    """Calculate and append an evidence-backed mean without imposing an approval threshold."""
    heading = _SCORECARD_HEADING.search(report)
    if heading is None:
        parsed = {"complete": False, "scores": {}, "evidence": {}, "mean": None,
                  "missing": list(SCORECARD_CRITERIA), "invalid": []}
        return _append_status(report, parsed), parsed

    tail = report[heading.end():]
    next_heading = re.search(r"(?m)^\s*#{1,6}\s+", tail)
    scorecard_text = tail[:next_heading.start()] if next_heading else tail
    criterion_lookup = {name.casefold(): name for name in SCORECARD_CRITERIA}
    scores: dict[str, int] = {}
    evidence: dict[str, str] = {}
    invalid: list[str] = []
    for line in scorecard_text.splitlines():
        match = _SCORE_ROW.match(line)
        if not match:
            continue
        criterion = criterion_lookup.get(match.group("criterion").strip().casefold())
        if criterion is None:
            continue
        if criterion in scores:
            invalid.append(f"duplicate rating for {criterion}")
            continue
        score = int(match.group("score"))
        item_evidence = match.group("evidence").strip()
        if score < 1 or score > 5 or len(item_evidence) < 12:
            invalid.append(f"rating or evidence is invalid for {criterion}")
            continue
        scores[criterion] = score
        evidence[criterion] = item_evidence

    missing = [name for name in SCORECARD_CRITERIA if name not in scores]
    complete = not missing and not invalid
    parsed = {
        "complete": complete,
        "scores": scores,
        "evidence": evidence,
        "mean": round(sum(scores.values()) / len(SCORECARD_CRITERIA), 2) if complete else None,
        "missing": missing,
        "invalid": invalid,
    }
    return _append_status(report, parsed), parsed


def _append_status(report: str, parsed: dict) -> str:
    if parsed["complete"]:
        status = (
            "\n\n**Harness-calculated arithmetic mean:** "
            f"{parsed['mean']:.2f}/5. This score is descriptive and is not an approval threshold."
        )
    else:
        details = []
        if parsed["missing"]:
            details.append("missing or unsupported evidence for: " + ", ".join(parsed["missing"]))
        if parsed["invalid"]:
            details.extend(parsed["invalid"])
        status = (
            "\n\n**Planning quality scorecard status:** INCOMPLETE; no mean was calculated. "
            + "; ".join(details or ["the scorecard was not supplied"])
            + "."
        )
    return report.rstrip() + status

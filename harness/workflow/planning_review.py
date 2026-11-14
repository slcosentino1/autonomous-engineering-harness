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

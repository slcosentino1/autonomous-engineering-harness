"""Helpers for evidence-based architectural proposal reviews."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from workflow.skills import file_inventory_section
from workflow.state import WorkflowError

ARCHITECTURAL_DECISION_OUTPUT = """Choose the simplest architecture that adequately satisfies the functional and operational requirements. Do not
optimize for minimum component count at the expense of correctness, reliability, concurrency, scalability, or operations.
For planning and proposal refinement, apply the architecture-design skill by recording a concise
`Architectural decisions and trade-offs` section. For each material decision, state the choice, viable alternatives,
the relevant consequences and user or operational impact, assumptions or unknowns, accepted risks, and what evidence
would change the choice. Explain why a deferred capability is unnecessary now, its accepted risk, likely later cost,
and a concrete reconsideration trigger. Include only decisions material to this assignment. Concurrency, idempotency,
storage, recovery, and failure behavior are examples of areas to assess when relevant, not mandatory components or
requirements. A simple, well-justified design can be the best result."""

COMMITMENT_LEDGER_GUIDANCE = """Commitment ledger contract
Represent only material commitments. Do not convert every proposal sentence or general best practice into an
implementation obligation. Classify each ledger entry as one of: mandatory_user_requirement,
accepted_architectural_commitment, required_validation_scenario, optional_recommendation,
future_production_consideration, or explicitly_out_of_scope. Use availability active or externally_blocked
independently from class. Give every entry a stable ID such as C-001, a concise statement, an observable acceptance
condition, implementation_expectations, and verification_scenarios (an array of {id, description}; use an empty
array only when no specific scenario is required). Include implementation_basis: use new_change when the approved
requirement needs a workflow-introduced change; use existing_behavior_acceptable only when the acceptance condition
explicitly allows existing behavior to satisfy it after current validation. Baseline inspection alone is never
implementation evidence. When known, list commitment-driven paths and symbols in relevant_sources as an array of
{path, symbol} objects; those references set review scope but do not establish fulfillment. Partial-delivery policy is optional. If no partial delivery is
authorized, omit the `partial_delivery_policy` property entirely; do not add an empty policy or infer permission.
When authorizations are explicitly approved, use exactly this structure:
`partial_delivery_policy: {"reason":"non-empty policy-level reason","authorizations":[{"commitment_id":"C-001","allowed_dispositions":["externally_blocked"]}]}`.
The non-empty `reason` belongs to the `partial_delivery_policy` object itself, never inside an authorization.
Each authorization contains `commitment_id` and `allowed_dispositions`; allowed dispositions are externally_blocked,
deferred, waived, or unverified. Do not infer partial-delivery authorization. Return exactly one machine-readable
fenced block in this form:

```commitment-ledger
{"schema_version":1,"commitments":[{"id":"C-001","class":"mandatory_user_requirement","statement":"...","acceptance_condition":"...","implementation_expectations":["..."],"implementation_basis":"new_change","relevant_sources":[{"path":"src/service.py","symbol":"process_request"}],"verification_scenarios":[{"id":"scenario-1","description":"..."}],"availability":"active"}]}
```

If a partial-delivery policy is present, add it as a top-level property of that same ledger JSON object using the
exact policy structure above. If no partial authorizations are approved, leave the property out.
The user's existing proposal approval explicitly approves the visible classifications, acceptance conditions, and
any partial-delivery policy. Preserve the block during every proposal refinement and human revision."""

PLANNING_PRINCIPLES = """Engineering Judgment & Proportional Design
1. Explicit requirements take priority over technical recommendations.
2. Do not turn general best practices into requirements automatically.
3. Choose the simplest solution that satisfies the requirements.
4. Compare alternatives when justifying important architectural decisions.
5. Avoid unnecessary abstractions, services, and components.
6. Separate must implement, should implement, future production considerations, and out of scope.
7. Do not invent product behavior; ask the user when a decision changes expected behavior.
8. Make reversible technical assumptions autonomously and document them.

Understand the problem before designing:
- Identify explicit requirements, implicit constraints, business rules, inputs, outputs, and expected outcomes.
- Trace the lifecycle of the main operations, including relevant component interactions and external systems.
- Explain how the proposed components communicate and what information crosses their boundaries.

Think beyond the happy path. Consider partial failures, unexpected responses, timeouts and uncertain outcomes,
concurrent or duplicate requests, restarts and interrupted execution, invalid data, resource constraints, and
operational limits. These are analysis prompts, not automatic implementation requirements. Address the scenarios
that matter to the assignment and explain why other scenarios do not change the design.

Analyze integrations and uncertainty:
- Identify external systems, downstream dependencies, known contracts, and missing contract information.
- Explain what can be implemented independently and what depends on another team or system.
- Consider integration errors, timeouts, authentication, data contracts, and compatibility where relevant.
- State important unknowns, why they matter, and plausible interpretations and consequences.
- Separate confirmed requirements from assumptions. Never silently invent external behavior or product rules.
- Make reversible technical assumptions when safe. Ask the user about ambiguities that change product behavior,
  business rules, or an important architectural decision that cannot safely be assumed.
- Identify what remains incomplete until an external dependency is clarified.

Evaluate the architecture by its consequences. Consider maintainability, reliability, complexity, and operational
cost. Compare realistic alternatives when their consequences differ materially. Do not add infrastructure, patterns,
abstractions, or components without a clear requirement or risk-based justification. Do not turn every identified
risk into implementation work.

Plan verification from the start. Define observable outcomes and acceptance criteria for important requirements.
Identify relevant functional, integration, failure, and boundary checks. State which checks can run locally, which
need a mock or stub, and which require a real external integration.

Classify decisions as must implement, should implement, future production considerations, out of scope, open
questions or ambiguities, and external dependencies. Use only the categories that apply. Keep the proposal at the
level of detail needed to explain the reasoning; do not make it longer or more sophisticated for its own sake."""

PLAN_REVIEW_GUIDANCE = """Act as an independent architectural reviewer, not only a requirements checklist. First assess the current
assignment and proposal on their own merits. Search for material decisions that are missing, viable alternatives that
were skipped, simplifications with unexamined consequences, weak trade-off reasoning, unstated assumptions, and
deferrals without an assessed risk, later cost, or reconsideration trigger. Examine concurrency, idempotency,
persistence, recovery, and failure behavior when relevant; these are not mandatory design features. Do not penalize a
simple architecture when it adequately meets the needs and its consequences are justified. A material choice that
was not analyzed is still an open gap even if the chosen design appears plausible. Do not mistake minimum component
count for simplicity when it sacrifices correctness, reliability, concurrency, scalability, or operational fit.

For each actionable finding, identify the decision or missing aspect, the alternative or scenario not evaluated, its
consequence, why that consequence matters here, and the concrete change or clarification recommended. Classify it as
an architectural defect, an acceptable but undocumented risk, insufficient information, or an optional improvement.
Do not elevate personal preferences or speculative enhancements into blockers. Important gaps must be findings that
Codex can address in its existing refinement step.

Return a `## Planning quality scorecard` with exactly one line per criterion in this form:
`- Criterion: N/5 — Evidence: specific proposal section, decision, or omission`
Score these seven criteria: Requirement understanding; Architectural decision completeness; Alternatives analysis;
Trade-off reasoning; Operational and failure-mode thinking; Assumption management; Review effectiveness. Use 1 for
unsupported or materially inadequate evidence, 3 for adequate but incomplete reasoning, and 5 for explicit, strong,
proportionate reasoning; use 2 and 4 for intermediate evidence. For the first six criteria, score the proposal; score
Review effectiveness by this review's coverage and evidence-supported findings, not by the number of findings. Cite
concrete evidence for every rating. Score reasoning, not proposal length or component count. The Harness calculates
the arithmetic mean; do not supply an aggregate. Scores are descriptive and have no approval threshold. A low score
is not automatically a blocker, but explain any important gap as a finding."""



SCORECARD_CRITERIA = (
    "Requirement understanding",
    "Architectural decision completeness",
    "Alternatives analysis",
    "Trade-off reasoning",
    "Operational and failure-mode thinking",
    "Assumption management",
    "Review effectiveness",
)


def clean_markdown(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.strip().splitlines()).strip() + "\n"


def repository_context_for_claude(guidance: dict, review_paths: list[str] | None = None) -> str:
    files = guidance.get("files") or []
    if review_paths is None:
        parts = [file_inventory_section(files)]
    else:
        by_root: dict[str, int] = {}
        for path in files:
            root = Path(path).parts[0] if Path(path).parts else "(repository root)"
            by_root[root] = by_root.get(root, 0) + 1
        parts = [
            "Repository inventory by observed top-level path (counts provide orientation; they do not imply architecture): "
            + (", ".join(f"{path}: {count} file(s)" for path, count in sorted(by_root.items())) or "empty repository")
        ]
        parts.append("Changed text files reviewed by this campaign: " + ", ".join(review_paths))
    instructions = guidance.get("instructions") or []
    if instructions:
        parts.append("Applicable repository instructions:")
        parts.extend(f"--- {item['path']} ---\n{item['content']}" for item in instructions)
    else:
        parts.append("No applicable AGENTS.md instructions were found in or above the target repository.")
    return "\n\n".join(parts)


def review_history_for_prompt(state: dict) -> str:
    records = state.get("plan_reviews", [])
    if not records:
        return "No previous Claude plan-review attempts."
    decisions = state.get("plan_review_decisions", [])
    parts = []
    for record in records:
        iteration = record.get("iteration", "unknown")
        if record.get("valid") is not True or not record.get("context_manifest"):
            report_hash = hashlib.sha256(record.get("report", "").encode("utf-8")).hexdigest()
            parts.append(
                f"Invalidated attempt {record.get('attempt', iteration)} (logical iteration {iteration}): "
                f"{record.get('invalidated_reason', 'context completeness is unverified')}\n"
                f"Codex disposition: {record.get('codex_disposition', 'Do not count or reuse its findings.')}\n"
                f"The unverified report is retained in workflow state for audit only; SHA-256: {report_hash}."
            )
            continue
        prior_decision = next((item for item in decisions if item.get("iteration") == iteration), None)
        disposition = prior_decision.get("dispositions") if prior_decision else "No Codex disposition recorded."
        parts.append(
            f"Valid iteration {iteration}, proposal version {record.get('proposal_version')}:\n"
            f"Claude findings:\n{record.get('report', '')}\n"
            f"Codex finding dispositions:\n{disposition}"
        )
    return "\n\n".join(parts)


def valid_plan_reviews(state: dict) -> list[dict]:
    return [
        record for record in state.get("plan_reviews", [])
        if record.get("valid") is True and isinstance(record.get("context_manifest"), dict)
    ]


def build_plan_review_prompt(
    state: dict,
    target: Path,
    iteration: int,
    *,
    repository_context: str,
    relevant_skills: str,
) -> tuple[str, dict[str, str], dict]:
    limit = state["authorization"]["claude_aily"]["max_plan_reviews"]
    version = int(state.get("proposal_version", 1))
    proposal_for_review = state["proposal"]
    disposition_heading = re.search(r"(?im)^\s*##\s+Claude finding dispositions\s*$", proposal_for_review)
    if disposition_heading:
        proposal_for_review = proposal_for_review[:disposition_heading.start()].rstrip()
    header = (
        "Act as an independent Senior Software Architect reviewing a proposal. Inspect the complete assignment, "
        "current proposal, repository context, workflow constraints, and listed skill guidance. "
        "Do not rely on prior review conclusions or Codex dispositions; they are withheld. Give evidence-based "
        "findings and a scorecard. Do not implement code."
    )
    sections = {
        "Review metadata": (
            f"Plan review iteration {iteration} of at most {limit}; proposal version {version}; "
            "all required context sections below are complete."
        ),
        "Assignment": state["task"],
        "Current proposal": proposal_for_review,
        "Repository context": repository_context,
        "Workflow constraints": (
            f"Target: {target}. The proposal is awaiting human approval; implementation is not authorized "
            "until the user approves it. The workflow does not authorize commits, pushes, merges, deployments, "
            "publication, shared-infrastructure changes, or access to unrelated repositories. Preserve the "
            "existing assignment and workflow budget."
        ),
        "Engineering Judgment & Proportional Design": PLANNING_PRINCIPLES,
        "Relevant skills": relevant_skills,
        "Review duties": PLAN_REVIEW_GUIDANCE,
        "Independence boundary": (
            "Prior Claude reports and Codex finding dispositions are deliberately withheld from this review. "
            "Base the assessment on the assignment and current proposal supplied here. The proposal's revision "
            "number is context only; do not infer that earlier conclusions were correct."
        ),
    }
    metadata = {"iteration": iteration, "proposal_version": version, "header_id": "plan-review-v2"}
    return header, sections, metadata


def refined_proposal(value: str) -> tuple[str, bool, str]:
    match = re.search(r"(?im)^\s*Second plan review needed:\s*(yes|no)\s*$", value)
    if not match:
        raise WorkflowError("Codex refinement must state whether a second plan review is needed.")
    review_again = match.group(1).lower() == "yes"
    proposal = value[:match.start()].strip()
    if not proposal:
        raise WorkflowError("Codex returned an empty refined proposal.")
    disposition_heading = re.search(r"(?im)^##\s+Claude finding dispositions\s*$", proposal)
    if not disposition_heading:
        raise WorkflowError("Codex refinement is missing the required Claude finding dispositions section.")
    dispositions = proposal[disposition_heading.end():].strip()
    if not dispositions:
        raise WorkflowError("Codex refinement has an empty Claude finding dispositions section.")
    no_findings = re.search(r"(?im)^-\s*No actionable findings\.?\s*$", dispositions)
    decisions = re.findall(
        r"(?im)^-\s*Finding:.*\|\s*Decision:\s*(?:incorporated|rejected|deferred)\s*\|\s*Reason:\s*.+$",
        dispositions,
    )
    if not no_findings and not decisions:
        raise WorkflowError(
            "Codex dispositions must list each finding with a decision and reason, or state that there were none."
        )
    return clean_markdown(proposal), review_again, dispositions

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

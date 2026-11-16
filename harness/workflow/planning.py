"""Deterministic planning request and prompt construction."""

from __future__ import annotations


def build_planning_prompt(
    *,
    architectural_decision_output: str,
    commitment_ledger_guidance: str,
    skill_guidance: str,
    planning_principles: str,
    target: str,
    assignment: str,
) -> str:
    """Build the read-only proposal-generation prompt from explicit context."""
    prompt = (
        "Understand the assignment and this repository. Use the applicable instruction and skill "
        "content supplied below; inspect relevant target files. Do not merely list skills: apply "
        "relevant guidance and record each selected skill and its use. If none apply, say so. "
        "Make reasonable technical decisions, identify requirements, edge cases, and verification. "
        "Apply Engineering Judgment & Proportional Design. Separate must implement, should implement, "
        "future production considerations, and out of scope. Do not invent product behavior. "
        "Return a concise, readable Markdown implementation proposal with scope, approach, risks, "
        "a requirements-to-design mapping, material architectural decisions and their trade-offs, "
        "relevant skills and how they were applied, and concrete validation commands. Include the "
        "approved-design commitment classifications and the required machine-readable ledger below. "
        "Do not modify "
        "files or run tests.\n\n"
        f"{commitment_ledger_guidance}\n\n"
        f"{skill_guidance}\n\n"
        f"{planning_principles}\n\nTarget: {target}\n\nAssignment:\n{assignment}"
    )
    return f"{architectural_decision_output}\n\n{prompt}"


def build_plan_refinement_prompt(
    *,
    architectural_decision_output: str,
    planning_principles: str,
    skill_guidance: str,
    commitment_ledger_guidance: str,
    assignment: str,
    proposal_version: int,
    proposal: str,
    iteration: int,
    review_proposal_version: object,
    report: str,
) -> str:
    """Build the read-only proposal refinement prompt for one saved review."""
    return (
        "Evaluate every distinct finding in this valid Claude plan-review report. For each, explicitly "
        "mark incorporated, rejected, or deferred and give a reason. Incorporate valid in-scope "
        "recommendations; do not turn optional production advice into assignment requirements. "
        "Return the complete refined proposal as readable Markdown, retaining requirements mapping, "
        "material architectural decisions and trade-offs, scope distinctions, and the relevant skills "
        "and their application. Preserve or update the machine-readable commitment ledger to reflect "
        "the refined proposal without converting optional or future advice into obligations. Put a section titled "
        "`## Claude finding dispositions` at the end of the proposal. Use one bullet for every finding "
        "in this format: `- Finding: ... | Decision: incorporated/rejected/deferred | Reason: ...`. "
        "If there are none, write `- No actionable findings.` Apply Engineering Judgment & "
        "Proportional Design. Add one final "
        "line exactly in this form: `Second plan review needed: yes` or `...: no`. Use yes only when "
        "a material plan change needs a second independent plan review.\n\n"
        f"{architectural_decision_output}\n\n{planning_principles}\n\n{skill_guidance}\n\n"
        f"{commitment_ledger_guidance}\n\n"
        f"Assignment:\n{assignment}\n\nCurrent proposal (version "
        f"{proposal_version}):\n{proposal}\n\n"
        f"Claude review (iteration {iteration}, proposal version {review_proposal_version}):\n{report}"
    )


def build_human_revision_prompt(
    *,
    architectural_decision_output: str,
    planning_principles: str,
    commitment_ledger_guidance: str,
    skill_guidance: str,
    assignment: str,
    proposal_version: int,
    proposal: str,
    review_history: str,
    human_feedback: str,
) -> str:
    """Build the read-only proposal revision prompt from explicit user feedback."""
    return (
        "Revise the saved proposal to address the user's explicit feedback. Keep the original "
        "assignment and unrelated decisions intact. Apply Engineering Judgment & Proportional Design. "
        "Apply relevant skill instructions below, and record the skill name and how it informed the "
        "proposal; if there are no applicable skills, state that. Distinguish must implement, should "
        "implement, future production considerations, and out of scope. Preserve and update the "
        "machine-readable commitment ledger using the user's explicit feedback. Return the complete proposal "
        "in readable Markdown; do not implement code.\n\n"
        f"{architectural_decision_output}\n\n{planning_principles}\n\n{commitment_ledger_guidance}\n\n{skill_guidance}\n\n"
        f"Assignment:\n{assignment}\n\nCurrent proposal (version "
        f"{proposal_version}):\n{proposal}\n\n"
        f"Prior review attempts:\n{review_history}\n\n"
        f"User feedback:\n{human_feedback}"
    )

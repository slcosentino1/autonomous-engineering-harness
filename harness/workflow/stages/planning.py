from __future__ import annotations

import copy
import hashlib
import json

from workflow import planning_review as _planning_review
from workflow.errors import WorkflowError
from workflow.review import _safe_review_path
from workflow.engine import StageInput
from workflow.stage_services import ProductionStageServices


def _execute_noop(stage_input: StageInput, services, stage: str) -> None:
    pass

def _execute_planning(stage_input: StageInput, services, stage: str) -> None:
    guidance = services.guidance()
    skill_guidance = services.guidance_for_codex(
        guidance, stage=stage, context=stage_input.context.task,
    )
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
        f"{services.commitment_ledger_guidance}\n\n"
        f"{skill_guidance}\n\n"
        f"{services.planning_principles}\n\nTarget: {services.target}\n\nAssignment:\n{services.state['task']}"
    )
    prompt = f"{services.architectural_decision_output}\n\n{prompt}"
    proposal = services.clean_markdown(services.run_codex(prompt, sandbox="read-only"))
    services.state["proposal"] = proposal
    services.state["proposal_version"] = 1
    services.write_proposal(proposal)
    services.reassess_after_planning(proposal)
    services.state["stage"] = "plan_review_1"
    services.save()

def _execute_plan_review(stage_input: StageInput, services, stage: str) -> None:
    iteration = int(stage[-1])
    header, sections, metadata = services.plan_review_prompt(iteration)
    recoverable_context = next((
        context for context in reversed(services.state.get("claude_contexts", []))
        if context.get("stage") == stage
        and context.get("proposal_version") == metadata["proposal_version"]
        and context.get("call_state") == "completed"
        and isinstance(context.get("report"), str)
        and context.get("report")
        and not any(
            review.get("context_manifest", {}).get("prompt_sha256")
            == context.get("prompt_sha256")
            for review in services.state.get("plan_reviews", [])
        )
    ), None)
    if recoverable_context is not None:
        report, manifest = recoverable_context["report"], recoverable_context
    else:
        report, manifest = services.call_claude(header, stage, sections, metadata=metadata)
    report, _scorecard = _planning_review.normalize_scorecard(report)
    services.state["plan_reviews"].append({
        "attempt": len(services.state.get("plan_reviews", [])) + 1,
        "iteration": iteration,
        "proposal_version": metadata["proposal_version"],
        "report": report,
        "time": services.now(),
        "valid": True,
        "context_manifest": manifest,
    })
    services.state["stage"] = f"plan_refine_{iteration}"
    services.save()

def _execute_plan_refinement(stage_input: StageInput, services, stage: str) -> None:
    iteration = int(stage[-1])
    review = next((record for record in reversed(services.state.get("plan_reviews", []))
                   if record.get("valid") is True and record.get("iteration") == iteration), None)
    if review is None:
        raise WorkflowError(f"No valid Claude plan review exists for iteration {iteration}.")
    report = review["report"]
    guidance = services.guidance()
    skill_guidance = services.guidance_for_codex(
        guidance, stage=stage,
        context="\n".join((stage_input.context.task, services.state["proposal"], report)),
    )
    prompt = (
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
        f"{services.architectural_decision_output}\n\n{services.planning_principles}\n\n{skill_guidance}\n\n"
        f"{services.commitment_ledger_guidance}\n\n"
        f"Assignment:\n{services.state['task']}\n\nCurrent proposal (version "
        f"{services.state.get('proposal_version', 1)}):\n{services.state['proposal']}\n\n"
        f"Claude review (iteration {iteration}, proposal version {review.get('proposal_version')}):\n{report}"
    )
    refined = services.run_codex(prompt, sandbox="read-only")
    next_proposal_version = int(services.state.get("proposal_version", 1)) + 1
    try:
        proposal, review_again, dispositions = services.refined_proposal(refined)
        validated_ledger = services.validate_proposal_ledger(proposal)
    except WorkflowError as exc:
        services.record_unvalidated_planning_output(
            refined, exc, stage=stage, proposal_version=next_proposal_version,
        )
        raise
    services.state["proposal"] = proposal
    services.state["proposal_version"] = next_proposal_version
    services.reassess_after_planning(proposal)
    services.state.setdefault("plan_review_decisions", []).append({
        "iteration": iteration,
        "proposal_version": services.state["proposal_version"],
        "dispositions": dispositions,
        "second_review_needed": review_again,
    })
    services.write_proposal(proposal, validated_ledger=validated_ledger)
    if iteration == 1 and review_again:
        services.state["stage"] = "plan_review_2"
    elif services.approval_required():
        services.state["status"] = "awaiting_approval"
        services.state["stage"] = "implementation"
    else:
        services.state["status"] = "running"
        services.state["stage"] = "implementation"
    services.save()

def _execute_human_revision(stage_input: StageInput, services, stage: str) -> None:
    guidance = services.guidance()
    skill_guidance = services.guidance_for_codex(
        guidance, stage=stage,
        context="\n".join((stage_input.context.task, services.state["proposal"], services.state["human_feedback"])),
    )
    prompt = (
        "Revise the saved proposal to address the user's explicit feedback. Keep the original "
        "assignment and unrelated decisions intact. Apply Engineering Judgment & Proportional Design. "
        "Apply relevant skill instructions below, and record the skill name and how it informed the "
        "proposal; if there are no applicable skills, state that. Distinguish must implement, should "
        "implement, future production considerations, and out of scope. Preserve and update the "
        "machine-readable commitment ledger using the user's explicit feedback. Return the complete proposal "
        "in readable Markdown; do not implement code.\n\n"
        f"{services.architectural_decision_output}\n\n{services.planning_principles}\n\n{services.commitment_ledger_guidance}\n\n{skill_guidance}\n\n"
        f"Assignment:\n{services.state['task']}\n\nCurrent proposal (version "
        f"{services.state.get('proposal_version', 1)}):\n{services.state['proposal']}\n\n"
        f"Prior review attempts:\n{services.review_history_for_prompt()}\n\n"
        f"User feedback:\n{services.state['human_feedback']}"
    )
    services.state["proposal"] = services.clean_markdown(services.run_codex(prompt, sandbox="read-only"))
    services.state["proposal_version"] = int(services.state.get("proposal_version", 1)) + 1
    services.reassess_after_planning(services.state["proposal"])
    services.write_proposal(services.state["proposal"])
    authorized_plan_limit = services.state["authorization"]["claude_aily"]["max_plan_reviews"]
    valid_reviews = services.valid_plan_reviews()
    if len(valid_reviews) < authorized_plan_limit:
        services.state["stage"] = f"plan_review_{len(valid_reviews) + 1}"
    elif services.approval_required():
        services.state["status"] = "awaiting_approval"
        services.state["stage"] = "implementation"
    else:
        services.state["status"] = "running"
        services.state["stage"] = "implementation"
    services.save()

from __future__ import annotations

import copy
import hashlib
import json
import re

from workflow import planning_review as _planning_review
from workflow.errors import WorkflowError
from workflow.review import _safe_review_path
from workflow.engine import StageInput, StageResult


def _current_stage(stage_input: StageInput) -> str:
    return str(stage_input.configuration.get("legacy_stage", stage_input.definition.stage_id))


def _execute_noop(stage_input: StageInput, services) -> StageResult:
    stage_id = stage_input.definition.stage_id
    state = services.state
    if stage_id == "revision_request":
        output = {"requested": bool(state.get("human_feedback")),
                  "proposal_sha256": state.get("proposal_artifact_sha256")}
    elif stage_id == "plan_review_2_request":
        prior = stage_input.predecessor_outputs.get("plan_refine_1", {})
        revision = state.get("workflow_execution", {}).get("stages", {}).get("human_revision", {})
        revision_applied = (
            isinstance(revision, dict)
            and revision.get("status") == "completed"
            and bool(revision.get("attempts"))
        )
        limit = state.get("authorization", {}).get("claude_aily", {}).get("max_plan_reviews", 2)
        output = {
            "requested": len(services.valid_plan_reviews()) < limit
            and (prior.get("second_review_needed") is True or revision_applied),
            "proposal_sha256": state.get("proposal_artifact_sha256"),
        }
    elif stage_id == "revision_plan_review_request":
        limit = state.get("authorization", {}).get("claude_aily", {}).get("max_plan_reviews", 2)
        output = {"requested": len(services.valid_plan_reviews()) < limit,
                  "proposal_sha256": state.get("proposal_artifact_sha256")}
    else:
        raise WorkflowError(f"Unsupported planning control stage: {stage_id}.")
    return StageResult(output=output)


def _execute_planning(stage_input: StageInput, services) -> StageResult:
    stage = _current_stage(stage_input)
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
    services.save()
    return StageResult(output={"proposal_version": 1,
                               "proposal_sha256": services.state.get("proposal_artifact_sha256")})

def _execute_plan_review(stage_input: StageInput, services) -> StageResult:
    stage = _current_stage(stage_input)
    iteration = int(stage[-1])
    header, sections, metadata = _plan_review_prompt(services, iteration)
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
    services.save()
    return StageResult(output={"valid": True,
                               "proposal_sha256": services.state.get("proposal_artifact_sha256")})

def _execute_plan_refinement(stage_input: StageInput, services) -> StageResult:
    stage = _current_stage(stage_input)
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
        proposal, review_again, dispositions = _refined_proposal(refined, services.clean_markdown)
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
    services.save()
    return StageResult(output={
        "second_review_needed": review_again if iteration == 1 else False,
        "revision_requested": False,
        "proposal_sha256": services.state.get("proposal_artifact_sha256"),
    })

def _execute_human_revision(stage_input: StageInput, services) -> StageResult:
    stage = _current_stage(stage_input)
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
        f"Prior review attempts:\n{_review_history_for_prompt(services.state)}\n\n"
        f"User feedback:\n{services.state['human_feedback']}"
    )
    services.state["proposal"] = services.clean_markdown(services.run_codex(prompt, sandbox="read-only"))
    services.state["proposal_version"] = int(services.state.get("proposal_version", 1)) + 1
    services.reassess_after_planning(services.state["proposal"])
    services.write_proposal(services.state["proposal"])
    authorized_plan_limit = services.state["authorization"]["claude_aily"]["max_plan_reviews"]
    valid_reviews = services.valid_plan_reviews()
    services.save()
    return StageResult(output={
        "revision_applied": True,
        "proposal_sha256": hashlib.sha256(
            str(services.state.get("proposal") or "").encode("utf-8"),
        ).hexdigest(),
    })


def _plan_review_prompt(services, iteration: int) -> tuple[str, dict[str, str], dict]:
    state = services.state
    limit = state["authorization"]["claude_aily"]["max_plan_reviews"]
    version = int(state.get("proposal_version", 1))
    guidance = services.guidance()
    proposal_for_review = state["proposal"]
    disposition_heading = re.search(
        r"(?im)^\s*##\s+Claude finding dispositions\s*$", proposal_for_review,
    )
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
        "Repository context": services.repository_context_for_claude(guidance),
        "Workflow constraints": (
            f"Target: {services.target}. The proposal is awaiting human approval; implementation is not authorized "
            "until the user approves it. The workflow does not authorize commits, pushes, merges, deployments, "
            "publication, shared-infrastructure changes, or access to unrelated repositories. Preserve the "
            "existing assignment and workflow budget."
        ),
        "Engineering Judgment & Proportional Design": services.planning_principles,
        "Relevant skills": services.guidance_for_claude(
            guidance,
            stage="plan_review",
            context="\n".join((state.get("task", ""), state.get("proposal", ""))),
            include_skill_content=services.claude_skill_content_authorized(),
        ),
        "Review duties": services.plan_review_guidance,
        "Independence boundary": (
            "Prior Claude reports and Codex finding dispositions are deliberately withheld from this review. "
            "Base the assessment on the assignment and current proposal supplied here. The proposal's revision "
            "number is context only; do not infer that earlier conclusions were correct."
        ),
    }
    metadata = {"iteration": iteration, "proposal_version": version, "header_id": "plan-review-v2"}
    return header, sections, metadata


def _review_history_for_prompt(state: dict) -> str:
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


def _refined_proposal(value: str, clean_markdown) -> tuple[str, bool, str]:
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
    finding_decisions = re.findall(
        r"(?im)^-\s*Finding:.*\|\s*Decision:\s*(?:incorporated|rejected|deferred)\s*\|\s*Reason:\s*.+$",
        dispositions,
    )
    if not no_findings and not finding_decisions:
        raise WorkflowError(
            "Codex dispositions must list each finding with a decision and reason, or state that there were none."
        )
    return clean_markdown(proposal), review_again, dispositions

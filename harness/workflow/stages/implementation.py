from __future__ import annotations

import copy
import hashlib
import json

from workflow import planning_review as _planning_review
from workflow.errors import WorkflowError
from workflow.review import _safe_review_path
from workflow.engine import StageInput, StageResult


def _execute_implementation(stage_input: StageInput, services) -> StageResult:
    stage = str(stage_input.configuration.get("legacy_stage", stage_input.definition.stage_id))
    if services.approval_required() and not services.state.get("implementation_approved_at"):
        raise WorkflowError("Explicit human approval is required before implementation.")
    guidance = services.guidance()
    skill_guidance = services.guidance_for_codex(
        guidance, stage=stage,
        context="\n".join((stage_input.context.task, services.state["proposal"])),
    )
    validation_baseline = services.commitment_validation_baseline()
    prompt = (
        "Implement the approved proposal in this repository. Follow repository instructions. "
        "Apply the relevant skill instructions supplied below; do not merely list skills. "
        "Use the persistent workflow authorization for ordinary target-workspace operations without "
        "individual approvals. Install dependencies only from project manifests using the project's "
        "package manager; do not use arbitrary download URLs or custom registries. Run relevant checks. "
        "Do not expose secrets or read unrelated repositories. Commit, push, merge, deploy, publish, "
        "shared-infrastructure changes, and access outside the target require separate explicit "
        "user authorization and are not included in this workflow authorization. "
        "Do not delete unrelated existing files. Follow Engineering Judgment & Proportional Design "
        "and do not invent product behavior. Return a concise summary with files changed, "
        "commands actually run, results, and known limitations. Do not use this summary to declare "
        "commitments fulfilled. For every required validation scenario, execute an actual pytest or "
        "unittest test that asserts the scenario acceptance condition; manual inspection and bare shell "
        "checks are not runner receipts. If no suitable existing test applies and adding an application "
        "test file would exceed scope, place a temporary unittest under the workflow verification "
        "directory inside `.engineering/workflows/` and run it from the target repository with "
        "`python3 -m unittest discover -v` so individual test IDs and outcomes are captured. "
        "For repository-scope scenarios, compare "
        "`git status --porcelain=v1 -z --untracked-files=all` with the supplied initial baseline; "
        "preserve pre-existing paths, exclude only the supplied Harness workflow-artifact prefix, "
        "verify each pre-existing path's supplied kind, mode, and SHA-256 state, and do not ignore "
        "other `.engineering/` paths or sensitive-path policy. If `preexisting_snapshot_complete` is "
        "false, do not claim the scope scenario passed. Include test IDs "
        "exactly as the runner reports them. Do not invent execution event IDs: set "
        "`execution_event_ids` to an empty array and the Harness will bind only observed receipts. "
        "Finish with one `harness-evidence` JSON block linking each approved commitment to source "
        "references and specific scenario/test IDs. Run all required scenarios after the final code edits, "
        "prefer one combined runner invocation, and make it the last tool action; do not edit code afterward. "
        "Do not include raw command output or secrets. Use exactly one fenced block labeled "
        "`harness-evidence` with this JSON shape, replacing example values:\n"
        "```harness-evidence\n"
        "{\"schema_version\":1,\"commitments\":["
        "{\"commitment_id\":\"C-001\",\"source_refs\":[{\"path\":\"src/file.py\","
        "\"symbol\":\"function_name\"}],\"validation_refs\":[{\"scenario_id\":\"scenario-1\","
        "\"test_ids\":[\"tests/test_file.py::test_case\"],\"execution_event_ids\":[]}],"
        "\"disposition\":null,\"reason\":null}]}\n```. Include only observed references.\n\n"
        f"Harness validation baseline (do not modify):\n{validation_baseline}\n\n"
        f"{services.commitment_ledger_guidance}\n\n{services.planning_principles}\n\n{skill_guidance}\n\n"
        f"Assignment:\n{services.state['task']}\n\nApproved proposal:\n{services.state['proposal']}"
    )
    approval_conditions = services.state.get("approval_conditions", "")
    if approval_conditions:
        prompt += (
            "\n\nHuman approval conditions (additional implementation constraints; "
            "preserve the approved proposal and commitment ledger):\n"
            f"{approval_conditions}"
        )
    services.state["implementation_summary"] = services.run_codex(prompt, sandbox="workspace-write")
    services.capture_commitment_evidence(services.state["implementation_summary"])
    services.save()
    return StageResult(output={"summary_sha256": hashlib.sha256(
        str(services.state.get("implementation_summary") or "").encode("utf-8"),
    ).hexdigest()})


def _execute_task_implementation(stage_input: StageInput, services) -> StageResult:
    """Implement a directly assigned low-risk task without fabricating proposal commitments."""
    if services.approval_required() and not services.state.get("implementation_approved_at"):
        raise WorkflowError("Explicit human approval is required before implementation.")
    guidance = services.guidance()
    skill_guidance = services.guidance_for_codex(
        guidance, stage="implementation", context=stage_input.context.task,
    )
    prompt = (
        "Implement the assigned task in this repository. Follow repository instructions and the supplied "
        "engineering guidance. Keep the change within the assignment, preserve unrelated work, and add or "
        "update focused tests where appropriate. Run relevant checks. Do not invent requirements. Do not "
        "commit, push, merge, deploy, publish, change shared infrastructure, expose secrets, or read outside "
        "the target repository. Return a concise summary of files changed, commands actually run, results, "
        "and remaining risks.\n\n"
        f"{skill_guidance}\n\nAssignment:\n{stage_input.context.task}"
    )
    summary = services.run_codex(prompt, sandbox="workspace-write")
    services.state["implementation_summary"] = summary
    services.save()
    return StageResult(output={"summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest()})


def _execute_tests(stage_input: StageInput, services) -> StageResult:
    """Run repository tests through Codex and require a recorded successful test command receipt."""
    guidance = services.guidance()
    skill_guidance = services.guidance_for_codex(
        guidance, stage="verification", context=stage_input.context.task,
    )
    prompt = (
        "Run the repository's relevant automated tests for the current workflow change. Inspect project "
        "instructions and manifests to choose the established test command. Execute the test runner; do not "
        "edit source files or tests in this stage. Report the exact command, outcome, and failing test IDs. "
        "If no automated tests exist or the command cannot be run, state that clearly. Do not claim success "
        "without an observed successful test command.\n\n"
        f"{skill_guidance}\n\nAssignment:\n{stage_input.context.task}"
        f"\n\nStage attempt: {stage_input.attempt}"
    )
    summary = services.run_codex(prompt, sandbox="workspace-write")
    operations = services.state.get("operation_history", [])
    operation = next((
        item for item in reversed(operations)
        if isinstance(item, dict) and item.get("operation_type") == "codex"
        and item.get("stage") == "tests" and item.get("status") == "completed"
    ), None) if isinstance(operations, list) else None
    receipts = operation.get("test_execution_events", []) if isinstance(operation, dict) else []
    successful = [
        item for item in receipts if isinstance(item, dict)
        and item.get("runner") != "unknown" and item.get("exit_code") == 0
    ] if isinstance(receipts, list) else []
    if not successful:
        raise WorkflowError("No successful automated test execution receipt was observed.")
    services.state["test_summary"] = summary
    services.state["test_evidence"] = [{
        "event_id": item.get("event_id"), "runner": item.get("runner"),
        "exit_code": item.get("exit_code"), "tests": item.get("tests", []),
    } for item in successful]
    services.save()
    return StageResult(output={
        "passed": True,
        "events": [item.get("event_id") for item in successful],
        "test_count": sum(len(item.get("tests", [])) for item in successful),
    })


def _successful_test_receipts(services, stage_id: str) -> list[dict]:
    operations = services.state.get("operation_history", [])
    operation = next((
        item for item in reversed(operations)
        if isinstance(item, dict) and item.get("operation_type") == "codex"
        and item.get("stage") == stage_id and item.get("status") == "completed"
    ), None) if isinstance(operations, list) else None
    receipts = operation.get("test_execution_events", []) if isinstance(operation, dict) else []
    return [
        item for item in receipts if isinstance(item, dict)
        and item.get("runner") != "unknown" and item.get("exit_code") == 0
    ] if isinstance(receipts, list) else []


def _execute_test_verification(stage_input: StageInput, services) -> StageResult:
    """Confirm tests and any conditional repair completed after the bugfix review."""
    execution = services.state.get("workflow_execution", {})
    stages = execution.get("stages", {}) if isinstance(execution, dict) else {}
    review = stages.get("review", {}) if isinstance(stages, dict) else {}
    correction = stages.get("task_correction", {}) if isinstance(stages, dict) else {}
    review_output = review.get("output", {}) if isinstance(review, dict) else {}
    correction_status = correction.get("status") if isinstance(correction, dict) else None
    if review.get("status") != "completed":
        raise WorkflowError("Verification is blocked because the independent review did not complete.")
    if review_output.get("has_findings") is True and correction_status != "completed":
        raise WorkflowError("Verification is blocked because review findings were not corrected.")
    evidence_stage = "task_correction" if correction_status == "completed" else "tests"
    receipts = _successful_test_receipts(services, evidence_stage)
    if not receipts:
        raise WorkflowError("Verification is blocked because no successful post-change test receipt exists.")
    services.state["integrity"] = services.state.get("integrity") or {}
    services.state["integrity"]["verification_result"] = {
        "status": "verified",
        "checked_at": services.now(),
        "test_stage": evidence_stage,
        "test_event_ids": [item.get("event_id") for item in receipts],
        "finding": "The independent review completed and observed automated tests passed.",
    }
    services.save()
    return StageResult(output={
        "verified": True,
        "test_event_ids": [item.get("event_id") for item in receipts],
    })


def _execute_task_correction(stage_input: StageInput, services) -> StageResult:
    """Apply only findings from a configured profile's independent review."""
    report = services.state.get("code_review")
    if not isinstance(report, str) or not report.strip():
        raise WorkflowError("Correction is blocked because the review report is missing.")
    guidance = services.guidance()
    skill_guidance = services.guidance_for_codex(
        guidance, stage="correction", context=f"{stage_input.context.task}\n{report}",
    )
    prompt = (
        "Evaluate the independent review findings against the assigned task. Fix only valid, in-scope "
        "blocking findings. Do not broaden the change or modify unrelated files. Add or update a focused "
        "regression test when needed and report which findings were addressed or rejected with reasons. "
        "Run the relevant tests after the corrections. Do not commit, push, merge, deploy, publish, or "
        "expose secrets.\n\n"
        f"{skill_guidance}\n\nAssignment:\n{stage_input.context.task}\n\nReview findings:\n{report}"
    )
    summary = services.run_codex(prompt, sandbox="workspace-write")
    if not _successful_test_receipts(services, "task_correction"):
        raise WorkflowError("Correction is blocked because no successful post-correction test receipt was observed.")
    services.state["correction_summary"] = summary
    services.save()
    return StageResult(output={"performed": True, "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest()})


def _execute_correction(stage_input: StageInput, services) -> StageResult:
    stage = str(stage_input.configuration.get("legacy_stage", stage_input.definition.stage_id))
    guidance = services.guidance()
    campaign = services.state.get("review_campaign") or {}
    if campaign.get("status") != "complete":
        raise WorkflowError("Correction is blocked because the initial review campaign is incomplete.")
    external = services.state["authorization"]["claude_aily"]
    if external.get("budget_unknown") is True:
        raise WorkflowError("Correction is blocked because Claude spend is unknown and verification cannot be authorized safely.")
    limits = services.execution_limits()
    usage = services.resource_usage()
    remaining_claude_calls = limits["max_claude_calls"] - usage["claude_calls"]
    minimum_verification_calls = max(1, int(campaign.get("required_calls", 1)))
    if remaining_claude_calls < minimum_verification_calls:
        raise WorkflowError(
            "Correction is blocked because the saved Claude call authorization cannot cover an independent verification campaign."
        )
    attempts = services.state.setdefault("correction_attempts", [])
    attempt = attempts[-1] if attempts and attempts[-1].get("status") == "in_progress" else None
    if attempt is None:
        if len(attempts) >= limits["max_correction_attempts"]:
            raise WorkflowError("The authorized Codex correction-attempt limit is exhausted.")
        attempt = {
            "attempt": len(attempts) + 1,
            "status": "in_progress",
            "started_at": services.now(),
            "original_campaign_id": campaign.get("campaign_id"),
            "original_report_sha256": hashlib.sha256(
                str(services.state.get("code_review") or "").encode("utf-8"),
            ).hexdigest(),
        }
        attempts.append(attempt)
        usage["correction_attempts"] += 1
        services.save()
    individual_reports = "\n\n".join(
        f"### {record.get('part_id')}\n{record.get('report', '')}"
        for record in campaign.get("part_reviews", []) if record.get("report")
    ) or "No separate individual reports were recorded."
    skill_guidance = services.guidance_for_codex(
        guidance, stage=stage,
        context="\n".join((
            stage_input.context.task, services.state["proposal"], services.state["code_review"], individual_reports,
        )),
    )
    validation_baseline = services.commitment_validation_baseline()
    prompt = (
        "Evaluate the independent Claude code review. Treat its findings as the complete correction "
        "assignment. Fix valid in-scope defects and reject incorrect recommendations with a brief "
        "reason. Assess every distinct finding from individual part reports and the consolidated "
        "integration report. Optional improvements are not mandatory. Modify only files necessary to "
        "address valid findings; preserve behavior outside those findings. Do not perform unrelated "
        "refactoring, cleanup, or scope expansion. Add or update focused tests when needed. For every "
        "required validation scenario, execute an actual pytest or unittest test that asserts its "
        "acceptance condition; manual inspection and bare shell checks are not runner receipts. If no "
        "suitable existing test applies and adding an application test file would exceed scope, place a "
        "temporary unittest under this workflow's `.engineering/workflows/` verification directory and "
        "run it from the target repository with `python3 -m unittest discover -v` so individual test IDs "
        "and outcomes are captured. For repository-scope "
        "scenarios, compare `git status --porcelain=v1 -z --untracked-files=all` with the supplied initial "
        "baseline; preserve pre-existing paths, exclude only the supplied Harness workflow-artifact "
        "prefix, verify each pre-existing path's supplied kind, mode, and SHA-256 state, and do not ignore "
        "other `.engineering/` paths or sensitive-path policy. If `preexisting_snapshot_complete` is false, "
        "do not claim the scope scenario passed. Include test IDs "
        "exactly as the runner reports them. Set `execution_event_ids` to an empty array; the Harness "
        "will bind only observed receipts. Do not repeat the review. Return a concise correction and "
        "validation summary plus one complete `harness-evidence` JSON block for every approved "
        "commitment. Run all required scenarios after the final code edits, prefer one combined runner "
        "invocation, and make it the last tool action; do not edit code afterward. Do not include raw "
        "output or secrets.\n\n"
        f"Harness validation baseline (do not modify):\n{validation_baseline}\n\n"
        f"{services.commitment_ledger_guidance}\n\n"
        f"{services.planning_principles}\n\n"
        f"{skill_guidance}\n\n"
        f"Assignment:\n{services.state['task']}\n\nApproved proposal:\n{services.state['proposal']}\n\n"
        f"Claude code review:\n{services.state['code_review']}\n\n"
        f"Individual Claude part reports:\n{individual_reports}\n\n"
        f"Prior implementation summary:\n{services.state['implementation_summary']}"
    )
    services.state["correction_summary"] = services.run_codex(prompt, sandbox="workspace-write")
    services.capture_commitment_evidence(services.state["correction_summary"])
    attempt["status"] = "completed"
    attempt["finished_at"] = services.now()
    attempt["correction_summary_sha256"] = hashlib.sha256(
        services.state["correction_summary"].encode("utf-8"),
    ).hexdigest()
    campaign["purpose"] = "initial"
    campaign_history = services.state.setdefault("review_campaign_history", [])
    if not any(item.get("campaign_id") == campaign.get("campaign_id") for item in campaign_history):
        campaign_history.append(copy.deepcopy(campaign))
    services.state["review_campaign"] = None
    services.state["review_campaign_id"] = None
    services.state["verification_report"] = None
    services.save()
    return StageResult(output={"performed": True})

from __future__ import annotations

import copy
import hashlib
import json

from workflow import planning_review as _planning_review
from workflow.errors import WorkflowError
from workflow.review import _safe_review_path
from workflow.engine import StageInput
from workflow.stage_services import ProductionStageServices


def _execute_implementation(stage_input: StageInput, services, stage: str) -> None:
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
    services.state["stage"] = "code_review"
    services.save()

def _execute_correction(stage_input: StageInput, services, stage: str) -> None:
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
    services.state["stage"] = "verification"
    services.save()

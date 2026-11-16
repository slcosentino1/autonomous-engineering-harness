"""Deterministic Codex execution request construction."""

from __future__ import annotations


def build_implementation_prompt(
    *,
    validation_baseline: str,
    commitment_ledger_guidance: str,
    planning_principles: str,
    skill_guidance: str,
    assignment: str,
    approved_proposal: str,
) -> str:
    """Build the workspace-write request for implementing an approved proposal."""
    return (
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
        f"{commitment_ledger_guidance}\n\n{planning_principles}\n\n{skill_guidance}\n\n"
        f"Assignment:\n{assignment}\n\nApproved proposal:\n{approved_proposal}"
    )


def build_correction_prompt(
    *,
    validation_baseline: str,
    commitment_ledger_guidance: str,
    planning_principles: str,
    skill_guidance: str,
    assignment: str,
    approved_proposal: str,
    code_review: str,
    individual_reports: str,
    implementation_summary: str,
) -> str:
    """Build a correction request from the saved independent review context."""
    return (
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
        f"{commitment_ledger_guidance}\n\n"
        f"{planning_principles}\n\n"
        f"{skill_guidance}\n\n"
        f"Assignment:\n{assignment}\n\nApproved proposal:\n{approved_proposal}\n\n"
        f"Claude code review:\n{code_review}\n\n"
        f"Individual Claude part reports:\n{individual_reports}\n\n"
        f"Prior implementation summary:\n{implementation_summary}"
    )

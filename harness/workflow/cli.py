"""Command-line interface for the autonomous engineering workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

import claude_reviewer
from observability.tracer import tracing_enabled_from_environment
from workflow.review import _validated_campaign_reports, render_review_feedback


DEFAULT_CLAUDE_BUDGET_USD = 2.00
DEFAULT_MAX_CODE_REVIEW_CALLS = 16
CODEX_EXECUTABLE = "codex"


def task_text(args: argparse.Namespace, *, workflow_error: type[Exception]) -> str:
    if bool(args.task) == bool(args.task_file):
        raise workflow_error("Supply exactly one of --task or --task-file.")
    if args.task is not None:
        return args.task
    try:
        return Path(args.task_file).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise workflow_error(f"Could not read task file: {args.task_file}") from exc


def code_review_calls_arg(value: str, *, maximum: int) -> int:
    try:
        calls = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= calls <= maximum:
        raise argparse.ArgumentTypeError(f"must be between 1 and {maximum}")
    return calls


def print_summary(state: dict, *, workflow_root: str) -> None:
    external_authorization = state.get("authorization", {}).get("claude_aily", {})
    reviews = state.get("plan_reviews", [])
    valid_reviews = [record for record in reviews if record.get("valid") is True]
    print(json.dumps({
        "workflow_id": state.get("workflow_id"),
        "target": state.get("target"),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "proposal": f"{workflow_root}/{state.get('workflow_id')}/proposal.md",
        "delivery": f"{workflow_root}/{state.get('workflow_id')}/delivery.md" if state.get("status") == "complete" else None,
        "authorization_id": state.get("authorization", {}).get("authorization_id"),
        "codex_model": state.get("codex_model"),
        "proposal_version": state.get("proposal_version"),
        "valid_plan_reviews": len(valid_reviews),
        "invalidated_plan_reviews": len(reviews) - len(valid_reviews),
        "claude_contexts": [
            {
                "stage": context.get("stage"),
                "iteration": context.get("iteration"),
                "proposal_version": context.get("proposal_version"),
                "campaign_id": context.get("campaign_id"),
                "review_kind": context.get("review_kind"),
                "part_id": context.get("part_id"),
                "characters": context.get("characters"),
                "utf8_bytes": context.get("utf8_bytes"),
                "payload_limit_characters": context.get("payload_limit_characters"),
                "cost_usd": context.get("cost_usd"),
                "prompt_sha256": context.get("prompt_sha256"),
                "sections": [section.get("name") for section in context.get("sections", [])],
                "complete": context.get("complete"),
                "call_state": context.get("call_state"),
            }
            for context in state.get("claude_contexts", [])
        ],
        "spent_usd": external_authorization.get("spent_usd"),
        "effective_budget_usd": external_authorization.get("budget_usd"),
        "budget_unknown": external_authorization.get("budget_unknown"),
        "active_call": external_authorization.get("active_call"),
        "recovery": state.get("recovery"),
        "active_operation": next((
            {
                key: operation.get(key)
                for key in (
                    "operation_id", "workflow_id", "operation_type", "stage", "attempt", "status",
                    "invocation_at", "started_at", "finished_at", "process_id", "launch_state", "exit_code",
                    "duration_ms", "model", "model_source", "reported_model", "cli_version",
                    "cli_version_source",
                    "input_sha256", "input_characters", "input_bytes", "stdout_sha256", "stdout_bytes",
                    "stdout_capture_status", "stdout_capture_representation", "stdout_truncated",
                    "stderr_sha256", "stderr_bytes", "stderr_capture_status", "stderr_capture_representation",
                    "stderr_truncated", "last_message_file_sha256",
                    "last_message_file_bytes", "last_message_file_status", "jsonl_event_coverage",
                    "jsonl_event_count", "jsonl_parse_error_count", "jsonl_unrecognized_event_count",
                    "codex_cli_turn_status", "tool_event_coverage", "tool_invocations", "reported_usage",
                    "usage_source", "reported_model_source", "result_text_characters", "result_text_bytes",
                    "outcome", "error_category",
                )
            }
            for operation in reversed(state.get("operation_history", []))
            if operation.get("operation_id") == state.get("active_operation_id")
        ), None),
        "recent_operations": [
            {
                key: operation.get(key)
                for key in (
                    "operation_id", "workflow_id", "operation_type", "stage", "attempt", "status",
                    "invocation_at", "started_at", "finished_at", "process_id", "launch_state", "exit_code",
                    "duration_ms", "model", "model_source", "reported_model", "cli_version",
                    "cli_version_source",
                    "input_sha256", "input_characters", "input_bytes", "stdout_sha256", "stdout_bytes",
                    "stdout_capture_status", "stdout_capture_representation", "stdout_truncated",
                    "stderr_sha256", "stderr_bytes", "stderr_capture_status", "stderr_capture_representation",
                    "stderr_truncated", "last_message_file_sha256",
                    "last_message_file_bytes", "last_message_file_status", "jsonl_event_coverage",
                    "jsonl_event_count", "jsonl_parse_error_count", "jsonl_unrecognized_event_count",
                    "codex_cli_turn_status", "tool_event_coverage", "tool_invocations", "reported_usage",
                    "usage_source", "reported_model_source", "result_text_characters", "result_text_bytes",
                    "result_sha256", "cost_usd", "resolution", "outcome", "error_category",
                )
            }
            for operation in state.get("operation_history", [])[-10:]
        ],
        "authorized_code_review_calls": external_authorization.get("max_code_reviews"),
        "review_campaign_status": state.get("review_campaign", {}).get("status"),
        "code_review_feedback": (
            render_review_feedback(_validated_campaign_reports(state.get("review_campaign", {})))
            if state.get("review_campaign") else None
        ),
        "code_review_report": state.get("code_review"),
        "integration_review_report": (
            state.get("review_campaign", {}).get("integration_review", {}).get("report")
            if state.get("review_campaign") else None
        ),
        "review_files_not_reviewed": state.get("review_files_not_reviewed", []),
        "review_partition_failure": state.get("review_partition_failure"),
        "error": state.get("error"),
    }, indent=2))


def main(
    argv: list[str] | None = None,
    *,
    runner_factory: Callable,
    recover_review: Callable,
    workflow_error: type[Exception],
    workflow_root: str,
    max_authorized_code_review_calls: int,
) -> int:
    parser = argparse.ArgumentParser(
        description="Run the plan, approval, implementation, review, and delivery workflow.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Create a proposal, review it, and wait for approval")
    start.add_argument("--target", required=True, help="Absolute Git repository root")
    task = start.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="Assignment text")
    task.add_argument("--task-file", help="UTF-8 file containing the assignment")
    start.add_argument("--workflow-id", help="Optional stable ID")
    start.add_argument("--budget-usd", type=float, default=DEFAULT_CLAUDE_BUDGET_USD,
                       help=f"Total cap for Claude/Aily calls (default: ${DEFAULT_CLAUDE_BUDGET_USD:.2f})")
    start.add_argument(
        "--max-code-review-calls",
        type=lambda value: code_review_calls_arg(value, maximum=max_authorized_code_review_calls),
        default=DEFAULT_MAX_CODE_REVIEW_CALLS,
        help=(f"Maximum calls in the Claude code-review campaign, including integration review "
              f"(default: {DEFAULT_MAX_CODE_REVIEW_CALLS}; maximum: {max_authorized_code_review_calls})"),
    )
    start.add_argument("--codex", default=CODEX_EXECUTABLE)
    start.add_argument("--aily", default=claude_reviewer.AILY_EXECUTABLE)
    start.add_argument("--model", help="Optional Codex model; omit to use the CLI's configured default")
    start.add_argument("--trace", action="store_true", help="Enable metadata-only Langfuse tracing for this workflow")

    for name, help_text in (
        ("approve", "Explicitly approve the saved proposal and start implementation"),
        ("resume", "Resume a failed stage or explicitly resolve an interrupted operation"),
        ("status", "Show current workflow state"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--target", required=True, help="Absolute Git repository root")
        command.add_argument("--workflow-id", required=True)
        command.add_argument("--codex", default=CODEX_EXECUTABLE)
        command.add_argument("--aily", default=claude_reviewer.AILY_EXECUTABLE)
        if name == "resume":
            command.add_argument(
                "--recovery-action", choices=("retry", "accept-implementation"),
                help="Explicitly resolve a detected crash recovery after inspecting status and target changes",
            )
            command.add_argument(
                "--verified-claude-cost-usd", type=float,
                help="Final Claude charge verified from provider usage before retrying an interrupted paid call",
            )
            command.add_argument(
                "--confirm-provider-inactive", action="store_true",
                help="Confirm that the interrupted Claude/Aily process is no longer active",
            )
            command.add_argument(
                "--confirm-process-inactive", action="store_true",
                help="Confirm that the interrupted Codex process is no longer active before retrying",
            )
            command.add_argument(
                "--confirm-target-reviewed", action="store_true",
                help="Confirm that target changes were inspected before retrying or accepting interrupted implementation",
            )

    recover = commands.add_parser(
        "recover-review", help="Locally revalidate saved code-review reports without provider calls",
    )
    recover.add_argument("--target", required=True, help="Absolute Git repository root")
    recover.add_argument("--workflow-id", required=True)
    recover.add_argument(
        "--apply", action="store_true",
        help="Write validated recovery metadata to workflow state (dry-run is the default)",
    )

    revise = commands.add_parser("revise", help="Request a proposal change before implementation")
    revise.add_argument("--target", required=True)
    revise.add_argument("--workflow-id", required=True)
    feedback = revise.add_mutually_exclusive_group(required=True)
    feedback.add_argument("--feedback")
    feedback.add_argument("--feedback-file")
    revise.add_argument("--codex", default=CODEX_EXECUTABLE)
    revise.add_argument("--aily", default=claude_reviewer.AILY_EXECUTABLE)

    reject = commands.add_parser("reject", help="Reject the proposal and stop the workflow")
    reject.add_argument("--target", required=True)
    reject.add_argument("--workflow-id", required=True)
    reject.add_argument("--reason", default="Rejected by the user.")

    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            if args.trace:
                from observability.langfuse import LangfusePreflightError, preflight_langfuse_tracing

                try:
                    preflight_langfuse_tracing()
                except LangfusePreflightError as exc:
                    raise workflow_error(str(exc)) from exc
            runner = runner_factory(
                args.target, task=task_text(args, workflow_error=workflow_error),
                budget_usd=args.budget_usd, max_code_review_calls=args.max_code_review_calls,
                workflow_id=args.workflow_id, codex=args.codex, aily=args.aily, model=args.model,
                tracing_enabled=args.trace or tracing_enabled_from_environment(),
            )
            print(f"Effective Claude/Aily budget: ${runner.budget_usd:.2f}")
            print(f"Authorized Claude code-review campaign calls: {runner.max_code_review_calls}")
            state = runner.start()
        elif args.command == "recover-review":
            result = recover_review(args.target, args.workflow_id, apply=args.apply)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 1 if result.get("errors") or result.get("apply_error") else 0
        else:
            if args.command == "reject":
                runner = runner_factory(args.target, workflow_id=args.workflow_id)
            else:
                runner = runner_factory(
                    args.target, workflow_id=args.workflow_id, codex=args.codex, aily=args.aily,
                )
            if args.command == "approve":
                state = runner.approve()
            elif args.command == "resume":
                recovery_arguments = {
                    "recovery_action": args.recovery_action,
                    "verified_claude_cost_usd": args.verified_claude_cost_usd,
                    "confirm_provider_inactive": args.confirm_provider_inactive,
                    "confirm_process_inactive": args.confirm_process_inactive,
                    "confirm_target_reviewed": args.confirm_target_reviewed,
                }
                if any(value not in (None, False) for value in recovery_arguments.values()):
                    state = runner.resume(**recovery_arguments)
                else:
                    state = runner.resume()
            elif args.command == "revise":
                feedback_text = args.feedback
                if args.feedback_file:
                    try:
                        feedback_text = Path(args.feedback_file).expanduser().read_text(encoding="utf-8")
                    except OSError as exc:
                        raise workflow_error(f"Could not read feedback file: {args.feedback_file}") from exc
                state = runner.revise(feedback_text)
            elif args.command == "reject":
                state = runner.reject(args.reason)
            else:
                state = runner.status()
        print_summary(state, workflow_root=workflow_root)
        return 0 if state.get("status") in {"awaiting_approval", "complete", "rejected"} else 1
    except (workflow_error, claude_reviewer.ReviewError) as exc:
        print(f"workflow error: {claude_reviewer.redact(str(exc))}", file=sys.stderr)
        return 2

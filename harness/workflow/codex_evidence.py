"""Pure parsing and normalization helpers for Codex invocation evidence."""

from __future__ import annotations

import hashlib
import json
import re


def _json_line_message(stdout: str) -> str:
    messages: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            messages.append(item["text"])
        elif isinstance(event, dict) and event.get("type") == "message" and isinstance(event.get("message"), str):
            messages.append(event["message"])
    return messages[-1].strip() if messages else ""


_CODEX_JSONL_EVENT_TYPES = frozenset({
    "thread.started", "turn.started", "turn.completed", "turn.failed", "item.started", "item.completed",
})
_CODEX_TOOL_ITEM_TYPES = frozenset({
    "command_execution", "mcp_tool_call", "web_search", "image_view", "image_generation",
    "collab_tool_call", "code_interpreter_call",
})
_CODEX_NON_TOOL_ITEM_TYPES = frozenset({"agent_message", "reasoning", "plan", "plan_update"})
_CODEX_USAGE_FIELDS = frozenset({
    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens",
})
MAX_CODEX_TOOL_NAMES = 50


def _output_bytes(value: bytes | str | None) -> tuple[bytes | None, str]:
    if isinstance(value, bytes):
        return value, "bytes"
    if isinstance(value, str):
        return value.encode("utf-8"), "utf8_encoded_text"
    return None, "unavailable"


def _codex_jsonl_evidence(stdout: bytes | None) -> dict:
    evidence = {
        "jsonl_event_coverage": "unavailable" if stdout is None else "empty",
        "jsonl_event_count": 0,
        "jsonl_parse_error_count": 0,
        "jsonl_unrecognized_event_count": 0,
        "codex_cli_event_counts": {},
        "codex_cli_turn_status": "not_reported",
        "tool_invocations": [],
        "tool_event_coverage": "unavailable" if stdout is None else "no_tool_events",
        "reported_usage": None,
        "usage_source": "not_reported",
        "reported_model": None,
        "reported_model_source": "not_reported",
    }
    if stdout is None or not stdout:
        return evidence

    event_counts: dict[str, int] = {}
    tools: dict[str, dict[str, int]] = {}
    parse_errors = 0
    unrecognized_events = 0
    turn_status = "not_reported"
    reported_usage: dict[str, int] | None = None
    reported_model: str | None = None
    event_count = 0
    tool_names_seen: dict[str, set[str]] = {}

    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parse_errors += 1
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            parse_errors += 1
            continue
        event_type = event["type"]
        event_count += 1
        if event_type not in _CODEX_JSONL_EVENT_TYPES:
            unrecognized_events += 1
            continue
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        if event_type == "turn.completed":
            turn_status = "completed"
            usage = event.get("usage")
            if isinstance(usage, dict):
                safe_usage = {
                    key: value for key, value in usage.items()
                    if key in _CODEX_USAGE_FIELDS and type(value) is int and value >= 0
                }
                if safe_usage:
                    reported_usage = safe_usage
        elif event_type == "turn.failed":
            turn_status = "failed"
        model = event.get("model")
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/+-]{1,128}", model):
            reported_model = model
        if event_type not in {"item.started", "item.completed"}:
            continue
        item = event.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        if not isinstance(item_type, str):
            continue
        if item_type in _CODEX_NON_TOOL_ITEM_TYPES:
            continue
        if item_type not in _CODEX_TOOL_ITEM_TYPES:
            unrecognized_events += 1
            continue
        counts = tools.setdefault(item_type, {
            "started_events": 0, "completed_events": 0, "success_reported_events": 0,
            "failure_reported_events": 0, "outcome_unreported_events": 0,
            "tool_names": [], "tool_names_truncated": False,
        })
        if item_type == "mcp_tool_call":
            tool_name = item.get("tool_name", item.get("tool"))
            if isinstance(tool_name, str) and re.fullmatch(r"[A-Za-z0-9_.:/+-]{1,128}", tool_name):
                seen = tool_names_seen.setdefault(item_type, set())
                if tool_name not in seen:
                    if len(seen) < MAX_CODEX_TOOL_NAMES:
                        seen.add(tool_name)
                        counts["tool_names"].append(tool_name)
                    else:
                        counts["tool_names_truncated"] = True
        if event_type == "item.started":
            counts["started_events"] += 1
            continue
        counts["completed_events"] += 1
        status = item.get("status")
        exit_code = item.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            outcome = "success" if exit_code == 0 else "failure"
        elif isinstance(status, str) and status.lower() in {"success", "succeeded"}:
            outcome = "success"
        elif isinstance(status, str) and status.lower() in {"failed", "failure", "error", "cancelled"}:
            outcome = "failure"
        else:
            outcome = "unreported"
        counts[f"{outcome}_reported_events" if outcome != "unreported" else "outcome_unreported_events"] += 1

    evidence.update({
        "jsonl_event_coverage": "partial" if parse_errors else ("parsed" if event_count else "empty"),
        "jsonl_event_count": event_count,
        "jsonl_parse_error_count": parse_errors,
        "jsonl_unrecognized_event_count": unrecognized_events,
        "codex_cli_event_counts": event_counts,
        "codex_cli_turn_status": turn_status,
        "tool_invocations": [
            {"item_type": name, **tools[name]} for name in sorted(tools)
        ],
        "tool_event_coverage": (
            "partial" if parse_errors else "captured" if tools else "no_tool_events"
        ),
        "reported_usage": reported_usage,
        "usage_source": "codex_jsonl_turn_completed" if reported_usage is not None else "not_reported",
        "reported_model": reported_model,
        "reported_model_source": "codex_jsonl_event" if reported_model is not None else "not_reported",
    })
    return evidence


def _codex_stream_evidence(
    stdout: bytes | None,
    stderr: bytes | None,
    *,
    stdout_status: str,
    stderr_status: str,
    stdout_truncated: bool | None,
    stderr_truncated: bool | None,
) -> dict:
    evidence = {
        "stdout_sha256": hashlib.sha256(stdout).hexdigest() if stdout is not None else None,
        "stdout_bytes": len(stdout) if stdout is not None else None,
        "stdout_capture_status": stdout_status,
        "stdout_truncated": stdout_truncated,
        "stderr_sha256": hashlib.sha256(stderr).hexdigest() if stderr is not None else None,
        "stderr_bytes": len(stderr) if stderr is not None else None,
        "stderr_capture_status": stderr_status,
        "stderr_truncated": stderr_truncated,
        **_codex_jsonl_evidence(stdout),
    }
    return evidence


def _codex_langfuse_metadata(
    operation: dict, *, status: str | None = None, finished_at: str | None = None,
) -> dict:
    """Build a bounded, content-free view of persisted Codex operation evidence."""
    metadata = {
        "operation": "codex_execution",
        "operation_id": operation.get("operation_id"),
        "stage": operation.get("stage"),
        "attempt": operation.get("attempt"),
        "invocation_at": operation.get("invocation_at"),
        "finished_at": finished_at if finished_at is not None else operation.get("finished_at"),
        "status": status if status is not None else operation.get("status"),
        "exit_code": operation.get("exit_code"),
        "error_category": operation.get("error_category"),
        "duration_ms": operation.get("duration_ms"),
        "codex_duration_ms": operation.get("duration_ms"),
        "input_sha256": operation.get("input_sha256"),
        "input_bytes": operation.get("input_bytes"),
        "input_characters": operation.get("input_characters"),
        "output_sha256": operation.get("stdout_sha256"),
        "output_bytes": operation.get("stdout_bytes"),
        "output_capture_status": operation.get("stdout_capture_status"),
        "output_hash_representation": operation.get("stdout_capture_representation"),
        "stderr_sha256": operation.get("stderr_sha256"),
        "stderr_bytes": operation.get("stderr_bytes"),
        "stderr_capture_status": operation.get("stderr_capture_status"),
        "stderr_hash_representation": operation.get("stderr_capture_representation"),
        "final_message_sha256": operation.get("last_message_file_sha256"),
        "final_message_bytes": operation.get("last_message_file_bytes"),
        "final_message_capture_status": operation.get("last_message_file_status"),
        "model": operation.get("model"),
        "model_source": operation.get("model_source"),
        "reported_model": operation.get("reported_model"),
        "reported_model_source": operation.get("reported_model_source"),
        "cli_version": operation.get("cli_version"),
        "cli_version_source": operation.get("cli_version_source"),
        "jsonl_event_coverage": operation.get("jsonl_event_coverage"),
        "usage_source": operation.get("usage_source"),
    }
    tools = operation.get("tool_invocations")
    if isinstance(tools, list):
        item_types = sorted({
            item["item_type"] for item in tools
            if isinstance(item, dict) and isinstance(item.get("item_type"), str)
        })
        names = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            for name in item.get("tool_names", []):
                if isinstance(name, str) and name not in names:
                    names.append(name)
        metadata.update({
            "tool_item_types": item_types,
            "tool_names": names,
            "tool_names_truncated": "true" if any(
                isinstance(item, dict) and item.get("tool_names_truncated") is True for item in tools
            ) else "false",
        })
        for field, output_field in (
            ("started_events", "tool_invocation_count"),
            ("completed_events", "tool_completed_count"),
            ("success_reported_events", "tool_success_count"),
            ("failure_reported_events", "tool_failure_count"),
            ("outcome_unreported_events", "tool_outcome_unreported_count"),
        ):
            metadata[output_field] = sum(
                item.get(field, 0) for item in tools
                if isinstance(item, dict) and type(item.get(field, 0)) is int and item.get(field, 0) >= 0
            )
    usage = operation.get("reported_usage")
    if isinstance(usage, dict):
        metadata.update({key: value for key, value in usage.items() if key in _CODEX_USAGE_FIELDS})
    return {key: value for key, value in metadata.items() if value is not None}

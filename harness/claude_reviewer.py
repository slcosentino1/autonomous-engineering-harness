#!/usr/bin/env python3
"""Small, read-only Aily adapter used by the workflow orchestrator."""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from workflow.process_control import process_group_options, terminate_process_group
from workflow import workspace
from workflow import review_text as _review_text


DEFAULT_AILY_EXECUTABLE = "/Users/slcosentino/.local/bin/aily"
AILY_EXECUTABLE = os.environ.get("AILY_EXECUTABLE", DEFAULT_AILY_EXECUTABLE)
DEFAULT_MAX_REVIEW_PAYLOAD_CHARS = _review_text.DEFAULT_MAX_REVIEW_PAYLOAD_CHARS
MAX_REVIEW_PAYLOAD_ENV = _review_text.MAX_REVIEW_PAYLOAD_ENV
SECRET_PATTERNS = _review_text.SECRET_PATTERNS


class ReviewError(RuntimeError):
    """A failed Aily invocation, with any cost metadata that was observable."""

    def __init__(self, message: str, *, cost_usd: float | None = None):
        super().__init__(message)
        self.cost_usd = cost_usd


@dataclass(frozen=True)
class ReviewResult:
    text: str
    cost_usd: float | None


def redact(text: str) -> str:
    return _review_text.redact(text)


def max_review_payload_chars() -> int:
    try:
        return _review_text.max_review_payload_chars()
    except _review_text.ReviewTextError as exc:
        raise ReviewError(str(exc)) from exc


def validate_review_payload(prompt: str, *, limit: int | None = None) -> int:
    try:
        return _review_text.validate_review_payload(prompt, limit=limit)
    except _review_text.ReviewTextError as exc:
        raise ReviewError(str(exc)) from exc


def validate_review_invocation(prompt: str, executable: str, *, limit: int | None = None) -> int:
    """Validate all caller-controlled text arguments before marking a review submitted."""
    try:
        return _review_text.validate_review_invocation(prompt, executable, limit=limit)
    except _review_text.ReviewTextError as exc:
        raise ReviewError(str(exc)) from exc


def _decode(stdout: str) -> tuple[str, float | None]:
    """Read Aily's JSON result envelope, while accepting plain-text responses."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip(), None
    if not isinstance(envelope, dict) or "result" not in envelope:
        return stdout.strip(), None

    raw_cost = envelope.get("total_cost_usd")
    cost = None
    if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
        if math.isfinite(float(raw_cost)) and raw_cost >= 0:
            cost = float(raw_cost)

    if envelope.get("is_error") is True or envelope.get("subtype") not in (None, "success"):
        raise ReviewError("Aily reported an unsuccessful Claude invocation.", cost_usd=cost)
    result = envelope["result"]
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, indent=2)
    return result.strip(), cost


def review(
    prompt: str,
    *,
    budget_usd: float,
    executable: str | None = None,
    timeout_seconds: int = 900,
    process_runner: Callable = subprocess.run,
    process_started: Callable[[int], None] | None = None,
) -> ReviewResult:
    """Run one Claude review with a hard per-call cap and no target access."""
    if not isinstance(budget_usd, (int, float)) or isinstance(budget_usd, bool):
        raise ReviewError("A positive USD budget is required for Claude review.")
    if not math.isfinite(float(budget_usd)) or budget_usd <= 0:
        raise ReviewError("A positive USD budget is required for Claude review.")
    if executable is None:
        executable = os.environ.get("AILY_EXECUTABLE", AILY_EXECUTABLE)
    validate_review_invocation(prompt, executable)

    command = [
        executable, "claude", "-p", prompt,
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-mode", "plan",
        "--permission-prompts", "none",
        "--restricted",
        "--tools", "Read,Glob,Grep",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--max-budget-usd", f"{budget_usd:.2f}",
    ]
    # Claude's file tools, if used, can see only this empty temporary directory.
    with tempfile.TemporaryDirectory(prefix="harness-claude-review-") as review_root:
        env = workspace.provider_env(Path(review_root) / "isolated-github-config")
        try:
            if process_started is not None and process_runner is subprocess.run:
                process = subprocess.Popen(
                    command,
                    cwd=review_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    **process_group_options(),
                )
                try:
                    process_started(process.pid)
                except BaseException:
                    terminate_process_group(process)
                    process.communicate()
                    raise
                try:
                    stdout, stderr = process.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    terminate_process_group(process)
                    stdout, stderr = process.communicate()
                    raise subprocess.TimeoutExpired(
                        command, timeout_seconds, output=stdout, stderr=stderr,
                    ) from exc
                except KeyboardInterrupt:
                    terminate_process_group(process)
                    process.communicate()
                    raise
                result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            else:
                result = process_runner(
                    command,
                    cwd=review_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout_seconds,
                    check=False,
                    env=env,
                )
        except FileNotFoundError as exc:
            raise ReviewError(f"Aily executable is unavailable: {executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ReviewError(f"Claude review timed out after {timeout_seconds} seconds.") from exc
        except OSError as exc:
            raise ReviewError(f"Aily could not start: {redact(str(exc))}") from exc
        except ValueError as exc:
            raise ReviewError(f"Aily command arguments could not be passed safely: {redact(str(exc))}") from exc

    stdout = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else str(result.stdout or "")
    stderr = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else str(result.stderr or "")
    try:
        text, cost = _decode(stdout)
    except ReviewError as exc:
        if result.returncode != 0:
            raise ReviewError(str(exc), cost_usd=exc.cost_usd) from exc
        raise
    if result.returncode != 0:
        detail = redact(stderr.strip()) or "Aily returned no diagnostic output."
        raise ReviewError(f"Aily exited with status {result.returncode}: {detail}", cost_usd=cost)
    if not text:
        raise ReviewError("Claude returned an empty review report.", cost_usd=cost)
    return ReviewResult(text=text, cost_usd=cost)

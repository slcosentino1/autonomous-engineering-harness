#!/usr/bin/env python3
"""Small, read-only Aily adapter used by the workflow orchestrator."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable


AILY_EXECUTABLE = "aily"
DEFAULT_MAX_REVIEW_PAYLOAD_CHARS = 100_000
MAX_REVIEW_PAYLOAD_ENV = "HARNESS_CLAUDE_MAX_PAYLOAD_CHARS"
SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['\"]?[^\s,'\"]{8,}"),
)


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
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def max_review_payload_chars() -> int:
    raw = os.environ.get(MAX_REVIEW_PAYLOAD_ENV)
    if raw is None:
        return DEFAULT_MAX_REVIEW_PAYLOAD_CHARS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReviewError(f"{MAX_REVIEW_PAYLOAD_ENV} must be a positive integer.") from exc
    if value < 1:
        raise ReviewError(f"{MAX_REVIEW_PAYLOAD_ENV} must be a positive integer.")
    return value


def validate_review_payload(prompt: str) -> int:
    if not prompt.strip():
        raise ReviewError("Claude review context is empty; no paid call was made.")
    limit = max_review_payload_chars()
    if len(prompt) > limit:
        raise ReviewError(
            f"Claude review context is {len(prompt)} characters, above the configured {limit}-character "
            f"limit ({MAX_REVIEW_PAYLOAD_ENV}); no paid call was made."
        )
    return limit


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
    executable: str = AILY_EXECUTABLE,
    timeout_seconds: int = 900,
    process_runner: Callable = subprocess.run,
) -> ReviewResult:
    """Run one Claude review with a hard per-call cap and no target access."""
    if not isinstance(budget_usd, (int, float)) or isinstance(budget_usd, bool):
        raise ReviewError("A positive USD budget is required for Claude review.")
    if not math.isfinite(float(budget_usd)) or budget_usd <= 0:
        raise ReviewError("A positive USD budget is required for Claude review.")
    validate_review_payload(prompt)

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
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
        try:
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

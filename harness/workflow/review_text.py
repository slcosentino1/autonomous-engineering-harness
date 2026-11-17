"""Provider-neutral text handling for review requests and payload limits."""

from __future__ import annotations

import os
import re


MAX_REVIEW_PAYLOAD_ENV = "HARNESS_CLAUDE_MAX_PAYLOAD_CHARS"
DEFAULT_MAX_REVIEW_PAYLOAD_CHARS = 200_000
SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['\"]?[^\s,'\"]{8,}"),
)


class ReviewTextError(RuntimeError):
    """Invalid review text or configured payload limit."""


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
        raise ReviewTextError(f"{MAX_REVIEW_PAYLOAD_ENV} must be a positive integer.") from exc
    if value < 1:
        raise ReviewTextError(f"{MAX_REVIEW_PAYLOAD_ENV} must be a positive integer.")
    return value


def validate_review_payload(prompt: str, *, limit: int | None = None) -> int:
    if not isinstance(prompt, str):
        raise ReviewTextError("Claude review context must be text; no paid call was made.")
    if not prompt.strip():
        raise ReviewTextError("Claude review context is empty; no paid call was made.")
    if "\x00" in prompt:
        raise ReviewTextError("Claude review context contains a NUL byte; no paid call was made.")
    try:
        prompt_bytes = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReviewTextError("Claude review context is not valid UTF-8 text; no paid call was made.") from exc
    effective_limit = max_review_payload_chars() if limit is None else limit
    if isinstance(effective_limit, bool) or not isinstance(effective_limit, int) or effective_limit < 1:
        raise ReviewTextError(f"{MAX_REVIEW_PAYLOAD_ENV} must be a positive integer.")
    if len(prompt) > effective_limit:
        raise ReviewTextError(
            f"Claude review context is {len(prompt)} characters ({len(prompt_bytes)} UTF-8 bytes), "
            f"above the configured {effective_limit}-character "
            f"limit ({MAX_REVIEW_PAYLOAD_ENV}); no paid call was made."
        )
    return effective_limit


def validate_review_invocation(prompt: str, executable: str, *, limit: int | None = None) -> int:
    """Validate caller-controlled text before marking a review submitted."""
    if not isinstance(executable, str) or not executable:
        raise ReviewTextError("Aily executable must be a non-empty text value; no paid call was made.")
    if "\x00" in executable:
        raise ReviewTextError("Aily executable contains a NUL byte; no paid call was made.")
    try:
        executable.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReviewTextError("Aily executable is not valid UTF-8 text; no paid call was made.") from exc
    return validate_review_payload(prompt, limit=limit)

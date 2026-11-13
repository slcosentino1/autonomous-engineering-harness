"""Lazy, buffered adapter for the official Langfuse Python SDK."""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Mapping
from urllib.parse import urlsplit

from observability.tracer import (
    NoOpTracer,
    OBSERVABILITY_SCOPE_VERSION,
    base_url_digest,
    effective_base_url,
    valid_observability_scope,
)


class LangfusePreflightError(RuntimeError):
    """A known Langfuse setup issue found before an explicitly traced workflow starts."""


@dataclass
class _BufferedEvent:
    name: str
    metadata: dict
    started: float
    as_type: str = "span"
    children: list["_BufferedEvent"] = field(default_factory=list)
    finished: float | None = None
    status: str | None = None
    error_category: str | None = None
    cost_usd: float | None = None
    scope_version: int = OBSERVABILITY_SCOPE_VERSION


class _BufferedSpan:
    def __init__(self, event: _BufferedEvent):
        self._event = event

    def update(self, **metadata: Any) -> None:
        if "status" in metadata:
            self._event.status = _safe_value(metadata.pop("status"))
        if "error_category" in metadata:
            self._event.error_category = _safe_category(metadata.pop("error_category"))
        cost = metadata.get("cost_usd")
        if (isinstance(cost, (int, float)) and not isinstance(cost, bool)
                and 0 <= cost < float("inf")):
            self._event.cost_usd = float(cost)
        self._event.metadata.update(_safe_metadata(metadata, scope_version=self._event.scope_version))


class _BufferedSpanContext:
    def __init__(
        self, tracer: "BufferedLangfuseTracer", name: str, metadata: dict, *, as_type: str = "span",
    ):
        self._tracer = tracer
        self._event = _BufferedEvent(
            name, _safe_metadata(metadata, scope_version=tracer.scope_version), time.monotonic(),
            as_type=as_type, scope_version=tracer.scope_version,
        )

    def __enter__(self) -> _BufferedSpan:
        stack = self._tracer._stack
        if stack:
            stack[-1].children.append(self._event)
        else:
            self._tracer._roots.append(self._event)
        stack.append(self._event)
        return _BufferedSpan(self._event)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self._event.finished = time.monotonic()
        self._event.status = "failure" if exc_type is not None else "success"
        if exc_type is not None:
            self._event.error_category = _safe_category(exc_type.__name__)
        if self._tracer._stack and self._tracer._stack[-1] is self._event:
            self._tracer._stack.pop()
        return False


class BufferedLangfuseTracer:
    """Collect metadata locally; import, emit and flush the SDK only after workflow unlock."""

    def __init__(
        self, *, session_id: str, public_key: str, secret_key: str, base_url: str,
        scope_version: int = OBSERVABILITY_SCOPE_VERSION,
    ):
        self.session_id = session_id
        self._public_key = public_key
        self._secret_key = secret_key
        self._base_url = base_url
        self.scope_version = scope_version
        self._roots: list[_BufferedEvent] = []
        self._stack: list[_BufferedEvent] = []

    def execution(self, *, name: str, session_id: str, metadata: dict) -> ContextManager[Any]:
        self.session_id = session_id
        return _BufferedSpanContext(self, name, {**metadata, "session_id": session_id})

    def span(self, *, name: str, metadata: dict) -> ContextManager[Any]:
        return _BufferedSpanContext(self, name, metadata)

    def generation(self, *, name: str, metadata: dict) -> ContextManager[Any]:
        return _BufferedSpanContext(self, name, metadata, as_type="generation")

    def _emit_event(self, client: Any, event: _BufferedEvent) -> None:
        metadata = dict(event.metadata)
        if event.finished is not None:
            # Export happens after workflow unlock; preserve the measured elapsed
            # time as metadata instead of deriving it from export-time timestamps.
            metadata["duration_ms"] = max(0, round((event.finished - event.started) * 1000))
        if event.status is not None:
            metadata["status"] = event.status
        if event.error_category is not None:
            metadata["error_category"] = event.error_category
        description = _event_descriptions(event, metadata)
        observation_kwargs = {
            "name": event.name,
            "as_type": event.as_type,
            "metadata": metadata,
        }
        if description[0] is not None:
            observation_kwargs["input"] = description[0]
        if description[1] is not None:
            observation_kwargs["output"] = description[1]
        if event.as_type == "generation":
            model = metadata.pop("model", None)
            # Generation cost has a native Langfuse field. Keep the generic
            # cost key for span metadata, but avoid reporting it twice here.
            metadata.pop("cost_usd", None)
            if model is not None:
                observation_kwargs["model"] = model
            if event.cost_usd is not None:
                observation_kwargs["cost_details"] = {"total": event.cost_usd}
        with client.start_as_current_observation(**observation_kwargs):
            for child in event.children:
                self._emit_event(client, child)

    def flush(self) -> None:
        if not self._roots:
            return
        client = None
        try:
            sdk = importlib.import_module("langfuse")
            client = sdk.Langfuse(
                public_key=self._public_key,
                secret_key=self._secret_key,
                base_url=self._base_url,
                tracing_enabled=True,
            )
            with sdk.propagate_attributes(
                session_id=self.session_id,
                trace_name="autonomous-engineering-workflow",
            ):
                for event in self._roots:
                    self._emit_event(client, event)
            client.flush()
        except Exception:
            print(
                "Tracing warning: Langfuse initialization or export failed; workflow result is unchanged.",
                file=sys.stderr,
            )
        finally:
            if client is not None:
                try:
                    client.shutdown()
                except Exception:
                    print("Tracing warning: Langfuse shutdown failed; workflow result is unchanged.", file=sys.stderr)


_STAGE_PURPOSES = {
    "planning": "Prepare the implementation proposal.",
    "plan_review_1": "Independently review the implementation proposal.",
    "plan_review_2": "Independently review the revised proposal.",
    "plan_refine_1": "Refine the proposal from review feedback.",
    "plan_refine_2": "Refine the proposal from review feedback.",
    "human_revision": "Apply the requested proposal feedback.",
    "implementation": "Execute the approved implementation plan.",
    "code_review": "Independently review the implementation.",
    "correction": "Address valid code-review findings.",
    "complete": "Finalize workflow delivery.",
}

_LANGFUSE_INIT_CHECK = (
    "import importlib, os; "
    "sdk = importlib.import_module('langfuse'); "
    "client = sdk.Langfuse(public_key=os.environ['LANGFUSE_PUBLIC_KEY'], "
    "secret_key=os.environ['LANGFUSE_SECRET_KEY'], "
    "base_url=os.environ['LANGFUSE_BASE_URL'], tracing_enabled=True); "
    "client.shutdown()"
)


def _event_descriptions(event: _BufferedEvent, metadata: dict) -> tuple[str | None, str | None]:
    input_lines = []
    if event.name == "workflow.execution":
        for label, key in (
            ("Command", "command"), ("Session", "session_id"), ("Initial stage", "initial_stage"),
        ):
            if metadata.get(key):
                input_lines.append(f"{label}: {metadata[key]}")
    else:
        for label, key in (
            ("Provider", "provider"), ("Operation", "operation"), ("Stage", "stage"),
            ("Sandbox", "sandbox"),
        ):
            if metadata.get(key) is not None:
                input_lines.append(f"{label}: {metadata[key]}")
        part_index = metadata.get("review_part_index")
        part_count = metadata.get("review_part_count")
        if part_index is not None and part_count is not None:
            input_lines.append(f"Review part: {part_index} of {part_count}")
        elif part_index is not None:
            input_lines.append(f"Review part: {part_index}")
        for label, key in (("Attempt", "attempt"), ("Retry count", "retry_count")):
            if metadata.get(key) is not None:
                input_lines.append(f"{label}: {metadata[key]}")
        for label, key in (
            ("Operation ID", "operation_id"), ("Invocation at", "invocation_at"),
            ("Input SHA-256", "input_sha256"), ("Input bytes", "input_bytes"),
        ):
            if metadata.get(key) is not None:
                input_lines.append(f"{label}: {metadata[key]}")
        if event.name == "workflow.stage" and metadata.get("stage") in _STAGE_PURPOSES:
            input_lines.append(f"Purpose: {_STAGE_PURPOSES[metadata['stage']]}")

    output_lines = []
    if metadata.get("execution_result"):
        output_lines.append(f"Execution result: {metadata['execution_result']}")
    elif metadata.get("status"):
        output_lines.append(f"Status: {metadata['status']}")
    if metadata.get("final_status"):
        output_lines.append(f"Workflow status: {metadata['final_status']}")
    for label, key in (
        ("Finished at", "finished_at"), ("Output SHA-256", "output_sha256"),
        ("Output bytes", "output_bytes"), ("Exit code", "exit_code"),
        ("Codex duration", "codex_duration_ms"),
        ("Codex CLI", "cli_version"), ("Tool names", "tool_names"),
        ("Tool invocations", "tool_invocation_count"), ("Tool successes", "tool_success_count"),
        ("Tool failures", "tool_failure_count"), ("Input tokens", "input_tokens"),
        ("Output tokens", "output_tokens"),
    ):
        value = metadata.get(key)
        if value is not None:
            rendered = ", ".join(value) if isinstance(value, list) else value
            output_lines.append(f"{label}: {rendered}")
    duration = metadata.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        output_lines.append(f"Duration: {duration:g} ms")
    if event.cost_usd is not None:
        output_lines.append(f"Cost: USD {event.cost_usd}")
    if metadata.get("review_result"):
        output_lines.append(f"Review result: {metadata['review_result']}")
    return "\n".join(input_lines) or None, "\n".join(output_lines) or None


def preflight_langfuse_tracing(
    *,
    environ: Mapping[str, str] | None = None,
    sdk_loader: Callable[[], Any] | None = None,
) -> None:
    """Check local SDK readiness for an explicitly requested --trace start.

    This does not send a test observation or check remote connectivity/authentication.
    """
    env = os.environ if environ is None else environ
    load_sdk = sdk_loader or (lambda: importlib.import_module("langfuse"))
    try:
        sdk = load_sdk()
    except ImportError:
        raise LangfusePreflightError(
            "Tracing was explicitly enabled, but the Langfuse SDK is not installed "
            "in the current Python environment.\n\n"
            "Activate the harness virtual environment and, from the harness repository, run:\n"
            "  python -m pip install -r requirements-tracing.txt\n\n"
            "Install this optional dependency once. No workflow was started."
        ) from None
    except Exception:
        raise LangfusePreflightError(
            "Tracing was explicitly enabled, but the Langfuse SDK could not be imported "
            "in the current Python environment. Check the optional tracing installation. "
            "No workflow was started."
        ) from None

    public_key = env.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = env.get("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        raise LangfusePreflightError(
            "Tracing was explicitly enabled, but Langfuse credentials are incomplete.\n\n"
            "Configure both LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.\n"
            "No workflow was started."
        )

    url = effective_base_url(env)
    try:
        parsed_url = urlsplit(url)
        valid_url = (
            parsed_url.scheme in {"http", "https"}
            and bool(parsed_url.hostname)
            and parsed_url.username is None
            and parsed_url.password is None
            and not parsed_url.query
            and not parsed_url.fragment
            and not any(char.isspace() for char in parsed_url.netloc)
        )
        parsed_url.port  # Validate an explicitly configured port.
    except ValueError:
        valid_url = False
    if not valid_url:
        raise LangfusePreflightError(
            "Tracing was explicitly enabled, but LANGFUSE_BASE_URL is invalid. "
            "Set it to an absolute HTTP or HTTPS Langfuse endpoint without embedded credentials. "
            "No workflow was started."
        )

    if env.get("LANGFUSE_TRACING_ENABLED", "true").strip().lower() == "false":
        raise LangfusePreflightError(
            "Tracing was explicitly enabled, but LANGFUSE_TRACING_ENABLED=false disables the SDK. "
            "Unset that variable or set it to true. No workflow was started."
        )
    if env.get("OTEL_SDK_DISABLED", "false").strip().lower() == "true":
        raise LangfusePreflightError(
            "Tracing was explicitly enabled, but OTEL_SDK_DISABLED=true disables the tracing SDK. "
            "Unset that variable or set it to false. No workflow was started."
        )

    client_factory = getattr(sdk, "Langfuse", None)
    propagate_attributes = getattr(sdk, "propagate_attributes", None)
    if not callable(client_factory) or not callable(propagate_attributes):
        raise LangfusePreflightError(
            "Tracing was explicitly enabled, but the installed Langfuse SDK lacks the required API. "
            "Install the pinned optional dependency from requirements-tracing.txt. No workflow was started."
        )

    child_env = os.environ.copy()
    if environ is not None:
        for key in tuple(child_env):
            if key.startswith(("LANGFUSE_", "OTEL_")) and key not in env:
                child_env.pop(key, None)
    child_env.update(env)
    child_env.update({
        "LANGFUSE_PUBLIC_KEY": public_key,
        "LANGFUSE_SECRET_KEY": secret_key,
        "LANGFUSE_BASE_URL": url,
    })
    try:
        result = subprocess.run(
            [sys.executable, "-c", _LANGFUSE_INIT_CHECK],
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise LangfusePreflightError(
            "Tracing was explicitly enabled, but the Langfuse client could not be initialized locally "
            "with the current Python interpreter. "
            "Check LANGFUSE_BASE_URL and the optional tracing installation. "
            "No remote connectivity test was performed. No workflow was started."
        ) from None
    if result.returncode != 0:
        raise LangfusePreflightError(
            "Tracing was explicitly enabled, but the Langfuse client could not be initialized locally "
            "with the current Python interpreter. Check LANGFUSE_BASE_URL and the optional tracing "
            "installation. No remote connectivity test was performed. No workflow was started."
        )


def create_langfuse_tracer(
    scope: dict,
    *,
    environ: Mapping[str, str] | None = None,
    warning_sink=print,
):
    """Build an optional local buffer only when the saved workflow scope allows it."""
    if (not isinstance(scope, dict) or scope.get("enabled") is not True
            or not valid_observability_scope(scope)):
        return NoOpTracer()
    env = os.environ if environ is None else environ
    url = effective_base_url(env)
    if scope.get("langfuse_base_url_sha256") != base_url_digest(url):
        warning_sink(
            "Tracing warning: current Langfuse endpoint does not match the workflow authorization; tracing is disabled.",
            file=sys.stderr,
        )
        return NoOpTracer()
    public_key = env.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = env.get("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        warning_sink(
            "Tracing warning: Langfuse credentials are unavailable; continuing without tracing.",
            file=sys.stderr,
        )
        return NoOpTracer()
    return BufferedLangfuseTracer(
        session_id="", public_key=public_key, secret_key=secret_key, base_url=url,
        scope_version=scope["version"],
    )


def _safe_category(value: Any) -> str:
    text = str(value)
    return text if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", text) else "Error"


def _safe_value(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 80 and re.fullmatch(r"[A-Za-z0-9_.:-]+", text) else "unknown"


def _safe_metadata(metadata: dict, *, scope_version: int = OBSERVABILITY_SCOPE_VERSION) -> dict:
    base_allowed = {
        "command", "session_id", "initial_stage", "final_stage", "final_status",
        "execution_result", "duration_ms", "stage", "provider", "operation", "sandbox", "model",
        "iteration", "review_kind", "review_part_index", "review_part_count", "review_result",
        "cost_usd", "error_category", "retry_count", "attempt", "status",
    }
    codex_allowed = {
        "operation_id", "invocation_at", "finished_at", "input_sha256", "input_bytes", "input_characters",
        "output_sha256", "output_bytes", "output_capture_status", "output_hash_representation", "codex_duration_ms",
        "stderr_sha256",
        "stderr_bytes", "stderr_capture_status", "stderr_hash_representation", "final_message_sha256",
        "final_message_bytes", "final_message_capture_status", "exit_code", "model_source", "reported_model",
        "reported_model_source", "cli_version", "cli_version_source", "usage_source", "jsonl_event_coverage",
        "tool_item_types", "tool_names", "tool_invocation_count", "tool_completed_count", "tool_success_count",
        "tool_failure_count", "tool_outcome_unreported_count", "tool_names_truncated", "input_tokens",
        "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens",
    }
    allowed = base_allowed | codex_allowed if scope_version == OBSERVABILITY_SCOPE_VERSION else base_allowed
    safe = {}
    for key, value in metadata.items():
        if key not in allowed:
            continue
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            if key == "cost_usd" and value < 0:
                continue
            safe[key] = value
        elif isinstance(value, float):
            if value >= 0 and value < float("inf"):
                safe[key] = value
        elif key in {"tool_item_types", "tool_names"} and isinstance(value, list):
            identifiers = [
                item for item in value[:8]
                if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:/+-]{1,80}", item)
            ]
            if identifiers:
                safe[key] = identifiers
        elif key == "tool_names_truncated" and isinstance(value, str) and value in {"true", "false"}:
            safe[key] = value
        elif key == "cli_version" and isinstance(value, str) and re.fullmatch(
                r"(?:codex-cli|codex)\s+v?[0-9][A-Za-z0-9.+-]{0,30}", value, re.IGNORECASE):
            safe[key] = value
        elif key == "cli_version_source" and value in {"codex --version", "unavailable"}:
            safe[key] = value
        elif isinstance(value, str) and len(value) <= 80 and re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            if key not in {"model", "reported_model"} or re.fullmatch(
                    r"(?:gpt|claude|codex|o[134])[A-Za-z0-9._:-]{0,63}", value, re.IGNORECASE):
                safe[key] = value
    return safe


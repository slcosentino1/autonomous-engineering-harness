"""Lazy, buffered adapter for the official Langfuse Python SDK."""

from __future__ import annotations

import importlib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, ContextManager, Mapping

from observability.tracer import (
    NoOpTracer,
    base_url_digest,
    effective_base_url,
)


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
        self._event.metadata.update(_safe_metadata(metadata))


class _BufferedSpanContext:
    def __init__(
        self, tracer: "BufferedLangfuseTracer", name: str, metadata: dict, *, as_type: str = "span",
    ):
        self._tracer = tracer
        self._event = _BufferedEvent(
            name, _safe_metadata(metadata), time.monotonic(), as_type=as_type,
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

    def __init__(self, *, session_id: str, public_key: str, secret_key: str, base_url: str):
        self.session_id = session_id
        self._public_key = public_key
        self._secret_key = secret_key
        self._base_url = base_url
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
            # SDK 4.18 has no public start_time argument for spans. Since export
            # happens after the workflow unlocks, monotonic duration is the reliable
            # elapsed-time value; do not manufacture native timestamps here.
            metadata["duration_ms"] = max(0, round((event.finished - event.started) * 1000))
        if event.status is not None:
            metadata["status"] = event.status
        if event.error_category is not None:
            metadata["error_category"] = event.error_category
        observation_kwargs = {
            "name": event.name,
            "as_type": event.as_type,
            "metadata": metadata,
        }
        if event.as_type == "generation":
            model = metadata.pop("model", None)
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


def create_langfuse_tracer(
    scope: dict,
    *,
    environ: Mapping[str, str] | None = None,
    warning_sink=print,
):
    """Build an optional local buffer only when the saved workflow scope allows it."""
    if not isinstance(scope, dict) or scope.get("enabled") is not True:
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
    )


def _safe_category(value: Any) -> str:
    text = str(value)
    return text if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", text) else "Error"


def _safe_value(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 80 and re.fullmatch(r"[A-Za-z0-9_.:-]+", text) else "unknown"


def _safe_metadata(metadata: dict) -> dict:
    allowed = {
        "command", "session_id", "initial_stage", "final_stage", "final_status",
        "execution_result", "duration_ms", "stage", "provider", "operation", "sandbox", "model",
        "iteration", "review_kind", "review_part_index", "review_part_count", "review_result",
        "cost_usd", "error_category", "retry_count", "attempt", "status",
    }
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
        elif isinstance(value, str) and len(value) <= 80 and re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            if key != "model" or re.fullmatch(r"(?:gpt|claude|codex|o[134])[A-Za-z0-9._:-]{0,63}", value, re.IGNORECASE):
                safe[key] = value
    return safe


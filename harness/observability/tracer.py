"""Small local tracing interface with failure-isolated context helpers."""

from __future__ import annotations

import hashlib
import os
import sys
from contextlib import nullcontext
from typing import Any, Callable, ContextManager, Protocol


DEFAULT_LANGFUSE_BASE_URL = "https://cloud.langfuse.com"
OBSERVABILITY_SCOPE_VERSION = 1
OBSERVABILITY_PERMITTED_DATA = [
    "workflow_command_and_safe_workflow_reference",
    "workflow_stage_and_status",
    "operation_and_provider_names",
    "configured_model_name_when_known",
    "duration_and_retry_count",
    "review_part_index_and_count",
    "provider_cost_already_reported_by_harness",
    "sanitized_error_category",
]


def effective_base_url(environ=None) -> str:
    env = os.environ if environ is None else environ
    return (env.get("LANGFUSE_BASE_URL", "").strip() or DEFAULT_LANGFUSE_BASE_URL).rstrip("/")


def base_url_digest(base_url: str) -> str:
    return hashlib.sha256(base_url.rstrip("/").encode("utf-8")).hexdigest()


def observability_scope(enabled: bool, environ=None) -> dict:
    return {
        "version": OBSERVABILITY_SCOPE_VERSION,
        "enabled": bool(enabled),
        "permitted_data": OBSERVABILITY_PERMITTED_DATA.copy(),
        "langfuse_base_url_sha256": base_url_digest(effective_base_url(environ)) if enabled else None,
    }


def tracing_enabled_from_environment(environ=None) -> bool:
    env = os.environ if environ is None else environ
    value = env.get("HARNESS_TRACING_ENABLED")
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    print(
        "Tracing warning: HARNESS_TRACING_ENABLED has an invalid value; tracing is disabled for this start.",
        file=sys.stderr,
    )
    return False


def safe_workflow_reference(workflow_id: str) -> str:
    return hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()[:24]


class Tracer(Protocol):
    def execution(self, *, name: str, session_id: str, metadata: dict) -> ContextManager[Any]: ...

    def span(self, *, name: str, metadata: dict) -> ContextManager[Any]: ...

    def generation(self, *, name: str, metadata: dict) -> ContextManager[Any]: ...

    def flush(self) -> None: ...


class NoOpSpan:
    def update(self, **metadata: Any) -> None:
        return None


class NoOpTracer:
    """Tracing implementation used when tracing is disabled or unavailable."""

    def execution(self, *, name: str, session_id: str, metadata: dict) -> ContextManager[Any]:
        return nullcontext(NoOpSpan())

    def span(self, *, name: str, metadata: dict) -> ContextManager[Any]:
        return nullcontext(NoOpSpan())

    def generation(self, *, name: str, metadata: dict) -> ContextManager[Any]:
        return nullcontext(NoOpSpan())

    def flush(self) -> None:
        return None


class IsolatedContext:
    """Suppress only failures from a tracing context, never the wrapped operation."""

    def __init__(self, factory: Callable[[], ContextManager[Any]]):
        self._factory = factory
        self._context: ContextManager[Any] | None = None
        self.span: Any = NoOpSpan()

    def __enter__(self) -> Any:
        try:
            self._context = self._factory()
            self.span = self._context.__enter__()
            return self.span if self.span is not None else NoOpSpan()
        except Exception:
            self._context = None
            self.span = NoOpSpan()
            return self.span

    def update(self, **metadata: Any) -> None:
        try:
            self.span.update(**metadata)
        except Exception:
            return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            self.update(
                status="failure" if exc_type is not None else "success",
                **({"error_category": exc_type.__name__} if exc_type is not None else {}),
            )
        except Exception:
            pass
        if self._context is not None:
            try:
                self._context.__exit__(exc_type, exc, traceback)
            except Exception:
                pass
        return False


def isolated_context(factory: Callable[[], ContextManager[Any]]) -> IsolatedContext:
    return IsolatedContext(factory)



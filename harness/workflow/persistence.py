"""Low-level persistence operations for workflow artifacts and state JSON."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class PersistenceError(RuntimeError):
    """An actionable workflow artifact persistence error."""


def safe_artifact(path: Path) -> None:
    if path.is_symlink():
        raise PersistenceError(f"Workflow artifact is a symlink: {path}")
    if path.exists() and not path.is_file():
        raise PersistenceError(f"Workflow artifact is not a regular file: {path}")


def atomic_text(path: Path, value: str) -> None:
    try:
        safe_artifact(path)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except PersistenceError:
        raise
    except OSError as exc:
        raise PersistenceError(str(exc)) from exc


def serialize_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def write_json(path: Path, value: Any) -> None:
    atomic_text(path, serialize_json(value))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def read_json_bytes(path: Path) -> tuple[bytes, Any]:
    original_bytes = read_bytes(path)
    return original_bytes, json.loads(original_bytes.decode("utf-8"))

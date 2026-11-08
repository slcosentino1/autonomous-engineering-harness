#!/usr/bin/env python3
"""Orchestrate explicit Codex, validation, and Claude review stages.

Codex only plans or implements the stage assigned by this controller. The
target-local workflow-state.json remains the durable workflow record.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import json
import math
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

HARNESS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HARNESS_ROOT / "harness"))
from preflight import Preflight, PreflightError, canonical  # noqa: E402
from safe_writer import create_directories, create_file, replace_file  # noqa: E402
import claude_reviewer as claude  # noqa: E402
from runner import _runtime_bundle_files, _runtime_bundle_unchanged  # noqa: E402

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RISK = {"low": 0, "medium": 1, "high": 2}
SECRET_PATH = re.compile(r"(?i)(^|/)(\.env(?:\..*)?|id_rsa|id_ed25519|.*\.(?:pem|key))$")


class OrchestratorError(ValueError):
    """A fail-closed orchestration error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"


def _blocker_fingerprint(value: Any) -> str:
    if not isinstance(value, dict):
        return _sha(_json_bytes(value))
    stable = {key: item for key, item in value.items() if key not in {"recorded_at", "updated_at"}}
    return _sha(_json_bytes(stable))


def _relative(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise OrchestratorError(f"{field} must be a normalized relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", "..", ".git"} for part in path.parts) or path.as_posix() != value or any(token in value for token in "*?[]"):
        raise OrchestratorError(f"{field} escapes the target or is not normalized")
    if SECRET_PATH.search(value):
        raise OrchestratorError(f"{field} cannot include a credential or environment file")
    return value


def _jcs(value: Any) -> bytes:
    """RFC 8785 canonical JSON for the manifest's strings, integers, arrays, and objects."""
    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if isinstance(item, int):
            return str(item)
        if isinstance(item, list):
            return "[" + ",".join(encode(child) for child in item) + "]"
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            keys = sorted(item, key=lambda key: key.encode("utf-16be", "surrogatepass"))
            return "{" + ",".join(encode(key) + ":" + encode(item[key]) for key in keys) + "}"
        raise OrchestratorError("Revision manifest contains a value outside the canonical JSON contract")
    return encode(value).encode("utf-8")


def _redact(value: str) -> str:
    patterns = (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
        re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"),
        re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['\"]?[^\s,'\"]{8,}"),
    )
    for pattern in patterns:
        value = pattern.sub("[REDACTED]", value)
    return value[-3000:]


def _skill_name(path: Path) -> str:
    fallback = path.parent.name[:128]
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index >= 32:
                    break
                match = re.fullmatch(r"\s*name\s*:\s*['\"]?([^'\"#]+?)['\"]?\s*", line.rstrip("\r\n"))
                if match:
                    return match.group(1).strip()[:128] or fallback
    except OSError:
        pass
    return fallback


def _skill_roots(target: Path, additional_roots: list[str] | None = None) -> list[Path]:
    roots = [target / ".agents" / "skills", target / ".codex" / "skills",
             HARNESS_ROOT / ".agents" / "skills", HARNESS_ROOT / ".codex" / "skills"]
    for value in additional_roots or []:
        if not isinstance(value, str):
            continue
        base = Path(value)
        if base.name == "skills":
            roots.append(base)
        else:
            roots.extend([base / ".agents" / "skills", base / ".codex" / "skills"])
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    roots.append(codex_home / "skills")
    return list(dict.fromkeys(roots))


def _available_skills(target: Path, additional_roots: list[str] | None = None) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for root in _skill_roots(target, additional_roots):
        try:
            if root.is_symlink():
                continue
            resolved_root = root.resolve(strict=True)
            if not resolved_root.is_dir():
                continue
            for directory, names, files in os.walk(resolved_root, followlinks=False):
                depth = len(Path(directory).relative_to(resolved_root).parts)
                names[:] = (sorted(name for name in names if not (Path(directory) / name).is_symlink())
                            if depth < 7 else [])
                if "SKILL.md" not in files:
                    continue
                skill_path = Path(directory) / "SKILL.md"
                if skill_path.is_symlink() or not skill_path.is_file():
                    continue
                relative = skill_path.relative_to(resolved_root).as_posix()
                if len(Path(relative).parts) > 7:
                    continue
                key = str(skill_path.resolve(strict=True))
                found[key] = {"name": _skill_name(skill_path), "skill_md_path": key}
        except OSError:
            continue
    return sorted(found.values(), key=lambda row: (row["name"].casefold(), row["skill_md_path"]))


def _skill_read_events(raw_events: str, target: Path, available: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Extract only successful, explicit file-read events; never persist command/output text."""
    evidence: list[dict[str, Any]] = []
    direct_readers = {"cat", "sed", "head", "tail", "more", "less", "awk"}
    for line in raw_events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if not isinstance(item, dict) or event.get("type") != "item.completed":
            continue
        operation = None
        paths: list[str] = []
        command_sha256 = None
        exit_code = item.get("exit_code")
        explicit_path = item.get("path")
        if item.get("type") in {"read_file", "file_read"} and isinstance(explicit_path, str):
            if item.get("status") not in {"completed", "success"}:
                continue
            output = item.get("content") or item.get("aggregated_output")
            if not isinstance(output, str) or not output.strip():
                continue
            operation = "runtime_file_read"
            paths = [explicit_path]
        elif item.get("type") == "command_execution" and isinstance(item.get("command"), str):
            command = item["command"]
            if item.get("status") not in {"completed", "success"} or exit_code != 0:
                continue
            output = item.get("aggregated_output")
            if not isinstance(output, str) or not output.strip():
                continue
            command_sha256 = _sha(command.encode("utf-8"))
            if any(token in command for token in (";", "|", ">", "<", "\n")):
                continue
            try:
                argv = shlex.split(command)
            except ValueError:
                continue
            if not argv:
                continue
            executable = Path(argv[0]).name
            if executable in direct_readers:
                operands = argv[1:]
                if executable == "sed":
                    operands = [arg for arg in operands if arg != "-n" and not re.fullmatch(r"[0-9,;$+{}p!d]+", arg)]
                elif executable in {"head", "tail"}:
                    operands = [arg for arg in operands if arg not in {"-n", "-c"} and not re.fullmatch(r"-?[0-9]+", arg)]
                paths = [arg for arg in operands if not arg.startswith("-")]
                operation = "explicit_file_read"
        for value in paths:
            try:
                path = Path(value)
                if not path.is_absolute():
                    cwd = item.get("cwd") or target
                    path = Path(cwd) / path
                canonical_path = path.resolve(strict=True)
                skill_dirs = [Path(skill["skill_md_path"]).parent for skill in available]
                if (not canonical_path.is_file() or
                        SECRET_PATH.search(canonical_path.as_posix()) or
                        re.search(r"(?i)(^|/)(secrets?|credentials?)(/|$)|(?:token|secret|credential)", canonical_path.as_posix()) or
                        not any(canonical_path == skill_dir / "SKILL.md" or skill_dir in canonical_path.parents
                                for skill_dir in skill_dirs)):
                    continue
                evidence.append({"path": str(canonical_path), "operation": operation,
                    "event_type": "item.completed", "event_id": str(item.get("id", ""))[:128],
                    "event_sha256": _sha(line.encode("utf-8")), "output_sha256": _sha(output.encode("utf-8")),
                    "command_sha256": command_sha256, "exit_code": exit_code})
            except (OSError, TypeError, ValueError):
                continue
    return evidence


def _skill_trace(stage: str, target: Path, context: dict[str, Any], raw_events: str = "") -> dict[str, Any]:
    extra_roots = context.get("authorized_instruction_roots", [])
    available = _available_skills(target, extra_roots if isinstance(extra_roots, list) else [])
    by_path = {row["skill_md_path"]: row for row in available}
    requested_sources = list(context.get("requested_sources", [])) if isinstance(context.get("requested_sources", []), list) else []
    selected = context.get("capabilities_selected", [])
    if isinstance(selected, list):
        requested_sources.extend(row.get("source_reference") for row in selected if isinstance(row, dict))
    instruction_bundle = context.get("skills_and_instructions")
    if isinstance(instruction_bundle, dict) and isinstance(instruction_bundle.get("requested_sources"), list):
        requested_sources.extend(instruction_bundle["requested_sources"])
    requested_rows: dict[str, dict[str, str]] = {}
    for source in requested_sources:
        if not isinstance(source, str) or "skill" not in source.lower():
            continue
        try:
            path = Path(source)
            if not path.is_absolute():
                path = target / path
            canonical_path = str(path.resolve(strict=True))
        except OSError:
            canonical_path = source
        owning_skill = next((skill_path for skill_path in by_path
                             if canonical_path == skill_path or Path(skill_path).parent in Path(canonical_path).parents), None)
        if owning_skill is not None:
            row = by_path[owning_skill]
            requested_rows[owning_skill] = {"name": row["name"], "skill_md_path": owning_skill}
        else:
            # Preserve the requested reference, but do not infer that it was available or read.
            requested_rows[canonical_path] = {"name": Path(canonical_path).parent.name[:128],
                                               "skill_md_path": canonical_path}
    reads = _skill_read_events(raw_events, target, available)
    verified_by_skill: dict[str, list[dict[str, Any]]] = {}
    for row in reads:
        path = Path(row["path"])
        owning_skill = next((skill_path for skill_path in by_path
                             if path == Path(skill_path) or Path(skill_path).parent in path.parents), None)
        if owning_skill is None:
            continue
        verified_by_skill.setdefault(owning_skill, []).append({
            "reference_path": row["path"], "operation": row["operation"],
            "runtime_event_type": row["event_type"], "runtime_event_id": row["event_id"],
            "runtime_event_sha256": row["event_sha256"], "returned_content_sha256": row["output_sha256"],
        })
    verified = []
    references_consulted = []
    for skill_path, evidence in sorted(verified_by_skill.items()):
        skill = by_path[skill_path]
        md_evidence = next((item for item in evidence if item["reference_path"] == skill_path), None)
        if md_evidence:
            verified.append({"name": skill["name"], "skill_md_path": skill_path,
                "stage": stage, "runtime_event": md_evidence})
        references_consulted.extend({"name": skill["name"], "skill_md_path": skill_path,
            "stage": stage, **item} for item in evidence if item["reference_path"] != skill_path)
    unverified = []
    for skill_path, row in sorted(requested_rows.items()):
        if not any(item["reference_path"] == skill_path for item in verified_by_skill.get(skill_path, [])):
            unverified.append({"name": row["name"], "skill_md_path": skill_path,
                "stage": stage, "reason": "no_successful_matching_skill_md_read_event"})
    return {"stage": stage,
        "available": [{"name": row["name"], "skill_md_path": row["skill_md_path"]} for row in available],
        "requested": [{"name": row["name"], "skill_md_path": row["skill_md_path"], "stage": stage}
                      for row in requested_rows.values()],
        "read_verified": verified, "references_consulted": references_consulted,
        "read_unverifiable": unverified,
        "prompt_directive": "Read only relevant target skills; explicit skill paths are listed in requested.",
        "runtime_event_source": "codex_exec_jsonl_item_completed",
        "limitations": [] if raw_events else ["No Codex runtime events were available for this attempt."]}


def _codex_failure_details(stdout: str, stderr: str) -> str:
    messages: list[str] = []
    for line in stdout.splitlines()[-100:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") not in {"error", "turn.failed"}:
            continue
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail")
        else:
            message = error or event.get("message")
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    details = [f"Codex: {message}" for message in messages[-2:]]
    if stderr.strip():
        details.append(f"stderr: {stderr[-700:].strip()}")
    return _redact("; ".join(details))


def _codex_mcp_disable_overrides() -> list[str]:
    config_path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OrchestratorError("Could not safely inspect Codex MCP server names for isolated stage execution") from exc
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise OrchestratorError("Codex MCP configuration must be a table to isolate stage execution")
    overrides: list[str] = []
    for name, server in sorted(servers.items()):
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or
                not isinstance(server, dict)):
            raise OrchestratorError("Codex MCP server configuration has an unsupported shape")
        if server.get("enabled", True) is not False:
            overrides.extend(("-c", f"mcp_servers.{name}.enabled=false"))
    return overrides


def _contained(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _git(target: Path, *args: str, check: bool = True) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"})
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-C", str(target), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
    )
    if check and result.returncode:
        raise OrchestratorError(f"Git inspection failed (exit {result.returncode})")
    return result.stdout if result.returncode == 0 else b""


def _status(target: Path) -> dict[str, dict[str, Any]]:
    raw = _git(target, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    rows, result, i = raw.split(b"\0"), {}, 0
    while i < len(rows):
        row = rows[i]
        i += 1
        if not row:
            continue
        if len(row) < 4:
            raise OrchestratorError("Malformed Git porcelain record")
        x, y = chr(row[0]), chr(row[1])
        path = row[3:].decode("utf-8", "strict")
        if x in "RC" or y in "RC":
            if i >= len(rows) or not rows[i]:
                raise OrchestratorError("Git rename record omitted its source path")
            source = rows[i].decode("utf-8", "strict")
            i += 1
            result[source] = {"index_state": "renamed", "working_tree_state": "renamed",
                              "worktree_sha256": None, "index_sha256": None}
        file = target / path
        # Never open secret/config paths while recording the Git baseline.
        if SECRET_PATH.search(path):
            content_hash = None
        elif file.is_symlink():
            content_hash = _sha(os.readlink(file).encode("utf-8", "surrogateescape"))
        elif file.is_file():
            content_hash = _sha(file.read_bytes())
        else:
            content_hash = "missing"
        staged = b"" if SECRET_PATH.search(path) else _git(target, "show", f":{path}", check=False)
        result[path] = {
            "index_state": "unchanged" if x == " " else ("added" if x in "A?" else "modified"),
            "working_tree_state": "untracked" if x == "?" else ("unchanged" if y == " " else ("deleted" if y == "D" else "modified")),
            "worktree_sha256": content_hash,
            "index_sha256": _sha(staged) if staged else None,
        }
    return result


class WorkflowStore:
    """Use safe_writer for workflow files and evidence."""

    def __init__(self, target: Path, workflow_id: str) -> None:
        self.target = target
        self.directory = f".engineering/workflows/{workflow_id}"
        self.state_path = f"{self.directory}/workflow-state.json"

    @staticmethod
    def _config(directory: Path, target: Path, path: str, *, classification: str = "artifact") -> Path:
        if classification not in {"code", "artifact"}:
            raise OrchestratorError("Write scope classification must be code or artifact")
        body = {
            "target_path": str(target),
            "authorized_working_directories": [str(target)],
            "included_paths": [path], "code_paths": [path] if classification == "code" else [],
            "artifact_paths": [path] if classification == "artifact" else [],
            "revision_paths": [], "excluded_paths": [],
        }
        file = directory / "scope.json"
        file.write_text(json.dumps(body), encoding="utf-8")
        return file

    def ensure_directory(self) -> None:
        current = self.target
        parts = PurePosixPath(self.directory).parts
        for index, component in enumerate(parts):
            current = current / component
            if current.is_symlink():
                raise OrchestratorError("Workflow directory contains a symlink")
            if current.exists():
                if not current.is_dir():
                    raise OrchestratorError("Workflow directory path is not a directory")
                continue
            rel = PurePosixPath(*parts[:index + 1]).as_posix()
            with tempfile.TemporaryDirectory(prefix="harness-scope-") as temp:
                cfg = self._config(Path(temp), self.target, rel)
                create_directories(self.target, cfg, rel)

    def read_state(self) -> dict[str, Any] | None:
        file = self.target / self.state_path
        if file.is_symlink():
            raise OrchestratorError("Workflow state is a symlink")
        if not file.exists():
            return None
        value = json.loads(self.read_bytes(self.state_path).decode("utf-8"))
        if not isinstance(value, dict):
            raise OrchestratorError("Workflow state must be a JSON object")
        return value

    def read_bytes(self, relative: str) -> bytes:
        relative = _relative(relative, "workflow artifact")
        if not relative.startswith(self.directory + "/"):
            raise OrchestratorError("Workflow reads must stay within their workflow directory")
        current = self.target
        parts = PurePosixPath(relative).parts
        for index, component in enumerate(parts):
            current = current / component
            try:
                info = current.lstat()
            except OSError as exc:
                raise OrchestratorError(f"Workflow artifact cannot be inspected: {relative}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise OrchestratorError(f"Workflow artifact path contains a symlink: {relative}")
            if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise OrchestratorError(f"Workflow artifact parent is not a directory: {relative}")
            if index == len(parts) - 1 and not stat.S_ISREG(info.st_mode):
                raise OrchestratorError(f"Workflow artifact is not a regular file: {relative}")
        return current.read_bytes()

    def read_json(self, relative: str) -> Any:
        return json.loads(self.read_bytes(relative).decode("utf-8"))

    def write(self, relative: str, value: Any) -> dict[str, Any]:
        relative = _relative(relative, "workflow artifact")
        if not (relative == self.directory or relative.startswith(self.directory + "/")):
            raise OrchestratorError("Workflow artifacts must stay within their workflow directory")
        file = self.target / relative
        parent = PurePosixPath(relative).parent
        if parent.as_posix() != ".":
            current = PurePosixPath()
            for component in parent.parts:
                current = current / component
                candidate = self.target / current.as_posix()
                if candidate.is_symlink():
                    raise OrchestratorError("Workflow artifact parent contains a symlink")
                if candidate.exists():
                    if not candidate.is_dir():
                        raise OrchestratorError("Workflow artifact parent is not a directory")
                    continue
                with tempfile.TemporaryDirectory(prefix="harness-scope-") as temp:
                    cfg = self._config(Path(temp), self.target, current.as_posix())
                    create_directories(self.target, cfg, current.as_posix())
        if file.is_symlink():
            raise OrchestratorError("Workflow artifact is a symlink")
        content = _json_bytes(value)
        with tempfile.TemporaryDirectory(prefix="harness-scope-") as temp:
            cfg = self._config(Path(temp), self.target, relative)
            if file.exists():
                if not file.is_file():
                    raise OrchestratorError("Workflow artifact is not a regular file")
                return replace_file(self.target, cfg, relative, content, _sha(file.read_bytes()))
            return create_file(self.target, cfg, relative, content)

    def save(self, state: dict[str, Any]) -> None:
        self.write(self.state_path, state)


def _interaction_store(target: str, workflow_id: str) -> tuple[WorkflowStore, dict[str, Any]]:
    if (not isinstance(target, str) or not Path(target).is_absolute() or
            not isinstance(workflow_id, str) or not SAFE_ID.fullmatch(workflow_id)):
        raise OrchestratorError("Decision interaction requires an absolute target and safe workflow ID")
    root = canonical(target)
    git_root = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve(strict=True)
    if git_root != root:
        raise OrchestratorError("Decision interaction target must be the canonical Git root")
    store = WorkflowStore(root, workflow_id)
    state = store.read_state()
    if (state is None or state.get("workflow_id") != workflow_id or
            not isinstance(state.get("target"), dict) or state["target"].get("git_root") != str(root)):
        raise OrchestratorError("Workflow state does not match the requested target and workflow ID")
    branch = _git(root, "branch", "--show-current").decode().strip()
    if not isinstance(state.get("orchestration"), dict) or state["orchestration"].get("branch") != branch:
        raise OrchestratorError("Workflow branch changed; decision interaction is refused")
    return store, state


def list_pending_decisions(target: str, workflow_id: str) -> dict[str, Any]:
    _, state = _interaction_store(target, workflow_id)
    pending = [copy.deepcopy(row) for row in state.get("pending_decisions", [])
               if isinstance(row, dict) and row.get("status") == "open" and
               row.get("invalidated_by_requirement_version") is None]
    return {"workflow_id": workflow_id, "workflow_status": state.get("status"),
            "pending_decisions": pending}


def record_decision_response(store: WorkflowStore, state: dict[str, Any], decision_id: str,
                             action: str, response: str, *, operator_identity: str,
                             source_reference: str,
                             source_kind: str = "interactive_local_cli") -> dict[str, Any]:
    actions = {"APPROVE", "REJECT", "REQUEST_CHANGES", "ALTERNATIVE"}
    if (not isinstance(decision_id, str) or not SAFE_ID.fullmatch(decision_id) or
            not isinstance(action, str) or action not in actions):
        raise OrchestratorError("Decision ID or response action is invalid")
    if not isinstance(response, str) or not response.strip() or len(response) > 10000:
        raise OrchestratorError("Decision response must contain 1 to 10000 characters")
    if state.get("workflow_id") != PurePosixPath(store.directory).name:
        raise OrchestratorError("Decision response state does not match the workflow store")
    sensitive = re.compile(
        r"(?i)(?:password|secret|api[_-]?key|access[_-]?token|bearer)\s*[:=]|"
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b")
    if sensitive.search(response):
        raise OrchestratorError("Decision response appears to contain a credential or secret")
    if (not isinstance(operator_identity, str) or not operator_identity.strip() or
            not isinstance(source_reference, str) or not source_reference.strip() or
            not isinstance(source_kind, str) or
            source_kind not in {"interactive_local_cli", "conversation_user_message"}):
        raise OrchestratorError("Decision response provenance must identify its declared source")
    if state.get("status") != "WAITING_FOR_HUMAN":
        raise OrchestratorError("Workflow is not waiting for a human decision")
    rows = state.get("pending_decisions")
    if not isinstance(rows, list):
        raise OrchestratorError("Workflow pending_decisions is malformed")
    matches = [row for row in rows if isinstance(row, dict) and row.get("decision_id") == decision_id]
    if (len(matches) != 1 or matches[0].get("status") != "open" or
            matches[0].get("invalidated_by_requirement_version") is not None):
        raise OrchestratorError("Decision ID is missing, ambiguous, or no longer open")
    decision = matches[0]
    if decision.get("workflow_id") not in {None, state["workflow_id"]}:
        raise OrchestratorError("Decision belongs to another workflow")
    history = decision.setdefault("response_history", [])
    if not isinstance(history, list):
        raise OrchestratorError("Decision response history is malformed")
    response_record = {
        "response_id": f"{decision_id}-R{len(history) + 1:02d}",
        "action": action, "response": response.strip(), "responded_at": _now(),
        "source": source_kind,
        "source_reference": source_reference, "operator_identity": operator_identity.strip(),
        "identity_assurance": "declared",
    }
    history.append(response_record)
    category = decision.get("decision_category", "unclassified_human_decision")
    interactive_allowed = (
        category == "functional_decision" and decision.get("interactive_resolution_allowed") is True and
        isinstance(decision.get("policy_or_skill_reference"), str) and
        bool(decision["policy_or_skill_reference"].strip())
    )
    resolved = False
    if interactive_allowed:
        decision["status"] = "resolved"
        decision["resolution_action"] = action
        decision["resolution"] = (decision.get("proposal") or decision.get("recommended_option")
                                  if action == "APPROVE" else response.strip())
        decision["resolved_by"] = operator_identity.strip()
        decision["resolved_by_kind"] = "human"
        decision["resolved_by_identity_assurance"] = "declared"
        decision["resolved_at"] = response_record["responded_at"]
        resolved = True
    # An interactive response is evidence of a declared response only. It can
    # never create external-service authorization or satisfy authenticated
    # review, risk acceptance, or gate-approval requirements.
    still_open = any(isinstance(row, dict) and row.get("status") == "open" for row in rows)
    if resolved and not still_open:
        state["status"] = "ACTIVE"
        state.pop("blocker", None)
    store.save(state)
    return {"workflow_id": state["workflow_id"], "decision_id": decision_id,
            "decision_status": decision["status"], "workflow_status": state["status"],
            "response_id": response_record["response_id"], "resolved": resolved,
            "identity_assurance": "declared"}


def submit_interactive_response(target: str, workflow_id: str, decision_id: str,
                                action: str, response: str, *, operator_identity: str) -> dict[str, Any]:
    store, state = _interaction_store(target, workflow_id)
    if state.get("status") != "WAITING_FOR_HUMAN":
        raise OrchestratorError("Workflow is not waiting for a human decision")
    return record_decision_response(store, state, decision_id, action, response,
                                    operator_identity=operator_identity,
                                    source_reference="interactive local CLI")


CLAUDE_AUTH_EXCLUSIONS = {"credentials", "environment_files", "private_configuration", "unrelated_files"}


def _validate_claude_authorization(value: Any) -> dict[str, Any]:
    required = {"authorization_id", "provider", "destination", "permitted_repositories", "permitted_paths",
                "mode", "max_budget_usd", "sensitive_exclusions", "authorized_at", "expires_at", "revoked",
                "authorization_reference", "authorizer_identity", "identity_assurance",
                "authorizer_authentication_reference"}
    if not isinstance(value, dict) or set(value) != required:
        raise OrchestratorError(f"Claude authorization must contain exactly: {', '.join(sorted(required))}")
    if not isinstance(value["authorization_id"], str) or not SAFE_ID.fullmatch(value["authorization_id"]):
        raise OrchestratorError("Claude authorization_id must be a safe identifier")
    if value["provider"] != "aily" or value["destination"] != "Claude through Aily":
        raise OrchestratorError("Claude authorization must explicitly name provider aily and destination Claude through Aily")
    if value["mode"] != "read_only":
        raise OrchestratorError("Claude authorization mode must be read_only")
    repositories = value["permitted_repositories"]
    if (not isinstance(repositories, list) or not repositories or
            any(not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts for path in repositories) or
            len(set(repositories)) != len(repositories)):
        raise OrchestratorError("Claude authorization permitted_repositories must be unique absolute repository paths")
    paths = value["permitted_paths"]
    if not isinstance(paths, list) or not paths:
        raise OrchestratorError("Claude authorization permitted_paths must be a non-empty explicit allowlist")
    value["permitted_paths"] = sorted({_relative(path, "Claude authorization permitted path") for path in paths})
    if len(value["permitted_paths"]) != len(paths):
        raise OrchestratorError("Claude authorization permitted_paths must not contain duplicates")
    exclusions = value["sensitive_exclusions"]
    if (not isinstance(exclusions, list) or any(not isinstance(item, str) for item in exclusions) or
            not CLAUDE_AUTH_EXCLUSIONS.issubset(exclusions) or len(set(exclusions)) != len(exclusions)):
        raise OrchestratorError("Claude authorization must explicitly exclude credentials, environment files, private configuration, and unrelated files")
    budget = value["max_budget_usd"]
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
        raise OrchestratorError("Claude authorization max_budget_usd must be finite and positive")
    if not isinstance(value["revoked"], bool):
        raise OrchestratorError("Claude authorization revoked must be boolean")
    value["permitted_repositories"] = sorted(value["permitted_repositories"])
    value["sensitive_exclusions"] = sorted(value["sensitive_exclusions"])
    if not isinstance(value["authorization_reference"], str) or not value["authorization_reference"].strip():
        raise OrchestratorError("Claude authorization_reference must identify the explicit authorization source")
    if not isinstance(value["authorizer_identity"], str) or not value["authorizer_identity"].strip():
        raise OrchestratorError("Claude authorization must identify its authorizer")
    if value["identity_assurance"] not in {"declared", "externally_authenticated"}:
        raise OrchestratorError("Claude authorization identity_assurance must be declared or externally_authenticated")
    auth_ref = value["authorizer_authentication_reference"]
    if value["identity_assurance"] == "externally_authenticated":
        if not isinstance(auth_ref, str) or not auth_ref.strip():
            raise OrchestratorError("Externally authenticated Claude authorization requires an authentication reference")
    elif auth_ref is not None:
        raise OrchestratorError("Declared Claude authorization must not claim external authentication evidence")
    try:
        authorized_at = datetime.fromisoformat(value["authorized_at"].replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
        if authorized_at.tzinfo is None or expires_at.tzinfo is None or expires_at <= authorized_at:
            raise ValueError("timezone and positive validity interval are required")
    except (AttributeError, TypeError, ValueError) as exc:
        raise OrchestratorError("Claude authorization timestamps must be ISO 8601 with timezone and a valid interval") from exc
    return value


def validate_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OrchestratorError("Configuration must be a JSON object")
    allowed = {"branch", "minimum_risk_band", "max_iterations", "stage_timeout_seconds",
               "claude_budget_usd", "validations", "review", "commit", "create_pr",
               "authorized_code_paths", "authorized_artifact_paths", "code_file_extensions"}
    if set(value) - allowed:
        raise OrchestratorError(f"Unsupported configuration keys: {sorted(set(value) - allowed)}")
    if not isinstance(value.get("branch"), str) or not value["branch"]:
        raise OrchestratorError("An exact existing target branch is required")
    authorized_code_paths = value.get("authorized_code_paths")
    authorized_artifact_paths = value.get("authorized_artifact_paths")
    if not isinstance(authorized_code_paths, list) or not authorized_code_paths:
        raise OrchestratorError("authorized_code_paths must explicitly authorize at least one code path")
    if not isinstance(authorized_artifact_paths, list):
        raise OrchestratorError("authorized_artifact_paths must be an explicit array (empty when no product artifacts are allowed)")
    authorized_code_paths = sorted({_relative(path, "authorized code path") for path in authorized_code_paths})
    authorized_artifact_paths = sorted({_relative(path, "authorized artifact path") for path in authorized_artifact_paths})
    if any(_contained(path, [other]) or _contained(other, [path])
           for path in authorized_code_paths for other in authorized_artifact_paths):
        raise OrchestratorError("Authorized code and artifact paths cannot overlap")
    extensions = value.get("code_file_extensions")
    if not isinstance(extensions, list) or any(not isinstance(ext, str) or
            not re.fullmatch(r"\.[A-Za-z0-9]+", ext) for ext in extensions):
        raise OrchestratorError("code_file_extensions must be an explicit array of dot-prefixed extensions")
    extensions = [ext.lower() for ext in extensions]
    if len(set(extensions)) != len(extensions):
        raise OrchestratorError("code_file_extensions must not contain duplicates")
    risk = value.get("minimum_risk_band", "medium")
    if not isinstance(risk, str) or risk not in RISK:
        raise OrchestratorError("minimum_risk_band must be low, medium, or high")
    iterations = value.get("max_iterations", 2)
    if isinstance(iterations, bool) or not isinstance(iterations, int) or not 1 <= iterations <= 5:
        raise OrchestratorError("max_iterations must be between 1 and 5")
    timeout = value.get("stage_timeout_seconds", 3600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 21600:
        raise OrchestratorError("stage_timeout_seconds must be between 1 and 21600")
    budget = value.get("claude_budget_usd")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or
                               not math.isfinite(budget) or budget <= 0):
        raise OrchestratorError("claude_budget_usd must be positive")
    validations = value.get("validations", [])
    if not isinstance(validations, list) or not validations:
        raise OrchestratorError("at least one explicit validation is required")
    ids = set()
    for item in validations:
        if not isinstance(item, dict) or set(item) - {"id", "argv", "cwd", "timeout_seconds"}:
            raise OrchestratorError("Validation entries allow only id, argv, cwd, timeout_seconds")
        if not isinstance(item.get("id"), str) or not SAFE_ID.fullmatch(item["id"]) or item["id"] in ids:
            raise OrchestratorError("Validation IDs must be unique safe identifiers")
        ids.add(item["id"])
        if not isinstance(item.get("argv"), list) or not item["argv"] or any(not isinstance(x, str) or not x for x in item["argv"]):
            raise OrchestratorError(f"Validation {item['id']} argv must be a non-empty argument array")
        if any(re.search(r"(?i)(?:password|secret|api[_-]?key|access[_-]?token|bearer)\s*[:=]|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b", arg)
               for arg in item["argv"]):
            raise OrchestratorError(f"Validation {item['id']} arguments cannot contain credentials or secret values")
        if item.get("cwd", ".") != ".":
            _relative(item["cwd"], f"validation {item['id']} cwd")
        timeout_value = item.get("timeout_seconds", timeout)
        if isinstance(timeout_value, bool) or not isinstance(timeout_value, int) or not 1 <= timeout_value <= timeout:
            raise OrchestratorError(f"Validation {item['id']} timeout must be between 1 and the configured stage timeout")
    if not isinstance(value.get("create_pr", False), bool):
        raise OrchestratorError("create_pr must be boolean")
    if value.get("create_pr", False):
        raise OrchestratorError("PR creation is not enabled: it requires an explicit publication integration and must never push")
    if not isinstance(value.get("commit", False), bool):
        raise OrchestratorError("commit must be boolean")
    if value.get("commit", False):
        raise OrchestratorError("Automatic commits are unavailable: policy requires separate approval for exact paths to stage and the exact staged diff")
    review = value.get("review", {"enabled": True})
    if not isinstance(review, dict) or set(review) - {"enabled", "context_paths", "authorization"}:
        raise OrchestratorError("review allows only enabled, context_paths, and persistent authorization")
    if not isinstance(review.get("enabled", True), bool):
        raise OrchestratorError("review.enabled must be boolean")
    if not review.get("enabled", True):
        raise OrchestratorError("Independent Claude review is a required orchestrator stage and cannot be disabled")
    context_paths = review.get("context_paths", [])
    if not isinstance(context_paths, list):
        raise OrchestratorError("review.context_paths must be an array")
    review["context_paths"] = sorted({_relative(path, "review context path") for path in context_paths})
    if review.get("enabled", True) and budget is None:
        raise OrchestratorError("Claude review requires an explicit claude_budget_usd")
    authorization = review.get("authorization")
    if authorization is not None:
        authorization = _validate_claude_authorization(copy.deepcopy(authorization))
        review["authorization"] = authorization
    else:
        review.pop("authorization", None)
    return {**value, "minimum_risk_band": risk, "max_iterations": iterations,
            "stage_timeout_seconds": timeout, "validations": validations,
            "review": review, "authorized_code_paths": authorized_code_paths,
            "authorized_artifact_paths": authorized_artifact_paths,
            "code_file_extensions": extensions}


def _plan_valid(plan: Any, config: dict[str, Any], *, controller_artifact_paths: list[str] | None = None) -> dict[str, Any]:
    required = {"decision", "summary", "requirements", "acceptance_criteria", "risk_band",
                "architecture_context", "documentation_context", "capabilities_used", "increments",
                "decision_context", "dependencies", "blockers"}
    legacy_required = required - {"decision_context", "dependencies", "blockers"}
    if (controller_artifact_paths is not None and isinstance(plan, dict) and
            frozenset(plan) in {frozenset(legacy_required), frozenset(legacy_required | {"decision_context"})}):
        plan["decision_context"] = None
        plan.setdefault("dependencies", [])
        plan.setdefault("blockers", [])
    if not isinstance(plan, dict) or set(plan) != required:
        raise OrchestratorError("Codex plan does not match the plan response contract")
    if not isinstance(plan["decision"], str) or plan["decision"] not in {"READY", "NEEDS_HUMAN_INPUT"}:
        raise OrchestratorError("Plan decision must be READY or NEEDS_HUMAN_INPUT")
    if not isinstance(plan["summary"], str) or not plan["summary"].strip():
        raise OrchestratorError("Plan summary must be a non-empty string")
    if not isinstance(plan["risk_band"], str) or plan["risk_band"] not in RISK:
        raise OrchestratorError("Plan risk_band must be low, medium, or high")
    plan["risk_band"] = max((plan["risk_band"], config["minimum_risk_band"]), key=RISK.get)
    decision_context = plan["decision_context"]
    if plan["decision"] == "NEEDS_HUMAN_INPUT":
        required_decision_fields = {"category", "question", "proposal", "alternatives", "reason",
                                    "policy_or_skill_reference", "required_authority",
                                    "interactive_resolution_allowed"}
        if (not isinstance(decision_context, dict) or set(decision_context) != required_decision_fields or
                not isinstance(decision_context.get("category"), str) or
                decision_context["category"] not in {"functional_decision", "external_data_transfer_authorization",
                    "restricted_operation_authorization", "security_risk_acceptance", "high_risk_approval",
                    "criterion_waiver", "unclassified_human_decision"}):
            raise OrchestratorError("A blocked plan needs a complete categorized decision context")
        for field in ("question", "proposal", "reason", "required_authority"):
            if not isinstance(decision_context[field], str) or not decision_context[field].strip():
                raise OrchestratorError(f"Decision context {field} must be a non-empty string")
        if (not isinstance(decision_context["alternatives"], list) or
                any(not isinstance(item, str) or not item.strip() for item in decision_context["alternatives"])):
            raise OrchestratorError("Decision context alternatives must be non-empty strings")
        reference = decision_context["policy_or_skill_reference"]
        if reference is not None and (not isinstance(reference, str) or not reference.strip()):
            raise OrchestratorError("Decision context policy_or_skill_reference must be null or non-empty")
        if reference is not None:
            _relative(reference.split("#", 1)[0], "decision policy/skill reference")
        if not isinstance(decision_context["interactive_resolution_allowed"], bool):
            raise OrchestratorError("Decision context interactive_resolution_allowed must be boolean")
        if decision_context["interactive_resolution_allowed"] and (
                decision_context["category"] != "functional_decision" or not reference):
            raise OrchestratorError("Interactive resolution is limited to cited functional decisions")
    elif decision_context is not None:
        raise OrchestratorError("A ready plan cannot include a pending decision context")
    if plan["decision"] == "NEEDS_HUMAN_INPUT":
        if any(plan[field] for field in ("requirements", "acceptance_criteria", "increments")):
            raise OrchestratorError("A blocked plan cannot include requirements, criteria, or increments")
        if any(not isinstance(plan[field], list) for field in
               ("requirements", "acceptance_criteria", "architecture_context", "documentation_context", "capabilities_used", "increments", "dependencies", "blockers")):
            raise OrchestratorError("Plan collections must be arrays")
        if any(not isinstance(value, str) or not value.strip()
               for field in ("dependencies", "blockers") for value in plan[field]):
            raise OrchestratorError("Plan dependencies and blockers must be non-empty strings")
        return plan
    for field in ("requirements", "acceptance_criteria", "increments"):
        if not isinstance(plan[field], list) or not plan[field]:
            raise OrchestratorError(f"Plan {field} must be non-empty")
    criteria = set()
    for field in ("requirements", "acceptance_criteria"):
        for item in plan[field]:
            if not isinstance(item, dict) or set(item) != {"id", "text", "source"}:
                raise OrchestratorError(f"Invalid plan {field} record")
            if any(not isinstance(item[key], str) or not item[key].strip() for key in item):
                raise OrchestratorError(f"Plan {field} fields must be non-empty strings")
            if item["id"] in criteria:
                raise OrchestratorError("Plan IDs must be unique")
            criteria.add(item["id"])
    for field in ("architecture_context", "documentation_context"):
        if not isinstance(plan[field], list) or any(not isinstance(x, str) or not x.strip() for x in plan[field]):
            raise OrchestratorError(f"Plan {field} must be an array of non-empty strings")
    for field in ("dependencies", "blockers"):
        if not isinstance(plan[field], list) or any(not isinstance(x, str) or not x.strip() for x in plan[field]):
            raise OrchestratorError(f"Plan {field} must be an array of non-empty strings")
    if plan["blockers"]:
        raise OrchestratorError("A READY plan cannot contain unresolved blockers")
    if not isinstance(plan["capabilities_used"], list):
        raise OrchestratorError("Plan capabilities_used must be an array")
    for capability in plan["capabilities_used"]:
        required_capability = {"name", "kind", "source_reference", "phases", "use_rationale", "limitations"}
        if not isinstance(capability, dict) or set(capability) != required_capability:
            raise OrchestratorError("Plan capability record has an invalid shape")
        if not isinstance(capability["kind"], str) or capability["kind"] not in {"skill", "agent_instruction", "tool_guidance", "workflow"}:
            raise OrchestratorError("Plan capability kind is unsupported")
        if any(not isinstance(capability[key], str) or not capability[key].strip()
               for key in ("name", "source_reference", "use_rationale")):
            raise OrchestratorError("Plan capability records need non-empty names and rationale")
        valid_phases = {"DISCOVERY", "REQUIREMENTS", "ARCHITECTURE", "PLANNING", "IMPLEMENTATION",
                        "TESTING", "INDEPENDENT_VERIFICATION", "CORRECTION", "VERIFICATION_GATE",
                        "COMMIT", "NEXT_INCREMENT"}
        phases = capability["phases"]
        if (not isinstance(phases, list) or not phases or
                any(not isinstance(phase, str) or phase not in valid_phases for phase in phases) or
                len(set(phases)) != len(phases)):
            raise OrchestratorError("Plan capability records need applicable phases")
        if not isinstance(capability["limitations"], list) or any(not isinstance(item, str) for item in capability["limitations"]):
            raise OrchestratorError("Plan capability limitations must be strings")
    validation_ids = {x["id"] for x in config["validations"]}
    increment_ids = set()
    expected = {"increment_id", "title", "code_paths", "artifact_paths", "revision_paths",
                "acceptance_criteria_ids", "validation_ids"}
    for inc in plan["increments"]:
        if not isinstance(inc, dict) or set(inc) != expected:
            raise OrchestratorError("Increment does not match the increment contract")
        if not isinstance(inc["increment_id"], str) or not SAFE_ID.fullmatch(inc["increment_id"]) or inc["increment_id"] in increment_ids:
            raise OrchestratorError("Increment IDs must be unique safe identifiers")
        increment_ids.add(inc["increment_id"])
        for field in ("code_paths", "artifact_paths", "revision_paths", "acceptance_criteria_ids", "validation_ids"):
            if not isinstance(inc[field], list):
                raise OrchestratorError(f"Increment {field} must be an array")
            if field.endswith("_paths"):
                normalized_paths = [_relative(path, field) for path in inc[field]]
                if len(set(normalized_paths)) != len(normalized_paths):
                    raise OrchestratorError(f"Increment {field} values must be unique")
                inc[field] = sorted(normalized_paths)
            else:
                if any(not isinstance(value, str) or not value.strip() for value in inc[field]):
                    raise OrchestratorError(f"Increment {field} values must be non-empty strings")
                if len(set(inc[field])) != len(inc[field]):
                    raise OrchestratorError(f"Increment {field} values must be unique")
        if not all(_contained(path, config["authorized_code_paths"]) for path in inc["code_paths"]):
            raise OrchestratorError("Plan code_paths exceed the explicitly authorized code paths")
        allowed_artifact_paths = config["authorized_artifact_paths"] + (controller_artifact_paths or [])
        if not all(_contained(path, allowed_artifact_paths) for path in inc["artifact_paths"]):
            raise OrchestratorError("Plan artifact_paths exceed the explicitly authorized artifact paths")
        for code_path in inc["code_paths"]:
            if any(_contained(code_path, [artifact]) or _contained(artifact, [code_path])
                   for artifact in inc["artifact_paths"]):
                raise OrchestratorError("code_paths and artifact_paths cannot overlap")
        if not inc["code_paths"] or not inc["revision_paths"]:
            raise OrchestratorError("Every increment needs code paths and revision paths")
        if not all(_contained(path, inc["code_paths"] + inc["artifact_paths"]) for path in inc["revision_paths"]):
            raise OrchestratorError("revision_paths must be inside the increment allowlist")
        if not inc["acceptance_criteria_ids"] or not set(inc["acceptance_criteria_ids"]) <= criteria:
            raise OrchestratorError("Increment criteria references are invalid")
        if not inc["validation_ids"]:
            raise OrchestratorError("Every increment must reference at least one configured validation")
        unknown_validations = sorted(set(inc["validation_ids"]) - validation_ids)
        if unknown_validations:
            raise OrchestratorError(
                f"Increment references unconfigured validations {unknown_validations}; configured IDs: {sorted(validation_ids)}"
            )
    return plan


def _stage_valid(result: Any, *, correction: bool = False) -> dict[str, Any]:
    required = {"status", "summary", "changes", "finding_decisions"}
    if not isinstance(result, dict) or set(result) != required:
        raise OrchestratorError("Codex stage response does not match the stage result contract")
    if not isinstance(result["status"], str) or result["status"] not in {"COMPLETED", "BLOCKED"}:
        raise OrchestratorError("Codex stage status must be COMPLETED or BLOCKED")
    if not isinstance(result["summary"], str) or not result["summary"].strip():
        raise OrchestratorError("Codex stage summary must be a non-empty string")
    if not isinstance(result["changes"], list):
        raise OrchestratorError("Codex stage changes must be an array")
    for change in result["changes"]:
        if (not isinstance(change, dict) or set(change) != {"path", "content"} or
                not isinstance(change["path"], str) or not isinstance(change["content"], str)):
            raise OrchestratorError("Each Codex change must contain a string path and UTF-8 content")
    decisions = result["finding_decisions"]
    if not isinstance(decisions, list):
        raise OrchestratorError("Codex finding_decisions must be an array")
    if correction:
        if result["status"] == "COMPLETED" and not isinstance(decisions, list):
            raise OrchestratorError("Codex correction response must contain finding_decisions")
        for item in decisions:
            if (not isinstance(item, dict) or set(item) != {"finding_id", "decision", "reason", "evidence_references"} or
                    not isinstance(item["finding_id"], str) or not item["finding_id"].strip() or
                    not isinstance(item["decision"], str) or item["decision"] not in {"ACCEPT", "REJECT", "DEFER"} or
                    not isinstance(item["reason"], str) or not item["reason"].strip() or
                    not isinstance(item["evidence_references"], list) or
                    any(not isinstance(ref, str) or not ref for ref in item["evidence_references"])):
                raise OrchestratorError("Codex finding decision does not match the correction contract")
    elif decisions:
        raise OrchestratorError("Only correction stages may return finding_decisions")
    return result


def _run_command(argv: list[str], cwd: Path, timeout: int,
                 process_runner: Callable[..., Any]) -> dict[str, Any]:
    executable = Path(argv[0]).name.lower()
    denied = {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh", "curl", "wget",
              "git", "codex", "aily", "claude", "ssh", "scp", "gh", "aws", "az", "gcloud",
              "kubectl", "helm", "terraform", "tofu", "docker", "podman", "brew", "pip", "pip3",
              "uv", "npm", "pnpm", "yarn"}
    subcommand = argv[1].lower() if len(argv) > 1 else ""
    dangerous_arguments = any(arg.lower().startswith(("http://", "https://", "--force", "--dangerous"))
                              for arg in argv[1:])
    if executable in denied or dangerous_arguments or subcommand in {"install", "restore", "clean", "push", "publish"}:
        raise OrchestratorError(f"Validation {argv[0]} has a prohibited install, network, Git, or destructive shape")
    if executable == "dotnet" and len(argv) > 1 and argv[1] in {"test", "build"} and "--no-restore" not in argv:
        raise OrchestratorError("dotnet test/build validations must include --no-restore")
    started = _now()
    begin = time.monotonic()
    secret_env = re.compile(r"(?i)(secret|token|password|credential|private.?key|api.?key|auth)")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and not secret_env.search(k)}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "UV_OFFLINE": "1", "PIP_NO_INDEX": "1",
                "NUGET_XMLDOC_MODE": "skip", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                "DOTNET_NOLOGO": "1", "npm_config_offline": "true"})
    try:
        result = process_runner(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=timeout, check=False)
        exit_code = result.returncode
        output = (result.stdout or b"") + b"\n" + (result.stderr or b"")
        if not isinstance(output, bytes):
            output = str(output).encode("utf-8", "replace")
        text = output.decode("utf-8", "replace")[-3000:]
    except subprocess.TimeoutExpired:
        exit_code, text = 124, f"Timed out after {timeout}s"
    return {"argv": argv, "working_directory": str(cwd), "exit_code": exit_code,
            "observed_result": text, "started_at": started, "finished_at": _now(),
            "duration_ms": int((time.monotonic() - begin) * 1000)}


class Orchestrator:
    """Run the plan → implement → self-review → validate → review → correct loop."""

    def __init__(self, target: str, task: str, config: dict[str, Any], workflow_id: str,
                 *, codex: str = "codex", aily: str | None = None,
                 process_runner: Callable[..., Any] = subprocess.run,
                 update_requirements_reason: str | None = None) -> None:
        if not Path(target).is_absolute() or not task.strip() or not SAFE_ID.fullmatch(workflow_id):
            raise OrchestratorError("An absolute target, non-empty task, and safe workflow ID are required")
        self.target = canonical(target)
        self.task = task.strip()
        self.config = validate_config(config)
        self.workflow_id, self.codex, self.aily = workflow_id, codex, aily
        self.process_runner = process_runner
        self.update_requirements_reason = update_requirements_reason
        self._codex_version: str | None = None
        self._codex_model: str | None = None
        self._codex_provider: str | None = None
        self.store = WorkflowStore(self.target, workflow_id)
        self.state: dict[str, Any] | None = None
        self.preflight: Preflight | None = None

    def initialize(self) -> None:
        root = Path(_git(self.target, "rev-parse", "--show-toplevel").decode().strip()).resolve(strict=True)
        if root != self.target:
            raise OrchestratorError("--target must be the canonical Git root")
        branch = _git(self.target, "branch", "--show-current").decode().strip()
        if branch != self.config["branch"]:
            raise OrchestratorError(f"Expected branch {self.config['branch']}; found {branch}")
        self.preflight = Preflight(str(self.target), str(HARNESS_ROOT), None, None)
        report = self.preflight.run()
        if report["overall_result"] != "READY":
            raise OrchestratorError(f"Target preflight is {report['overall_result']}: {report.get('blockers', [])}")
        self.store.ensure_directory()
        existing = self.store.read_state()
        current = _status(self.target)
        if existing:
            status = existing.get("status")
            target_record = existing.get("target")
            if not isinstance(status, str) or status not in {"ACTIVE", "WAITING_FOR_HUMAN", "BLOCKED", "COMPLETED"}:
                raise OrchestratorError("Existing workflow state has an unsupported status")
            if (existing.get("workflow_id") != self.workflow_id or not isinstance(target_record, dict) or
                    target_record.get("git_root") != str(self.target)):
                raise OrchestratorError("Existing workflow state identifies another target or workflow")
            orchestration = existing.get("orchestration")
            if not isinstance(orchestration, dict):
                raise OrchestratorError("Existing workflow state has no valid orchestration record")
            if orchestration.get("branch") != branch:
                raise OrchestratorError("Existing workflow state belongs to another branch")
            if existing.get("baseline", {}).get("commit") != report["repository_state"].get("head"):
                raise OrchestratorError("Target HEAD changed since workflow initialization; resume on the original baseline")
            preexisting_hashes = orchestration.get("preexisting_hashes")
            if (not isinstance(preexisting_hashes, list) or not isinstance(existing.get("pending_decisions"), list) or
                    not isinstance(existing.get("artifact_references"), list) or
                    not isinstance(existing.get("scope"), dict) or
                    not isinstance(existing.get("authorizations"), list) or
                    not isinstance(existing.get("review_assignments"), list) or
                    not isinstance(existing.get("claude_review_cycles", []), list) or
                    not isinstance(orchestration.get("attempts"), list) or
                    not isinstance(orchestration.get("increments"), list) or
                    not isinstance(orchestration.get("validation_results"), list)):
                raise OrchestratorError("Existing workflow state has malformed baseline, authorization, or history records")
            for item in preexisting_hashes:
                if (not isinstance(item, dict) or not isinstance(item.get("path"), str) or
                        not isinstance(item.get("protected_path"), bool) or
                        item.get("worktree_sha256") is not None and not isinstance(item.get("worktree_sha256"), str) or
                        item.get("index_sha256") is not None and not isinstance(item.get("index_sha256"), str)):
                    raise OrchestratorError("Existing workflow state contains a malformed pre-existing path record")
                latest = current.get(item["path"])
                if (not latest or latest["index_sha256"] != item["index_sha256"] or
                        (not item.get("protected_path") and latest["worktree_sha256"] != item["worktree_sha256"])):
                    raise OrchestratorError(f"Pre-existing change changed since workflow start: {item['path']}")
            task_changed = orchestration.get("task_sha256") != _sha(self.task.encode())
            config_changed = orchestration.get("config_sha256") != _sha(_json_bytes(self.config))
            if task_changed or config_changed:
                if not task_changed and config_changed and self._is_authorization_only_config_change(orchestration):
                    if status == "COMPLETED":
                        raise OrchestratorError("A completed workflow's Claude authorization cannot be changed in place")
                    self._apply_claude_authorization_config_change(existing)
                    self.store.save(existing)
                    self.state = existing
                    return
                if not self.update_requirements_reason or not self.update_requirements_reason.strip():
                    if task_changed:
                        raise OrchestratorError("Existing workflow belongs to different task text; use --update-requirements with a reason to preserve history and replan")
                    raise OrchestratorError("Existing workflow configuration changed; use --update-requirements with a reason to invalidate old scopes and replan")
                self._update_existing_requirements(existing, task_changed=task_changed,
                                                   config_changed=config_changed)
                self.store.save(existing)
                self.state = existing
                return
            if self.update_requirements_reason is not None:
                raise OrchestratorError("--update-requirements requires changed task text or configuration")
            self._ensure_requirement_metadata(existing)
            self.state = existing
            return
        if self.update_requirements_reason is not None:
            raise OrchestratorError("--update-requirements applies only to an existing workflow")
        workflow_directory = self.target / self.store.directory
        if any(workflow_directory.iterdir()):
            raise OrchestratorError("Workflow directory contains artifacts but no matching state; preserving them")
        assert self.preflight is not None
        repo = self.preflight.git_repos["target"]
        harness = self.preflight.git_repos["harness"]
        head = report["repository_state"]["head"]
        preexisting = []
        for path, row in sorted(current.items()):
            preexisting.append({"path": path, "index_state": row["index_state"],
                                "working_tree_state": row["working_tree_state"]})
        instructions = []
        for row in report.get("instructions", []):
            if row.get("status") in {"applied", "discovered_not_applicable"}:
                instructions.append({"source_reference": row["source_path"], "canonical_path": row["canonical_path"],
                    "content_fingerprint": row["sha256"], "applicability_paths": [row.get("applicability_path", str(self.target))],
                    "status": row["status"]})
        target_id = "target-" + _sha(str(self.target).encode())[:24]
        status_fp = _sha(json.dumps(current, sort_keys=True).encode())
        task_reference = f"{self.store.directory}/task.json"
        preflight_reference = f"{self.store.directory}/preflight.json"
        self.store.write(preflight_reference, report)
        self.store.write(task_reference, {"workflow_id": self.workflow_id, "task": self.task,
                                          "source_reference": f"task:{self.workflow_id}"})
        self.state = {
            "schema_version": 1, "workflow_id": self.workflow_id,
            "target": {"target_id": target_id, "input_path": str(self.target), "resolved_input_path": str(self.target),
                "git_root": str(repo["root"]), "git_common_dir": str(repo["common"]), "git_refs_dir": str(repo["refs"]),
                "git_object_store_paths": [str(x) for x in repo["object_stores"]]},
            "harness_root": str(HARNESS_ROOT), "harness_git_root": str(harness["root"]),
            "harness_git_common_dir": str(harness["common"]), "harness_git_refs_dir": str(harness["refs"]),
            "harness_git_object_store_paths": [str(x) for x in harness["object_stores"]],
            "scope": {"assignment_reference": f"task:{self.workflow_id}", "authorized_working_directories": [str(self.target)],
                "increment_id": "PENDING", "included_paths": [], "code_paths": [], "artifact_paths": [],
                "revision_paths": [], "excluded_paths": [".git", *current.keys()]},
            "baseline": {"commit": head, "status_captured_at": _now(), "working_tree_fingerprint": status_fp,
                         "preexisting_changes": preexisting},
            "phase": "DISCOVERY", "status": "ACTIVE", "phase_records": [], "artifact_references": [task_reference, preflight_reference],
            "findings_register_reference": f"{self.store.directory}/findings-register.json",
            "review_assignments": [], "authorized_instruction_roots": report.get("authorized_instruction_roots", [str(self.target)]),
            "instruction_records": instructions, "applied_capabilities": [], "authorizations": [],
            "authorization_verifier": {"identity": "primary Codex session", "role": "Workflow Owner",
                "identity_assurance": "declared", "source_reference": f"task:{self.workflow_id}",
                "authentication_reference": None, "authority_reference": None},
            "claude_review_cycles": [], "pending_decisions": [], "updated_at": _now(),
            "orchestration": {"version": 1, "task_sha256": _sha(self.task.encode()),
                "config_sha256": _sha(_json_bytes(self.config)),
                "branch": branch, "current_stage": "PLANNING", "max_iterations": self.config["max_iterations"],
                "iteration": 0, "current_increment_id": None, "plan_reference": None, "increments": [],
                "attempts": [], "validation_results": [], "review_results": [], "correction_status": [],
                "current_requirement_version": 1,
                "requirement_history": [{"version": 1, "task_sha256": _sha(self.task.encode()),
                    "task_reference": f"{self.store.directory}/requirements-v1.json", "reason": "initial requirement snapshot",
                    "created_at": _now(), "status": "current"}],
                "invalidations": [], "self_reviews": [],
                "config_snapshot": copy.deepcopy(self.config),
                "claude_authorization": copy.deepcopy(self.config["review"].get("authorization")),
                "claude_authorization_history": ([{"authorization_id": self.config["review"]["authorization"]["authorization_id"],
                    "action": "configured", "recorded_at": _now(),
                    "authorization_reference": self.config["review"]["authorization"]["authorization_reference"],
                    "authorization_sha256": _sha(_json_bytes(self.config["review"]["authorization"]))}]
                    if self.config["review"].get("authorization") else []),
                "claude_authorization_uses": [],
                "preexisting_hashes": [{"path": p, "worktree_sha256": row["worktree_sha256"], "index_sha256": row["index_sha256"],
                                         "protected_path": bool(SECRET_PATH.search(p))}
                                       for p, row in sorted(current.items())],
                "commit_authorization_fingerprint": None},
        }
        self.store.write(f"{self.store.directory}/requirements-v1.json", {
            "workflow_id": self.workflow_id, "version": 1, "task": self.task,
            "task_sha256": _sha(self.task.encode()), "reason": "initial requirement snapshot", "created_at": _now(),
            "configuration_sha256": _sha(_json_bytes(self.config)),
        })
        self.state["artifact_references"].append(f"{self.store.directory}/requirements-v1.json")
        self.store.save(self.state)

    def _apply_claude_authorization_config_change(self, existing: dict[str, Any]) -> None:
        orchestration = existing["orchestration"]
        previous_auth = orchestration.get("claude_authorization")
        next_auth = copy.deepcopy(self.config["review"].get("authorization"))
        now = _now()
        history = orchestration.setdefault("claude_authorization_history", [])
        if previous_auth and (not next_auth or previous_auth != next_auth):
            history.append({"authorization_id": previous_auth.get("authorization_id", "unspecified"),
                "action": "revoked", "recorded_at": now,
                "authorization_reference": previous_auth.get("authorization_reference"),
                "authorization_sha256": _sha(_json_bytes(previous_auth))})
        if next_auth:
            history.append({"authorization_id": next_auth.get("authorization_id", "unspecified"),
                "action": "revoked" if next_auth.get("revoked") else "configured", "recorded_at": now,
                "authorization_reference": next_auth.get("authorization_reference"),
                "authorization_sha256": _sha(_json_bytes(next_auth))})
        orchestration["config_snapshot"] = copy.deepcopy(self.config)
        orchestration["config_sha256"] = _sha(_json_bytes(self.config))
        orchestration["claude_authorization"] = next_auth
        auth_usable = bool(next_auth and not next_auth.get("revoked"))
        if auth_usable:
            try:
                authorized_at = datetime.fromisoformat(next_auth["authorized_at"].replace("Z", "+00:00"))
                expires_at = datetime.fromisoformat(next_auth["expires_at"].replace("Z", "+00:00"))
                auth_usable = (authorized_at.tzinfo is not None and expires_at.tzinfo is not None and
                    authorized_at <= datetime.now(timezone.utc) < expires_at and
                    any(Path(path).resolve(strict=True) == self.target for path in next_auth["permitted_repositories"]))
            except (KeyError, OSError, TypeError, ValueError):
                auth_usable = False
        for decision in existing.get("pending_decisions", []):
            if (not isinstance(decision, dict) or decision.get("status") != "open" or
                    decision.get("decision_category") != "external_data_transfer_authorization"):
                continue
            if not auth_usable:
                continue
            resolved_at = now
            source = next_auth["authorization_reference"]
            assurance = next_auth["identity_assurance"]
            response = {"response_id": f"{next_auth['authorization_id']}-config", "action": "APPROVE",
                "response": "Persistent Claude/Aily authorization supplied in the workflow configuration.",
                "responded_at": resolved_at, "source": "workflow_configuration", "source_reference": source,
                "operator_identity": next_auth["authorizer_identity"], "identity_assurance": assurance}
            if assurance == "externally_authenticated":
                response["authentication_reference"] = next_auth["authorizer_authentication_reference"]
                response["authority_reference"] = source
                decision["resolution_authentication_reference"] = next_auth["authorizer_authentication_reference"]
                decision["resolved_by_identity_assurance"] = assurance
            else:
                decision["resolved_by_identity_assurance"] = "declared"
            decision.setdefault("response_history", []).append(response)
            decision.update({"status": "resolved", "resolution_action": "APPROVE", "resolution": response["response"],
                "resolved_by": next_auth["authorizer_identity"], "resolved_by_kind": "human", "resolved_at": resolved_at})
        if auth_usable and not any(
                row.get("status") == "open" and row.get("decision_category") == "external_data_transfer_authorization"
                for row in existing.get("pending_decisions", []) if isinstance(row, dict)):
            existing["status"] = "ACTIVE"
            existing.pop("blocker", None)

    def _is_authorization_only_config_change(self, orchestration: dict[str, Any]) -> bool:
        previous = orchestration.get("config_snapshot")
        if not isinstance(previous, dict):
            if not self.config.get("review", {}).get("authorization"):
                return False
            candidate = copy.deepcopy(self.config)
            candidate.get("review", {}).pop("authorization", None)
            return _sha(_json_bytes(candidate)) == orchestration.get("config_sha256")
        old = copy.deepcopy(previous)
        new = copy.deepcopy(self.config)
        old_review = old.get("review") if isinstance(old.get("review"), dict) else {}
        new_review = new.get("review") if isinstance(new.get("review"), dict) else {}
        old_review.pop("authorization", None)
        new_review.pop("authorization", None)
        previous_auth = previous.get("review", {}).get("authorization")
        current_auth = self.config.get("review", {}).get("authorization")
        return old_review == new_review and old == new and previous_auth != current_auth

    def _ensure_requirement_metadata(self, existing: dict[str, Any]) -> None:
        orchestration = existing["orchestration"]
        changed = False
        if not orchestration.get("requirement_history"):
            task = self.store.read_json(f"{self.store.directory}/task.json")
            if (not isinstance(task, dict) or not isinstance(task.get("task"), str) or
                    _sha(task["task"].encode()) != orchestration.get("task_sha256")):
                raise OrchestratorError("Cannot migrate this workflow: the initial task artifact is unavailable")
            task_reference = f"{self.store.directory}/requirements-v1.json"
            snapshot = {"workflow_id": self.workflow_id, "version": 1, "task": task["task"],
                "task_sha256": _sha(task["task"].encode()), "reason": "legacy workflow snapshot migration",
                "created_at": _now(), "configuration_sha256": orchestration.get("config_sha256")}
            path = self.target / task_reference
            if path.is_symlink():
                raise OrchestratorError("Requirement migration snapshot path is a symlink")
            if path.exists():
                saved = self.store.read_json(task_reference)
                if (not isinstance(saved, dict) or saved.get("version") != 1 or
                        saved.get("task_sha256") != snapshot["task_sha256"] or saved.get("task") != task["task"]):
                    raise OrchestratorError("An untracked requirement migration artifact already exists")
                snapshot = saved
            else:
                self.store.write(task_reference, snapshot)
            orchestration["requirement_history"] = [{"version": 1, "task_sha256": snapshot["task_sha256"],
                "task_reference": task_reference, "reason": snapshot["reason"],
                "created_at": snapshot["created_at"], "status": "current"}]
            if task_reference not in existing.setdefault("artifact_references", []):
                existing["artifact_references"].append(task_reference)
            changed = True
        if "current_requirement_version" not in orchestration:
            current_rows = [row.get("version") for row in orchestration.get("requirement_history", [])
                            if isinstance(row, dict) and row.get("status") == "current"]
            if len(current_rows) != 1 or not isinstance(current_rows[0], int):
                raise OrchestratorError("Cannot infer the current requirement version from legacy workflow state")
            orchestration["current_requirement_version"] = current_rows[0]
            changed = True
        for field, default in (("invalidations", []), ("self_reviews", []),
                               ("claude_authorization_history", []), ("claude_authorization_uses", [])):
            if field not in orchestration:
                orchestration[field] = default
                changed = True
        if "config_snapshot" not in orchestration:
            orchestration["config_snapshot"] = copy.deepcopy(self.config)
            changed = True
        if "claude_authorization" not in orchestration:
            orchestration["claude_authorization"] = copy.deepcopy(self.config["review"].get("authorization"))
            changed = True
        if orchestration.get("config_snapshot") != self.config:
            raise OrchestratorError("Persisted workflow config snapshot does not match the current config fingerprint")
        if orchestration.get("claude_authorization") != self.config["review"].get("authorization"):
            raise OrchestratorError("Persisted Claude authorization does not match the explicitly supplied workflow config")
        self._validate_current_requirement_snapshot(orchestration)
        if changed:
            self.store.save(existing)

    def _validate_current_requirement_snapshot(self, orchestration: dict[str, Any]) -> dict[str, Any]:
        version = orchestration.get("current_requirement_version")
        history = orchestration.get("requirement_history")
        current = [row for row in history if isinstance(row, dict) and row.get("status") == "current"] if isinstance(history, list) else []
        if len(current) != 1 or current[0].get("version") != version:
            raise OrchestratorError("Requirement history must have exactly one current version")
        record = current[0]
        if record.get("task_sha256") != orchestration.get("task_sha256"):
            raise OrchestratorError("Current requirement history fingerprint disagrees with workflow state")
        reference = _relative(record.get("task_reference"), "current requirement reference")
        if not reference.startswith(self.store.directory + "/"):
            raise OrchestratorError("Current requirement snapshot reference is outside the workflow directory")
        snapshot = self.store.read_json(reference)
        if (not isinstance(snapshot, dict) or snapshot.get("workflow_id") != self.workflow_id or
                snapshot.get("version") != version or snapshot.get("task_sha256") != record["task_sha256"] or
                not isinstance(snapshot.get("task"), str) or _sha(snapshot["task"].encode()) != record["task_sha256"]):
            raise OrchestratorError("Current requirement snapshot content does not match its workflow record")
        snapshot_config = snapshot.get("configuration_sha256")
        if snapshot_config is not None and (not isinstance(snapshot_config, str) or
                not re.fullmatch(r"[a-f0-9]{64}", snapshot_config)):
            raise OrchestratorError("Requirement snapshot configuration fingerprint is malformed")
        return snapshot

    def _update_existing_requirements(self, existing: dict[str, Any], *, task_changed: bool,
                                      config_changed: bool) -> None:
        """Append a new requirement snapshot and invalidate all dependent workflow outputs."""
        orchestration = existing.get("orchestration")
        if not isinstance(orchestration, dict):
            raise OrchestratorError("Existing workflow has no orchestration record")
        history = orchestration.setdefault("requirement_history", [])
        invalidations = orchestration.setdefault("invalidations", [])
        self_reviews = orchestration.setdefault("self_reviews", [])
        if not isinstance(history, list) or not isinstance(invalidations, list) or not isinstance(self_reviews, list):
            raise OrchestratorError("Existing requirement history is malformed")
        current_version = orchestration.get("current_requirement_version", 1)
        if isinstance(current_version, bool) or not isinstance(current_version, int) or current_version < 1:
            raise OrchestratorError("Existing requirement version is malformed")
        if history:
            self._validate_current_requirement_snapshot(orchestration)
        next_version = current_version + 1
        reason = _redact(self.update_requirements_reason or "").strip()
        if not reason:
            raise OrchestratorError("Requirement update reason must not be empty")
        old_plan = orchestration.get("plan_reference")
        if old_plan is not None:
            old_plan = _relative(old_plan, "obsolete plan reference")
            if not old_plan.startswith(self.store.directory + "/"):
                raise OrchestratorError("Existing plan reference is outside its workflow directory")
        old_increment_ids = [row.get("increment_id") for row in orchestration.get("increments", [])
                             if isinstance(row, dict) and isinstance(row.get("increment_id"), str)]
        old_scope = copy.deepcopy(existing.get("scope", {}))
        prior_decisions = [row.get("decision_id") for row in existing.get("pending_decisions", [])
                           if isinstance(row, dict) and isinstance(row.get("decision_id"), str)]
        validation_ids = [row.get("validation_id") for row in orchestration.get("validation_results", [])
                          if isinstance(row, dict) and row.get("increment_id") in old_increment_ids]
        review_ids = [row.get("cycle_id") for row in existing.get("claude_review_cycles", [])
                      if isinstance(row, dict) and (row.get("increment_id") in old_increment_ids or
                          row.get("scope_kind") == "final")]
        authorization_ids = [row.get("authorization_id") for row in existing.get("authorizations", [])
                             if isinstance(row, dict) and isinstance(row.get("authorization_id"), str)]
        assignment_ids = [row.get("review_id") for row in existing.get("review_assignments", [])
                          if isinstance(row, dict) and isinstance(row.get("review_id"), str)]
        task_reference = f"{self.store.directory}/requirements-v{next_version}.json"
        if (self.target / task_reference).is_symlink():
            raise OrchestratorError("Next requirement snapshot path is a symlink")
        if not history:
            old_task = self.store.read_json(f"{self.store.directory}/task.json")
            prior_text = old_task.get("task") if isinstance(old_task, dict) else None
            if not isinstance(prior_text, str) or _sha(prior_text.encode()) != orchestration.get("task_sha256"):
                raise OrchestratorError("Cannot reconstruct the previous requirement snapshot safely")
            history.append({"version": current_version, "task_sha256": _sha(prior_text.encode()),
                "task_reference": f"{self.store.directory}/task.json", "reason": "reconstructed from preserved initial task artifact",
                "created_at": existing.get("updated_at", _now()), "status": "superseded"})
        for row in history:
            if isinstance(row, dict) and row.get("version") == current_version:
                row["status"] = "superseded"
        snapshot = {"workflow_id": self.workflow_id, "version": next_version,
            "task": self.task, "task_sha256": _sha(self.task.encode()), "reason": reason, "created_at": _now(),
            "configuration_sha256": _sha(_json_bytes(self.config))}
        if (self.target / task_reference).exists():
            saved_snapshot = self.store.read_json(task_reference)
            if (not isinstance(saved_snapshot, dict) or any(saved_snapshot.get(key) != snapshot[key]
                    for key in ("workflow_id", "version", "task", "task_sha256", "reason", "configuration_sha256")) or
                    not isinstance(saved_snapshot.get("created_at"), str)):
                raise OrchestratorError("Next requirement snapshot exists with different content; preserving it")
            snapshot = saved_snapshot
        else:
            self.store.write(task_reference, snapshot)
        history.append({"version": next_version, "task_sha256": _sha(self.task.encode()),
            "task_reference": task_reference, "reason": reason, "created_at": snapshot["created_at"], "status": "current"})
        invalidation = {"event_id": f"REQ-UPDATE-{next_version}", "from_version": current_version,
            "to_version": next_version, "reason": reason, "recorded_at": _now(),
            "task_changed": task_changed, "configuration_changed": config_changed,
            "obsolete_plan_reference": old_plan, "obsolete_increment_ids": old_increment_ids,
            "invalidated_decision_ids": prior_decisions,
            "invalidated_scope": old_scope,
            "invalidated_validation_ids": sorted({item for item in validation_ids if isinstance(item, str)}),
            "invalidated_review_cycle_ids": sorted({item for item in review_ids if isinstance(item, str)}),
            "invalidated_authorization_ids": sorted({item for item in authorization_ids if isinstance(item, str)}),
            "invalidated_review_assignment_ids": sorted({item for item in assignment_ids if isinstance(item, str)}),
            "invalidated_completion": copy.deepcopy(existing.get("completion")),
            "reasoning": "All planning-derived decisions, scopes, validations, reviews, and approvals are conservatively stale until replanned and reauthorized."}
        invalidations.append(invalidation)
        invalidated_decision_set = set(prior_decisions)
        invalidated_increment_set = set(old_increment_ids)
        invalidated_authorization_set = set(authorization_ids)
        invalidated_assignment_set = set(assignment_ids)
        for decision in existing.get("pending_decisions", []):
            if isinstance(decision, dict) and decision.get("decision_id") in invalidated_decision_set:
                decision.setdefault("invalidated_by_requirement_version", next_version)
        for row in orchestration.get("increments", []):
            if isinstance(row, dict) and (row.get("increment_id") in invalidated_increment_set or
                    row.get("scope_kind") == "final"):
                row.setdefault("invalidated_by_requirement_version", next_version)
        for row in orchestration.get("validation_results", []):
            if isinstance(row, dict) and row.get("increment_id") in invalidated_increment_set:
                row.setdefault("invalidated_by_requirement_version", next_version)
        for row in existing.get("review_assignments", []):
            if isinstance(row, dict) and row.get("review_id") in invalidated_assignment_set:
                row.setdefault("invalidated_by_requirement_version", next_version)
        for row in existing.get("claude_review_cycles", []):
            if isinstance(row, dict) and row.get("increment_id") in invalidated_increment_set:
                row.setdefault("invalidated_by_requirement_version", next_version)
        for authorization in existing.get("authorizations", []):
            if isinstance(authorization, dict):
                if authorization.get("revoked") is not True:
                    authorization["revoked"] = True
                    previous = authorization.get("expires_or_revocation", "")
                    authorization["expires_or_revocation"] = _redact(f"{previous}; revoked by requirement update {next_version}")[:1000]
        existing.pop("completion", None)
        existing.setdefault("artifact_references", []).append(task_reference)
        # Decision, authorization, validation, review, and increment records
        # remain intact. The invalidation event is the authoritative boundary
        # that prevents old records from being reused for the new version.
        existing["scope"] = {**old_scope, "increment_id": f"REQ-{next_version}",
            "included_paths": [], "code_paths": [], "artifact_paths": [], "revision_paths": []}
        orchestration.update({"task_sha256": _sha(self.task.encode()),
            "config_sha256": _sha(_json_bytes(self.config)), "max_iterations": self.config["max_iterations"],
            "current_requirement_version": next_version, "plan_reference": None, "current_increment_id": None,
            "current_stage": "PLANNING", "iteration": 0,
            })
        orchestration["config_snapshot"] = copy.deepcopy(self.config)
        orchestration["claude_authorization"] = copy.deepcopy(self.config["review"].get("authorization"))
        orchestration.setdefault("claude_authorization_history", [])
        orchestration.setdefault("claude_authorization_uses", [])
        orchestration.pop("final_revision_id", None)
        orchestration.pop("revision_manifest_reference", None)
        existing["status"] = "ACTIVE"
        existing["phase"] = "PLANNING"
        existing.pop("blocker", None)
        self.state = existing

    def _save(self) -> None:
        assert self.state is not None
        self.state["updated_at"] = _now()
        self.store.save(self.state)

    def _set_stage(self, stage: str, phase: str) -> None:
        assert self.state is not None
        self.state["orchestration"]["current_stage"] = stage
        self.state["phase"] = phase
        self.state["status"] = "ACTIVE"
        self._save()

    def _changed_validation_revision(self, blocker: dict[str, Any]) -> tuple[str, str] | None:
        """Permit one resume after a failed validation only when its scoped revision changed."""
        reason = blocker.get("reason") if isinstance(blocker, dict) else None
        if not isinstance(reason, str) or not reason.startswith("Required validation "):
            return None
        orchestration = self.state.get("orchestration", {})
        failures = [row for row in orchestration.get("validation_results", [])
                    if isinstance(row, dict) and row.get("exit_code") != 0]
        if not failures:
            return None
        failed = failures[-1]
        plan_reference = orchestration.get("plan_reference")
        if not isinstance(plan_reference, str):
            return None
        relative = _relative(plan_reference, "plan_reference")
        if not relative.startswith(self.store.directory + "/"):
            return None
        plan = self._read_plan(relative)
        increment = next((row for row in plan["increments"]
                          if row.get("increment_id") == failed.get("increment_id")), None)
        if increment is None:
            return None
        current_revision, _manifest, _reference = self._revision_manifest(increment)
        previous_revision = failed.get("revision_id")
        if not isinstance(previous_revision, str) or current_revision == previous_revision:
            return None
        return previous_revision, current_revision

    def _codex_runtime_metadata(self, binary: str) -> dict[str, Any]:
        if self._codex_version is None:
            self._codex_version = "unavailable"
            try:
                env = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_") and not re.search(r"(?i)(secret|token|password|credential|private.?key|api.?key|auth)", key)}
                result = subprocess.run([binary, "--version"], cwd=str(self.target), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False)
                version = ((result.stdout or b"") + b"\n" + (result.stderr or b"")).decode("utf-8", "replace").strip()
                if result.returncode == 0 and version and len(version) <= 160 and "\n" not in version:
                    self._codex_version = _redact(version)
            except (OSError, subprocess.SubprocessError):
                pass
        if self._codex_model is None or self._codex_provider is None:
            self._codex_model = self._codex_provider = "unavailable"
            config_path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
            try:
                config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
                model, provider = config.get("model"), config.get("model_provider")
                if isinstance(model, str) and len(model) <= 128:
                    self._codex_model = _redact(model)
                if isinstance(provider, str) and len(provider) <= 128:
                    self._codex_provider = _redact(provider)
            except (OSError, tomllib.TOMLDecodeError):
                pass
        return {"cli_version": self._codex_version, "model": self._codex_model,
                "provider": self._codex_provider, "python_runtime": platform.python_version()}

    def _read_plan(self, relative: str) -> dict[str, Any]:
        saved = self.store.read_json(relative)
        version = self.state["orchestration"].get("current_requirement_version", 1)
        if isinstance(saved, dict) and saved.get("artifact_schema_version") == 1:
            expected = {"artifact_schema_version": 1, "requirement_version": version,
                "task_sha256": self.state["orchestration"].get("task_sha256"),
                "config_sha256": self.state["orchestration"].get("config_sha256")}
            if any(saved.get(key) != value for key, value in expected.items()):
                raise OrchestratorError("Stored plan does not match the current requirement/configuration version")
            plan = saved.get("plan")
            if not isinstance(plan, dict):
                raise OrchestratorError("Stored versioned plan artifact is malformed")
        else:
            if version != 1:
                raise OrchestratorError("A replanned workflow cannot reuse an unversioned prior plan")
            plan = saved
        return _plan_valid(plan, self.config, controller_artifact_paths=[self.store.directory])

    def _activate_plan(self, plan: dict[str, Any], plan_path: str, version: int) -> None:
        orchestration = self.state["orchestration"]
        self.state["applied_capabilities"] = plan["capabilities_used"]
        if plan_path not in self.state["artifact_references"]:
            self.state["artifact_references"].append(plan_path)
        orchestration["plan_reference"] = plan_path
        existing_ids = {row.get("increment_id") for row in orchestration["increments"] if isinstance(row, dict)}
        for item in plan["increments"]:
            if item["increment_id"] in existing_ids:
                continue
            orchestration["increments"].append({**item, "status": "pending", "requirement_version": version})
        self.state["phase"] = "IMPLEMENTATION"
        self.state["status"] = "ACTIVE"
        self._save()

    def _source_trace(self, context: dict[str, Any]) -> dict[str, Any]:
        requested: list[str] = []
        for value in context.get("requested_sources", []):
            if isinstance(value, str):
                safe_value = "source-sha256:" + _sha(value.encode("utf-8")) if SECRET_PATH.search(value) else _redact(value)
                if safe_value not in requested:
                    requested.append(safe_value)
        fingerprints: list[dict[str, str]] = []
        trusted_roots = [self.target, HARNESS_ROOT]
        for value in self.state.get("authorized_instruction_roots", []):
            if isinstance(value, str):
                try:
                    trusted_roots.append(Path(value).resolve(strict=True))
                except OSError:
                    continue
        for value in requested:
            if SECRET_PATH.search(value):
                continue
            path = Path(value)
            if not path.is_absolute():
                path = self.target / path
            try:
                resolved = path.resolve(strict=True)
                if not any(resolved == root or root in resolved.parents for root in trusted_roots):
                    continue
                info = resolved.stat()
                if stat.S_ISREG(info.st_mode) and not path.is_symlink():
                    fingerprints.append({"reference": value, "available_source_sha256": _sha(resolved.read_bytes())})
            except OSError:
                continue
        return {"requested_sources": requested, "available_source_fingerprints": fingerprints,
                "read_evidence": "unavailable_without_codex_tool_event_log"}

    def _attempt_trace(self, stage: str, context: dict[str, Any], runtime: Path,
                       prompt: str, binary: str) -> dict[str, Any]:
        try:
            harness_revision = _git(HARNESS_ROOT, "rev-parse", "HEAD").decode("utf-8", "replace").strip()
        except OrchestratorError:
            harness_revision = "unavailable"
        try:
            runtime_files = _runtime_bundle_files(runtime)
            runtime_fingerprint = _sha(_json_bytes(runtime_files))
        except (OSError, OrchestratorError):
            runtime_fingerprint = "unavailable"
        orchestration = self.state.get("orchestration", {})
        write_scope = context.get("scope") or {
            "authorized_code_paths": context.get("authorized_code_paths", []),
            "authorized_artifact_paths": context.get("authorized_artifact_paths", []),
            "code_paths": context.get("code_paths", []), "artifact_paths": context.get("artifact_paths", []),
        }
        requirement = context.get("requirement", {})
        skill_context = {**context, "authorized_instruction_roots": self.state.get("authorized_instruction_roots", [])}
        trace = {"requirement_version": orchestration.get("current_requirement_version", 1),
            "requirement_sha256": requirement.get("task_sha256", orchestration.get("task_sha256")),
            "workflow_config_sha256": orchestration.get("config_sha256"),
            "plan_reference": orchestration.get("plan_reference"),
            "increment_id": (context.get("increment", {}).get("increment_id")
                             if isinstance(context.get("increment"), dict) else None),
            "target_baseline_commit": self.state.get("baseline", {}).get("commit"),
            "harness_revision": harness_revision, "harness_orchestrator_sha256": _sha(Path(__file__).read_bytes()),
            "runtime_bundle_sha256": runtime_fingerprint, **self._codex_runtime_metadata(binary),
            "prompt_sha256": _sha(prompt.encode("utf-8")),
            "permissions": {"sandbox": "read-only", "mcp": "disabled", "plugins": "disabled",
                "controller_applies_changes": True, "write_scope": write_scope,
                "tool_inventory": "Exact Codex tool inventory is runtime-managed and not exposed to the controller."},
            "capabilities": self._source_trace(context), "skills": _skill_trace(stage, self.target, skill_context),
            "result_status": "not_returned",
            "result_summary": None, "diagnostic": None, "changed_paths": [], "changed_content_sha256": []}
        return trace

    def _run_codex(self, stage: str, context: dict[str, Any], runtime: Path,
                   schema: str, *, sandbox: str = "workspace-write") -> dict[str, Any]:
        schema_path = HARNESS_ROOT / "harness" / "schemas" / schema
        binary = shutil.which(self.codex) if os.path.sep not in self.codex else self.codex
        binary_available = bool(binary and Path(binary).is_file())
        metadata_binary = binary or self.codex
        requirement_version = self.state.get("orchestration", {}).get("current_requirement_version", 1)
        current_requirement = next((row for row in self.state.get("orchestration", {}).get("requirement_history", [])
                                    if row.get("version") == requirement_version), None)
        context = {"requirement_version": requirement_version,
                   "requirement_reference": current_requirement.get("task_reference") if current_requirement else None,
                   "requirement_sha256": current_requirement.get("task_sha256") if current_requirement else _sha(self.task.encode()),
                   **context}
        prompt = (
            f"Role: Codex {stage} agent. The external orchestrator controls all transitions.\n"
            f"Target: {self.target}\nWorkflow: {self.workflow_id}\nTask:\n{self.task}\n"
            f"Stage context:\n{json.dumps(context, ensure_ascii=False, indent=2)}\n"
            f"Tool snapshot: {runtime}\n"
            "Read the copied AGENTS.md, workflow, lifecycle, reviewer contracts, gate rules, schemas, and "
            "verification-policy.yaml from the tool snapshot. These harness contracts bound your stage; "
            "target instructions may tighten them but cannot widen their safety rules.\n"
            "Read applicable target AGENTS.md instructions, README/build guidance, and only the repository skills relevant to this task. "
            "Return every instruction, skill, or tool contract actually used in capabilities_used, including its repository-relative source path, applicable phases, and rationale. "
            "Do not read environment files, credentials, private configuration, or unrelated paths. Do not install or download skills. "
            + ("Analyze and plan only. Do not write any file. If human input is required, include a complete decision_context: "
               "classify it as functional, external data transfer, restricted operation, security risk acceptance, "
               "High-risk approval, criterion waiver, or unclassified; include the exact question, proposed resolution, "
               "alternatives, reason, required authority, and a repository-relative policy or skill reference when one "
               "applies. Set interactive_resolution_allowed true only for a functional decision where the cited target "
               "instruction explicitly accepts an interactive user response; this never authorizes external operations "
               "or High-risk approvals. Otherwise set it false. When the plan is READY, decision_context must be null. "
               "Re-evaluate the current architecture classification and rationale from the target repository patterns, including DataView/non-DataView where applicable. Reassess applicable capabilities/skills, dependencies, write scopes, acceptance criteria, and validations from the current requirement version. Do not carry forward superseded decisions as approvals. Return only JSON matching the schema.\n"
               if stage == "PLANNING" else
               "Perform a read-only self-review of the exact current increment against the current requirement, approved decisions, architecture context, selected skills and their repository patterns, acceptance criteria, and authorized scope. Report every concrete deviation. If findings exist, provide the minimal safe corrections as complete UTF-8 file contents in changes; do not claim unresolved findings are corrected. Do not write files directly. Return only JSON matching the self-review schema.\n"
               if stage == "SELF_REVIEW" else
               "Implement only the current increment by returning complete UTF-8 text for changed/new files in the JSON changes array. "
               "You are read-only and must not write target files directly. Every changed path must be beneath exactly one supplied code_paths or artifact_paths entry. "
               "Do not modify workflow state, environment files, credentials, unrelated files, or Git index. Do not install, download, access network services, deploy, commit, or push. "
               "Do not return deletions or binary files. Always include finding_decisions; use an empty array outside correction. "
               "Return only JSON matching the schema.\n")
        )
        with tempfile.TemporaryDirectory(prefix="codex-stage-") as temp:
            output = Path(temp) / "result.json"
            started_at = _now()
            started_clock = time.monotonic()
            if not binary_available:
                trace = self._attempt_trace(stage, context, runtime, prompt, metadata_binary)
                trace["result_status"] = "process_not_started"
                trace["diagnostic"] = _redact(f"Codex executable is unavailable: {self.codex}")
                self.state["orchestration"]["attempts"].append({"stage": stage, "schema": schema,
                    "started_at": started_at, "finished_at": _now(), "exit_code": 127,
                    "duration_ms": int((time.monotonic() - started_clock) * 1000), "result_sha256": None,
                    "blocker_sha256": None, "trace": trace})
                self._save()
                raise OrchestratorError(f"Codex executable is unavailable: {self.codex}")
            cmd = [binary, "exec", "--json", "--ephemeral", "--disable", "plugins",
                   "-c", "mcp_servers={}", *_codex_mcp_disable_overrides(),
                   "--sandbox", "read-only", "--cd", str(self.target),
                   "--add-dir", str(runtime), "--output-schema", str(schema_path),
                   "--output-last-message", str(output), "-"]
            try:
                proc = self.process_runner(cmd, cwd=str(self.target), input=prompt.encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.config["stage_timeout_seconds"], check=False)
            except subprocess.TimeoutExpired as exc:
                trace = self._attempt_trace(stage, context, runtime, prompt, binary)
                trace["diagnostic"] = f"Timed out after {self.config['stage_timeout_seconds']}s"
                trace["result_status"] = "failed"
                self.state["orchestration"]["attempts"].append({"stage": stage, "schema": schema,
                    "started_at": started_at, "finished_at": _now(), "exit_code": 124,
                    "duration_ms": int((time.monotonic() - started_clock) * 1000), "result_sha256": None,
                    "blocker_sha256": None, "trace": trace})
                self._save()
                raise OrchestratorError(f"Codex {stage} timed out") from exc
            except OSError as exc:
                trace = self._attempt_trace(stage, context, runtime, prompt, binary)
                trace["result_status"] = "process_launch_failed"
                trace["diagnostic"] = _redact(str(exc))
                self.state["orchestration"]["attempts"].append({"stage": stage, "schema": schema,
                    "started_at": started_at, "finished_at": _now(), "exit_code": 127,
                    "duration_ms": int((time.monotonic() - started_clock) * 1000), "result_sha256": None,
                    "blocker_sha256": None, "trace": trace})
                self._save()
                raise OrchestratorError(f"Codex {stage} process could not launch: {_redact(str(exc))}") from exc
            raw = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else str(proc.stdout or "")
            trace = self._attempt_trace(stage, context, runtime, prompt, binary)
            trace["skills"] = _skill_trace(stage, self.target,
                {**context, "authorized_instruction_roots": self.state.get("authorized_instruction_roots", [])}, raw)
            attempt = {"stage": stage, "schema": schema, "started_at": started_at, "finished_at": _now(),
                       "exit_code": proc.returncode, "duration_ms": int((time.monotonic() - started_clock) * 1000),
                       "result_sha256": _sha(raw.encode("utf-8")) if raw else None, "blocker_sha256": None,
                       "trace": trace}
            try:
                result = json.loads(output.read_text(encoding="utf-8")) if output.exists() else json.loads(raw.strip().splitlines()[-1])
            except (OSError, json.JSONDecodeError, IndexError) as exc:
                trace["result_status"] = "invalid_or_missing_response"
                stderr = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else str(proc.stderr or "")
                process_detail = _codex_failure_details(raw, stderr)
                trace["diagnostic"] = _redact(f"Could not parse structured result: {type(exc).__name__}; {process_detail}")
                attempt["finished_at"] = _now()
                self.state["orchestration"]["attempts"].append(attempt)
                self._save()
                detail = f": {process_detail}" if process_detail else ""
                raise OrchestratorError(f"Codex {stage} did not return a valid structured result (exit {proc.returncode}){detail}") from exc
            if proc.returncode != 0:
                stderr = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else str(proc.stderr or "")
                diagnostic = _codex_failure_details(raw, stderr)
                trace["result_status"] = "process_failed"
                trace["diagnostic"] = diagnostic or f"Codex exited with status {proc.returncode}"
                self.state["orchestration"]["attempts"].append(attempt)
                self._save()
                detail = f": {diagnostic}" if diagnostic else ""
                raise OrchestratorError(f"Codex {stage} failed with exit {proc.returncode}{detail}")
            if not isinstance(result, dict):
                trace["result_status"] = "invalid_response_type"
                trace["diagnostic"] = "Structured response was not a JSON object"
                self.state["orchestration"]["attempts"].append(attempt)
                self._save()
                raise OrchestratorError(f"Codex {stage} response must be an object")
            trace["result_status"] = str(result.get("status", result.get("decision", "returned")))[:64]
            trace["normalized_result_sha256"] = _sha(_json_bytes(result))
            trace["result_summary"] = _redact(result.get("summary", "")) if isinstance(result.get("summary"), str) else None
            changes = result.get("changes", [])
            if isinstance(changes, list):
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    changed_path = change.get("path")
                    content = change.get("content")
                    if isinstance(changed_path, str):
                        try:
                            trace["changed_paths"].append(_relative(changed_path, "trace changed path"))
                        except OrchestratorError:
                            trace["changed_paths"].append("path-sha256:" + _sha(changed_path.encode("utf-8", "replace")))
                    if isinstance(content, str):
                        trace["changed_content_sha256"].append(_sha(content.encode("utf-8")))
            if stage == "SELF_REVIEW" and isinstance(result.get("findings"), list):
                trace["finding_count"] = len(result["findings"])
            self.state["orchestration"]["attempts"].append(attempt)
            self._save()
            return result

    def run(self) -> dict[str, Any]:
        try:
            self.initialize()
            assert self.state is not None
            if self.state["status"] == "COMPLETED":
                return self._result()
            invalidated_decision_ids = {row.get("decision_id") for row in self.state.get("pending_decisions", [])
                                        if isinstance(row, dict) and row.get("invalidated_by_requirement_version") is not None}
            pending = [row for row in self.state.get("pending_decisions", [])
                       if not isinstance(row, dict) or row.get("decision_id") not in invalidated_decision_ids]
            open_decisions = [row for row in pending if row.get("status") == "open"]
            if self.state["status"] == "WAITING_FOR_HUMAN":
                if any(row.get("decision_id") in {"HUMAN-APPROVAL", "COMMIT-AUTHORIZATION"} and row.get("status") == "open"
                       for row in pending) or open_decisions:
                    return self._result()
                if pending and any(not all(isinstance(row.get(key), str) and row[key].strip() for key in
                        ("resolution", "resolved_by", "resolved_at", "resolved_by_kind",
                         "resolved_by_identity_assurance")) for row in pending):
                    return self._result()
                self.state["status"] = "ACTIVE"
                self.state.pop("blocker", None)
                self._save()
            elif self.state["status"] == "BLOCKED":
                if open_decisions:
                    return self._result()
                blocker = self.state.get("blocker", {})
                blocker_hash = _blocker_fingerprint(blocker)
                prior = [row for row in self.state["orchestration"].get("attempts", [])
                         if row.get("stage") == "RESUME" and row.get("blocker_sha256") == blocker_hash]
                changed_validation_revision = self._changed_validation_revision(blocker)
                if prior and changed_validation_revision is None:
                    return self._result()
                resume_attempt = {
                    "stage": "RESUME", "schema": None, "started_at": _now(), "finished_at": _now(),
                    "exit_code": 0, "duration_ms": 0, "result_sha256": None,
                    "blocker_sha256": blocker_hash,
                }
                if changed_validation_revision is not None:
                    resume_attempt.update({
                        "resume_basis": "corrected_validation_revision",
                        "previous_revision_id": changed_validation_revision[0],
                        "current_revision_id": changed_validation_revision[1],
                    })
                self.state["orchestration"]["attempts"].append(resume_attempt)
                self.state["status"] = "ACTIVE"
                self.state.pop("blocker", None)
                self._save()
            with tempfile.TemporaryDirectory(prefix="harness-runtime-") as temp:
                runtime = Path(temp).resolve()
                manifest = _runtime_bundle_files(runtime)
                plan = self._planning(runtime)
                if plan is None:
                    return self._result()
                for increment in plan["increments"]:
                    self._increment(plan, increment, runtime)
                    if self.state["status"] != "ACTIVE":
                        return self._result()
                self._finalize_revision(plan)
                if not _runtime_bundle_unchanged(runtime, manifest):
                    raise OrchestratorError("Ephemeral harness tool snapshot changed")
                self._review_and_gate(plan)
                return self._result()
        except (OSError, OrchestratorError, PreflightError, ValueError) as exc:
            if self.state is not None:
                orchestration = self.state.get("orchestration", {})
                attempts = orchestration.get("attempts", []) if isinstance(orchestration, dict) else []
                if attempts and isinstance(attempts[-1], dict):
                    trace = attempts[-1].get("trace")
                    if (isinstance(trace, dict) and trace.get("diagnostic") is None and
                            attempts[-1].get("stage") == orchestration.get("current_stage")):
                        trace["diagnostic"] = _redact(str(exc))
                        trace["result_status"] = "controller_validation_or_stage_failure"
                self.state["status"] = "BLOCKED"
                self.state["blocker"] = {
                    "reason": _redact(str(exc))[:1200], "affected_phase": self.state.get("phase", "DISCOVERY"),
                    "recorded_at": _now(),
                    "safe_recovery_path": "Resolve the recorded blocker and resume with the same target, task, and workflow ID.",
                }
                self._save()
            raise

    def _planning(self, runtime: Path) -> dict[str, Any] | None:
        orchestration = self.state["orchestration"]
        if orchestration["plan_reference"]:
            relative = _relative(orchestration["plan_reference"], "plan_reference")
            if not relative.startswith(self.store.directory + "/"):
                raise OrchestratorError("Existing plan reference is outside the workflow directory")
            return self._read_plan(relative)
        self._set_stage("PLANNING", "PLANNING")
        version = orchestration.get("current_requirement_version", 1)
        plan_path = f"{self.store.directory}/plan.json" if version == 1 else f"{self.store.directory}/plan-v{version}.json"
        if (self.target / plan_path).is_symlink():
            raise OrchestratorError("Versioned plan artifact path is a symlink")
        if (self.target / plan_path).exists():
            plan = self._read_plan(plan_path)
            self._activate_plan(plan, plan_path, version)
            return plan
        invalidated_decision_ids = {item for event in orchestration.get("invalidations", [])
                                    if isinstance(event, dict)
                                    for item in event.get("invalidated_decision_ids", [])}
        superseded_decisions = [row for row in self.state.get("pending_decisions", [])
                                if isinstance(row, dict) and
                                row.get("decision_id") in invalidated_decision_ids]
        current_decisions = [row for row in self.state.get("pending_decisions", [])
                             if isinstance(row, dict) and row.get("status") == "resolved" and
                             row.get("decision_id") not in invalidated_decision_ids]
        result = self._run_codex("PLANNING", {"branch": self.config["branch"],
             "preflight": "READY", "instructions": [row["source_reference"] for row in self.state["instruction_records"]],
             "requested_sources": [row["source_reference"] for row in self.state["instruction_records"]],
             "previous_plan_reference": orchestration.get("plan_reference"),
             "requirement_update_history": copy.deepcopy(orchestration.get("invalidations", [])),
             "superseded_decisions_not_reusable": superseded_decisions,
             "authorized_code_paths": self.config["authorized_code_paths"],
             "authorized_artifact_paths": self.config["authorized_artifact_paths"],
             "code_file_extensions": self.config["code_file_extensions"],
             "configured_validations": [
                 {"id": item["id"], "argv": item["argv"], "cwd": item.get("cwd", ".")}
                 for item in self.config["validations"]
             ],
             "resolved_decisions": current_decisions,
             "selected_validations": copy.deepcopy(self.config["validations"])},
             runtime, "orchestrator-plan.schema.json", sandbox="read-only")
        if result["decision"] == "NEEDS_HUMAN_INPUT":
            decision = result["decision_context"]
            base_id = "PLAN-CLARIFICATION"
            existing_ids = {row.get("decision_id") for row in self.state["pending_decisions"]}
            decision_id = base_id
            suffix = 2
            while decision_id in existing_ids:
                decision_id = f"{base_id}-{suffix}"
                suffix += 1
            requested_at = _now()
            self.state["status"] = "WAITING_FOR_HUMAN"
            self.state["pending_decisions"].append({
                "decision_id": decision_id, "workflow_id": self.workflow_id,
                "decision_category": decision["category"], "stage": "PLANNING",
                "question": decision["question"], "proposal": decision["proposal"],
                "reason": decision["reason"], "policy_or_skill_reference": decision["policy_or_skill_reference"],
                "interactive_resolution_allowed": decision["interactive_resolution_allowed"],
                "response_history": [], "question_or_action": decision["question"],
                "impact": decision["reason"], "recommended_option": decision["proposal"],
                "alternatives": decision["alternatives"], "required_authority": decision["required_authority"],
                "affected_phase": "PLANNING", "requested_at": requested_at,
                "created_at": requested_at, "status": "open",
            })
            self._save()
            return None
        plan = _plan_valid(result, self.config)
        for item in plan["increments"]:
            if version > 1:
                raw_id = item["increment_id"]
                prefix = f"req{version}-"
                item["increment_id"] = prefix + raw_id if len(prefix + raw_id) <= 64 else prefix + raw_id[:43] + "-" + _sha(raw_id.encode())[:12]
            item["artifact_paths"] = sorted(set(item["artifact_paths"] + [self.store.directory]))
        self.state["applied_capabilities"] = plan["capabilities_used"]
        self.store.write(plan_path, {"artifact_schema_version": 1, "requirement_version": version,
            "task_sha256": orchestration["task_sha256"], "config_sha256": orchestration["config_sha256"],
            "plan": plan})
        self._activate_plan(plan, plan_path, version)
        return plan

    def _implementation_context(self, plan: dict[str, Any], increment: dict[str, Any]) -> dict[str, Any]:
        orchestration = self.state["orchestration"]
        version = orchestration.get("current_requirement_version", 1)
        history = next((row for row in orchestration.get("requirement_history", [])
                        if row.get("version") == version), {})
        invalidated = {item for event in orchestration.get("invalidations", []) if isinstance(event, dict)
                       for item in event.get("invalidated_decision_ids", [])}
        decisions = [copy.deepcopy(row) for row in self.state.get("pending_decisions", [])
                     if isinstance(row, dict) and row.get("status") == "resolved" and row.get("decision_id") not in invalidated]
        capability_context = [copy.deepcopy(item) for item in plan["capabilities_used"]
                              if "IMPLEMENTATION" in item.get("phases", []) or "CORRECTION" in item.get("phases", [])]
        requested_sources = [row.get("source_reference") for row in self.state.get("instruction_records", [])
                             if isinstance(row, dict) and isinstance(row.get("source_reference"), str)]
        requested_sources.extend(item["source_reference"] for item in capability_context)
        validations = {item["id"]: item for item in self.config["validations"]}
        return {"requirement": {"version": version, "reference": history.get("task_reference"),
                    "task_sha256": history.get("task_sha256", orchestration.get("task_sha256")),
                    "text": self.task},
            "increment": copy.deepcopy(increment), "requirements": copy.deepcopy(plan["requirements"]),
            "approved_decisions": decisions, "architecture_context": copy.deepcopy(plan["architecture_context"]),
            "documentation_context": copy.deepcopy(plan["documentation_context"]),
            "capabilities_selected": capability_context,
            "skills_and_instructions": {"requested_sources": requested_sources,
                "read_evidence": "Source fingerprints can establish available content, not that the child session read it."},
            "acceptance_criteria": copy.deepcopy(plan["acceptance_criteria"]),
            "constraints": {"task": self.task, "code_paths": increment["code_paths"],
                "artifact_paths": increment["artifact_paths"], "revision_paths": increment["revision_paths"],
                "authorized_code_paths": self.config["authorized_code_paths"],
                "authorized_artifact_paths": self.config["authorized_artifact_paths"]},
            "scope": copy.deepcopy(self.state["scope"]),
            "dependencies": copy.deepcopy(plan["dependencies"]), "blockers": copy.deepcopy(plan["blockers"]),
            "validations": [copy.deepcopy(validations[key]) for key in increment["validation_ids"]],
            "prior_requirement_invalidations": copy.deepcopy(orchestration.get("invalidations", [])),
            "requested_sources": requested_sources}

    @staticmethod
    def _self_review_valid(value: Any, increment: dict[str, Any]) -> dict[str, Any]:
        required = {"decision", "summary", "findings", "changes"}
        if not isinstance(value, dict) or set(value) != required:
            raise OrchestratorError("Self-review response does not match the self-review contract")
        if not isinstance(value["decision"], str) or value["decision"] not in {"PASS", "CORRECTED", "BLOCKED"}:
            raise OrchestratorError("Self-review decision must be PASS, CORRECTED, or BLOCKED")
        if not isinstance(value["summary"], str) or not value["summary"].strip():
            raise OrchestratorError("Self-review summary must be non-empty")
        if not isinstance(value["findings"], list) or not isinstance(value["changes"], list):
            raise OrchestratorError("Self-review findings and changes must be arrays")
        finding_ids: set[str] = set()
        for finding in value["findings"]:
            fields = {"id", "severity", "file", "description", "recommendation"}
            if not isinstance(finding, dict) or set(finding) != fields:
                raise OrchestratorError("Self-review finding has an invalid shape")
            if (not isinstance(finding["id"], str) or not SAFE_ID.fullmatch(finding["id"]) or
                    finding["id"] in finding_ids):
                raise OrchestratorError("Self-review finding IDs must be unique safe identifiers")
            finding_ids.add(finding["id"])
            if not isinstance(finding["severity"], str) or finding["severity"] not in {"critical", "high", "medium", "low"}:
                raise OrchestratorError("Self-review finding severity is invalid")
            path = _relative(finding["file"], "self-review finding file")
            if not _contained(path, increment["code_paths"] + increment["artifact_paths"]):
                raise OrchestratorError("Self-review finding refers outside the increment allowlist")
            if any(not isinstance(finding[key], str) or not finding[key].strip()
                   for key in ("description", "recommendation")):
                raise OrchestratorError("Self-review finding description and recommendation must be non-empty")
        for change in value["changes"]:
            if not isinstance(change, dict) or set(change) != {"path", "content"} or not isinstance(change["content"], str):
                raise OrchestratorError("Self-review changes must contain only path and complete text content")
        if value["decision"] == "PASS" and (value["findings"] or value["changes"]):
            raise OrchestratorError("PASS self-review cannot contain findings or changes")
        if value["decision"] == "CORRECTED" and (not value["findings"] or not value["changes"]):
            raise OrchestratorError("CORRECTED self-review must identify findings and provide corrections")
        if value["decision"] == "BLOCKED" and not value["findings"]:
            raise OrchestratorError("BLOCKED self-review must include an evidenced finding")
        return value

    def _self_review(self, plan: dict[str, Any], increment: dict[str, Any], runtime: Path) -> None:
        orchestration = self.state["orchestration"]
        version = orchestration.get("current_requirement_version", 1)
        before_revision, _, _ = self._revision_manifest(increment)
        existing = next((row for row in reversed(orchestration.setdefault("self_reviews", []))
                         if row.get("increment_id") == increment["increment_id"] and
                         row.get("requirement_version") == version and row.get("status") == "passed" and
                         row.get("revision_after") == before_revision), None)
        if existing is not None:
            return
        self._set_stage("SELF_REVIEW", "IMPLEMENTATION")
        context = self._implementation_context(plan, increment)
        context.update({"review_mode": "self-review; not independent review or approval",
            "revision_before_review": before_revision,
            "changed_paths": next((row.get("changed_paths", []) for row in orchestration["increments"]
                                    if row.get("increment_id") == increment["increment_id"]), []),
            "review_checklist": ["current requirement and version", "approved decisions", "architecture context",
                "applicable skills and repository patterns", "acceptance criteria", "authorized paths"]})
        findings: list[dict[str, Any]] = []
        correction_count = 0
        passed = False
        while correction_count <= self.config["max_iterations"]:
            review = self._self_review_valid(self._run_codex("SELF_REVIEW", context, runtime,
                "orchestrator-self-review.schema.json", sandbox="read-only"), increment)
            for finding in review["findings"]:
                safe_finding = copy.deepcopy(finding)
                safe_finding["description"] = _redact(safe_finding["description"])
                safe_finding["recommendation"] = _redact(safe_finding["recommendation"])
                if safe_finding not in findings:
                    findings.append(safe_finding)
            if review["decision"] == "BLOCKED":
                raise OrchestratorError("Self-review is blocked: " + _redact(review["summary"]))
            if review["decision"] == "PASS":
                passed = True
                break
            if correction_count >= self.config["max_iterations"]:
                break
            self._apply_changes(review["changes"], increment)
            correction_count += 1
            context.update({"review_mode": "verify self-review and any prior corrections",
                "prior_self_review_findings": findings,
                "correction_paths": [item["path"] for item in review["changes"]],
                "correction_pass": correction_count})
        if not passed:
            raise OrchestratorError("Self-review findings remain after the bounded correction loop")
        after_revision, _, _ = self._revision_manifest(increment)
        orchestration["self_reviews"].append({"requirement_version": version,
            "increment_id": increment["increment_id"], "revision_before": before_revision,
            "revision_after": after_revision, "status": "passed", "findings": findings,
            "correction_passes": correction_count, "completed_at": _now(),
            "classification": "self_review_not_independent"})
        self._save()

    def _increment(self, plan: dict[str, Any], increment: dict[str, Any], runtime: Path) -> None:
        orchestration = self.state["orchestration"]
        record = next(item for item in orchestration["increments"] if item["increment_id"] == increment["increment_id"])
        if record["status"] in {"validated", "reviewed", "complete"}:
            return
        orchestration["current_increment_id"] = increment["increment_id"]
        self.state["scope"].update({"increment_id": increment["increment_id"],
            "included_paths": sorted(set(increment["code_paths"] + increment["artifact_paths"])),
            "code_paths": increment["code_paths"], "artifact_paths": increment["artifact_paths"],
            "revision_paths": increment["revision_paths"]})
        self._set_stage("IMPLEMENTATION", "IMPLEMENTATION")
        if record["status"] == "pending":
            result = self._run_codex("IMPLEMENTATION", self._implementation_context(plan, increment),
                runtime, "orchestrator-stage-result.schema.json", sandbox="read-only")
            _stage_valid(result)
            if result["status"] != "COMPLETED":
                raise OrchestratorError(result.get("summary", "Codex implementation did not complete"))
            self._apply_changes(result.get("changes"), increment)
        elif record["status"] != "implemented":
            raise OrchestratorError(f"Unsupported increment resume state: {record['status']}")
        self._assert_increment_changes(increment)
        record["status"] = "implemented"
        current = _status(self.target)
        baseline = {row["path"] for row in self.state["baseline"]["preexisting_changes"]}
        record["changed_paths"] = sorted(path for path in set(current) - baseline
            if not (path == self.store.directory or path.startswith(self.store.directory + "/")))
        self._save()
        self._self_review(plan, increment, runtime)
        self._assert_increment_changes(increment)
        current = _status(self.target)
        baseline = {row["path"] for row in self.state["baseline"]["preexisting_changes"]}
        changed = sorted(path for path in set(current) - baseline
                         if not (path == self.store.directory or path.startswith(self.store.directory + "/")))
        record["changed_paths"] = changed
        revision_id, revision_manifest, manifest_path = self._revision_manifest(increment)
        record["revision_id"] = revision_id
        record["revision_manifest_reference"] = manifest_path
        self.state["orchestration"]["final_revision_id"] = revision_id
        self.state["orchestration"]["revision_manifest_reference"] = manifest_path
        self.store.write(manifest_path, revision_manifest)
        if manifest_path not in self.state["artifact_references"]:
            self.state["artifact_references"].append(manifest_path)
        self._save()
        self._validate(plan, increment, revision_id)

    def _assert_increment_changes(self, increment: dict[str, Any]) -> None:
        current = _status(self.target)
        baseline = {row["path"] for row in self.state["baseline"]["preexisting_changes"]}
        changed = sorted(path for path in set(current) - baseline
                         if not (path == self.store.directory or path.startswith(self.store.directory + "/")))
        authorized = increment["code_paths"] + increment["artifact_paths"]
        unexpected = [path for path in changed if not _contained(path, authorized)]
        if unexpected:
            raise OrchestratorError(f"Agent changed paths outside authorized increment scope: {unexpected}")
        for path in baseline:
            saved = next(x for x in self.state["orchestration"]["preexisting_hashes"] if x["path"] == path)
            now = current.get(path)
            if (not now or now["index_sha256"] != saved["index_sha256"] or
                    (not saved.get("protected_path") and now["worktree_sha256"] != saved["worktree_sha256"])):
                raise OrchestratorError(f"Pre-existing change was modified: {path}")

    def _revision_manifest(self, increment: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
        files: dict[str, dict[str, Any]] = {}
        for relative in increment["revision_paths"]:
            path = self.target / relative
            if not path.exists() and not path.is_symlink():
                raise OrchestratorError(f"A declared revision path does not exist: {relative}")
            candidates: list[Path] = []
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                for current, dirs, names in os.walk(path, topdown=True, followlinks=False):
                    dirs.sort()
                    names.sort()
                    current_path = Path(current)
                    for name in list(dirs):
                        child = current_path / name
                        if child.is_symlink():
                            candidates.append(child)
                            dirs.remove(name)
                    candidates.extend(current_path / name for name in names)
            else:
                candidates.append(path)
            for candidate in candidates:
                name = candidate.relative_to(self.target).as_posix()
                _relative(name, "revision manifest path")
                item = candidate.lstat()
                if stat.S_ISLNK(item.st_mode):
                    resolved = candidate.resolve(strict=True)
                    if resolved != self.target and self.target not in resolved.parents:
                        raise OrchestratorError(f"Revision symlink escapes the target: {name}")
                    payload = os.readlink(candidate).encode("utf-8", "surrogateescape")
                    file_type = "symlink"
                elif stat.S_ISREG(item.st_mode):
                    payload = candidate.read_bytes()
                    file_type = "regular_file"
                else:
                    raise OrchestratorError(f"Unsupported file type in revision paths: {name}")
                files[name] = {"path": name, "file_type": file_type,
                               "mode": stat.S_IMODE(item.st_mode), "sha256": _sha(payload)}
        manifest_rows = [files[name] for name in sorted(files)]
        if not manifest_rows:
            raise OrchestratorError("The final revision manifest contains no files")
        material = {"baseline_commit": self.state["baseline"]["commit"],
                    "revision_paths": sorted(increment["revision_paths"]), "manifest": manifest_rows}
        revision_id = "content:" + _sha(_jcs(material))
        artifact_path = f"{self.store.directory}/revision-manifest-{increment['increment_id']}.json"
        return revision_id, {**material, "revision_id": revision_id}, artifact_path

    def _finalize_revision(self, plan: dict[str, Any]) -> str:
        revision_paths = sorted({path for increment in plan["increments"]
                                 for path in increment["revision_paths"]})
        revision_id, manifest, manifest_path = self._revision_manifest({
            "increment_id": "final", "revision_paths": revision_paths,
        })
        self.store.write(manifest_path, manifest)
        if manifest_path not in self.state["artifact_references"]:
            self.state["artifact_references"].append(manifest_path)
        self.state["orchestration"]["final_revision_id"] = revision_id
        self.state["orchestration"]["revision_manifest_reference"] = manifest_path
        self._save()
        return revision_id

    def _apply_changes(self, changes: Any, increment: dict[str, Any]) -> None:
        if not isinstance(changes, list):
            raise OrchestratorError("Codex implementation result must contain a changes array")
        allowed = increment["code_paths"] + increment["artifact_paths"]
        protected_paths = {row["path"] for row in self.state["orchestration"]["preexisting_hashes"]}
        seen: set[str] = set()
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"path", "content"}:
                raise OrchestratorError("Each Codex file change must contain only path and content")
            path = _relative(change["path"], "Codex changed path")
            if path in seen or not _contained(path, allowed):
                raise OrchestratorError(f"Codex file change is duplicate or outside its allowlist: {path}")
            classification = "code" if _contained(path, increment["code_paths"]) else "artifact"
            if (classification == "code" and self.config["code_file_extensions"] and
                    Path(path).suffix.lower() not in self.config["code_file_extensions"]):
                raise OrchestratorError(f"Codex code file extension is not authorized: {path}")
            if path == self.store.directory or path.startswith(self.store.directory + "/"):
                raise OrchestratorError("Codex cannot write workflow state or harness-owned workflow artifacts")
            if path in protected_paths:
                raise OrchestratorError(f"Codex cannot modify a pre-existing user change: {path}")
            if not isinstance(change["content"], str):
                raise OrchestratorError(f"Codex file content must be UTF-8 text: {path}")
            seen.add(path)
            destination = self.target / path
            parent_rel = PurePosixPath(path).parent.as_posix()
            if parent_rel != ".":
                current = PurePosixPath()
                for component in PurePosixPath(parent_rel).parts:
                    current = current / component
                    candidate = self.target / current.as_posix()
                    if candidate.is_symlink():
                        raise OrchestratorError(f"Authorized file parent contains a symlink: {current}")
                    if candidate.exists():
                        if not candidate.is_dir():
                            raise OrchestratorError(f"Authorized file parent is not a directory: {current}")
                        continue
                    if not _contained(current.as_posix(), allowed):
                        raise OrchestratorError(
                            f"Every new parent directory must be inside an explicitly planned write scope: {current}"
                        )
                    with tempfile.TemporaryDirectory(prefix="harness-scope-") as temp:
                        cfg = self.store._config(Path(temp), self.target, current.as_posix(), classification=classification)
                        create_directories(self.target, cfg, current.as_posix())
            if destination.is_symlink():
                raise OrchestratorError(f"Refusing to write through a symlink: {path}")
            content = change["content"].encode("utf-8")
            with tempfile.TemporaryDirectory(prefix="harness-scope-") as temp:
                cfg = self.store._config(Path(temp), self.target, path, classification=classification)
                if destination.exists():
                    if not destination.is_file():
                        raise OrchestratorError(f"Authorized file destination is not a regular file: {path}")
                    replace_file(self.target, cfg, path, content, _sha(destination.read_bytes()))
                else:
                    create_file(self.target, cfg, path, content)

    def _validate(self, plan: dict[str, Any], increment: dict[str, Any], revision_id: str) -> None:
        self._set_stage("VALIDATION", "TESTING")
        validations = {item["id"]: item for item in self.config["validations"]}
        for validation_id in increment["validation_ids"]:
            item = validations[validation_id]
            cwd = self.target if item.get("cwd", ".") == "." else canonical(self.target / item["cwd"])
            if cwd != self.target and self.target not in cwd.parents:
                raise OrchestratorError("Validation working directory escaped the target")
            result = _run_command(item["argv"], cwd, item.get("timeout_seconds", self.config["stage_timeout_seconds"]), self.process_runner)
            revision_after, _manifest_after, _manifest_path = self._revision_manifest(increment)
            if revision_after != revision_id:
                raise OrchestratorError(f"Validation {validation_id} changed the reviewed product revision")
            result.update({"validation_id": validation_id, "increment_id": increment["increment_id"],
                           "revision_id": revision_id,
                           "evidence_id": f"VAL-{len(self.state['orchestration']['validation_results']) + 1:04d}"})
            result["observed_result"] = _redact(result["observed_result"])
            evidence_path = f"{self.store.directory}/evidence/{result['evidence_id']}.json"
            result["artifact_reference"] = evidence_path
            evidence = {
                "evidence_id": result["evidence_id"], "target_id": self.state["target"]["target_id"],
                "increment_id": increment["increment_id"], "kind": "validation",
                "description": f"Configured validation {validation_id}",
                "observed_at": result["finished_at"], "observed_result": f"exit_code={result['exit_code']}",
                "artifact_reference": evidence_path, "source_reference": self.workflow_id,
                "criterion_ids": increment["acceptance_criteria_ids"], "limitations": [],
                "command": shlex.join(result["argv"]), "working_directory": result["working_directory"],
                "exit_code": result["exit_code"],
                "revision": {"source_state": "working_tree", "baseline_commit": self.state["baseline"]["commit"],
                    "revision_id": revision_id, "scope_paths": increment["revision_paths"],
                    "fingerprint": _sha(_jcs({"revision_id": revision_id, "scope_paths": increment["revision_paths"]}))},
            }
            evidence_dir = self.target / f"{self.store.directory}/evidence"
            if not evidence_dir.exists():
                path = f"{self.store.directory}/evidence"
                with tempfile.TemporaryDirectory(prefix="harness-scope-") as temp:
                    cfg = self.store._config(Path(temp), self.target, path)
                    create_directories(self.target, cfg, path)
            self.store.write(evidence_path, evidence)
            if evidence_path not in self.state["artifact_references"]:
                self.state["artifact_references"].append(evidence_path)
            self.state["orchestration"]["validation_results"].append(result)
            self._save()
            if result["exit_code"] != 0:
                raise OrchestratorError(f"Required validation {validation_id} failed (exit {result['exit_code']})")
        record = next(x for x in self.state["orchestration"]["increments"] if x["increment_id"] == increment["increment_id"])
        record["status"] = "validated"
        self._save()

    def _review_and_gate(self, plan: dict[str, Any]) -> None:
        self._set_stage("REVIEW", "INDEPENDENT_VERIFICATION")
        for increment in plan["increments"]:
            record = next(row for row in self.state["orchestration"]["increments"]
                          if row["increment_id"] == increment["increment_id"])
            if record.get("status") == "reviewed":
                continue
            if not self.config["review"].get("enabled", True):
                raise OrchestratorError("Claude review is required by this orchestrator configuration")
            revision_id = record.get("revision_id")
            if not revision_id:
                raise OrchestratorError("A current increment revision manifest is required before review")
            current_id, _current_manifest, _current_path = self._revision_manifest(increment)
            if current_id != revision_id:
                raise OrchestratorError("Increment content changed after validation; rerun validations before review")
            report = self._review_increment(plan, increment, revision_id)
            if report is None:
                return
            record = next(row for row in self.state["orchestration"]["increments"]
                          if row["increment_id"] == increment["increment_id"])
            record["status"] = "reviewed"
            record["review_revision_id"] = revision_id
            self._save()
        risk = max((plan["risk_band"], self.config["minimum_risk_band"]), key=RISK.get)
        if risk == "high":
            self._waiting_for_human(
                "High-risk approval requires an externally authenticated human reviewer distinct from the implementer, "
                "and a distinct externally authenticated gate approver. Claude is supplementary and cannot fill either role.",
                authority="authenticated human reviewer and distinct final gate approver",
            )
            return
        self.state["status"] = "BLOCKED"
        self.state["phase"] = "VERIFICATION_GATE"
        self.state["orchestration"]["current_stage"] = "FINAL_GATE"
        self.state["blocker"] = {
            "reason": "The existing gate contracts require cross-record policy evaluation; this orchestrator does not mark APPROVE without a complete evaluator result.",
            "affected_phase": "VERIFICATION_GATE", "recorded_at": _now(),
            "safe_recovery_path": "Run the existing policy gate workflow on the recorded exact revision and resume after its decision is persisted.",
        }
        self._save()

    def _wait_for_claude_authorization(self, increment: dict[str, Any], reason: str,
                                       permitted_paths: list[str]) -> None:
        decision_id = f"CLAUDE-AUTH-{increment['increment_id']}"
        decision = next((row for row in self.state["pending_decisions"]
                         if row.get("decision_id") == decision_id), None)
        requested_at = _now()
        content = ("Configure review.authorization with provider aily, destination Claude through Aily, the exact "+
            "canonical repository and permitted path prefixes, read_only mode, maximum USD budget, sensitive exclusions, "+
            "validity interval, revocation state, and honest identity assurance. The authorization must cover: "+
            ", ".join(permitted_paths))
        if decision is None:
            self.state["pending_decisions"].append({
                "decision_id": decision_id, "workflow_id": self.workflow_id,
                "decision_category": "external_data_transfer_authorization", "stage": "INDEPENDENT_VERIFICATION",
                "question": "Authorize the bounded Claude through Aily read-only review for this repository and file scope?",
                "proposal": content, "reason": _redact(reason),
                "policy_or_skill_reference": "harness/config/verification-policy.yaml#authorization_matrix.approval_required.contact_external_services",
                "interactive_resolution_allowed": False, "response_history": [],
                "question_or_action": "Authorize the bounded Claude through Aily read-only review for this repository and file scope?",
                "impact": _redact(reason), "recommended_option": content, "alternatives": [],
                "required_authority": "human external data-transfer authorization",
                "affected_phase": "INDEPENDENT_VERIFICATION", "requested_at": requested_at, "status": "open",
            })
        else:
            decision["reason"] = _redact(reason)
            decision["proposal"] = content
            decision["recommended_option"] = content
            decision["status"] = "open"
            for key in ("resolution_action", "resolution", "resolved_by", "resolved_by_kind",
                        "resolved_by_identity_assurance", "resolution_authentication_reference", "resolved_at"):
                decision.pop(key, None)
        self.state["status"] = "WAITING_FOR_HUMAN"
        self.state["phase"] = "INDEPENDENT_VERIFICATION"
        self.state["orchestration"]["current_stage"] = "REVIEW"
        self._save()

    def _persistent_claude_authorization(self, permitted_paths: list[str]) -> tuple[dict[str, Any] | None, float, str | None]:
        authorization = self.state.get("orchestration", {}).get("claude_authorization")
        if not isinstance(authorization, dict):
            return None, 0.0, "No persistent Claude/Aily external data-transfer authorization is configured."
        if authorization.get("revoked") is True:
            return None, 0.0, "The persistent Claude/Aily authorization has been revoked."
        try:
            authorized_at = datetime.fromisoformat(authorization["authorized_at"].replace("Z", "+00:00"))
            expires_at = datetime.fromisoformat(authorization["expires_at"].replace("Z", "+00:00"))
            if authorized_at.tzinfo is None or expires_at.tzinfo is None:
                raise ValueError
        except (KeyError, AttributeError, TypeError, ValueError):
            return None, 0.0, "Persistent Claude/Aily authorization has invalid validity timestamps."
        now = datetime.now(timezone.utc)
        if authorized_at > now or expires_at <= now:
            return None, 0.0, "Persistent Claude/Aily authorization is not yet valid or has expired."
        if authorization.get("provider") != "aily" or authorization.get("destination") != "Claude through Aily" or authorization.get("mode") != "read_only":
            return None, 0.0, "Persistent Claude/Aily authorization does not match the required provider, destination, and read-only mode."
        repo_match = False
        for repository in authorization.get("permitted_repositories", []):
            try:
                resolved_repository = Path(repository).resolve(strict=True)
                repo_match = repo_match or (str(resolved_repository) == repository and resolved_repository == self.target)
            except (OSError, TypeError):
                continue
        if not repo_match:
            return None, 0.0, "The canonical target repository is outside the persistent Claude authorization."
        prefixes = authorization.get("permitted_paths", [])
        if any(not _contained(path, prefixes) for path in permitted_paths):
            return None, 0.0, "The review packet includes paths outside the persistent Claude authorization."
        if not CLAUDE_AUTH_EXCLUSIONS.issubset(set(authorization.get("sensitive_exclusions", []))):
            return None, 0.0, "The persistent Claude authorization does not include all required sensitive-data exclusions."
        total_limit = min(float(authorization["max_budget_usd"]), float(self.config["claude_budget_usd"]))
        spent = 0.0
        for use in self.state.get("orchestration", {}).get("claude_authorization_uses", []):
            if not isinstance(use, dict) or use.get("authorization_id") != authorization.get("authorization_id"):
                continue
            if use.get("status") == "reserved":
                return None, 0.0, "A prior persistent-authorization use was reserved without a terminal result; remaining budget cannot be verified."
            if use.get("invocation_attempted") is True:
                cost = use.get("cost_usd")
                if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(float(cost)):
                    return None, 0.0, "Prior Claude authorization usage has unknown cost; remaining budget cannot be verified."
                spent += float(cost)
        remaining = total_limit - spent
        if remaining <= 0:
            return None, 0.0, "The persistent Claude authorization budget is exhausted."
        return authorization, remaining, None

    def _waiting_for_human(self, reason: str, *, authority: str) -> None:
        self.state["status"] = "WAITING_FOR_HUMAN"
        self.state["phase"] = "INDEPENDENT_VERIFICATION"
        self.state["orchestration"]["current_stage"] = "FINAL_GATE"
        pending_id = "HUMAN-APPROVAL"
        if not any(item.get("decision_id") == pending_id and item.get("status") == "open"
                   for item in self.state["pending_decisions"]):
            self.state["pending_decisions"].append({
                "decision_id": pending_id,
                "workflow_id": self.workflow_id, "decision_category": "high_risk_approval",
                "stage": "INDEPENDENT_VERIFICATION",
                "question": f"Record the mandatory authenticated human review and distinct final gate approval for {self.state['orchestration'].get('final_revision_id')}.",
                "proposal": "Have the authenticated reviewer and distinct final gate approver inspect the exact revision.",
                "reason": reason, "policy_or_skill_reference": "harness/config/verification-policy.yaml#review",
                "interactive_resolution_allowed": False, "response_history": [],
                "question_or_action": f"Record the mandatory authenticated human review and distinct final gate approval for {self.state['orchestration'].get('final_revision_id')}.",
                "impact": reason,
                "recommended_option": "Have the required authenticated human reviewer inspect the final diff, tests, Claude report, and evidence; then have the distinct gate approver inspect the same revision and record approval.",
                "alternatives": [], "required_authority": authority,
                "affected_phase": "INDEPENDENT_VERIFICATION", "requested_at": _now(), "status": "open",
            })
        self._save()

    def _review_increment(self, plan: dict[str, Any], increment: dict[str, Any], revision_id: str,
                          *, cycle: dict[str, Any] | None = None,
                          previous_findings: list[dict[str, Any]] | None = None,
                          prior_decisions: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        if not self.aily:
            self.aily = claude.AILY_EXECUTABLE
        scope_paths = increment["revision_paths"]
        context_paths = []
        for value in self.config["review"].get("context_paths", []):
            path = _relative(value, "review context path")
            try:
                claude._safe_target_file(self.target, path)
            except (OSError, claude.ReviewError) as exc:
                raise OrchestratorError(f"Review context file is unavailable or unsafe: {path}") from exc
            context_paths.append(path)
        manifest_path = self.state["orchestration"].get("revision_manifest_reference")
        if manifest_path and manifest_path not in context_paths:
            context_paths.append(manifest_path)
        evidence_rows = [row for row in self.state["orchestration"]["validation_results"]
                         if row.get("increment_id") == increment["increment_id"] and
                         row.get("exit_code") == 0 and row.get("revision_id") == revision_id]
        if not evidence_rows:
            raise OrchestratorError("Claude review requires current successful verification evidence")
        evidence_artifacts = []
        test_evidence = []
        for row in evidence_rows:
            relative = row["artifact_reference"]
            raw = self.store.read_bytes(relative)
            evidence_artifacts.append({"evidence_id": row["evidence_id"], "artifact_reference": relative,
                                       "sha256": _sha(raw), "media_type": "application/json"})
            test_evidence.append({"evidence_id": row["evidence_id"], "command": shlex.join(row["argv"]),
                                  "exit_code": row["exit_code"], "result": row["observed_result"]})
        env = claude._git_environment()
        changed_paths = claude._changed_path_names(self.target, env, self.state["baseline"]["commit"], scope_paths)
        permitted = claude._authorized_review_paths(self.target, scope_paths, context_paths,
                                                      [row["artifact_reference"] for row in evidence_artifacts])
        authorized_input_paths = sorted(set(permitted + changed_paths))
        round_reserved = False
        if cycle is None:
            cycle_id = claude.stable_review_cycle_id(str(self.target), increment["increment_id"])
            matches = [row for row in self.state["claude_review_cycles"]
                       if row.get("cycle_id") == cycle_id]
            if len(matches) > 1:
                raise OrchestratorError("Workflow state contains duplicate Claude review cycles")
            if matches:
                cycle = copy.deepcopy(matches[0])
                rounds = cycle.get("rounds", [])
                if not rounds:
                    raise OrchestratorError("Existing Claude review cycle has no round history")
                latest = rounds[-1]
                if latest.get("status") == "failed":
                    attempt = latest.get("attempt_number")
                    auth_reference = None
                    if attempt == 2:
                        current_auth, _remaining, auth_error = self._persistent_claude_authorization(authorized_input_paths)
                        if auth_error:
                            self._wait_for_claude_authorization(increment, auth_error, authorized_input_paths)
                            return None
                        auth_reference = current_auth["authorization_reference"]
                    cycle = claude.recover_uncompleted_review_round(
                        cycle, reason="Resume after a recorded failed Claude invocation or rejected report.",
                        recovered_by="primary Codex workflow session", target_revision_id=revision_id,
                        verification_evidence_references=[row["evidence_id"] for row in evidence_rows],
                        authorization_reference=auth_reference)
                    round_reserved = True
                elif latest.get("status") == "reserved":
                    if latest.get("invocation_attempted_at"):
                        raise OrchestratorError("Claude invocation was claimed but its result is missing; do not repeat a one-shot invocation")
                    round_reserved = True
                elif latest.get("status") == "completed":
                    report_reference = latest.get("report_reference")
                    if not isinstance(report_reference, str):
                        raise OrchestratorError("Completed Claude round has no report reference")
                    report_reference = _relative(report_reference, "Claude report reference")
                    if not report_reference.startswith(self.store.directory + "/"):
                        raise OrchestratorError("Claude report is outside the workflow artifact directory")
                    report = self.store.read_json(report_reference)
                    if cycle.get("round_count") == 2:
                        finalized = claude.finalize_review_cycle(cycle)
                        self.state = claude.apply_review_cycle_to_workflow_state(self.state, finalized)
                        self._save()
                        return report if finalized["status"] == "complete" else None
                    if cycle.get("round_count") != 1:
                        raise OrchestratorError("Existing Claude review round count is inconsistent")
                    if not report.get("findings") and latest.get("reviewer_status") == "APPROVED":
                        return report
                    first_ids = {row["id"] for row in report.get("findings", [])}
                    decisions = [row for row in cycle.get("finding_decisions", [])
                                 if row.get("round_number") == 1]
                    if {row.get("finding_id") for row in decisions} == first_ids and len(decisions) == len(first_ids):
                        return self._review_increment(plan, increment, revision_id, cycle=cycle,
                            previous_findings=report["findings"], prior_decisions=decisions)
                    return self._correct_and_reverify(plan, increment, report, cycle)
                else:
                    raise OrchestratorError("Existing Claude review cycle is not resumable")
                for index, prior in enumerate(self.state["claude_review_cycles"]):
                    if prior.get("cycle_id") == cycle["cycle_id"]:
                        self.state["claude_review_cycles"][index] = cycle
                        break
                else:
                    raise OrchestratorError("Existing Claude review cycle disappeared during recovery")
                self.state = claude.apply_review_cycle_to_workflow_state(self.state, cycle)
            else:
                if any(row.get("increment_id") == increment["increment_id"]
                       for row in self.state["claude_review_cycles"]):
                    raise OrchestratorError("A prior Claude review cycle for this increment cannot be reset")
                cycle = claude.new_review_cycle(cycle_id=cycle_id, scope_kind="increment",
                    increment_id=increment["increment_id"], assignment_reference=f"task:{self.workflow_id}",
                    target_revision_id=revision_id, scope_paths=scope_paths)
        persistent_authorization, remaining_authorization_budget, authorization_error = self._persistent_claude_authorization(authorized_input_paths)
        if authorization_error:
            self._wait_for_claude_authorization(increment, authorization_error, authorized_input_paths)
            return None
        assert persistent_authorization is not None
        if not round_reserved:
            cycle = claude.reserve_review_round(cycle, target_revision_id=revision_id,
                verification_evidence_references=[row["evidence_id"] for row in evidence_rows])
            matches = [index for index, prior in enumerate(self.state["claude_review_cycles"])
                       if prior.get("cycle_id") == cycle["cycle_id"]]
            if matches:
                if len(matches) != 1:
                    raise OrchestratorError("Workflow state contains duplicate Claude review cycles")
                self.state["claude_review_cycles"][matches[0]] = cycle
            else:
                self.state["claude_review_cycles"].append(cycle)
        else:
            # Recovery transitions are persisted before the next Aily invocation.
            self._save()

        assignment = f"task:{self.workflow_id}"
        review_id = f"review-{increment['increment_id']}-{cycle['rounds'][-1]['round_number']}"
        now = datetime.now(timezone.utc)
        stamp = lambda value: value.isoformat(timespec="seconds").replace("+00:00", "Z")
        invocation_limit = min(remaining_authorization_budget, float(self.config["claude_budget_usd"]))
        persistent_auth_id = persistent_authorization["authorization_id"]
        source_reference = persistent_authorization["authorization_reference"]
        authorization = {
            "authorization_id": f"AUTH-CLAUDE-{self.workflow_id}-{increment['increment_id']}-R{cycle['rounds'][-1]['round_number']}", "operation": "contact_external_services",
            "resource": "Claude through Aily", "scope": f"Read-only review of target {self.target}, workflow {self.workflow_id}, increment {increment['increment_id']}, revision {revision_id}.",
            "target_id": self.state["target"]["target_id"], "workflow_id": self.workflow_id,
            "increment_id": increment["increment_id"], "assignment_reference": assignment,
            "review_id": review_id, "reviewer": "Claude through Aily", "reviewer_role": "Code Reviewer",
            "permitted_paths": authorized_input_paths,
            "permitted_data": ["task", "requirements", "acceptance_criteria", "reviewer_contract", "assigned_diff", "context_files", "verification_evidence", "workflow_identity", "revision_manifest"],
            "limits": [f"Persistent authorization {persistent_auth_id}; read-only review packet for exact authorized paths; no commands, writes, network tools, remote mutation, secrets, or unrelated files."],
            "authorizer_identity": persistent_authorization["authorizer_identity"],
            "authorizer_kind": "human", "authorizer_role": "Requesting user",
            "authorizer_identity_assurance": persistent_authorization["identity_assurance"],
            "authorizer_authentication_reference": persistent_authorization["authorizer_authentication_reference"],
            "authorizer_session_identity": None,
            "authority_reference": source_reference,
            "operation_effects": {"may_mutate_state": False, "may_incur_cost": True,
                                  "cost_limit": f"USD {invocation_limit:.2f} total"},
            "authorized_at": persistent_authorization["authorized_at"],
            "source_reference": source_reference,
            "expires_at": persistent_authorization["expires_at"],
            "expires_or_revocation": f"Persistent authorization {persistent_auth_id} expires at expires_at or when revoked in workflow configuration.",
            "revoked": False,
            "workflow_owner_verification": {"verified_by": "primary Codex session", "verified_at": stamp(now),
                "source_reference": source_reference, "identity_assurance": "declared", "verified_scope_sha256": ""},
        }
        authorization["workflow_owner_verification"]["verified_scope_sha256"] = claude.authorization_scope_fingerprint(authorization)
        self.state["authorizations"].append(authorization)
        self.state["authorization_verifier"]["source_reference"] = authorization["source_reference"]
        self.state["scope"]["increment_id"] = increment["increment_id"]
        self.state["scope"].update({"included_paths": increment["code_paths"] + increment["artifact_paths"],
            "code_paths": increment["code_paths"], "artifact_paths": increment["artifact_paths"],
            "revision_paths": increment["revision_paths"]})
        use = {"use_id": f"{review_id}-use", "authorization_id": persistent_auth_id,
            "review_id": review_id, "review_cycle_id": cycle["cycle_id"],
            "increment_id": increment["increment_id"], "revision_id": revision_id,
            "permitted_paths": authorized_input_paths, "mode": "read_only",
            "budget_limit_usd": invocation_limit, "invocation_attempted": False,
            "status": "reserved", "started_at": stamp(now), "finished_at": None,
            "invocation_id": None, "session_identity": None, "duration_ms": None,
            "model": None, "usage": None, "cost_usd": None}
        self.state["orchestration"].setdefault("claude_authorization_uses", []).append(use)
        self._save()

        contract_path = HARNESS_ROOT / "harness" / "verification" / "reviewer-contracts.md"
        request = {
            "target_path": str(self.target), "target_id": self.state["target"]["target_id"],
            "workflow_id": self.workflow_id, "increment_id": increment["increment_id"],
            "assignment_reference": assignment, "reviewer_role": "Code Reviewer", "review_id": review_id,
            "review_scope_kind": "increment", "target_revision_id": revision_id,
            "baseline_commit": self.state["baseline"]["commit"], "source_state": "working_tree",
            "scope_paths": scope_paths, "workflow_states_root": ".engineering/workflows",
            "workflow_state_path": self.store.state_path, "evidence_artifacts": evidence_artifacts,
            "external_authorization_reference": authorization["authorization_id"],
            "external_authorization": authorization, "task": self.task,
            "requirements": plan["requirements"], "acceptance_criteria": plan["acceptance_criteria"],
            "architecture_context": plan["architecture_context"], "context_paths": context_paths,
            "test_evidence": test_evidence, "documentation_context": plan["documentation_context"],
            "reviewer_contract_reference": "harness/verification/reviewer-contracts.md#code-reviewer",
            "reviewer_contract": contract_path.read_text(encoding="utf-8"),
            "previous_findings": previous_findings or [], "codex_finding_decisions": prior_decisions or [],
            "review_cycle": cycle, "max_budget_usd": invocation_limit,
        }
        result = claude.run_claude_review(request, aily_executable=self.aily,
            timeout_seconds=min(self.config["stage_timeout_seconds"], 900))
        process = result.get("process") if isinstance(result.get("process"), dict) else {}
        # The adapter claims the round in the target state immediately before Aily; reload that record.
        self.state = self.store.read_state()
        persisted_use = next((row for row in self.state["orchestration"].get("claude_authorization_uses", [])
                              if isinstance(row, dict) and row.get("use_id") == use["use_id"]), None)
        if persisted_use is None:
            raise OrchestratorError("Persistent Claude authorization usage record disappeared during invocation")
        process_cost = process.get("cost_usd")
        failure_kind = result.get("failure_kind")
        usage_status = ("REVIEW_COMPLETED" if result.get("report_valid") is True else
                        "REVIEW_REJECTED" if failure_kind == "invalid_reviewer_report" else "INVOCATION_FAILED")
        persisted_use.update({"invocation_attempted": bool(process.get("invocation_attempted_at")),
            "status": usage_status,
            "finished_at": _now(), "invocation_id": process.get("invocation_id") or
                ((process.get("response_metadata") or {}).get("invocation_id") if isinstance(process.get("response_metadata"), dict) else None),
            "session_identity": process.get("reviewer_session_identity"), "duration_ms": process.get("duration_ms"),
            "model": process.get("model"), "usage": process.get("usage"),
            "cost_usd": process_cost if isinstance(process_cost, (int, float)) and not isinstance(process_cost, bool) else None})
        self._save()
        if not process.get("invocation_attempted_at"):
            self.state["orchestration"]["review_results"].append({
                "increment_id": increment["increment_id"], "status": "INVOCATION_FAILED",
                "failure_kind": result.get("failure_kind"), "diagnostic": str(result.get("error") or "No invocation claim was recorded")[:1200],
                "model": process.get("model"), "usage": process.get("usage"), "cost_usd": process.get("cost_usd"),
            })
            self.state["status"] = "BLOCKED"
            self.state["blocker"] = {"reason": "Claude did not reach an auditable invocation claim; no review is recorded as completed.",
                "affected_phase": "INDEPENDENT_VERIFICATION", "recorded_at": _now(),
                "safe_recovery_path": "Inspect the recorded adapter diagnostic and resume only through the documented review-cycle recovery transition."}
            self._save()
            return None
        report_reference = None
        if result.get("report_valid") is True:
            report_reference = f"{self.store.directory}/reviews/{increment['increment_id']}-round-{cycle['rounds'][-1]['round_number']}.json"
            self.store.write(report_reference, result["report"])
            if report_reference not in self.state["artifact_references"]:
                self.state["artifact_references"].append(report_reference)
            self._save()
        evidence_reference = f"{self.store.directory}/evidence/REVIEW-{increment['increment_id']}-{cycle['rounds'][-1]['round_number']}.json"
        evidence_record = claude.build_review_evidence_record(result, request,
            evidence_id=f"REVIEW-{increment['increment_id']}-{cycle['rounds'][-1]['round_number']}",
            report_reference=report_reference)
        self.store.write(evidence_reference, evidence_record)
        if evidence_reference not in self.state["artifact_references"]:
            self.state["artifact_references"].append(evidence_reference)
        completed_cycle = claude.record_review_result(cycle, result,
            report_reference=report_reference, evidence_reference=evidence_reference,
            target_path=self.target, workflow_state_path=self.store.state_path)
        self.state = claude.apply_review_cycle_to_workflow_state(self.state, completed_cycle)
        self.state["orchestration"]["review_results"].append({
            "increment_id": increment["increment_id"], "revision_id": revision_id,
            "status": completed_cycle["status"], "outcome": completed_cycle["rounds"][-1].get("outcome"),
            "report_reference": report_reference, "evidence_reference": evidence_reference,
            "model": process.get("model"), "usage": process.get("usage"), "cost_usd": process.get("cost_usd"),
        })
        self._save()
        if not result.get("report_valid"):
            return None
        findings = result["report"]["findings"]
        if completed_cycle["round_count"] == 2:
            finalized = claude.finalize_review_cycle(completed_cycle)
            self.state = claude.apply_review_cycle_to_workflow_state(self.state, finalized)
            self._save()
            return result["report"] if finalized["status"] == "complete" else None
        if not findings:
            return result["report"]
        return self._correct_and_reverify(plan, increment, result["report"], completed_cycle)

    def _correct_and_reverify(self, plan: dict[str, Any], increment: dict[str, Any],
                              report: dict[str, Any], cycle: dict[str, Any]) -> dict[str, Any] | None:
        orchestration = self.state["orchestration"]
        if orchestration["iteration"] >= orchestration["max_iterations"]:
            orchestration["current_stage"] = "CORRECTION"
            self.state["phase"] = "CORRECTION"
            self.state["status"] = "BLOCKED"
            self.state["blocker"] = {"reason": "Claude findings were returned after the configured correction limit.",
                "affected_phase": "CORRECTION", "recorded_at": _now(),
                "safe_recovery_path": "Review the findings and authorize a new bounded workflow iteration if appropriate."}
            self._save()
            return None
        orchestration["iteration"] += 1
        self._set_stage("CORRECTION", "CORRECTION")
        with tempfile.TemporaryDirectory(prefix="orchestrator-correction-runtime-") as temp:
            runtime = Path(temp).resolve()
            runtime_manifest = _runtime_bundle_files(runtime)
            correction_context = self._implementation_context(plan, increment)
            correction_context.update({"findings": report["findings"],
                "instruction": "Triage every finding as ACCEPT, REJECT, or DEFER. Fix every accepted finding within the same allowlist; use DIFF:path evidence for rejected findings. Do not change requirements."})
            result = self._run_codex("CORRECTION", correction_context,
                runtime, "orchestrator-stage-result.schema.json", sandbox="read-only")
            if not _runtime_bundle_unchanged(runtime, runtime_manifest):
                raise OrchestratorError("Ephemeral harness tool snapshot changed during correction")
        _stage_valid(result, correction=True)
        if result["status"] != "COMPLETED":
            raise OrchestratorError(result.get("summary", "Codex correction did not complete"))
        decisions = result.get("finding_decisions")
        if not isinstance(decisions, list) or {row.get("finding_id") for row in decisions} != {f["id"] for f in report["findings"]}:
            raise OrchestratorError("Codex correction must decide every Claude finding exactly once")
        if len(decisions) != len(report["findings"]):
            raise OrchestratorError("Codex correction returned duplicate finding decisions")
        if any(row["decision"] == "DEFER" for row in decisions):
            self.state["status"] = "WAITING_FOR_HUMAN"
            self.state["phase"] = "CORRECTION"
            self.state["pending_decisions"].append({
                "decision_id": "CLAUDE-FINDING-DECISION", "question_or_action": "Resolve the deferred Claude finding before continuing.",
                "impact": "Codex could not safely determine or correct this issue within the authorized scope.",
                "recommended_option": "Provide a product/security decision for the finding.", "alternatives": [],
                "required_authority": "task owner", "affected_phase": "CORRECTION", "requested_at": _now(), "status": "open"})
            self.state["pending_decisions"][-1].update({
                "workflow_id": self.workflow_id, "decision_category": "unclassified_human_decision",
                "stage": "CORRECTION", "question": "Resolve the deferred Claude finding before continuing.",
                "proposal": "Provide a product/security decision for the finding.",
                "reason": "Codex could not safely determine or correct this issue within the authorized scope.",
                "policy_or_skill_reference": None, "interactive_resolution_allowed": False,
                "response_history": [],
            })
            self._save()
            return None
        self._apply_changes(result.get("changes"), increment)
        with tempfile.TemporaryDirectory(prefix="orchestrator-correction-self-review-") as temp:
            runtime = Path(temp).resolve()
            runtime_manifest = _runtime_bundle_files(runtime)
            self._self_review(plan, increment, runtime)
            if not _runtime_bundle_unchanged(runtime, runtime_manifest):
                raise OrchestratorError("Ephemeral harness tool snapshot changed during correction self-review")
        current_manifest_id, manifest, manifest_path = self._revision_manifest(increment)
        self.store.write(manifest_path, manifest)
        if manifest_path not in self.state["artifact_references"]:
            self.state["artifact_references"].append(manifest_path)
        increment_record = next(row for row in orchestration["increments"]
                                if row["increment_id"] == increment["increment_id"])
        increment_record["revision_id"] = current_manifest_id
        increment_record["revision_manifest_reference"] = manifest_path
        orchestration["final_revision_id"] = current_manifest_id
        orchestration["revision_manifest_reference"] = manifest_path
        self._save()
        self._validate(plan, increment, current_manifest_id)
        self._finalize_revision(plan)
        decision_rows = []
        for decision in decisions:
            finding = next(f for f in report["findings"] if f["id"] == decision["finding_id"])
            if decision["decision"] == "REJECT":
                refs = decision["evidence_references"]
                if not refs or any(not ref.startswith("DIFF:") or not _contained(
                        ref[5:].split(":", 1)[0], increment["revision_paths"]) for ref in refs):
                    raise OrchestratorError("Rejected findings require evidence scoped to the reviewed diff")
                evidence_refs = refs
                resolution_refs = []
            else:
                evidence_refs = []
                resolution_refs = [row["evidence_id"] for row in orchestration["validation_results"]
                    if row.get("increment_id") == increment["increment_id"] and row.get("revision_id") == current_manifest_id and row.get("exit_code") == 0]
                if not resolution_refs:
                    raise OrchestratorError(f"Accepted Claude finding {finding['id']} has no current successful verification evidence")
            cycle = claude.record_finding_decision(cycle, finding_id=finding["id"], decision=decision["decision"],
                reason=decision["reason"], evidence_references=evidence_refs,
                resolution_evidence_references=resolution_refs)
            decision_rows.append({"finding_id": finding["id"], "round_number": 1, "decision": decision["decision"],
                "reason": decision["reason"], "evidence_references": evidence_refs,
                "resolution_evidence_references": resolution_refs, "resolution_status": "verified" if resolution_refs else "resolved",
                "material_disagreement": False})
        self.state = claude.apply_review_cycle_to_workflow_state(self.state, cycle)
        self._save()
        # Follow-up consumes the sole remaining round and reuses the same target/increment cycle.
        return self._review_increment(plan, increment, current_manifest_id, cycle=cycle,
            previous_findings=report["findings"], prior_decisions=decision_rows)

    def _result(self) -> dict[str, Any]:
        return {"status": self.state["status"], "workflow_id": self.workflow_id,
                "stage": self.state["orchestration"]["current_stage"],
                "phase": self.state["phase"], "state_path": self.store.state_path}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target")
    parser.add_argument("--branch")
    task = parser.add_mutually_exclusive_group()
    task.add_argument("--task")
    task.add_argument("--task-file")
    parser.add_argument("--config")
    parser.add_argument("--workflow-id")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--aily", default=claude.AILY_EXECUTABLE)
    parser.add_argument("--update-requirements", action="store_true",
                        help="Append a new requirement version to an existing workflow and force replanning")
    parser.add_argument("--update-reason", help="Required auditable reason for --update-requirements")
    interaction = parser.add_mutually_exclusive_group()
    interaction.add_argument("--list-pending-decisions", action="store_true")
    interaction.add_argument("--respond-decision")
    parser.add_argument("--response-action", choices=["approve", "reject", "changes", "alternative"])
    parser.add_argument("--response")
    args = parser.parse_args(argv)
    try:
        if not args.target:
            parser.error("--target is required")
        if args.list_pending_decisions:
            if (not args.workflow_id or args.branch or args.task is not None or args.task_file or args.config or
                    args.update_requirements or args.update_reason):
                parser.error("--list-pending-decisions requires only --target and --workflow-id")
            outcome = list_pending_decisions(args.target, args.workflow_id)
            print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
            return 0
        if args.respond_decision:
            if (not args.workflow_id or not args.response_action or args.response is None or
                    args.branch or args.task is not None or args.task_file or args.config or
                    args.update_requirements or args.update_reason):
                parser.error("--respond-decision requires --workflow-id, --response-action and --response")
            action = {"approve": "APPROVE", "reject": "REJECT", "changes": "REQUEST_CHANGES",
                      "alternative": "ALTERNATIVE"}[args.response_action]
            outcome = submit_interactive_response(
                args.target, args.workflow_id, args.respond_decision, action, args.response,
                operator_identity=f"local OS account: {getpass.getuser()}",
            )
            print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
            return 0
        if (args.update_requirements != bool(args.update_reason)):
            parser.error("--update-requirements and --update-reason must be supplied together")
        if not args.branch or not args.config or (args.task is None and not args.task_file):
            parser.error("workflow execution requires --branch, exactly one of --task/--task-file, and --config")
        text = args.task if args.task is not None else Path(args.task_file).read_text(encoding="utf-8")
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        config["branch"] = args.branch
        identity = args.workflow_id or "flow-" + _sha((str(Path(args.target).resolve()) + "\0" + text).encode())[:24]
        outcome = Orchestrator(args.target, text, config, identity, codex=args.codex, aily=args.aily,
            update_requirements_reason=args.update_reason if args.update_requirements else None).run()
    except (OSError, json.JSONDecodeError, OrchestratorError, PreflightError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
    return 0 if outcome["status"] == "COMPLETED" else 3


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Orchestrate explicit Codex, validation, and Claude review stages.

Codex only plans or implements the stage assigned by this controller. The
target-local workflow-state.json remains the durable workflow record.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
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
    if not isinstance(review, dict) or set(review) - {"enabled", "context_paths"}:
        raise OrchestratorError("review allows only enabled and context_paths")
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
    return {**value, "minimum_risk_band": risk, "max_iterations": iterations,
            "stage_timeout_seconds": timeout, "validations": validations,
            "review": review, "authorized_code_paths": authorized_code_paths,
            "authorized_artifact_paths": authorized_artifact_paths,
            "code_file_extensions": extensions}


def _plan_valid(plan: Any, config: dict[str, Any], *, controller_artifact_paths: list[str] | None = None) -> dict[str, Any]:
    required = {"decision", "summary", "requirements", "acceptance_criteria", "risk_band",
                "architecture_context", "documentation_context", "capabilities_used", "increments"}
    if not isinstance(plan, dict) or set(plan) != required:
        raise OrchestratorError("Codex plan does not match the plan response contract")
    if not isinstance(plan["decision"], str) or plan["decision"] not in {"READY", "NEEDS_HUMAN_INPUT"}:
        raise OrchestratorError("Plan decision must be READY or NEEDS_HUMAN_INPUT")
    if not isinstance(plan["summary"], str) or not plan["summary"].strip():
        raise OrchestratorError("Plan summary must be a non-empty string")
    if not isinstance(plan["risk_band"], str) or plan["risk_band"] not in RISK:
        raise OrchestratorError("Plan risk_band must be low, medium, or high")
    plan["risk_band"] = max((plan["risk_band"], config["minimum_risk_band"]), key=RISK.get)
    if plan["decision"] == "NEEDS_HUMAN_INPUT":
        if any(plan[field] for field in ("requirements", "acceptance_criteria", "increments")):
            raise OrchestratorError("A blocked plan cannot include requirements, criteria, or increments")
        if any(not isinstance(plan[field], list) for field in
               ("requirements", "acceptance_criteria", "architecture_context", "documentation_context", "capabilities_used", "increments")):
            raise OrchestratorError("Plan collections must be arrays")
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
    if not isinstance(plan["capabilities_used"], list):
        raise OrchestratorError("Plan capabilities_used must be an array")
    for capability in plan["capabilities_used"]:
        required_capability = {"name", "kind", "source_reference", "phases", "use_rationale"}
        if not isinstance(capability, dict) or set(capability) - required_capability - {"limitations"} or not required_capability <= set(capability):
            raise OrchestratorError("Plan capability record has an invalid shape")
        if not isinstance(capability["kind"], str) or capability["kind"] not in {"skill", "agent_instruction", "tool_guidance", "workflow"}:
            raise OrchestratorError("Plan capability kind is unsupported")
        if any(not isinstance(capability[key], str) or not capability[key].strip()
               for key in ("name", "source_reference", "use_rationale")):
            raise OrchestratorError("Plan capability records need non-empty names and rationale")
        if not isinstance(capability["phases"], list) or not capability["phases"]:
            raise OrchestratorError("Plan capability records need applicable phases")
        if "limitations" in capability and (not isinstance(capability["limitations"], list) or
                any(not isinstance(item, str) for item in capability["limitations"])):
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
                inc[field] = sorted({_relative(path, field) for path in inc[field]})
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
        if not inc["validation_ids"] or not set(inc["validation_ids"]) <= validation_ids:
            raise OrchestratorError("Increment references an unconfigured validation")
    return plan


def _stage_valid(result: Any, *, correction: bool = False) -> dict[str, Any]:
    required = {"status", "summary", "changes"}
    permitted = required | ({"finding_decisions"} if correction else set())
    if not isinstance(result, dict) or not required <= set(result) or set(result) - permitted:
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
    if correction:
        decisions = result.get("finding_decisions")
        if result["status"] == "COMPLETED" and not isinstance(decisions, list):
            raise OrchestratorError("Codex correction response must contain finding_decisions")
        if decisions is None:
            return result
        if not isinstance(decisions, list):
            raise OrchestratorError("Codex finding_decisions must be an array")
        for item in decisions:
            if (not isinstance(item, dict) or set(item) != {"finding_id", "decision", "reason", "evidence_references"} or
                    not isinstance(item["finding_id"], str) or not item["finding_id"].strip() or
                    not isinstance(item["decision"], str) or item["decision"] not in {"ACCEPT", "REJECT", "DEFER"} or
                    not isinstance(item["reason"], str) or not item["reason"].strip() or
                    not isinstance(item["evidence_references"], list) or
                    any(not isinstance(ref, str) or not ref for ref in item["evidence_references"])):
                raise OrchestratorError("Codex finding decision does not match the correction contract")
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
    """Run the plan → implement → validate → review → correct loop."""

    def __init__(self, target: str, task: str, config: dict[str, Any], workflow_id: str,
                 *, codex: str = "codex", aily: str | None = None,
                 process_runner: Callable[..., Any] = subprocess.run) -> None:
        if not Path(target).is_absolute() or not task.strip() or not SAFE_ID.fullmatch(workflow_id):
            raise OrchestratorError("An absolute target, non-empty task, and safe workflow ID are required")
        self.target = canonical(target)
        self.task = task.strip()
        self.config = validate_config(config)
        self.workflow_id, self.codex, self.aily = workflow_id, codex, aily
        self.process_runner = process_runner
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
            baseline_hash = orchestration.get("task_sha256")
            if baseline_hash != _sha(self.task.encode()):
                raise OrchestratorError("Existing workflow belongs to different task text; it was preserved")
            if orchestration.get("config_sha256") != _sha(_json_bytes(self.config)):
                raise OrchestratorError("Existing workflow configuration changed; authorization and validation scopes are immutable on resume")
            preexisting_hashes = orchestration.get("preexisting_hashes")
            if not isinstance(preexisting_hashes, list) or not isinstance(existing.get("pending_decisions"), list):
                raise OrchestratorError("Existing workflow state has malformed baseline or pending-decision records")
            if not isinstance(orchestration.get("attempts"), list) or not isinstance(orchestration.get("increments"), list):
                raise OrchestratorError("Existing workflow state has malformed orchestration history")
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
            self.state = existing
            return
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
                "preexisting_hashes": [{"path": p, "worktree_sha256": row["worktree_sha256"], "index_sha256": row["index_sha256"],
                                         "protected_path": bool(SECRET_PATH.search(p))}
                                       for p, row in sorted(current.items())],
                "commit_authorization_fingerprint": None},
        }
        self.store.save(self.state)

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

    def _run_codex(self, stage: str, context: dict[str, Any], runtime: Path,
                   schema: str, *, sandbox: str = "workspace-write") -> dict[str, Any]:
        schema_path = HARNESS_ROOT / "harness" / "schemas" / schema
        binary = shutil.which(self.codex) if os.path.sep not in self.codex else self.codex
        if not binary or not Path(binary).is_file():
            raise OrchestratorError(f"Codex executable is unavailable: {self.codex}")
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
            + ("Analyze and plan only. Do not write any file. Return only JSON matching the schema.\n"
               if stage == "PLANNING" else
               "Implement only the current increment by returning complete UTF-8 text for changed/new files in the JSON changes array. "
               "You are read-only and must not write target files directly. Every changed path must be beneath exactly one supplied code_paths or artifact_paths entry. "
               "Do not modify workflow state, environment files, credentials, unrelated files, or Git index. Do not install, download, access network services, deploy, commit, or push. "
               "Do not return deletions or binary files. Return only JSON matching the schema.\n")
        )
        with tempfile.TemporaryDirectory(prefix="codex-stage-") as temp:
            output = Path(temp) / "result.json"
            cmd = [binary, "exec", "--json", "--ephemeral", "--sandbox", "read-only", "--cd", str(self.target),
                   "--add-dir", str(runtime), "--output-schema", str(schema_path),
                   "--output-last-message", str(output), "-"]
            started_at = _now()
            started_clock = time.monotonic()
            try:
                proc = self.process_runner(cmd, cwd=str(self.target), input=prompt.encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.config["stage_timeout_seconds"], check=False)
            except subprocess.TimeoutExpired as exc:
                self.state["orchestration"]["attempts"].append({"stage": stage, "schema": schema,
                    "started_at": started_at, "finished_at": _now(), "exit_code": 124,
                    "duration_ms": int((time.monotonic() - started_clock) * 1000), "result_sha256": None,
                    "blocker_sha256": None})
                self._save()
                raise OrchestratorError(f"Codex {stage} timed out") from exc
            raw = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else str(proc.stdout or "")
            attempt = {"stage": stage, "schema": schema, "started_at": started_at, "finished_at": _now(),
                       "exit_code": proc.returncode, "duration_ms": int((time.monotonic() - started_clock) * 1000),
                       "result_sha256": _sha(raw.encode("utf-8")) if raw else None, "blocker_sha256": None}
            self.state["orchestration"]["attempts"].append(attempt)
            self._save()
            try:
                result = json.loads(output.read_text(encoding="utf-8")) if output.exists() else json.loads(raw.strip().splitlines()[-1])
            except (OSError, json.JSONDecodeError, IndexError) as exc:
                raise OrchestratorError(f"Codex {stage} did not return a valid structured result (exit {proc.returncode})") from exc
            if proc.returncode != 0:
                raise OrchestratorError(f"Codex {stage} failed with exit {proc.returncode}")
            if not isinstance(result, dict):
                raise OrchestratorError(f"Codex {stage} response must be an object")
            return result

    def run(self) -> dict[str, Any]:
        try:
            self.initialize()
            assert self.state is not None
            if self.state["status"] == "COMPLETED":
                return self._result()
            pending = self.state.get("pending_decisions", [])
            open_decisions = [row for row in pending if row.get("status") == "open"]
            if self.state["status"] == "WAITING_FOR_HUMAN":
                if any(row.get("decision_id") in {"HUMAN-APPROVAL", "COMMIT-AUTHORIZATION"}
                       for row in pending) or open_decisions:
                    return self._result()
                if self.state["orchestration"].get("plan_reference") is not None:
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
                if prior:
                    return self._result()
                self.state["orchestration"]["attempts"].append({
                    "stage": "RESUME", "schema": None, "started_at": _now(), "finished_at": _now(),
                    "exit_code": 0, "duration_ms": 0, "result_sha256": None,
                    "blocker_sha256": blocker_hash,
                })
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
                self.state["status"] = "BLOCKED"
                self.state["blocker"] = {
                    "reason": str(exc)[:1200], "affected_phase": self.state.get("phase", "DISCOVERY"),
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
            return _plan_valid(self.store.read_json(relative), self.config,
                               controller_artifact_paths=[self.store.directory])
        self._set_stage("PLANNING", "PLANNING")
        result = self._run_codex("PLANNING", {"branch": self.config["branch"],
             "preflight": "READY", "instructions": [row["source_reference"] for row in self.state["instruction_records"]],
             "authorized_code_paths": self.config["authorized_code_paths"],
             "authorized_artifact_paths": self.config["authorized_artifact_paths"],
             "code_file_extensions": self.config["code_file_extensions"],
             "resolved_decisions": [row for row in self.state.get("pending_decisions", []) if row.get("status") == "resolved"]},
             runtime, "orchestrator-plan.schema.json", sandbox="read-only")
        if result["decision"] == "NEEDS_HUMAN_INPUT":
            self.state["status"] = "WAITING_FOR_HUMAN"
            self.state["pending_decisions"].append({
                "decision_id": "PLAN-CLARIFICATION", "question_or_action": result["summary"],
                "impact": "The orchestrator cannot define a safe implementation scope without this decision.",
                "recommended_option": "Provide the missing product or authorization detail, then resume the same workflow.",
                "alternatives": [], "required_authority": "task owner",
                "affected_phase": "PLANNING", "requested_at": _now(), "status": "open",
            })
            self._save()
            return None
        plan = _plan_valid(result, self.config)
        for item in plan["increments"]:
            item["artifact_paths"] = sorted(set(item["artifact_paths"] + [self.store.directory]))
        self.state["applied_capabilities"] = plan["capabilities_used"]
        plan_path = f"{self.store.directory}/plan.json"
        self.store.write(plan_path, plan)
        self.state["artifact_references"].append(plan_path)
        orchestration["plan_reference"] = plan_path
        orchestration["increments"] = [{**item, "status": "pending"} for item in plan["increments"]]
        self.state["phase"] = "IMPLEMENTATION"
        self._save()
        return plan

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
            result = self._run_codex("IMPLEMENTATION",
                {"increment": increment, "requirements": plan["requirements"],
                 "acceptance_criteria": plan["acceptance_criteria"]},
                runtime, "orchestrator-stage-result.schema.json", sandbox="read-only")
            _stage_valid(result)
            if result["status"] != "COMPLETED":
                raise OrchestratorError(result.get("summary", "Codex implementation did not complete"))
            self._apply_changes(result.get("changes"), increment)
        elif record["status"] != "implemented":
            raise OrchestratorError(f"Unsupported increment resume state: {record['status']}")
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
        record["status"] = "implemented"
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

    def _waiting_for_human(self, reason: str, *, authority: str) -> None:
        self.state["status"] = "WAITING_FOR_HUMAN"
        self.state["phase"] = "INDEPENDENT_VERIFICATION"
        self.state["orchestration"]["current_stage"] = "FINAL_GATE"
        pending_id = "HUMAN-APPROVAL"
        if not any(item.get("decision_id") == pending_id and item.get("status") == "open"
                   for item in self.state["pending_decisions"]):
            self.state["pending_decisions"].append({
                "decision_id": pending_id,
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
                        auth_reference = f"task-prompt:{self.workflow_id}:claude-authorization"
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
        authorization = {
            "authorization_id": f"AUTH-CLAUDE-{self.workflow_id}-{increment['increment_id']}-R{cycle['rounds'][-1]['round_number']}", "operation": "contact_external_services",
            "resource": "Claude through Aily", "scope": f"Read-only review of target {self.target}, workflow {self.workflow_id}, increment {increment['increment_id']}, revision {revision_id}.",
            "target_id": self.state["target"]["target_id"], "workflow_id": self.workflow_id,
            "increment_id": increment["increment_id"], "assignment_reference": assignment,
            "review_id": review_id, "reviewer": "Claude through Aily", "reviewer_role": "Code Reviewer",
            "permitted_paths": [],
            "permitted_data": ["task", "requirements", "acceptance_criteria", "reviewer_contract", "assigned_diff", "context_files", "verification_evidence", "workflow_identity", "revision_manifest"],
            "limits": ["Read-only review packet for the exact listed paths; no commands, writes, network tools, remote mutation, secrets, or unrelated files."],
            "authorizer_identity": "User task authorization; authenticated account identity unavailable to this runtime",
            "authorizer_kind": "human", "authorizer_role": "Requesting user",
            "authorizer_identity_assurance": "declared", "authorizer_authentication_reference": None,
            "authorizer_session_identity": None,
            "authority_reference": f"Explicit Claude/Aily authorization in task:{self.workflow_id}",
            "operation_effects": {"may_mutate_state": False, "may_incur_cost": True,
                                  "cost_limit": f"USD {float(self.config['claude_budget_usd']):.2f} total"},
            "authorized_at": stamp(now - timedelta(seconds=1)),
            "source_reference": f"task-prompt:{self.workflow_id}:claude-authorization",
            "expires_at": stamp(now + timedelta(hours=8)),
            "expires_or_revocation": "Authorization expires at expires_at or upon user revocation; checked before invocation.",
            "revoked": False,
            "workflow_owner_verification": {"verified_by": "primary Codex session", "verified_at": stamp(now),
                "source_reference": f"task-prompt:{self.workflow_id}:claude-authorization",
                "identity_assurance": "declared", "verified_scope_sha256": ""},
        }
        authorization["workflow_owner_verification"]["verified_scope_sha256"] = claude.authorization_scope_fingerprint(authorization)
        env = claude._git_environment()
        changed_paths = claude._changed_path_names(self.target, env, self.state["baseline"]["commit"], scope_paths)
        permitted = claude._authorized_review_paths(self.target, scope_paths, context_paths,
                                                      [row["artifact_reference"] for row in evidence_artifacts])
        authorization["permitted_paths"] = sorted(set(permitted + changed_paths))
        # permitted_paths is part of the authorization fingerprint; bind the attestation after exact expansion.
        authorization["workflow_owner_verification"]["verified_scope_sha256"] = claude.authorization_scope_fingerprint(authorization)
        self.state["authorizations"].append(authorization)
        self.state["authorization_verifier"]["source_reference"] = authorization["source_reference"]
        self.state["scope"]["increment_id"] = increment["increment_id"]
        self.state["scope"].update({"included_paths": increment["code_paths"] + increment["artifact_paths"],
            "code_paths": increment["code_paths"], "artifact_paths": increment["artifact_paths"],
            "revision_paths": increment["revision_paths"]})
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
            "review_cycle": cycle, "max_budget_usd": float(self.config["claude_budget_usd"]),
        }
        result = claude.run_claude_review(request, aily_executable=self.aily,
            timeout_seconds=min(self.config["stage_timeout_seconds"], 900))
        process = result.get("process") if isinstance(result.get("process"), dict) else {}
        # The adapter claims the round in the target state immediately before Aily; reload that record.
        self.state = self.store.read_state()
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
            result = self._run_codex("CORRECTION", {
                "increment": increment, "findings": report["findings"],
                "requirements": plan["requirements"], "acceptance_criteria": plan["acceptance_criteria"],
                "instruction": "Triage every finding as ACCEPT, REJECT, or DEFER. Fix every accepted finding within the same allowlist; use DIFF:path evidence for rejected findings. Do not change requirements."},
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
            self._save()
            return None
        self._apply_changes(result.get("changes"), increment)
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
    parser.add_argument("--target", required=True)
    parser.add_argument("--branch", required=True)
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--task")
    task.add_argument("--task-file")
    parser.add_argument("--config", required=True)
    parser.add_argument("--workflow-id")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--aily", default=claude.AILY_EXECUTABLE)
    args = parser.parse_args(argv)
    try:
        text = args.task if args.task is not None else Path(args.task_file).read_text(encoding="utf-8")
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        config["branch"] = args.branch
        identity = args.workflow_id or "flow-" + _sha((str(Path(args.target).resolve()) + "\0" + text).encode())[:24]
        outcome = Orchestrator(args.target, text, config, identity, codex=args.codex, aily=args.aily).run()
    except (OSError, json.JSONDecodeError, OrchestratorError, PreflightError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
    return 0 if outcome["status"] == "COMPLETED" else 3


if __name__ == "__main__":
    raise SystemExit(main())

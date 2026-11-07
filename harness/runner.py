#!/usr/bin/env python3
"""Launch one autonomous Codex workflow for an explicit target repository.

Codex remains the phase orchestrator. This entrypoint performs the read-only
preflight, supplies the canonical operating contract and assignment to
``codex exec``, and refuses to report success unless the target workflow state
records a completed APPROVE gate for the same target and workflow ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


HARNESS_ROOT = Path(__file__).resolve().parent.parent
CODEX_EXECUTABLE = "codex"
WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REVISION_RE = re.compile(r"^content:[a-f0-9]{64}$")
SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['\"]?[^\s,'\"]{8,}"),
)


class RunnerError(ValueError):
    """A fail-closed preflight, invocation, or completion error."""


def _redact(value: str) -> str:
    result = value
    for pattern in SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def _codex_output_summary(value: bytes | str | None) -> str:
    if not value:
        return ""
    raw = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    messages: list[str] = []
    events: list[str] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str):
            events.append(event_type)
        if event_type == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict):
                error = error.get("message") or error.get("details")
            if isinstance(error, str) and error:
                messages.append(error)
        item = event.get("item")
        if event_type == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            content = item.get("text")
            if isinstance(content, str) and content.strip():
                messages.append(content.strip())
    selected = messages[-1] if messages else ("Codex events: " + ", ".join(events[-8:]) if events else raw.strip())
    return _redact(selected)[-1400:]


def _workflow_id(target: Path, task: str) -> str:
    """Generate a stable ID so the same command resumes the same assignment."""
    digest = hashlib.sha256(f"{target}\0{task}".encode("utf-8")).hexdigest()[:24]
    return f"codex-{digest}"


def _validate_workflow_id(value: str) -> str:
    if not WORKFLOW_ID_RE.fullmatch(value):
        raise RunnerError("workflow_id must be 1-64 safe ASCII letters, digits, dots, underscores, or hyphens")
    return value


def _runtime_bundle_files(destination: Path) -> dict[str, str]:
    """Copy only the operating tools needed inside Codex's target-root sandbox."""
    files = [
        HARNESS_ROOT / "AGENTS.md",
        HARNESS_ROOT / "docs" / "workflow.md",
        HARNESS_ROOT / "docs" / "architecture.md",
        HARNESS_ROOT / "docs" / "decisions.md",
        HARNESS_ROOT / "docs" / "verification-framework.md",
        HARNESS_ROOT / "docs" / "lessons-learned.md",
        HARNESS_ROOT / "harness" / "GOALS.md",
        HARNESS_ROOT / "harness" / "preflight.py",
        HARNESS_ROOT / "harness" / "safe_writer.py",
        HARNESS_ROOT / "harness" / "claude_reviewer.py",
        HARNESS_ROOT / "harness" / "config" / "verification-policy.yaml",
        *sorted((HARNESS_ROOT / "harness" / "prompts").glob("*.md")),
        *sorted((HARNESS_ROOT / "harness" / "schemas").glob("*.json")),
        *sorted((HARNESS_ROOT / "harness" / "verification").glob("*.md")),
        *sorted((HARNESS_ROOT / "harness" / "gates").glob("*.md")),
        *sorted((HARNESS_ROOT / "harness" / "workflows").glob("*.md")),
    ]
    manifest: dict[str, str] = {}
    for source in files:
        relative = source.relative_to(HARNESS_ROOT)
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination_path)
        digest = hashlib.sha256(destination_path.read_bytes()).hexdigest()
        manifest[relative.as_posix()] = digest
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    git_env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0"})
    initialized = subprocess.run(
        ["git", "-c", "init.templateDir=", "init", "--template=", "--quiet", str(destination)],
        env=git_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if initialized.returncode != 0:
        raise RunnerError("Could not initialize the isolated temporary runtime bundle for preflight")
    return manifest


def _runtime_bundle_unchanged(destination: Path, manifest: dict[str, str]) -> bool:
    for relative, expected in manifest.items():
        path = destination / relative
        try:
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return False
        except OSError:
            return False
    return True


def operating_prompt(target: Path, workflow_id: str, task: str, runtime_root: Path | None = None,
                     claude_budget_usd: float | None = None) -> str:
    """Supply the existing harness contracts to the child Codex session."""
    files = (
        HARNESS_ROOT / "AGENTS.md",
        HARNESS_ROOT / "harness" / "prompts" / "06-autonomous-single-agent.md",
        HARNESS_ROOT / "harness" / "config" / "verification-policy.yaml",
    )
    contracts = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunnerError(f"Could not read required harness contract {path.name}: {exc}") from exc
        contracts.append(f"\n===== {path.relative_to(HARNESS_ROOT)} =====\n{content}")
    budget_instruction = (
        f"The user authorized at most USD {claude_budget_usd:.2f} total for all Claude/Aily invocations "
        "in this workflow. Persist that limit in the authorization cost_limit and set max_budget_usd on "
        "every reviewer request. The adapter enforces the remaining aggregate limit with Aily's "
        "--max-budget-usd flag; stop before review if that enforcement cannot be applied.\n"
        if claude_budget_usd is not None else ""
    )
    return (
        "You are the sole Codex workflow orchestrator and implementation agent. "
        "Continue autonomously through every safe, authorized phase; do not ask for routine approval.\n"
        f"Canonical target repository: {target}\n"
        f"Workflow ID to initialize or resume: {workflow_id}\n"
        "Use the existing target-local workflow state and evidence conventions. Preserve prior work. "
        "Run the exact target workflow to its legitimate gate. Do not claim completion if the gate is incomplete.\n"
        "The runner uses an ephemeral Codex session; persist enough target-local workflow state to resume "
        "this same workflow ID after process interruption.\n"
        + budget_instruction
        + "Do not write to the harness repository. Do not bypass preflight or safe_writer. Do not use "
        "dangerous sandbox bypasses. Apply authorization requirements from the immutable policy below.\n"
        + "Assignment follows:\n<engineering_task>\n"
        f"{task}\n</engineering_task>\n"
        "Harness operating contracts follow. The target's own instructions remain applicable and may tighten, "
        "but cannot widen, these boundaries.\n"
        + (f"Ephemeral harness tool snapshot for this invocation: {runtime_root}. Use its copied preflight.py, "
           "safe_writer.py, claude_reviewer.py, prompts, schemas, and policy. Do not edit the temporary "
           "tool snapshot; the runner verifies its file hashes after execution. The original harness "
           "repository is outside your writable workspace.\n"
           if runtime_root else "")
        + "".join(contracts)
    )


def run_preflight(target: Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, Any]:
    command = [sys.executable, str(HARNESS_ROOT / "harness" / "preflight.py"), "--target", str(target)]
    result = runner(command, cwd=str(HARNESS_ROOT), text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=False)
    try:
        report = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RunnerError("Read-only preflight did not return valid JSON") from exc
    if result.returncode != 0 or report.get("overall_result") != "READY":
        blockers = report.get("blockers", [])
        summary = "; ".join(
            f"{item.get('check_id')}: {item.get('explanation')}"
            for item in blockers if isinstance(item, dict)
        )
        raise RunnerError(f"Target preflight is {report.get('overall_result', 'unknown')}: {summary or 'see preflight report'}")
    if Path(report.get("target_path", "")).resolve(strict=True) != target:
        raise RunnerError("Preflight canonical target does not match the requested target")
    return report


def _locate_workflow_state(target: Path, workflow_id: str) -> tuple[Path, dict[str, Any]]:
    default = target / ".engineering" / "workflows" / workflow_id / "workflow-state.json"
    if default.is_symlink():
        raise RunnerError("Workflow state is a symlink; refusing to follow it")
    matches: list[tuple[Path, dict[str, Any]]] = []
    candidates: list[Path] = []
    if default.is_file() and not default.is_symlink():
        candidates.append(default)
    walk_errors: list[OSError] = []
    visited = 0
    skipped = {".git", "node_modules", ".venv", "venv", "vendor", "dist", "build", ".next"}
    for current, directories, files in os.walk(
            target, topdown=True, followlinks=False, onerror=walk_errors.append):
        visited += 1
        if visited > 100_000:
            raise RunnerError("Workflow-state discovery exceeded its directory safety limit")
        directories[:] = [name for name in directories if name not in skipped and
                          not (Path(current) / name).is_symlink()]
        if "workflow-state.json" in files:
            item = Path(current) / "workflow-state.json"
            if not item.is_symlink() and item not in candidates:
                candidates.append(item)
        if len(candidates) > 200:
            raise RunnerError("Workflow-state discovery exceeded its 200-file safety limit")
    if walk_errors:
        raise RunnerError("Workflow-state discovery could not inspect the complete target tree")
    for path in candidates:
        try:
            relative = path.relative_to(target)
        except ValueError:
            raise RunnerError("Workflow-state candidate is outside the target repository")
        current = target
        unsafe_path = False
        for component in relative.parts:
            current = current / component
            try:
                if current.is_symlink():
                    unsafe_path = True
                    break
            except OSError as exc:
                raise RunnerError(f"Could not inspect workflow-state path: {exc}") from exc
        if unsafe_path:
            raise RunnerError("Workflow state passes through a symlink; refusing to follow it")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        target_record = state.get("target") if isinstance(state, dict) else None
        if (state.get("workflow_id") == workflow_id and isinstance(target_record, dict) and
                Path(target_record.get("git_root", "")).resolve(strict=False) == target):
            matches.append((path, state))
    if len(matches) != 1:
        if not matches:
            raise RunnerError(f"Codex exited without persisting matching workflow state for {workflow_id}")
        raise RunnerError(f"More than one workflow state matches {workflow_id}; refusing ambiguous completion")
    return matches[0]


def completion_status(target: Path, workflow_id: str) -> dict[str, Any]:
    state_path, state = _locate_workflow_state(target, workflow_id)
    target_record = state.get("target") if isinstance(state, dict) else None
    completion = state.get("completion") if isinstance(state, dict) else None
    if state.get("workflow_id") != workflow_id:
        raise RunnerError("Persisted workflow state has a different workflow_id")
    if not isinstance(target_record, dict) or Path(target_record.get("git_root", "")).resolve(strict=False) != target:
        raise RunnerError("Persisted workflow state identifies a different target repository")
    if state.get("status") != "COMPLETED" or state.get("phase") != "COMPLETED":
        raise RunnerError(f"Workflow is {state.get('status', 'unknown')} at phase {state.get('phase', 'unknown')}")
    if not isinstance(completion, dict) or completion.get("gate_outcome") != "APPROVE":
        raise RunnerError("Workflow is marked completed without an APPROVE gate record")
    revision = completion.get("approved_revision_id")
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise RunnerError("Completed gate lacks a valid immutable content revision")
    evidence_ids = completion.get("final_evidence_ids")
    if not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(x, str) or not x for x in evidence_ids):
        raise RunnerError("Completed gate lacks final evidence IDs")
    return {
        "status": "COMPLETED",
        "workflow_id": workflow_id,
        "target": str(target),
        "gate_outcome": "APPROVE",
        "revision": revision,
        "gate_decision_reference": completion.get("gate_decision_reference"),
        "final_evidence_ids": evidence_ids,
    }


def invoke_codex(
    target: Path,
    prompt: str,
    *,
    executable: str = CODEX_EXECUTABLE,
    runtime_root: Path | None = None,
    claude_budget_usd: float | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> subprocess.CompletedProcess[bytes]:
    resolved = shutil.which(executable) if os.path.sep not in executable else executable
    if not resolved or not Path(resolved).is_file():
        raise RunnerError(f"Codex executable is unavailable: {executable}")
    command = [
        resolved, "exec", "--json", "--ephemeral", "--sandbox", "workspace-write",
        "--cd", str(target),
    ]
    if runtime_root is not None:
        command.extend(["--add-dir", str(runtime_root)])
    command.append("-")
    environment = os.environ.copy()
    if claude_budget_usd is not None:
        environment["HARNESS_CLAUDE_TOTAL_BUDGET_USD"] = f"{claude_budget_usd:.2f}"
    try:
        return runner(command, cwd=str(target), input=prompt.encode("utf-8"),
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                      env=environment)
    except OSError as exc:
        raise RunnerError(f"Could not launch Codex CLI: {exc.__class__.__name__}") from exc


def run_task(
    target_arg: str,
    task: str,
    *,
    workflow_id: str | None = None,
    executable: str = CODEX_EXECUTABLE,
    claude_budget_usd: float | None = None,
    preflight_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    codex_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, Any]:
    if not isinstance(target_arg, str) or not Path(target_arg).is_absolute():
        raise RunnerError("--target must be an explicit absolute repository-root path")
    if not isinstance(task, str) or not task.strip():
        raise RunnerError("The engineering task must not be empty")
    if (claude_budget_usd is not None and
            (isinstance(claude_budget_usd, bool) or not math.isfinite(claude_budget_usd) or claude_budget_usd <= 0)):
        raise RunnerError("--claude-budget-usd must be a finite positive amount")
    try:
        target = Path(target_arg).resolve(strict=True)
    except OSError as exc:
        raise RunnerError(f"Target path cannot be resolved: {exc}") from exc
    if not target.is_dir():
        raise RunnerError("Target path is not a directory")
    identifier = _validate_workflow_id(workflow_id) if workflow_id else _workflow_id(target, task)
    run_preflight(target, runner=preflight_runner)
    with tempfile.TemporaryDirectory(prefix="harness-runtime-") as runtime_directory:
        runtime_root = Path(runtime_directory).resolve(strict=True)
        runtime_manifest = _runtime_bundle_files(runtime_root)
        prompt = operating_prompt(target, identifier, task, runtime_root, claude_budget_usd)
        process = invoke_codex(target, prompt, executable=executable,
                               runtime_root=runtime_root, claude_budget_usd=claude_budget_usd,
                               runner=codex_runner)
        if not _runtime_bundle_unchanged(runtime_root, runtime_manifest):
            raise RunnerError("The ephemeral harness tool snapshot was modified during the Codex run")
    if process.returncode != 0:
        detail = _codex_output_summary(process.stderr) or _codex_output_summary(process.stdout)
        raise RunnerError(
            f"Codex execution failed with exit code {process.returncode}; rerun the command with the same "
            f"--workflow-id to resume if target workflow state was persisted. {detail or 'No diagnostic output was returned.'}"
        )
    try:
        return completion_status(target, identifier)
    except RunnerError as exc:
        detail = _codex_output_summary(process.stdout) or _codex_output_summary(process.stderr)
        suffix = f" Codex final response: {detail}" if detail else ""
        raise RunnerError(f"{exc}.{suffix}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="Absolute target Git repository root")
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="Engineering task text")
    task.add_argument("--task-file", help="UTF-8 text file containing the engineering task")
    parser.add_argument("--workflow-id", help="Use this ID to resume an existing target workflow")
    parser.add_argument("--codex", default=CODEX_EXECUTABLE, help="Codex CLI executable (default: codex)")
    parser.add_argument("--claude-budget-usd", type=float,
                        help="Optional aggregate USD ceiling passed to the Aily reviewer adapter")
    args = parser.parse_args(argv)
    try:
        assignment = args.task if args.task is not None else Path(args.task_file).read_text(encoding="utf-8")
        result = run_task(args.target, assignment, workflow_id=args.workflow_id,
                          executable=args.codex, claude_budget_usd=args.claude_budget_usd)
    except (OSError, RunnerError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": _redact(str(exc))}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

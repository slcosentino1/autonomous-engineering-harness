#!/usr/bin/env python3
"""Run the plan, approval, implementation, review, and delivery workflow."""

from __future__ import annotations

import argparse
import contextlib
import copy
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Callable

if os.name == "nt":
    import msvcrt
else:
    import fcntl

import claude_reviewer
from observability.tracer import (
    OBSERVABILITY_PERMITTED_DATA,
    OBSERVABILITY_SCOPE_VERSION,
    NoOpTracer,
    isolated_context,
    observability_scope,
    safe_workflow_reference,
)
from workflow import cli as _cli
from workflow import persistence as _persistence
from workflow import workspace as _workspace
from workflow.review import (
    PART_REVIEW_FIELDS,
    PART_REVIEW_SECTIONS,
    PART_REVIEW_SEVERITIES,
    PART_REVIEW_VALIDATOR_VERSION,
    CLAUDE_CODE_REVIEW_GUIDANCE,
    ReviewCampaign as _ReviewCampaign,
    ReviewCampaignError as _ReviewCampaignError,
    _safe_review_path,
    ReviewDiff,
    ReviewSegment,
    _integration_report_is_structured,
    _normalize_review_text,
    _part_coverage,
    _path_affinity,
    _review_patch_sha256,
    _review_partition_error,
    _safe_patch_boundaries,
    _validated_campaign_reports,
    parse_part_review_report,
    render_review_feedback,
    recover_review_campaign as _recover_review_campaign,
)
from workflow.workspace import (
    has_symlink_component as _has_symlink_component,
    is_sensitive as _is_sensitive,
    project_skills as _project_skills,
    read_instruction_files as _read_instruction_files,
    skill_metadata as _skill_metadata,
)


WORKFLOW_ROOT = _workspace.WORKFLOW_ROOT
WORKFLOW_LOCK_NAME_RE = _workspace.WORKFLOW_LOCK_NAME_RE
MAX_PLAN_REVIEWS = 2
DEFAULT_CLAUDE_BUDGET_USD = _cli.DEFAULT_CLAUDE_BUDGET_USD
DEFAULT_MAX_CODE_REVIEW_CALLS = _cli.DEFAULT_MAX_CODE_REVIEW_CALLS
MAX_AUTHORIZED_CODE_REVIEW_CALLS = 64
MAX_PLANNING_FAILURE_OUTPUT_CHARS = 50_000
MAX_PLANNING_FAILURE_RECORDS = 3
TRACE_STAGES = frozenset({
    "planning", "plan_review_1", "plan_review_2", "plan_refine_1", "plan_refine_2",
    "human_revision", "implementation", "code_review", "correction", "complete",
})
CODEX_EXECUTABLE = _cli.CODEX_EXECUTABLE
PLANNING_PRINCIPLES = """Engineering Judgment & Proportional Design
1. Explicit requirements take priority over technical recommendations.
2. Do not turn general best practices into requirements automatically.
3. Choose the simplest solution that satisfies the requirements.
4. Compare alternatives when justifying important architectural decisions.
5. Avoid unnecessary abstractions, services, and components.
6. Separate must implement, should implement, future production considerations, and out of scope.
7. Do not invent product behavior; ask the user when a decision changes expected behavior.
8. Make reversible technical assumptions autonomously and document them.

Understand the problem before designing:
- Identify explicit requirements, implicit constraints, business rules, inputs, outputs, and expected outcomes.
- Trace the lifecycle of the main operations, including relevant component interactions and external systems.
- Explain how the proposed components communicate and what information crosses their boundaries.

Think beyond the happy path. Consider partial failures, unexpected responses, timeouts and uncertain outcomes,
concurrent or duplicate requests, restarts and interrupted execution, invalid data, resource constraints, and
operational limits. These are analysis prompts, not automatic implementation requirements. Address the scenarios
that matter to the assignment and explain why other scenarios do not change the design.

Analyze integrations and uncertainty:
- Identify external systems, downstream dependencies, known contracts, and missing contract information.
- Explain what can be implemented independently and what depends on another team or system.
- Consider integration errors, timeouts, authentication, data contracts, and compatibility where relevant.
- State important unknowns, why they matter, and plausible interpretations and consequences.
- Separate confirmed requirements from assumptions. Never silently invent external behavior or product rules.
- Make reversible technical assumptions when safe. Ask the user about ambiguities that change product behavior,
  business rules, or an important architectural decision that cannot safely be assumed.
- Identify what remains incomplete until an external dependency is clarified.

Evaluate the architecture by its consequences. Consider maintainability, reliability, complexity, and operational
cost. Compare realistic alternatives when their consequences differ materially. Do not add infrastructure, patterns,
abstractions, or components without a clear requirement or risk-based justification. Do not turn every identified
risk into implementation work.

Plan verification from the start. Define observable outcomes and acceptance criteria for important requirements.
Identify relevant functional, integration, failure, and boundary checks. State which checks can run locally, which
need a mock or stub, and which require a real external integration.

Classify decisions as must implement, should implement, future production considerations, out of scope, open
questions or ambiguities, and external dependencies. Use only the categories that apply. Keep the proposal at the
level of detail needed to explain the reasoning; do not make it longer or more sophisticated for its own sake."""
CLAUDE_MAX_PAYLOAD_ENV = claude_reviewer.MAX_REVIEW_PAYLOAD_ENV
WORKSPACE_OPERATIONS = [
    "read_create_edit_rename_delete_assignment_files_in_target",
    "run_development_commands_builds_tests_linters_and_static_analysis",
    "install_manifest_declared_dependencies_using_repository_package_manager",
    "run_codex_planning_implementation_and_correction",
    "save_workflow_state_logs_proposals_reports_and_artifacts",
]
CODEX_STAGES = ["planning", "implementation", "correction"]
CLAUDE_DATA_SCOPE = [
    "assignment",
    "implementation_proposal",
    "implementation_and_validation_summary",
    "relevant_redacted_code_changes",
]
RESTRICTED_OPERATIONS = [
    "access_outside_target_workspace",
    "disclose_secrets",
    "commit",
    "push",
    "merge",
    "deploy",
    "publish",
    "change_shared_infrastructure",
]
OS_METADATA_NAMES = {
    ".ds_store", "thumbs.db", "ehthumbs.db", "desktop.ini", ".directory", ".appledouble",
}
KNOWN_BINARY_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"PK\x03\x04",
    b"%PDF-", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
)
SECRET_PATTERNS = claude_reviewer.SECRET_PATTERNS
SENSITIVE_NAMES = _workspace.SENSITIVE_NAMES


class WorkflowError(RuntimeError):
    """An actionable workflow error."""


def _workspace_error(call):
    try:
        return call()
    except _workspace.WorkspaceError as exc:
        raise WorkflowError(str(exc)) from exc


def _persistence_call(call):
    try:
        return call()
    except _persistence.PersistenceError as exc:
        raise WorkflowError(str(exc)) from exc


def _git_env() -> dict[str, str]:
    return _workspace.git_env()


def git(target: Path, *args: str, check: bool = True) -> bytes:
    return _workspace_error(lambda: _workspace.git(target, *args, check=check, redactor=redact))


def canonical_target(value: str) -> Path:
    return _workspace_error(lambda: _workspace.canonical_target(value, redactor=redact))


def _changed_paths(target: Path, baseline: str) -> tuple[list[str], list[str]]:
    return _workspace_error(lambda: _workspace.changed_paths(target, baseline, redactor=redact))


def _protected_paths(target: Path, baseline: str) -> list[str]:
    return _workspace_error(lambda: _workspace.protected_paths(target, baseline, redactor=redact))


def _project_guidance(target: Path, task: str) -> dict:
    return _workspace_error(lambda: _workspace.project_guidance(target, task, redactor=redact))


def _safe_artifact(path: Path) -> None:
    _persistence_call(lambda: _persistence.safe_artifact(path))


def _atomic_text(path: Path, value: str) -> None:
    _persistence_call(lambda: _persistence.atomic_text(path, value))


def _write_json(path: Path, value: dict) -> None:
    _persistence_call(lambda: _persistence.write_json(path, value))




def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def redact(value: str) -> str:
    return claude_reviewer.redact(value)


def clean_markdown(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.strip().splitlines()).strip() + "\n"


def valid_workflow_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise WorkflowError("Workflow ID must contain only letters, numbers, dots, underscores, and hyphens.")
    return value


def _check_plain_directories(root: Path, relative: str, *, create: bool = False) -> Path:
    current = root
    parts = Path(relative).parts
    for part in parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise WorkflowError(f"Workflow path contains a symlink or non-directory: {current}")
        elif create:
            try:
                current.mkdir()
            except FileExistsError:
                if current.is_symlink() or not current.is_dir():
                    raise WorkflowError(f"Workflow path contains a symlink or non-directory: {current}")
    return current


def _lock_workflow_fd(fd: int) -> None:
    if os.name == "nt":
        if os.fstat(fd).st_size == 0:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            busy_errnos = {errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", -1)}
            if exc.errno in busy_errnos:
                raise BlockingIOError(errno.EWOULDBLOCK, "workflow lock is held") from exc
            raise
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_workflow_fd(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def _workflow_process_lock(target: Path, workflow_id: str):
    """Exclusively coordinate mutating commands for one workflow across processes.

    The lock file intentionally remains after release. Ownership is determined by
    the kernel lock, never by the file's existence, so a process crash cannot leave
    a stale lock that blocks future recovery.
    """
    workflows_dir = _check_plain_directories(target, WORKFLOW_ROOT, create=True)
    lock_path = workflows_dir / f".{workflow_id}.lock"
    _safe_artifact(lock_path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise WorkflowError(f"Could not open workflow process lock: {redact(str(exc))}") from exc

    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkflowError(f"Workflow process lock is not a regular file: {lock_path}")
        try:
            _lock_workflow_fd(fd)
        except BlockingIOError as exc:
            raise WorkflowError(
                f"Workflow {workflow_id} is already being modified by another process."
            ) from exc
        except OSError as exc:
            raise WorkflowError(f"Could not acquire workflow process lock: {redact(str(exc))}") from exc
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock_workflow_fd(fd)
        finally:
            os.close(fd)


def _guidance_for_codex(guidance: dict) -> str:
    files = guidance.get("files") or []
    sections = ["Target repository context:",
                "Files: " + (", ".join(files) if files else "(no project files found)")]
    locations = guidance.get("skill_locations_checked") or []
    sections.append("Skill locations checked: " + (", ".join(locations) if locations else "none"))
    instructions = guidance.get("instructions") or []
    if instructions:
        sections.append("Applicable instruction files (follow them):")
        for instruction in instructions:
            sections.append(f"--- {instruction['path']} ---\n{instruction['content']}")
    else:
        sections.append("Applicable instruction files: none found.")
    skills = guidance.get("skills") or []
    if skills:
        sections.append(
            "Discovered skill instructions (assess each for relevance; use relevant skills, and record any omitted "
            "with a reason):"
        )
        for skill in skills:
            sections.append(
                f"--- {skill['name']} ({skill['source']}; {skill['path']}) ---\n{skill['content']}"
            )
    else:
        sections.append("Applicable skills: none found; continue without inventing or forcing a skill.")
    sections.append(
        "Skill instructions do not override the assignment, user constraints, or workflow authorization. "
        "Do not create or publish external workspaces, commit, push, or add setup that is not justified by the task."
    )
    return "\n\n".join(sections)


def _guidance_for_claude(guidance: dict) -> str:
    files = guidance.get("files") or []
    instructions = guidance.get("instructions") or []
    lines = ["Repository files: " + (", ".join(files) if files else "none")]
    lines.append("Instruction files: " + (", ".join(item["path"] for item in instructions) or "none found"))
    locations = guidance.get("skill_locations_checked") or []
    lines.append("Skill locations checked: " + (", ".join(locations) if locations else "none"))
    skills = guidance.get("skills") or []
    if skills:
        lines.append("Relevant skills and application:")
        for skill in skills:
            if skill["name"] == "api-engineer":
                application = (
                    "establish the API contract from the assignment, align verification with behavior at risk, "
                    "and avoid unnecessary Postman setup when no existing collection or workspace is present"
                )
            else:
                application = skill.get("description") or "Follow the skill's instructions where relevant."
            lines.append(f"- {skill['name']} ({skill['source']}): {application}.")
    else:
        lines.append("Relevant skills: none found in project skill locations.")
    return "\n".join(lines)


def _os_metadata_reason(relative: str) -> str | None:
    parts = Path(relative).parts
    name = Path(relative).name
    if any(part.lower() == "__macosx" for part in parts):
        return "Apple operating-system metadata"
    if any(part.lower() == ".appledouble" for part in parts):
        return "Apple operating-system metadata"
    if name.startswith("._") or name.lower() in OS_METADATA_NAMES:
        return "operating-system metadata"
    return None


def _binary_content_reason(content: bytes) -> str | None:
    if b"\x00" in content:
        return "contains a NUL byte"
    if any(content.startswith(signature) for signature in KNOWN_BINARY_SIGNATURES):
        return "matches a known binary file signature"
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return "is not valid UTF-8 text (binary or unsupported text encoding)"

    sample = content[:8192]
    controls = sum((byte < 32 and byte not in (9, 10, 12, 13)) or byte == 127 for byte in sample)
    if controls and controls / len(sample) > 0.10:
        return "contains a high proportion of non-text control bytes"
    return None


def _baseline_blob(target: Path, baseline: str, relative: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(target), "show", f"{baseline}:{relative}"],
        cwd=target,
        env=_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _review_diff(state: dict, target: Path) -> ReviewDiff:
    tracked, untracked = _changed_paths(target, state["baseline_commit"])
    protected = set(state.get("preexisting_paths", []))
    workflow_prefix = WORKFLOW_ROOT + "/"
    chunks: list[str] = []
    file_patches: list[tuple[str, str]] = []
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    tracked_paths = set(tracked)

    for relative in sorted(set(tracked) | set(untracked)):
        reason = _os_metadata_reason(relative)
        if reason is None and relative in protected:
            reason = "pre-existing target change is protected from review"
        if reason is None and relative.startswith(workflow_prefix):
            reason = "workflow state is not part of the implementation diff"
        if reason is None and _is_sensitive(relative):
            reason = "sensitive path is excluded from external review"
        if reason is None and ".git" in Path(relative).parts:
            reason = "Git metadata is not part of the implementation diff"
        if reason is None and _has_symlink_component(target, relative):
            reason = "symlink paths are excluded from external review"
        if reason is not None:
            excluded.append((relative, reason))
            continue

        path = target / relative
        if path.is_symlink():
            excluded.append((relative, "symlink files are excluded from external review"))
            continue
        working_content: bytes | None = None
        try:
            if path.exists():
                if not path.is_file():
                    excluded.append((relative, "changed path is not a regular file"))
                    continue
                working_content = path.read_bytes()
        except OSError:
            excluded.append((relative, "working-tree content could not be read"))
            continue

        previous_content = (
            _baseline_blob(target, state["baseline_commit"], relative) if relative in tracked_paths else None
        )
        if working_content is None and previous_content is None:
            excluded.append((relative, "changed file content is unavailable in both the working tree and baseline"))
            continue

        binary_reason = _binary_content_reason(working_content) if working_content is not None else None
        if binary_reason is None and previous_content is not None:
            binary_reason = _binary_content_reason(previous_content)
            if binary_reason is not None:
                binary_reason = f"baseline version {binary_reason}"
        if binary_reason is not None:
            excluded.append((relative, binary_reason))
            continue

        try:
            relative.encode("utf-8")
            if relative in tracked_paths:
                raw = git(
                    target, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                    state["baseline_commit"], "--", relative,
                )
                text = raw.decode("utf-8")
                if "\x00" in text:
                    excluded.append((relative, "generated diff contains a NUL byte"))
                    continue
                patch = redact(text)
            else:
                text = working_content.decode("utf-8")
                source_path = json.dumps(f"a/{relative}", ensure_ascii=True)
                destination_path = json.dumps(f"b/{relative}", ensure_ascii=True)
                content_lines = text.splitlines(keepends=True)
                if text and not text.endswith(("\n", "\r")):
                    content_lines[-1] += "\n"
                hunk = "".join(f"+{line}" for line in content_lines)
                patch = (
                    f"diff --git {source_path} {destination_path}\nnew file mode 100644\n"
                    f"--- /dev/null\n+++ {destination_path}\n"
                    f"@@ -0,0 +1,{len(content_lines)} @@\n{hunk}"
                )
                patch = redact(patch)
            chunks.append(patch)
            file_patches.append((relative, patch))
        except UnicodeDecodeError:
            excluded.append((relative, "generated diff is not valid UTF-8 text"))
            continue
        except UnicodeEncodeError:
            excluded.append((relative, "path cannot be safely encoded as UTF-8"))
            continue
        included.append(relative)

    discovered = set(tracked) | set(untracked)
    accounted_for = set(included) | {path for path, _reason in excluded}
    if discovered != accounted_for:
        missing = sorted(discovered - accounted_for)
        raise WorkflowError(
            "Review diff file accounting is incomplete; changed paths were not included or recorded as excluded: "
            + ", ".join(json.dumps(path, ensure_ascii=True) for path in missing)
        )

    result = "\n".join(chunks)
    return ReviewDiff(
        result if result.strip() else "No reviewable changes were found.",
        tuple(included),
        tuple(excluded),
        tuple(file_patches),
    )


def _format_context_prompt(header: str, sections: dict[str, str]) -> str:
    blocks = [header.strip()]
    for name, value in sections.items():
        content = value if name == "Complete patch" else value.strip()
        blocks.append(f"## {name}\n{content}")
    return "\n\n".join(blocks).strip()


def _sha256_text(value: str) -> str | None:
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None


def _json_line_message(stdout: str) -> str:
    messages: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            messages.append(item["text"])
        elif isinstance(event, dict) and event.get("type") == "message" and isinstance(event.get("message"), str):
            messages.append(event["message"])
    return messages[-1].strip() if messages else ""


def build_codex_command(
    executable: str, target: Path, sandbox: str, output_path: Path, model: str | None = None,
) -> list[str]:
    """Build the installed CLI's supported command; approval policy is a root option."""
    command = [
        executable, "--ask-for-approval", "never", "exec", "--json", "--ephemeral", "--disable", "plugins",
        "-c", "mcp_servers={}", "--sandbox", sandbox, "--cd", str(target),
        "--output-last-message", str(output_path),
    ]
    if model is not None:
        command.extend(["--model", model])
    command.append("-")
    return command


def _clearly_no_review_findings(report: str) -> bool:
    approved = re.search(r"(?im)^\s*Decision:\s*APPROVED\b", report)
    changes_requested = re.search(r"(?im)^\s*Decision:\s*CHANGES_REQUESTED\b", report)
    empty = re.search(
        r"(?i)\b(no\s+(?:actionable\s+|material\s+|code\s+)?(?:findings|issues|defects)|"
        r"nothing to change|findings?\s*:\s*(?:none|no))\b", report,
    )
    if changes_requested:
        return False
    if approved:
        blocking_severity = re.search(
            r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?Severity(?:\*\*)?\s*:\s*(?:\*\*)?Must fix\b|"
            r"^\s*(?:#+\s*)?Must fix finding\b", report,
        )
        return not blocking_severity
    return bool(empty and not re.search(r"(?i)\bMust fix\b", report))


def new_workflow_authorization(
    target: Path, workflow_id: str, budget_usd: float,
    max_code_review_calls: int = DEFAULT_MAX_CODE_REVIEW_CALLS,
    tracing_enabled: bool = False,
) -> dict:
    """Create the single persistent authorization attached to a new workflow."""
    if (isinstance(max_code_review_calls, bool) or not isinstance(max_code_review_calls, int)
            or not 1 <= max_code_review_calls <= MAX_AUTHORIZED_CODE_REVIEW_CALLS):
        raise WorkflowError(
            f"Code review call authorization must be between 1 and {MAX_AUTHORIZED_CODE_REVIEW_CALLS}."
        )
    authorization = {
        "version": 1,
        "authorization_id": uuid.uuid4().hex,
        "created_at": now(),
        "target": str(target),
        "workflow_id": workflow_id,
        "workspace_operations": WORKSPACE_OPERATIONS.copy(),
        "codex_stages": CODEX_STAGES.copy(),
        "claude_aily": {
            "provider": "Claude through Aily",
            "permitted_data": CLAUDE_DATA_SCOPE.copy(),
            "max_plan_reviews": MAX_PLAN_REVIEWS,
            "max_code_reviews": max_code_review_calls,
            "code_review_policy": {
                "mode": "adaptive_multi_part",
                "max_calls": max_code_review_calls,
            },
            "budget_usd": float(budget_usd),
            "spent_usd": 0.0,
            "budget_unknown": False,
            "active_call": None,
        },
        "observability": observability_scope(tracing_enabled),
        "restricted_operations": RESTRICTED_OPERATIONS.copy(),
    }
    authorization["scope_sha256"] = authorization_scope_digest(authorization)
    return authorization


def authorization_scope_digest(authorization: dict) -> str:
    """Hash authorization and spend state so scope or budget edits are detected on resume."""
    terms = {key: value for key, value in authorization.items() if key != "scope_sha256"}
    encoded = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_workflow_authorization(state: dict, target: Path, workflow_id: str) -> dict:
    """Reject missing, mismatched, or broadened saved authorization."""
    authorization = state.get("authorization")
    if not isinstance(authorization, dict) or authorization.get("version") != 1:
        raise WorkflowError("Workflow has no supported persistent authorization; start a newly authorized workflow.")
    if authorization.get("target") != str(target) or authorization.get("workflow_id") != workflow_id:
        raise WorkflowError("Saved authorization belongs to a different target or workflow.")
    if not isinstance(authorization.get("authorization_id"), str) or not authorization["authorization_id"]:
        raise WorkflowError("Saved workflow authorization has no ID.")
    if authorization.get("workspace_operations") != WORKSPACE_OPERATIONS:
        raise WorkflowError("Saved workspace authorization is missing, changed, or broader than the supported scope.")
    if authorization.get("codex_stages") != CODEX_STAGES:
        raise WorkflowError("Saved Codex authorization is missing, changed, or broader than the supported stages.")
    if authorization.get("restricted_operations") != RESTRICTED_OPERATIONS:
        raise WorkflowError("Saved protected-operation boundaries are missing or changed.")
    observability = authorization.get("observability")
    if observability is not None:
        enabled = observability.get("enabled") if isinstance(observability, dict) else None
        endpoint_hash = observability.get("langfuse_base_url_sha256") if isinstance(observability, dict) else None
        valid_endpoint_hash = (
            isinstance(endpoint_hash, str) and re.fullmatch(r"[a-f0-9]{64}", endpoint_hash)
            if enabled is True else endpoint_hash is None
        )
        if (not isinstance(observability, dict)
                or set(observability) != {"version", "enabled", "permitted_data", "langfuse_base_url_sha256"}
                or type(observability.get("version")) is not int
                or observability.get("version") != OBSERVABILITY_SCOPE_VERSION
                or type(enabled) is not bool
                or observability.get("permitted_data") != OBSERVABILITY_PERMITTED_DATA
                or not valid_endpoint_hash):
            raise WorkflowError("Saved observability scope is invalid or changed.")
    external = authorization.get("claude_aily")
    if not isinstance(external, dict):
        raise WorkflowError("Saved Claude/Aily authorization is missing.")
    code_review_limit = external.get("max_code_reviews")
    policy = external.get("code_review_policy")
    legacy_single_review = (
        policy is None and isinstance(code_review_limit, int)
        and not isinstance(code_review_limit, bool) and code_review_limit == 1
    )
    configured_review_policy = (
        isinstance(policy, dict)
        and policy.get("mode") == "adaptive_multi_part"
        and policy.get("max_calls") == code_review_limit
        and isinstance(code_review_limit, int)
        and not isinstance(code_review_limit, bool)
        and 1 <= code_review_limit <= MAX_AUTHORIZED_CODE_REVIEW_CALLS
    )
    if (external.get("provider") != "Claude through Aily"
            or external.get("permitted_data") != CLAUDE_DATA_SCOPE
            or external.get("max_plan_reviews") != MAX_PLAN_REVIEWS
            or not (legacy_single_review or configured_review_policy)):
        raise WorkflowError("Saved Claude/Aily scope or review limit is missing or changed.")
    try:
        budget = Decimal(str(external.get("budget_usd")))
        spent = Decimal(str(external.get("spent_usd")))
    except Exception as exc:
        raise WorkflowError("Saved Claude/Aily budget is invalid.") from exc
    if (not budget.is_finite() or budget < Decimal("0.01")
            or budget != budget.quantize(Decimal("0.01"))
            or not spent.is_finite() or spent < 0):
        raise WorkflowError("Saved Claude/Aily budget or spending is invalid.")
    if authorization.get("scope_sha256") != authorization_scope_digest(authorization):
        raise WorkflowError("Saved workflow authorization changed after initialization.")
    return authorization


class Orchestrator:
    """A single-run stage controller with small, target-local JSON state."""

    def __init__(
        self,
        target: str | Path,
        *,
        task: str | None = None,
        budget_usd: float | None = DEFAULT_CLAUDE_BUDGET_USD,
        max_code_review_calls: int = DEFAULT_MAX_CODE_REVIEW_CALLS,
        model: str | None = None,
        workflow_id: str | None = None,
        codex: str = CODEX_EXECUTABLE,
        aily: str = claude_reviewer.AILY_EXECUTABLE,
        timeout_seconds: int = 3600,
        process_runner: Callable = subprocess.run,
        claude_runner: Callable = claude_reviewer.review,
        tracing_enabled: bool = False,
    ):
        if (isinstance(max_code_review_calls, bool) or not isinstance(max_code_review_calls, int)
                or not 1 <= max_code_review_calls <= MAX_AUTHORIZED_CODE_REVIEW_CALLS):
            raise WorkflowError(
                f"Code review call authorization must be between 1 and {MAX_AUTHORIZED_CODE_REVIEW_CALLS}."
            )
        self.target = canonical_target(str(target))
        self.task = task
        self.budget_usd = budget_usd
        self.max_code_review_calls = max_code_review_calls
        if model is not None and not model.strip():
            raise WorkflowError("--model must name a Codex model when provided.")
        self.model = model.strip() if model is not None else None
        self.workflow_id = valid_workflow_id(workflow_id or self._new_workflow_id())
        self.codex = codex
        self.aily = aily
        self.timeout_seconds = timeout_seconds
        self.process_runner = process_runner
        self.claude_runner = claude_runner
        self.tracing_enabled = bool(tracing_enabled)
        self._tracer = None
        self._active_trace_context = None
        self._trace_started_at: float | None = None
        self.workflow_dir = _check_plain_directories(self.target, f"{WORKFLOW_ROOT}/{self.workflow_id}")
        self.state_path = self.workflow_dir / "state.json"
        self.proposal_path = self.workflow_dir / "proposal.md"
        self.delivery_path = self.workflow_dir / "delivery.md"
        self._process_lock_held = False

    @staticmethod
    def _new_workflow_id() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]

    def _save(self) -> None:
        if not self._process_lock_held:
            raise WorkflowError("Workflow state writes require the workflow process lock.")
        self.state["updated_at"] = now()
        authorization = self.state.get("authorization")
        if isinstance(authorization, dict):
            authorization["scope_sha256"] = authorization_scope_digest(authorization)
        _write_json(self.state_path, self.state)

    def _record_unvalidated_planning_output(
        self, output: str, error: WorkflowError, *, stage: str, proposal_version: int,
    ) -> None:
        """Keep bounded, redacted diagnostics without making output approval eligible."""
        safe_output = redact(output)
        records = self.state.setdefault("planning_validation_failures", [])
        records.append({
            "validation_status": "failed_unvalidated",
            "approval_eligible": False,
            "stage": stage,
            "proposal_version": proposal_version,
            "attempted_at": now(),
            "validation_error": redact(str(error)),
            "generated_output": safe_output[:MAX_PLANNING_FAILURE_OUTPUT_CHARS],
            "generated_output_characters": len(safe_output),
            "generated_output_sha256": hashlib.sha256(safe_output.encode("utf-8")).hexdigest(),
            "generated_output_truncated": len(safe_output) > MAX_PLANNING_FAILURE_OUTPUT_CHARS,
            "secrets_redacted": safe_output != output,
        })
        if len(records) > MAX_PLANNING_FAILURE_RECORDS:
            del records[:-MAX_PLANNING_FAILURE_RECORDS]
            self.state["planning_validation_failure_history_truncated"] = True
        self._save()

    @contextlib.contextmanager
    def _workflow_lock(self):
        if self._process_lock_held:
            raise WorkflowError("Workflow process locks cannot be nested.")
        with _workflow_process_lock(self.target, self.workflow_id):
            self._process_lock_held = True
            try:
                yield
            finally:
                self._process_lock_held = False

    def _read_state(self) -> dict:
        """Read and validate persisted state without recovery or disk writes."""
        _safe_artifact(self.state_path)
        try:
            self.state = _persistence.read_json(self.state_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"Workflow state is missing or invalid: {self.state_path}") from exc
        if self.state.get("workflow_id") != self.workflow_id or self.state.get("target") != str(self.target):
            raise WorkflowError("Workflow state belongs to another target or workflow ID.")
        saved_model = self.state.get("codex_model")
        if saved_model is not None and (not isinstance(saved_model, str) or not saved_model.strip()):
            raise WorkflowError("Saved Codex model configuration is invalid.")
        if self.model is not None and self.model != saved_model:
            raise WorkflowError("Codex model is fixed when the workflow starts.")
        self.model = saved_model

        return validate_workflow_authorization(self.state, self.target, self.workflow_id)

    def _load(self) -> None:
        """Load state for a locked mutation, recovering only a truly ownerless call."""
        if not self._process_lock_held:
            raise WorkflowError("Workflow state recovery requires the workflow process lock.")
        authorization = self._read_state()
        external = authorization["claude_aily"]
        if external.get("active_call"):
            external["budget_unknown"] = True
            external["active_call"] = None
            self.state["error"] = "A prior Claude call ended without a recorded result; further paid reviews are stopped."
            self.state["status"] = "failed"
            self._save()

    def _read_proposal(self) -> str:
        _safe_artifact(self.proposal_path)
        try:
            proposal = _persistence.read_text(self.proposal_path)
        except OSError as exc:
            raise WorkflowError(f"Saved proposal is unavailable: {self.proposal_path}") from exc
        if not proposal.strip():
            raise WorkflowError("Saved proposal is empty.")
        return clean_markdown(proposal)

    def start(self) -> dict:
        return self._execute_locked(self._start_locked)

    def _start_locked(self) -> dict:
        if not self.task or not self.task.strip():
            raise WorkflowError("A non-empty task is required to start a workflow.")
        if self.workflow_dir.exists() or self.workflow_dir.is_symlink():
            raise WorkflowError(f"Workflow already exists; use resume: {self.workflow_id}")
        try:
            budget = Decimal(str(self.budget_usd if self.budget_usd is not None else DEFAULT_CLAUDE_BUDGET_USD))
        except Exception as exc:
            raise WorkflowError("--budget-usd must be a finite amount with at most two decimal places.") from exc
        if not budget.is_finite() or budget < Decimal("0.01"):
            raise WorkflowError("--budget-usd must be at least 0.01.")
        if budget != budget.quantize(Decimal("0.01")):
            raise WorkflowError("--budget-usd must use at most two decimal places.")
        baseline = git(self.target, "rev-parse", "HEAD").decode("ascii", "strict").strip()
        self.workflow_dir = _check_plain_directories(self.target, WORKFLOW_ROOT, create=True) / self.workflow_id
        if self.workflow_dir.exists() or self.workflow_dir.is_symlink():
            raise WorkflowError(f"Workflow already exists: {self.workflow_id}")
        self.workflow_dir.mkdir()
        self.state_path = self.workflow_dir / "state.json"
        self.proposal_path = self.workflow_dir / "proposal.md"
        self.delivery_path = self.workflow_dir / "delivery.md"
        self.state = {
            "version": 1,
            "workflow_id": self.workflow_id,
            "target": str(self.target),
            "task": self.task.strip(),
            "created_at": now(),
            "updated_at": now(),
            "status": "running",
            "stage": "planning",
            "codex_model": self.model,
            "baseline_commit": baseline,
            "preexisting_paths": _protected_paths(self.target, baseline),
            "project_guidance": _project_guidance(self.target, self.task.strip()),
            "proposal_version": 0,
            "planning_validation_failures": [],
            "planning_validation_failure_history_truncated": False,
            "authorization": new_workflow_authorization(
                self.target, self.workflow_id, float(budget), self.max_code_review_calls,
                tracing_enabled=self.tracing_enabled,
            ),
            "plan_reviews": [],
            "claude_contexts": [],
            "plan_review_decisions": [],
            "proposal": None,
            "implementation_approved_at": None,
            "implementation_summary": None,
            "review_included_files": [],
            "review_exclusions": [],
            "review_files_not_reviewed": [],
            "code_review": None,
            "correction_summary": None,
            "error": None,
        }
        self._save()
        self._begin_execution_trace("start")
        return self.run_pending()

    def approve(self) -> dict:
        return self._execute_locked(self._approve_locked)

    def _approve_locked(self) -> dict:
        self._load()
        if self.state.get("status") != "awaiting_approval":
            raise WorkflowError("This workflow is not waiting for implementation approval.")
        current_proposal = self._read_proposal()
        if current_proposal != clean_markdown(self.state.get("proposal", "")):
            raise WorkflowError("The proposal file changed after review; use revise to review the change before approval.")
        self._begin_execution_trace("approve")
        self.state["implementation_approved_at"] = {"time": now(), "method": "explicit approve command"}
        self.state["status"] = "running"
        self.state["stage"] = "implementation"
        self.state["error"] = None
        self._save()
        return self.run_pending()

    def revise(self, feedback: str) -> dict:
        return self._execute_locked(lambda: self._revise_locked(feedback))

    def _revise_locked(self, feedback: str) -> dict:
        self._load()
        if self.state.get("status") != "awaiting_approval":
            raise WorkflowError("This workflow is not waiting for proposal feedback.")
        if not feedback.strip():
            raise WorkflowError("Proposal feedback must not be empty.")
        self._begin_execution_trace("revise")
        self._invalidate_unverified_plan_reviews()
        self.state["proposal"] = self._read_proposal()
        self.state["proposal_version"] = max(1, int(self.state.get("proposal_version", 1)))
        self.state["project_guidance"] = _project_guidance(self.target, self.state["task"])
        self.state["human_feedback"] = feedback.strip()
        self.state["status"] = "running"
        self.state["stage"] = "human_revision"
        self.state["error"] = None
        self._save()
        return self.run_pending()

    def _valid_plan_reviews(self) -> list[dict]:
        return [
            record for record in self.state.get("plan_reviews", [])
            if record.get("valid") is True and isinstance(record.get("context_manifest"), dict)
        ]

    def _invalidate_unverified_plan_reviews(self) -> None:
        """Keep legacy review evidence, but do not count calls with no verifiable context."""
        for record in self.state.get("plan_reviews", []):
            if record.get("valid") is True and isinstance(record.get("context_manifest"), dict):
                continue
            record["valid"] = False
            record["invalidated_reason"] = (
                "Legacy review has no context manifest. The previous redaction helper truncated prompts at "
                "1,200 characters, so completeness cannot be established."
            )
            record["codex_disposition"] = (
                "Invalid review attempt; do not count its findings. Reassess the current proposal using complete context."
            )

    def _guidance(self) -> dict:
        return self.state.get("project_guidance") or _project_guidance(self.target, self.state["task"])

    def reject(self, reason: str) -> dict:
        return self._execute_locked(lambda: self._reject_locked(reason))

    def _reject_locked(self, reason: str) -> dict:
        self._load()
        if self.state.get("status") != "awaiting_approval":
            raise WorkflowError("This workflow is not waiting for a proposal decision.")
        self._begin_execution_trace("reject")
        self.state["status"] = "rejected"
        self.state["rejection_reason"] = reason.strip() or "Rejected by the user."
        self.state["completed_at"] = now()
        self._save()
        return self.state

    def resume(self) -> dict:
        return self._execute_locked(self._resume_locked)

    def _resume_locked(self) -> dict:
        self._load()
        if self.state.get("status") == "awaiting_approval":
            return self.state
        if self.state.get("status") == "complete":
            return self.state
        if self.state.get("status") != "failed":
            raise WorkflowError(f"Workflow cannot resume from status {self.state.get('status')!r}.")
        authorization = validate_workflow_authorization(self.state, self.target, self.workflow_id)
        if (authorization["claude_aily"]["budget_unknown"]
                and self.state.get("stage", "").startswith(("plan_review", "code_review"))):
            raise WorkflowError(self.state.get("error") or "Claude review stopped because spend is unknown.")
        self._begin_execution_trace("resume")
        self.state["status"] = "running"
        self.state["error"] = None
        self._save()
        return self.run_pending()

    def _execute_locked(self, operation: Callable[[], dict]) -> dict:
        try:
            with self._workflow_lock():
                result = operation()
        except BaseException as exc:
            self._finish_execution_trace(exc)
            raise
        self._finish_execution_trace(None)
        return result

    def _begin_execution_trace(self, command: str) -> None:
        authorization = self.state.get("authorization", {})
        scope = authorization.get("observability", {})
        if scope.get("enabled") is not True:
            return
        try:
            from observability.langfuse import create_langfuse_tracer

            self._tracer = create_langfuse_tracer(scope)
            workflow_ref = safe_workflow_reference(self.workflow_id)
            initial_stage = self.state.get("stage")
            if not isinstance(initial_stage, str) or initial_stage not in TRACE_STAGES:
                initial_stage = "unknown"
            self._trace_started_at = time.monotonic()
            self._active_trace_context = isolated_context(lambda: self._tracer.execution(
                name="workflow.execution",
                session_id=workflow_ref,
                metadata={
                    "command": command,
                    "workflow_ref": workflow_ref,
                    "initial_stage": initial_stage,
                },
            ))
            self._active_trace_context.__enter__()
        except Exception:
            self._tracer = None
            self._active_trace_context = None
            self._trace_started_at = None
            print("Tracing warning: could not initialize tracing; continuing without it.", file=sys.stderr)

    def _trace_span(self, name: str, metadata: dict):
        tracer = self._tracer or NoOpTracer()
        return isolated_context(lambda: tracer.span(name=name, metadata=metadata))

    def _finish_execution_trace(self, error: BaseException | None) -> None:
        context = self._active_trace_context
        tracer = self._tracer
        self._active_trace_context = None
        self._tracer = None
        started_at = self._trace_started_at
        self._trace_started_at = None
        if context is None:
            return
        final_stage = self.state.get("stage") if isinstance(self.state, dict) else None
        final_status = self.state.get("status") if isinstance(self.state, dict) else None
        if not isinstance(final_stage, str) or final_stage not in TRACE_STAGES:
            final_stage = "unknown"
        if final_status not in {"running", "awaiting_approval", "failed", "complete", "rejected"}:
            final_status = "unknown"
        duration = max(0, round((time.monotonic() - started_at) * 1000)) if started_at is not None else 0
        context.update(
            final_stage=final_stage,
            final_status=final_status,
            duration_ms=duration,
            execution_result="success" if error is None else "failure",
            **({"error_category": type(error).__name__} if error is not None else {}),
        )
        context.__exit__(type(error) if error is not None else None, error, error.__traceback__ if error else None)
        try:
            tracer.flush()
        except Exception:
            print("Tracing warning: export failed; workflow result is unchanged.", file=sys.stderr)

    def status(self) -> dict:
        self._read_state()
        return self.state

    def _run_codex(self, prompt: str, *, sandbox: str) -> str:
        metadata = {"provider": "Codex", "operation": "codex_execution", "sandbox": sandbox}
        if self.model:
            metadata["model"] = self.model
        with self._trace_span("provider.codex", metadata):
            return self._run_codex_impl(prompt, sandbox=sandbox)

    def _run_codex_impl(self, prompt: str, *, sandbox: str) -> str:
        with tempfile.TemporaryDirectory(prefix="harness-codex-result-") as output_dir:
            output_path = Path(output_dir) / "last-message.md"
            authorization = self.state["authorization"]
            authorization_context = (
                "Persistent workflow authorization is already active for target "
                f"{authorization['target']}. It covers: {', '.join(authorization['workspace_operations'])}. "
                "Do not request individual approval for those operations. It does not cover: "
                f"{', '.join(authorization['restricted_operations'])}. Do not perform those actions without separate "
                "explicit user authorization. The sandbox for this stage still applies; planning is read-only.\n\n"
            )
            command = build_codex_command(self.codex, self.target, sandbox, output_path, self.model)
            try:
                result = self.process_runner(
                    command,
                    cwd=str(self.target),
                    input=(authorization_context + prompt).encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=_git_env(),
                )
            except subprocess.TimeoutExpired as exc:
                raise WorkflowError(f"Codex timed out after {self.timeout_seconds} seconds.") from exc
            except OSError as exc:
                raise WorkflowError(f"Codex could not start: {redact(str(exc))}") from exc
            stdout = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else str(result.stdout or "")
            stderr = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else str(result.stderr or "")
            if result.returncode:
                detail = redact(stderr.strip() or _json_line_message(stdout) or "Codex returned no diagnostic output.")
                raise WorkflowError(f"Codex exited with status {result.returncode}: {detail}")
            if output_path.exists():
                message = output_path.read_text(encoding="utf-8").strip()
            else:
                message = _json_line_message(stdout) or stdout.strip()
            if not message:
                raise WorkflowError("Codex returned an empty response.")
            return message

    def _call_claude(
        self,
        header: str,
        stage: str,
        sections: dict[str, str],
        *,
        metadata: dict | None = None,
    ) -> tuple[str, dict]:
        operation_metadata = {
            "provider": "Claude_Aily",
            "operation": "plan_review" if stage.startswith("plan_review_") else "code_review",
        }
        if stage.startswith("plan_review_"):
            operation_metadata["iteration"] = int(stage[-1])
        if stage == "code_review" and isinstance(metadata, dict):
            retry_count = metadata.get("retry_count")
            if isinstance(retry_count, int) and not isinstance(retry_count, bool) and retry_count >= 0:
                operation_metadata["retry_count"] = retry_count
            if metadata.get("review_kind") == "integration":
                operation_metadata["review_kind"] = "integration"
            else:
                operation_metadata["review_kind"] = "part"
                match = re.fullmatch(r"part-(\d+)", str(metadata.get("part_id", "")))
                if match:
                    operation_metadata["review_part_index"] = int(match.group(1))
        with self._trace_span("provider.claude_review", operation_metadata) as span:
            report, manifest = self._call_claude_impl(header, stage, sections, metadata=metadata)
            cost = manifest.get("cost_usd") if isinstance(manifest, dict) else None
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
                span.update(reported_cost_usd=float(cost))
            attempt = manifest.get("campaign_call_number") if isinstance(manifest, dict) else None
            if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
                span.update(attempt=attempt)
            return report, manifest

    def _call_claude_impl(
        self,
        header: str,
        stage: str,
        sections: dict[str, str],
        *,
        metadata: dict | None = None,
    ) -> tuple[str, dict]:
        if not self._process_lock_held:
            raise WorkflowError("Claude review requires the workflow process lock.")
        authorization = validate_workflow_authorization(self.state, self.target, self.workflow_id)
        external = authorization["claude_aily"]
        if external["budget_unknown"]:
            raise WorkflowError("Claude review stopped because a previous call has no recorded cost.")
        if external["permitted_data"] != CLAUDE_DATA_SCOPE:
            raise WorkflowError("This Claude/Aily request is outside the workflow's persisted data authorization.")
        if stage.startswith("plan_review_") and len(self._valid_plan_reviews()) >= external["max_plan_reviews"]:
            raise WorkflowError("The authorized Claude plan-review limit is exhausted.")
        code_review_call_number = None
        if stage == "code_review":
            code_review_calls = [
                context for context in self.state.get("claude_contexts", [])
                if context.get("stage") == "code_review" and context.get("call_state") != "validation_failed"
            ]
            if len(code_review_calls) >= external["max_code_reviews"]:
                raise WorkflowError(
                    "The authorized Claude code-review campaign call limit is exhausted "
                    f"({external['max_code_reviews']} calls)."
                )
            code_review_call_number = len(code_review_calls) + 1

        code_required = {
            "Review metadata", "Assignment", "Approved proposal", "Implementation summary",
            "Repository context", "Relevant skills", "Independent reviewer role",
            "Repository/module map and dependencies", "Engineering Judgment & Proportional Design",
            "Review file coverage",
        }
        if stage == "code_review":
            code_required.add(
                "Individual part reports" if (metadata or {}).get("review_kind") == "integration"
                else "Complete patch"
            )

        required_sections = {
            "plan_review": {
                "Review metadata", "Assignment", "Current proposal", "Repository context",
                "Workflow constraints", "Engineering Judgment & Proportional Design", "Relevant skills",
                "Review duties", "Review history and Codex decisions",
            },
        }["plan_review"] if stage != "code_review" else code_required
        safe_header = redact(header)
        safe_sections = {
            name: redact(value) if name == "Complete patch" else redact(value).strip()
            for name, value in sections.items()
        }
        prompt = _format_context_prompt(safe_header, safe_sections)
        missing = sorted(name for name in required_sections if not safe_sections.get(name, "").strip())
        validation_errors: list[str] = []
        if missing:
            validation_errors.append(
                "Claude review context is incomplete; missing mandatory sections: " + ", ".join(missing) + "."
            )
        if stage.startswith("plan_review_") and not all(
                isinstance((metadata or {}).get(key), int) for key in ("iteration", "proposal_version")):
            validation_errors.append("Claude plan-review metadata must include the review iteration and proposal version.")
        if stage == "code_review" and not isinstance((metadata or {}).get("proposal_version"), int):
            validation_errors.append("Claude code-review metadata must include the proposal version.")
        for name in required_sections:
            value = safe_sections.get(name)
            block = f"## {name}\n{value}".rstrip() if value is not None else ""
            if not block or block not in prompt:
                validation_errors.append(f"Claude review context section {name!r} is not present in the payload.")

        try:
            configured_payload_limit = claude_reviewer.max_review_payload_chars()
            requested_payload_limit = (metadata or {}).get("payload_limit_chars")
            if requested_payload_limit is not None and (
                    isinstance(requested_payload_limit, bool) or not isinstance(requested_payload_limit, int)
                    or requested_payload_limit < 1):
                raise claude_reviewer.ReviewError("Saved code-review payload limit is invalid.")
            if requested_payload_limit is not None and configured_payload_limit < requested_payload_limit:
                raise claude_reviewer.ReviewError(
                    f"Current {CLAUDE_MAX_PAYLOAD_ENV} limit ({configured_payload_limit}) is below the "
                    f"saved review campaign limit ({requested_payload_limit}); no paid call was made."
                )
            payload_limit = requested_payload_limit or configured_payload_limit
        except claude_reviewer.ReviewError as exc:
            payload_limit = None
            validation_errors.append(str(exc))
        if payload_limit is not None:
            try:
                claude_reviewer.validate_review_invocation(prompt, self.aily, limit=payload_limit)
            except claude_reviewer.ReviewError as exc:
                validation_errors.append(str(exc))

        prompt_bytes: bytes | None
        try:
            prompt_bytes = prompt.encode("utf-8")
        except UnicodeEncodeError:
            prompt_bytes = None

        included_files = list((metadata or {}).get("included_files", []))
        excluded_files = list((metadata or {}).get("excluded_files", []))
        section_manifest = [
            {
                "name": name,
                "characters": len(value),
                "sha256": _sha256_text(value),
            }
            for name, value in safe_sections.items()
        ]

        def context_manifest(call_state: str, error: str | None = None) -> dict:
            manifest = {
                "stage": stage,
                "iteration": (metadata or {}).get("iteration"),
                "proposal_version": (metadata or {}).get("proposal_version"),
                "header_id": (metadata or {}).get("header_id", "claude-review-v1"),
                "header_characters": len(safe_header),
                "header_sha256": _sha256_text(safe_header),
                "characters": len(prompt),
                "utf8_bytes": len(prompt_bytes) if prompt_bytes is not None else None,
                "payload_limit_characters": payload_limit,
                "payload_limit_source": "environment" if CLAUDE_MAX_PAYLOAD_ENV in os.environ else "default",
                "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest() if prompt_bytes is not None else None,
                "complete": not missing and not validation_errors,
                "sections": section_manifest,
                "included_files": included_files,
                "excluded_files": excluded_files,
                "call_state": call_state,
            }
            for key in (
                "campaign_id", "review_kind", "part_id", "shared_context_sha256", "part_patch_sha256",
            ):
                if (metadata or {}).get(key) is not None:
                    manifest[key] = (metadata or {})[key]
            if code_review_call_number is not None:
                manifest["campaign_call_number"] = code_review_call_number
            if error is not None:
                manifest["error"] = redact(error)
            return manifest

        if validation_errors:
            detail = " ".join(validation_errors)
            contexts = self.state.setdefault("claude_contexts", [])
            contexts.append(context_manifest("validation_failed", detail))
            self.state["error"] = detail
            self._save()
            raise WorkflowError(detail + " No paid call was made.")

        assert payload_limit is not None and prompt_bytes is not None
        print(
            f"Claude {stage} context: {len(prompt)} characters ({len(prompt_bytes)} UTF-8 bytes); "
            f"configured limit {payload_limit} characters.",
            file=sys.stderr,
        )

        remaining_amount = Decimal(str(external["budget_usd"])) - Decimal(str(external["spent_usd"]))
        remaining = float(remaining_amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        if remaining < 0.01:
            raise WorkflowError("The authorized Claude budget is exhausted.")

        manifest = context_manifest("submitted")
        manifest["submitted_at"] = now()
        contexts = self.state.setdefault("claude_contexts", [])
        context_index = len(contexts)
        contexts.append(manifest)
        external["active_call"] = {
            "stage": stage,
            "started_at": now(),
            "cap_usd": remaining,
            "context_sha256": manifest["prompt_sha256"],
        }
        self._save()
        try:
            result = self.claude_runner(prompt, budget_usd=remaining, executable=self.aily)
        except claude_reviewer.ReviewError as exc:
            if exc.cost_usd is None:
                external["budget_unknown"] = True
            else:
                external["spent_usd"] = round(external["spent_usd"] + exc.cost_usd, 4)
            external["active_call"] = None
            self.state["error"] = str(exc)
            contexts[context_index]["call_state"] = "failed"
            contexts[context_index]["error"] = redact(str(exc))
            self._save()
            raise WorkflowError(str(exc)) from exc
        except Exception as exc:
            external["budget_unknown"] = True
            external["active_call"] = None
            error = f"Claude invocation failed without recorded cost: {redact(str(exc))}"
            self.state["error"] = error
            contexts[context_index]["call_state"] = "failed_unknown_cost"
            contexts[context_index]["error"] = error
            self._save()
            raise WorkflowError(error) from exc
        cost = result.cost_usd
        if cost is not None:
            external["spent_usd"] = round(external["spent_usd"] + cost, 4)
        else:
            external["budget_unknown"] = True
        external["active_call"] = None
        contexts[context_index]["call_state"] = "completed"
        contexts[context_index]["cost_usd"] = cost
        if stage == "code_review":
            contexts[context_index]["report"] = result.text
        self._save()
        return result.text, manifest

    def _code_review_shared_context(self, review_diff: ReviewDiff) -> dict[str, str]:
        guidance = self._guidance()
        directory_counts: dict[str, int] = {}
        contract_paths: list[str] = []
        for path in review_diff.included_paths:
            parent = str(Path(path).parent)
            directory = parent if parent != "." else "(repository root)"
            directory_counts[directory] = directory_counts.get(directory, 0) + 1
            if re.search(r"(?i)(api|schema|contract|interface|openapi|proto|event)", Path(path).name):
                contract_paths.append(path)
        module_lines = ["Modules are inferred from changed directory structure; directory names are clues, not architecture rules."]
        module_lines.extend(f"- `{path}`: {count} changed text file(s)" for path, count in sorted(directory_counts.items()))
        module_lines.append(
            "Changed contract/interface/schema candidates: "
            + (", ".join(f"`{path}`" for path in sorted(contract_paths)) if contract_paths else "none detected by filename")
        )
        module_lines.append(
            "Use the assignment, approved proposal, implementation summary, repository instructions, and individual "
            "reports to assess dependencies. Do not infer dependency direction from folder names alone."
        )
        return {
            "Assignment": self.state["task"],
            "Approved proposal": self.state["proposal"],
            "Implementation summary": self.state["implementation_summary"],
            "Repository context": self._repository_context_for_claude(
                guidance, list(review_diff.included_paths),
            ),
            "Relevant skills": _guidance_for_claude(guidance),
            "Repository/module map and dependencies": "\n".join(module_lines),
            "Independent reviewer role": CLAUDE_CODE_REVIEW_GUIDANCE,
            "Engineering Judgment & Proportional Design": PLANNING_PRINCIPLES,
        }


    def _review_campaign(self) -> _ReviewCampaign:
        authorization = self.state.get("authorization", {}).get("claude_aily", {})
        return _ReviewCampaign(
            self.state,
            save_state=self._save,
            request_review=self._call_claude,
            authorized_call_limit=authorization.get("max_code_reviews"),
            authorize_call_count=self._review_call_count_authorized,
            trace_span=self._trace_span,
        )

    def _review_call_count_authorized(self, required_calls: int) -> bool:
        limit = self.state["authorization"]["claude_aily"]["max_code_reviews"]
        return required_calls <= limit

    @staticmethod
    def _campaign_helper(call):
        try:
            return call()
        except _ReviewCampaignError as exc:
            raise WorkflowError(str(exc)) from exc

    @staticmethod
    def _code_review_header() -> str:
        return _ReviewCampaign._code_review_header()

    @staticmethod
    def _legacy_code_review_header() -> str:
        return _ReviewCampaign._legacy_code_review_header()

    @staticmethod
    def _review_segment(
        path: str, patch: str, start: int = 1, end: int | None = None, total: int | None = None,
    ) -> ReviewSegment:
        return _ReviewCampaign._review_segment(path, patch, start, end, total)

    @staticmethod
    def _part_coverage(segments: list[ReviewSegment], *, excluded_count: int) -> str:
        return _ReviewCampaign._part_coverage(segments, excluded_count=excluded_count)

    def _part_sections(
        self, shared: dict[str, str], segments: list[ReviewSegment], *, campaign_id: str,
        part_id: str, excluded_count: int,
    ) -> dict[str, str]:
        return self._campaign_helper(lambda: self._review_campaign()._part_sections(
            shared, segments, campaign_id=campaign_id, part_id=part_id, excluded_count=excluded_count,
        ))

    @staticmethod
    def _serialized_review_fits(header: str, sections: dict[str, str], limit: int) -> bool:
        return Orchestrator._campaign_helper(
            lambda: _ReviewCampaign._serialized_review_fits(header, sections, limit),
        )

    def _split_oversized_segment(self, *args, **kwargs) -> list[ReviewSegment]:
        return self._campaign_helper(lambda: self._review_campaign()._split_oversized_segment(*args, **kwargs))

    def _partition_review_segments(self, *args, **kwargs) -> tuple[list[dict], bool]:
        return self._campaign_helper(lambda: self._review_campaign()._partition_review_segments(*args, **kwargs))

    @staticmethod
    def _validate_review_partition(review_diff: ReviewDiff, parts: list[dict]) -> None:
        Orchestrator._campaign_helper(lambda: _ReviewCampaign._validate_review_partition(review_diff, parts))

    @staticmethod
    def _campaign_source_hash(review_diff: ReviewDiff) -> str:
        return _ReviewCampaign._campaign_source_hash(review_diff)

    @staticmethod
    def _campaign_part_manifest(parts: list[dict]) -> list[dict]:
        return _ReviewCampaign._campaign_part_manifest(parts)

    def _prepare_review_campaign(self, review_diff: ReviewDiff) -> tuple[dict, dict[str, str], list[dict]]:
        shared = self._code_review_shared_context(review_diff)
        return self._campaign_helper(
            lambda: self._review_campaign()._prepare_review_campaign(review_diff, shared),
        )

    def _sync_review_coverage(self, campaign: dict, review_diff: ReviewDiff) -> None:
        self._campaign_helper(lambda: self._review_campaign()._sync_review_coverage(campaign, review_diff))

    @staticmethod
    def _part_report_validation(report: str, part_id: str, segments: list[ReviewSegment]) -> dict:
        return _ReviewCampaign._part_report_validation(report, part_id, segments)

    @staticmethod
    def _part_report_is_structured(report: str, part_id: str, segments: list[ReviewSegment]) -> bool:
        return _ReviewCampaign._part_report_is_structured(report, part_id, segments)

    @staticmethod
    def _integration_report_is_structured(report: str) -> bool:
        return _ReviewCampaign._integration_report_is_structured(report)

    def _run_review_campaign(
        self, campaign: dict, shared: dict[str, str], parts: list[dict], review_diff: ReviewDiff,
    ) -> str:
        with self._trace_span("review.campaign", {"review_part_count": len(parts)}):
            return self._campaign_helper(lambda: self._review_campaign()._run_review_campaign(
                campaign, shared, parts, review_diff,
            ))

    def _set_stage(self, stage: str) -> None:
        self.state["stage"] = stage
        self.state["status"] = "running"
        self.state["error"] = None
        self._save()

    def _fail(self, exc: Exception) -> None:
        self.state["status"] = "failed"
        self.state["error"] = redact(str(exc))
        self._save()

    def run_pending(self) -> dict:
        if not self._process_lock_held:
            raise WorkflowError("Workflow execution requires the workflow process lock.")
        while self.state.get("status") == "running":
            stage = self.state["stage"]
            stage_label = stage if isinstance(stage, str) and stage in TRACE_STAGES else "unknown"
            stage_started = time.monotonic()
            stage_context = self._trace_span("workflow.stage", {"stage": stage_label})
            stage_context.__enter__()
            try:
                if stage == "planning":
                    guidance = self._guidance()
                    prompt = (
                        "Understand the assignment and this repository. Use the applicable instruction and skill "
                        "content supplied below; inspect relevant target files. Do not merely list skills: apply "
                        "relevant guidance and record each selected skill and its use. If none apply, say so. "
                        "Make reasonable technical decisions, identify requirements, edge cases, and verification. "
                        "Apply Engineering Judgment & Proportional Design. Separate must implement, should implement, "
                        "future production considerations, and out of scope. Compare alternatives for important "
                        "architectural choices and do not invent product behavior. "
                        "Return a concise, readable Markdown implementation proposal with scope, approach, risks, "
                        "a requirements-to-design mapping, relevant skills and how they were applied, and concrete "
                        "validation commands. Do not modify files or run tests.\n\n"
                        f"{_guidance_for_codex(guidance)}\n\n"
                        f"{PLANNING_PRINCIPLES}\n\nTarget: {self.target}\n\nAssignment:\n{self.state['task']}"
                    )
                    proposal = clean_markdown(self._run_codex(prompt, sandbox="read-only"))
                    self.state["proposal"] = proposal
                    self.state["proposal_version"] = 1
                    _atomic_text(self.proposal_path, proposal)
                    self.state["stage"] = "plan_review_1"
                    self._save()
                elif stage in {"plan_review_1", "plan_review_2"}:
                    iteration = int(stage[-1])
                    header, sections, metadata = self._plan_review_prompt(iteration)
                    report, manifest = self._call_claude(header, stage, sections, metadata=metadata)
                    self.state["plan_reviews"].append({
                        "attempt": len(self.state.get("plan_reviews", [])) + 1,
                        "iteration": iteration,
                        "proposal_version": metadata["proposal_version"],
                        "report": report,
                        "time": now(),
                        "valid": True,
                        "context_manifest": manifest,
                    })
                    self.state["stage"] = f"plan_refine_{iteration}"
                    self._save()
                elif stage in {"plan_refine_1", "plan_refine_2"}:
                    iteration = int(stage[-1])
                    review = next((record for record in reversed(self.state.get("plan_reviews", []))
                                   if record.get("valid") is True and record.get("iteration") == iteration), None)
                    if review is None:
                        raise WorkflowError(f"No valid Claude plan review exists for iteration {iteration}.")
                    report = review["report"]
                    guidance = self._guidance()
                    prompt = (
                        "Evaluate every distinct finding in this valid Claude plan-review report. For each, explicitly "
                        "mark incorporated, rejected, or deferred and give a reason. Incorporate valid in-scope "
                        "recommendations; do not turn optional production advice into assignment requirements. "
                        "Return the complete refined proposal as readable Markdown, retaining requirements mapping, "
                        "scope distinctions, and the relevant skills and their application. Put a section titled "
                        "`## Claude finding dispositions` at the end of the proposal. Use one bullet for every finding "
                        "in this format: `- Finding: ... | Decision: incorporated/rejected/deferred | Reason: ...`. "
                        "If there are none, write `- No actionable findings.` Apply Engineering Judgment & "
                        "Proportional Design. Add one final "
                        "line exactly in this form: `Second plan review needed: yes` or `...: no`. Use yes only when "
                        "a material plan change needs a second independent plan review.\n\n"
                        f"{PLANNING_PRINCIPLES}\n\n{_guidance_for_codex(guidance)}\n\n"
                        f"Assignment:\n{self.state['task']}\n\nCurrent proposal (version "
                        f"{self.state.get('proposal_version', 1)}):\n{self.state['proposal']}\n\n"
                        f"Claude review (iteration {iteration}, proposal version {review.get('proposal_version')}):\n{report}"
                    )
                    refined = self._run_codex(prompt, sandbox="read-only")
                    next_proposal_version = int(self.state.get("proposal_version", 1)) + 1
                    try:
                        proposal, review_again, dispositions = self._refined_proposal(refined)
                    except WorkflowError as exc:
                        self._record_unvalidated_planning_output(
                            refined, exc, stage=stage, proposal_version=next_proposal_version,
                        )
                        raise
                    self.state["proposal"] = proposal
                    self.state["proposal_version"] = next_proposal_version
                    self.state.setdefault("plan_review_decisions", []).append({
                        "iteration": iteration,
                        "proposal_version": self.state["proposal_version"],
                        "dispositions": dispositions,
                    })
                    _atomic_text(self.proposal_path, proposal)
                    if iteration == 1 and review_again:
                        self.state["stage"] = "plan_review_2"
                    else:
                        self.state["status"] = "awaiting_approval"
                        self.state["stage"] = "implementation"
                    self._save()
                elif stage == "human_revision":
                    guidance = self._guidance()
                    prompt = (
                        "Revise the saved proposal to address the user's explicit feedback. Keep the original "
                        "assignment and unrelated decisions intact. Apply Engineering Judgment & Proportional Design. "
                        "Apply relevant skill instructions below, and record the skill name and how it informed the "
                        "proposal; if there are no applicable skills, state that. Distinguish must implement, should "
                        "implement, future production considerations, and out of scope. Return the complete proposal "
                        "in readable Markdown; do not implement code.\n\n"
                        f"{PLANNING_PRINCIPLES}\n\n{_guidance_for_codex(guidance)}\n\n"
                        f"Assignment:\n{self.state['task']}\n\nCurrent proposal (version "
                        f"{self.state.get('proposal_version', 1)}):\n{self.state['proposal']}\n\n"
                        f"Prior review attempts:\n{self._review_history_for_prompt()}\n\n"
                        f"User feedback:\n{self.state['human_feedback']}"
                    )
                    self.state["proposal"] = clean_markdown(self._run_codex(prompt, sandbox="read-only"))
                    self.state["proposal_version"] = int(self.state.get("proposal_version", 1)) + 1
                    _atomic_text(self.proposal_path, self.state["proposal"])
                    authorized_plan_limit = self.state["authorization"]["claude_aily"]["max_plan_reviews"]
                    valid_reviews = self._valid_plan_reviews()
                    if len(valid_reviews) < authorized_plan_limit:
                        self.state["stage"] = f"plan_review_{len(valid_reviews) + 1}"
                    else:
                        self.state["status"] = "awaiting_approval"
                        self.state["stage"] = "implementation"
                    self._save()
                elif stage == "implementation":
                    if not self.state.get("implementation_approved_at"):
                        self.state["status"] = "awaiting_approval"
                        self._save()
                        break
                    guidance = self._guidance()
                    prompt = (
                        "Implement the approved proposal in this repository. Follow repository instructions. "
                        "Apply the relevant skill instructions supplied below; do not merely list skills. "
                        "Use the persistent workflow authorization for ordinary target-workspace operations without "
                        "individual approvals. Install dependencies only from project manifests using the project's "
                        "package manager; do not use arbitrary download URLs or custom registries. Run relevant checks. "
                        "Do not expose secrets or read unrelated repositories. Commit, push, merge, deploy, publish, "
                        "shared-infrastructure changes, and access outside the target require separate explicit "
                        "user authorization and are not included in this workflow authorization. "
                        "Do not delete unrelated existing files. Follow Engineering Judgment & Proportional Design "
                        "and do not invent product behavior. Return a concise summary with files changed, "
                        "commands actually run, results, and known limitations.\n\n"
                        f"{PLANNING_PRINCIPLES}\n\n{_guidance_for_codex(guidance)}\n\n"
                        f"Assignment:\n{self.state['task']}\n\nApproved proposal:\n{self.state['proposal']}"
                    )
                    self.state["implementation_summary"] = self._run_codex(prompt, sandbox="workspace-write")
                    self.state["stage"] = "code_review"
                    self._save()
                elif stage == "code_review":
                    review_diff = _review_diff(self.state, self.target)
                    if not review_diff.included_paths:
                        self.state["review_included_files"] = []
                        self.state["review_exclusions"] = [
                            {"path": _safe_review_path(path), "reason": reason}
                            for path, reason in review_diff.excluded_files
                        ]
                        self.state["review_files_not_reviewed"] = []
                        self._save()
                        reasons = "; ".join(
                            f"{path}: {reason}" for path, reason in review_diff.excluded_files
                        )
                        detail = f"No reviewable text changes were found. Excluded changed files: {reasons or 'none'}"
                        raise WorkflowError(detail)
                    try:
                        campaign, shared, parts = self._prepare_review_campaign(review_diff)
                    except WorkflowError as exc:
                        if not self.state.get("review_campaign"):
                            self.state["review_included_files"] = list(review_diff.included_paths)
                            self.state["review_exclusions"] = [
                                {"path": _safe_review_path(path), "reason": reason}
                                for path, reason in review_diff.excluded_files
                            ]
                            self.state["review_files_not_reviewed"] = [
                                {"path": path, "reason": redact(str(exc))}
                                for path in review_diff.included_paths
                            ]
                            self._save()
                        raise
                    self.state["code_review"] = self._run_review_campaign(
                        campaign, shared, parts, review_diff,
                    )
                    self.state["code_review_context_sha256"] = campaign.get("shared_context_sha256")
                    review_evidence = "\n\n".join([
                        self.state["code_review"],
                        *(record.get("report", "") for record in campaign.get("part_reviews", [])),
                    ])
                    if _clearly_no_review_findings(review_evidence):
                        self.state["correction_summary"] = (
                            "Claude reported no acceptance-blocking findings; no correction run was needed."
                        )
                        self.state["stage"] = "complete"
                    else:
                        self.state["stage"] = "correction"
                    self._save()
                elif stage == "correction":
                    campaign = self.state.get("review_campaign") or {}
                    individual_reports = "\n\n".join(
                        f"### {record.get('part_id')}\n{record.get('report', '')}"
                        for record in campaign.get("part_reviews", []) if record.get("report")
                    ) or "No separate individual reports were recorded."
                    prompt = (
                        "Evaluate the independent Claude code review. Fix valid in-scope defects, reject incorrect "
                        "recommendations with a brief reason, and assess every distinct finding from individual part "
                        "reports as well as the consolidated integration report. Optional improvements are not "
                        "mandatory. Rerun tests/checks affected by any fixes. Do not "
                        "make unrelated changes or repeat the review. Return a concise correction and validation "
                        "summary.\n\n"
                        f"Assignment:\n{self.state['task']}\n\nApproved proposal:\n{self.state['proposal']}\n\n"
                        f"Claude code review:\n{self.state['code_review']}\n\n"
                        f"Individual Claude part reports:\n{individual_reports}\n\n"
                        f"Prior implementation summary:\n{self.state['implementation_summary']}"
                    )
                    self.state["correction_summary"] = self._run_codex(prompt, sandbox="workspace-write")
                    self.state["stage"] = "complete"
                    self._save()
                elif stage == "complete":
                    self.state["status"] = "complete"
                    self.state["completed_at"] = now()
                    _atomic_text(self.delivery_path, self._delivery_report())
                    self._save()
                else:
                    raise WorkflowError(f"Unknown workflow stage: {stage}")
            except Exception as exc:
                self._fail(exc)
                raise WorkflowError(f"Workflow stopped at {stage}: {redact(str(exc))}") from exc
            finally:
                stage_context.update(duration_ms=max(0, round((time.monotonic() - stage_started) * 1000)))
                stage_context.__exit__(*sys.exc_info())
        return self.state

    def _repository_context_for_claude(self, guidance: dict, review_paths: list[str] | None = None) -> str:
        files = guidance.get("files") or []
        if review_paths is None:
            parts = ["Files: " + (", ".join(files) if files else "none found")]
        else:
            by_root: dict[str, int] = {}
            for path in files:
                root = Path(path).parts[0] if Path(path).parts else "(repository root)"
                by_root[root] = by_root.get(root, 0) + 1
            parts = [
                "Repository inventory by observed top-level path (counts provide orientation; they do not imply architecture): "
                + (", ".join(f"{path}: {count} file(s)" for path, count in sorted(by_root.items())) or "empty repository")
            ]
            parts.append("Changed text files reviewed by this campaign: " + ", ".join(review_paths))
        instructions = guidance.get("instructions") or []
        if instructions:
            parts.append("Applicable repository instructions:")
            parts.extend(f"--- {item['path']} ---\n{item['content']}" for item in instructions)
        else:
            parts.append("No applicable AGENTS.md instructions were found in or above the target repository.")
        return "\n\n".join(parts)

    def _review_history_for_prompt(self) -> str:
        records = self.state.get("plan_reviews", [])
        if not records:
            return "No previous Claude plan-review attempts."
        decisions = self.state.get("plan_review_decisions", [])
        parts = []
        for record in records:
            iteration = record.get("iteration", "unknown")
            if record.get("valid") is not True or not record.get("context_manifest"):
                report_hash = hashlib.sha256(record.get("report", "").encode("utf-8")).hexdigest()
                parts.append(
                    f"Invalidated attempt {record.get('attempt', iteration)} (logical iteration {iteration}): "
                    f"{record.get('invalidated_reason', 'context completeness is unverified')}\n"
                    f"Codex disposition: {record.get('codex_disposition', 'Do not count or reuse its findings.')}\n"
                    f"The unverified report is retained in workflow state for audit only; SHA-256: {report_hash}."
                )
                continue
            prior_decision = next((item for item in decisions if item.get("iteration") == iteration), None)
            disposition = prior_decision.get("dispositions") if prior_decision else "No Codex disposition recorded."
            parts.append(
                f"Valid iteration {iteration}, proposal version {record.get('proposal_version')}:\n"
                f"Claude findings:\n{record.get('report', '')}\n"
                f"Codex finding dispositions:\n{disposition}"
            )
        return "\n\n".join(parts)

    def _plan_review_prompt(self, iteration: int) -> tuple[str, dict[str, str], dict]:
        limit = self.state["authorization"]["claude_aily"]["max_plan_reviews"]
        version = int(self.state.get("proposal_version", 1))
        guidance = self._guidance()
        header = (
            "Act as a Senior Software Architect, Senior Software Engineer, and Product Engineer performing a "
            "read-only plan review. Inspect the complete assignment, complete current proposal, repository context, "
            "workflow constraints, and listed skill guidance. Give concrete findings with reasons. Do not implement code."
        )
        sections = {
            "Review metadata": (
                f"Plan review iteration {iteration} of at most {limit}; proposal version {version}; "
                "all required context sections below are complete."
            ),
            "Assignment": self.state["task"],
            "Current proposal": self.state["proposal"],
            "Repository context": self._repository_context_for_claude(guidance),
            "Workflow constraints": (
                f"Target: {self.target}. The proposal is awaiting human approval; implementation is not authorized "
                "until the user approves it. The workflow does not authorize commits, pushes, merges, deployments, "
                "publication, shared-infrastructure changes, or access to unrelated repositories. Preserve the "
                "existing assignment and workflow budget."
            ),
            "Engineering Judgment & Proportional Design": PLANNING_PRINCIPLES,
            "Relevant skills": _guidance_for_claude(guidance),
            "Review duties": (
                "Answer both questions explicitly: (1) Does the proposal satisfy every explicit assignment requirement? "
                "(2) Is each architectural component and abstraction justified by those requirements? Prefer the "
                "simplest adequate solution. Distinguish genuine omissions from should-implement items, future "
                "production considerations, and out-of-scope ideas. Do not reward complexity by itself."
            ),
            "Review history and Codex decisions": self._review_history_for_prompt(),
        }
        metadata = {
            "iteration": iteration,
            "proposal_version": version,
            "header_id": "plan-review-v2",
        }
        return header, sections, metadata

    @staticmethod
    def _refined_proposal(value: str) -> tuple[str, bool, str]:
        match = re.search(r"(?im)^\s*Second plan review needed:\s*(yes|no)\s*$", value)
        if not match:
            raise WorkflowError("Codex refinement must state whether a second plan review is needed.")
        review_again = match.group(1).lower() == "yes"
        proposal = value[:match.start()].strip()
        if not proposal:
            raise WorkflowError("Codex returned an empty refined proposal.")
        disposition_heading = re.search(r"(?im)^##\s+Claude finding dispositions\s*$", proposal)
        if not disposition_heading:
            raise WorkflowError("Codex refinement is missing the required Claude finding dispositions section.")
        dispositions = proposal[disposition_heading.end():].strip()
        if not dispositions:
            raise WorkflowError("Codex refinement has an empty Claude finding dispositions section.")
        no_findings = re.search(r"(?im)^-\s*No actionable findings\.?\s*$", dispositions)
        finding_decisions = re.findall(
            r"(?im)^-\s*Finding:.*\|\s*Decision:\s*(?:incorporated|rejected|deferred)\s*\|\s*Reason:\s*.+$",
            dispositions,
        )
        if not no_findings and not finding_decisions:
            raise WorkflowError(
                "Codex dispositions must list each finding with a decision and reason, or state that there were none."
            )
        return clean_markdown(proposal), review_again, dispositions

    def _delivery_report(self) -> str:
        reviews = self.state.get("plan_reviews", [])
        valid_reviews = self._valid_plan_reviews()
        external_authorization = self.state["authorization"]["claude_aily"]
        sections = [
            "# Workflow delivery",
            "",
            f"- Workflow: `{self.workflow_id}`",
            f"- Target: `{self.target}`",
            f"- Completed: {self.state.get('completed_at', now())}",
            f"- Codex model: {self.state.get('codex_model') or 'Codex CLI default'}",
            f"- Claude spend recorded: ${external_authorization['spent_usd']:.4f} / ${external_authorization['budget_usd']:.2f}",
            f"- Spend metadata complete: {'no' if external_authorization['budget_unknown'] else 'yes'}",
            "",
            "## Implementation",
            "",
            self.state.get("implementation_summary") or "No implementation summary was recorded.",
            "",
            "## Plan review",
            "",
            f"Claude plan-review iterations: {len(valid_reviews)} valid of {external_authorization['max_plan_reviews']} maximum.",
            f"Invalidated plan-review attempts: {len(reviews) - len(valid_reviews)}.",
            "",
        ]
        for record in reviews:
            validity = "valid" if record.get("valid") is True else "invalidated"
            sections.extend([f"### Attempt {record.get('attempt', record.get('iteration'))} ({validity})", "",
                             record.get("invalidated_reason", ""), "", record["report"], ""])
        decisions = self.state.get("plan_review_decisions", [])
        for decision in decisions:
            sections.extend([
                f"### Codex dispositions after iteration {decision['iteration']}", "",
                decision["dispositions"], "",
            ])
        sections.extend([
            "## Claude context audit",
            "",
        ])
        for context in self.state.get("claude_contexts", []):
            sections.append(
                f"- `{context.get('stage')}` iteration {context.get('iteration')} proposal version "
                f"{context.get('proposal_version')}: {context.get('characters')} characters, "
                f"{context.get('utf8_bytes', 'unknown')} UTF-8 bytes against a "
                f"{context.get('payload_limit_characters', 'unknown')}-character limit; "
                f"call_state={context.get('call_state')}; "
                f"part={context.get('part_id', 'n/a')}; "
                f"SHA-256 `{context.get('prompt_sha256')}`; complete={context.get('complete')}; "
                f"sections: {', '.join(item['name'] for item in context.get('sections', []))}."
            )
        if not self.state.get("claude_contexts"):
            sections.append("No Claude context was submitted.")
        sections.extend(["", "## Code review file coverage", ""])
        included_files = self.state.get("review_included_files", [])
        excluded_files = self.state.get("review_exclusions", [])
        sections.append(f"Included changed files: {len(included_files)}.")
        sections.extend(f"- `{item}`" for item in included_files)
        sections.append(f"Excluded changed files: {len(excluded_files)}.")
        sections.extend(f"- `{item.get('path')}` — {item.get('reason')}" for item in excluded_files)
        if not included_files and not excluded_files:
            sections.append("No code-review file coverage was recorded.")
        campaign = self.state.get("review_campaign")
        sections.extend(["", "## Code review campaign", ""])
        if campaign:
            feedback_reports = _validated_campaign_reports(campaign)
            if feedback_reports:
                sections.extend([
                    "## Structured code-review feedback", "", render_review_feedback(feedback_reports), "",
                ])
            invocation_count = sum(
                call.get("call_state") != "validation_failed"
                for record in campaign.get("part_reviews", []) for call in record.get("calls", [])
            ) + sum(
                call.get("call_state") != "validation_failed"
                for call in campaign.get("integration_review", {}).get("calls", [])
            )
            sections.extend([
                f"- Campaign `{campaign.get('campaign_id')}`; strategy `{campaign.get('strategy')}`; "
                f"status `{campaign.get('status')}`.",
                f"- Parts: {len(campaign.get('parts', []))}; Claude invocation attempts: {invocation_count}; "
                f"authorized call maximum: {campaign.get('authorized_call_limit')}.",
                f"- Shared context SHA-256: `{campaign.get('shared_context_sha256')}`; per-call payload limit: "
                f"{campaign.get('payload_limit_characters')} characters.",
                f"- Files reviewed: {len(campaign.get('files_reviewed', []))}.",
            ])
            sections.extend(f"  - `{path}`" for path in campaign.get("files_reviewed", []))
            sections.append(f"- Files not reviewed: {len(campaign.get('files_not_reviewed', []))}.")
            sections.extend(
                f"  - `{item.get('path')}` — {item.get('reason')}"
                for item in campaign.get("files_not_reviewed", [])
            )
            sections.append(
                f"- Integration review: {campaign.get('integration_review', {}).get('status')}."
            )
            for record in campaign.get("part_reviews", []):
                sections.extend([
                    f"### {record.get('part_id')} — {record.get('status')}", "",
                    record.get("report") or "No report recorded.", "",
                ])
            integration_report = campaign.get("integration_review", {}).get("report")
            if integration_report:
                sections.extend(["### Integration report", "", integration_report, ""])
        else:
            sections.append("No code review campaign was recorded.")
        sections.extend([
            "## Code review",
            "",
            self.state.get("code_review") or "No code review report was recorded.",
            "",
            "## Corrections and validation",
            "",
            self.state.get("correction_summary") or "No correction summary was recorded.",
            "",
            "## Review limits",
            "",
            f"Claude context payload limit: {CLAUDE_MAX_PAYLOAD_ENV} (default "
            f"{claude_reviewer.DEFAULT_MAX_REVIEW_PAYLOAD_CHARS:,} characters). Required sections are validated; "
            "oversized contexts stop before a paid call. Prompt and section hashes are recorded in state.json.",
            "",
            "## Approved proposal",
            "",
            self.state.get("proposal") or "",
        ])
        return "\n".join(sections).rstrip() + "\n"


def recover_code_review(target: str | Path, workflow_id: str, *, apply: bool = False) -> dict:
    """Revalidate persisted part reports locally; never calls a provider."""
    runner = Orchestrator(target, workflow_id=workflow_id)
    if apply:
        with runner._workflow_lock():
            return _recover_code_review(runner, workflow_id, apply=True)
    return _recover_code_review(runner, workflow_id, apply=False)


def _recover_code_review(runner: Orchestrator, workflow_id: str, *, apply: bool) -> dict:
    _safe_artifact(runner.state_path)
    try:
        original_bytes, state = _persistence.read_json_bytes(runner.state_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Workflow state is missing or invalid: {runner.state_path}") from exc
    if state.get("workflow_id") != workflow_id or state.get("target") != str(runner.target):
        raise WorkflowError("Workflow state belongs to another target or workflow ID.")
    authorization = validate_workflow_authorization(state, runner.target, workflow_id)
    if authorization["claude_aily"].get("active_call") or authorization["claude_aily"].get("budget_unknown"):
        raise WorkflowError(
            "Local review recovery is unsafe while a Claude call is active or its cost is unknown."
        )
    if state.get("status") != "failed" or state.get("stage") != "code_review":
        raise WorkflowError("Local code-review recovery requires a failed workflow at the code_review stage.")
    campaign = state.get("review_campaign")
    if not isinstance(campaign, dict) or not campaign.get("campaign_id"):
        raise WorkflowError("No persisted code-review campaign is available to recover.")

    runner.state = state
    try:
        review_diff = _review_diff(state, runner.target)
    except Exception as exc:
        raise WorkflowError(f"Could not safely reconstruct the saved target diff: {redact(str(exc))}") from exc
    shared = runner._code_review_shared_context(review_diff)
    result, proposed = Orchestrator._campaign_helper(lambda: _recover_review_campaign(
        state, workflow_id, runner.target, review_diff, shared, apply=apply,
    ))
    if proposed is None:
        return result
    if _persistence.read_bytes(runner.state_path) != original_bytes:
        raise WorkflowError("Workflow state changed during local recovery; no recovery changes were written.")
    proposed["updated_at"] = now()
    _write_json(runner.state_path, proposed)
    result["applied"] = True
    result["campaign_completed"] = proposed["review_campaign"].get("status") == "complete"
    result["workflow_status_preserved"] = proposed.get("status")
    result["workflow_stage_preserved"] = proposed.get("stage")
    return result

def _task_text(args: argparse.Namespace) -> str:
    return _cli.task_text(args, workflow_error=WorkflowError)


def _code_review_calls_arg(value: str) -> int:
    return _cli.code_review_calls_arg(value, maximum=MAX_AUTHORIZED_CODE_REVIEW_CALLS)


def _print_summary(state: dict) -> None:
    return _cli.print_summary(state, workflow_root=WORKFLOW_ROOT)


def main(argv: list[str] | None = None) -> int:
    return _cli.main(
        argv,
        runner_factory=Orchestrator,
        recover_review=recover_code_review,
        workflow_error=WorkflowError,
        workflow_root=WORKFLOW_ROOT,
        max_authorized_code_review_calls=MAX_AUTHORIZED_CODE_REVIEW_CALLS,
    )



if __name__ == "__main__":
    raise SystemExit(main())

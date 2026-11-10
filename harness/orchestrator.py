#!/usr/bin/env python3
"""Run the plan, approval, implementation, review, and delivery workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Callable

import claude_reviewer


MAX_PLAN_REVIEWS = 2
DEFAULT_CLAUDE_BUDGET_USD = 2.00
CODEX_EXECUTABLE = "codex"
WORKFLOW_ROOT = ".engineering/workflows"
PLANNING_PRINCIPLES = """Engineering Judgment & Proportional Design
1. Explicit requirements take priority over technical recommendations.
2. Do not turn general best practices into requirements automatically.
3. Choose the simplest solution that satisfies the requirements.
4. Compare alternatives when justifying important architectural decisions.
5. Avoid unnecessary abstractions, services, and components.
6. Separate must implement, should implement, future production considerations, and out of scope.
7. Do not invent product behavior; ask the user when a decision changes expected behavior.
8. Make reversible technical assumptions autonomously and document them."""
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
SENSITIVE_NAMES = {
    ".aws", ".ssh", ".env", ".env.local", ".env.production", "credentials",
    "credentials.json", "secrets", "secrets.json", "id_rsa", "id_ed25519",
}
SECRET_PATTERNS = claude_reviewer.SECRET_PATTERNS


class WorkflowError(RuntimeError):
    """An actionable workflow error."""


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


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"})
    return env


def git(target: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.pager=cat", "-c", "maintenance.auto=false", "-C", str(target), *args],
        cwd=target,
        env=_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        detail = redact(result.stderr.decode("utf-8", "replace").strip())
        raise WorkflowError(f"Git command failed ({' '.join(shlex.quote(part) for part in args)}): {detail}")
    return result.stdout


def canonical_target(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise WorkflowError("--target must be an absolute repository path.")
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError(f"Target path is unavailable: {path}") from exc
    if not root.is_dir():
        raise WorkflowError("Target must be a directory.")
    reported = git(root, "rev-parse", "--show-toplevel").decode("utf-8", "strict").strip()
    if Path(reported).resolve(strict=True) != root:
        raise WorkflowError("Target must be the canonical Git repository root.")
    return root


def _check_plain_directories(root: Path, relative: str, *, create: bool = False) -> Path:
    current = root
    parts = Path(relative).parts
    for part in parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise WorkflowError(f"Workflow path contains a symlink or non-directory: {current}")
        elif create:
            current.mkdir()
    return current


def _safe_artifact(path: Path) -> None:
    if path.is_symlink():
        raise WorkflowError(f"Workflow artifact is a symlink: {path}")
    if path.exists() and not path.is_file():
        raise WorkflowError(f"Workflow artifact is not a regular file: {path}")


def _atomic_text(path: Path, value: str) -> None:
    _safe_artifact(path)
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


def _changed_paths(target: Path, baseline: str) -> tuple[list[str], list[str]]:
    tracked = git(target, "diff", "--name-only", "-z", baseline, "--").decode("utf-8", "surrogateescape").split("\0")
    untracked = git(target, "ls-files", "--others", "--exclude-standard", "-z").decode("utf-8", "surrogateescape").split("\0")
    return sorted({item for item in tracked if item}), sorted({item for item in untracked if item})


def _protected_paths(target: Path, baseline: str) -> list[str]:
    tracked, untracked = _changed_paths(target, baseline)
    return sorted(set(tracked + untracked))


def _read_instruction_files(target: Path) -> list[dict[str, str]]:
    paths: set[Path] = set()
    for directory in [*reversed(target.parents), target]:
        candidate = directory / "AGENTS.md"
        if candidate.is_file() and not candidate.is_symlink():
            paths.add(candidate)
    for candidate in target.rglob("AGENTS.md"):
        relative = candidate.relative_to(target)
        if any(part in {".git", WORKFLOW_ROOT.split("/")[0]} for part in relative.parts):
            continue
        if candidate.is_file() and not candidate.is_symlink():
            paths.add(candidate)
    result = []
    for path in sorted(paths):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        label = str(path.relative_to(target)) if path.is_relative_to(target) else str(path)
        result.append({"path": label, "content": content})
    return result


def _skill_metadata(path: Path, content: str) -> tuple[str, str]:
    name = path.parent.name
    description = ""
    frontmatter = re.match(r"(?s)^---\s*\n(.*?)\n---", content)
    if frontmatter:
        name_match = re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+)", frontmatter.group(1))
        description_match = re.search(r"(?m)^description:\s*['\"]?([^'\"\n]+)", frontmatter.group(1))
        if name_match:
            name = name_match.group(1).strip()
        if description_match:
            description = description_match.group(1).strip()
    return name, description


def _project_skills(target: Path, task: str) -> list[dict[str, str]]:
    """Read project skills and the installed API engineering skill when it applies."""
    candidates: list[tuple[Path, str]] = []
    for root in (target / ".agents" / "skills", target / ".codex" / "skills"):
        if root.is_dir():
            candidates.extend((path, "project") for path in root.rglob("SKILL.md")
                              if path.is_file() and not path.is_symlink())

    if re.search(r"\b(api|rest|endpoint)\b", task, re.IGNORECASE):
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        global_candidates = [
            *codex_home.glob(".agents/skills/api-engineer/SKILL.md"),
            *codex_home.glob("skills/api-engineer/SKILL.md"),
            *codex_home.glob(".tmp/marketplaces/*/skills/api-engineer/SKILL.md"),
            *codex_home.glob("plugins/cache/*/*/*/skills/api-engineer/SKILL.md"),
        ]
        candidates.extend((path, "installed Codex plugin") for path in global_candidates
                          if path.is_file() and not path.is_symlink())

    seen: set[Path] = set()
    project_skill_names: set[str] = set()
    installed_skill_names: set[str] = set()
    skills: list[dict[str, str]] = []
    for path, source in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        name, description = _skill_metadata(path, content)
        if source != "project" and name != "api-engineer":
            continue
        if source == "project":
            project_skill_names.add(name)
        elif name in project_skill_names or name in installed_skill_names:
            continue
        else:
            installed_skill_names.add(name)
        skills.append({
            "name": name,
            "path": str(path),
            "source": source,
            "description": description,
            "content": content,
        })
    return skills


def _project_guidance(target: Path, task: str) -> dict:
    files = git(target, "ls-files", "-co", "--exclude-standard", "-z").decode(
        "utf-8", "surrogateescape").split("\0")
    visible_files = sorted({
        path for path in files if path and not path.startswith(f"{WORKFLOW_ROOT}/")
        and ".git" not in Path(path).parts and not _is_sensitive(path)
        and Path(path).name != ".DS_Store"
    })
    instructions = _read_instruction_files(target)
    skills = _project_skills(target, task)
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return {
        "files": visible_files,
        "instructions": instructions,
        "skills": skills,
        "skill_locations_checked": [
            str(target / ".agents" / "skills"),
            str(target / ".codex" / "skills"),
            str(codex_home / ".agents" / "skills"),
            str(codex_home / "skills"),
            str(codex_home / ".tmp" / "marketplaces" / "*" / "skills"),
            str(codex_home / "plugins" / "cache" / "*" / "*" / "*" / "skills"),
        ],
    }


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


def _is_sensitive(relative: str) -> bool:
    return any(part.lower() in SENSITIVE_NAMES for part in Path(relative).parts)


def _has_symlink_component(target: Path, relative: str) -> bool:
    current = target
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _review_diff(state: dict, target: Path) -> str:
    tracked, untracked = _changed_paths(target, state["baseline_commit"])
    protected = set(state.get("preexisting_paths", []))
    workflow_prefix = WORKFLOW_ROOT + "/"
    safe_tracked = [path for path in tracked if path not in protected and not path.startswith(workflow_prefix)
                    and not _is_sensitive(path) and ".git" not in Path(path).parts
                    and not _has_symlink_component(target, path)]
    chunks: list[str] = []
    if safe_tracked:
        raw = git(target, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--binary",
                  state["baseline_commit"], "--", *safe_tracked).decode("utf-8", "replace")
        chunks.append(redact(raw))
    for relative in untracked:
        if (relative in protected or relative.startswith(workflow_prefix) or _is_sensitive(relative)
                or ".git" in Path(relative).parts or _has_symlink_component(target, relative)):
            continue
        path = target / relative
        if path.is_symlink() or not path.is_file():
            continue
        try:
            file_size = path.stat().st_size
            limit = claude_reviewer.max_review_payload_chars()
            if file_size > limit * 4:
                raise WorkflowError(
                    f"Complete new-file diff for {relative} cannot fit the configured {limit}-character "
                    f"Claude review limit ({CLAUDE_MAX_PAYLOAD_ENV}); no paid call was made."
                )
            content = path.read_text(encoding="utf-8", errors="replace")
            if len(content) > limit:
                raise WorkflowError(
                    f"Complete new-file diff for {relative} is larger than the configured {limit}-character "
                    f"Claude review limit ({CLAUDE_MAX_PAYLOAD_ENV}); no paid call was made."
                )
        except WorkflowError:
            raise
        except OSError:
            continue
        chunks.append(f"diff --git a/{relative} b/{relative}\nnew file\n{redact(content)}\n")
    result = "\n".join(chunks).strip()
    return result or "No reviewable changes were found."


def _format_context_prompt(header: str, sections: dict[str, str]) -> str:
    blocks = [header.strip()]
    blocks.extend(f"## {name}\n{value.strip()}" for name, value in sections.items())
    return "\n\n".join(blocks).strip()


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
    empty = re.search(
        r"(?i)\b(no\s+(?:actionable\s+|material\s+|code\s+)?(?:findings|issues|defects)|"
        r"nothing to change|findings?\s*:\s*(?:none|no))\b", report,
    )
    return bool(approved and empty)


def new_workflow_authorization(target: Path, workflow_id: str, budget_usd: float) -> dict:
    """Create the single persistent authorization attached to a new workflow."""
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
            "max_code_reviews": 1,
            "budget_usd": float(budget_usd),
            "spent_usd": 0.0,
            "budget_unknown": False,
            "active_call": None,
        },
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
    external = authorization.get("claude_aily")
    if not isinstance(external, dict):
        raise WorkflowError("Saved Claude/Aily authorization is missing.")
    if (external.get("provider") != "Claude through Aily"
            or external.get("permitted_data") != CLAUDE_DATA_SCOPE
            or external.get("max_plan_reviews") != MAX_PLAN_REVIEWS
            or external.get("max_code_reviews") != 1):
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
        model: str | None = None,
        workflow_id: str | None = None,
        codex: str = CODEX_EXECUTABLE,
        aily: str = claude_reviewer.AILY_EXECUTABLE,
        timeout_seconds: int = 3600,
        process_runner: Callable = subprocess.run,
        claude_runner: Callable = claude_reviewer.review,
    ):
        self.target = canonical_target(str(target))
        self.task = task
        self.budget_usd = budget_usd
        if model is not None and not model.strip():
            raise WorkflowError("--model must name a Codex model when provided.")
        self.model = model.strip() if model is not None else None
        self.workflow_id = valid_workflow_id(workflow_id or self._new_workflow_id())
        self.codex = codex
        self.aily = aily
        self.timeout_seconds = timeout_seconds
        self.process_runner = process_runner
        self.claude_runner = claude_runner
        self.workflow_dir = _check_plain_directories(self.target, f"{WORKFLOW_ROOT}/{self.workflow_id}")
        self.state_path = self.workflow_dir / "state.json"
        self.proposal_path = self.workflow_dir / "proposal.md"
        self.delivery_path = self.workflow_dir / "delivery.md"

    @staticmethod
    def _new_workflow_id() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]

    def _save(self) -> None:
        self.state["updated_at"] = now()
        authorization = self.state.get("authorization")
        if isinstance(authorization, dict):
            authorization["scope_sha256"] = authorization_scope_digest(authorization)
        _atomic_text(self.state_path, json.dumps(self.state, indent=2, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        _safe_artifact(self.state_path)
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
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
        authorization = validate_workflow_authorization(self.state, self.target, self.workflow_id)
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
            proposal = self.proposal_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(f"Saved proposal is unavailable: {self.proposal_path}") from exc
        if not proposal.strip():
            raise WorkflowError("Saved proposal is empty.")
        return clean_markdown(proposal)

    def start(self) -> dict:
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
            "authorization": new_workflow_authorization(self.target, self.workflow_id, float(budget)),
            "plan_reviews": [],
            "claude_contexts": [],
            "plan_review_decisions": [],
            "proposal": None,
            "implementation_approved_at": None,
            "implementation_summary": None,
            "code_review": None,
            "correction_summary": None,
            "error": None,
        }
        self._save()
        return self.run_pending()

    def approve(self) -> dict:
        self._load()
        if self.state.get("status") != "awaiting_approval":
            raise WorkflowError("This workflow is not waiting for implementation approval.")
        current_proposal = self._read_proposal()
        if current_proposal != clean_markdown(self.state.get("proposal", "")):
            raise WorkflowError("The proposal file changed after review; use revise to review the change before approval.")
        self.state["implementation_approved_at"] = {"time": now(), "method": "explicit approve command"}
        self.state["status"] = "running"
        self.state["stage"] = "implementation"
        self.state["error"] = None
        self._save()
        return self.run_pending()

    def revise(self, feedback: str) -> dict:
        self._load()
        if self.state.get("status") != "awaiting_approval":
            raise WorkflowError("This workflow is not waiting for proposal feedback.")
        if not feedback.strip():
            raise WorkflowError("Proposal feedback must not be empty.")
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
        self._load()
        if self.state.get("status") != "awaiting_approval":
            raise WorkflowError("This workflow is not waiting for a proposal decision.")
        self.state["status"] = "rejected"
        self.state["rejection_reason"] = reason.strip() or "Rejected by the user."
        self.state["completed_at"] = now()
        self._save()
        return self.state

    def resume(self) -> dict:
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
        self.state["status"] = "running"
        self.state["error"] = None
        self._save()
        return self.run_pending()

    def status(self) -> dict:
        self._load()
        return self.state

    def _run_codex(self, prompt: str, *, sandbox: str) -> str:
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
        authorization = validate_workflow_authorization(self.state, self.target, self.workflow_id)
        external = authorization["claude_aily"]
        if external["budget_unknown"]:
            raise WorkflowError("Claude review stopped because a previous call has no recorded cost.")
        if external["permitted_data"] != CLAUDE_DATA_SCOPE:
            raise WorkflowError("This Claude/Aily request is outside the workflow's persisted data authorization.")
        if stage.startswith("plan_review_") and len(self._valid_plan_reviews()) >= external["max_plan_reviews"]:
            raise WorkflowError("The authorized Claude plan-review limit is exhausted.")
        if stage == "code_review" and external["max_code_reviews"] < 1:
            raise WorkflowError("The workflow authorization does not include a Claude code review.")

        required_sections = {
            "plan_review": {
                "Review metadata", "Assignment", "Current proposal", "Repository context",
                "Workflow constraints", "Engineering Judgment & Proportional Design", "Relevant skills",
                "Review duties", "Review history and Codex decisions",
            },
            "code_review": {
                "Review metadata", "Assignment", "Approved proposal", "Implementation summary",
                "Repository context", "Relevant skills", "Complete patch",
            },
        }["code_review" if stage == "code_review" else "plan_review"]
        missing = sorted(name for name in required_sections if not sections.get(name, "").strip())
        if missing:
            raise WorkflowError(
                "Claude review context is incomplete; missing mandatory sections: " + ", ".join(missing)
                + ". No paid call was made."
            )
        if stage.startswith("plan_review_") and not all(
                isinstance((metadata or {}).get(key), int) for key in ("iteration", "proposal_version")):
            raise WorkflowError(
                "Claude plan-review metadata must include the review iteration and proposal version. "
                "No paid call was made."
            )
        if stage == "code_review" and not isinstance((metadata or {}).get("proposal_version"), int):
            raise WorkflowError("Claude code-review metadata must include the proposal version. No paid call was made.")
        safe_header = redact(header)
        safe_sections = {name: redact(value).strip() for name, value in sections.items()}
        prompt = _format_context_prompt(safe_header, safe_sections)
        for name in required_sections:
            block = f"## {name}\n{safe_sections[name]}"
            if block not in prompt:
                raise WorkflowError(
                    f"Claude review context section {name!r} is not present in the payload. No paid call was made."
                )
        try:
            payload_limit = claude_reviewer.validate_review_payload(prompt)
        except claude_reviewer.ReviewError as exc:
            raise WorkflowError(str(exc)) from exc

        remaining_amount = Decimal(str(external["budget_usd"])) - Decimal(str(external["spent_usd"]))
        remaining = float(remaining_amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        if remaining < 0.01:
            raise WorkflowError("The authorized Claude budget is exhausted.")

        prompt_bytes = prompt.encode("utf-8")
        section_manifest = [
            {
                "name": name,
                "characters": len(value),
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            }
            for name, value in safe_sections.items()
        ]
        manifest = {
            "stage": stage,
            "iteration": (metadata or {}).get("iteration"),
            "proposal_version": (metadata or {}).get("proposal_version"),
            "header_id": (metadata or {}).get("header_id", "claude-review-v1"),
            "header_characters": len(safe_header),
            "header_sha256": hashlib.sha256(safe_header.encode("utf-8")).hexdigest(),
            "characters": len(prompt),
            "payload_limit_characters": payload_limit,
            "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
            "complete": True,
            "sections": section_manifest,
            "submitted_at": now(),
            "call_state": "submitted",
        }
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
        cost = result.cost_usd
        if cost is not None:
            external["spent_usd"] = round(external["spent_usd"] + cost, 4)
        else:
            external["budget_unknown"] = True
        external["active_call"] = None
        contexts[context_index]["call_state"] = "completed"
        contexts[context_index]["cost_usd"] = cost
        self._save()
        return result.text, manifest

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
        while self.state.get("status") == "running":
            stage = self.state["stage"]
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
                    proposal, review_again, dispositions = self._refined_proposal(refined)
                    self.state["proposal"] = proposal
                    self.state["proposal_version"] = int(self.state.get("proposal_version", 1)) + 1
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
                    diff = _review_diff(self.state, self.target)
                    if diff == "No reviewable changes were found.":
                        raise WorkflowError(
                            "No reviewable patch is available. The implementation may not have changed files, "
                            "or all changes were excluded as pre-existing or sensitive."
                        )
                    guidance = self._guidance()
                    header = (
                        "Act as a senior software engineer performing an independent, read-only code review. "
                        "Review the complete patch for defects, missing scenarios, reliability/security gaps, and "
                        "missing tests. Give concrete file references and fixes. Start with `Decision: APPROVED` or "
                        "`Decision: CHANGES_REQUESTED`, then explain findings. The review is advisory."
                    )
                    sections = {
                        "Review metadata": (
                            f"Code review; proposal version {self.state.get('proposal_version', 1)}; "
                            "all patch content is complete subject to the configured payload limit."
                        ),
                        "Assignment": self.state["task"],
                        "Approved proposal": self.state["proposal"],
                        "Implementation summary": self.state["implementation_summary"],
                        "Repository context": self._repository_context_for_claude(guidance),
                        "Relevant skills": _guidance_for_claude(guidance),
                        "Complete patch": diff,
                    }
                    self.state["code_review"], manifest = self._call_claude(
                        header, stage, sections,
                        metadata={"proposal_version": self.state.get("proposal_version", 1),
                                  "header_id": "code-review-v1"},
                    )
                    self.state["code_review_context_sha256"] = manifest["prompt_sha256"]
                    if _clearly_no_review_findings(self.state["code_review"]):
                        self.state["correction_summary"] = "Claude reported no findings; no correction run was needed."
                        self.state["stage"] = "complete"
                    else:
                        self.state["stage"] = "correction"
                    self._save()
                elif stage == "correction":
                    prompt = (
                        "Evaluate the independent Claude code review. Fix valid in-scope defects, reject incorrect "
                        "recommendations with a brief reason, and rerun tests/checks affected by any fixes. Do not "
                        "make unrelated changes or repeat the review. Return a concise correction and validation "
                        "summary.\n\n"
                        f"Assignment:\n{self.state['task']}\n\nApproved proposal:\n{self.state['proposal']}\n\n"
                        f"Claude code review:\n{self.state['code_review']}\n\n"
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
        return self.state

    def _repository_context_for_claude(self, guidance: dict) -> str:
        files = guidance.get("files") or []
        parts = ["Files: " + (", ".join(files) if files else "none found")]
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
                f"{context.get('proposal_version')}: {context.get('characters')} characters; "
                f"SHA-256 `{context.get('prompt_sha256')}`; complete={context.get('complete')}; "
                f"sections: {', '.join(item['name'] for item in context.get('sections', []))}."
            )
        if not self.state.get("claude_contexts"):
            sections.append("No Claude context was submitted.")
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


def _task_text(args: argparse.Namespace) -> str:
    if bool(args.task) == bool(args.task_file):
        raise WorkflowError("Supply exactly one of --task or --task-file.")
    if args.task is not None:
        return args.task
    try:
        return Path(args.task_file).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"Could not read task file: {args.task_file}") from exc


def _print_summary(state: dict) -> None:
    external_authorization = state.get("authorization", {}).get("claude_aily", {})
    reviews = state.get("plan_reviews", [])
    valid_reviews = [record for record in reviews if record.get("valid") is True]
    print(json.dumps({
        "workflow_id": state.get("workflow_id"),
        "target": state.get("target"),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "proposal": f"{WORKFLOW_ROOT}/{state.get('workflow_id')}/proposal.md",
        "delivery": f"{WORKFLOW_ROOT}/{state.get('workflow_id')}/delivery.md" if state.get("status") == "complete" else None,
        "authorization_id": state.get("authorization", {}).get("authorization_id"),
        "codex_model": state.get("codex_model"),
        "proposal_version": state.get("proposal_version"),
        "valid_plan_reviews": len(valid_reviews),
        "invalidated_plan_reviews": len(reviews) - len(valid_reviews),
        "claude_contexts": [
            {
                "stage": context.get("stage"),
                "iteration": context.get("iteration"),
                "proposal_version": context.get("proposal_version"),
                "characters": context.get("characters"),
                "prompt_sha256": context.get("prompt_sha256"),
                "sections": [section.get("name") for section in context.get("sections", [])],
                "complete": context.get("complete"),
                "call_state": context.get("call_state"),
            }
            for context in state.get("claude_contexts", [])
        ],
        "spent_usd": external_authorization.get("spent_usd"),
        "effective_budget_usd": external_authorization.get("budget_usd"),
        "error": state.get("error"),
    }, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Create a proposal, review it, and wait for approval")
    start.add_argument("--target", required=True, help="Absolute Git repository root")
    task = start.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="Assignment text")
    task.add_argument("--task-file", help="UTF-8 file containing the assignment")
    start.add_argument("--workflow-id", help="Optional stable ID")
    start.add_argument("--budget-usd", type=float, default=DEFAULT_CLAUDE_BUDGET_USD,
                       help=f"Total cap for Claude/Aily calls (default: ${DEFAULT_CLAUDE_BUDGET_USD:.2f})")
    start.add_argument("--codex", default=CODEX_EXECUTABLE)
    start.add_argument("--aily", default=claude_reviewer.AILY_EXECUTABLE)
    start.add_argument("--model", help="Optional Codex model; omit to use the CLI's configured default")

    for name, help_text in (
        ("approve", "Explicitly approve the saved proposal and start implementation"),
        ("resume", "Resume the failed stage without restarting completed stages"),
        ("status", "Show current workflow state"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--target", required=True, help="Absolute Git repository root")
        command.add_argument("--workflow-id", required=True)
        command.add_argument("--codex", default=CODEX_EXECUTABLE)
        command.add_argument("--aily", default=claude_reviewer.AILY_EXECUTABLE)

    revise = commands.add_parser("revise", help="Request a proposal change before implementation")
    revise.add_argument("--target", required=True)
    revise.add_argument("--workflow-id", required=True)
    feedback = revise.add_mutually_exclusive_group(required=True)
    feedback.add_argument("--feedback")
    feedback.add_argument("--feedback-file")
    revise.add_argument("--codex", default=CODEX_EXECUTABLE)
    revise.add_argument("--aily", default=claude_reviewer.AILY_EXECUTABLE)

    reject = commands.add_parser("reject", help="Reject the proposal and stop the workflow")
    reject.add_argument("--target", required=True)
    reject.add_argument("--workflow-id", required=True)
    reject.add_argument("--reason", default="Rejected by the user.")

    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            runner = Orchestrator(
                args.target, task=_task_text(args), budget_usd=args.budget_usd,
                workflow_id=args.workflow_id, codex=args.codex, aily=args.aily, model=args.model,
            )
            print(f"Effective Claude/Aily budget: ${runner.budget_usd:.2f}")
            state = runner.start()
        else:
            runner = Orchestrator(args.target, workflow_id=args.workflow_id, codex=args.codex, aily=args.aily)
            if args.command == "approve":
                state = runner.approve()
            elif args.command == "resume":
                state = runner.resume()
            elif args.command == "revise":
                feedback_text = args.feedback
                if args.feedback_file:
                    try:
                        feedback_text = Path(args.feedback_file).expanduser().read_text(encoding="utf-8")
                    except OSError as exc:
                        raise WorkflowError(f"Could not read feedback file: {args.feedback_file}") from exc
                state = runner.revise(feedback_text)
            elif args.command == "reject":
                state = runner.reject(args.reason)
            else:
                state = runner.status()
        _print_summary(state)
        return 0 if state.get("status") in {"awaiting_approval", "complete", "rejected"} else 1
    except (WorkflowError, claude_reviewer.ReviewError) as exc:
        print(f"workflow error: {redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

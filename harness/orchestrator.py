#!/usr/bin/env python3
"""Run the plan, approval, implementation, review, and delivery workflow."""

from __future__ import annotations

import argparse
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
CODEX_EXECUTABLE = "codex"
WORKFLOW_ROOT = ".engineering/workflows"
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
            if path.stat().st_size > 500_000:
                chunks.append(f"New file omitted because it exceeds 500 KB: {relative}\n")
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(f"diff --git a/{relative} b/{relative}\nnew file\n{redact(content)}\n")
    result = "\n".join(chunks).strip()
    if len(result) > 200_000:
        result = result[:200_000] + "\n\n[Review packet truncated at 200 KB.]"
    return result or "No reviewable changes were found."


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


def _clearly_no_review_findings(report: str) -> bool:
    approved = re.search(r"(?im)^\s*Decision:\s*APPROVED\b", report)
    empty = re.search(
        r"(?i)\b(no\s+(?:actionable\s+|material\s+|code\s+)?(?:findings|issues|defects)|"
        r"nothing to change|findings?\s*:\s*(?:none|no))\b", report,
    )
    return bool(approved and empty)


class Orchestrator:
    """A single-run stage controller with small, target-local JSON state."""

    def __init__(
        self,
        target: str | Path,
        *,
        task: str | None = None,
        budget_usd: float | None = None,
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
        _atomic_text(self.state_path, json.dumps(self.state, indent=2, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        _safe_artifact(self.state_path)
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"Workflow state is missing or invalid: {self.state_path}") from exc
        if self.state.get("workflow_id") != self.workflow_id or self.state.get("target") != str(self.target):
            raise WorkflowError("Workflow state belongs to another target or workflow ID.")
        if self.state.get("active_claude_call"):
            self.state["budget_unknown"] = True
            self.state["active_claude_call"] = None
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
            budget = Decimal(str(self.budget_usd))
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
            "baseline_commit": baseline,
            "preexisting_paths": _protected_paths(self.target, baseline),
            "budget_usd": float(budget),
            "spent_usd": 0.0,
            "budget_unknown": False,
            "active_claude_call": None,
            "plan_reviews": [],
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
        self.state["proposal"] = self._read_proposal()
        self.state["human_feedback"] = feedback.strip()
        self.state["status"] = "running"
        self.state["stage"] = "human_revision"
        self.state["error"] = None
        self._save()
        return self.run_pending()

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
        if self.state.get("budget_unknown") and self.state.get("stage", "").startswith(("plan_review", "code_review")):
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
            command = [
                self.codex, "exec", "--json", "--ephemeral", "--disable", "plugins",
                "-c", "mcp_servers={}", "--sandbox", sandbox,
                "--ask-for-approval", "never", "--cd", str(self.target),
                "--output-last-message", str(output_path), "-",
            ]
            try:
                result = self.process_runner(
                    command,
                    cwd=str(self.target),
                    input=prompt.encode("utf-8"),
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

    def _call_claude(self, prompt: str, stage: str) -> str:
        if self.state["budget_unknown"]:
            raise WorkflowError("Claude review stopped because a previous call has no recorded cost.")
        remaining_amount = Decimal(str(self.state["budget_usd"])) - Decimal(str(self.state["spent_usd"]))
        remaining = float(remaining_amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        if remaining < 0.01:
            raise WorkflowError("The authorized Claude budget is exhausted.")
        self.state["active_claude_call"] = {"stage": stage, "started_at": now(), "cap_usd": remaining}
        self._save()
        prompt = redact(prompt)
        try:
            result = self.claude_runner(prompt, budget_usd=remaining, executable=self.aily)
        except claude_reviewer.ReviewError as exc:
            if exc.cost_usd is None:
                self.state["budget_unknown"] = True
            else:
                self.state["spent_usd"] = round(self.state["spent_usd"] + exc.cost_usd, 4)
            self.state["active_claude_call"] = None
            self.state["error"] = str(exc)
            self._save()
            raise WorkflowError(str(exc)) from exc
        cost = result.cost_usd
        if cost is not None:
            self.state["spent_usd"] = round(self.state["spent_usd"] + cost, 4)
        else:
            self.state["budget_unknown"] = True
        self.state["active_claude_call"] = None
        self._save()
        return result.text

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
                    prompt = (
                        "Understand the assignment and this repository. Read its instructions and relevant code. "
                        "Make reasonable technical decisions, identify requirements, edge cases, and verification. "
                        "Return a concise, readable Markdown implementation proposal with scope, approach, risks, "
                        "and concrete validation commands. Do not modify files or run tests.\n\n"
                        f"Target: {self.target}\n\nAssignment:\n{self.state['task']}"
                    )
                    proposal = clean_markdown(self._run_codex(prompt, sandbox="read-only"))
                    self.state["proposal"] = proposal
                    _atomic_text(self.proposal_path, proposal)
                    self.state["stage"] = "plan_review_1"
                    self._save()
                elif stage in {"plan_review_1", "plan_review_2"}:
                    iteration = int(stage[-1])
                    prompt = self._plan_review_prompt()
                    report = self._call_claude(prompt, stage)
                    self.state["plan_reviews"].append({"iteration": iteration, "report": report, "time": now()})
                    self.state["stage"] = f"plan_refine_{iteration}"
                    self._save()
                elif stage in {"plan_refine_1", "plan_refine_2"}:
                    iteration = int(stage[-1])
                    report = self.state["plan_reviews"][iteration - 1]["report"]
                    prompt = (
                        "Evaluate the Claude plan-review report. Incorporate valid recommendations, reject weak ones "
                        "with a short rationale, and return the complete refined proposal as readable Markdown. "
                        "Add one final line exactly in this form: `Second plan review needed: yes` or `...: no`. "
                        "Use yes only when a material plan change needs a second independent plan review.\n\n"
                        f"Assignment:\n{self.state['task']}\n\nCurrent proposal:\n{self.state['proposal']}\n\n"
                        f"Claude review:\n{report}"
                    )
                    refined = self._run_codex(prompt, sandbox="read-only")
                    proposal, review_again = self._refined_proposal(refined)
                    self.state["proposal"] = proposal
                    _atomic_text(self.proposal_path, proposal)
                    if iteration == 1 and review_again:
                        self.state["stage"] = "plan_review_2"
                    else:
                        self.state["status"] = "awaiting_approval"
                        self.state["stage"] = "implementation"
                    self._save()
                elif stage == "human_revision":
                    prompt = (
                        "Revise the saved proposal to address the user's explicit feedback. Keep the original "
                        "assignment and unrelated decisions intact. Return the complete proposal in readable "
                        "Markdown; do not implement code.\n\n"
                        f"Assignment:\n{self.state['task']}\n\nCurrent proposal:\n{self.state['proposal']}\n\n"
                        f"User feedback:\n{self.state['human_feedback']}"
                    )
                    self.state["proposal"] = clean_markdown(self._run_codex(prompt, sandbox="read-only"))
                    _atomic_text(self.proposal_path, self.state["proposal"])
                    if len(self.state["plan_reviews"]) < MAX_PLAN_REVIEWS:
                        self.state["stage"] = f"plan_review_{len(self.state['plan_reviews']) + 1}"
                    else:
                        self.state["status"] = "awaiting_approval"
                        self.state["stage"] = "implementation"
                    self._save()
                elif stage == "implementation":
                    if not self.state.get("implementation_approved_at"):
                        self.state["status"] = "awaiting_approval"
                        self._save()
                        break
                    prompt = (
                        "Implement the approved proposal in this repository. Follow repository instructions. "
                        "Work autonomously inside this workspace, install needed dependencies using the project's "
                        "package manager, and run relevant tests and checks. Do not expose secrets, read unrelated "
                        "repositories, or commit, push, merge, deploy, publish, or change shared infrastructure. "
                        "Do not delete unrelated existing files. Return a concise summary with files changed, "
                        "commands actually run, results, and known limitations.\n\n"
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
                    prompt = (
                        "Act as a senior software engineer performing an independent, read-only code review. "
                        "Review the actual patch below for defects, missing scenarios, reliability/security gaps, "
                        "and missing tests. Give concrete file references and fixes. Start with `Decision: APPROVED` "
                        "or `Decision: CHANGES_REQUESTED`, then explain findings. The review is advisory.\n\n"
                        f"Assignment:\n{self.state['task']}\n\nApproved proposal:\n{self.state['proposal']}\n\n"
                        f"Implementation summary:\n{self.state['implementation_summary']}\n\nPatch:\n{diff}"
                    )
                    self.state["code_review"] = self._call_claude(prompt, stage)
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

    def _plan_review_prompt(self) -> str:
        iteration = len(self.state["plan_reviews"]) + 1
        return (
            "Act as a Senior Software Architect, Senior Software Engineer, and Product Engineer. "
            f"This is plan review {iteration} of at most {MAX_PLAN_REVIEWS}. Inspect the assignment and proposal. "
            "Identify missing requirements/scenarios, weak assumptions, architecture, reliability, security, "
            "testing gaps, and unnecessary complexity. Give concrete recommendations. Do not implement code.\n\n"
            f"Assignment:\n{self.state['task']}\n\nProposal:\n{self.state['proposal']}"
        )

    @staticmethod
    def _refined_proposal(value: str) -> tuple[str, bool]:
        match = re.search(r"(?im)^\s*Second plan review needed:\s*(yes|no)\s*$", value)
        review_again = bool(match and match.group(1).lower() == "yes")
        proposal = value[:match.start()].strip() if match else value.strip()
        if not proposal:
            raise WorkflowError("Codex returned an empty refined proposal.")
        return clean_markdown(proposal), review_again

    def _delivery_report(self) -> str:
        reviews = self.state.get("plan_reviews", [])
        sections = [
            "# Workflow delivery",
            "",
            f"- Workflow: `{self.workflow_id}`",
            f"- Target: `{self.target}`",
            f"- Completed: {self.state.get('completed_at', now())}",
            f"- Claude spend recorded: ${self.state.get('spent_usd', 0):.4f} / ${self.state.get('budget_usd', 0):.2f}",
            f"- Spend metadata complete: {'no' if self.state.get('budget_unknown') else 'yes'}",
            "",
            "## Implementation",
            "",
            self.state.get("implementation_summary") or "No implementation summary was recorded.",
            "",
            "## Plan review",
            "",
            f"Claude plan-review iterations: {len(reviews)} (maximum {MAX_PLAN_REVIEWS}).",
            "",
        ]
        for record in reviews:
            sections.extend([f"### Iteration {record['iteration']}", "", record["report"], ""])
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
            "Claude received only the redacted changes available for review. Pre-existing, ignored, sensitive, and "
            "symlinked paths were excluded; new files above 500 KB were omitted and the patch was capped at 200 KB.",
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
    print(json.dumps({
        "workflow_id": state.get("workflow_id"),
        "target": state.get("target"),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "proposal": f"{WORKFLOW_ROOT}/{state.get('workflow_id')}/proposal.md",
        "delivery": f"{WORKFLOW_ROOT}/{state.get('workflow_id')}/delivery.md" if state.get("status") == "complete" else None,
        "spent_usd": state.get("spent_usd"),
        "budget_usd": state.get("budget_usd"),
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
    start.add_argument("--budget-usd", required=True, type=float, help="Total cap for Claude/Aily calls")
    start.add_argument("--codex", default=CODEX_EXECUTABLE)
    start.add_argument("--aily", default=claude_reviewer.AILY_EXECUTABLE)

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
                workflow_id=args.workflow_id, codex=args.codex, aily=args.aily,
            )
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

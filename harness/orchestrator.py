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
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Callable

import claude_reviewer


MAX_PLAN_REVIEWS = 2
DEFAULT_CLAUDE_BUDGET_USD = 2.00
DEFAULT_MAX_CODE_REVIEW_CALLS = 16
MAX_AUTHORIZED_CODE_REVIEW_CALLS = 64
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
SENSITIVE_NAMES = {
    ".aws", ".ssh", ".env", ".env.local", ".env.production", "credentials",
    "credentials.json", "secrets", "secrets.json", "id_rsa", "id_ed25519",
}
OS_METADATA_NAMES = {
    ".ds_store", "thumbs.db", "ehthumbs.db", "desktop.ini", ".directory", ".appledouble",
}
KNOWN_BINARY_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"PK\x03\x04",
    b"%PDF-", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
)
SECRET_PATTERNS = claude_reviewer.SECRET_PATTERNS


class WorkflowError(RuntimeError):
    """An actionable workflow error."""


@dataclass(frozen=True)
class ReviewDiff:
    """Complete text patch plus an auditable account of files filtered from it."""

    text: str
    included_paths: tuple[str, ...]
    excluded_files: tuple[tuple[str, str], ...]
    file_patches: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReviewSegment:
    """One complete file patch or an explicitly bounded, lossless patch segment."""

    path: str
    text: str
    patch_line_start: int
    patch_line_end: int
    patch_line_count: int
    patch_sha256: str


CLAUDE_CODE_REVIEW_GUIDANCE = """Independent review roles and duties
Act as a Senior Software Architect, Senior Backend Engineer, Product Engineer, and independent senior reviewer.
Do not assume the approved proposal is correct merely because it was approved. Verify the actual implementation
against the assignment and challenge assumptions where evidence warrants it.

Evaluate requirements and product behavior; architecture and boundaries; design and maintainability; engineering
judgment and proportionality; relevant reliability and operational behavior; security; and test quality. Evaluate
the architecture that exists in context. Do not prefer Clean, Hexagonal, Microservices, CQRS, or another style by
default. Ask what concrete problem each significant decision solves, whether that problem exists here, what trade-off
it creates, and whether a simpler design would satisfy the requirement. Do not demand lengthy justification for
trivial details. Treat timeout, retry, concurrency, recovery, and scale concerns as findings only when relevant to
the actual requirements and execution model.

Use explicit requirements as the acceptance baseline. Separate confirmed defects from risks needing verification
and optional improvements. Do not report a style preference as a defect. Every finding must explain what is wrong,
why it matters in this assignment, the affected requirement or engineering property, and a concrete correction.
Classify severity as Must fix, Should fix, or Optional improvement; optional improvements do not block acceptance.
Use category Requirements, Architecture, Design, Correctness, Reliability, Security, Testing, or Integration.

For individual parts, assess local responsibilities, cohesion, coupling, dependency direction, contracts, tests, and
failure handling. Put concerns that depend on another part under Cross-component concerns and state what evidence
the integration review should verify. For final integration, synthesize all part reports; inspect boundaries,
contracts, data flow, assumptions, error/state behavior, duplicate responsibilities, and cross-component defects.
Do not merely concatenate reports. Consolidate duplicates while preserving references and introduce new integration
findings only when supported by the shared context and part reports.

Return the requested structured report. Each finding must include severity, category, file and location when known,
description, assignment-specific impact and reasoning, and recommended correction. Finish with a concise overall
assessment of requirement satisfaction, architecture/design quality, main risks, proportionality, and whether any
finding blocks acceptance. Only Must fix findings or an explicit CHANGES_REQUESTED decision should trigger the
correction stage; Should fix and Optional improvements must be reasoned about and remain advisory unless the report
explicitly identifies an acceptance-blocking defect."""


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


def _safe_review_path(relative: str) -> str:
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(relative, ensure_ascii=True)
    return relative


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


def _review_patch_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_patch_boundaries(lines: list[str], path: str) -> list[int]:
    """Return conservative split positions at hunk, declaration, or prose boundaries."""
    boundaries: set[int] = set()
    declaration = re.compile(
        r"^(?:async\s+)?(?:def|class|function|interface|type|enum|struct|trait|impl|func|fn)\b"
        r"|^(?:export\s+)?(?:class|function|interface|type|enum)\b"
    )
    prose_file = Path(path).suffix.lower() in {".md", ".markdown", ".rst", ".txt", ".adoc"}
    for index in range(1, len(lines)):
        current = lines[index]
        previous = lines[index - 1]
        if current.startswith("@@") and any(line.startswith("@@") for line in lines[:index]):
            boundaries.add(index)
            continue
        if current[:1] in {"+", "-", " "}:
            content = current[1:].rstrip("\r\n")
        else:
            content = current.rstrip("\r\n")
        indentation = len(content) - len(content.lstrip(" \t"))
        if indentation == 0 and declaration.match(content):
            boundaries.add(index)
            continue
        previous_content = previous[1:].strip() if previous[:1] in {"+", "-", " "} else previous.strip()
        if not previous_content:
            is_heading = prose_file and content.lstrip().startswith("#")
            if is_heading or (prose_file and content.strip()):
                boundaries.add(index)
    return sorted(boundary for boundary in boundaries if 0 < boundary < len(lines))


def _path_affinity(left: str, right: str) -> tuple[int, str]:
    """Rank paths by actual shared structure without assuming an architectural style."""
    left_parts = Path(left).parts
    right_parts = Path(right).parts
    common_parent = 0
    for a, b in zip(left_parts[:-1], right_parts[:-1]):
        if a != b:
            break
        common_parent += 1
    left_stem = Path(left).stem.lower()
    right_stem = Path(right).stem.lower()
    left_test = re.sub(r"^(?:test_|tests?_)", "", left_stem)
    right_test = re.sub(r"^(?:test_|tests?_)", "", right_stem)
    stem_match = left_test == right_test or left_stem == right_test or right_stem == left_test
    score = common_parent * 10 + (8 if stem_match else 0)
    if Path(left).suffix.lower() == Path(right).suffix.lower():
        score += 1
    return score, right


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
            "authorization": new_workflow_authorization(
                self.target, self.workflow_id, float(budget), self.max_code_review_calls,
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

    @staticmethod
    def _code_review_header() -> str:
        return (
            "Perform an independent, read-only code review. Treat the implementation and approved proposal as claims "
            "to verify against the original assignment. Apply all shared review criteria and the specific scope for "
            "this part. Do not implement changes. Return a structured report with exactly these headings: `Part ID: "
            "<id>`, `Files reviewed`, `Findings`, `Cross-component concerns`, and `Overall assessment`. Format each "
            "finding with fields `Severity`, `Category`, `File/location`, `Description`, `Impact and reasoning`, and "
            "`Recommended correction`. Write `Findings: None` when there are none. Start with `Decision: APPROVED` "
            "unless an acceptance-blocking defect warrants `Decision: CHANGES_REQUESTED`."
        )

    @staticmethod
    def _review_segment(
        path: str, patch: str, start: int = 1, end: int | None = None, total: int | None = None,
    ) -> ReviewSegment:
        lines = patch.splitlines(keepends=True)
        end = len(lines) if end is None else end
        total = len(lines) if total is None else total
        return ReviewSegment(path, patch, start, end, total, _review_patch_sha256(patch))

    @staticmethod
    def _part_coverage(segments: list[ReviewSegment], *, excluded_count: int) -> str:
        lines = ["Every patch line listed below is included in this review request; no file content was truncated."]
        for segment in segments:
            line_description = (
                f"patch lines {segment.patch_line_start}-{segment.patch_line_end} of {segment.patch_line_count}"
            )
            if segment.patch_line_start == 1 and segment.patch_line_end == segment.patch_line_count:
                line_description = "complete patch"
            lines.append(
                f"- `{segment.path}` — {line_description}; SHA-256 `{segment.patch_sha256}`"
            )
        lines.append(f"Changed metadata/binary/protected paths explicitly excluded and recorded: {excluded_count}.")
        return "\n".join(lines)

    def _part_sections(
        self,
        shared: dict[str, str],
        segments: list[ReviewSegment],
        *,
        campaign_id: str,
        part_id: str,
        excluded_count: int,
    ) -> dict[str, str]:
        patch_lines = []
        for segment in segments:
            patch_lines.extend([
                f"FILE: {json.dumps(segment.path, ensure_ascii=True)}; patch lines "
                f"{segment.patch_line_start}-{segment.patch_line_end} of {segment.patch_line_count}; "
                f"segment SHA-256 {segment.patch_sha256}",
                segment.text,
            ])
        sections = {
            "Review metadata": (
                f"Code review campaign: {campaign_id}\nPart ID: {part_id}\n"
                f"Proposal version: {self.state.get('proposal_version', 1)}. This request contains all assigned patch lines. "
                "Return one structured individual-part report using the required format."
            ),
            **shared,
            "Review file coverage": self._part_coverage(segments, excluded_count=excluded_count),
            "Complete patch": "\n".join(patch_lines),
        }
        return sections

    @staticmethod
    def _serialized_review_fits(header: str, sections: dict[str, str], limit: int) -> bool:
        safe_header = redact(header)
        safe_sections = {
            name: redact(value) if name == "Complete patch" else redact(value).strip()
            for name, value in sections.items()
        }
        prompt = _format_context_prompt(safe_header, safe_sections)
        try:
            claude_reviewer.validate_review_invocation(prompt, "aily", limit=limit)
            return True
        except claude_reviewer.ReviewError as exc:
            if "above the configured" in str(exc):
                return False
            raise WorkflowError(str(exc)) from exc

    def _split_oversized_segment(
        self,
        path: str,
        patch: str,
        *,
        shared: dict[str, str],
        campaign_id: str,
        limit: int,
        excluded_count: int,
    ) -> list[ReviewSegment]:
        lines = patch.splitlines(keepends=True)
        safe_boundaries = _safe_patch_boundaries(lines, path)
        safe_boundaries.append(len(lines))
        safe_boundaries = sorted(set(safe_boundaries))
        chunks: list[tuple[int, int, str]] = []
        start = 0
        while start < len(lines):
            fitting_end = None
            for end in reversed([boundary for boundary in safe_boundaries if boundary > start]):
                candidate_text = "".join(lines[start:end])
                candidate = self._review_segment(path, candidate_text, start + 1, end, len(lines))
                if self._serialized_review_fits(
                    self._code_review_header(),
                    self._part_sections(
                        shared, [candidate], campaign_id=campaign_id, part_id="part-000001",
                        excluded_count=excluded_count,
                    ),
                    limit,
                ):
                    fitting_end = end
                    break
            if fitting_end is None:
                raise WorkflowError(
                    f"Changed file {path} cannot be split at a safe patch boundary to fit the configured "
                    f"{limit}-character payload. Its full relevant patch remains unreviewed; no source was truncated."
                )
            chunks.append((start + 1, fitting_end, "".join(lines[start:fitting_end])))
            start = fitting_end
        total = len(lines)
        return [
            self._review_segment(path, text, first, last, total)
            for first, last, text in chunks
        ]

    def _partition_review_segments(
        self,
        review_diff: ReviewDiff,
        shared: dict[str, str],
        *,
        campaign_id: str,
        limit: int,
    ) -> tuple[list[dict], bool]:
        header = self._code_review_header()
        probe = self._review_segment("review-context-probe.txt", "x")
        probe_sections = self._part_sections(
            shared, [probe], campaign_id=campaign_id, part_id="part-000001",
            excluded_count=len(review_diff.excluded_files),
        )
        if not self._serialized_review_fits(header, probe_sections, limit):
            raise WorkflowError(
                f"Required shared code-review context cannot fit within the configured {limit}-character "
                "payload even with a minimal patch. No files were truncated or omitted."
            )
        complete_segments = [self._review_segment(path, patch) for path, patch in review_diff.file_patches]
        full_sections = self._part_sections(
            shared, complete_segments, campaign_id=campaign_id, part_id="part-000001",
            excluded_count=len(review_diff.excluded_files),
        )
        if self._serialized_review_fits(header, full_sections, limit):
            parts = [{"part_id": "part-000001", "segments": complete_segments}]
            self._validate_review_partition(review_diff, parts)
            return parts, False

        segments: list[ReviewSegment] = []
        for path, patch in review_diff.file_patches:
            complete = self._review_segment(path, patch)
            singleton_sections = self._part_sections(
                shared, [complete], campaign_id=campaign_id, part_id="part-000001",
                excluded_count=len(review_diff.excluded_files),
            )
            if self._serialized_review_fits(header, singleton_sections, limit):
                segments.append(complete)
            else:
                segments.extend(self._split_oversized_segment(
                    path, patch, shared=shared, campaign_id=campaign_id, limit=limit,
                    excluded_count=len(review_diff.excluded_files),
                ))

        pending = sorted(segments, key=lambda item: (item.path, item.patch_line_start))
        planned: list[dict] = []
        while pending:
            seed = pending.pop(0)
            group = [seed]
            candidates = sorted(
                pending,
                key=lambda item: (
                    -max(_path_affinity(current.path, item.path)[0] for current in group),
                    item.path,
                    item.patch_line_start,
                ),
            )
            for candidate in candidates:
                trial = [*group, candidate]
                sections = self._part_sections(
                    shared, trial, campaign_id=campaign_id, part_id="part-000001",
                    excluded_count=len(review_diff.excluded_files),
                )
                if self._serialized_review_fits(header, sections, limit):
                    group.append(candidate)
                    pending.remove(candidate)
            part_id = f"part-{len(planned) + 1:06d}"
            planned.append({"part_id": part_id, "segments": group})

        for part in planned:
            sections = self._part_sections(
                shared, part["segments"], campaign_id=campaign_id, part_id=part["part_id"],
                excluded_count=len(review_diff.excluded_files),
            )
            if not self._serialized_review_fits(header, sections, limit):
                raise WorkflowError(
                    f"Review part {part['part_id']} exceeds the configured payload after serialization; "
                    "the complete review was not submitted."
                )
        self._validate_review_partition(review_diff, planned)
        return planned, True

    @staticmethod
    def _validate_review_partition(review_diff: ReviewDiff, parts: list[dict]) -> None:
        original = dict(review_diff.file_patches)
        assigned: dict[str, list[ReviewSegment]] = {path: [] for path in original}
        for part in parts:
            for segment in part["segments"]:
                if segment.path not in assigned:
                    raise WorkflowError(f"Review partition contains an unknown changed file: {segment.path}.")
                assigned[segment.path].append(segment)
        for path, patch in original.items():
            lines = patch.splitlines(keepends=True)
            segments = sorted(assigned[path], key=lambda item: item.patch_line_start)
            next_line = 1
            reconstructed = []
            for segment in segments:
                if (segment.patch_line_start != next_line
                        or segment.patch_line_count != len(lines)
                        or segment.patch_line_end < segment.patch_line_start
                        or segment.patch_line_end > len(lines)):
                    raise WorkflowError(
                        f"Review partition has a gap, overlap, or invalid range for {path}; "
                        "the file cannot be claimed as completely reviewed."
                    )
                expected = "".join(lines[segment.patch_line_start - 1:segment.patch_line_end])
                if segment.text != expected or _review_patch_sha256(segment.text) != segment.patch_sha256:
                    raise WorkflowError(f"Review partition content hash does not match the source patch for {path}.")
                reconstructed.append(segment.text)
                next_line = segment.patch_line_end + 1
            if next_line != len(lines) + 1 or "".join(reconstructed) != patch:
                raise WorkflowError(f"Review partition does not cover the complete changed file patch: {path}.")

    @staticmethod
    def _campaign_source_hash(review_diff: ReviewDiff) -> str:
        content = {
            "patches": list(review_diff.file_patches),
            "exclusions": list(review_diff.excluded_files),
        }
        encoded = json.dumps(content, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _campaign_part_manifest(parts: list[dict]) -> list[dict]:
        return [
            {
                "part_id": part["part_id"],
                "segments": [
                    {
                        "path": segment.path,
                        "patch_line_start": segment.patch_line_start,
                        "patch_line_end": segment.patch_line_end,
                        "patch_line_count": segment.patch_line_count,
                        "patch_sha256": segment.patch_sha256,
                    }
                    for segment in part["segments"]
                ],
            }
            for part in parts
        ]

    def _prepare_review_campaign(self, review_diff: ReviewDiff) -> tuple[dict, dict[str, str], list[dict]]:
        existing = self.state.get("review_campaign")
        campaign_limit = None
        if existing:
            campaign_id = existing["campaign_id"]
            partition_limit = existing.get(
                "partition_limit_characters", existing["payload_limit_characters"],
            )
            campaign_limit = existing["payload_limit_characters"]
            try:
                current_limit = claude_reviewer.max_review_payload_chars()
            except claude_reviewer.ReviewError as exc:
                raise WorkflowError(str(exc)) from exc
            if current_limit < campaign_limit:
                raise WorkflowError(
                    f"Current {CLAUDE_MAX_PAYLOAD_ENV} limit ({current_limit}) is below the saved review "
                    f"campaign limit ({campaign_limit}); restore the saved limit to resume safely."
                )
            limit = partition_limit
        else:
            try:
                limit = claude_reviewer.max_review_payload_chars()
            except claude_reviewer.ReviewError as exc:
                raise WorkflowError(str(exc)) from exc
            campaign_id = uuid.uuid4().hex

        shared = self._code_review_shared_context(review_diff)
        shared_hash = _review_patch_sha256(_format_context_prompt(
            redact(self._code_review_header()),
            {name: redact(value).strip() for name, value in shared.items()},
        ))
        source_hash = self._campaign_source_hash(review_diff)
        try:
            parts, multipart = self._partition_review_segments(
                review_diff, shared, campaign_id=campaign_id, limit=limit,
            )
        except WorkflowError as exc:
            if not existing:
                self.state["review_included_files"] = list(review_diff.included_paths)
                self.state["review_exclusions"] = [
                    {"path": _safe_review_path(path), "reason": reason}
                    for path, reason in review_diff.excluded_files
                ]
                self.state["review_files_not_reviewed"] = [
                    {"path": path, "reason": redact(str(exc))}
                    for path in review_diff.included_paths
                ]
                self.state["review_partition_failure"] = {
                    "campaign_id": campaign_id,
                    "source_patch_sha256": source_hash,
                    "shared_context_sha256": shared_hash,
                    "payload_limit_characters": limit,
                    "error": redact(str(exc)),
                }
                self.state["review_campaign_id"] = campaign_id
                self._save()
            raise
        part_manifest = self._campaign_part_manifest(parts)
        if existing:
            if (existing.get("source_patch_sha256") != source_hash
                    or existing.get("shared_context_sha256") != shared_hash
                    or existing.get("parts") != part_manifest):
                raise WorkflowError(
                    "The target diff, shared review context, or partition changed after the review campaign began. "
                    "No remaining review calls were made; start a newly authorized workflow for the changed review."
                )
            if current_limit > campaign_limit:
                existing["payload_limit_history"] = existing.get("payload_limit_history", []) + [
                    {"characters": current_limit, "updated_at": now()},
                ]
                existing["payload_limit_characters"] = current_limit
                self._save()
            if existing.get("required_calls", 0) > self.state["authorization"]["claude_aily"]["max_code_reviews"]:
                raise WorkflowError(
                    f"This saved review campaign requires {existing.get('required_calls')} Claude calls, but its "
                    f"persisted authorization allows {self.state['authorization']['claude_aily']['max_code_reviews']}. "
                    "The existing authorization was not expanded."
                )
            return existing, shared, parts

        required_calls = len(parts) + (1 if multipart else 0)
        authorization = self.state["authorization"]["claude_aily"]
        campaign = {
            "campaign_id": campaign_id,
            "status": "planned",
            "strategy": "multi_part_with_integration" if multipart else "single_call",
            "created_at": now(),
            "payload_limit_characters": limit,
            "partition_limit_characters": limit,
            "source_patch_sha256": source_hash,
            "shared_context_sha256": shared_hash,
            "required_calls": required_calls,
            "authorized_call_limit": authorization["max_code_reviews"],
            "parts": part_manifest,
            "part_reviews": [
                {"part_id": part["part_id"], "status": "pending", "report": None, "calls": []}
                for part in parts
            ],
            "integration_review": {
                "required": multipart,
                "status": "pending" if multipart else "not_required",
                "report": None,
                "calls": [],
            },
            "files_reviewed": [],
            "files_not_reviewed": [
                {"path": path, "reason": "assigned to a pending review part"}
                for path in review_diff.included_paths
            ],
            "excluded_files": [
                {"path": _safe_review_path(path), "reason": reason}
                for path, reason in review_diff.excluded_files
            ],
            "consolidated_report": None,
        }
        self.state["review_campaign"] = campaign
        self.state["review_campaign_id"] = campaign_id
        self.state["review_shared_context_sha256"] = shared_hash
        self.state["review_included_files"] = list(review_diff.included_paths)
        self.state["review_exclusions"] = campaign["excluded_files"]
        self.state["review_files_not_reviewed"] = campaign["files_not_reviewed"]
        self.state.pop("review_partition_failure", None)
        self._save()
        if required_calls > authorization["max_code_reviews"]:
            campaign["status"] = "blocked_authorization"
            campaign["files_not_reviewed"] = [
                {"path": path, "reason": "campaign exceeds its persisted Claude code-review call authorization"}
                for path in review_diff.included_paths
            ]
            self._save()
            raise WorkflowError(
                f"This review requires {required_calls} Claude calls (parts plus integration), but this workflow "
                f"authorizes {authorization['max_code_reviews']}. Existing authorization was not expanded."
            )
        campaign["status"] = "in_progress"
        self._save()
        return campaign, shared, parts

    def _sync_review_coverage(self, campaign: dict, review_diff: ReviewDiff) -> None:
        reviewed_segments = {
            (
                segment.get("path"), segment.get("patch_line_start"),
                segment.get("patch_line_end"), segment.get("patch_sha256"),
            )
            for part in campaign["part_reviews"] if part.get("status") == "reviewed"
            for segment in part.get("segments", [])
        }
        # Segment ownership is copied from the immutable part manifest at campaign creation.
        if not any(item.get("segments") for item in campaign["part_reviews"]):
            by_id = {item["part_id"]: item for item in campaign["part_reviews"]}
            for part in campaign["parts"]:
                by_id[part["part_id"]]["segments"] = part["segments"]
            reviewed_segments = {
                (
                    segment.get("path"), segment.get("patch_line_start"),
                    segment.get("patch_line_end"), segment.get("patch_sha256"),
                )
                for part in campaign["part_reviews"] if part.get("status") == "reviewed"
                for segment in part.get("segments", [])
            }
        expected_by_path: dict[str, set[tuple[str, int, int, str]]] = {}
        for part in campaign["parts"]:
            for segment in part["segments"]:
                identity = (
                    segment["path"], segment["patch_line_start"],
                    segment["patch_line_end"], segment["patch_sha256"],
                )
                expected_by_path.setdefault(segment["path"], set()).add(identity)
        campaign["files_reviewed"] = sorted(
            path for path, expected in expected_by_path.items() if expected <= reviewed_segments
        )
        campaign["files_not_reviewed"] = [
            {
                "path": path,
                "reason": next((
                    f"review part {part['part_id']} is {part['status']}"
                    for part in campaign["part_reviews"]
                    if any(segment["path"] == path for segment in part.get("segments", []))
                    and part["status"] != "reviewed"
                ), "one or more patch segments have not been reviewed"),
            }
            for path in sorted(expected_by_path) if path not in campaign["files_reviewed"]
        ]
        campaign["excluded_files"] = [
            {"path": _safe_review_path(path), "reason": reason}
            for path, reason in review_diff.excluded_files
        ]
        self.state["review_files_not_reviewed"] = campaign["files_not_reviewed"]

    @staticmethod
    def _part_report_is_structured(report: str, part_id: str, segments: list[ReviewSegment]) -> bool:
        if not re.search(rf"(?im)^\s*Part ID:\s*{re.escape(part_id)}\s*$", report):
            return False
        if not re.search(r"(?im)^\s*Decision:\s*(?:APPROVED|CHANGES_REQUESTED)\b", report):
            return False
        for heading in ("Files reviewed", "Findings", "Cross-component concerns", "Overall assessment"):
            if not re.search(
                    rf"(?im)^\s*(?:#+\s*)?{re.escape(heading)}\s*:?\s*(?:.*)?$", report):
                return False
        findings_match = re.search(
            r"(?ims)^\s*(?:#+\s*)?Findings\s*:?\s*(.*?)"
            r"(?=^\s*(?:#+\s*)?Cross-component concerns\b)", report,
        )
        if not findings_match:
            return False
        findings = findings_match.group(1).strip()
        if findings and not re.fullmatch(r"(?is)(?:none|no findings\.?|n/a)\s*", findings):
            for field in (
                "Severity", "Category", "File/location", "Description", "Impact and reasoning",
                "Recommended correction",
            ):
                if not re.search(rf"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?{re.escape(field)}(?:\*\*)?\s*:", findings):
                    return False
        files_match = re.search(
            r"(?ims)^\s*(?:#{1,6}\s*)?Files reviewed\s*:?\s*\n(.*?)"
            r"(?=^\s*(?:#{1,6}\s*)?(?:Findings|Cross-component concerns|Overall assessment)\b|\Z)",
            report,
        )
        if not files_match:
            return False
        listed_paths: set[str] = set()
        for line in files_match.group(1).splitlines():
            item_match = re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*?)\s*$", line)
            if not item_match:
                continue
            item = item_match.group(1)
            backticked_paths = re.findall(r"`([^`]+)`", item)
            if backticked_paths:
                listed_paths.update(backticked_paths)
            else:
                listed_paths.add(item.split(" — ", 1)[0].strip())
        assigned_paths = {segment.path for segment in segments}
        return assigned_paths <= listed_paths

    @staticmethod
    def _integration_report_is_structured(report: str) -> bool:
        return bool(
            re.search(r"(?im)^\s*Decision:\s*(?:APPROVED|CHANGES_REQUESTED)\b", report)
            and re.search(r"(?im)^\s*(?:#+\s*)?Consolidated findings\s*:?\s*(?:.*)?$", report)
            and re.search(r"(?im)^\s*(?:#+\s*)?Integration findings\s*:?\s*(?:.*)?$", report)
            and re.search(r"(?im)^\s*(?:#+\s*)?Overall assessment\s*:?\s*(?:.*)?$", report)
        )

    def _run_review_campaign(
        self, campaign: dict, shared: dict[str, str], parts: list[dict], review_diff: ReviewDiff,
    ) -> str:
        campaign["status"] = "in_progress"
        self._sync_review_coverage(campaign, review_diff)
        self._save()
        part_records = {record["part_id"]: record for record in campaign["part_reviews"]}
        for part in parts:
            record = part_records[part["part_id"]]
            if record["status"] == "reviewed":
                continue
            if record["status"] == "report_saved" and record.get("report"):
                record["status"] = (
                    "reviewed" if self._part_report_is_structured(
                        record["report"], part["part_id"], part["segments"],
                    ) else "invalid_report"
                )
                record["segments"] = self._campaign_part_manifest([part])[0]["segments"]
                self._sync_review_coverage(campaign, review_diff)
                self._save()
                if record["status"] == "reviewed":
                    continue
            recoverable = next((
                context for context in reversed(self.state.get("claude_contexts", []))
                if context.get("campaign_id") == campaign["campaign_id"]
                and context.get("part_id") == part["part_id"]
                and context.get("call_state") == "completed"
                and context.get("report") is not None
                and not any(call.get("prompt_sha256") == context.get("prompt_sha256")
                            for call in record.get("calls", []))
            ), None)
            if recoverable is not None:
                report = recoverable["report"]
                record["calls"].append({
                    "prompt_sha256": recoverable["prompt_sha256"], "cost_usd": recoverable.get("cost_usd"),
                    "call_state": recoverable["call_state"],
                    "campaign_call_number": recoverable.get("campaign_call_number"),
                })
                record["report"] = report
                record["context_sha256"] = recoverable["prompt_sha256"]
                record["status"] = (
                    "reviewed" if self._part_report_is_structured(report, part["part_id"], part["segments"])
                    else "invalid_report"
                )
                record["segments"] = self._campaign_part_manifest([part])[0]["segments"]
                self._sync_review_coverage(campaign, review_diff)
                self._save()
                if record["status"] != "reviewed":
                    raise WorkflowError(
                        f"Persisted Claude output for {part['part_id']} is not a complete structured report."
                    )
                continue
            sections = self._part_sections(
                shared, part["segments"], campaign_id=campaign["campaign_id"],
                part_id=part["part_id"], excluded_count=len(review_diff.excluded_files),
            )
            patch_hash = _review_patch_sha256(sections["Complete patch"])
            try:
                report, manifest = self._call_claude(
                    self._code_review_header(), "code_review", sections,
                    metadata={
                        "proposal_version": self.state.get("proposal_version", 1),
                        "header_id": "code-review-part-v1",
                        "review_kind": "part",
                        "campaign_id": campaign["campaign_id"],
                        "part_id": part["part_id"],
                        "shared_context_sha256": campaign["shared_context_sha256"],
                        "part_patch_sha256": patch_hash,
                        "payload_limit_chars": campaign["payload_limit_characters"],
                        "included_files": sorted({segment.path for segment in part["segments"]}),
                        "excluded_files": campaign["excluded_files"],
                    },
                )
            except Exception:
                context = next((
                    item for item in reversed(self.state.get("claude_contexts", []))
                    if item.get("campaign_id") == campaign["campaign_id"]
                    and item.get("part_id") == part["part_id"]
                ), None)
                if context is not None and not any(
                        call.get("prompt_sha256") == context.get("prompt_sha256") for call in record["calls"]):
                    record["calls"].append({
                        "prompt_sha256": context.get("prompt_sha256"), "cost_usd": context.get("cost_usd"),
                        "call_state": context.get("call_state"),
                        "campaign_call_number": context.get("campaign_call_number"),
                    })
                if context is not None and context.get("call_state") == "completed" and context.get("report"):
                    record["report"] = context["report"]
                    record["context_sha256"] = context.get("prompt_sha256")
                    record["status"] = "report_saved"
                else:
                    record["status"] = "call_failed"
                record["segments"] = self._campaign_part_manifest([part])[0]["segments"]
                self._sync_review_coverage(campaign, review_diff)
                self._save()
                raise
            record["calls"].append({
                "prompt_sha256": manifest["prompt_sha256"], "cost_usd": manifest.get("cost_usd"),
                "call_state": manifest["call_state"], "campaign_call_number": manifest.get("campaign_call_number"),
            })
            record["report"] = report
            record["context_sha256"] = manifest["prompt_sha256"]
            record["status"] = (
                "reviewed" if self._part_report_is_structured(report, part["part_id"], part["segments"])
                else "invalid_report"
            )
            record["segments"] = self._campaign_part_manifest([part])[0]["segments"]
            self._sync_review_coverage(campaign, review_diff)
            self._save()
            if record["status"] != "reviewed":
                raise WorkflowError(
                    f"Claude returned an incomplete structured report for {part['part_id']}; "
                    "the part remains unreviewed and may be retried only within the saved call and budget limits."
                )

        if len(parts) == 1:
            campaign["integration_review"]["status"] = "not_required"
            if campaign["files_not_reviewed"]:
                raise WorkflowError("The single-part review cannot complete while changed files remain unreviewed.")
            campaign["status"] = "complete"
            campaign["consolidated_report"] = part_records[parts[0]["part_id"]]["report"]
            self.state["review_campaign_call_count"] = sum(
                call.get("call_state") != "validation_failed"
                for call in part_records[parts[0]["part_id"]].get("calls", [])
            )
            self._save()
            return campaign["consolidated_report"]

        integration = campaign["integration_review"]
        if integration["status"] == "report_saved" and integration.get("report"):
            integration["status"] = (
                "reviewed" if self._integration_report_is_structured(integration["report"])
                else "invalid_report"
            )
            self._save()
        if integration["status"] != "reviewed":
            part_reports = "\n\n".join(
                f"### Part {part['part_id']} report\n{part_records[part['part_id']]['report']}"
                for part in parts
            )
            coverage = "\n".join(
                f"- `{path}` reviewed across its assigned patch segment(s)"
                for path in campaign["files_reviewed"]
            )
            coverage += "\nFiles not reviewed: " + (
                "; ".join(f"{item['path']}: {item['reason']}" for item in campaign["files_not_reviewed"])
                if campaign["files_not_reviewed"] else "none"
            )
            sections = {
                "Review metadata": (
                    f"Code review campaign {campaign['campaign_id']}; final integration review; "
                    f"{len(parts)} individual parts; shared context SHA-256 {campaign['shared_context_sha256']}."
                ),
                **shared,
                "Review file coverage": coverage,
                "Individual part reports": part_reports,
            }
            integration_header = (
                "Perform the final independent integration review for this multi-part campaign. Synthesize the "
                "individual reports and shared architecture context. Examine contracts, boundaries, data flow, "
                "inter-part assumptions, inconsistent error/state behavior, duplicated responsibilities, and defects "
                "that only appear across components. Deduplicate findings while preserving references. Add new "
                "integration findings only with evidence in the reports/context. Do not repeat local reviews."
                " Return `Decision: APPROVED` or `Decision: CHANGES_REQUESTED`, then use the exact headings "
                "`Consolidated findings`, `Integration findings`, and `Overall assessment`. Preserve original finding "
                "references and severity; state explicitly when a section has none."
            )
            # The integration payload is itself reviewed against the exact configured size before submission.
            integration_sections = {
                **sections,
                "Individual part reports": (
                    sections["Individual part reports"] + "\n\nShared module/dependency map:\n"
                    + shared["Repository/module map and dependencies"]
                ),
            }
            recoverable = next((
                context for context in reversed(self.state.get("claude_contexts", []))
                if context.get("campaign_id") == campaign["campaign_id"]
                and context.get("part_id") == "integration"
                and context.get("call_state") == "completed"
                and context.get("report") is not None
                and not any(call.get("prompt_sha256") == context.get("prompt_sha256")
                            for call in integration.get("calls", []))
            ), None)
            if recoverable is not None:
                report = recoverable["report"]
                integration["calls"].append({
                    "prompt_sha256": recoverable["prompt_sha256"], "cost_usd": recoverable.get("cost_usd"),
                    "call_state": recoverable["call_state"],
                    "campaign_call_number": recoverable.get("campaign_call_number"),
                })
                integration["report"] = report
                integration["context_sha256"] = recoverable["prompt_sha256"]
                integration["status"] = (
                    "reviewed" if self._integration_report_is_structured(report) else "invalid_report"
                )
                self._save()
                if integration["status"] != "reviewed":
                    raise WorkflowError("Persisted Claude integration output is not a complete structured report.")
            else:
                try:
                    report, manifest = self._call_claude(
                        integration_header, "code_review", integration_sections,
                        metadata={
                            "proposal_version": self.state.get("proposal_version", 1),
                            "header_id": "code-review-integration-v1",
                            "review_kind": "integration",
                            "campaign_id": campaign["campaign_id"],
                            "part_id": "integration",
                            "shared_context_sha256": campaign["shared_context_sha256"],
                            "payload_limit_chars": campaign["payload_limit_characters"],
                            "included_files": campaign["files_reviewed"],
                            "excluded_files": campaign["excluded_files"],
                        },
                    )
                except Exception:
                    context = next((
                        item for item in reversed(self.state.get("claude_contexts", []))
                        if item.get("campaign_id") == campaign["campaign_id"]
                        and item.get("part_id") == "integration"
                    ), None)
                    if context is not None and not any(
                            call.get("prompt_sha256") == context.get("prompt_sha256")
                            for call in integration["calls"]):
                        integration["calls"].append({
                            "prompt_sha256": context.get("prompt_sha256"), "cost_usd": context.get("cost_usd"),
                            "call_state": context.get("call_state"),
                            "campaign_call_number": context.get("campaign_call_number"),
                        })
                    if context is not None and context.get("call_state") == "completed" and context.get("report"):
                        integration["report"] = context["report"]
                        integration["context_sha256"] = context.get("prompt_sha256")
                        integration["status"] = "report_saved"
                    else:
                        integration["status"] = "call_failed"
                    self._save()
                    raise
                integration["calls"].append({
                    "prompt_sha256": manifest["prompt_sha256"], "cost_usd": manifest.get("cost_usd"),
                    "call_state": manifest["call_state"],
                    "campaign_call_number": manifest.get("campaign_call_number"),
                })
                integration["report"] = report
                integration["context_sha256"] = manifest["prompt_sha256"]
                integration["status"] = (
                    "reviewed" if self._integration_report_is_structured(report) else "invalid_report"
                )
                self._save()
                if integration["status"] != "reviewed":
                    raise WorkflowError(
                        "Claude returned an incomplete integration report; campaign remains incomplete."
                    )

        if campaign["files_not_reviewed"] or any(
                part_records[part["part_id"]]["status"] != "reviewed" for part in parts):
            raise WorkflowError("The review campaign cannot complete while any required part remains unreviewed.")
        campaign["consolidated_report"] = integration["report"]
        campaign["status"] = "complete"
        self.state["review_campaign_call_count"] = sum(
            sum(call.get("call_state") != "validation_failed" for call in record.get("calls", []))
            for record in campaign["part_reviews"]
        ) + sum(
            call.get("call_state") != "validation_failed" for call in integration.get("calls", [])
        )
        self._save()
        return campaign["consolidated_report"]

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


def _task_text(args: argparse.Namespace) -> str:
    if bool(args.task) == bool(args.task_file):
        raise WorkflowError("Supply exactly one of --task or --task-file.")
    if args.task is not None:
        return args.task
    try:
        return Path(args.task_file).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"Could not read task file: {args.task_file}") from exc


def _code_review_calls_arg(value: str) -> int:
    try:
        calls = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= calls <= MAX_AUTHORIZED_CODE_REVIEW_CALLS:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_AUTHORIZED_CODE_REVIEW_CALLS}"
        )
    return calls


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
                "campaign_id": context.get("campaign_id"),
                "review_kind": context.get("review_kind"),
                "part_id": context.get("part_id"),
                "characters": context.get("characters"),
                "utf8_bytes": context.get("utf8_bytes"),
                "payload_limit_characters": context.get("payload_limit_characters"),
                "cost_usd": context.get("cost_usd"),
                "prompt_sha256": context.get("prompt_sha256"),
                "sections": [section.get("name") for section in context.get("sections", [])],
                "complete": context.get("complete"),
                "call_state": context.get("call_state"),
            }
            for context in state.get("claude_contexts", [])
        ],
        "spent_usd": external_authorization.get("spent_usd"),
        "effective_budget_usd": external_authorization.get("budget_usd"),
        "authorized_code_review_calls": external_authorization.get("max_code_reviews"),
        "review_campaign_status": state.get("review_campaign", {}).get("status"),
        "review_files_not_reviewed": state.get("review_files_not_reviewed", []),
        "review_partition_failure": state.get("review_partition_failure"),
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
    start.add_argument(
        "--max-code-review-calls", type=_code_review_calls_arg, default=DEFAULT_MAX_CODE_REVIEW_CALLS,
        help=(f"Maximum calls in the Claude code-review campaign, including integration review "
              f"(default: {DEFAULT_MAX_CODE_REVIEW_CALLS}; maximum: {MAX_AUTHORIZED_CODE_REVIEW_CALLS})"),
    )
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
                max_code_review_calls=args.max_code_review_calls,
                workflow_id=args.workflow_id, codex=args.codex, aily=args.aily, model=args.model,
            )
            print(f"Effective Claude/Aily budget: ${runner.budget_usd:.2f}")
            print(f"Authorized Claude code-review campaign calls: {runner.max_code_review_calls}")
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

"""Git, repository discovery, and workspace guidance helpers."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable


class WorkspaceError(RuntimeError):
    """An actionable workspace inspection or validation error."""


WORKFLOW_ROOT = ".engineering/workflows"


WORKFLOW_LOCK_NAME_RE = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.lock$")


SENSITIVE_NAMES = {
    ".aws", ".ssh", ".env", ".env.local", ".env.production", "credentials",
    "credentials.json", "secrets", "secrets.json", "id_rsa", "id_ed25519",
}


def git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"})
    return env


def git(
    target: Path, *args: str, check: bool = True, redactor: Callable[[str], str] | None = None,
) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.pager=cat", "-c", "maintenance.auto=false", "-C", str(target), *args],
        cwd=target,
        env=git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        if redactor is not None:
            detail = redactor(detail)
        raise WorkspaceError(f"Git command failed ({' '.join(shlex.quote(part) for part in args)}): {detail}")
    return result.stdout


def canonical_target(value: str, *, redactor: Callable[[str], str] | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise WorkspaceError("--target must be an absolute repository path.")
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"Target path is unavailable: {path}") from exc
    if not root.is_dir():
        raise WorkspaceError("Target must be a directory.")
    reported = git(root, "rev-parse", "--show-toplevel", redactor=redactor).decode("utf-8", "strict").strip()
    if Path(reported).resolve(strict=True) != root:
        raise WorkspaceError("Target must be the canonical Git repository root.")
    return root


def changed_paths(
    target: Path, baseline: str, *, redactor: Callable[[str], str] | None = None,
) -> tuple[list[str], list[str]]:
    tracked = git(target, "diff", "--name-only", "-z", baseline, "--", redactor=redactor).decode("utf-8", "surrogateescape").split("\0")
    untracked = git(target, "ls-files", "--others", "--exclude-standard", "-z", redactor=redactor).decode("utf-8", "surrogateescape").split("\0")
    lock_prefix = f"{WORKFLOW_ROOT}/"
    user_paths = {
        item for item in untracked
        if item and not (
            item.startswith(lock_prefix)
            and "/" not in item[len(lock_prefix):]
            and WORKFLOW_LOCK_NAME_RE.fullmatch(item[len(lock_prefix):])
        )
    }
    return sorted({item for item in tracked if item}), sorted(user_paths)


def protected_paths(
    target: Path, baseline: str, *, redactor: Callable[[str], str] | None = None,
) -> list[str]:
    tracked, untracked = changed_paths(target, baseline, redactor=redactor)
    return sorted(set(tracked + untracked))


def read_instruction_files(target: Path) -> list[dict[str, str]]:
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


def skill_metadata(path: Path, content: str) -> tuple[str, str]:
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


def project_skills(target: Path, task: str) -> list[dict[str, str]]:
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
        name, description = skill_metadata(path, content)
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


def project_guidance(
    target: Path, task: str, *, redactor: Callable[[str], str] | None = None,
) -> dict:
    files = git(target, "ls-files", "-co", "--exclude-standard", "-z", redactor=redactor).decode(
        "utf-8", "surrogateescape").split("\0")
    visible_files = sorted({
        path for path in files if path and not path.startswith(f"{WORKFLOW_ROOT}/")
        and ".git" not in Path(path).parts and not is_sensitive(path)
        and Path(path).name != ".DS_Store"
    })
    instructions = read_instruction_files(target)
    skills = project_skills(target, task)
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


def is_sensitive(relative: str) -> bool:
    return any(part.lower() in SENSITIVE_NAMES for part in Path(relative).parts)


def has_symlink_component(target: Path, relative: str) -> bool:
    current = target
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False

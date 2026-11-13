"""Git, repository discovery, and workspace guidance helpers."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Callable


class WorkspaceError(RuntimeError):
    """An actionable workspace inspection or validation error."""


WORKFLOW_ROOT = ".engineering/workflows"


WORKFLOW_LOCK_NAME_RE = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.lock$")
FINGERPRINT_VERSION = 1
FINGERPRINT_ALGORITHM = "sha256-length-prefixed-head-index-worktree-v1"
FINGERPRINT_EXCLUDED_INTERNAL_PATHS = [".git at any depth", f"{WORKFLOW_ROOT}/**"]


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
    read_only: bool = False,
) -> bytes:
    env = git_env()
    if read_only:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-c", "core.pager=cat", "-c", "maintenance.auto=false", "-C", str(target), *args],
        cwd=target,
        env=env,
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


def _fingerprint_git(
    target: Path, *args: str, redactor: Callable[[str], str] | None = None,
) -> bytes:
    return git(target, *args, redactor=redactor, read_only=True)


def _fingerprint_internal_path(path: bytes) -> bool:
    parts = path.split(b"/")
    return b".git" in parts or path == WORKFLOW_ROOT.encode() or path.startswith(WORKFLOW_ROOT.encode() + b"/")


def _fingerprint_sensitive_path(path: bytes) -> bool:
    relative = os.fsdecode(path)
    name = Path(relative).name.lower()
    return is_sensitive(relative) or name.startswith(".env.")


def fingerprint_path_is_sensitive(path: str | bytes) -> bool:
    """Return whether a relative repository path is excluded from content hashing."""
    return _fingerprint_sensitive_path(os.fsencode(path) if isinstance(path, str) else path)


def _fingerprint_field(digest, name: bytes, value: bytes) -> None:
    digest.update(len(name).to_bytes(2, "big"))
    digest.update(name)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _parse_ls_tree(output: bytes) -> dict[bytes, list[tuple[bytes, bytes, bytes]]]:
    entries: dict[bytes, list[tuple[bytes, bytes, bytes]]] = {}
    for record in output.split(b"\0"):
        if not record:
            continue
        header, separator, path = record.partition(b"\t")
        fields = header.split(b" ")
        if not separator or len(fields) != 3:
            raise WorkspaceError("Git returned an invalid tree record during fingerprinting.")
        mode, object_type, object_id = fields
        entries.setdefault(path, []).append((mode, object_type, object_id))
    return entries


def _parse_index(output: bytes) -> dict[bytes, list[tuple[bytes, bytes, bytes]]]:
    entries: dict[bytes, list[tuple[bytes, bytes, bytes]]] = {}
    for record in output.split(b"\0"):
        if not record:
            continue
        header, separator, path = record.partition(b"\t")
        fields = header.split(b" ")
        if not separator or len(fields) != 3:
            raise WorkspaceError("Git returned an invalid index record during fingerprinting.")
        mode, object_id, stage = fields
        entries.setdefault(path, []).append((mode, object_id, stage))
    return entries


def _status_flags(output: bytes) -> tuple[bool, bool, bool, int]:
    """Return Git status across non-internal paths, including sensitive paths."""
    staged = False
    unstaged = False
    untracked = 0
    for record in output.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise WorkspaceError("Git returned an invalid status record during fingerprinting.")
        path = record[3:]
        if _fingerprint_internal_path(path):
            continue
        status = record[:2]
        if status == b"??":
            untracked += 1
        else:
            staged = staged or status[0:1] != b" "
            unstaged = unstaged or status[1:2] != b" "
    return staged, unstaged, untracked > 0, untracked


def _hash_worktree_path(
    root: Path,
    path: bytes,
    index_entries: list[tuple[bytes, bytes, bytes]],
    *,
    redactor=None,
    depth: int = 0,
) -> tuple[bytes, bytes, bytes, bytes]:
    full_path = root / os.fsdecode(path)
    parent = root
    for component in Path(os.fsdecode(path)).parts[:-1]:
        parent = parent / component
        try:
            parent_mode = parent.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(parent_mode):
            raise WorkspaceError("A repository path traverses a symbolic link during fingerprinting.")
    try:
        metadata = full_path.lstat()
    except FileNotFoundError:
        return b"missing", b"", b"0", b""
    mode = stat.S_IMODE(metadata.st_mode)
    mode_bytes = format(mode, "04o").encode("ascii")
    if stat.S_ISLNK(metadata.st_mode):
        link_target = os.fsencode(os.readlink(full_path))
        return b"symlink", mode_bytes, str(len(link_target)).encode("ascii"), hashlib.sha256(link_target).digest()
    if stat.S_ISREG(metadata.st_mode):
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(full_path, flags)
        except OSError as exc:
            raise WorkspaceError("A repository file could not be read for fingerprinting.") from exc
        file_hash = hashlib.sha256()
        size = 0
        try:
            before = os.fstat(descriptor)
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                file_hash.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns or size != after.st_size):
            raise WorkspaceError("A repository file changed while its fingerprint was being calculated.")
        return b"file", mode_bytes, str(size).encode("ascii"), file_hash.digest()
    if stat.S_ISDIR(metadata.st_mode) and any(entry[0] == b"160000" for entry in index_entries):
        if depth >= 16:
            raise WorkspaceError("Nested submodules exceed the fingerprint recursion limit.")
        submodule = repository_fingerprint(full_path, redactor=redactor, _depth=depth + 1)
        details = (
            f"{submodule['commit_sha']}:{submodule['working_tree_sha256']}:{submodule['working_tree_clean']}"
        ).encode("ascii")
        return b"submodule", mode_bytes, b"0", hashlib.sha256(details).digest()
    if stat.S_ISDIR(metadata.st_mode):
        return b"directory", mode_bytes, b"0", b""
    # Git does not store these objects' contents; record only their kind and mode, never open them.
    if stat.S_ISFIFO(metadata.st_mode):
        kind = b"fifo"
    elif stat.S_ISSOCK(metadata.st_mode):
        kind = b"socket"
    elif stat.S_ISCHR(metadata.st_mode):
        kind = b"character_device"
    elif stat.S_ISBLK(metadata.st_mode):
        kind = b"block_device"
    else:
        kind = b"special"
    return kind, mode_bytes, b"0", b""


def repository_fingerprint(
    root: Path, *, redactor: Callable[[str], str] | None = None, expected_commit: str | None = None,
    _depth: int = 0,
) -> dict:
    """Capture the v1 Git identity, scoped status, and relevant file contents without repository writes.

    The v1 content digest deliberately includes the complete HEAD commit ID even though sensitive paths are
    omitted from per-path content fields. Git status indicators and the excluded-path count also cover sensitive
    paths; they reveal presence/status only, not their names or contents.
    """
    root = Path(root).resolve(strict=True)
    top_level = _fingerprint_git(root, "rev-parse", "--show-toplevel", redactor=redactor).decode(
        "utf-8", "strict",
    ).strip()
    canonical_root = Path(top_level).resolve(strict=True)
    if canonical_root != root:
        raise WorkspaceError("Fingerprint root must be the canonical Git repository root.")
    commit_sha = _fingerprint_git(root, "rev-parse", "HEAD", redactor=redactor).decode("ascii", "strict").strip()
    if expected_commit is not None and commit_sha != expected_commit:
        raise WorkspaceError("Repository HEAD changed while the initial fingerprint was being captured.")

    head_entries = _parse_ls_tree(_fingerprint_git(
        root, "ls-tree", "-r", "-z", "--full-tree", "HEAD", redactor=redactor,
    ))
    index_listing = _fingerprint_git(root, "ls-files", "--stage", "-z", redactor=redactor)
    index_entries = _parse_index(index_listing)
    untracked_paths = {
        path for path in _fingerprint_git(
            root, "ls-files", "--others", "--exclude-standard", "-z", redactor=redactor,
        )
        .split(b"\0") if path
    }
    status = _fingerprint_git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
        "--ignore-submodules=none", redactor=redactor,
    )
    # Status is repository-wide apart from generated workflow internals; sensitive paths affect these flags.
    staged_changes, unstaged_changes, has_untracked_status, _untracked_status_count = _status_flags(status)
    relevant_status = []
    for record in status.split(b"\0"):
        if not record:
            continue
        path = record[3:]
        if not _fingerprint_internal_path(path):
            relevant_status.append(record)
    working_tree_clean = not relevant_status

    all_paths = set(head_entries) | set(index_entries) | untracked_paths
    relevant_paths = sorted(
        path for path in all_paths
        if not _fingerprint_internal_path(path) and not _fingerprint_sensitive_path(path)
    )
    digest = hashlib.sha256()
    digest.update(b"workflow-repository-fingerprint-v1\0")
    # Keep the full Git commit identity in v1; it covers committed sensitive paths as part of HEAD.
    _fingerprint_field(digest, b"commit", commit_sha.encode("ascii"))
    for path in relevant_paths:
        _fingerprint_field(digest, b"path", path)
        head_records = sorted(head_entries.get(path, []))
        _fingerprint_field(digest, b"head_count", str(len(head_records)).encode("ascii"))
        for mode, object_type, object_id in head_records:
            _fingerprint_field(digest, b"head_mode", mode)
            _fingerprint_field(digest, b"head_type", object_type)
            _fingerprint_field(digest, b"head_object", object_id)
        index_records = sorted(index_entries.get(path, []))
        _fingerprint_field(digest, b"index_count", str(len(index_records)).encode("ascii"))
        for mode, object_id, stage in index_records:
            _fingerprint_field(digest, b"index_mode", mode)
            _fingerprint_field(digest, b"index_object", object_id)
            _fingerprint_field(digest, b"index_stage", stage)
        _fingerprint_field(digest, b"untracked", b"1" if path in untracked_paths else b"0")
        kind, mode, size, content_sha256 = _hash_worktree_path(
            root, path, index_records, redactor=redactor, depth=_depth,
        )
        _fingerprint_field(digest, b"worktree_kind", kind)
        _fingerprint_field(digest, b"worktree_mode", mode)
        _fingerprint_field(digest, b"worktree_size", size)
        _fingerprint_field(digest, b"worktree_content_sha256", content_sha256)

    commit_after = _fingerprint_git(root, "rev-parse", "HEAD", redactor=redactor).decode("ascii", "strict").strip()
    index_after = _fingerprint_git(root, "ls-files", "--stage", "-z", redactor=redactor)
    untracked_after = _fingerprint_git(
        root, "ls-files", "--others", "--exclude-standard", "-z", redactor=redactor,
    )
    status_after = _fingerprint_git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
        "--ignore-submodules=none", redactor=redactor,
    )
    if commit_after != commit_sha or index_after != index_listing or status_after != status:
        raise WorkspaceError("Git repository metadata changed during fingerprint calculation.")
    if {
        path for path in untracked_after.split(b"\0") if path
    } != untracked_paths:
        raise WorkspaceError("Untracked repository files changed during fingerprint calculation.")

    return {
        "version": FINGERPRINT_VERSION,
        "algorithm": FINGERPRINT_ALGORITHM,
        "repository_root": str(canonical_root),
        "commit_sha": commit_sha,
        "working_tree_sha256": digest.hexdigest(),
        "working_tree_clean": working_tree_clean,
        "staged_changes": staged_changes,
        "unstaged_changes": unstaged_changes,
        "untracked_files": has_untracked_status,
        "tracked_path_count": len({
            path for path in relevant_paths if path in head_entries or path in index_entries
        }),
        "untracked_path_count": len({path for path in relevant_paths if path in untracked_paths}),
        # A count discloses only how many excluded paths exist, not their names or contents.
        "excluded_sensitive_path_count": len({
            path for path in all_paths
            if not _fingerprint_internal_path(path) and _fingerprint_sensitive_path(path)
        }),
        "excluded_internal_paths": FINGERPRINT_EXCLUDED_INTERNAL_PATHS.copy(),
        "excluded_sensitive_paths": "known sensitive path components and .env.* basenames",
        "ignored_untracked_files_excluded": True,
        "tracked_ignored_files_included": True,
    }


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


def target_change_inventory(
    target: Path,
    baseline: str | None,
    preexisting_paths: Iterable[str],
    *,
    redactor: Callable[[str], str] | None = None,
) -> list[dict[str, str]]:
    """Classify target repository changes relative to a saved Git baseline."""
    if not isinstance(baseline, str) or not baseline:
        return []
    changes: dict[str, str] = {}
    raw = git(
        target, "diff", "--name-status", "-z", "--no-renames", baseline, "--", redactor=redactor,
    )
    fields = raw.decode("utf-8", "surrogateescape").split("\0")
    for index in range(0, len(fields) - 1, 2):
        status, path = fields[index:index + 2]
        if path:
            changes[path] = {
                "D": "deleted", "A": "added", "M": "modified", "T": "type_changed",
            }.get(status[:1], "modified")
    raw_untracked = git(
        target, "ls-files", "--others", "--exclude-standard", "-z", "--", redactor=redactor,
    )
    for path in raw_untracked.decode("utf-8", "surrogateescape").split("\0"):
        if path:
            changes.setdefault(path, "untracked")
    preexisting = set(preexisting_paths)
    return [
        {"path": path, "change": changes[path], "preexisting": "yes" if path in preexisting else "no"}
        for path in sorted(changes)
        if path != WORKFLOW_ROOT and not path.startswith(WORKFLOW_ROOT + "/")
    ]


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

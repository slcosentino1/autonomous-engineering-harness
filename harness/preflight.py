#!/usr/bin/env python3
"""Read-only target repository preflight for the Autonomous Engineering Harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any


POLICY_GIT_REJECT = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_NAMESPACE",
    "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE",
}
POLICY_GIT_NON_IDENTITY = {
    "GIT_PAGER", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_ASKPASS",
    "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_TRACE", "GIT_TRACE_SETUP",
    "GIT_TRACE_PACKET", "GIT_TRACE_PERFORMANCE", "GIT_EXEC_PATH", "GIT_PREFIX",
    "GIT_CONFIG_NOSYSTEM",
}
INSTRUCTION_FILES = {
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", "CODEX.md", "CONTRIBUTING", "CONTRIBUTING.md",
}
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "vendor", ".venv", "venv", "dist", "build"}
CONFIG_KEYS = {
    "target_path", "included_paths", "code_paths", "artifact_paths", "revision_paths",
    "excluded_paths", "workspace_root", "instruction_roots", "authorized_working_directories",
}


class PreflightError(Exception):
    def __init__(self, message: str, status: str = "FAIL") -> None:
        super().__init__(message)
        self.status = status


def canonical(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=True)


def canonical_authorized_root(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise PreflightError(f"{field} must be a non-empty absolute canonical path")
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        raise PreflightError(f"{field} must be an absolute canonical path")
    resolved = supplied.resolve(strict=True)
    if str(supplied) != str(resolved):
        raise PreflightError(f"{field} must be supplied in canonical form: {resolved}")
    return resolved


def path_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def is_path_or_child(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def paths_overlap_by_component(left: str, right: str) -> bool:
    return is_path_or_child(left, right) or is_path_or_child(right, left)


def config_relative(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PreflightError(f"{field} entries must be non-empty strings")
    if ("\\" in value or value.startswith("/") or
            (len(value) >= 2 and value[0].isalpha() and value[1] == ":") or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise PreflightError(f"{field} path must be repository-relative POSIX form: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or str(PurePosixPath(value)) != value:
        raise PreflightError(f"{field} path is not normalized: {value!r}")
    return value


class Preflight:
    def __init__(self, target_arg: str, harness_arg: str | None, config_arg: str | None,
                 workspace_arg: str | None) -> None:
        self.target_arg = target_arg
        self.harness_arg = harness_arg
        self.config_arg = config_arg
        self.workspace_arg = workspace_arg
        self.checks: list[dict[str, Any]] = []
        self.report: dict[str, Any] = {"report_version": 1, "checks": self.checks}
        self.config: dict[str, Any] = {}
        self.config_loaded = True
        self.target: Path | None = None
        self.harness: Path | None = None
        self.git_env: dict[str, str] | None = None
        self.git_repos: dict[str, dict[str, Any]] = {}

    def add(self, check_id: str, status: str, explanation: str, *, paths: Any = None,
            values: Any = None, evidence: Any = None, limitations: Any = None) -> None:
        if status == "NEEDS_HUMAN_INPUT":
            category = "human_input_required"
        elif status == "FAIL":
            if check_id in {"config.input", "target.path", "scope.validation", "discovery.instructions"}:
                category = "unsafe_condition" if "symlink" in explanation.lower() or "outside target" in explanation.lower() else "invalid_input"
            elif check_id == "git.identity.target" and ("no readable repository .git marker" in explanation or "not a non-bare Git worktree" in explanation):
                category = "invalid_input"
            else:
                category = "unsafe_condition"
        else:
            category = "observation"
        row: dict[str, Any] = {"check_id": check_id, "status": status, "explanation": explanation,
                               "category": category,
                               "paths": paths or {}, "values": values or {}, "evidence": evidence or {},
                               "limitations": limitations or []}
        self.checks.append(row)

    def load_config(self) -> None:
        if not self.config_arg:
            self.config = {}
            self.add("config.input", "PASS", "No optional config supplied; write scopes default to empty.",
                     evidence={"config": None, "default_write_scope": "empty"})
            return
        try:
            path = canonical(self.config_arg)
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise PreflightError("Config JSON must contain an object")
            extra = sorted(set(data) - CONFIG_KEYS)
            if extra:
                raise PreflightError(f"Unsupported config keys: {extra}")
            self.config = data
            self.add("config.input", "PASS", "Read optional JSON preflight config.", paths={"config": str(path)},
                     evidence={"keys": sorted(data.keys())})
        except (OSError, json.JSONDecodeError, PreflightError) as exc:
            self.config = {}
            self.config_loaded = False
            self.add("config.input", "FAIL", str(exc), paths={"config": self.config_arg})

    def identify_roots(self) -> None:
        try:
            self.target = canonical(self.target_arg)
            if not self.target.is_dir():
                raise PreflightError("Target path is not a directory")
            self.report["target_path"] = str(self.target)
            self.add("target.path", "PASS", "Explicit target exists and resolves to a canonical directory.",
                     paths={"input": self.target_arg, "canonical": str(self.target)},
                     evidence={"input_alias": str(Path(self.target_arg).expanduser()) != str(self.target)})
        except (OSError, PreflightError) as exc:
            self.add("target.path", "FAIL", str(exc), paths={"input": self.target_arg})
        try:
            default_root = Path(__file__).resolve().parent.parent
            self.harness = canonical(self.harness_arg) if self.harness_arg else canonical(default_root)
            if not self.harness.is_dir():
                raise PreflightError("Harness root is not a directory")
            self.report["harness_root"] = str(self.harness)
            self.add("harness.path", "PASS", "Harness root is derived independently from the CLI target argument.",
                     paths={"canonical": str(self.harness), "source": "--harness-root" if self.harness_arg else "CLI module location"})
        except (OSError, PreflightError) as exc:
            self.add("harness.path", "FAIL", str(exc), paths={"input": self.harness_arg})

    def make_git_environment(self) -> None:
        present = sorted(key for key in os.environ if key.startswith("GIT_"))
        rejected = sorted(key for key in present if key in POLICY_GIT_REJECT or
                          key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_") or
                          key not in POLICY_GIT_NON_IDENTITY)
        unknown = sorted(key for key in present if key not in POLICY_GIT_REJECT and
                         not key.startswith("GIT_CONFIG_KEY_") and not key.startswith("GIT_CONFIG_VALUE_") and
                         key not in POLICY_GIT_NON_IDENTITY)
        if rejected:
            self.add("git.environment", "FAIL", "Unsafe or unknown inherited Git environment variable names were present; Git was not started.",
                     values={"rejected_names": rejected, "unknown_names": unknown})
            self.git_env = None
            return
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat",
                    "LC_ALL": "C"})
        self.git_env = env
        self.add("git.environment", "PASS", "Inherited Git variables were enumerated; non-identity values were stripped and a controlled read-only Git environment was built.",
                 values={"stripped_names": present, "child_git_variables": ["GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_PAGER"]},
                 limitations=["Git local configuration remains in effect after its include and core.worktree settings are inspected."])

    def git(self, root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if self.git_env is None:
            raise PreflightError("Git environment has not passed validation")
        cmd = ["git", "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
               "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-C", str(root), *args]
        try:
            result = subprocess.run(cmd, env=self.git_env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False)
        except OSError as exc:
            raise PreflightError(f"Could not start Git: {exc}") from exc
        result = subprocess.CompletedProcess(cmd, result.returncode,
                                             result.stdout.decode("utf-8", "surrogateescape"),
                                             result.stderr.decode("utf-8", "surrogateescape"))
        if check and result.returncode != 0:
            raise PreflightError(f"Git command failed ({result.returncode}): {' '.join(args)}: {result.stderr.strip()}")
        return result

    def git_config_file(self, config_file: Path) -> list[tuple[str, str]]:
        if self.git_env is None:
            raise PreflightError("Git environment has not passed validation")
        command = ["git", "config", "--file", str(config_file), "--no-includes", "--null", "--list"]
        try:
            result = subprocess.run(command, env=self.git_env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False)
        except OSError as exc:
            raise PreflightError(f"Could not inspect local Git config: {exc}") from exc
        stdout = result.stdout.decode("utf-8", "surrogateescape")
        if result.returncode != 0:
            raise PreflightError(f"Could not inspect local Git config {config_file}: {result.stderr.decode('utf-8', 'replace').strip()}")
        entries = []
        for row in stdout.split("\0"):
            if row and "\n" in row:
                key, value = row.split("\n", 1)
                entries.append((key.lower(), value))
        return entries

    def local_git_metadata(self, label: str, root: Path) -> tuple[Path, Path, list[tuple[str, str]], list[Path]]:
        marker = root / ".git"
        try:
            marker_info = marker.lstat()
        except OSError as exc:
            raise PreflightError(f"{label} has no readable repository .git marker at the explicit root: {exc}") from exc
        if stat.S_ISLNK(marker_info.st_mode):
            raise PreflightError(f"{label} .git marker is a symlink; repository identity cannot be established safely.")
        if stat.S_ISDIR(marker_info.st_mode):
            gitdir = marker.resolve(strict=True)
            gitdir_lexical = Path(os.path.abspath(marker))
        elif stat.S_ISREG(marker_info.st_mode):
            marker_text = marker.read_text(encoding="utf-8", errors="strict").strip()
            if not marker_text.lower().startswith("gitdir:"):
                raise PreflightError(f"{label} .git file is not a valid gitdir pointer.")
            gitdir_value = marker_text.split(":", 1)[1].strip()
            gitdir_path = Path(gitdir_value)
            gitdir_lexical = Path(os.path.abspath((root / gitdir_path) if not gitdir_path.is_absolute() else gitdir_path))
            gitdir = gitdir_lexical.resolve(strict=True)
        else:
            raise PreflightError(f"{label} .git marker is neither a directory nor a worktree pointer file.")
        if not gitdir.is_dir():
            raise PreflightError(f"{label} Git directory does not resolve to a directory: {gitdir}")
        common_file = gitdir / "commondir"
        if common_file.is_file():
            common_value = common_file.read_text(encoding="utf-8", errors="strict").strip()
            common_path = Path(common_value)
            common_lexical = Path(os.path.abspath((gitdir_lexical / common_path) if not common_path.is_absolute() else common_path))
            common = common_lexical.resolve(strict=True)
        else:
            common = gitdir
            common_lexical = gitdir_lexical
        if not common.is_dir():
            raise PreflightError(f"{label} common Git directory is unresolved: {common}")
        config_files = [common / "config"]
        worktree_config = gitdir / "config.worktree"
        if worktree_config.is_file():
            config_files.append(worktree_config)
        entries: list[tuple[str, str]] = []
        for config_file in config_files:
            if config_file.is_file():
                entries.extend(self.git_config_file(config_file))
        includes = [(key, value) for key, value in entries if key == "include.path" or key.startswith("includeif.")]
        if includes:
            raise PreflightError(f"{label} local Git config contains include directives; their sources are not followed or trusted: {[k for k, _ in includes]}")
        filters = [(key, value) for key, value in entries
                   if key.startswith("filter.") and key.rsplit(".", 1)[-1] in {"clean", "smudge", "process"} and value.strip()]
        if filters:
            raise PreflightError(f"{label} local Git config defines executable content filters; status inspection was stopped: {[k for k, _ in filters]}")
        core_worktrees = [value for key, value in entries if key == "core.worktree"]
        for value in core_worktrees:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = gitdir / candidate
            if candidate.resolve(strict=False) != root:
                raise PreflightError(f"{label} local core.worktree does not resolve to its canonical root.")
        metadata_paths = [marker, gitdir_lexical, common_lexical]
        return gitdir, common, entries, metadata_paths

    def discover_git_repo(self, label: str, root: Path) -> dict[str, Any]:
        gitdir_expected, common_expected, entries, metadata_paths = self.local_git_metadata(label, root)
        inside = self.git(root, "rev-parse", "--is-inside-work-tree").stdout.strip()
        top_raw = self.git(root, "rev-parse", "--show-toplevel").stdout.strip()
        top = Path(top_raw).resolve(strict=True)
        if inside != "true" or top != root:
            raise PreflightError(f"{label} is not a non-bare Git worktree rooted at the authorized canonical path.")
        common_raw = self.git(root, "rev-parse", "--git-common-dir").stdout.strip()
        refs_raw = self.git(root, "rev-parse", "--git-path", "refs").stdout.strip()
        objects_raw = self.git(root, "rev-parse", "--git-path", "objects").stdout.strip()
        gitdir_raw = self.git(root, "rev-parse", "--git-dir").stdout.strip()
        def git_path(value: str) -> Path:
            item = Path(value)
            return (root / item).resolve(strict=False) if not item.is_absolute() else item.resolve(strict=False)
        def raw_git_path(value: str) -> Path:
            item = Path(value)
            return (root / item).absolute() if not item.is_absolute() else item
        common, refs, objects, gitdir = map(git_path, [common_raw, refs_raw, objects_raw, gitdir_raw])
        raw_paths = [raw_git_path(value) for value in [common_raw, refs_raw, objects_raw, gitdir_raw]]
        if gitdir != gitdir_expected or common != common_expected:
            raise PreflightError(f"{label} Git identity returned by Git disagrees with its non-following .git metadata inspection.")
        alternates, alternate_path_spellings = self.discover_alternates(objects)
        return {"label": label, "root": root, "common": common, "refs": refs, "objects": objects,
                "gitdir": gitdir, "git_metadata_paths": metadata_paths,
                "raw_git_paths": [*raw_paths, *alternate_path_spellings],
                "object_stores": alternates, "config_entries": entries}

    def discover_alternates(self, object_dir: Path) -> tuple[list[Path], list[Path]]:
        found: list[Path] = []
        path_spellings: list[Path] = []
        pending = [(object_dir, frozenset())]
        visited: set[Path] = set()
        while pending:
            candidate, ancestors = pending.pop()
            store = candidate.resolve(strict=False)
            if store in ancestors:
                raise PreflightError(f"Cycle detected in Git object alternates at {store}")
            if store in visited:
                continue
            visited.add(store)
            if len(visited) > 10000:
                raise PreflightError("Git object alternate graph exceeds the inspection limit")
            if not store.is_dir():
                raise PreflightError(f"Git object store cannot be resolved: {store}")
            found.append(store)
            alt_file = store / "info" / "alternates"
            if alt_file.exists():
                try:
                    lines = alt_file.read_text(encoding="utf-8").splitlines()
                except OSError as exc:
                    raise PreflightError(f"Cannot inspect Git alternates file {alt_file}: {exc}") from exc
                for line in lines:
                    value = line.strip()
                    if not value:
                        continue
                    candidate = Path(value)
                    if not candidate.is_absolute():
                        candidate = store / candidate
                    candidate = Path(os.path.abspath(candidate))
                    path_spellings.append(candidate)
                    contains_symlink = False
                    for component in self._absolute_path_components(candidate):
                        try:
                            info = component.lstat()
                        except OSError as exc:
                            raise PreflightError(f"Cannot inspect Git alternate path component {component}: {exc}",
                                                 "NEEDS_HUMAN_INPUT") from exc
                        if stat.S_ISLNK(info.st_mode):
                            contains_symlink = True
                    candidate = candidate.resolve(strict=False)
                    if not candidate.is_dir():
                        raise PreflightError(f"Git alternate object store is missing or unresolved: {candidate}")
                    if not contains_symlink:
                        pending.append((candidate, ancestors | {store}))
                    elif candidate not in found:
                        # The canonical store identity is still useful for overlap
                        # detection, but its contents are not traversed through a
                        # symlinked path. Isolation will fail on the recorded path.
                        found.append(candidate)
        return found, path_spellings

    def validate_git_identity(self) -> None:
        if not self.target or not self.harness:
            self.add("git.identity", "NOT_APPLICABLE", "Canonical harness and target paths are required before Git identity checks.")
            self.add("git.isolation", "NOT_APPLICABLE", "Git isolation depends on verified repository identities.")
            return
        if self.git_env is None:
            self.add("git.identity", "NOT_APPLICABLE", "Git identity commands were not run because inherited Git environment validation failed.")
            self.add("git.isolation", "NOT_APPLICABLE", "Git isolation could not run without a safe Git environment.")
            return
        try:
            target_stat, harness_stat = self.target.stat(), self.harness.stat()
        except OSError as exc:
            self.add("git.roots", "NEEDS_HUMAN_INPUT", f"Cannot compare target and harness filesystem identities: {exc}")
            self.add("git.identity", "NOT_APPLICABLE", "Repository identity checks require verifiable root filesystem identities.")
            self.add("git.isolation", "NOT_APPLICABLE", "Git store isolation requires verifiable root filesystem identities.")
            return
        same_root_inode = (target_stat.st_dev, target_stat.st_ino) == (harness_stat.st_dev, harness_stat.st_ino)
        if path_overlap(self.target, self.harness) or same_root_inode:
            self.add("git.roots", "FAIL", "Harness and target working roots are equal or nested.",
                     paths={"harness_root": str(self.harness), "target_root": str(self.target)},
                     evidence={"same_root_filesystem_identity": same_root_inode})
            self.add("git.identity", "NOT_APPLICABLE", "Repository identity commands were skipped because the roots overlap.")
            self.add("git.isolation", "NOT_APPLICABLE", "Git store isolation cannot be established for overlapping roots.")
            return
        self.add("git.roots", "PASS", "Canonical harness and target working roots are disjoint.",
                 paths={"harness_root": str(self.harness), "target_root": str(self.target)})
        problems = []
        for label, root in (("harness", self.harness), ("target", self.target)):
            try:
                repo = self.discover_git_repo(label, root)
                self.git_repos[label] = repo
                self.add(f"git.identity.{label}", "PASS", f"{label.title()} Git root matches its authorized canonical directory.",
                         paths={"root": str(root), "common_dir": str(repo["common"]), "refs_dir": str(repo["refs"]),
                                "object_dir": str(repo["objects"]), "git_dir": str(repo["gitdir"])})
            except (OSError, UnicodeError, ValueError, PreflightError) as exc:
                problems.append(f"{label}: {exc}")
                self.add(f"git.identity.{label}", "FAIL", str(exc), paths={"expected_root": str(root)})
        if problems:
            self.add("git.isolation", "NOT_APPLICABLE", "Store isolation needs both repositories to have verified Git identities.", evidence={"reasons": problems})
            return
        harness, target = self.git_repos["harness"], self.git_repos["target"]
        overlaps = []
        for field in ("common", "refs"):
            if path_overlap(harness[field], target[field]):
                overlaps.append({"kind": field, "harness": str(harness[field]), "target": str(target[field])})
        for left in harness["object_stores"]:
            for right in target["object_stores"]:
                if path_overlap(left, right):
                    overlaps.append({"kind": "object_store", "harness": str(left), "target": str(right)})
        try:
            harness_inventory = self.git_metadata_inventory(harness)
            target_inventory = self.git_metadata_inventory(target)
            harness_worktrees = self.parse_worktrees(self.git(harness["root"], "worktree", "list", "--porcelain").stdout)
            target_worktrees = self.parse_worktrees(self.git(target["root"], "worktree", "list", "--porcelain").stdout)
        except PreflightError as exc:
            self.add("git.isolation", exc.status, str(exc), evidence={"verified_roots": [str(self.harness), str(self.target)]})
            return
        by_inode = {identity: path for path, identity in harness_inventory["files"].items()}
        shared = []
        for file, identity in target_inventory["files"].items():
            other = by_inode.get(identity)
            if other:
                shared.append({"harness_file": other, "target_file": file,
                               "device": identity[0], "inode": identity[1]})
        harness_dirs = {identity: path for path, identity in harness_inventory["directories"].items()}
        directory_aliases = []
        for path, identity in target_inventory["directories"].items():
            other = harness_dirs.get(identity)
            if other:
                directory_aliases.append({"harness_directory": other, "target_directory": path,
                                          "device": identity[0], "inode": identity[1]})
        metadata_symlinks = {"harness": harness_inventory["symlinks"], "target": target_inventory["symlinks"]}
        if overlaps or directory_aliases or shared or metadata_symlinks["harness"] or metadata_symlinks["target"]:
            self.add("git.isolation", "FAIL", "Git metadata or object paths are shared, aliased, or symlinked in a way that prevents proving isolation.",
                     evidence={"overlapping_stores": overlaps, "shared_directory_inodes": directory_aliases, "shared_file_inodes": shared[:100],
                               "metadata_symlinks": metadata_symlinks,
                               "harness_worktrees": harness_worktrees, "target_worktrees": target_worktrees},
                     limitations=["Shared-file listing is capped at 100 entries."] if len(shared) > 100 else None)
        else:
            self.add("git.isolation", "PASS", "Common directories, refs stores, transitive object stores, and existing Git metadata file identities are disjoint.",
                     paths={"harness_common": str(harness["common"]), "target_common": str(target["common"]),
                            "harness_refs": str(harness["refs"]), "target_refs": str(target["refs"])},
                     evidence={"harness_object_stores": [str(x) for x in harness["object_stores"]],
                               "target_object_stores": [str(x) for x in target["object_stores"]],
                               "harness_git_files_examined": len(harness_inventory["files"]), "target_git_files_examined": len(target_inventory["files"]),
                               "shared_directory_inodes": [], "shared_file_inodes": [], "metadata_symlinks": metadata_symlinks,
                               "harness_worktrees": harness_worktrees, "target_worktrees": target_worktrees})

    @staticmethod
    def _absolute_path_components(path: Path) -> list[Path]:
        absolute = Path(os.path.abspath(path))
        result = []
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current = current / part
            result.append(current)
        return result

    def git_metadata_inventory(self, repo: dict[str, Any]) -> dict[str, Any]:
        """Inventory Git metadata without following symlinks; uncertainty blocks isolation."""
        roots = [*repo.get("git_metadata_paths", []), *repo.get("raw_git_paths", []),
                 repo["gitdir"], repo["common"], repo["refs"], repo["objects"],
                 *repo["object_stores"]]
        unique_roots: list[Path] = []
        for value in roots:
            absolute = Path(os.path.abspath(value))
            if absolute not in unique_roots:
                unique_roots.append(absolute)

        files: dict[str, tuple[int, int]] = {}
        directories: dict[str, tuple[int, int]] = {}
        symlinks: list[dict[str, str]] = []
        errors: list[str] = []
        visited = 0

        def record_symlink(path: Path) -> bool:
            try:
                info = path.lstat()
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                return False
            if stat.S_ISLNK(info.st_mode):
                try:
                    resolved = str(path.resolve(strict=False))
                except (OSError, RuntimeError):
                    resolved = "unresolved"
                symlinks.append({"path": str(path), "resolved_path": resolved})
                return True
            return False

        def has_symlink_component(path: Path) -> bool:
            for component in self._absolute_path_components(path):
                if record_symlink(component):
                    return True
            return False

        for root in sorted(unique_roots, key=lambda item: (len(item.parts), str(item))):
            # Check ancestors before deduplicating a child root against a scanned
            # parent, so symlinked metadata components cannot be hidden.
            if has_symlink_component(root):
                continue
            if any(Path(previous) == root or Path(previous) in root.parents for previous in directories):
                continue
            try:
                root_info = root.lstat()
            except OSError as exc:
                errors.append(f"{root}: {exc}")
                continue
            if stat.S_ISREG(root_info.st_mode):
                files[str(root)] = (root_info.st_dev, root_info.st_ino)
                continue
            if not stat.S_ISDIR(root_info.st_mode):
                errors.append(f"{root}: expected a regular metadata file or directory")
                continue
            for current, dirs, names in os.walk(root, topdown=True, followlinks=False,
                                                onerror=lambda exc: errors.append(str(exc))):
                current_path = Path(current)
                try:
                    current_info = current_path.lstat()
                except OSError as exc:
                    errors.append(f"{current_path}: {exc}")
                    dirs[:] = []
                    continue
                if stat.S_ISLNK(current_info.st_mode):
                    record_symlink(current_path)
                    dirs[:] = []
                    continue
                if not stat.S_ISDIR(current_info.st_mode):
                    errors.append(f"{current_path}: metadata directory changed type during inspection")
                    dirs[:] = []
                    continue
                directories[str(current_path)] = (current_info.st_dev, current_info.st_ino)
                visited += 1
                for name in list(dirs):
                    item = current_path / name
                    try:
                        info = item.lstat()
                    except OSError as exc:
                        errors.append(f"{item}: {exc}")
                        dirs.remove(name)
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        record_symlink(item)
                        dirs.remove(name)
                    elif not stat.S_ISDIR(info.st_mode):
                        errors.append(f"{item}: expected a metadata directory")
                        dirs.remove(name)
                for name in names:
                    item = current_path / name
                    try:
                        info = item.lstat()
                    except OSError as exc:
                        errors.append(f"{item}: {exc}")
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        record_symlink(item)
                    elif stat.S_ISREG(info.st_mode):
                        files[str(item)] = (info.st_dev, info.st_ino)
                    else:
                        errors.append(f"{item}: unsupported file type in Git metadata")
                if visited > 1_000_000 or len(files) > 2_000_000:
                    errors.append("Git metadata inventory reached its safety limit")
                    dirs[:] = []
                    break
        if errors:
            raise PreflightError(f"Could not completely inspect Git metadata for {repo['label']}: {errors[:20]}",
                                 "NEEDS_HUMAN_INPUT")
        return {"files": files, "directories": directories, "symlinks": symlinks}

    def inspect_repository_state(self) -> None:
        isolation = next((row for row in self.checks if row["check_id"] == "git.isolation"), None)
        if isolation and isolation["status"] != "PASS":
            self.add("repository.state", "NOT_APPLICABLE",
                     "Target Git state commands were skipped because Git metadata isolation was not established.")
            return
        repo = self.git_repos.get("target")
        if not repo:
            self.add("repository.state", "NOT_APPLICABLE", "Target Git identity was not verified.")
            return
        try:
            root = repo["root"]
            branch_result = self.git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
            head_result = self.git(root, "rev-parse", "--verify", "HEAD", check=False)
            head = head_result.stdout.strip() if head_result.returncode == 0 else None
            commits_result = self.git(root, "rev-list", "--count", "--all", check=False)
            if commits_result.returncode != 0:
                raise PreflightError(f"Could not inspect commit count: {commits_result.stderr.strip()}")
            commits = int(commits_result.stdout.strip() or "0")
            if (head_result.returncode == 0) != (commits > 0):
                raise PreflightError("HEAD and existing commit count are inconsistent.")
            if branch_result.returncode not in {0, 1}:
                raise PreflightError(f"Could not inspect current branch: {branch_result.stderr.strip()}")
            status = self.git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all").stdout
            parsed_status = self.parse_porcelain_status_z(status)
            tracked = parsed_status["tracked_modifications"]
            untracked = parsed_status["untracked_files"]
            staged = parsed_status["staged_paths"]
            worktree = parsed_status["worktree_paths"]
            worktree_raw = self.git(root, "worktree", "list", "--porcelain").stdout
            worktrees = self.parse_worktrees(worktree_raw)
            submodules, submodule_limits, submodule_attention = self.inspect_submodules(repo)
            submodule_changes = any(item.get("state") in {
                "dirty", "different_commit", "conflict", "unverified", "unverified_symlink", "unverified_outside_target"
            } for item in submodules)
            self.report["repository_state"] = {"branch": branch, "head": head, "existing_commit_count": commits,
                                                  "status": "dirty" if tracked or untracked or submodule_changes else "clean",
                                                  "tracked_modifications": tracked[:500], "untracked_files": untracked[:500],
                                                  "worktrees": worktrees, "submodules": submodules}
            limitations = ["Path lists are capped at 500 entries."] if len(tracked) > 500 or len(untracked) > 500 else []
            limitations.extend(submodule_limits)
            self.add("repository.state", "NEEDS_HUMAN_INPUT" if submodule_attention else "PASS", "Read branch, HEAD, commits, tracked and untracked status, worktrees, and direct submodule state without changing the target.",
                     values={"branch": branch, "head": head, "existing_commit_count": commits,
                             "tracked_modification_count": len(tracked), "untracked_file_count": len(untracked),
                             "staged_path_count": len(staged), "worktree_path_count": len(worktrees), "submodule_count": len(submodules)},
                     evidence={"tracked_modifications": tracked[:500], "untracked_files": untracked[:500], "worktrees": worktrees, "submodules": submodules},
                     limitations=limitations or None)
        except (PreflightError, ValueError) as exc:
            self.add("repository.state", "FAIL", str(exc), paths={"target": str(repo["root"])})

    @staticmethod
    def parse_worktrees(raw: str) -> list[dict[str, Any]]:
        result = []
        current: dict[str, Any] = {}
        for line in raw.splitlines() + [""]:
            if not line:
                if current:
                    result.append(current)
                    current = {}
                continue
            if " " in line:
                key, value = line.split(" ", 1)
                current[key] = value
            else:
                current[line] = True
        return result

    @staticmethod
    def parse_porcelain_status_z(raw: str) -> dict[str, list[Any]]:
        """Parse porcelain v1 -z records, including the second path in R/C entries."""
        records = raw.split("\0")
        tracked: list[dict[str, Any]] = []
        untracked: list[str] = []
        staged: list[str] = []
        worktree: list[str] = []
        index = 0
        while index < len(records):
            entry = records[index]
            index += 1
            if not entry:
                continue
            if len(entry) < 3 or entry[2] != " ":
                raise ValueError("Malformed git status --porcelain=v1 -z record")
            state, path = entry[:2], entry[3:]
            if state == "??":
                untracked.append(path)
                continue
            row: dict[str, Any] = {"path": path, "index": state[0], "worktree": state[1]}
            if "R" in state or "C" in state:
                if index >= len(records) or not records[index]:
                    raise ValueError("Rename/copy status record is missing its source path")
                source_path = records[index]
                index += 1
                row["destination_path"] = path
                row["source_path"] = source_path
            tracked.append(row)
            if state[0] != " ":
                staged.append(path)
            if state[1] != " ":
                worktree.append(path)
        return {"tracked_modifications": tracked, "untracked_files": untracked,
                "staged_paths": staged, "worktree_paths": worktree}

    def inspect_submodules(self, repo: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], bool]:
        raw = self.git(repo["root"], "ls-files", "--stage", "-z").stdout
        entries = []
        for item in raw.split("\0"):
            if not item or "\t" not in item:
                continue
            metadata, path = item.split("\t", 1)
            fields = metadata.split()
            if len(fields) == 3 and fields[0] == "160000":
                entries.append({"expected_commit": fields[1], "stage": fields[2], "path": path})
        results, attention = [], False
        for entry in entries:
            target_root = repo["root"]
            relative = PurePosixPath(entry["path"])
            subpath = target_root.joinpath(*relative.parts)
            try:
                canonical_subpath = subpath.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                results.append({**entry, "state": "unverified", "reason": f"Could not resolve submodule path; Git was not invoked: {exc}"})
                attention = True
                continue
            symlink_components = []
            current = target_root
            for part in relative.parts:
                current = current / part
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    break
                except OSError as exc:
                    symlink_components.append({"path": str(current), "inspection_error": str(exc)})
                    break
                if stat.S_ISLNK(info.st_mode):
                    symlink_components.append({"path": str(current), "resolved_path": str(current.resolve(strict=False))})
                    break
            if canonical_subpath != target_root and target_root not in canonical_subpath.parents:
                results.append({**entry, "state": "unverified", "canonical_submodule_path": str(canonical_subpath),
                                "symlink_components": symlink_components,
                                "reason": "Submodule path resolves outside the canonical target; Git was not invoked."})
                attention = True
                continue
            if symlink_components:
                results.append({**entry, "state": "unverified", "canonical_submodule_path": str(canonical_subpath),
                                "symlink_components": symlink_components,
                                "reason": "Submodule path contains a symlink component; Git was not invoked."})
                attention = True
                continue
            if not canonical_subpath.exists():
                results.append({**entry, "state": "uninitialized"})
                continue
            try:
                subrepo = self.discover_git_repo("submodule", canonical_subpath)
                head_result = self.git(subrepo["root"], "rev-parse", "--verify", "HEAD", check=False)
                head = head_result.stdout.strip() if head_result.returncode == 0 else None
                status_result = self.git(subrepo["root"], "status", "--porcelain=v1", "-z", "--untracked-files=all")
                changes = [row for row in status_result.stdout.split("\0") if row]
                state = "initialized" if head == entry["expected_commit"] else "different_commit"
                if changes:
                    state = "dirty"
                results.append({**entry, "state": state, "head": head,
                                "tracked_or_untracked_change_count": len(changes),
                                "submodule_root": str(subrepo["root"])})
            except (OSError, UnicodeError, PreflightError) as exc:
                results.append({**entry, "state": "unverified", "reason": str(exc)})
                attention = True
        limitations = ["Only direct gitlinks are inspected; nested submodules remain separate targets and are not traversed.",
                       "Submodule Git-store independence from the harness is not established by the parent target's isolation check."] if entries else []
        return results, limitations, attention

    def load_scope_lists(self) -> dict[str, list[str]]:
        result = {}
        for field in ("included_paths", "code_paths", "artifact_paths", "revision_paths", "excluded_paths"):
            value = self.config.get(field, [])
            if not isinstance(value, list):
                raise PreflightError(f"{field} must be a JSON array")
            normalized = [config_relative(item, field) for item in value]
            if len(normalized) != len(set(normalized)):
                raise PreflightError(f"{field} contains duplicate paths")
            result[field] = normalized
        return result

    def _is_nested_bare_repository(self, path: Path) -> bool:
        """Recognize a bare repository from Git metadata, not from its directory name."""
        marker_paths = [path / "HEAD", path / "config", path / "objects", path / "refs"]
        try:
            marker_info = [item.lstat() for item in marker_paths]
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise PreflightError(f"Could not inspect possible bare repository {path}: {exc}") from exc

        # A directory with only a subset of these conventional entries is not
        # enough evidence to call it a repository.
        config_info = marker_info[1]
        if stat.S_ISLNK(config_info.st_mode) or not stat.S_ISREG(config_info.st_mode):
            return False
        try:
            result = subprocess.run(
                ["git", "config", "--file", str(path / "config"), "--no-includes", "--bool", "--get", "core.bare"],
                env=self.git_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except OSError as exc:
            raise PreflightError(f"Could not inspect possible bare repository config {path / 'config'}: {exc}") from exc
        if result.returncode == 1:
            return False
        if result.returncode != 0:
            raise PreflightError(f"Could not parse possible bare repository config {path / 'config'}: "
                                 f"{result.stderr.decode('utf-8', 'replace').strip()}")
        bare_value = result.stdout.decode("ascii", "replace").strip().lower()
        if bare_value != "true":
            return False

        head_info, _, objects_info, refs_info = marker_info
        if (stat.S_ISLNK(head_info.st_mode) or not stat.S_ISREG(head_info.st_mode)
                or stat.S_ISLNK(objects_info.st_mode) or not stat.S_ISDIR(objects_info.st_mode)
                or stat.S_ISLNK(refs_info.st_mode) or not stat.S_ISDIR(refs_info.st_mode)):
            raise PreflightError(f"Possible bare repository has symlinked or invalid structural metadata: {path}")
        try:
            head = (path / "HEAD").read_text(encoding="ascii", errors="strict").strip()
        except (OSError, UnicodeError) as exc:
            raise PreflightError(f"Could not read possible bare repository HEAD at {path}: {exc}") from exc
        if not (head.startswith("ref: refs/") or
                (len(head) in {40, 64} and all(char in "0123456789abcdefABCDEF" for char in head))):
            raise PreflightError(f"Possible bare repository has an invalid HEAD file: {path}")
        return True

    def nested_git_repository_roots(self) -> list[str]:
        """Find nested worktrees and bare repositories without following symlinked dirs."""
        assert self.target is not None
        if self.git_env is None:
            raise PreflightError("Cannot inspect nested repositories without the controlled Git environment")
        found: list[str] = []
        errors: list[str] = []
        directories_seen = files_seen = 0
        limited = False
        for current, dirs, files in os.walk(
                self.target, topdown=True, followlinks=False,
                onerror=lambda exc: errors.append(str(exc))):
            current_path = Path(current)
            relative = current_path.relative_to(self.target)
            if relative == Path("."):
                # Never recurse through symlinked directories or the target's
                # own Git metadata.
                safe_dirs = []
                for name in dirs:
                    candidate = current_path / name
                    try:
                        info = candidate.lstat()
                    except OSError as exc:
                        errors.append(f"{candidate}: {exc}")
                        continue
                    if name != ".git" and stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        safe_dirs.append(name)
                dirs[:] = safe_dirs
                files[:] = [name for name in files if name != ".git"]
                continue
            try:
                if self._is_nested_bare_repository(current_path):
                    found.append(relative.as_posix())
                    dirs[:] = []
                    continue
            except PreflightError as exc:
                errors.append(str(exc))
                dirs[:] = []
                continue
            marker = current_path / ".git"
            try:
                marker.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{marker}: {exc}")
            else:
                found.append(relative.as_posix())
                dirs[:] = []
                continue
            directories_seen += 1
            files_seen += len(files)
            safe_dirs = []
            for name in dirs:
                candidate = current_path / name
                try:
                    info = candidate.lstat()
                except OSError as exc:
                    errors.append(f"{candidate}: {exc}")
                    continue
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    safe_dirs.append(name)
            dirs[:] = safe_dirs
            if directories_seen > 100_000 or files_seen > 1_000_000:
                limited = True
                dirs[:] = []
                break
        if errors or limited:
            details = errors[:5]
            if limited:
                details.append("nested repository scan reached its safety limit")
            raise PreflightError(f"Could not conclusively inspect nested Git repositories: {details}")
        return sorted(found)

    def scope_path_safety(self, relative: str) -> tuple[bool, list[str]]:
        assert self.target is not None
        current = self.target
        symlinks = []
        for part in relative.split("/"):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PreflightError(f"Cannot inspect scoped path {current}: {exc}")
            if stat.S_ISLNK(info.st_mode):
                symlinks.append(str(current))
                resolved = current.resolve(strict=False)
                if resolved != self.target and self.target not in resolved.parents:
                    raise PreflightError(f"Scoped path resolves outside target root: {current} -> {resolved}")
        return not symlinks, symlinks

    def validate_scopes(self) -> None:
        if not self.target:
            self.add("scope.validation", "NOT_APPLICABLE", "Target root is unavailable.")
            return
        if not self.config_loaded:
            self.add("scope.validation", "NOT_APPLICABLE", "Scope config could not be loaded; the config input check has failed.")
            return
        try:
            lists = self.load_scope_lists()
            if "target_path" in self.config:
                if not isinstance(self.config["target_path"], str):
                    raise PreflightError("Configured target_path must be a string")
                cfg_target = canonical(self.config["target_path"])
                if cfg_target != self.target:
                    raise PreflightError("Configured target_path does not match the explicit CLI target.")
            if self.workspace_arg and self.config.get("workspace_root"):
                if canonical_authorized_root(self.workspace_arg, "--workspace-root") != canonical_authorized_root(self.config["workspace_root"], "workspace_root"):
                    raise PreflightError("--workspace-root and configured workspace_root disagree")
            workdirs = self.config.get("authorized_working_directories", [])
            if not isinstance(workdirs, list):
                raise PreflightError("authorized_working_directories must be a JSON array")
            canonical_workdirs = []
            for value in workdirs:
                workdir = canonical_authorized_root(value, "authorized_working_directories entry")
                if workdir != self.target and self.target not in workdir.parents:
                    raise PreflightError(f"Authorized working directory is outside the target root: {workdir}")
                canonical_workdirs.append(str(workdir))
            included, code, artifacts, revision, excluded = (lists[k] for k in ("included_paths", "code_paths", "artifact_paths", "revision_paths", "excluded_paths"))
            if set(included) != set(code) | set(artifacts):
                raise PreflightError("included_paths must equal the union of code_paths and artifact_paths.")
            writable_paths = sorted(set(included + code + artifacts + revision))
            metadata_paths = [path for path in writable_paths if ".git" in PurePosixPath(path).parts]
            if metadata_paths:
                raise PreflightError(f"Write scopes may not include Git metadata component '.git': {metadata_paths}")
            nested_repositories = self.nested_git_repository_roots() if writable_paths else []
            for path in writable_paths:
                for nested_root in nested_repositories:
                    if paths_overlap_by_component(path, nested_root):
                        raise PreflightError(f"Write scope overlaps nested Git repository {nested_root!r}: {path!r}")
            for left in code:
                for right in artifacts:
                    if is_path_or_child(left, right) or is_path_or_child(right, left):
                        raise PreflightError(f"Code and artifact path classifications overlap: {left!r}, {right!r}")
            for path in revision:
                if not any(is_path_or_child(path, prefix) for prefix in code + artifacts):
                    raise PreflightError(f"revision_paths entry is outside authorized code/artifact paths: {path!r}")
                if any(is_path_or_child(path, prefix) or is_path_or_child(prefix, path) for prefix in excluded):
                    raise PreflightError(f"revision_paths entry is excluded: {path!r}")
            unsafe = []
            for path in set(included + code + artifacts + revision + excluded):
                safe, links = self.scope_path_safety(path)
                if not safe:
                    unsafe.extend(links)
            if unsafe:
                raise PreflightError(f"Configured scopes pass through symlinks and cannot be proven safe: {sorted(set(unsafe))}")
            scopes = {"included_paths": included, "code_paths": code, "artifact_paths": artifacts,
                      "revision_paths": revision, "excluded_paths": excluded}
            self.report["scope"] = scopes
            self.report["authorized_working_directories"] = canonical_workdirs or [str(self.target)]
            self.add("scope.validation", "PASS", "Configured paths are normalized and contained; classifications are disjoint, exclusions deny descendant writes, and revision paths are authorized.",
                     evidence=scopes,
                     limitations=["An empty included_paths list authorizes zero writes.",
                                  "Preflight remains read-only; safe_writer.py re-runs preflight and binds its own writes to verified filesystem handles."])
        except (OSError, PreflightError) as exc:
            self.add("scope.validation", "FAIL", str(exc), evidence={"config_keys": sorted(self.config.keys())})

    def instruction_config(self) -> tuple[list[Path], list[str]]:
        assert self.target is not None
        workspace_value = self.workspace_arg or self.config.get("workspace_root")
        workspace = canonical_authorized_root(workspace_value, "workspace_root") if workspace_value else None
        if workspace and workspace != self.target and workspace not in self.target.parents:
            raise PreflightError("workspace_root must be a canonical ancestor of or equal to the target root")
        raw_roots = self.config.get("instruction_roots", [])
        if not isinstance(raw_roots, list):
            raise PreflightError("instruction_roots must be a JSON array")
        roots = [self.target]
        for raw in raw_roots:
            root = canonical_authorized_root(raw, "instruction_roots entry")
            if root != self.target and root not in self.target.parents:
                raise PreflightError(f"Instruction root is not an ancestor of target: {root}")
            if workspace and root != workspace and workspace not in root.parents:
                raise PreflightError(f"Instruction root is broader than workspace_root: {root}")
            roots.append(root)
        chain = []
        if workspace:
            current = self.target
            while True:
                chain.append(current)
                if current == workspace:
                    break
                current = current.parent
        else:
            chain = list(dict.fromkeys(roots))
        roots = list(dict.fromkeys(roots + chain))
        return roots, [str(x) for x in roots]

    def discover_instructions_and_capabilities(self) -> None:
        if not self.target:
            self.add("discovery.instructions", "NOT_APPLICABLE", "Target root is unavailable.")
            self.add("discovery.capabilities", "NOT_APPLICABLE", "Target root is unavailable.")
            self.add("discovery.documentation", "NOT_APPLICABLE", "Target root is unavailable.")
            return
        try:
            roots, authorized = self.instruction_config()
        except (OSError, PreflightError) as exc:
            self.add("discovery.instructions", "FAIL", str(exc))
            self.add("discovery.capabilities", "NOT_APPLICABLE", "Instruction-root authorization is invalid.")
            self.add("discovery.documentation", "NOT_APPLICABLE", "Instruction-root authorization is invalid.")
            return
        self.report["authorized_instruction_roots"] = authorized
        instruction_records = []
        blocked = []
        for base in roots:
            for name in sorted(INSTRUCTION_FILES):
                candidate = base / name
                if candidate.is_file() or candidate.is_symlink():
                    record = self.read_instruction(candidate, roots)
                    instruction_records.append(record)
                    if record["status"] == "blocked":
                        blocked.append(record)
            copilot = base / ".github" / "copilot-instructions.md"
            if copilot.is_file() or copilot.is_symlink():
                record = self.read_instruction(copilot, roots)
                instruction_records.append(record)
                if record["status"] == "blocked":
                    blocked.append(record)
        target_docs, skills, agents, walk_limited, walk_errors = self.walk_inventory(self.target)
        for item in target_docs["instructions"]:
            if not any(x["source_path"] == item for x in instruction_records):
                record = self.read_instruction(Path(item), roots)
                instruction_records.append(record)
                if record["status"] == "blocked":
                    blocked.append(record)
        status = "NEEDS_HUMAN_INPUT" if blocked or walk_errors or walk_limited else ("PASS" if instruction_records else "NOT_APPLICABLE")
        instr_lims = ["Dependencies, generated build outputs, and VCS metadata directories were excluded from recursive discovery."]
        if walk_limited:
            instr_lims.append("Inventory reached its directory/file safety cap.")
        if walk_errors:
            instr_lims.append(f"Some directories could not be inspected: {walk_errors[:20]}")
        self.report["instructions"] = instruction_records
        self.add("discovery.instructions", status, "Applicable target and explicitly authorized ancestor instruction files were inventoried and fingerprinted; none were executed.",
                 paths={"authorized_roots": authorized}, evidence={"resources": instruction_records}, limitations=instr_lims)
        cap_resources = [{"path": p, "kind": "skill"} for p in skills] + [{"path": p, "kind": "specialized_agent_definition"} for p in agents]
        cap_status = "NEEDS_HUMAN_INPUT" if blocked or walk_errors or walk_limited else ("PASS" if cap_resources else "NOT_APPLICABLE")
        self.report["capabilities"] = cap_resources
        self.add("discovery.capabilities", cap_status,
                 "Project skills and specialized agent definitions were located by path only; their instructions were not executed." if cap_resources else "No project-level skills or specialized agent definitions were found in the inventoried locations.",
                 paths={"resources": cap_resources}, evidence={"skill_count": len(skills), "agent_definition_count": len(agents)},
                 limitations=["Capabilities in external user-configured locations are not inventoried.", "No discovered skill or agent instruction was executed."])
        doc_dirs = sorted(target_docs["doc_dirs"])
        doc_files = sorted(target_docs["doc_files"])
        doc_status = "NEEDS_HUMAN_INPUT" if walk_errors or walk_limited else ("PASS" if doc_dirs or doc_files else "NOT_APPLICABLE")
        self.add("discovery.documentation", doc_status, "Existing documentation locations and top-level conventions were inventoried." if doc_dirs or doc_files else "No existing documentation conventions were found.",
                 paths={"directories": doc_dirs, "files": doc_files}, evidence={"markdown_directory_count": len(doc_dirs), "documentation_file_count": len(doc_files)},
                 limitations=["This reports locations and names, not a semantic review of every document."])

    @staticmethod
    def read_instruction(path: Path, allowed_roots: list[Path]) -> dict[str, Any]:
        record: dict[str, Any] = {"source_path": str(path), "status": "applied", "applicability_path": str(path.parent)}
        try:
            resolved = path.resolve(strict=True)
            if not any(resolved == root or root in resolved.parents for root in allowed_roots):
                record.update({"canonical_path": str(resolved), "status": "blocked", "reason": "Resolves outside authorized instruction roots; content was not read."})
                return record
            content = resolved.read_bytes()
            record.update({"canonical_path": str(resolved), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
        except OSError as exc:
            record.update({"status": "blocked", "reason": f"Instruction could not be read: {exc}"})
        return record

    @staticmethod
    def walk_inventory(root: Path) -> tuple[dict[str, Any], list[str], list[str], bool, list[str]]:
        instructions, doc_dirs, doc_files = [], set(), set()
        skills, agents, errors = [], [], []
        directories = files_seen = 0
        limited = False
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=lambda e: errors.append(str(e))):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(current) / d).is_symlink())
            directories += 1
            files_seen += len(files)
            if directories > 100_000 or files_seen > 1_000_000:
                limited = True
                dirs[:] = []
                break
            rel_dir = Path(current).relative_to(root)
            rel_dir_text = "" if str(rel_dir) == "." else rel_dir.as_posix()
            if rel_dir_text and (rel_dir_text == "docs" or rel_dir_text.startswith("docs/") or rel_dir_text == "doc" or rel_dir_text.startswith("doc/") or rel_dir_text.startswith("documentation/")):
                doc_dirs.add(rel_dir_text)
            for name in files:
                path = Path(current) / name
                rel = path.relative_to(root).as_posix()
                if name in INSTRUCTION_FILES or (rel_dir_text == ".github" and name == "copilot-instructions.md"):
                    instructions.append(str(path))
                if name == "SKILL.md":
                    skills.append(rel)
                if (rel.startswith((".agents/agents/", ".claude/agents/", ".codex/agents/", "agents/")) and name.lower().endswith((".md", ".yaml", ".yml", ".json"))):
                    agents.append(rel)
                if name.lower().endswith((".md", ".rst", ".adoc")) and ("/" not in rel or rel.split("/", 1)[0] in {"docs", "doc", "documentation"}):
                    doc_files.add(rel)
        return {"instructions": instructions, "doc_dirs": sorted(doc_dirs), "doc_files": sorted(doc_files)}, sorted(skills), sorted(agents), limited, errors

    def inspect_filesystem(self) -> None:
        if not self.target or not self.harness:
            self.add("filesystem.identity", "NOT_APPLICABLE", "Canonical roots are unavailable.")
            self.add("filesystem.symlinks", "NOT_APPLICABLE", "Target root is unavailable.")
            self.add("filesystem.hardlinks", "NOT_APPLICABLE", "Target root is unavailable.")
            return
        try:
            tstat, hstat = self.target.stat(), self.harness.stat()
            self.add("filesystem.identity", "PASS", "Filesystem device and inode identities were collected; canonical aliases were resolved.",
                     paths={"target": str(self.target), "harness": str(self.harness)},
                     values={"target_device": tstat.st_dev, "target_inode": tstat.st_ino, "harness_device": hstat.st_dev, "harness_inode": hstat.st_ino,
                             "same_filesystem_device": tstat.st_dev == hstat.st_dev},
                     limitations=["Different path spellings that resolve to the same canonical root are detected; undiscoverable filesystem-specific aliases cannot be exhaustively enumerated."])
        except OSError as exc:
            self.add("filesystem.identity", "NEEDS_HUMAN_INPUT", f"Filesystem identity could not be established: {exc}")
        symlinks, hardlinks, errors = [], [], []
        directories_seen = files_seen = 0
        limited = False
        for current, dirs, files in os.walk(self.target, topdown=True, followlinks=False, onerror=lambda e: errors.append(str(e))):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            directories_seen += 1
            files_seen += len(files)
            if directories_seen > 100_000 or files_seen > 1_000_000:
                limited = True
                dirs[:] = []
                break
            for name in list(dirs) + files:
                item = Path(current) / name
                try:
                    info = item.lstat()
                    rel = item.relative_to(self.target).as_posix()
                    if stat.S_ISLNK(info.st_mode):
                        resolved = item.resolve(strict=False)
                        symlinks.append({"path": rel, "resolved_path": str(resolved), "within_target": resolved == self.target or self.target in resolved.parents})
                        if name in dirs:
                            dirs.remove(name)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                        hardlinks.append({"path": rel, "link_count": info.st_nlink, "device": info.st_dev, "inode": info.st_ino})
                except OSError as exc:
                    errors.append(f"{item}: {exc}")
        fs_status = "NEEDS_HUMAN_INPUT" if errors or limited else "PASS"
        scan_limits = ["The target filesystem inventory reached its directory/file safety cap."] if limited else []
        self.add("filesystem.symlinks", fs_status, "Target symlinks were inventoried outside excluded VCS, dependency, and generated-build directories without following directory links.",
                 evidence={"count": len(symlinks), "entries": symlinks[:500]},
                 limitations=(["VCS metadata, dependency, and generated-build directories were excluded; configured scope paths are checked separately."] + (["The list is capped at 500 entries."] if len(symlinks) > 500 else []) + scan_limits + ([f"Some entries could not be inspected: {errors[:20]}"] if errors else [])))
        self.add("filesystem.hardlinks", fs_status, "Existing regular files with link count greater than one were inventoried outside excluded VCS, dependency, and generated-build directories.",
                 evidence={"count": len(hardlinks), "entries": hardlinks[:500]},
                 limitations=["VCS metadata, dependency, and generated-build directories were excluded; configured scope paths are checked separately.", "This is observation only. safe_writer.py rechecks file identity/link count and rejects mutation of multiply linked files."] + (["The list is capped at 500 entries."] if len(hardlinks) > 500 else []) + scan_limits + ([f"Some entries could not be inspected: {errors[:20]}"] if errors else []))

    def run(self) -> dict[str, Any]:
        self.load_config()
        self.identify_roots()
        self.make_git_environment()
        self.validate_git_identity()
        self.inspect_repository_state()
        self.validate_scopes()
        self.discover_instructions_and_capabilities()
        self.inspect_filesystem()
        failed = any(row["status"] == "FAIL" for row in self.checks)
        human = any(row["status"] == "NEEDS_HUMAN_INPUT" for row in self.checks)
        self.report["overall_result"] = "BLOCKED" if failed else ("NEEDS_HUMAN_INPUT" if human else "READY")
        self.report["result_categories"] = sorted({row["category"] for row in self.checks if row["status"] in {"FAIL", "NEEDS_HUMAN_INPUT"}})
        self.report["blockers"] = [{"check_id": row["check_id"], "category": row["category"], "explanation": row["explanation"]}
                                    for row in self.checks if row["status"] in {"FAIL", "NEEDS_HUMAN_INPUT"}]
        self.report["safety_boundary"] = "Read-only observations; this report does not enforce future write safety."
        return self.report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only target repository preflight; target path is always explicit.")
    parser.add_argument("--target", required=True, help="Explicit repository-root path to inspect")
    parser.add_argument("--harness-root", help="Harness repository root (defaults to this CLI's repository)")
    parser.add_argument("--config", help="Optional JSON scope and instruction-root configuration")
    parser.add_argument("--workspace-root", help="Optional explicitly authorized ancestor instruction root")
    args = parser.parse_args(argv)
    result = Preflight(args.target, args.harness_root, args.config, args.workspace_root).run()
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return {"READY": 0, "BLOCKED": 2, "NEEDS_HUMAN_INPUT": 3}[result["overall_result"]]


if __name__ == "__main__":
    raise SystemExit(main())

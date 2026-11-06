#!/usr/bin/env python3
"""Narrow, scope-checked directory and file writes for target repositories.

Replacement uses a documented single-writer assumption. The final rename is
atomic for readers, but it is not a compare-and-swap against arbitrary writers.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from preflight import Preflight, PreflightError, canonical, config_relative, is_path_or_child


HARNESS_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class SafeWriteError(Exception):
    """A requested target write was rejected or failed safely."""

    def __init__(self, message: str, *, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence


def _sha256_fd(fd: int) -> str:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise SafeWriteError("Destination is not a regular file")
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    after = os.fstat(fd)
    if _identity(before) != _identity(after):
        raise SafeWriteError("Destination changed while its content was being checked")
    return digest.hexdigest()


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_nlink, stat.S_IFMT(info.st_mode),
            stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _normalize_destination(value: str) -> str:
    try:
        normalized = config_relative(value, "path")
    except PreflightError as exc:
        raise SafeWriteError(str(exc)) from exc
    if ".git" in PurePosixPath(normalized).parts:
        raise SafeWriteError("Git metadata paths are never writable")
    return normalized


def _scope_allows(target: Path, relative_path: str, config: dict[str, Any]) -> None:
    lists: dict[str, list[str]] = {}
    for name in ("included_paths", "code_paths", "artifact_paths", "excluded_paths"):
        raw = config.get(name, [])
        if not isinstance(raw, list):
            raise SafeWriteError(f"Scope field {name} must be an array")
        try:
            lists[name] = [config_relative(value, name) for value in raw]
        except PreflightError as exc:
            raise SafeWriteError(str(exc)) from exc

    included, code, artifacts, excluded = (lists[name] for name in
                                            ("included_paths", "code_paths", "artifact_paths", "excluded_paths"))
    if not included or set(included) != set(code) | set(artifacts):
        raise SafeWriteError("The explicit included_paths scope is missing or inconsistent")
    if not any(is_path_or_child(relative_path, prefix) for prefix in included):
        raise SafeWriteError("Destination is outside included_paths")
    classifications = [prefix for prefix in code + artifacts if is_path_or_child(relative_path, prefix)]
    if len(classifications) != 1:
        raise SafeWriteError("Destination must be covered by exactly one code_paths or artifact_paths prefix")
    if any(is_path_or_child(relative_path, prefix) for prefix in excluded):
        raise SafeWriteError("Destination is excluded from write scope")
    working_directories = config.get("authorized_working_directories", [])
    if not isinstance(working_directories, list):
        raise SafeWriteError("authorized_working_directories must be an array")
    allowed = []
    for raw in working_directories:
        try:
            path = canonical(raw)
        except (OSError, TypeError) as exc:
            raise SafeWriteError("An authorized working directory cannot be resolved") from exc
        if path != target and target not in path.parents:
            raise SafeWriteError("An authorized working directory is outside the target")
        allowed.append(path)
    if allowed:
        destination = target.joinpath(*PurePosixPath(relative_path).parts)
        if not any(destination == root or root in destination.parents for root in allowed):
            raise SafeWriteError("Destination is outside authorized_working_directories")


def _run_preflight(target: Path, config_path: Path) -> dict[str, Any]:
    if not NOFOLLOW or not getattr(os, "O_DIRECTORY", 0) or not all(hasattr(os, name) for name in ("fchmod", "fchown", "fsync")) or \
            os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd or os.rename not in os.supports_dir_fd or \
            os.unlink not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd:
        raise SafeWriteError("This platform lacks required no-follow directory-relative write primitives")
    result = Preflight(str(target), str(HARNESS_ROOT), str(config_path), None).run()
    if result.get("overall_result") != "READY":
        raise SafeWriteError("Fresh target preflight did not return READY; no write was attempted")
    return result


def _evidence_context(root: Path, config_sha256: str,
                      report: dict[str, Any], root_fd: int, parent_fd: int) -> dict[str, Any]:
    root_info = os.fstat(root_fd)
    parent_info = os.fstat(parent_fd)
    report_bytes = json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_path": str(root),
        "target_root_device": root_info.st_dev,
        "target_root_inode": root_info.st_ino,
        "destination_parent_device": parent_info.st_dev,
        "destination_parent_inode": parent_info.st_ino,
        "scope_config_sha256": config_sha256,
        "preflight": report["overall_result"],
        "preflight_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "preflight_checks": {item["check_id"]: item["status"] for item in report.get("checks", [])},
    }


def _assert_config_unchanged(config_path: Path, expected_sha256: str) -> None:
    if hashlib.sha256(config_path.read_bytes()).hexdigest() != expected_sha256:
        raise SafeWriteError("Scope configuration changed during the write operation")


def _open_root_and_parent(root: Path, parts: tuple[str, ...]) -> tuple[int, int]:
    root_fd = os.open(root, REQUIRED_DIR_FLAGS)
    try:
        root_info = os.fstat(root_fd)
        path_info = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(root_info.st_mode) or (root_info.st_dev, root_info.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise SafeWriteError("Target root identity changed during validation")
    except BaseException:
        os.close(root_fd)
        raise
    parent_fd = root_fd
    try:
        for component in parts:
            entry_info = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(entry_info.st_mode):
                raise SafeWriteError("A destination parent is not a real directory")
            next_fd = os.open(component, REQUIRED_DIR_FLAGS, dir_fd=parent_fd)
            opened_info = os.fstat(next_fd)
            if (entry_info.st_dev, entry_info.st_ino) != (opened_info.st_dev, opened_info.st_ino):
                os.close(next_fd)
                raise SafeWriteError("A destination parent changed during path resolution")
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
        return root_fd, parent_fd
    except BaseException:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)
        raise


def _verify_parent_still_named(root: Path, parts: tuple[str, ...], root_fd: int, parent_fd: int) -> None:
    check_root_fd, check_parent_fd = _open_root_and_parent(root, parts)
    try:
        expected_root = os.fstat(root_fd)
        actual_root = os.fstat(check_root_fd)
        expected_parent = os.fstat(parent_fd)
        actual_parent = os.fstat(check_parent_fd)
        if (expected_root.st_dev, expected_root.st_ino) != (actual_root.st_dev, actual_root.st_ino):
            raise SafeWriteError("Target root identity changed before the write")
        if (expected_parent.st_dev, expected_parent.st_ino) != (actual_parent.st_dev, actual_parent.st_ino):
            raise SafeWriteError("Destination parent identity changed before the write")
    finally:
        if check_parent_fd != check_root_fd:
            os.close(check_parent_fd)
        os.close(check_root_fd)


def _stat_destination(parent_fd: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SafeWriteError(f"Could not inspect destination entry: {exc.strerror}") from exc


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise SafeWriteError("Write returned no progress")
        view = view[written:]


def _prepare_preflight(target: str | Path, config_path: str | Path,
                       relative_path: str) -> tuple[Path, Path, str, dict[str, Any], str]:
    target_root = canonical(target)
    config_file = canonical(config_path)
    relative = _normalize_destination(relative_path)
    try:
        config_relative_path = config_file.relative_to(target_root).as_posix()
    except ValueError:
        config_relative_path = None
    if config_relative_path == relative:
        raise SafeWriteError("The scope configuration cannot be modified through its own authorization")
    config_bytes = config_file.read_bytes()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    config = json.loads(config_bytes.decode("utf-8"))
    if not isinstance(config, dict):
        raise SafeWriteError("Scope configuration must be a JSON object")
    configured_target = config.get("target_path")
    if configured_target is not None:
        if not isinstance(configured_target, str):
            raise SafeWriteError("Configured target_path must be a string")
        if canonical(configured_target) != target_root:
            raise SafeWriteError("Configured target_path differs from the explicit target")
    _scope_allows(target_root, relative, config)
    report = _run_preflight(target_root, config_file)
    if hashlib.sha256(config_file.read_bytes()).hexdigest() != config_digest:
        raise SafeWriteError("Scope configuration changed during preflight; no write was attempted")
    if report.get("target_path") != str(target_root):
        raise SafeWriteError("Preflight target identity differs from the explicit target")
    return target_root, config_file, relative, report, config_digest


def _open_existing(parent_fd: int, name: str) -> tuple[int, os.stat_result, str]:
    entry = _stat_destination(parent_fd, name)
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        raise SafeWriteError("Existing destination must be a single-link regular file")
    if getattr(entry, "st_flags", 0):
        raise SafeWriteError("Existing destination has file flags the safe writer does not preserve")
    fd = os.open(name, os.O_RDONLY | NOFOLLOW | NONBLOCK, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
            raise SafeWriteError("Destination entry and opened file identity do not match safely")
        digest = _sha256_fd(fd)
        return fd, opened, digest
    except BaseException:
        os.close(fd)
        raise


def create_file(target: str | Path, config_path: str | Path, relative_path: str,
                content: bytes) -> dict[str, Any]:
    """Create a new file using exclusive creation; never replace a destination."""
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    root, config_file, relative, report, config_digest = _prepare_preflight(target, config_path, relative_path)
    path = PurePosixPath(relative)
    root_fd, parent_fd = _open_root_and_parent(root, tuple(path.parts[:-1]))
    name = path.name
    created_fd: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        _verify_parent_still_named(root, tuple(path.parts[:-1]), root_fd, parent_fd)
        _assert_config_unchanged(config_file, config_digest)
        created_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o666, dir_fd=parent_fd)
        created = os.fstat(created_fd)
        if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1:
            raise SafeWriteError("New destination is not a single-link regular file")
        created_identity = (created.st_dev, created.st_ino)
        _write_all(created_fd, content)
        os.fsync(created_fd)
        final = os.fstat(created_fd)
        entry = _stat_destination(parent_fd, name)
        if (entry.st_dev, entry.st_ino) != created_identity or (final.st_dev, final.st_ino) != created_identity or final.st_nlink != 1:
            raise SafeWriteError("New destination identity changed during writing")
        evidence = _evidence_context(root, config_digest, report, root_fd, parent_fd)
        os.fsync(parent_fd)
        return {**evidence, "operation": "create", "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "device": final.st_dev, "inode": final.st_ino, "link_count": final.st_nlink,
                "mode": stat.S_IMODE(final.st_mode), "owner_uid": final.st_uid,
                "owner_gid": final.st_gid}
    except BaseException as operation_error:
        if created_fd is not None and created_identity is not None:
            try:
                entry = _stat_destination(parent_fd, name)
                if (entry.st_dev, entry.st_ino) == created_identity:
                    os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                raise SafeWriteError(f"{operation_error}; cleanup of the newly created destination also failed: {cleanup_error}") from operation_error
        raise
    finally:
        if created_fd is not None:
            os.close(created_fd)
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def create_directories(target: str | Path, config_path: str | Path,
                       relative_path: str) -> dict[str, Any]:
    """Create missing directory components exclusively beneath authorized scopes.

    Existing parents are opened without following symlinks and are never
    changed directly. Each missing component needs its own explicit scope
    authorization. If a later component fails, earlier created directories
    are retained and reported; this operation never removes directories.
    """
    root, config_file, relative, report, config_digest = _prepare_preflight(
        target, config_path, relative_path
    )
    path = PurePosixPath(relative)
    parts = tuple(path.parts)
    if not parts:
        raise SafeWriteError("The target root is not a creatable destination")

    # Load the already-digested configuration for component-by-component
    # authorization. _prepare_preflight has validated its shape and target.
    config = json.loads(config_file.read_text(encoding="utf-8"))
    root_fd = os.open(root, REQUIRED_DIR_FLAGS)
    parent_fd = root_fd
    parent_parts: tuple[str, ...] = ()
    created: list[dict[str, Any]] = []
    created_but_unverified: str | None = None
    try:
        root_info = os.fstat(root_fd)
        root_path_info = os.stat(root, follow_symlinks=False)
        if (not stat.S_ISDIR(root_info.st_mode) or
                (root_info.st_dev, root_info.st_ino) != (root_path_info.st_dev, root_path_info.st_ino)):
            raise SafeWriteError("Target root identity changed during directory validation")

        missing_at: int | None = None
        # Resolve the existing prefix one component at a time with no-follow
        # directory handles. Never use Path.exists()/resolve() on the requested
        # destination, because either may traverse a symlink.
        for index, component in enumerate(parts):
            _verify_parent_still_named(root, parent_parts, root_fd, parent_fd)
            try:
                entry = _stat_destination(parent_fd, component)
            except FileNotFoundError:
                missing_at = index
                break
            if not stat.S_ISDIR(entry.st_mode):
                raise SafeWriteError("An existing directory path component is not a real directory")
            try:
                next_fd = os.open(component, REQUIRED_DIR_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise SafeWriteError(f"Could not open an existing directory without following links: {exc.strerror}") from exc
            try:
                opened = os.fstat(next_fd)
                if (not stat.S_ISDIR(opened.st_mode) or
                        (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)):
                    raise SafeWriteError("An existing directory changed during no-follow path resolution")
            except BaseException:
                os.close(next_fd)
                raise
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
            parent_parts = parts[:index + 1]

        if missing_at is None:
            raise FileExistsError(relative)

        # Pre-authorize all components that would be created before making the
        # first filesystem change, so a narrow child scope cannot implicitly
        # authorize its missing parent.
        for index in range(missing_at, len(parts)):
            component_path = PurePosixPath(*parts[:index + 1]).as_posix()
            _scope_allows(root, component_path, config)

        for index in range(missing_at, len(parts)):
            component = parts[index]
            component_path = PurePosixPath(*parts[:index + 1]).as_posix()
            _verify_parent_still_named(root, parent_parts, root_fd, parent_fd)
            _assert_config_unchanged(config_file, config_digest)
            # mkdirat is exclusive: an existing entry, including a symlink,
            # causes failure and is never reused or replaced.
            os.mkdir(component, 0o777, dir_fd=parent_fd)
            created_but_unverified = component_path
            entry = _stat_destination(parent_fd, component)
            if not stat.S_ISDIR(entry.st_mode):
                raise SafeWriteError("New directory entry is not a real directory")
            next_fd = os.open(component, REQUIRED_DIR_FLAGS, dir_fd=parent_fd)
            try:
                opened = os.fstat(next_fd)
                named_again = _stat_destination(parent_fd, component)
                if (not stat.S_ISDIR(opened.st_mode) or
                        (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino) or
                        (named_again.st_dev, named_again.st_ino) != (opened.st_dev, opened.st_ino)):
                    raise SafeWriteError("New directory identity or type changed during verification")
                created.append({
                    "path": component_path,
                    "device": opened.st_dev,
                    "inode": opened.st_ino,
                    "mode": stat.S_IMODE(opened.st_mode),
                    "owner_uid": opened.st_uid,
                    "owner_gid": opened.st_gid,
                })
                created_but_unverified = None
                _verify_parent_still_named(root, parent_parts, root_fd, parent_fd)
                os.fsync(parent_fd)
            except BaseException:
                os.close(next_fd)
                raise
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
            parent_parts = parts[:index + 1]

        # Confirm the final requested path still resolves to the opened
        # directory through the target root, then obtain its actual parent for
        # evidence consistent with file operations.
        _verify_parent_still_named(root, parts, root_fd, parent_fd)
        evidence_root_fd, evidence_parent_fd = _open_root_and_parent(root, parts[:-1])
        try:
            if (os.fstat(evidence_root_fd).st_ino, os.fstat(evidence_root_fd).st_dev) != (root_info.st_ino, root_info.st_dev):
                raise SafeWriteError("Target root identity changed before directory evidence was recorded")
            final_entry = _stat_destination(evidence_parent_fd, parts[-1])
            final_info = os.fstat(parent_fd)
            if (not stat.S_ISDIR(final_entry.st_mode) or
                    (final_entry.st_dev, final_entry.st_ino) != (final_info.st_dev, final_info.st_ino)):
                raise SafeWriteError("Final directory name and opened identity do not match")
            evidence = _evidence_context(root, config_digest, report, evidence_root_fd, evidence_parent_fd)
            os.fsync(evidence_parent_fd)
            return {
                **evidence,
                "operation": "mkdir",
                "path": relative,
                "created_directories": created,
                "device": final_info.st_dev,
                "inode": final_info.st_ino,
                "mode": stat.S_IMODE(final_info.st_mode),
                "owner_uid": final_info.st_uid,
                "owner_gid": final_info.st_gid,
            }
        finally:
            if evidence_parent_fd != evidence_root_fd:
                os.close(evidence_parent_fd)
            os.close(evidence_root_fd)
    except (OSError, SafeWriteError) as exc:
        if not created and created_but_unverified is None:
            raise
        try:
            partial = _evidence_context(root, config_digest, report, root_fd, parent_fd)
        except (OSError, SafeWriteError):
            partial = {
                "target_path": str(root),
                "scope_config_sha256": config_digest,
                "preflight": report.get("overall_result"),
            }
        partial.update({
            "operation": "mkdir",
            "path": relative,
            "created_directories": created,
            "created_path_unverified": created_but_unverified,
        })
        raise SafeWriteError(
            f"Directory creation stopped after a partial result; created directories were left in place: {exc}",
            evidence=partial,
        ) from exc
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def replace_file(target: str | Path, config_path: str | Path, relative_path: str,
                 content: bytes, expected_sha256: str) -> dict[str, Any]:
    """Replace one file after checking its expected baseline and current identity.

    The single-writer precondition is required. The final rename is atomic for
    readers but cannot exclude a non-cooperating writer racing after recheck.
    """
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise SafeWriteError("expected_sha256 must be a lowercase SHA-256 digest")
    root, config_file, relative, report, config_digest = _prepare_preflight(target, config_path, relative_path)
    path = PurePosixPath(relative)
    parent_parts = tuple(path.parts[:-1])
    name = path.name
    root_fd, parent_fd = _open_root_and_parent(root, parent_parts)
    original_fd: int | None = None
    temp_name: str | None = None
    temp_fd: int | None = None
    try:
        _verify_parent_still_named(root, parent_parts, root_fd, parent_fd)
        original_fd, original_info, original_digest = _open_existing(parent_fd, name)
        if original_digest != expected_sha256:
            raise SafeWriteError("Destination content differs from the workflow's expected baseline hash")
        temp_info: os.stat_result | None = None
        for _ in range(10):
            candidate = f".harness-write-{secrets.token_hex(12)}.tmp"
            try:
                temp_fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=parent_fd)
                temp_name = candidate
                break
            except FileExistsError:
                continue
        if temp_fd is None or temp_name is None:
            raise SafeWriteError("Could not allocate an exclusive temporary file")
        try:
            os.fchown(temp_fd, original_info.st_uid, original_info.st_gid)
        except PermissionError as exc:
            raise SafeWriteError("Could not preserve destination ownership on the replacement file") from exc
        os.fchmod(temp_fd, stat.S_IMODE(original_info.st_mode))
        _write_all(temp_fd, content)
        os.fsync(temp_fd)
        temp_info = os.fstat(temp_fd)
        if not stat.S_ISREG(temp_info.st_mode) or temp_info.st_nlink != 1:
            raise SafeWriteError("Replacement temporary file is not a single-link regular file")

        _assert_config_unchanged(config_file, config_digest)
        _verify_parent_still_named(root, parent_parts, root_fd, parent_fd)
        current_entry = _stat_destination(parent_fd, name)
        current_fd_info = os.fstat(original_fd)
        current_digest = _sha256_fd(original_fd)
        if (not stat.S_ISREG(current_entry.st_mode) or current_entry.st_nlink != 1 or
                (current_entry.st_dev, current_entry.st_ino) != (original_info.st_dev, original_info.st_ino) or
                _identity(current_entry) != _identity(original_info) or
                _identity(current_fd_info) != _identity(original_info) or current_digest != expected_sha256):
            raise SafeWriteError("Destination identity, link count, metadata, or content changed before replacement")
        os.rename(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temp_name = None
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise SafeWriteError("Replacement completed but directory fsync failed; inspect the destination before retrying") from exc
        result_fd, result_info, result_digest = _open_existing(parent_fd, name)
        try:
            if (result_digest != hashlib.sha256(content).hexdigest() or
                    stat.S_IMODE(result_info.st_mode) != stat.S_IMODE(original_info.st_mode) or
                    result_info.st_uid != original_info.st_uid or result_info.st_gid != original_info.st_gid):
                raise SafeWriteError("Replacement completed but post-write content or metadata verification failed")
            evidence = _evidence_context(root, config_digest, report, root_fd, parent_fd)
            return {**evidence, "operation": "replace", "path": relative,
                    "before_sha256": original_digest,
                    "after_sha256": result_digest, "before_device": original_info.st_dev,
                    "before_inode": original_info.st_ino, "after_device": result_info.st_dev,
                    "after_inode": result_info.st_ino, "link_count": result_info.st_nlink,
                    "mode": stat.S_IMODE(result_info.st_mode), "owner_uid": result_info.st_uid,
                    "owner_gid": result_info.st_gid}
        finally:
            os.close(result_fd)
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_name is not None:
            active_error = sys.exc_info()[1]
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                if active_error is None:
                    raise SafeWriteError(f"Temporary replacement file cleanup failed: {cleanup_error}") from cleanup_error
                raise SafeWriteError(f"{active_error}; temporary replacement file cleanup also failed: {cleanup_error}") from active_error
        if original_fd is not None:
            os.close(original_fd)
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scope-checked target directory/file write helper")
    parser.add_argument("operation", choices=("mkdir", "create", "replace"))
    parser.add_argument("--target", required=True, help="Explicit canonical target repository root")
    parser.add_argument("--config", required=True, help="Existing JSON preflight scope configuration")
    parser.add_argument("--path", required=True, help="Authorized repository-relative destination")
    parser.add_argument("--expected-sha256", help="Required baseline content hash for replacement")
    args = parser.parse_args(argv)
    try:
        if args.operation == "mkdir":
            if args.expected_sha256 is not None:
                raise SafeWriteError("--expected-sha256 is valid only for replacement")
            result = create_directories(args.target, args.config, args.path)
        elif args.operation == "create":
            data = sys.stdin.buffer.read()
            if args.expected_sha256 is not None:
                raise SafeWriteError("--expected-sha256 is valid only for replacement")
            result = create_file(args.target, args.config, args.path, data)
        else:
            data = sys.stdin.buffer.read()
            if args.expected_sha256 is None:
                raise SafeWriteError("Replacement requires --expected-sha256 from the recorded baseline")
            result = replace_file(args.target, args.config, args.path, data, args.expected_sha256)
        json.dump({"status": "PASS", "evidence": result}, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except (OSError, TypeError, ValueError, SafeWriteError) as exc:
        failure = {"status": "FAIL", "error": str(exc)}
        if isinstance(exc, SafeWriteError) and exc.evidence is not None:
            failure["evidence"] = exc.evidence
        json.dump(failure, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

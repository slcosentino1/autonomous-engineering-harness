"""Small cross-process locks for target-local workflow operations."""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from workflow.workspace import WORKFLOW_LOCK_NAME_RE, WORKFLOW_ROOT

_LOCK_NAME = WORKFLOW_LOCK_NAME_RE


class ProcessLockError(RuntimeError):
    """A lock path could not be used or is currently held."""


def _workflows_directory(target: Path) -> Path:
    current = target
    for part in Path(WORKFLOW_ROOT).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise ProcessLockError(f"Workflow path contains a symlink or non-directory: {current}")
        else:
            try:
                current.mkdir()
            except FileExistsError:
                if current.is_symlink() or not current.is_dir():
                    raise ProcessLockError(f"Workflow path contains a symlink or non-directory: {current}")
            except OSError as exc:
                raise ProcessLockError("Could not create the workflow lock directory.") from exc
    return current


def _lock_fd(fd: int) -> None:
    if os.name == "nt":
        if os.fstat(fd).st_size == 0:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", -1)}:
                raise BlockingIOError(errno.EWOULDBLOCK, "process lock is held") from exc
            raise
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def named_process_lock(target: Path, name: str, *, busy_message: str):
    """Acquire a fail-fast lock stored below the target's workflow artifact directory."""
    if not _LOCK_NAME.fullmatch(name):
        raise ProcessLockError("Invalid workflow lock name.")
    directory = _workflows_directory(target)
    lock_path = directory / name
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ProcessLockError("Workflow process lock is not a regular file.")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ProcessLockError("Could not open workflow process lock.") from exc
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProcessLockError("Workflow process lock is not a regular file.")
        try:
            _lock_fd(fd)
        except BlockingIOError as exc:
            raise ProcessLockError(busy_message) from exc
        except OSError as exc:
            raise ProcessLockError("Could not acquire workflow process lock.") from exc
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock_fd(fd)
        finally:
            os.close(fd)

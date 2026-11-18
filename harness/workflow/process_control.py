"""Best-effort termination for provider process groups."""

from __future__ import annotations

import os
import signal
import subprocess
import time


PROCESS_TERMINATION_GRACE_SECONDS = 2


def process_group_options() -> dict:
    """Start a provider in an independently terminable process group/session."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def process_presence(process_id: object) -> str:
    """Report best-effort PID presence without treating a reused PID as ownership proof."""
    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
        return "not_recorded"
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return "not_running"
    except PermissionError:
        return "present_unverified"
    except OSError:
        return "unknown"
    return "present_unverified"


def terminate_process_group(process: subprocess.Popen, *, grace_seconds: int = PROCESS_TERMINATION_GRACE_SECONDS) -> None:
    """Stop a provider and descendants where the operating system exposes group signals."""
    if os.name == "nt":
        if process.poll() is None:
            try:
                process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            except OSError:
                try:
                    process.terminate()
                except OSError:
                    pass
            try:
                process.wait(timeout=grace_seconds)
                return
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait()
        except OSError:
            pass
        return

    # The group leader may exit while a child still holds the provider's pipes.
    # Signal the group even when process.poll() says the leader has already exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass

    deadline = time.monotonic() + max(0, grace_seconds)
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        except OSError:
            break
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait()
    except OSError:
        pass

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from workflow import codex_executor  # noqa: E402


class CodexExecutorContractTests(unittest.TestCase):
    def request(self, root: Path, *, command=("codex", "exec", "-"), timeout=2, environment=None):
        return codex_executor.CodexExecutionRequest(
            operation_id="operation-123",
            command=tuple(command),
            prompt_bytes=b"explicit prompt bytes",
            working_directory=root,
            timeout_seconds=timeout,
            environment=dict(os.environ) if environment is None else environment,
        )

    def test_request_is_frozen_explicit_and_executor_needs_no_orchestrator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {"PATH": os.environ.get("PATH", "")}
            request = self.request(root, environment=environment)
            original = (request.operation_id, request.command, request.prompt_bytes, dict(request.environment))
            self.assertEqual(
                {item.name for item in fields(request)},
                {
                    "operation_id", "command", "prompt_bytes", "working_directory", "timeout_seconds",
                    "environment",
                },
            )
            with self.assertRaises(FrozenInstanceError):
                request.operation_id = "other-operation"

            def process_runner(command, **kwargs):
                self.assertEqual(command, list(request.command))
                self.assertEqual(kwargs["input"], request.prompt_bytes)
                self.assertEqual(kwargs["cwd"], str(root))
                self.assertEqual(kwargs["env"], environment)
                self.assertEqual(kwargs["timeout"], request.timeout_seconds)
                return SimpleNamespace(returncode=0, stdout=b"stdout", stderr=b"stderr", pid=12345)

            persisted_pids = []
            result = codex_executor.CodexExecutor(process_runner=process_runner).execute(
                request, on_process_started=persisted_pids.append,
            )

            self.assertEqual(result.operation_id, request.operation_id)
            self.assertEqual(result.outcome, "completed")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout, b"stdout")
            self.assertEqual(result.stderr, b"stderr")
            self.assertEqual(result.process_id, 12345)
            self.assertEqual(persisted_pids, [12345])
            self.assertEqual(
                (request.operation_id, request.command, request.prompt_bytes, dict(request.environment)), original,
            )

    def test_nonzero_exit_is_normalized_without_workflow_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self.request(root)
            runner = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 7, b"partial", b"failure")
            result = codex_executor.CodexExecutor(process_runner=runner).execute(
                request, on_process_started=lambda _pid: self.fail("no PID was reported"),
            )
        self.assertEqual(result.outcome, "nonzero_exit")
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stdout, b"partial")
        self.assertEqual(result.stderr, b"failure")
        self.assertIsNone(result.process_id)

    def test_launch_failure_is_distinct_from_an_uncertain_post_launch_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self.request(root)
            launch_failure = codex_executor.CodexExecutor(
                process_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("codex missing")),
            ).execute(request, on_process_started=lambda _pid: None)
            uncertain = codex_executor.CodexExecutor(
                process_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("lost result")),
            ).execute(request, on_process_started=lambda _pid: None)
        self.assertEqual(launch_failure.outcome, "launch_failed")
        self.assertEqual(launch_failure.error_category, "FileNotFoundError")
        self.assertEqual(uncertain.outcome, "result_unavailable")
        self.assertEqual(uncertain.error_category, "RuntimeError")

    def test_timeout_and_cancellation_are_normalized_with_captured_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self.request(root)
            timeout_runner = lambda command, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"partial out", stderr=b"partial err"),
            )
            timeout = codex_executor.CodexExecutor(process_runner=timeout_runner).execute(
                request, on_process_started=lambda _pid: None,
            )
            cancelled = codex_executor.CodexExecutor(
                process_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
            ).execute(request, on_process_started=lambda _pid: None)
        self.assertEqual(timeout.outcome, "timed_out")
        self.assertEqual(timeout.stdout, b"partial out")
        self.assertEqual(timeout.stderr, b"partial err")
        self.assertEqual(cancelled.outcome, "cancelled")

    def test_pid_callback_failure_after_spawn_is_uncertain_not_launch_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self.request(root)
            fake_process = SimpleNamespace(pid=789, returncode=0)
            fake_process.communicate = lambda **_kwargs: (b"", b"")
            with (
                patch("workflow.codex_executor.subprocess.Popen", return_value=fake_process),
                patch("workflow.codex_executor._process_control.terminate_process_group"),
            ):
                result = codex_executor.CodexExecutor().execute(
                    request,
                    on_process_started=lambda _pid: (_ for _ in ()).throw(OSError("PID persistence failed")),
                )
        self.assertEqual(result.outcome, "result_unavailable")
        self.assertEqual(result.process_id, 789)
        self.assertEqual(result.error_category, "OSError")

    def test_pid_is_reported_before_process_output_is_collected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self.request(root)
            observed = []

            class FakeProcess:
                pid = 456
                returncode = 0

                def communicate(self, *, input, timeout):
                    observed.append(("communicate", input, timeout))
                    return b"stdout", b"stderr"

            fake_process = FakeProcess()
            with patch("workflow.codex_executor.subprocess.Popen", return_value=fake_process) as popen:
                result = codex_executor.CodexExecutor().execute(
                    request, on_process_started=lambda process_id: observed.append(("pid", process_id)),
                )
        self.assertEqual(observed[0], ("pid", 456))
        self.assertEqual(observed[1], ("communicate", request.prompt_bytes, request.timeout_seconds))
        self.assertEqual(popen.call_args.args[0], list(request.command))
        self.assertTrue(popen.call_args.kwargs["start_new_session"] if os.name != "nt"
                        else "creationflags" in popen.call_args.kwargs)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.stdout, b"stdout")
        self.assertEqual(result.stderr, b"stderr")

    @unittest.skipIf(os.name == "nt", "POSIX process-group cleanup is covered on POSIX only")
    def test_timeout_terminates_executor_process_group_and_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready_path = root / "child-ready"
            terminated_path = root / "child-terminated"
            child_source = (
                "import pathlib, signal, sys, time\n"
                "ready, terminated = map(pathlib.Path, sys.argv[1:])\n"
                "signal.signal(signal.SIGTERM, lambda *_: (terminated.write_text('yes'), sys.exit(0)))\n"
                "ready.write_text('ready')\n"
                "time.sleep(30)\n"
            )
            parent_source = (
                "import pathlib, subprocess, sys, time\n"
                "print('parent-ready', flush=True)\n"
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])\n"
                "deadline = time.monotonic() + 5\n"
                "while not pathlib.Path(sys.argv[2]).exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
                "time.sleep(30)\n"
            )
            request = self.request(
                root,
                command=(sys.executable, "-c", parent_source, child_source, str(ready_path), str(terminated_path)),
                timeout=0.8,
            )
            result = codex_executor.CodexExecutor().execute(
                request, on_process_started=lambda _pid: None,
            )
            self.assertEqual(result.outcome, "timed_out")
            self.assertIn(b"parent-ready", result.stdout or b"")
            self.assertTrue(ready_path.exists(), "child process did not start before timeout")
            self.assertTrue(terminated_path.exists(), "executor did not terminate its child process group")


if __name__ == "__main__":
    unittest.main()

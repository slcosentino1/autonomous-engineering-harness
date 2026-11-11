from __future__ import annotations

import json
import selectors
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import claude_reviewer  # noqa: E402
import orchestrator  # noqa: E402


def make_target(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Harness Test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "harness-test@example.invalid"], check=True,
    )
    (path / "README.md").write_text("target\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "baseline"], check=True)
    return path


def wait_for_line(process: subprocess.Popen, expected: str) -> None:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        if not selector.select(timeout=15):
            raise AssertionError(
                f"child did not report {expected!r}; exit={process.poll()}"
            )
        line = process.stdout.readline().strip()
        if line != expected:
            raise AssertionError(
                f"expected {expected!r}, got {line!r}; exit={process.poll()}"
            )
    finally:
        selector.close()


class WorkflowConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-lock-tests-")
        self.root = Path(self.temp.name)
        self.target = make_target(self.root / "target")

    def tearDown(self):
        self.temp.cleanup()

    def start_blocked_workflow(self, workflow_id: str) -> tuple[subprocess.Popen, Path]:
        script = f"""
import json, sys
sys.path.insert(0, {str(ROOT / 'harness')!r})
import claude_reviewer, orchestrator

class FakeCodex:
    responses = [
        "# Proposal\\nCoordinate workflow state across processes.\\n",
        "# Refined proposal\\nUse a per-workflow process lock.\\n\\n"
        "## Claude finding dispositions\\n- No actionable findings.\\n\\n"
        "Second plan review needed: no\\n",
    ]
    def __call__(self, command, **kwargs):
        path = command[command.index("--output-last-message") + 1]
        with open(path, "w", encoding="utf-8") as output:
            output.write(self.responses.pop(0))
        return orchestrator.subprocess.CompletedProcess(command, 0, b"", b"")

def fake_claude(_prompt, *, budget_usd, executable):
    print("ACTIVE_CALL", flush=True)
    if sys.stdin.readline().strip() != "continue":
        raise RuntimeError("test parent did not release the fake provider")
    return claude_reviewer.ReviewResult("Plan looks good.", 0.125)

runner = orchestrator.Orchestrator(
    {str(self.target)!r}, task="Coordinate workflow state across processes", budget_usd=1.0,
    workflow_id={workflow_id!r}, process_runner=FakeCodex(), claude_runner=fake_claude,
)
state = runner.start()
print("DONE " + json.dumps({{"status": state["status"], "budget_unknown": state["authorization"]["claude_aily"]["budget_unknown"]}}), flush=True)
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            wait_for_line(process, "ACTIVE_CALL")
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            self.close_process(process)
            raise
        state_path = self.target / ".engineering" / "workflows" / workflow_id / "state.json"
        return process, state_path

    def finish_blocked_workflow(self, process: subprocess.Popen) -> dict:
        assert process.stdin is not None
        process.stdin.write("continue\n")
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stderr)
        line = next((line for line in stdout.splitlines() if line.startswith("DONE ")), None)
        self.assertIsNotNone(line, stdout)
        result = json.loads(line.removeprefix("DONE "))
        self.close_process(process)
        return result

    @staticmethod
    def close_process(process: subprocess.Popen) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def run_cli(self, command: str, workflow_id: str):
        return subprocess.run(
            [
                sys.executable, str(ROOT / "harness" / "orchestrator.py"), command,
                "--target", str(self.target), "--workflow-id", workflow_id,
                # Fail closed without reaching a real provider if a regression
                # accidentally takes the second process past its state checks.
                "--aily", "/usr/bin/false",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_status_during_active_call_is_read_only_and_reports_active_call(self):
        process, state_path = self.start_blocked_workflow("status-during-call")
        try:
            before = state_path.read_bytes()
            result = self.run_cli("status", "status-during-call")
            self.assertIn(result.returncode, (0, 1), result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "running")
            self.assertEqual(summary["active_call"]["stage"], "plan_review_1")
            self.assertFalse(summary["budget_unknown"])
            self.assertEqual(state_path.read_bytes(), before)

            completed = self.finish_blocked_workflow(process)
            self.assertEqual(completed["status"], "awaiting_approval")
            final = json.loads(state_path.read_text(encoding="utf-8"))
            authorization = final["authorization"]["claude_aily"]
            self.assertEqual(final["plan_reviews"][0]["report"], "Plan looks good.")
            self.assertEqual(final["claude_contexts"][0]["cost_usd"], 0.125)
            self.assertFalse(authorization["budget_unknown"])
            self.assertIsNone(authorization["active_call"])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            self.close_process(process)

    def test_second_mutating_process_cannot_interfere_with_active_call(self):
        process, state_path = self.start_blocked_workflow("mutation-during-call")
        try:
            before = state_path.read_bytes()
            result = self.run_cli("resume", "mutation-during-call")
            self.assertEqual(result.returncode, 2)
            self.assertIn("already being modified by another process", result.stderr)
            self.assertEqual(state_path.read_bytes(), before)

            completed = self.finish_blocked_workflow(process)
            self.assertEqual(completed["status"], "awaiting_approval")
            final = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(final["authorization"]["claude_aily"]["budget_unknown"])
            self.assertEqual(len(final["plan_reviews"]), 1)
            self.assertEqual(len(final["claude_contexts"]), 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            self.close_process(process)

    def test_ownerless_active_call_still_sets_unknown_cost_without_retry(self):
        claude_calls = []

        def claude(*_args, **_kwargs):
            claude_calls.append("called")
            return claude_reviewer.ReviewResult("Plan looks good.", 0.125)

        codex = self._fake_codex()
        runner = orchestrator.Orchestrator(
            self.target,
            task="Coordinate workflow state across processes",
            budget_usd=1.0,
            workflow_id="ownerless-active-call",
            process_runner=codex,
            claude_runner=claude,
        )
        runner.start()
        with runner._workflow_lock():
            runner.state["status"] = "failed"
            runner.state["stage"] = "plan_review_1"
            runner.state["authorization"]["claude_aily"]["active_call"] = {
                "stage": "plan_review_1", "started_at": "test", "cap_usd": 0.875,
            }
            runner._save()

        with self.assertRaisesRegex(orchestrator.WorkflowError, "prior Claude call ended without a recorded result"):
            orchestrator.Orchestrator(
                self.target,
                workflow_id="ownerless-active-call",
                process_runner=codex,
                claude_runner=claude,
            ).resume()

        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        authorization = state["authorization"]["claude_aily"]
        self.assertTrue(authorization["budget_unknown"])
        self.assertIsNone(authorization["active_call"])
        self.assertEqual(authorization["spent_usd"], 0.125)
        self.assertEqual(claude_calls, ["called"])

    @staticmethod
    def _fake_codex():
        responses = [
            "# Proposal\nCoordinate workflow state across processes.\n",
            "# Refined proposal\nUse a per-workflow process lock.\n\n"
            "## Claude finding dispositions\n- No actionable findings.\n\n"
            "Second plan review needed: no\n",
        ]
        def run(command, **_kwargs):
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(responses.pop(0), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        return run

    def test_lock_releases_after_normal_exit_exception_and_process_termination(self):
        workflow_id = "lock-lifecycle"
        runner = orchestrator.Orchestrator(self.target, workflow_id=workflow_id)
        lock_path = self.target / ".engineering" / "workflows" / f".{workflow_id}.lock"

        with runner._workflow_lock():
            self.assertTrue(lock_path.is_file())
        with runner._workflow_lock():
            pass
        other = orchestrator.Orchestrator(self.target, workflow_id="another-workflow")
        with runner._workflow_lock():
            with other._workflow_lock():
                pass

        with self.assertRaisesRegex(RuntimeError, "test exception"):
            with runner._workflow_lock():
                raise RuntimeError("test exception")
        with runner._workflow_lock():
            pass

        script = f"""
import sys
sys.path.insert(0, {str(ROOT / 'harness')!r})
import orchestrator
runner = orchestrator.Orchestrator({str(self.target)!r}, workflow_id={workflow_id!r})
with runner._workflow_lock():
    print("LOCKED", flush=True)
    sys.stdin.readline()
"""
        owner = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            wait_for_line(owner, "LOCKED")
            with self.assertRaisesRegex(orchestrator.WorkflowError, "already being modified"):
                with runner._workflow_lock():
                    pass
            owner.terminate()
            owner.wait(timeout=10)
            owner.communicate(timeout=5)
            self.assertTrue(lock_path.is_file(), "lock file existence must not represent ownership")
            with runner._workflow_lock():
                pass
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.communicate(timeout=5)
            self.close_process(owner)


if __name__ == "__main__":
    unittest.main()

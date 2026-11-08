import json
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "harness"))

from runner import (  # noqa: E402
    RunnerError,
    _workflow_id,
    _codex_output_summary,
    _runtime_bundle_files,
    _runtime_bundle_unchanged,
    completion_status,
    invoke_codex,
    operating_prompt,
    run_task,
)
import runner  # noqa: E402


class WorkflowRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-runner-test-")
        self.target = Path(self.temp.name) / "target"
        self.target.mkdir()
        self.workflow_id = "codex-test-workflow"
        self.state_path = self.target / ".engineering" / "workflows" / self.workflow_id / "workflow-state.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_runner_cli_warns_and_points_to_orchestrator(self):
        stderr = io.StringIO()
        with patch.object(runner, "run_task", return_value={"status": "COMPLETED"}), \
                redirect_stderr(stderr):
            result = runner.main(["--target", str(self.target), "--task", "Example task"])

        self.assertEqual(result, 0)
        self.assertIn("DEPRECATED", stderr.getvalue())
        self.assertIn("harness/orchestrator.py", stderr.getvalue())

    def state(self, *, status="COMPLETED", phase="COMPLETED", outcome="APPROVE", root=None):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "workflow_id": self.workflow_id,
            "target": {"git_root": str(root or self.target.resolve())},
            "phase": phase,
            "status": status,
            "completion": {
                "gate_outcome": outcome,
                "approved_revision_id": "content:" + "a" * 64,
                "gate_decision_reference": ".engineering/workflows/example/verification-decision.json",
                "final_evidence_ids": ["EV-1"],
            },
        }
        self.state_path.write_text(json.dumps(record), encoding="utf-8")

    @staticmethod
    def ready_preflight(command, **kwargs):
        target = Path(command[command.index("--target") + 1]).resolve()
        return subprocess.CompletedProcess(command, 0, json.dumps({"overall_result": "READY", "target_path": str(target)}), "")

    def test_workflow_id_is_stable_for_resume(self):
        first = _workflow_id(self.target.resolve(), "Add a small feature")
        self.assertEqual(first, _workflow_id(self.target.resolve(), "Add a small feature"))
        self.assertNotEqual(first, _workflow_id(self.target.resolve(), "A different feature"))

    def test_prompt_contains_task_target_and_existing_operating_contracts(self):
        prompt = operating_prompt(self.target.resolve(), self.workflow_id, "Add the feature")
        self.assertIn(str(self.target.resolve()), prompt)
        self.assertIn(self.workflow_id, prompt)
        self.assertIn("Add the feature", prompt)
        self.assertIn("harness/prompts/06-autonomous-single-agent.md", prompt)
        self.assertIn("verification-policy.yaml", prompt)

    def test_runtime_snapshot_contains_required_tools_and_is_hash_checked(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir()
        manifest = _runtime_bundle_files(runtime)
        self.assertIn("harness/preflight.py", manifest)
        self.assertIn("harness/safe_writer.py", manifest)
        self.assertIn("harness/claude_reviewer.py", manifest)
        self.assertIn("harness/GOALS.md", manifest)
        self.assertTrue(_runtime_bundle_unchanged(runtime, manifest))
        (runtime / "harness" / "safe_writer.py").write_text("changed", encoding="utf-8")
        self.assertFalse(_runtime_bundle_unchanged(runtime, manifest))

    def test_claude_budget_is_explicit_in_prompt_and_forwarded_to_child(self):
        prompt = operating_prompt(self.target.resolve(), self.workflow_id, "Task",
                                  claude_budget_usd=2.0)
        self.assertIn("USD 2.00 total", prompt)
        self.assertIn("--max-budget-usd", prompt)
        captured = {}

        def fake(command, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        invoke_codex(self.target, prompt, executable=sys.executable,
                     claude_budget_usd=2.0, runner=fake)
        self.assertEqual(captured["env"]["HARNESS_CLAUDE_TOTAL_BUDGET_USD"], "2.00")

    def test_codex_command_uses_bounded_workspace_sandbox_and_never_bypass_flags(self):
        seen = {}
        runtime_root = Path(self.temp.name) / "runtime"
        runtime_root.mkdir()

        def fake(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(command, 0, b"{}\n", b"")

        result = invoke_codex(self.target, "task", executable=sys.executable,
                              runtime_root=runtime_root, runner=fake)
        command = seen["command"]
        self.assertEqual(result.returncode, 0)
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertEqual(command[command.index("--add-dir") + 1], str(runtime_root))
        self.assertEqual(seen["kwargs"]["cwd"], str(self.target))
        self.assertEqual(seen["kwargs"]["input"], b"task")

    def test_preflight_failure_prevents_agent_launch(self):
        calls = []

        def blocked(command, **kwargs):
            return subprocess.CompletedProcess(command, 2, json.dumps({
                "overall_result": "BLOCKED",
                "target_path": str(self.target.resolve()),
                "blockers": [{"check_id": "git.isolation", "explanation": "shared repository"}],
            }), "")

        with self.assertRaisesRegex(RunnerError, "preflight is BLOCKED"):
            run_task(str(self.target), "Task", workflow_id=self.workflow_id,
                     preflight_runner=blocked, codex_runner=lambda *a, **kw: calls.append(a))
        self.assertEqual(calls, [])

    def test_codex_failure_is_reported_as_resumable_not_success(self):
        def failed(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, b"", b"401 Unauthorized")

        with self.assertRaisesRegex(RunnerError, "same --workflow-id"):
            run_task(str(self.target), "Task", workflow_id=self.workflow_id,
                     preflight_runner=self.ready_preflight, codex_runner=failed)

    def test_jsonl_agent_error_is_retained_when_gate_state_is_missing(self):
        output = b'\n'.join((
            b'{"type":"turn.started"}',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"Authentication is unavailable."}}',
            b'{"type":"turn.completed"}',
        ))
        self.assertEqual(_codex_output_summary(output), "Authentication is unavailable.")

        def completed_without_state(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, output, b"")

        with self.assertRaisesRegex(RunnerError, "Codex final response: Authentication is unavailable"):
            run_task(str(self.target), "Task", workflow_id=self.workflow_id,
                     preflight_runner=self.ready_preflight, codex_runner=completed_without_state)

    def test_zero_exit_requires_matching_completed_approved_state(self):
        self.state()
        process = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"{}\n", b"")
        result = run_task(str(self.target), "Task", workflow_id=self.workflow_id,
                          preflight_runner=self.ready_preflight, codex_runner=process)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["gate_outcome"], "APPROVE")

    def test_success_is_rejected_for_blocked_or_unapproved_workflow(self):
        for status, phase, outcome in (("BLOCKED", "TESTING", None), ("COMPLETED", "COMPLETED", "ESCALATE")):
            with self.subTest(status=status, outcome=outcome):
                self.state(status=status, phase=phase, outcome=outcome)
                with self.assertRaises(RunnerError):
                    completion_status(self.target.resolve(), self.workflow_id)

    def test_missing_state_prevents_success(self):
        with self.assertRaisesRegex(RunnerError, "without persisting matching workflow state"):
            completion_status(self.target.resolve(), self.workflow_id)

    def test_workflow_state_symlink_is_rejected(self):
        outside = Path(self.temp.name) / "outside-state.json"
        outside.write_text(json.dumps({"workflow_id": self.workflow_id}), encoding="utf-8")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.symlink_to(outside)
        with self.assertRaisesRegex(RunnerError, "symlink"):
            completion_status(self.target.resolve(), self.workflow_id)

    def test_duplicate_matching_workflow_state_is_rejected(self):
        self.state()
        duplicate = self.target / "custom" / "workflow-state.json"
        duplicate.parent.mkdir()
        duplicate.write_text(self.state_path.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(RunnerError, "More than one workflow state"):
            completion_status(self.target.resolve(), self.workflow_id)


if __name__ == "__main__":
    unittest.main()

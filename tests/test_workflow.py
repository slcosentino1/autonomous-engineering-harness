from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import claude_reviewer  # noqa: E402
import orchestrator  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def make_target(root: Path) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git(root, "config", "user.name", "Harness Test")
    git(root, "config", "user.email", "harness-test@example.invalid")
    (root / "README.md").write_text("target\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "baseline")
    return root


class FakeCodex:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.commands: list[list[str]] = []
        self.prompts: list[str] = []
        self.on_call = None

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.prompts.append(kwargs["input"].decode("utf-8"))
        if not self.responses:
            raise AssertionError("Unexpected Codex call")
        output_path = Path(command[command.index("--output-last-message") + 1])
        response = self.responses.pop(0)
        if self.on_call is not None:
            self.on_call(len(self.commands) - 1, command, kwargs)
        output_path.write_text(response, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")


class FakeClaude:
    def __init__(self, *results):
        self.results = list(results)
        self.prompts: list[str] = []
        self.budgets: list[float] = []

    def __call__(self, prompt, *, budget_usd, executable):
        self.prompts.append(prompt)
        self.budgets.append(budget_usd)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="simple-harness-")
        self.root = Path(self.temp.name)
        self.target = make_target(self.root / "target")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def refined(second_review: str = "no") -> str:
        return f"# Refined proposal\n\n## Scope\nAdd the requested behavior.\n\nSecond plan review needed: {second_review}\n"

    def test_plan_is_saved_and_implementation_waits_for_explicit_approval(self):
        codex = FakeCodex(
            "# Proposal\n\n## Approach\nImplement the request and run the unit tests.\n",
            self.refined(),
            "Implemented the change.\nTests: `python -m unittest`; passed.\n",
            "No valid findings required changes. Tests remain passing.\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nThe proposal covers the request.", 0.12),
            claude_reviewer.ReviewResult("Decision: APPROVED\nNo code findings.", 0.18),
        )
        (self.target / "src").mkdir()
        codex.on_call = lambda index, _command, _kwargs: (
            (self.target / "src" / "feature.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
            if index == 2 else None
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="approval-flow",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertTrue((runner.workflow_dir / "proposal.md").is_file())
        self.assertEqual(len(codex.commands), 2)
        self.assertFalse(any("workspace-write" in command for command in codex.commands))
        self.assertIsNone(state["implementation_approved_at"])

        approved = orchestrator.Orchestrator(
            self.target, workflow_id="approval-flow", process_runner=codex, claude_runner=claude,
        ).approve()
        self.assertEqual(approved["status"], "complete")
        self.assertEqual(len(codex.commands), 3)
        self.assertEqual([command[command.index("--sandbox") + 1] for command in codex.commands],
                         ["read-only", "read-only", "workspace-write"])
        self.assertTrue(all(command[command.index("--ask-for-approval") + 1] == "never"
                            for command in codex.commands))
        self.assertEqual(approved["spent_usd"], 0.3)
        self.assertIn("return 42", claude.prompts[1])
        self.assertIn("commit, push", codex.prompts[2])
        self.assertTrue((runner.workflow_dir / "delivery.md").is_file())
        self.assertIn("python -m unittest", (runner.workflow_dir / "delivery.md").read_text())

    def test_plan_review_is_limited_to_two_iterations(self):
        codex = FakeCodex(
            "# Proposal\nInitial plan.\n",
            self.refined("yes"),
            self.refined("yes"),
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Please clarify the data edge case.", 0.1),
            claude_reviewer.ReviewResult("The revised plan is adequate.", 0.2),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="two-reviews",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertEqual(state["stage"], "implementation")
        self.assertEqual(len(state["plan_reviews"]), 2)
        self.assertEqual(len(claude.prompts), 2)
        self.assertEqual(len(codex.commands), 3)

    def test_user_revision_uses_remaining_plan_review_and_still_waits_for_approval(self):
        codex = FakeCodex(
            "# Proposal\nInitial plan.\n", self.refined(),
            "# Revised proposal\nAdd explicit empty-input behavior.\n",
            "# Final proposal\nHandle the empty-input case.\n\nSecond plan review needed: no\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Clarify empty input.", 0.1),
            claude_reviewer.ReviewResult("The change addresses empty input.", 0.1),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="revision-flow",
            process_runner=codex, claude_runner=claude,
        )
        self.assertEqual(runner.start()["status"], "awaiting_approval")
        (runner.workflow_dir / "proposal.md").write_text(
            "# User-edited proposal\nCall out empty input.\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "proposal file changed"):
            orchestrator.Orchestrator(
                self.target, workflow_id="revision-flow", process_runner=codex, claude_runner=claude,
            ).approve()
        revised = runner.revise("Document the empty-input behavior.")
        self.assertEqual(revised["status"], "awaiting_approval")
        self.assertEqual(len(revised["plan_reviews"]), 2)
        self.assertEqual(len(claude.prompts), 2)
        self.assertFalse(any("workspace-write" in command for command in codex.commands))
        self.assertIn("User-edited proposal", codex.prompts[2])
        self.assertIn("empty-input", (runner.workflow_dir / "proposal.md").read_text(encoding="utf-8"))

    def test_user_can_reject_without_implementation(self):
        codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Looks good.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="reject-flow",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        rejected = runner.reject("Out of scope.")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["rejection_reason"], "Out of scope.")
        self.assertFalse(any("workspace-write" in command for command in codex.commands))

    def test_code_findings_trigger_one_correction_without_a_review_loop(self):
        codex = FakeCodex(
            "# Proposal\nPlan.\n", self.refined(),
            "Implemented the feature.\nChecks passed.\n",
            "Handled the empty-input finding and reran the unit tests; they passed.\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Plan looks good.", 0.1),
            claude_reviewer.ReviewResult(
                "Decision: CHANGES_REQUESTED\nFinding: Handle empty input in the new parser.", 0.2,
            ),
        )
        (self.target / "src").mkdir()
        codex.on_call = lambda index, _command, _kwargs: (
            (self.target / "src" / "parser.py").write_text("def parse(value): return value\n", encoding="utf-8")
            if index == 2 else None
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="finding-flow",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        state = orchestrator.Orchestrator(
            self.target, workflow_id="finding-flow", process_runner=codex, claude_runner=claude,
        ).approve()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(len(claude.prompts), 2)
        self.assertEqual(len(codex.commands), 4)
        self.assertIn("Handle empty input", codex.prompts[-1])
        self.assertIn("reran the unit tests", state["correction_summary"])

    def test_failed_review_resume_retries_only_the_failed_stage(self):
        first_codex = FakeCodex("# Proposal\nInitial plan.\n")
        failed_claude = FakeClaude(claude_reviewer.ReviewError("Aily unavailable", cost_usd=0))
        first = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="resume-flow",
            process_runner=first_codex, claude_runner=failed_claude,
        )
        with self.assertRaises(orchestrator.WorkflowError):
            first.start()
        self.assertEqual(first.state["stage"], "plan_review_1")
        self.assertEqual(first.state["status"], "failed")

        retry_codex = FakeCodex(self.refined())
        retry_claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="resume-flow", process_runner=retry_codex, claude_runner=retry_claude,
        ).resume()
        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertEqual(len(retry_codex.commands), 1)
        self.assertEqual(retry_codex.commands[0][retry_codex.commands[0].index("--sandbox") + 1], "read-only")

    def test_spend_unknown_stops_later_paid_reviews(self):
        codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", None))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="unknown-spend",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertTrue(state["budget_unknown"])
        self.assertEqual(state["spent_usd"], 0)
        self.assertEqual(len(claude.prompts), 1)

    def test_exhausted_budget_prevents_a_later_paid_review(self):
        codex = FakeCodex(
            "# Proposal\nInitial plan.\n", self.refined(),
            "Implementation complete.\n", "No corrections needed.\n",
        )
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.5))
        (self.target / "src").mkdir()
        codex.on_call = lambda index, _command, _kwargs: (
            (self.target / "src" / "feature.py").write_text("def answer(): return 1\n", encoding="utf-8")
            if index == 2 else None
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=0.5, workflow_id="budget-flow",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        with self.assertRaisesRegex(orchestrator.WorkflowError, "budget is exhausted"):
            orchestrator.Orchestrator(
                self.target, workflow_id="budget-flow", process_runner=codex, claude_runner=claude,
            ).approve()
        state = json.loads((runner.workflow_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], "code_review")
        self.assertEqual(state["spent_usd"], 0.5)
        self.assertEqual(len(claude.prompts), 1)

    def test_review_packet_excludes_sensitive_and_preexisting_paths_and_redacts_tokens(self):
        (self.target / "src").mkdir()
        (self.target / "src" / "app.py").write_text('TOKEN = "ghp_123456789012345678901234"\n', encoding="utf-8")
        (self.target / ".env").write_text("PASSWORD=do-not-send-this-value\n", encoding="utf-8")
        git(self.target, "add", "src/app.py", ".env")
        git(self.target, "commit", "-q", "-m", "files")
        baseline = git(self.target, "rev-parse", "HEAD")
        (self.target / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        (self.target / ".env").write_text("PASSWORD=changed-secret-value\n", encoding="utf-8")
        outside = self.root / "outside-secret.txt"
        outside.write_text("OUTSIDE_REPOSITORY_SECRET", encoding="utf-8")
        (self.target / "src" / "external.txt").symlink_to(outside)
        state = {"baseline_commit": baseline, "preexisting_paths": ["src/app.py"]}
        packet = orchestrator._review_diff(state, self.target)
        self.assertIn("No reviewable changes", packet)
        self.assertNotIn("changed-secret-value", packet)

        state["preexisting_paths"] = []
        packet = orchestrator._review_diff(state, self.target)
        self.assertIn("[REDACTED]", packet)
        self.assertNotIn("ghp_123456789012345678901234", packet)
        self.assertNotIn("changed-secret-value", packet)
        self.assertNotIn("OUTSIDE_REPOSITORY_SECRET", packet)

    def test_aily_adapter_uses_budget_cap_and_isolated_review_directory(self):
        observed = {}

        def process_runner(command, **kwargs):
            observed["command"] = command
            observed["cwd"] = kwargs["cwd"]
            envelope = {"type": "result", "subtype": "success", "is_error": False,
                        "result": "Decision: APPROVED", "total_cost_usd": 0.25}
            return subprocess.CompletedProcess(command, 0, json.dumps(envelope).encode(), b"")

        result = claude_reviewer.review("review this sanitized patch", budget_usd=0.5,
                                       process_runner=process_runner)
        self.assertEqual(result.text, "Decision: APPROVED")
        self.assertEqual(result.cost_usd, 0.25)
        self.assertIn("--max-budget-usd", observed["command"])
        self.assertEqual(observed["command"][observed["command"].index("--max-budget-usd") + 1], "0.50")
        self.assertIn("--restricted", observed["command"])
        self.assertIn("--strict-mcp-config", observed["command"])
        self.assertNotEqual(Path(observed["cwd"]), self.target)


if __name__ == "__main__":
    unittest.main()

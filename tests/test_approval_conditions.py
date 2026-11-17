from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr
from io import StringIO

from test_workflow import FakeCodex, FakeClaude, make_target, WorkflowTests, orchestrator, claude_reviewer


class ApprovalConditionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="approval-conditions-")
        self.root = Path(self.temp.name)
        self.target = make_target(self.root / "target")

    def tearDown(self):
        self.temp.cleanup()

    def test_conditions_are_persisted_and_added_to_implementation_prompt(self):
        codex = FakeCodex(
            "# Proposal\nPlan.\n",
            WorkflowTests.refined(),
            "Implementation complete.\n",
            "No corrections needed.\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=lambda *_args: claude_reviewer.ReviewResult(
                "Decision: APPROVED\nNo acceptance-blocking findings.", 0.05,
            ),
        )

        def implement(index, _command, _kwargs):
            if index == 2:
                (self.target / "feature.py").write_text("value = 42\n", encoding="utf-8")

        codex.on_call = implement
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0,
            workflow_id="approval-conditions-persist",
            process_runner=codex, claude_runner=claude,
        )
        self.assertEqual(runner.start()["status"], "awaiting_approval")

        conditions = "Keep the public API backward compatible.\nDo not add dependencies."
        approved = runner.approve(conditions=conditions)

        self.assertEqual(approved["status"], "complete")
        self.assertEqual(approved["approval_conditions"], conditions)
        self.assertEqual(
            approved["implementation_approved_at"]["conditions_sha256"],
            __import__("hashlib").sha256(conditions.encode("utf-8")).hexdigest(),
        )
        saved = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["approval_conditions"], conditions)
        implementation_prompt = codex.prompts[2]
        self.assertIn("Human approval conditions (additional implementation constraints", implementation_prompt)
        self.assertIn("Keep the public API backward compatible.", implementation_prompt)
        self.assertIn("Do not add dependencies.", implementation_prompt)

    def test_conditions_survive_resume_after_interrupted_implementation(self):
        codex = FakeCodex("# Proposal\nPlan.\n", WorkflowTests.refined(), "partial implementation")
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=lambda *_args: claude_reviewer.ReviewResult(
                "Decision: APPROVED\\nNo acceptance-blocking findings.", 0.05,
            ),
        )

        def interrupt(index, _command, _kwargs):
            if index == 2:
                raise KeyboardInterrupt()

        codex.on_call = interrupt
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0,
            workflow_id="approval-conditions-resume",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        conditions = "Preserve existing database migrations."
        with self.assertRaises(KeyboardInterrupt):
            runner.approve(conditions=conditions)

        saved = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["approval_conditions"], conditions)

        retry_codex = FakeCodex("Implementation complete.", "No corrections needed.")
        retry_codex.on_call = lambda index, *_args: (
            (self.target / "feature.py").write_text("value = 1\n", encoding="utf-8")
            if index == 0 else None
        )
        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="approval-conditions-resume",
            process_runner=retry_codex, claude_runner=claude,
        ).resume(recovery_action="retry", confirm_process_inactive=True, confirm_target_reviewed=True)
        self.assertEqual(resumed["approval_conditions"], conditions)
        self.assertIn("Preserve existing database migrations.", retry_codex.prompts[0])

    def test_approval_conditions_over_limit_are_rejected_before_workflow_changes(self):
        codex = FakeCodex("# Proposal\nPlan.\n", WorkflowTests.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0,
            workflow_id="approval-conditions-limit",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        before = runner.state_path.read_bytes()

        with self.assertRaisesRegex(orchestrator.WorkflowError, "12,000 characters"):
            runner.approve(conditions="x" * 12_001)

        self.assertEqual(runner.state_path.read_bytes(), before)
        self.assertEqual(len(codex.commands), 2)

    def test_cli_reads_conditions_file_and_reports_missing_or_invalid_utf8(self):
        conditions_file = self.root / "conditions.md"
        conditions_file.write_text("Preserve compatibility.\n", encoding="utf-8")
        state = {
            "workflow_id": "cli-approval-conditions", "target": str(self.target),
            "status": "complete", "authorization": {}, "plan_reviews": [],
        }
        with patch.object(orchestrator, "Orchestrator") as factory:
            factory.return_value.approve.return_value = state
            result = orchestrator.main([
                "approve", "--target", str(self.target), "--workflow-id", "cli-approval-conditions",
                "--conditions-file", str(conditions_file),
            ])
        self.assertEqual(result, 0)
        factory.return_value.approve.assert_called_once_with(conditions="Preserve compatibility.\n")

        for filename, payload, expected in (
            ("missing.md", None, "Could not read approval conditions file"),
            ("invalid.md", bytes([0xFF]), "must be valid UTF-8"),
        ):
            path = self.root / filename
            if payload is not None:
                path.write_bytes(payload)
            errors = StringIO()
            with patch.object(orchestrator, "Orchestrator"), redirect_stderr(errors):
                result = orchestrator.main([
                    "approve", "--target", str(self.target), "--workflow-id", "cli-approval-conditions",
                    "--conditions-file", str(path),
                ])
            self.assertEqual(result, 2)
            self.assertIn(expected, errors.getvalue())


if __name__ == "__main__":
    unittest.main()

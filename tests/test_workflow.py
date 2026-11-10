from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


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
    def refined(
        second_review: str = "no",
        dispositions: str = "- No actionable findings.",
    ) -> str:
        return (
            "# Refined proposal\n\n## Scope\nAdd the requested behavior.\n\n"
            f"## Claude finding dispositions\n{dispositions}\n\nSecond plan review needed: {second_review}\n"
        )

    def test_missing_budget_uses_two_dollars_and_persists_it(self):
        codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", workflow_id="default-budget",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        authorization = state["authorization"]["claude_aily"]
        self.assertEqual(authorization["budget_usd"], 2.0)
        persisted = json.loads((runner.workflow_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["authorization"]["claude_aily"]["budget_usd"], 2.0)
        self.assertEqual(claude.budgets, [2.0])
        self.assertIsNone(state["codex_model"])
        self.assertTrue(all("--model" not in command for command in codex.commands))

    def test_cli_reports_default_budget_without_requesting_it(self):
        fake_state = {
            "workflow_id": "cli-default-budget", "target": str(self.target),
            "status": "awaiting_approval", "stage": "implementation", "error": None,
            "authorization": {
                "authorization_id": "auth-test",
                "claude_aily": {"budget_usd": 2.0, "spent_usd": 0.0},
            },
        }
        output = StringIO()
        with patch.object(orchestrator, "Orchestrator") as factory:
            factory.return_value.budget_usd = 2.0
            factory.return_value.start.return_value = fake_state
            with redirect_stdout(output):
                result = orchestrator.main([
                    "start", "--target", str(self.target), "--task", "Add a feature",
                ])
        self.assertEqual(result, 0)
        self.assertEqual(factory.call_args.kwargs["budget_usd"], 2.0)
        self.assertIn("Effective Claude/Aily budget: $2.00", output.getvalue())
        self.assertIn('"effective_budget_usd": 2.0', output.getvalue())
        self.assertIsNone(factory.call_args.kwargs["model"])

    def test_cli_budget_override_and_optional_model(self):
        fake_state = {
            "workflow_id": "cli-override", "target": str(self.target),
            "status": "awaiting_approval", "stage": "implementation", "error": None,
            "authorization": {
                "authorization_id": "auth-test",
                "claude_aily": {"budget_usd": 0.75, "spent_usd": 0.0},
            },
        }
        output = StringIO()
        with patch.object(orchestrator, "Orchestrator") as factory:
            factory.return_value.budget_usd = 0.75
            factory.return_value.start.return_value = fake_state
            with redirect_stdout(output):
                result = orchestrator.main([
                    "start", "--target", str(self.target), "--task", "Add a feature",
                    "--budget-usd", "0.75", "--model", "codex-model-test",
                ])
        self.assertEqual(result, 0)
        self.assertEqual(factory.call_args.kwargs["budget_usd"], 0.75)
        self.assertEqual(factory.call_args.kwargs["model"], "codex-model-test")
        self.assertIn("Effective Claude/Aily budget: $0.75", output.getvalue())

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
            model="codex-model-test", process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertTrue((runner.workflow_dir / "proposal.md").is_file())
        self.assertEqual(len(codex.commands), 2)
        self.assertFalse(any("workspace-write" in command for command in codex.commands))
        self.assertIsNone(state["implementation_approved_at"])
        authorization_id = state["authorization"]["authorization_id"]
        authorization = state["authorization"]
        self.assertEqual(authorization["target"], str(self.target.resolve()))
        self.assertIn("run_development_commands_builds_tests_linters_and_static_analysis",
                      authorization["workspace_operations"])
        self.assertEqual(authorization["claude_aily"]["permitted_data"], orchestrator.CLAUDE_DATA_SCOPE)
        self.assertEqual(authorization["claude_aily"]["budget_usd"], 1.0)

        approved = orchestrator.Orchestrator(
            self.target, workflow_id="approval-flow", process_runner=codex, claude_runner=claude,
        ).approve()
        self.assertEqual(approved["status"], "complete")
        self.assertEqual(len(codex.commands), 3)
        self.assertEqual([command[command.index("--sandbox") + 1] for command in codex.commands],
                         ["read-only", "read-only", "workspace-write"])
        self.assertTrue(all(prompt.startswith("Persistent workflow authorization is already active")
                            for prompt in codex.prompts))
        self.assertEqual(approved["authorization"]["authorization_id"], authorization_id)
        self.assertEqual(approved["codex_model"], "codex-model-test")
        self.assertTrue(all(
            command[command.index("--model") + 1] == "codex-model-test" for command in codex.commands
        ))
        self.assertEqual(approved["authorization"]["claude_aily"]["spent_usd"], 0.3)
        self.assertEqual(claude.budgets, [1.0, 0.88])
        self.assertIn("return 42", claude.prompts[1])
        self.assertIn("commit, push", codex.prompts[2])
        self.assertIn("commit", approved["authorization"]["restricted_operations"])
        self.assertTrue((runner.workflow_dir / "delivery.md").is_file())
        self.assertIn("python -m unittest", (runner.workflow_dir / "delivery.md").read_text())

    def test_plan_review_is_limited_to_two_iterations(self):
        codex = FakeCodex(
            "# Proposal\nInitial plan.\n",
            self.refined("yes", "- Finding: clarify the data edge case | Decision: incorporated | "
                                  "Reason: cover the data boundary."),
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
        self.assertIn("Please clarify the data edge case.", claude.prompts[1])
        self.assertIn("Decision: incorporated", claude.prompts[1])
        self.assertIn("Plan review iteration 2", claude.prompts[1])
        self.assertIn("proposal version 2", claude.prompts[1])
        self.assertEqual(state["claude_contexts"][1]["proposal_version"], 2)

    def test_user_revision_uses_remaining_plan_review_and_still_waits_for_approval(self):
        codex = FakeCodex(
            "# Proposal\nInitial plan.\n",
            self.refined(dispositions="- Finding: clarify empty input | Decision: incorporated | "
                                      "Reason: define zero-item behavior."),
            "# Revised proposal\nAdd explicit empty-input behavior.\n",
            "# Final proposal\nHandle the empty-input case.\n\n"
            "## Claude finding dispositions\n- No actionable findings.\n\nSecond plan review needed: no\n",
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
        self.assertIn("uncertain outcomes", codex.prompts[2])
        self.assertIn("external dependencies", codex.prompts[2])
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
        self.assertTrue(all(prompt.startswith("Persistent workflow authorization is already active")
                            for prompt in codex.prompts))
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
        authorization_id = first.state["authorization"]["authorization_id"]
        self.assertEqual(first.state["stage"], "plan_review_1")
        self.assertEqual(first.state["status"], "failed")

        retry_codex = FakeCodex(self.refined())
        retry_claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="resume-flow", process_runner=retry_codex, claude_runner=retry_claude,
        ).resume()
        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertEqual(resumed["authorization"]["authorization_id"], authorization_id)
        self.assertEqual(len(retry_codex.commands), 1)
        self.assertEqual(retry_codex.commands[0][retry_codex.commands[0].index("--sandbox") + 1], "read-only")

    def test_resume_rejects_a_changed_authorization_budget(self):
        codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=0.5, workflow_id="tampered-auth",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        state_path = runner.workflow_dir / "state.json"
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        saved["authorization"]["claude_aily"]["budget_usd"] = 10.0
        state_path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "authorization changed"):
            orchestrator.Orchestrator(self.target, workflow_id="tampered-auth").status()

    def test_resume_rejects_workspace_or_external_scope_expansion(self):
        for workflow_id, mutation, expected_error in (
            ("expanded-workspace", lambda auth: auth["workspace_operations"].append("access_outside_target"),
             "workspace authorization"),
            ("expanded-aily", lambda auth: auth["claude_aily"]["permitted_data"].append("unrelated_files"),
             "Claude/Aily scope"),
        ):
            with self.subTest(workflow_id=workflow_id):
                codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
                claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
                runner = orchestrator.Orchestrator(
                    self.target, task="Add a feature", budget_usd=0.5, workflow_id=workflow_id,
                    process_runner=codex, claude_runner=claude,
                )
                runner.start()
                state_path = runner.workflow_dir / "state.json"
                saved = json.loads(state_path.read_text(encoding="utf-8"))
                mutation(saved["authorization"])
                state_path.write_text(json.dumps(saved), encoding="utf-8")
                with self.assertRaisesRegex(orchestrator.WorkflowError, expected_error):
                    orchestrator.Orchestrator(self.target, workflow_id=workflow_id).resume()

    def test_spend_unknown_stops_later_paid_reviews(self):
        codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", None))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="unknown-spend",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertTrue(state["authorization"]["claude_aily"]["budget_unknown"])
        self.assertEqual(state["authorization"]["claude_aily"]["spent_usd"], 0)
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
        self.assertEqual(state["authorization"]["claude_aily"]["spent_usd"], 0.5)
        self.assertEqual(len(claude.prompts), 1)

    def test_codex_command_uses_supported_global_approval_option(self):
        command = orchestrator.build_codex_command(
            "codex", self.target, "workspace-write", self.root / "last-message.md",
        )
        self.assertEqual(command[0:4], ["codex", "--ask-for-approval", "never", "exec"])
        self.assertLess(command.index("--ask-for-approval"), command.index("exec"))
        self.assertNotIn("--ask-for-approval", command[4:])
        self.assertIn("--sandbox", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--model", command)

        configured = orchestrator.build_codex_command(
            "codex", self.target, "workspace-write", self.root / "last-message.md", "codex-model-test",
        )
        model_index = configured.index("--model")
        self.assertEqual(configured[model_index + 1], "codex-model-test")

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

    def test_long_assignment_and_complete_proposal_reach_claude_with_audit_hashes(self):
        task = "# Full assignment\n" + ("requirement detail " * 120) + "\nASSIGNMENT_END_MARKER"
        proposal = "# Complete proposal\n" + ("architecture detail " * 120) + "\nPROPOSAL_END_MARKER"
        codex = FakeCodex(proposal, self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Decision: CHANGES_REQUESTED\nCheck the edge cases.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task=task, budget_usd=1.0, workflow_id="complete-review-context",
            process_runner=codex, claude_runner=claude,
        )

        state = runner.start()
        sent = claude.prompts[0]
        self.assertGreater(len(sent), 1200)
        self.assertIn("ASSIGNMENT_END_MARKER", sent)
        self.assertIn("PROPOSAL_END_MARKER", sent)
        self.assertEqual(state["plan_reviews"][0]["valid"], True)
        manifest = state["plan_reviews"][0]["context_manifest"]
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["characters"], len(sent))
        self.assertEqual(manifest["prompt_sha256"], hashlib.sha256(sent.encode("utf-8")).hexdigest())
        section_hashes = {item["name"]: item["sha256"] for item in manifest["sections"]}
        self.assertEqual(section_hashes["Assignment"], hashlib.sha256(task.encode("utf-8")).hexdigest())
        current_proposal = orchestrator.clean_markdown(proposal).strip()
        self.assertEqual(section_hashes["Current proposal"], hashlib.sha256(current_proposal.encode("utf-8")).hexdigest())
        self.assertIn("must implement, should implement", codex.prompts[0])
        self.assertIn("These are analysis prompts, not automatic implementation requirements", codex.prompts[0])
        self.assertIn("State important unknowns", codex.prompts[0])
        self.assertIn("observable outcomes and acceptance criteria", codex.prompts[0])
        self.assertIn("future production considerations", claude.prompts[0])

    def test_missing_or_oversized_review_context_prevents_a_paid_call(self):
        codex = FakeCodex("# Proposal\nComplete.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="context-guards",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        header, sections, metadata = runner._plan_review_prompt(2)
        incomplete = dict(sections)
        incomplete.pop("Current proposal")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "missing mandatory sections"):
            runner._call_claude(header, "plan_review_2", incomplete, metadata=metadata)
        self.assertEqual(len(claude.prompts), 1)

        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "80"}):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "no paid call was made"):
                runner._call_claude(header, "plan_review_2", sections, metadata=metadata)
        self.assertEqual(len(claude.prompts), 1)
        self.assertEqual(len(runner.state["claude_contexts"]), 1)

    def test_revise_invalidates_unverified_legacy_review_and_reuses_iteration(self):
        codex = FakeCodex(
            "# Proposal\nCurrent plan.\n",
            self.refined(),
            "# Corrected proposal\nThe prior review is invalid.\n",
            self.refined(dispositions="- No actionable findings."),
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("The assignment and proposal are missing.", 0.1),
            claude_reviewer.ReviewResult("Decision: APPROVED\nThe complete proposal meets the assignment.", 0.2),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Build a REST API", budget_usd=1.0, workflow_id="invalidate-legacy-review",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        state_path = runner.state_path
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        saved["plan_reviews"][0].pop("context_manifest")
        saved["plan_reviews"][0].pop("valid")
        state_path.write_text(json.dumps(saved), encoding="utf-8")

        revised = runner.revise(
            "Preserve the existing proposal. Mark the prior review invalid because its 1,200-character context "
            "was incomplete; do not count its findings. Apply the proportional-design rule and the relevant "
            "api-engineer skill, then request a complete review."
        )
        self.assertEqual(revised["status"], "awaiting_approval")
        self.assertFalse(revised["plan_reviews"][0]["valid"])
        self.assertIn("1,200 characters", revised["plan_reviews"][0]["invalidated_reason"])
        self.assertTrue(revised["plan_reviews"][1]["valid"])
        self.assertEqual(revised["plan_reviews"][1]["iteration"], 1)
        self.assertEqual(revised["plan_reviews"][1]["proposal_version"], 3)
        self.assertEqual(len([r for r in revised["plan_reviews"] if r.get("valid")]), 1)
        self.assertEqual(revised["authorization"]["claude_aily"]["spent_usd"], 0.3)
        self.assertIn("Invalidated attempt", claude.prompts[1])
        self.assertIn("Does the proposal satisfy every explicit assignment requirement?", claude.prompts[1])
        self.assertIn("Is each architectural component and abstraction justified", claude.prompts[1])
        self.assertIn("Mark the prior review invalid", codex.prompts[2])

    def test_codex_receives_target_instructions_and_applicable_skill_contents(self):
        codex_home = self.root / "codex-home"
        skill = codex_home / ".tmp" / "marketplaces" / "postman" / "skills" / "api-engineer" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: api-engineer\ndescription: Use when designing or implementing an API.\n---\n"
            "Establish the API contract first and match tests to behavior at risk.\n",
            encoding="utf-8",
        )
        target_skill = self.target / ".agents" / "skills" / "project-api" / "SKILL.md"
        target_skill.parent.mkdir(parents=True)
        target_skill.write_text(
            "---\nname: project-api\ndescription: Local API conventions.\n---\nUse snake_case field names.\n",
            encoding="utf-8",
        )
        (self.target / "AGENTS.md").write_text("Keep endpoints versioned.\n", encoding="utf-8")
        codex = FakeCodex("# Proposal\nUse the assignment contract.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            runner = orchestrator.Orchestrator(
                self.target, task="Build a REST API", budget_usd=1.0, workflow_id="project-skills",
                process_runner=codex, claude_runner=claude,
            )
            state = runner.start()
        self.assertIn("Keep endpoints versioned.", codex.prompts[0])
        self.assertIn("Establish the API contract first", codex.prompts[0])
        self.assertIn("Use snake_case field names.", codex.prompts[0])
        self.assertIn("api-engineer", claude.prompts[0])
        self.assertEqual({item["name"] for item in state["project_guidance"]["skills"]},
                         {"api-engineer", "project-api"})

    def test_no_applicable_project_skill_is_recorded_without_inventing_one(self):
        codex_home = self.root / "empty-codex-home"
        codex = FakeCodex("# Proposal\nImplement the requested behavior.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            runner = orchestrator.Orchestrator(
                self.target, task="Add a arithmetic helper", budget_usd=1.0, workflow_id="no-skills",
                process_runner=codex, claude_runner=claude,
            )
            state = runner.start()
        self.assertEqual(state["project_guidance"]["skills"], [])
        self.assertIn("Applicable skills: none found", codex.prompts[0])
        self.assertIn("Relevant skills: none found", claude.prompts[0])

    def test_refinement_requires_explicit_disposition_for_findings(self):
        incomplete = (
            "# Proposal\n\n## Claude finding dispositions\n"
            "- Finding: missing empty-input behavior.\n\nSecond plan review needed: no\n"
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "decision and reason"):
            orchestrator.Orchestrator._refined_proposal(incomplete)

    def test_complete_diff_is_not_truncated_and_oversize_fails_explicitly(self):
        content = "# complete diff marker\n" + ("line of relevant code\n" * 120)
        (self.target / "large.py").write_text(content, encoding="utf-8")
        state = {"baseline_commit": git(self.target, "rev-parse", "HEAD"), "preexisting_paths": []}
        diff = orchestrator._review_diff(state, self.target)
        self.assertIn(content.rstrip(), diff)
        self.assertGreater(len(diff), 1200)
        self.assertNotIn("truncated", diff.lower())
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "100"}):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "no paid call was made"):
                orchestrator._review_diff(state, self.target)

    def test_redaction_preserves_long_text_and_adapter_refuses_oversize_before_call(self):
        long_context = "context " * 300
        self.assertEqual(claude_reviewer.redact(long_context), long_context)
        called = False
        observed = {}

        def process_runner(command, **kwargs):
            nonlocal called
            called = True
            observed["prompt"] = command[command.index("-p") + 1]
            envelope = {"type": "result", "subtype": "success", "is_error": False,
                        "result": "Decision: APPROVED", "total_cost_usd": 0.1}
            return subprocess.CompletedProcess(command, 0, json.dumps(envelope).encode(), b"")

        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "5000"}):
            claude_reviewer.review(long_context, budget_usd=0.5, process_runner=process_runner)
        self.assertEqual(observed["prompt"], long_context)

        called = False
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "100"}):
            with self.assertRaisesRegex(claude_reviewer.ReviewError, "no paid call was made"):
                claude_reviewer.review(long_context, budget_usd=0.5, process_runner=process_runner)
        self.assertFalse(called)

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

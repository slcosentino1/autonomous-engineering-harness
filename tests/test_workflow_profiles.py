from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import test_workflow as test_helpers
from workflow.assessment import assess_task
from workflow.configuration import resolve_workflow_selection
from workflow.definitions import BUILT_IN_WORKFLOWS


class ProfileCodex(test_helpers.FakeCodex):
    """Fake Codex that leaves observable source changes and real parsed test receipts."""

    def __init__(self, *responses: str, fail_test_calls: int = 0):
        super().__init__(*responses)
        self.fail_test_calls = fail_test_calls

    def __call__(self, command, **kwargs):
        result = super().__call__(command, **kwargs)
        prompt = self.prompts[-1]
        target = Path(command[command.index("--cd") + 1])
        if ("Implement the assigned task in this repository." in prompt
                or "Implement the approved proposal in this repository." in prompt):
            source = target / "src" / "workflow_profile_change.py"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("def workflow_profile_value():\n    return 'implemented'\n", encoding="utf-8")
        if ("Run the repository's relevant automated tests" in prompt
                or "Run relevant tests after the corrections" in prompt):
            if self.fail_test_calls:
                self.fail_test_calls -= 1
                return subprocess.CompletedProcess(command, 0, b"No test command was available.\n", b"")
            event = {
                "type": "item.completed",
                "item": {
                    "id": f"profile-test-{len(self.commands)}",
                    "type": "command_execution",
                    "command": "python3 -m unittest discover -s tests",
                    "exit_code": 0,
                    "aggregated_output": "Ran 1 test in 0.01s\n\nOK\n",
                },
            }
            return subprocess.CompletedProcess(
                command, 0, json.dumps(event).encode("utf-8"), b"",
            )
        return result


class WorkflowProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workflow-profile-")
        self.root = Path(self.temporary.name)
        self.target = test_helpers.make_target(self.root / "target")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def good_review():
        return test_helpers.claude_reviewer.ReviewResult(
            "Decision: APPROVED\nNo acceptance-blocking findings.", 0.05,
        )

    def profile_runner(self, profile, task, workflow_id, *, responses=None, claude_results=None,
                       selected_stages=None, codex_type=ProfileCodex):
        codex = codex_type(*(responses or ["Stage completed.", "Tests completed."]))
        claude = test_helpers.FakeClaude(*(claude_results or [self.good_review()] * 3))
        runner = test_helpers.orchestrator.Orchestrator(
            self.target,
            task=task,
            budget_usd=1.0,
            workflow_id=workflow_id,
            workflow_name=profile,
            selected_stages=selected_stages,
            process_runner=codex,
            claude_runner=claude,
        )
        return runner, codex, claude

    def test_full_development_keeps_legacy_definition_and_initial_assessment(self):
        codex = test_helpers.FakeCodex("# Proposal\nPlan.\n", test_helpers.WorkflowTests.refined())
        claude = test_helpers.FakeClaude(self.good_review())
        runner = test_helpers.orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="profile-snapshot",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertEqual(state["workflow_name"], "full-development")
        self.assertEqual(state["workflow_definition"]["schema_version"], 2)
        self.assertEqual(
            {stage["stage_id"] for stage in state["workflow_definition"]["stages"]},
            {stage.stage_id for stage in BUILT_IN_WORKFLOWS["full-development"].stages},
        )
        self.assertEqual(state["assessment"]["task_type"], "feature")
        self.assertEqual(state["assessment_history"][0]["phase"], "initial")
        self.assertTrue(state["policy_decisions"]["proposal"]["required"])
        self.assertTrue(state["policy_decisions"]["approval"]["required"])
        self.assertEqual(state["workflow_execution"]["waiting_stage"], "implementation")

    def test_builtin_workflow_profiles_have_the_promised_stage_sequences(self):
        expected = {
            "bugfix": ("assessment", "investigation", "implementation", "tests", "review",
                       "task_correction", "verification", "delivery"),
            "quick-fix": ("assessment", "implementation", "tests", "verification", "delivery"),
            "implementation-only": ("assessment", "implementation", "tests", "verification"),
            "review-only": ("assessment", "review", "report"),
        }
        for name, stage_ids in expected.items():
            with self.subTest(profile=name):
                self.assertEqual(
                    tuple(stage.stage_id for stage in BUILT_IN_WORKFLOWS[name].stages), stage_ids,
                )

    def test_full_development_executes_proposal_approval_review_and_delivery(self):
        codex = ProfileCodex(
            "# Proposal\n\n## Scope\nImplement the requested feature.\n",
            test_helpers.WorkflowTests.refined(),
            "Implemented the approved feature.",
        )
        claude = test_helpers.FakeClaude(self.good_review(), self.good_review())
        runner = test_helpers.orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="full-profile-e2e",
            process_runner=codex, claude_runner=claude,
        )
        waiting = runner.run()
        self.assertEqual(waiting["status"], "awaiting_approval")
        self.assertEqual(waiting["workflow_execution"]["waiting_stage"], "implementation")
        completed = runner.approve()
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["workflow_execution"]["status"], "complete")
        self.assertTrue(runner.delivery_path.is_file())

    def test_quick_fix_executes_through_production_engine_and_writes_delivery(self):
        runner, codex, _claude = self.profile_runner(
            "quick-fix", "Fix typo in documentation", "quick-profile",
            responses=["Implemented the documentation fix.", "All relevant tests passed."],
        )
        state = runner.run()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["workflow_execution"]["status"], "complete")
        self.assertEqual(
            [operation["stage"] for operation in state["operation_history"] if operation.get("operation_type") == "codex"],
            ["implementation", "tests"],
        )
        self.assertEqual(state["workflow_execution"]["stages"]["tests"]["output"]["passed"], True)
        self.assertEqual(state["integrity"]["verification_result"]["status"], "verified")
        self.assertTrue(runner.delivery_path.is_file())
        self.assertIn("src/workflow_profile_change.py", state["review_included_files"])

    def test_bugfix_investigates_reviews_and_conditionally_corrects(self):
        runner, codex, claude = self.profile_runner(
            "bugfix", "Fix a small isolated defect", "bugfix-profile",
            responses=["Diagnosis found the faulty guard.", "Changed the guard and added a test.",
                       "Tests passed."],
            claude_results=[self.good_review(), self.good_review()],
        )
        state = runner.run()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["workflow_execution"]["stages"]["investigation"]["status"], "completed")
        self.assertEqual(state["workflow_execution"]["stages"]["task_correction"]["status"], "not_applicable")
        self.assertEqual(state["workflow_execution"]["stages"]["verification"]["output"]["verified"], True)
        self.assertEqual(len(claude.prompts), 1)
        self.assertEqual([item["stage"] for item in state["operation_history"] if item.get("operation_type") == "codex"],
                         ["investigation", "implementation", "tests"])

    def test_implementation_only_omits_proposal_review_and_delivery(self):
        runner, codex, claude = self.profile_runner(
            "implementation-only", "Fix typo in documentation", "implementation-only-profile",
            responses=["Implemented the requested correction.", "Tests passed."],
            claude_results=[self.good_review()],
        )
        state = runner.run()
        self.assertEqual(state["status"], "complete")
        self.assertFalse(state["traceability_required"])
        self.assertFalse(state["policy_decisions"]["proposal"]["required"])
        self.assertFalse(state["policy_decisions"]["approval"]["required"])
        self.assertNotIn("delivery", state["workflow_execution"]["stages"])
        self.assertEqual(len(codex.commands), 2)
        self.assertEqual(len(claude.prompts), 1)
        self.assertFalse(runner.delivery_path.exists())

    def test_review_only_reviews_existing_changes_and_never_modifies_source(self):
        source = self.target / "README.md"
        source.write_text("pre-existing change to review\n", encoding="utf-8")
        before = source.read_bytes()
        claude = test_helpers.FakeClaude(self.good_review())
        codex = test_helpers.FakeCodex()
        runner = test_helpers.orchestrator.Orchestrator(
            self.target, task="Review this change", budget_usd=1.0,
            workflow_id="review-only-profile", workflow_name="review-only",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.run()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(codex.commands, [])
        self.assertTrue(any("README.md" in prompt for prompt in claude.prompts))
        self.assertEqual(state["workflow_execution"]["stages"]["report"]["status"], "completed")
        self.assertIn("Review report", runner.delivery_path.read_text(encoding="utf-8"))

    def test_custom_stage_subset_runs_and_preserves_dependency_order(self):
        selected = ("assessment", "implementation", "tests", "verification")
        runner, _codex, _claude = self.profile_runner(
            "quick-fix", "Fix typo in documentation", "custom-stages",
            responses=["Implemented.", "Tests passed."], selected_stages=selected,
            claude_results=[self.good_review()],
        )
        state = runner.run()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(tuple(stage["stage_id"] for stage in state["workflow_definition"]["stages"]), selected)
        self.assertNotIn("delivery", state["workflow_execution"]["stages"])

    def test_optional_stage_skip_executes_profile_with_dependency_semantics(self):
        codex = ProfileCodex("Implemented the isolated defect.", "Tests passed.")
        runner = test_helpers.orchestrator.Orchestrator(
            self.target,
            task="Fix a small isolated defect",
            budget_usd=1.0,
            workflow_id="skip-investigation",
            workflow_name="bugfix",
            skip_stages=("investigation",),
            process_runner=codex,
            claude_runner=test_helpers.FakeClaude(self.good_review()),
        )
        state = runner.run()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["workflow_execution"]["stages"]["investigation"]["status"], "skipped")
        self.assertEqual(
            [operation["stage"] for operation in state["operation_history"]
             if operation.get("operation_type") == "codex"],
            ["implementation", "tests"],
        )

    def test_failed_stage_resumes_same_profile_without_restarting_completed_work(self):
        class FailFirstTests(ProfileCodex):
            def __init__(self, *responses):
                super().__init__(*responses, fail_test_calls=1)

        runner, codex, claude = self.profile_runner(
            "quick-fix", "Fix typo in documentation", "resume-profile",
            responses=["Implemented.", "Tests passed."], codex_type=FailFirstTests,
            claude_results=[self.good_review()],
        )
        with self.assertRaises(test_helpers.orchestrator.WorkflowError):
            runner.run()
        failed = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(failed["stage"], "tests")
        self.assertEqual(failed["workflow_execution"]["stages"]["implementation"]["status"], "completed")
        replacement = ProfileCodex("Tests passed.")
        resumed = test_helpers.orchestrator.Orchestrator(
            self.target, workflow_id="resume-profile", process_runner=replacement,
            claude_runner=claude,
        ).resume()
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(resumed["workflow_name"], "quick-fix")
        self.assertEqual(resumed["workflow_execution"]["stages"]["implementation"]["attempts"][-1]["attempt"], 1)
        self.assertEqual(resumed["workflow_execution"]["stages"]["tests"]["attempts"][-1]["attempt"], 2)
        self.assertIn("Stage attempt: 2", replacement.prompts[0])

    def test_continue_from_reexecutes_selected_stage_and_only_its_dependents(self):
        runner, _codex, _claude = self.profile_runner(
            "quick-fix", "Fix typo in documentation", "continue-profile",
            responses=["Implemented.", "Tests passed.", "Implemented again.", "Tests passed again."],
            claude_results=[self.good_review()],
        )
        first = runner.run()
        self.assertEqual(first["status"], "complete")
        continued = test_helpers.orchestrator.Orchestrator(
            self.target, workflow_id="continue-profile",
            process_runner=ProfileCodex("Implemented again.", "Tests passed again."),
            claude_runner=test_helpers.FakeClaude(self.good_review()),
        ).resume(continue_from="tests")
        self.assertEqual(continued["status"], "complete")
        execution = continued["workflow_execution"]
        self.assertEqual(execution["stages"]["assessment"]["attempts"][-1]["attempt"], 1)
        self.assertEqual(execution["stages"]["implementation"]["attempts"][-1]["attempt"], 1)
        self.assertEqual(execution["stages"]["tests"]["attempts"][-1]["attempt"], 2)
        self.assertEqual(
            continued["workflow_execution"]["continuation_history"][-1]["invalidated_stages"],
            ["tests", "verification", "delivery"],
        )

    def test_assessment_recommends_workflow_but_explicit_selection_wins(self):
        assessment = assess_task("Fix typo in documentation")
        self.assertEqual(assessment.recommended_workflow, "quick-fix")
        runner, _codex, _claude = self.profile_runner(
            "implementation-only", "Fix typo in documentation", "explicit-selection",
            responses=["Implemented.", "Tests passed."], claude_results=[self.good_review()],
        )
        state = runner.run()
        self.assertEqual(state["workflow_name"], "implementation-only")
        self.assertEqual(state["assessment"]["recommended_workflow"], "quick-fix")

    def test_assessment_auto_selects_review_only_for_review_tasks(self):
        definition, _snapshot, assessment, policy = resolve_workflow_selection("auto", "Review this change")
        self.assertEqual(definition.name, "review-only")
        self.assertEqual(assessment.recommended_workflow, "review-only")
        self.assertFalse(policy.approval_required)

    def test_assessment_mandatory_gates_reject_a_short_profile_without_proposal(self):
        with self.assertRaisesRegex(ValueError, "requires proposal and planning gates"):
            resolve_workflow_selection("quick-fix", "Change authentication for a public API")

    def test_skip_and_stage_selection_validate_names_and_mandatory_gates(self):
        for options in (
            {"workflow_name": "unknown"},
            {"skip_stages": ("implementation",)},
            {"skip_stages": ("assessment",)},
            {"selected_stages": ("context", "implementation")},
            {"selected_stages": ("planning", None)},
        ):
            with self.subTest(options=options):
                runner = test_helpers.orchestrator.Orchestrator(
                    self.target, task="Fix a small defect", budget_usd=1.0,
                    workflow_id="invalid-selection-" + str(len(options)) + str(list(options)[0]),
                    process_runner=test_helpers.FakeCodex("must not run"),
                    **options,
                )
                with self.assertRaises(test_helpers.orchestrator.WorkflowError):
                    runner.start()
                self.assertFalse(runner.workflow_dir.exists())

    def test_forged_direct_selection_cannot_grant_an_unapproved_capability(self):
        definition, _snapshot, assessment, policy = resolve_workflow_selection("full-development", "Fix a typo")
        forged = replace(definition, stages=tuple(
            replace(stage, capabilities=(*stage.capabilities, "documentation.read"))
            if stage.stage_id == "implementation" else stage
            for stage in definition.stages
        ))
        runner = test_helpers.orchestrator.Orchestrator(
            self.target, task="Fix a typo", budget_usd=1.0, workflow_id="forged-selection",
            process_runner=test_helpers.FakeCodex("must not run"),
            workflow_selection=(forged, test_helpers.orchestrator._definition_snapshot(forged), assessment, policy),
        )
        with self.assertRaisesRegex(test_helpers.orchestrator.WorkflowError, "invalid or conflicts"):
            runner.start()
        self.assertFalse(runner.workflow_dir.exists())

    def test_saved_definition_cannot_gain_a_capability_with_recomputed_digest(self):
        codex = test_helpers.FakeCodex("# Proposal\nPlan.\n", test_helpers.WorkflowTests.refined())
        runner = test_helpers.orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="pinned-capabilities",
            process_runner=codex, claude_runner=test_helpers.FakeClaude(self.good_review()),
        )
        runner.start()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        definition = state["workflow_definition"]
        implementation = next(stage for stage in definition["stages"] if stage["stage_id"] == "implementation")
        implementation["capabilities"].append("documentation.read")
        unsigned = {key: value for key, value in definition.items() if key != "sha256"}
        definition["sha256"] = hashlib.sha256(json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()
        runner.state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        replacement = test_helpers.orchestrator.Orchestrator(self.target, workflow_id="pinned-capabilities")
        with self.assertRaisesRegex(test_helpers.orchestrator.WorkflowError, "trusted profile"):
            replacement._read_state()

    def test_cli_exposes_run_workflow_selection_and_clear_rejection(self):
        factory = Mock()
        factory.return_value.budget_usd = 1.0
        factory.return_value.max_code_review_calls = 1
        factory.return_value.run.return_value = {
            "workflow_id": "cli-profile", "workflow_name": "quick-fix", "target": str(self.target),
            "status": "complete", "authorization": {}, "plan_reviews": [],
        }
        with patch.object(test_helpers.orchestrator, "Orchestrator", factory):
            result = test_helpers.orchestrator.main([
                "run", "--target", str(self.target), "--task", "Fix typo in documentation",
                "--workflow", "quick-fix", "--stages", "assessment,implementation,tests,verification",
            ])
        self.assertEqual(result, 0)
        factory.return_value.run.assert_called_once()
        self.assertEqual(factory.call_args.kwargs["workflow_selection"][0].name, "quick-fix")
        self.assertEqual(factory.call_args.kwargs["workflow_name"], "quick-fix")

        factory.reset_mock()
        with patch.object(test_helpers.orchestrator, "Orchestrator", factory):
            result = test_helpers.orchestrator.main([
                "run", "--target", str(self.target), "--task", "Fix typo", "--workflow", "unknown",
            ])
        self.assertEqual(result, 2)
        factory.assert_not_called()

        factory.return_value.run.return_value = {
            "workflow_id": "cli-profile", "workflow_name": "bugfix", "target": str(self.target),
            "status": "complete", "authorization": {}, "plan_reviews": [],
        }
        with patch.object(test_helpers.orchestrator, "Orchestrator", factory):
            result = test_helpers.orchestrator.main([
                "run", "--target", str(self.target), "--task", "Fix a small defect",
                "--workflow", "bugfix", "--skip", "investigation,task_correction",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(factory.call_args.kwargs["skip_stages"], ("investigation", "task_correction"))

        factory.reset_mock()
        factory.return_value.resume.return_value = {
            "workflow_id": "cli-profile", "workflow_name": "quick-fix", "target": str(self.target),
            "status": "complete", "authorization": {}, "plan_reviews": [],
        }
        with patch.object(test_helpers.orchestrator, "Orchestrator", factory):
            result = test_helpers.orchestrator.main([
                "resume", "--target", str(self.target), "--workflow-id", "cli-profile",
                "--continue-from", "tests",
            ])
        self.assertEqual(result, 0)
        factory.return_value.resume.assert_called_once_with(continue_from="tests")

    def test_readme_documents_executable_profiles_and_policy_resolution(self):
        readme = (Path(test_helpers.orchestrator.__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8",
        )
        for token in ("full-development", "bugfix", "quick-fix", "implementation-only",
                      "review-only", "--stages A,B,C", "--skip", "--workflow auto"):
            self.assertIn(token, readme)


if __name__ == "__main__":
    unittest.main()

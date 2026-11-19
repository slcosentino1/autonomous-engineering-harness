from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import test_workflow as test_helpers


class WorkflowProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workflow-profile-")
        self.root = Path(self.temporary.name)
        self.target = test_helpers.make_target(self.root / "target")

    def tearDown(self):
        self.temporary.cleanup()

    def test_full_development_profile_is_snapshotted_with_initial_assessment_and_gates(self):
        codex = test_helpers.FakeCodex("# Proposal\nPlan.\n", test_helpers.WorkflowTests.refined())
        claude = test_helpers.FakeClaude(
            test_helpers.claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
        )
        runner = test_helpers.orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="profile-snapshot",
            process_runner=codex, claude_runner=claude,
        )

        state = runner.start()

        self.assertEqual(state["status"], "awaiting_approval")
        self.assertEqual(state["workflow_name"], "full-development")
        self.assertEqual(state["workflow_definition"]["schema_version"], 2)
        self.assertEqual(state["workflow_definition"]["name"], "full-development")
        self.assertEqual(
            {stage["stage_id"] for stage in state["workflow_definition"]["stages"]},
            set(test_helpers.orchestrator.TRACE_STAGES),
        )
        self.assertEqual(state["assessment"]["risk"], "low")
        self.assertEqual(
            [item["phase"] for item in state["assessment_history"]],
            ["initial", "post_planning", "post_planning"],
        )
        self.assertEqual(
            [item["phase"] for item in state["policy_decision_history"]],
            ["initial", "post_planning", "post_planning"],
        )
        self.assertTrue(state["policy_decisions"]["proposal"]["required"])
        self.assertTrue(state["policy_decisions"]["approval"]["required"])
        execution = state["workflow_execution"]
        self.assertEqual(execution["status"], "paused")
        self.assertEqual(execution["waiting_stage"], "implementation")
        self.assertEqual(execution["definition"], state["workflow_definition"])
        self.assertEqual(execution["stages"]["planning"]["status"], "completed")
        self.assertTrue(execution["stages"]["planning"]["attempts"])
        planning_attempt_id = execution["stages"]["planning"]["attempts"][-1]["execution_id"]
        planning_operation = next(
            operation for operation in state["operation_history"] if operation.get("stage") == "planning"
        )
        self.assertEqual(planning_operation["stage_execution_id"], planning_attempt_id)
        self.assertEqual(execution["stages"]["human_revision"]["status"], "not_applicable")
        self.assertEqual(execution["stages"]["implementation"]["status"], "pending")

    def test_selected_workflow_executes_a_distinct_policy_compliant_stage_subset(self):
        selected = (
            "planning", "plan_review_1", "plan_refine_1", "implementation",
            "code_review", "verification", "complete",
        )
        codex = test_helpers.FakeCodex(
            "# Proposal\nPlan.\n", test_helpers.WorkflowTests.refined(),
        )
        claude = test_helpers.FakeClaude(
            test_helpers.claude_reviewer.ReviewResult("Decision: APPROVED\\nPlan is adequate.", 0.05),
        )
        runner = test_helpers.orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="selected-stage-profile",
            process_runner=codex, claude_runner=claude, selected_stages=selected,
        )

        state = runner.start()

        expected = set(selected) | {"correction"}
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertEqual(
            {stage["stage_id"] for stage in state["workflow_definition"]["stages"]}, expected,
        )
        self.assertEqual(
            set(state["workflow_execution"]["stages"]),
            {"planning", "plan_review_1", "plan_refine_1", "implementation"},
        )
        self.assertNotIn("plan_review_2_request", expected)
        self.assertNotIn("human_revision", expected)
        self.assertEqual(state["workflow_execution"]["waiting_stage"], "implementation")

    def test_reassessment_rejects_partial_saved_assessment_without_key_error(self):
        runner = object.__new__(test_helpers.orchestrator.Orchestrator)
        runner.state = {
            "task": "Fix a typo.", "assessment": {"complexity": "low"},
            "workflow_definition": {"proposal_policy": "required", "approval_policy": "always"},
        }

        with self.assertRaisesRegex(test_helpers.orchestrator.WorkflowError, "assessment is incomplete or invalid"):
            runner._reassess_after_planning("A plan")

    def test_direct_workflow_selection_cannot_grant_an_unapproved_capability(self):
        definition, _snapshot, assessment, policy = (
            test_helpers.orchestrator._configuration.resolve_workflow_selection(
                "full-development", "Add a feature",
            )
        )
        forged = replace(definition, stages=tuple(
            replace(stage, capabilities=(*stage.capabilities, "documentation.read"))
            if stage.stage_id == "implementation" else stage
            for stage in definition.stages
        ))
        selection = (
            forged, test_helpers.orchestrator._definition_snapshot(forged), assessment, policy,
        )
        codex = test_helpers.FakeCodex("must not run")
        runner = test_helpers.orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="forged-selection",
            process_runner=codex, workflow_selection=selection,
        )

        with self.assertRaisesRegex(test_helpers.orchestrator.WorkflowError, "conflicts with repository policy"):
            runner.start()

        self.assertEqual(codex.commands, [])
        self.assertFalse(runner.workflow_dir.exists())

    def test_recomputed_definition_digest_cannot_add_a_saved_capability(self):
        runner = test_helpers.orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="pinned-capabilities",
            process_runner=test_helpers.FakeCodex(
                "# Proposal\nPlan.\n", test_helpers.WorkflowTests.refined(),
            ),
            claude_runner=test_helpers.FakeClaude(
                test_helpers.claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            ),
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

        replacement = test_helpers.orchestrator.Orchestrator(
            self.target, workflow_id="pinned-capabilities",
        )
        with self.assertRaisesRegex(test_helpers.orchestrator.WorkflowError, "trusted profile"):
            replacement._read_state()

    def test_policy_incompatible_profile_and_overrides_fail_before_state_or_provider_work(self):
        for options in (
            {"workflow_name": "quick-fix"},
            {"workflow_name": "bugfix"},
            {"workflow_name": "implementation-only"},
            {"skip_stages": ("planning",)},
            {"skip_stages": ("correction",)},
            {"selected_stages": ("context", "implementation")},
            {"selected_stages": ("planning", None)},
            {"workflow_name": "unknown"},
        ):
            with self.subTest(options=options):
                codex = test_helpers.FakeCodex("must not run")
                runner = test_helpers.orchestrator.Orchestrator(
                    self.target, task="Fix a small defect", budget_usd=1.0,
                    workflow_id="rejected-" + str(len(options)) + str(list(options)[0]),
                    process_runner=codex, claude_runner=test_helpers.FakeClaude(), **options,
                )
                with self.assertRaises(test_helpers.orchestrator.WorkflowError):
                    runner.start()
                self.assertEqual(codex.commands, [])
                self.assertFalse(runner.workflow_dir.exists())

    def test_cli_forwards_profile_and_exclusive_stage_override_inputs(self):
        factory = Mock()
        with patch.object(test_helpers.orchestrator, "Orchestrator", factory):
            result = test_helpers.orchestrator.main([
                "start", "--target", str(self.target), "--task", "Do work",
                "--workflow", "full-development", "--skip", "planning",
            ])
        self.assertEqual(result, 2)
        factory.assert_not_called()

        factory.reset_mock()
        factory.return_value.budget_usd = 1.0
        factory.return_value.max_code_review_calls = 1
        factory.return_value.start.return_value = {
            "workflow_id": "cli-profile", "target": str(self.target),
            "status": "awaiting_approval", "authorization": {}, "plan_reviews": [],
        }
        with patch.object(test_helpers.orchestrator, "Orchestrator", factory):
            result = test_helpers.orchestrator.main([
                "start", "--target", str(self.target), "--task", "Do work",
                "--workflow", "full-development", "--stages",
                "planning,plan_review_1,plan_refine_1,plan_review_2,plan_refine_2,human_revision,implementation,code_review,correction,verification,complete",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(
            factory.call_args.kwargs["selected_stages"],
            ("planning", "plan_review_1", "plan_refine_1", "plan_review_2", "plan_refine_2", "human_revision",
             "implementation", "code_review", "correction", "verification", "complete"),
        )
        self.assertEqual(factory.call_args.kwargs["workflow_selection"][0].name, "full-development")

    def test_cli_rejects_combined_skip_and_stage_selection(self):
        with self.assertRaises(SystemExit) as error:
            test_helpers.orchestrator.main([
                "start", "--target", str(self.target), "--task", "Do work",
                "--skip", "planning", "--stages", "planning,implementation",
            ])
        self.assertEqual(error.exception.code, 2)

    def test_cli_rejects_policy_incompatible_profile_before_runner_creation(self):
        factory = Mock()
        with patch.object(test_helpers.orchestrator, "Orchestrator", factory):
            result = test_helpers.orchestrator.main([
                "start", "--target", str(self.target), "--task", "Do work", "--workflow", "quick-fix",
            ])
        self.assertEqual(result, 2)
        factory.assert_not_called()

    def test_readme_documents_profile_policy_and_engine_driven_execution(self):
        readme = (Path(test_helpers.orchestrator.__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8",
        )
        self.assertIn("--workflow full-development", readme)
        self.assertIn("profiles that omit those gates are rejected", readme)
        self.assertIn("--stages A,B,C", readme)
        self.assertIn("resume` still retries only the saved failed stage", readme)
        self.assertIn("The workflow engine drives production stage order", readme)


if __name__ == "__main__":
    unittest.main()

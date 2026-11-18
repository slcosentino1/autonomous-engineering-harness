from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import orchestrator  # noqa: E402
import claude_reviewer  # noqa: E402
from workflow import authorization  # noqa: E402
from workflow import cli  # noqa: E402


class BudgetResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.target = Path(self.temporary.name) / "target"
        self.target.mkdir()
        self.target = self.target.resolve()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.target)], check=True)
        self.workflow_id = "budget-resume"
        self.workflow_dir = self.target / ".engineering" / "workflows" / self.workflow_id
        self.workflow_dir.mkdir(parents=True)
        self.state_path = self.workflow_dir / "state.json"
        auth = authorization.new_workflow_authorization(self.target, self.workflow_id, 1.71)
        auth["claude_aily"]["spent_usd"] = 1.71
        auth["scope_sha256"] = authorization.authorization_scope_digest(auth)
        self.initial = {
            "version": 1,
            "workflow_id": self.workflow_id,
            "target": str(self.target),
            "codex_model": None,
            "status": "failed",
            "stage": "verification",
            "error": "Verification stopped at the existing budget limit.",
            "authorization": auth,
            "resource_usage": {
                "codex_calls": 2, "claude_calls": 4, "provider_runtime_seconds": 1.0,
                "workflow_runtime_seconds": 5.0, "correction_attempts": 1,
            },
            "operation_history": [],
            "claude_contexts": [{"stage": "verification", "call_state": "completed", "cost_usd": 0.25}],
            "review_campaign": {"campaign_id": "review-existing", "purpose": "verification", "status": "failed"},
            "review_campaign_history": [{"campaign_id": "review-initial", "status": "complete"}],
            "verification_report": "Existing verification report",
            "implementation_summary": "Existing implementation summary",
            "correction_summary": "Existing correction summary",
            "implementation_approved_at": {"time": "approved-at-start", "method": "explicit approve command"},
            "commitment_ledger_approval": {"proposal_version": 1, "ledger_sha256": "approved-ledger"},
            "commitment_evidence": {"evidence_sha256": "existing-evidence"},
            "code_review": "Existing code review",
            "recovery": None,
        }
        self.write_state(self.initial)

    def tearDown(self):
        self.temporary.cleanup()

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def runner(self, workflow_id=None):
        return orchestrator.Orchestrator(
            self.target, workflow_id=workflow_id or self.workflow_id,
            process_runner=lambda *_args, **_kwargs: self.fail("Codex provider must not be called"),
            claude_runner=lambda *_args, **_kwargs: self.fail("Claude provider must not be called"),
        )

    def resume_without_providers(self, runner, **kwargs):
        def pending():
            self.assertTrue(runner._process_lock_held)
            return runner.state

        with patch.object(runner, "run_pending", side_effect=pending):
            return runner.resume(**kwargs)

    def test_additive_extension_saves_limit_spend_remaining_and_valid_digest(self):
        runner = self.runner()
        state = self.resume_without_providers(runner, extend_budget="2.00")

        external = state["authorization"]["claude_aily"]
        self.assertEqual(external["budget_usd"], 3.71)
        self.assertEqual(external["spent_usd"], 1.71)
        self.assertEqual(external["budget_changes"][0]["remaining_usd"], 2.0)
        self.assertEqual(external["budget_changes"][0]["operation"], "extend")
        self.assertEqual(external["budget_changes"][0]["requested_amount_usd"], 2.0)
        self.assertEqual(external["budget_changes"][0]["workflow_id"], self.workflow_id)
        self.assertEqual(external["budget_changes"][0]["authorization_id"], state["authorization"]["authorization_id"])
        self.assertEqual(external["budget_changes"][0]["command"], "resume")
        self.assertEqual(external["budget_changes"][0]["stage"], "verification")
        self.assertTrue(external["budget_changes"][0]["timestamp"].endswith("Z"))
        authorization.validate_workflow_authorization(state, self.target, self.workflow_id)

        status = runner.status()
        summary = StringIO()
        with redirect_stdout(summary):
            orchestrator._cli.print_summary(status, workflow_root=".engineering/workflows")
        self.assertEqual(json.loads(summary.getvalue())["effective_budget_usd"], 3.71)

    def test_absolute_limit_and_allowed_lower_or_equal_limits_are_recorded(self):
        self.initial["authorization"]["claude_aily"]["spent_usd"] = 1.25
        self.initial["authorization"]["scope_sha256"] = authorization.authorization_scope_digest(
            self.initial["authorization"],
        )
        self.write_state(self.initial)
        runner = self.runner()
        self.resume_without_providers(runner, set_budget_limit="1.50")
        self.assertEqual(runner.state["authorization"]["claude_aily"]["budget_usd"], 1.5)
        self.assertEqual(runner.state["authorization"]["claude_aily"]["budget_changes"][0]["remaining_usd"], 0.25)

        runner.state["status"] = "failed"
        with runner._workflow_lock():
            runner.execution._save()
        self.resume_without_providers(runner, set_budget_limit="1.50")
        changes = runner.state["authorization"]["claude_aily"]["budget_changes"]
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[-1]["previous_limit_usd"], 1.5)
        self.assertEqual(changes[-1]["requested_amount_usd"], 1.5)

    def test_set_limit_below_spend_rejects_without_mutation(self):
        state = copy.deepcopy(self.initial)
        state["authorization"]["claude_aily"]["spent_usd"] = 1.7149
        state["authorization"]["scope_sha256"] = authorization.authorization_scope_digest(state["authorization"])
        self.write_state(state)
        before = self.state_path.read_bytes()
        with self.assertRaisesRegex(orchestrator.WorkflowError, "below accumulated spending"):
            self.runner().resume(set_budget_limit="1.71")
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_budget_change_is_rejected_when_resume_would_not_retry_a_failed_stage(self):
        state = copy.deepcopy(self.initial)
        state["status"] = "awaiting_approval"
        self.write_state(state)
        before = self.state_path.read_bytes()
        with self.assertRaisesRegex(orchestrator.WorkflowError, "only when resuming a saved failed stage"):
            self.runner().resume(extend_budget="1.00")
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_equal_limit_is_saved_but_insufficient_budget_leaves_stage_failed(self):
        stderr = StringIO()
        runner = self.runner()
        with redirect_stderr(stderr), self.assertRaisesRegex(orchestrator.WorkflowError, "less than USD 0.01"):
            runner.resume(set_budget_limit="1.71")

        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["stage"], "verification")
        self.assertEqual(saved["authorization"]["claude_aily"]["budget_usd"], 1.71)
        self.assertEqual(len(saved["authorization"]["claude_aily"]["budget_changes"]), 1)
        self.assertIn("budget authorization saved", stderr.getvalue())

    def test_invalid_amounts_do_not_mutate_state(self):
        for amount in (
            "bad", "1_0", "0", "-0.01", "NaN", "Infinity", "1.001", "1e309",
            "9999999999999999.99",
        ):
            with self.subTest(amount=amount):
                before = self.state_path.read_bytes()
                with self.assertRaises(orchestrator.WorkflowError):
                    self.runner().resume(extend_budget=amount)
                self.assertEqual(self.state_path.read_bytes(), before)

    def test_legacy_state_and_arbitrary_edits_keep_existing_integrity_behavior(self):
        legacy = copy.deepcopy(self.initial)
        legacy["authorization"]["claude_aily"].pop("budget_changes", None)
        legacy["authorization"]["scope_sha256"] = authorization.authorization_scope_digest(legacy["authorization"])
        self.write_state(legacy)
        runner = self.runner()
        with patch.object(runner, "run_pending", side_effect=lambda: runner.state):
            resumed = runner.resume()
        self.assertEqual(resumed["authorization"]["claude_aily"]["budget_usd"], 1.71)
        self.assertNotIn("budget_changes", resumed["authorization"]["claude_aily"])

        edited = json.loads(self.state_path.read_text(encoding="utf-8"))
        edited["authorization"]["claude_aily"]["budget_usd"] = 9.0
        self.write_state(edited)
        with self.assertRaisesRegex(orchestrator.WorkflowError, "authorization changed"):
            self.runner().status()

    def test_history_arithmetic_and_digest_protect_legitimate_changes(self):
        runner = self.runner()
        self.resume_without_providers(runner, extend_budget="0.50")
        authorization.validate_workflow_authorization(runner.state, self.target, self.workflow_id)

        malformed = copy.deepcopy(runner.state)
        malformed["authorization"]["claude_aily"]["budget_changes"][0]["new_limit_usd"] = 3.0
        malformed["authorization"]["scope_sha256"] = authorization.authorization_scope_digest(
            malformed["authorization"],
        )
        with self.assertRaisesRegex(authorization.AuthorizationError, "history arithmetic"):
            authorization.validate_workflow_authorization(malformed, self.target, self.workflow_id)

        arbitrary = copy.deepcopy(runner.state)
        arbitrary["authorization"]["claude_aily"]["budget_changes"][0]["requested_amount_usd"] = 7.0
        with self.assertRaisesRegex(authorization.AuthorizationError, "history arithmetic|changed after initialization"):
            authorization.validate_workflow_authorization(arbitrary, self.target, self.workflow_id)

    def test_multiple_extensions_append_history_and_keep_spend(self):
        runner = self.runner()
        self.resume_without_providers(runner, extend_budget="0.50")
        runner.state["status"] = "failed"
        with runner._workflow_lock():
            runner.execution._save()
        second = self.runner()
        self.resume_without_providers(second, extend_budget="0.25")

        external = second.state["authorization"]["claude_aily"]
        self.assertEqual(external["budget_usd"], 2.46)
        self.assertEqual(external["spent_usd"], 1.71)
        self.assertEqual(len(external["budget_changes"]), 2)
        self.assertEqual(external["budget_changes"][1]["previous_limit_usd"], 2.21)
        self.assertEqual(external["budget_changes"][1]["remaining_usd"], 0.75)
        authorization.validate_workflow_authorization(second.state, self.target, self.workflow_id)

        malformed = copy.deepcopy(second.state)
        malformed_changes = malformed["authorization"]["claude_aily"]["budget_changes"]
        malformed_changes[1]["spent_usd"] = 1.0
        malformed_changes[1]["remaining_usd"] = 1.46
        malformed["authorization"]["scope_sha256"] = authorization.authorization_scope_digest(
            malformed["authorization"],
        )
        with self.assertRaisesRegex(authorization.AuthorizationError, "spending decreased"):
            authorization.validate_workflow_authorization(malformed, self.target, self.workflow_id)

    def test_failed_retry_keeps_change_and_plain_resume_does_not_extend_again(self):
        runner = self.runner()

        def fail_retry():
            runner.state["status"] = "failed"
            runner.state["error"] = "Fake verification retry failed."
            runner.execution._save()
            raise orchestrator.WorkflowError("Fake verification retry failed.")

        with patch.object(runner, "run_pending", side_effect=fail_retry):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "Fake verification retry failed"):
                runner.resume(extend_budget="0.50")
        failed = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["authorization"]["claude_aily"]["budget_usd"], 2.21)
        self.assertEqual(len(failed["authorization"]["claude_aily"]["budget_changes"]), 1)

        later = self.runner()
        with patch.object(later, "run_pending", side_effect=lambda: later.state):
            later.resume()
        external = later.state["authorization"]["claude_aily"]
        self.assertEqual(external["budget_usd"], 2.21)
        self.assertEqual(len(external["budget_changes"]), 1)

    def test_verification_resume_preserves_campaign_reports_evidence_and_workflow_identity(self):
        original = copy.deepcopy(self.initial)
        runner = self.runner()
        observed = {}

        def retry_saved_stage():
            observed["workflow_id"] = runner.state["workflow_id"]
            observed["stage"] = runner.state["stage"]
            observed["campaign"] = copy.deepcopy(runner.state["review_campaign"])
            observed["reports"] = copy.deepcopy(runner.state["review_campaign_history"])
            observed["evidence"] = copy.deepcopy(runner.state["commitment_evidence"])
            observed["implementation"] = runner.state["implementation_summary"]
            observed["correction"] = runner.state["correction_summary"]
            observed["verification_report"] = runner.state["verification_report"]
            observed["approval"] = copy.deepcopy(runner.state["implementation_approved_at"])
            observed["ledger_approval"] = copy.deepcopy(runner.state["commitment_ledger_approval"])
            observed["spend"] = runner.state["authorization"]["claude_aily"]["spent_usd"]
            observed["limit"] = runner.state["authorization"]["claude_aily"]["budget_usd"]
            return runner.state

        with patch.object(runner, "run_pending", side_effect=retry_saved_stage):
            runner.resume(extend_budget="2.00")

        self.assertEqual(observed["workflow_id"], self.workflow_id)
        self.assertEqual(observed["stage"], "verification")
        self.assertEqual(observed["campaign"], original["review_campaign"])
        self.assertEqual(observed["reports"], original["review_campaign_history"])
        self.assertEqual(observed["evidence"], original["commitment_evidence"])
        self.assertEqual(observed["implementation"], original["implementation_summary"])
        self.assertEqual(observed["correction"], original["correction_summary"])
        self.assertEqual(observed["verification_report"], original["verification_report"])
        self.assertEqual(observed["approval"], original["implementation_approved_at"])
        self.assertEqual(observed["ledger_approval"], original["commitment_ledger_approval"])
        self.assertEqual(observed["spend"], 1.71)
        self.assertEqual(observed["limit"], 3.71)

    def test_budget_change_can_resume_each_saved_resumable_stage(self):
        for stage in sorted(authorization._RESUMABLE_STAGES):
            with self.subTest(stage=stage):
                state = copy.deepcopy(self.initial)
                state["stage"] = stage
                self.write_state(state)
                runner = self.runner()
                observed = {}

                def pending():
                    observed["stage"] = runner.state["stage"]
                    return runner.state

                with patch.object(runner, "run_pending", side_effect=pending):
                    runner.resume(extend_budget="0.01")
                self.assertEqual(observed["stage"], stage)
                self.assertEqual(
                    runner.state["authorization"]["claude_aily"]["budget_changes"][0]["stage"], stage,
                )

    def test_fake_claude_call_uses_extended_remaining_budget_as_cap(self):
        state = copy.deepcopy(self.initial)
        external = state["authorization"]["claude_aily"]
        external["budget_usd"] = 3.71
        state["authorization"]["scope_sha256"] = authorization.authorization_scope_digest(state["authorization"])
        runner = self.runner()
        runner.state = state
        runner._process_lock_held = True
        fake_claude = Mock(return_value=claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        runner.claude_runner = fake_claude
        runner.execution.claude_runner = fake_claude
        sections = {
            "Review metadata": "metadata", "Assignment": "assignment", "Current proposal": "proposal",
            "Repository context": "context",
            "Workflow constraints": "constraints", "Engineering Judgment & Proportional Design": "principles",
            "Relevant skills": "skills", "Review duties": "duties", "Independence boundary": "boundary",
        }
        with (patch("orchestrator.claude_reviewer.max_review_payload_chars", return_value=100_000),
              patch("orchestrator.claude_reviewer.validate_review_invocation"),
              patch.object(runner.workflow_state, "save"),
              patch.object(runner.workflow_state, "operation_launching")):
            runner._call_claude(
                "Review this proposal", "plan_review_1", sections,
                metadata={"iteration": 1, "proposal_version": 1},
            )
        self.assertEqual(fake_claude.call_args.kwargs["budget_usd"], 2.0)

    def test_unknown_spend_blocks_budget_change_without_mutation(self):
        state = copy.deepcopy(self.initial)
        state["authorization"]["claude_aily"]["budget_unknown"] = True
        state["authorization"]["scope_sha256"] = authorization.authorization_scope_digest(state["authorization"])
        self.write_state(state)
        before = self.state_path.read_bytes()
        with self.assertRaises(orchestrator.WorkflowError):
            self.runner().resume(extend_budget="1.00")
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_budget_options_are_mutually_exclusive_and_cli_no_option_is_unchanged(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            cli.main(
                ["resume", "--target", str(self.target), "--workflow-id", self.workflow_id,
                 "--extend-budget", "1.00", "--set-budget-limit", "3.00"],
                runner_factory=Mock(), recover_review=Mock(), workflow_error=RuntimeError,
                workflow_root=".engineering/workflows", max_authorized_code_review_calls=64,
            )

        fake_runner = Mock()
        fake_runner.resume.return_value = {
            "workflow_id": self.workflow_id, "target": str(self.target), "status": "failed",
            "stage": "verification", "authorization": {"claude_aily": {"budget_usd": 1.71, "spent_usd": 1.71}},
        }
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = cli.main(
                ["resume", "--target", str(self.target), "--workflow-id", self.workflow_id],
                runner_factory=Mock(return_value=fake_runner), recover_review=Mock(),
                workflow_error=RuntimeError, workflow_root=".engineering/workflows",
                max_authorized_code_review_calls=64,
            )
        self.assertEqual(result, 1)
        fake_runner.resume.assert_called_once_with()

    def test_cli_passes_explicit_budget_option_to_resume(self):
        fake_runner = Mock()
        fake_runner.resume.return_value = {
            "workflow_id": self.workflow_id, "target": str(self.target), "status": "failed",
            "stage": "verification", "authorization": {"claude_aily": {"budget_usd": 2.21, "spent_usd": 1.71}},
        }
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            cli.main(
                ["resume", "--target", str(self.target), "--workflow-id", self.workflow_id,
                 "--extend-budget", "0.50"],
                runner_factory=Mock(return_value=fake_runner), recover_review=Mock(),
                workflow_error=RuntimeError, workflow_root=".engineering/workflows",
                max_authorized_code_review_calls=64,
            )
        self.assertEqual(fake_runner.resume.call_args.kwargs["extend_budget"], "0.50")

    def test_readme_documents_budget_options_and_saved_audit_history(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "--extend-budget", "--set-budget-limit", "including prior spend",
            "may be lowered or set equal", "authorization.claude_aily.budget_changes",
            "each repeated extension is a new addition", "later plain `resume` uses the saved limit",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)

    def test_lock_contention_prevents_concurrent_budget_mutation(self):
        runner = self.runner()
        before = self.state_path.read_bytes()
        with runner._workflow_lock():
            contender = self.runner()
            with self.assertRaisesRegex(orchestrator.WorkflowError, "already being modified"):
                contender.resume(extend_budget="1.00")
        self.assertEqual(self.state_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

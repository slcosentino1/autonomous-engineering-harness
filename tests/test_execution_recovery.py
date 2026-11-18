from __future__ import annotations

import inspect
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import orchestrator
from workflow.execution import StageOutcome, WorkflowExecution
from workflow.state import RecoveryFacts, decide_recovery


class RecoveryPolicyTests(unittest.TestCase):
    def facts(self, **changes) -> RecoveryFacts:
        values = {
            "operation_id": "operation-1",
            "operation_type": "codex",
            "operation_stage": "implementation",
            "operation_status": "interrupted",
            "operation_sandbox": "workspace-write",
            "process_presence": "not_running",
            "target_change_paths": ("src/module.py",),
            "target_changes_overlap_preexisting": False,
            "target_fingerprint_status": "captured",
            "claude_budget_unknown": False,
            "claude_budget_usd": None,
            "claude_spent_usd": None,
            "claude_call_cap_usd": None,
            "action": "retry",
            "verified_claude_cost_usd": None,
            "confirm_provider_inactive": False,
            "confirm_process_inactive": True,
            "confirm_target_reviewed": True,
        }
        values.update(changes)
        return RecoveryFacts(**values)

    def test_load_reconciliation_and_resume_share_recovery_decision_contract(self):
        for method in (orchestrator.Orchestrator._require_recovery, orchestrator.Orchestrator._resolve_recovery):
            source = inspect.getsource(method)
            self.assertIn("self._recovery_facts(", source)
            self.assertIn("decide_recovery(", source)

        decision = decide_recovery(self.facts(action=None))
        self.assertEqual(decision.kind, "requires_recovery")
        self.assertIsNone(decision.error)

        retry = decide_recovery(self.facts())
        self.assertEqual(retry.kind, "retry")
        self.assertEqual(retry.resolution, "user_approved_retry_after_target_inspection")

    def test_write_retry_requires_process_and_target_confirmation(self):
        no_process_confirmation = decide_recovery(self.facts(confirm_process_inactive=False))
        self.assertEqual(no_process_confirmation.kind, "blocked")
        self.assertIn("--confirm-process-inactive", no_process_confirmation.error)

        no_target_confirmation = decide_recovery(self.facts(confirm_target_reviewed=False))
        self.assertEqual(no_target_confirmation.kind, "blocked")
        self.assertIn("--confirm-target-reviewed", no_target_confirmation.error)

    def test_interrupted_implementation_acceptance_rejects_preexisting_overlap(self):
        decision = decide_recovery(self.facts(
            action="accept-implementation",
            target_changes_overlap_preexisting=True,
        ))
        self.assertEqual(decision.kind, "blocked")
        self.assertIn("overlap paths", decision.error)

    def test_claude_retry_requires_verified_cost_within_budget_and_call_cap(self):
        facts = self.facts(
            operation_type="claude",
            operation_stage="code_review",
            operation_sandbox=None,
            claude_budget_unknown=True,
            claude_budget_usd="1.00",
            claude_spent_usd="0.50",
            claude_call_cap_usd="0.25",
            verified_claude_cost_usd=0.20,
            confirm_provider_inactive=True,
        )
        decision = decide_recovery(facts)
        self.assertEqual(decision.kind, "retry")
        self.assertEqual(decision.resolution_cost_usd, 0.2)

        over_cap = decide_recovery(RecoveryFacts(**{
            **facts.__dict__,
            "verified_claude_cost_usd": 0.26,
        }))
        self.assertEqual(over_cap.kind, "blocked")
        self.assertIn("active call cap", over_cap.error)


class ReviewTargetAcceptanceTests(unittest.TestCase):
    def review_diff(self, content="accepted diff"):
        return SimpleNamespace(
            text=content,
            included_paths=("src/module.py",),
            excluded_files=(),
            file_patches=(("src/module.py", content),),
            workflow_paths=(),
            context_files=(),
            context_exclusions=(),
            operation_diffs=(),
        )

    def coordinator(self, *, current="accepted", previous="implementation", changes=None, history=None,
                    legacy=None, baseline_commit="baseline"):
        coordinator = object.__new__(orchestrator.Orchestrator)
        coordinator.state = {
            "status": "failed", "stage": "code_review", "active_operation_id": None,
            "baseline_commit": baseline_commit,
            "baseline_snapshot": {
                "sha256": "snapshot-sha",
                "baseline_fingerprint_sha256": "initial-tree",
            },
            "execution_fingerprint": {"initial": {"target": {
                "status": "captured", "commit_sha": baseline_commit,
                "working_tree_sha256": "initial-tree",
            }}},
            "operation_history": [{
                "operation_id": "implementation-1", "operation_type": "codex",
                "stage": "implementation", "status": "completed",
                "target_fingerprint_after": {
                    "status": "captured", "commit_sha": baseline_commit,
                    "working_tree_sha256": previous,
                },
            }],
        }
        if history is not None:
            coordinator.state["review_target_acceptances"] = history
        if legacy is not None:
            coordinator.state["review_target_acceptance"] = legacy
        coordinator.workflow_state = SimpleNamespace(save=lambda: None)
        coordinator.target = Path("/tmp/target")
        coordinator._recovery_target_fingerprint = lambda: {
            "status": "captured", "commit_sha": baseline_commit, "working_tree_sha256": current,
        }
        coordinator._recovery_target_change_inventory = lambda: (
            changes if changes is not None else [{"path": "src/module.py", "change": "modified", "preexisting": "no"}]
        )
        return coordinator

    @staticmethod
    def previous_fingerprint(tree="implementation"):
        return {"status": "captured", "commit_sha": "baseline", "working_tree_sha256": tree}

    @staticmethod
    def accepted_fingerprint(tree="accepted"):
        return {"status": "captured", "commit_sha": "baseline", "working_tree_sha256": tree}

    def test_acceptance_requires_confirmation_and_records_baseline_diff_and_changes(self):
        coordinator = self.coordinator()
        diff = self.review_diff()

        with self.assertRaisesRegex(orchestrator.WorkflowError, "confirm-target-reviewed"):
            coordinator._accept_current_target_for_review(confirm_target_reviewed=False)

        with patch.object(orchestrator, "_review_diff", return_value=diff):
            coordinator._accept_current_target_for_review(confirm_target_reviewed=True)

        acceptance = coordinator.state["review_target_acceptance"]
        self.assertEqual(acceptance["operation_id"], "implementation-1")
        self.assertEqual(acceptance["previous_target_fingerprint"], self.previous_fingerprint())
        self.assertEqual(acceptance["target_fingerprint"]["working_tree_sha256"], "accepted")
        self.assertEqual(acceptance["changed_path_count"], 1)
        self.assertEqual(acceptance["changed_paths"][0]["path"], "src/module.py")
        self.assertEqual(
            acceptance["baseline_diff_sha256"],
            orchestrator._ReviewCampaign._campaign_source_hash(diff),
        )
        self.assertEqual(acceptance["baseline_commit"], "baseline")
        self.assertEqual(acceptance["baseline_fingerprint_sha256"], "initial-tree")
        self.assertEqual(coordinator.state["review_target_acceptances"], [acceptance])

    def test_acceptance_rejects_missing_baseline_snapshot_without_persisting(self):
        coordinator = self.coordinator()
        coordinator.state.pop("baseline_snapshot")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "requires an intact baseline snapshot"):
            coordinator._accept_current_target_for_review(confirm_target_reviewed=True)
        self.assertNotIn("review_target_acceptances", coordinator.state)

    def test_continuation_appends_without_overwriting_legacy_acceptance(self):
        legacy = {
            "accepted_at": "prior-time", "operation_id": "implementation-1",
            "target_fingerprint": self.accepted_fingerprint("prior-accepted"),
            "changed_path_count": 2,
            "confirmation": "explicit_target_reviewed_for_independent_review",
        }
        coordinator = self.coordinator(current="next-accepted", legacy=legacy)

        with patch.object(orchestrator, "_review_diff", return_value=self.review_diff("new diff")):
            coordinator._accept_current_target_for_review(confirm_target_reviewed=True)

        history = coordinator.state["review_target_acceptances"]
        self.assertEqual(len(history), 2)
        self.assertTrue(history[0]["legacy_record_without_diff_binding"])
        self.assertEqual(history[0]["target_fingerprint"], legacy["target_fingerprint"])
        self.assertEqual(history[1]["previous_target_fingerprint"], legacy["target_fingerprint"])
        self.assertEqual(history[1]["target_fingerprint"]["working_tree_sha256"], "next-accepted")
        self.assertEqual(coordinator.state["review_target_acceptance"], history[1])

        prior_new_acceptance = dict(history[1])
        coordinator._recovery_target_fingerprint = lambda: {
            "status": "captured", "commit_sha": "baseline", "working_tree_sha256": "third-accepted",
        }
        coordinator._recovery_target_change_inventory = lambda: [
            {"path": "src/module.py", "change": "modified", "preexisting": "no"},
            {"path": "src/other.py", "change": "added", "preexisting": "no"},
        ]
        third_diff = self.review_diff("third diff")
        with patch.object(orchestrator, "_review_diff", return_value=third_diff):
            coordinator._accept_current_target_for_review(confirm_target_reviewed=True)
        history = coordinator.state["review_target_acceptances"]
        self.assertEqual(len(history), 3)
        self.assertEqual(history[1], prior_new_acceptance)
        self.assertEqual(history[2]["previous_target_fingerprint"], prior_new_acceptance["target_fingerprint"])
        self.assertEqual(history[2]["target_fingerprint"]["working_tree_sha256"], "third-accepted")

        execution = object.__new__(WorkflowExecution)
        execution.workflow_state = coordinator.state
        execution.target = coordinator.target
        execution._fingerprint_repository = lambda _target: {
            "status": "captured", "commit_sha": "baseline", "working_tree_sha256": "third-accepted",
        }
        execution._target_change_inventory = lambda: history[2]["changed_paths"]
        with patch("workflow.execution._review_diff", return_value=third_diff):
            execution._require_write_operation_target("initial")

    def test_review_gate_checks_fingerprint_and_complete_baseline_diff_hash(self):
        execution = object.__new__(WorkflowExecution)
        diff = self.review_diff()
        source_hash = orchestrator._ReviewCampaign._campaign_source_hash(diff)
        implementation = {
            "operation_id": "implementation-1", "operation_type": "codex",
            "stage": "implementation", "status": "completed",
            "target_fingerprint_after": self.previous_fingerprint(),
        }
        acceptance = {
            "accepted_at": "accepted-time",
            "operation_id": "implementation-1",
            "previous_target_fingerprint": self.previous_fingerprint(),
            "target_fingerprint": self.accepted_fingerprint(),
            "changed_paths": [{"path": "src/module.py", "change": "modified", "preexisting": "no"}],
            "changed_path_count": 1,
            "baseline_commit": "baseline",
            "baseline_fingerprint_sha256": "initial-tree",
            "baseline_snapshot_sha256": "snapshot-sha",
            "baseline_diff_sha256": source_hash,
            "confirmation": "explicit_target_reviewed_for_independent_review",
        }
        execution.workflow_state = {
            "baseline_commit": "baseline",
            "baseline_snapshot": {
                "schema_version": 1, "sha256": "snapshot-sha",
                "baseline_fingerprint_sha256": "initial-tree",
            },
            "execution_fingerprint": {"initial": {"target": {
                "status": "captured", "commit_sha": "baseline", "working_tree_sha256": "initial-tree",
            }}},
            "operation_history": [implementation],
            "review_target_acceptances": [acceptance],
        }
        execution.target = Path("/tmp/target")
        execution._fingerprint_repository = lambda _target: {
            "status": "captured", "commit_sha": "baseline", "working_tree_sha256": "accepted",
        }
        with (patch("workflow.execution._review_diff", return_value=diff),
              patch.object(execution, "_target_change_inventory", return_value=acceptance["changed_paths"])):
            execution._require_write_operation_target("initial")

        execution._fingerprint_repository = lambda _target: {
            "status": "captured", "commit_sha": "baseline", "working_tree_sha256": "changed-after-acceptance",
        }
        with self.assertRaisesRegex(RuntimeError, "Target changed after explicit current-target acceptance"):
            execution._require_write_operation_target("initial")

        execution._fingerprint_repository = lambda _target: {
            "status": "captured", "commit_sha": "baseline", "working_tree_sha256": "accepted",
        }
        with patch("workflow.execution._review_diff", return_value=self.review_diff("tampered diff")):
            with self.assertRaisesRegex(RuntimeError, "Baseline-to-current review diff changed after acceptance"):
                execution._require_write_operation_target("initial")

        with (patch("workflow.execution._review_diff", return_value=diff),
              patch.object(execution, "_target_change_inventory", return_value=[])):
            with self.assertRaisesRegex(RuntimeError, "changed-path inventory no longer matches"):
                execution._require_write_operation_target("initial")

    def test_review_gate_rejects_inconsistent_acceptance_chain(self):
        execution = object.__new__(WorkflowExecution)
        execution.target = Path("/tmp/target")
        execution.workflow_state = {
            "baseline_commit": "baseline",
            "baseline_snapshot": {"sha256": "snapshot-sha", "baseline_fingerprint_sha256": "initial-tree"},
            "execution_fingerprint": {"initial": {"target": {
                "status": "captured", "commit_sha": "baseline", "working_tree_sha256": "initial-tree",
            }}},
            "operation_history": [{
                "operation_id": "implementation-1", "operation_type": "codex",
                "stage": "implementation", "status": "completed",
                "target_fingerprint_after": self.previous_fingerprint(),
            }],
            "review_target_acceptances": [{
                "accepted_at": "accepted-time",
                "operation_id": "implementation-1",
                "previous_target_fingerprint": self.accepted_fingerprint("wrong-previous"),
                "target_fingerprint": self.accepted_fingerprint(),
                "changed_paths": [], "changed_path_count": 0,
                "baseline_commit": "baseline", "baseline_fingerprint_sha256": "initial-tree",
                "baseline_snapshot_sha256": "snapshot-sha", "baseline_diff_sha256": "a" * 64,
                "confirmation": "explicit_target_reviewed_for_independent_review",
            }],
            "review_target_acceptance": {"inconsistent": True},
        }
        with self.assertRaisesRegex(RuntimeError, "acceptance chain is inconsistent"):
            execution._require_write_operation_target("initial")

    def test_review_gate_rejects_acceptance_when_baseline_snapshot_is_missing(self):
        execution = object.__new__(WorkflowExecution)
        execution.target = Path("/tmp/target")
        execution.workflow_state = {
            "review_target_acceptances": [{"target_fingerprint": self.accepted_fingerprint()}],
        }
        with self.assertRaisesRegex(RuntimeError, "baseline snapshot is unavailable"):
            execution._require_write_operation_target("initial")

    def test_review_campaign_rechecks_accepted_diff_before_claude(self):
        execution = object.__new__(WorkflowExecution)
        diff = self.review_diff()
        source_hash = orchestrator._ReviewCampaign._campaign_source_hash(diff)
        execution.workflow_state = {
            "review_target_acceptances": [{"baseline_diff_sha256": source_hash}],
        }
        execution._require_write_operation_target = lambda _purpose: (_ for _ in ()).throw(
            RuntimeError("target drift")
        )
        execution._trace_span = lambda *_args, **_kwargs: self.fail("provider boundary must not be entered")
        execution._campaign_helper = lambda call: self.fail("Claude campaign must not be called")
        with self.assertRaisesRegex(RuntimeError, "target drift"):
            execution._run_review_campaign(
                {"purpose": "initial"}, {}, [], diff,
            )

        execution._require_write_operation_target = lambda _purpose: None
        campaign = {
            "purpose": "initial", "source_patch_sha256": "different-source",
            "header_id": "code-review-part-v2", "shared_context_sha256": "unused", "parts": [],
        }
        with self.assertRaisesRegex(RuntimeError, "does not match the explicitly accepted baseline diff"):
            execution._run_review_campaign(campaign, {}, [], diff)

        campaign["source_patch_sha256"] = source_hash
        campaign["shared_context_sha256"] = "wrong-context"
        with self.assertRaisesRegex(RuntimeError, "shared context differs"):
            execution._run_review_campaign(campaign, {}, [], diff)

    def test_current_target_acceptance_rejects_preexisting_path_overlap(self):
        coordinator = self.coordinator(
            changes=[{"path": "src/module.py", "change": "modified", "preexisting": "yes"}],
        )

        with self.assertRaisesRegex(orchestrator.WorkflowError, "pre-existing target paths"):
            coordinator._accept_current_target_for_review(confirm_target_reviewed=True)


class StageOutcomeTests(unittest.TestCase):
    def test_stage_outcome_is_a_semantic_result_not_a_saved_transition(self):
        outcome = StageOutcome("review_clean")
        self.assertEqual(outcome.result, "review_clean")

    def test_stage_outcome_rejects_unknown_results(self):
        with self.assertRaisesRegex(RuntimeError, "unsupported result"):
            StageOutcome("new_persisted_data")

    def test_stage_execution_returns_result_without_mutating_saved_state(self):
        execution = object.__new__(WorkflowExecution)
        execution.workflow_state = {"stage": "planning"}
        execution._stage_execution_active = False
        execution._pending_stage_result = None
        execution._dispatch_stage = lambda _stage: execution._stage_result("completed")

        outcome = execution._execute_stage("planning")

        self.assertEqual(execution.workflow_state["stage"], "planning")
        self.assertEqual(outcome.result, "completed")

    def test_coordinator_maps_stage_results_to_workflow_transitions(self):
        from types import SimpleNamespace

        coordinator = object.__new__(orchestrator.Orchestrator)
        coordinator.workflow_state = SimpleNamespace(document={})
        self.assertEqual(
            coordinator._transition_after_stage("planning", StageOutcome("completed")),
            {"stage": "plan_review_1"},
        )
        self.assertEqual(
            coordinator._transition_after_stage("code_review", StageOutcome("blocking_findings")),
            {"stage": "correction"},
        )
        self.assertEqual(
            coordinator._transition_after_stage("implementation", StageOutcome("implemented")),
            {"stage": "code_review"},
        )


if __name__ == "__main__":
    unittest.main()

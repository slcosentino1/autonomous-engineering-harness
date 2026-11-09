import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
import copy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import orchestrator  # noqa: E402


def config(**updates):
    result = {
        "branch": "feature/task",
        "authorized_code_paths": ["src"],
        "authorized_artifact_paths": [],
        "code_file_extensions": [],
        "minimum_risk_band": "medium",
        "max_iterations": 2,
        "stage_timeout_seconds": 30,
        "claude_budget_usd": 2.0,
        "validations": [{"id": "tests", "argv": ["python3", "-m", "unittest"]}],
        "review": {"enabled": True},
        "commit": False,
    }
    result.update(updates)
    return result


def persistent_claude_authorization(repository, **updates):
    now = datetime.now(timezone.utc)
    result = {
        "authorization_id": "AUTH-CLAUDE-TEST",
        "provider": "aily",
        "destination": "Claude through Aily",
        "permitted_repositories": [str(Path(repository).resolve())],
        "permitted_paths": ["src", ".engineering/workflows/flow-test-1"],
        "mode": "read_only",
        "max_budget_usd": 3.0,
        "sensitive_exclusions": ["credentials", "environment_files", "private_configuration", "unrelated_files"],
        "authorized_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "expires_at": (now + timedelta(days=30)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "revoked": False,
        "authorization_reference": "user-config:claude-authorization",
        "authorizer_identity": "declared task owner",
        "identity_assurance": "declared",
        "authorizer_authentication_reference": None,
    }
    result.update(updates)
    return result


def plan(*, increments=None, risk="medium"):
    return {
        "decision": "READY", "summary": "Plan is ready.", "risk_band": risk,
        "decision_context": None,
        "requirements": [{"id": "REQ-1", "text": "Implement the requested behavior.", "source": "task"}],
        "acceptance_criteria": [{"id": "AC-1", "text": "The behavior is verified.", "source": "task"}],
        "architecture_context": [], "documentation_context": [], "capabilities_used": [],
        "dependencies": [], "blockers": [],
        "engineering_discovery": {"quality_requirements": [], "constraints_and_dependencies": [],
            "ambiguities": [], "scenarios": [{"id": "SC-1", "scenario": "Normal operation",
                "expected_behavior": "The requested behavior works", "verification": "Run the configured test",
                "requirement_ids": ["REQ-1"]}], "assumptions": [], "decisions": []},
        "increments": increments or [{
            "increment_id": "inc-1", "title": "Implement feature", "code_paths": ["src"],
            "artifact_paths": [], "revision_paths": ["src"],
            "acceptance_criteria_ids": ["AC-1"], "validation_ids": ["tests"], "blocked_by_decision": False,
        }],
    }


def git_fixture(root: Path, branch="feature/task") -> tuple[Path, str]:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Harness Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "harness-test@example.invalid"], check=True)
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "baseline"], check=True)
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return root, head


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orchestrator-tests-")
        self.target = Path(self.temp.name) / "target"
        self.target.mkdir()
        (self.target / "src").mkdir()
        self.flow = "flow-test-1"
        self.runner = orchestrator.Orchestrator(
            str(self.target), "Add a feature", config(), self.flow,
            process_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"ok", b""),
        )

    def tearDown(self):
        self.temp.cleanup()

    def new_git_workflow(self, name, *, files=None, workflow_id=None):
        target = Path(self.temp.name) / name
        git_fixture(target)
        (target / "src").mkdir()
        for relative, content in (files or {}).items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        flow = workflow_id or f"flow-{name}"
        runner = orchestrator.Orchestrator(
            str(target), "Add a feature", config(), flow,
            process_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"ok", b""),
        )
        runner.initialize()
        return target, runner

    def plan_review_workflow(self, name):
        target = Path(self.temp.name) / name
        git_fixture(target)
        (target / "src").mkdir()
        workflow_id = f"flow-{name}"
        authorization = persistent_claude_authorization(target,
            permitted_paths=["src", f".engineering/workflows/{workflow_id}"])
        runner = orchestrator.Orchestrator(str(target), "Add a feature",
            config(review={"enabled": True, "authorization": authorization}), workflow_id,
            process_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"ok", b""))
        with patch("safe_writer._run_preflight", return_value={
                "overall_result": "READY", "target_path": str(target.resolve())}):
            runner.initialize()
        return target, runner

    @staticmethod
    def successful_plan_review(request, report=None):
        now = orchestrator._now()
        report = report or {"decision": "APPROVED", "summary": "The complete proposal covers the assignment.", "findings": []}
        process = {"invocation_attempted_at": now, "review_manifest_sha256": "a" * 64,
            "scope_fingerprint_sha256": "b" * 64, "exit_code": 0, "duration_ms": 1,
            "review_cycle_id": request["review_cycle"]["cycle_id"],
            "review_round": request["review_cycle"]["rounds"][-1]["round_number"],
            "review_id": request["review_id"], "assignment_reference": request["assignment_reference"],
            "reviewer_role": request["reviewer_role"], "started_at": now, "finished_at": now,
            "invocation_method": "aily claude", "executable": "/fake/aily",
            "stdout_sha256": "c" * 64, "stderr_sha256": "d" * 64, "prompt_sha256": "e" * 64,
            "authorization_scope_sha256": "f" * 64,
            "authorization_verification_reference": "test-authorization-source",
            "snapshot_sha256": "1" * 64,
            "snapshot_file_paths": request["scope_paths"],
            "reviewer_contract_reference": request["reviewer_contract_reference"],
            "reviewer_contract_sha256": "2" * 64,
            "report_sha256": orchestrator.claude._sha256(orchestrator.claude._canonical_json(report)),
            "response_metadata": {"cost_usd": 0.0, "session_id": "claude-session"},
            "cost_usd": 0.0, "model": "claude-test", "usage": {"input_tokens": 10},
            "reviewer_identity": "Claude", "reviewer_identity_assurance": "declared",
            "reviewer_session_identity": "claude-session"}
        return {"status": "APPROVED", "failure_kind": None, "error": None,
            "envelope_valid": True, "report_valid": True, "report_received": True,
            "report": report, "process": process}

    def test_two_plan_reviews_and_explicit_approval_precede_any_increment(self):
        target, runner = self.plan_review_workflow("plan-review-approval")
        current_plan = plan()
        runner.initialize = lambda: None
        runner._planning = lambda _runtime: current_plan
        implemented = []
        runner._increment = lambda _plan, item, _runtime: implemented.append(item["increment_id"])
        runner._finalize_revision = lambda _plan: "content:" + "c" * 64
        runner._review_and_gate = lambda _plan: None
        calls = []

        def review(request, **_kwargs):
            calls.append(request["review_cycle"]["rounds"][-1]["round_number"])
            return self.successful_plan_review(request)

        ready = {"overall_result": "READY", "target_path": str(target.resolve())}
        with patch("safe_writer._run_preflight", return_value=ready), \
             patch("orchestrator.claude.run_claude_review", side_effect=review), \
             patch("orchestrator._runtime_bundle_files", return_value={}), \
             patch("orchestrator._runtime_bundle_unchanged", return_value=True):
            pending = runner.run()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(implemented, [])
        self.assertEqual(pending["status"], "WAITING_FOR_HUMAN")
        self.assertIn("plan_approval", pending)
        self.assertIn("Claude round 1", pending["plan_approval"]["proposal"])
        self.assertEqual(runner.state["orchestration"]["plan_review"]["review_round_count"], 2)

        with patch("safe_writer._run_preflight", return_value=ready):
            state = runner.store.read_state()
            receipt = orchestrator.record_decision_response(runner.store, state, "PLAN-APPROVAL",
                "APPROVE", "I approve the reviewed plan.", operator_identity="local account: test",
                source_reference="interactive local CLI")
        self.assertTrue(receipt["resolved"])
        runner.state = runner.store.read_state()
        runner._planning = lambda _runtime: runner._read_plan(
            runner.state["orchestration"]["plan_reference"])
        with patch("safe_writer._run_preflight", return_value=ready), \
             patch("orchestrator._runtime_bundle_files", return_value={}), \
             patch("orchestrator._runtime_bundle_unchanged", return_value=True):
            resumed = runner.run()
        self.assertEqual(implemented, ["inc-1"])
        self.assertEqual(resumed["status"], "ACTIVE")
        self.assertEqual(calls, [1, 2])

    def test_unavailable_claude_plan_review_blocks_without_claiming_independent_review(self):
        _target, runner = self.plan_review_workflow("plan-review-unavailable")
        runner.initialize = lambda: None
        runner._planning = lambda _runtime: plan()
        implemented = []
        runner._increment = lambda _plan, item, _runtime: implemented.append(item["increment_id"])
        def unavailable(_request, **_kwargs):
            return {"status": "BLOCKED", "failure_kind": "process_launch_failure",
                "error": "Aily executable unavailable", "report_valid": False,
                "envelope_valid": False, "report": None,
                "process": {"invocation_attempted_at": None, "exit_code": None}}
        with patch("safe_writer._run_preflight", return_value={"overall_result": "READY",
                "target_path": str(runner.target.resolve())}), \
             patch("orchestrator.claude.run_claude_review", side_effect=unavailable), \
             patch("orchestrator._runtime_bundle_files", return_value={}):
            result = runner.run()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(implemented, [])
        self.assertEqual(runner.state["orchestration"]["plan_review"]["status"], "blocked")
        self.assertIn("not independently reviewed", runner.state["blocker"]["reason"])

    def test_codex_dispositions_refine_plan_and_no_third_claude_round_runs(self):
        target, runner = self.plan_review_workflow("plan-review-findings")
        initial_plan = plan()
        runner.initialize = lambda: None
        runner._planning = lambda _runtime: initial_plan
        runner._increment = lambda *_args: self.fail("human approval is required before implementation")
        calls = []
        first_finding = {"id": "PLAN-1", "severity": "high",
            "rationale": "The proposed job state has no crash recovery transition.",
            "impact": "A worker restart could duplicate or lose an item.",
            "recommendation": "Persist item attempts and define resume behavior."}
        second_finding = {"id": "PLAN-2", "severity": "low",
            "rationale": "The plan adds a metrics exporter outside the assignment.",
            "impact": "It increases scope and adds an operational dependency.",
            "recommendation": "Drop the exporter from this prototype."}
        final_finding = {"id": "PLAN-3", "severity": "medium",
            "rationale": "The plan does not specify request validation failures.",
            "impact": "Malformed job payloads may have inconsistent API behavior.",
            "recommendation": "Define a stable validation error response."}

        def review(request, **_kwargs):
            round_number = request["review_cycle"]["rounds"][-1]["round_number"]
            calls.append((round_number, request["implementation_plan"]["summary"],
                copy.deepcopy(request.get("codex_finding_decisions", []))))
            if round_number == 1:
                report = {"decision": "CHANGES_REQUESTED", "summary": "Recovery is missing and telemetry is out of scope.",
                    "findings": [first_finding, second_finding]}
            else:
                report = {"decision": "CHANGES_REQUESTED", "summary": "The API error contract needs detail.",
                    "findings": [final_finding]}
            return self.successful_plan_review(request, report)

        refinement_calls = []
        def refine(stage, context, *_args, **_kwargs):
            self.assertEqual(stage, "PLAN_REFINEMENT")
            refinement_calls.append(context["review_round"])
            updated = copy.deepcopy(context["current_plan"])
            if context["review_round"] == 1:
                updated["summary"] = "Persist item attempts and resume failed jobs; keep prototype telemetry out of scope."
                dispositions = [
                    {"finding_id": "PLAN-1", "decision": "ACCEPT",
                     "rationale": "Crash recovery is required to prevent duplicate item processing.",
                     "plan_change": "Add persisted item attempts and resumable state transitions.",
                     "evidence_references": [context["current_plan_reference"]]},
                    {"finding_id": "PLAN-2", "decision": "REJECT",
                     "rationale": "The exporter is outside the requested job lifecycle and has no supporting requirement.",
                     "plan_change": None, "evidence_references": [context["current_plan_reference"]]},
                ]
            else:
                updated["summary"] += " Return a consistent validation error response for malformed job payloads."
                dispositions = [{"finding_id": "PLAN-3", "decision": "ACCEPT",
                    "rationale": "Stable request errors are part of the REST contract.",
                    "plan_change": "Add a validation error response contract to the API plan.",
                    "evidence_references": [context["current_plan_reference"]]}]
            return {"status": "COMPLETED", "summary": "Plan findings were dispositioned.",
                "plan": updated, "dispositions": dispositions}

        ready = {"overall_result": "READY", "target_path": str(target.resolve())}
        with patch("safe_writer._run_preflight", return_value=ready), \
             patch("orchestrator.claude.run_claude_review", side_effect=review), \
             patch.object(runner, "_run_codex", side_effect=refine), \
             patch("orchestrator._runtime_bundle_files", return_value={}), \
             patch("orchestrator._runtime_bundle_unchanged", return_value=True):
            result = runner.run()
        self.assertEqual([item[0] for item in calls], [1, 2])
        self.assertTrue(calls[1][1].startswith("Persist item attempts"))
        self.assertEqual(calls[1][2][0]["finding_id"], "PLAN-1")
        self.assertEqual(calls[1][2][0]["decision"], "ACCEPT")
        self.assertEqual(refinement_calls, [1, 2])
        final_plan = runner._read_plan(runner.state["orchestration"]["plan_reference"])
        self.assertIn("consistent validation error response", final_plan["summary"])
        self.assertEqual(result["status"], "WAITING_FOR_HUMAN")
        self.assertEqual(runner.state["orchestration"]["plan_review"]["review_round_count"], 2)
        self.assertIn("not sent for a third independent review", result["plan_approval"]["proposal"])
        self.assertIn("Codex REJECT", result["plan_approval"]["proposal"])

    def process_evidence_fixture(self, name):
        target = Path(self.temp.name) / name
        git_fixture(target)
        (target / "src").mkdir()
        (target / "src/feature.py").write_text("value = 1\n", encoding="utf-8")
        (target / "docs").mkdir()
        workflow_id = f"flow-{name}"
        runner = orchestrator.Orchestrator(str(target), "Add a feature",
            config(authorized_artifact_paths=["docs"]), workflow_id,
            process_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"ok", b""))
        runner.initialize()
        (target / orchestrator.PROCESS_EVIDENCE_PATH).write_text("Prior process notes.\n", encoding="utf-8")
        runner.state["scope"].update({"increment_id": "inc-1", "included_paths": ["src", "docs"],
            "code_paths": ["src"], "artifact_paths": ["docs"], "revision_paths": ["src", "docs"]})
        runner.state["orchestration"]["increments"] = [{"increment_id": "inc-1", "changed_paths": []}]
        runner._save()
        increment = {"increment_id": "inc-1", "title": "Implement feature", "code_paths": ["src"],
            "artifact_paths": ["docs"], "revision_paths": ["src", "docs"],
            "acceptance_criteria_ids": ["AC-1"], "validation_ids": ["tests"],
            "blocked_by_decision": False}
        return target, runner, plan(increments=[increment]), increment

    def test_process_evidence_refresh_archives_prior_narrative_and_keeps_review_revision_stable(self):
        target, runner, current_plan, increment = self.process_evidence_fixture("process-evidence-controller-owned")
        before_revision, _, _ = runner._revision_manifest(increment)
        original = (target / orchestrator.PROCESS_EVIDENCE_PATH).read_text(encoding="utf-8")
        ready = {"overall_result": "READY", "target_path": str(target.resolve())}

        with patch("safe_writer._run_preflight", return_value=ready):
            runner._sync_process_evidence(increment)
            refreshed = (target / orchestrator.PROCESS_EVIDENCE_PATH).read_text(encoding="utf-8")
            archive_ref = runner.state["orchestration"]["process_evidence_archive"]["artifact_reference"]
            archived = runner.store.read_json(archive_ref)
            runner._sync_process_evidence(increment)

        after_revision, _, _ = runner._revision_manifest(increment)
        self.assertEqual(before_revision, after_revision)
        self.assertIn("Controller-managed workflow evidence", refreshed)
        self.assertNotIn("Prior process notes.", refreshed)
        self.assertEqual(archived["content_utf8"], original)
        self.assertEqual(archived["sha256"], orchestrator._sha(original.encode("utf-8")))
        self.assertEqual(runner.state["artifact_references"].count(archive_ref), 1)
        self.assertIn(archive_ref, (target / orchestrator.PROCESS_EVIDENCE_PATH).read_text(encoding="utf-8"))

    @staticmethod
    def self_review_response(decision="PASS", *, findings=None, changes=None, summary=None):
        return {"decision": decision, "summary": summary or f"Self-review {decision.lower()}.",
            "findings": findings or [], "changes": changes or []}

    @staticmethod
    def record_mock_self_review_attempt(runner, result):
        now = orchestrator._now()
        trace = {"result_status": result["decision"], "result_summary": result["summary"],
            "finding_count": len(result["findings"]), "changed_paths": [row["path"] for row in result["changes"]],
            "changed_content_sha256": [orchestrator._sha(row["content"].encode()) for row in result["changes"]]}
        runner.state["orchestration"]["attempts"].append({"stage": "SELF_REVIEW",
            "schema": "orchestrator-self-review.schema.json", "started_at": now, "finished_at": now,
            "exit_code": 0, "duration_ms": 1, "result_sha256": orchestrator._sha(orchestrator._json_bytes(result)),
            "blocker_sha256": None, "trace": trace})

    @staticmethod
    def write_state(runner, status="ACTIVE", stage="PLANNING", pending=None, attempts=None):
        runner.state = {
            "workflow_id": runner.workflow_id, "status": status, "phase": stage,
            "pending_decisions": pending or [], "artifact_references": [],
            "orchestration": {
                "current_stage": "PLANNING", "plan_reference": None, "task_sha256": "a" * 64,
                "config_sha256": "b" * 64,
                "branch": "feature/task", "max_iterations": 2, "iteration": 0,
                "increments": [], "attempts": attempts or [], "validation_results": [],
                "review_results": [], "correction_status": [], "preexisting_hashes": [],
                "commit_authorization_fingerprint": None,
            },
        }

    def test_commit_configuration_is_rejected_without_policy_specific_authorization(self):
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "exact staged diff"):
            orchestrator.validate_config(config(commit=True))

    def test_configuration_rejects_credentials_in_validation_arguments(self):
        unsafe = config(validations=[{"id": "tests", "argv": ["python3", "test.py", "--api-key=do-not-store"]}])
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "credentials"):
            orchestrator.validate_config(unsafe)

    def test_persistent_claude_authorization_requires_explicit_read_only_bounded_scope(self):
        auth = persistent_claude_authorization(self.target)
        accepted = orchestrator.validate_config(config(review={"enabled": True, "authorization": auth}))
        self.assertEqual(accepted["review"]["authorization"]["authorization_id"], auth["authorization_id"])
        for change, message in [
            ({"provider": "other"}, "provider aily"),
            ({"mode": "write"}, "read_only"),
            ({"sensitive_exclusions": ["credentials"]}, "must explicitly exclude"),
            ({"permitted_repositories": ["relative/repo"]}, "absolute repository paths"),
        ]:
            invalid = persistent_claude_authorization(self.target, **change)
            with self.subTest(change=change), self.assertRaisesRegex(orchestrator.OrchestratorError, message):
                orchestrator.validate_config(config(review={"enabled": True, "authorization": invalid}))

    def test_authorization_only_config_update_does_not_require_requirement_replanning(self):
        old_config = self.runner.config
        auth = persistent_claude_authorization(self.target)
        self.runner.config = orchestrator.validate_config(config(review={"enabled": True, "authorization": auth}))
        self.assertTrue(self.runner._is_authorization_only_config_change({
            "config_sha256": orchestrator._sha(orchestrator._json_bytes(old_config))}))

    def test_review_budget_and_authorization_update_preserves_existing_plan_scope(self):
        old_auth = persistent_claude_authorization(self.target, max_budget_usd=0.01)
        old_config = orchestrator.validate_config(config(
            claude_budget_usd=0.01, review={"enabled": True, "authorization": old_auth}))
        new_auth = copy.deepcopy(old_auth)
        new_auth["max_budget_usd"] = 2.0
        new_auth["authorization_reference"] = "conversation: explicit $2 review authorization"
        self.runner.config = orchestrator.validate_config(config(
            claude_budget_usd=2.0, review={"enabled": True, "authorization": new_auth}))

        self.assertTrue(self.runner._is_authorization_only_config_change({"config_snapshot": old_config}))

        planning_change = copy.deepcopy(self.runner.config)
        planning_change["authorized_code_paths"] = ["src", "lib"]
        self.runner.config = orchestrator.validate_config(planning_change)
        self.assertFalse(self.runner._is_authorization_only_config_change({"config_snapshot": old_config}))

    def test_persisted_authorization_resolves_transfer_pause_without_authenticating_identity(self):
        old_config = self.runner.config
        auth = persistent_claude_authorization(self.target)
        self.runner.config = orchestrator.validate_config(config(review={"enabled": True, "authorization": auth}))
        normalized_auth = self.runner.config["review"]["authorization"]
        requirement_reference = f"{self.runner.store.directory}/requirements-v1.json"
        requirement_task = "Add a feature"
        requirement_sha = orchestrator._sha(requirement_task.encode())
        requirement_path = self.target / requirement_reference
        requirement_path.parent.mkdir(parents=True, exist_ok=True)
        requirement_path.write_text(json.dumps({"workflow_id": self.flow, "version": 1,
            "task": requirement_task, "task_sha256": requirement_sha,
            "configuration_sha256": orchestrator._sha(orchestrator._json_bytes(old_config))}), encoding="utf-8")
        decision = {"decision_id": "CLAUDE-AUTH-inc-1", "decision_category": "external_data_transfer_authorization",
            "status": "open", "response_history": [], "interactive_resolution_allowed": False}
        state = {"status": "WAITING_FOR_HUMAN", "pending_decisions": [decision], "orchestration": {
            "config_snapshot": old_config, "config_sha256": orchestrator._sha(orchestrator._json_bytes(old_config)),
            "task_sha256": requirement_sha, "current_requirement_version": 1,
            "requirement_history": [{"version": 1, "task_sha256": requirement_sha,
                "task_reference": requirement_reference, "status": "current"}],
            "claude_authorization": None, "claude_authorization_history": [], "claude_authorization_uses": []}}
        self.runner._apply_claude_authorization_config_change(state)
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["orchestration"]["claude_authorization"], normalized_auth)
        self.assertEqual(state["pending_decisions"][0]["status"], "resolved")
        self.assertEqual(state["pending_decisions"][0]["resolved_by_identity_assurance"], "declared")
        self.assertEqual(state["pending_decisions"][0]["decision_category"], "external_data_transfer_authorization")
        self.assertEqual(state["pending_decisions"][0]["response_history"][0]["source"], "workflow_configuration")
        self.assertEqual(state["orchestration"]["claude_authorization_history"][-1]["action"], "configured")
        self.assertEqual(self.runner._validate_current_requirement_snapshot(state["orchestration"])["task"], requirement_task)

    def test_persistent_claude_authorization_enforces_repo_paths_and_budget_log(self):
        auth = persistent_claude_authorization(self.target)
        self.runner.state = {"orchestration": {"claude_authorization": auth, "claude_authorization_uses": []}}
        accepted, remaining, error = self.runner._persistent_claude_authorization(["src/module.cs"])
        self.assertIsNone(error)
        self.assertEqual(accepted["authorization_id"], auth["authorization_id"])
        self.assertEqual(remaining, 2.0)
        self.assertEqual(self.runner._claude_total_budget(auth), 2.0)
        _auth, _remaining, denied = self.runner._persistent_claude_authorization(["private/other.txt"])
        self.assertIn("outside the persistent Claude authorization", denied)
        self.runner.state["orchestration"]["claude_authorization_uses"].append({
            "authorization_id": auth["authorization_id"], "status": "REVIEW_COMPLETED",
            "invocation_attempted": True, "cost_usd": 0.334493})
        _auth, remaining, error = self.runner._persistent_claude_authorization(["src/module.cs"])
        self.assertIsNone(error)
        self.assertAlmostEqual(remaining, 1.665507)
        self.assertEqual(self.runner._claude_total_budget(auth), 2.0)
        self.runner.state["orchestration"]["claude_authorization_uses"][0]["cost_usd"] = None
        _auth, _remaining, denied = self.runner._persistent_claude_authorization(["src/module.cs"])
        self.assertIn("unknown cost", denied)

    def test_claude_authorization_use_ids_are_unique_per_round_and_attempt(self):
        first = orchestrator._claude_authorization_use_id("review-inc-1-1", 1, 1, set())
        retry = orchestrator._claude_authorization_use_id("review-inc-1-1", 1, 2, {first})
        second_round = orchestrator._claude_authorization_use_id("review-inc-1-2", 2, 1, {first, retry})
        duplicate_reservation = orchestrator._claude_authorization_use_id(
            "review-inc-1-1", 1, 1, {first, retry, second_round})
        self.assertEqual(len({first, retry, second_round, duplicate_reservation}), 4)

    def test_legacy_duplicate_reservation_and_missing_attempt_cost_reconcile_append_only(self):
        authorization = persistent_claude_authorization(
            self.target, authorization_id="AUTH-AILY-20261109", max_budget_usd=2.0)
        self.runner.config = orchestrator.validate_config(config(
            claude_budget_usd=2.0, review={"enabled": True, "authorization": authorization}))
        cycle_id = "cycle-ohpen"
        review_id = "review-inc-1-1"
        revision = "content:" + "a" * 64
        completed_use = {
            "use_id": f"{review_id}-use", "authorization_id": authorization["authorization_id"],
            "review_id": review_id, "review_cycle_id": cycle_id, "increment_id": "inc-1",
            "revision_id": revision, "permitted_paths": ["src/feature.py"], "mode": "read_only",
            "budget_limit_usd": 2.0, "invocation_attempted": True, "status": "REVIEW_COMPLETED",
            "started_at": orchestrator._now(), "finished_at": orchestrator._now(),
            "invocation_id": None, "session_identity": None, "duration_ms": 10, "model": None,
            "usage": {}, "cost_usd": 0.3821072,
        }
        stale_reservation = copy.deepcopy(completed_use)
        stale_reservation.update({"invocation_attempted": False, "status": "reserved",
            "finished_at": None, "cost_usd": None, "usage": None, "duration_ms": None})
        self.runner.state = {
            "claude_review_cycles": [{"cycle_id": cycle_id, "increment_id": "inc-1", "round_count": 1,
                "rounds": [{"round_number": 1, "invocation_attempts": [
                    {"attempt_number": 1, "invocation_attempted_at": orchestrator._now(),
                     "target_revision_id": revision, "response_metadata": {"cost_usd": 0.334493}},
                    {"attempt_number": 2, "invocation_attempted_at": orchestrator._now(),
                     "target_revision_id": revision, "response_metadata": {"cost_usd": 0.3821072}},
                ]}]}],
            "orchestration": {
                "claude_authorization": authorization,
                "claude_authorization_uses": [completed_use, stale_reservation],
                "review_results": [
                    {"increment_id": "inc-1", "revision_id": revision, "cost_usd": 0.334493},
                    {"increment_id": "inc-1", "revision_id": revision, "cost_usd": 0.3821072},
                ],
            },
        }
        self.runner._save = lambda: None
        original_uses = copy.deepcopy(self.runner.state["orchestration"]["claude_authorization_uses"])

        _auth, remaining, error = self.runner._persistent_claude_authorization(["src/feature.py"])
        self.assertIsNone(error)
        self.assertAlmostEqual(remaining, 1.2833998)
        self.assertEqual(self.runner.state["orchestration"]["claude_authorization_uses"], original_uses)
        events = self.runner.state["orchestration"]["claude_authorization_use_reconciliations"]
        self.assertEqual({event["action"] for event in events},
                         {"reservation_released", "invocation_cost_reconciled"})
        self.assertAlmostEqual(sum(event["cost_usd"] or 0 for event in events), 0.334493)
        historical_invocation = next(event for event in events if event["action"] == "invocation_cost_reconciled")
        self.assertTrue(historical_invocation["use_id"].startswith("legacy-use-"))

        _auth, remaining_again, error_again = self.runner._persistent_claude_authorization(["src/feature.py"])
        self.assertIsNone(error_again)
        self.assertAlmostEqual(remaining_again, remaining)
        self.assertEqual(len(self.runner.state["orchestration"]["claude_authorization_use_reconciliations"]), 2)

    def test_validation_command_policy_rejects_shells_and_implicit_restore(self):
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "prohibited"):
            orchestrator._run_command(["bash", "-c", "echo unsafe"], self.target, 1,
                                      lambda *args, **kwargs: self.fail("must not execute"))
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "Inline-code"):
            orchestrator._run_command(["python3", "-c", "import socket"], self.target, 1,
                                      lambda *args, **kwargs: self.fail("must not execute"))
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "--no-restore"):
            orchestrator._run_command(["dotnet", "test"], self.target, 1,
                                      lambda *args, **kwargs: self.fail("must not execute"))

    def test_workspace_command_policy_runs_package_install_and_local_git_reads(self):
        observed = []
        def run(argv, **kwargs):
            observed.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, b"installed", b"")

        with patch.dict(os.environ, {"WORKFLOW_TEST_SECRET": "must-not-pass",
                                    "PIP_INDEX_URL": "https://user:password@example.invalid/simple",
                                    "npm_config_registry": "https://registry.example.invalid"}, clear=False):
            installed = orchestrator._run_command(["python3", "-m", "pip", "install", "example-package"],
                                                   self.target, 5, run)
        git_read = orchestrator._run_command(["git", "status", "--short"], self.target, 5, run)
        staged = orchestrator._run_command(["git", "add", "--", "src/feature.py"], self.target, 5, run,
                                           stageable_paths={"src/feature.py"})
        self.assertEqual(installed["exit_code"], 0)
        self.assertEqual(git_read["exit_code"], 0)
        self.assertEqual(staged["exit_code"], 0)
        self.assertEqual(observed[2][0], ["git", "add", "--", "src/feature.py"])
        package_env = observed[0][1]["env"]
        self.assertNotIn("WORKFLOW_TEST_SECRET", package_env)
        self.assertNotIn("PIP_NO_INDEX", package_env)
        self.assertNotIn("npm_config_offline", package_env)
        self.assertNotIn("PIP_INDEX_URL", package_env)
        self.assertNotIn("npm_config_registry", package_env)

    def test_workspace_command_policy_keeps_remote_and_paid_actions_protected(self):
        for command in (["git", "push", "origin", "main"], ["pip", "install", "--index-url", "https://example.invalid/simple"],
                        ["python3", "-m", "pip", "install", "--index-url=https://example.invalid/simple"],
                        ["python", "-m", "pip", "install", "git+https://example.invalid/project.git"],
                        ["git", "commit", "-m", "unauthorized"], ["git", "merge", "feature"],
                        ["git", "branch", "new-branch"], ["git", "add", "."],
                        ["pip", "install", "file:///private/tmp/example.whl"],
                        ["git", "add", "--", "../unrelated/file.py"],
                        ["git", "add", "--", "credentials/token.txt"], ["git-upload-pack", "https://example.invalid/repo"],
                        ["env", "curl", "https://example.invalid"], ["sudo", "pip", "install", "example-package"],
                        ["rm", "--", "README.md"], ["rmdir", "--", "src"],
                        ["unlink", "--", "README.md"], ["truncate", "-s", "0", "README.md"],
                        ["git", "-C", "../unrelated", "status"],
                        ["python3", "-m", "pytest", "../unrelated/test_external.py"],
                        ["curl", "https://example.invalid"]):
            with self.subTest(command=command), self.assertRaises(orchestrator.OrchestratorError):
                orchestrator._run_command(command, self.target, 5,
                    lambda *args, **kwargs: self.fail("protected command must not execute"))
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "pre-existing or out-of-increment"):
            orchestrator._run_command(["git", "add", "--", "notes.txt"], self.target, 5,
                lambda *args, **kwargs: self.fail("pre-existing path must not be staged"),
                stageable_paths={"src/feature.py"})

    def test_revision_canonical_json_sorts_keys_by_utf16_code_units(self):
        value = {"\ue000": 1, "\U00010000": 2}
        self.assertEqual(orchestrator._jcs(value), '{"𐀀":2,"":1}'.encode("utf-8"))

    def test_correction_limit_blocks_before_another_codex_call(self):
        self.write_state(self.runner)
        self.runner.state["orchestration"].update({"iteration": 2, "max_iterations": 2})
        self.runner._save = lambda: None
        self.runner._run_codex = lambda *args, **kwargs: self.fail("iteration limit should stop first")
        result = self.runner._correct_and_reverify(plan(), plan()["increments"][0],
            {"findings": [{"id": "CF-1"}]}, {"round_count": 1})
        self.assertIsNone(result)
        self.assertEqual(self.runner.state["status"], "BLOCKED")
        self.assertEqual(self.runner.state["orchestration"]["current_stage"], "CORRECTION")
        self.assertEqual(self.runner.state["phase"], "CORRECTION")

    def test_blocker_fingerprint_ignores_recorded_time_but_binds_reason(self):
        first = {"reason": "same issue", "affected_phase": "TESTING", "recorded_at": "t1"}
        second = {"reason": "same issue", "affected_phase": "TESTING", "recorded_at": "t2"}
        different = {"reason": "different issue", "affected_phase": "TESTING", "recorded_at": "t2"}
        self.assertEqual(orchestrator._blocker_fingerprint(first), orchestrator._blocker_fingerprint(second))
        self.assertNotEqual(orchestrator._blocker_fingerprint(first), orchestrator._blocker_fingerprint(different))

    def test_plan_requires_scoped_paths_and_configured_validation(self):
        bad = plan()
        bad["increments"][0]["code_paths"] = ["../outside"]
        with self.assertRaises(orchestrator.OrchestratorError):
            orchestrator._plan_valid(bad, orchestrator.validate_config(config()))
        bad = plan()
        bad["increments"][0]["validation_ids"] = ["unknown"]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "unconfigured validation"):
            orchestrator._plan_valid(bad, orchestrator.validate_config(config()))

    def test_plan_validation_preserves_unique_items_after_provider_schema_simplification(self):
        bad = plan()
        bad["increments"][0]["code_paths"] = ["src", "src"]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "code_paths values must be unique"):
            orchestrator._plan_valid(bad, orchestrator.validate_config(config()))
        bad = plan()
        bad["increments"][0]["validation_ids"] = ["tests", "tests"]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "validation_ids values must be unique"):
            orchestrator._plan_valid(bad, orchestrator.validate_config(config()))

    def test_engineering_discovery_requires_scenarios_and_documents_assumptions(self):
        bad = plan()
        bad["engineering_discovery"]["scenarios"] = []
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "at least one relevant scenario"):
            orchestrator._plan_valid(bad, orchestrator.validate_config(config()), require_discovery=True)

        candidate = plan()
        candidate["engineering_discovery"]["assumptions"] = [{"id": "ASM-1",
            "description": "Use the repository's existing test runner", "rationale": "No new dependency is needed",
            "impact_if_wrong": "Validation command may need adjustment", "reversible": True,
            "requires_confirmation": False}]
        candidate["engineering_discovery"]["decisions"] = [{"id": "DEC-1", "category": "engineering",
            "decision": "Reuse the existing test layout", "rationale": "It is already established",
            "alternatives": ["Add a new test framework"], "requires_human_input": False}]
        self.assertEqual(orchestrator._plan_valid(candidate, orchestrator.validate_config(config()),
            require_discovery=True)["engineering_discovery"]["assumptions"][0]["id"], "ASM-1")

        unresolved = plan()
        unresolved["engineering_discovery"]["assumptions"] = [{"id": "ASM-2",
            "description": "Retain job data indefinitely", "rationale": "Simplifies support",
            "impact_if_wrong": "Could violate retention obligations", "reversible": False,
            "requires_confirmation": True}]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "discovery still requires human input"):
            orchestrator._plan_valid(unresolved, orchestrator.validate_config(config()), require_discovery=True)

    def test_routine_engineering_decision_cannot_be_made_a_human_approval_gate(self):
        candidate = plan()
        candidate["engineering_discovery"]["decisions"] = [{"id": "DEC-1", "category": "engineering",
            "decision": "Use the existing runner", "rationale": "Local convention", "alternatives": [],
            "requires_human_input": True}]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "Routine engineering decisions"):
            orchestrator._plan_valid(candidate, orchestrator.validate_config(config()), require_discovery=True)

    def test_plan_can_defer_dependent_increment_while_unblocked_increment_proceeds(self):
        increments = [
            {**plan()["increments"][0], "increment_id": "inc-independent", "blocked_by_decision": False},
            {**plan()["increments"][0], "increment_id": "inc-dependent", "blocked_by_decision": True},
        ]
        candidate = plan(increments=increments)
        candidate["decision"] = "NEEDS_HUMAN_INPUT"
        candidate["decision_context"] = {"category": "functional_decision",
            "question": "Should clients be able to cancel a running job?",
            "proposal": "Keep cancellation out of scope for this release.",
            "alternatives": ["Add cancellation", "Defer cancellation"],
            "reason": "The API capability changes client behavior and processing guarantees.",
            "policy_or_skill_reference": None, "required_authority": "product owner",
            "interactive_resolution_allowed": False}
        validated = orchestrator._plan_valid(candidate, orchestrator.validate_config(config()), require_discovery=True)
        self.assertEqual([row["blocked_by_decision"] for row in validated["increments"]], [False, True])

    def test_provider_output_schemas_omit_unsupported_strict_schema_keywords(self):
        unsupported = {'"uniqueItems"', '"minItems"', '"minLength"', '"pattern"', '"allOf"', '"if"', '"then"'}
        for name in ("orchestrator-plan.schema.json", "orchestrator-stage-result.schema.json",
                     "orchestrator-self-review.schema.json"):
            encoded = json.dumps(json.loads((ROOT / "harness" / "schemas" / name).read_text()))
            for keyword in unsupported:
                self.assertNotIn(keyword, encoded, f"{keyword} is unsupported by the provider's strict schema")

    def test_plan_scope_is_review_guidance_inside_workspace_not_a_second_approval_list(self):
        value = plan()
        value["increments"][0]["code_paths"] = ["outside"]
        value["increments"][0]["revision_paths"] = ["outside"]
        validated = orchestrator._plan_valid(value, orchestrator.validate_config(config()))
        self.assertEqual(validated["increments"][0]["code_paths"], ["outside"])

    def test_failed_codex_stage_preserves_redacted_process_diagnostic(self):
        self.write_state(self.runner)
        self.runner._save = lambda: None
        secret = "Bearer abcdefghijklmnop123456"
        self.runner.process_runner = lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, b'{"type":"turn.failed","error":{"message":"provider connection failed"}}',
            f"Operation not permitted: {secret}".encode()
        )

        with patch.object(orchestrator.shutil, "which", return_value=sys.executable):
            with self.assertRaises(orchestrator.OrchestratorError) as raised:
                self.runner._run_codex("PLANNING", {}, Path(self.temp.name), "orchestrator-plan.schema.json")

        self.assertIn("Operation not permitted", str(raised.exception))
        self.assertIn("provider connection failed", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(self.runner.state["orchestration"]["attempts"][-1]["exit_code"], 1)

    def test_unavailable_codex_executable_is_recorded_as_a_stage_attempt(self):
        self.write_state(self.runner)
        self.runner._save = lambda: None
        with patch.object(orchestrator.shutil, "which", return_value=None), \
             patch.object(self.runner, "_codex_runtime_metadata", return_value={
                 "cli_version": "unavailable", "model": "unavailable", "provider": "unavailable",
                 "python_runtime": "test",
             }):
            with self.assertRaisesRegex(orchestrator.OrchestratorError, "Codex executable is unavailable"):
                self.runner._run_codex("PLANNING", {}, Path(self.temp.name), "orchestrator-plan.schema.json")
        attempt = self.runner.state["orchestration"]["attempts"][-1]
        self.assertEqual(attempt["exit_code"], 127)
        self.assertEqual(attempt["trace"]["result_status"], "process_not_started")
        self.assertEqual(attempt["trace"]["diagnostic"], "Codex executable is unavailable: codex")
        self.assertRegex(attempt["trace"]["prompt_sha256"], r"^[a-f0-9]{64}$")

    def test_skill_trace_distinguishes_available_requested_and_runtime_verified_reads(self):
        skill_dir = self.target / ".agents" / "skills" / "api-patterns"
        references = skill_dir / "references"
        references.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        reference_file = references / "graphql.md"
        skill_file.write_text("---\nname: api-patterns\n---\nDo not persist this content.\n", encoding="utf-8")
        reference_file.write_text("Reference content must not be persisted.\n", encoding="utf-8")
        events = "\n".join([
            json.dumps({"type": "item.completed", "item": {"id": "read-skill", "type": "command_execution",
                "command": f"cat {skill_file}", "status": "completed", "exit_code": 0,
                "aggregated_output": "---\\nname: api-patterns\\n---\\nDo not persist this content.\\n"}}),
            json.dumps({"type": "item.completed", "item": {"id": "read-reference", "type": "command_execution",
                "command": f"sed -n '1,2p' {reference_file}", "status": "completed", "exit_code": 0,
                "aggregated_output": "Reference content must not be persisted.\\n"}}),
        ])
        context = {"requested_sources": [str(skill_file)]}
        with patch.object(orchestrator, "_skill_roots", return_value=[self.target / ".agents" / "skills"]):
            trace = orchestrator._skill_trace("IMPLEMENTATION", self.target, context, events)
        self.assertEqual(trace["available"][0]["name"], "api-patterns")
        self.assertEqual(trace["requested"][0]["skill_md_path"], str(skill_file.resolve()))
        self.assertEqual(trace["read_verified"][0]["stage"], "IMPLEMENTATION")
        self.assertEqual(trace["read_verified"][0]["runtime_event"]["reference_path"], str(skill_file.resolve()))
        self.assertEqual(trace["references_consulted"][0]["reference_path"], str(reference_file.resolve()))
        serialized = json.dumps(trace)
        self.assertNotIn("Do not persist this content", serialized)
        self.assertNotIn("Reference content must not be persisted", serialized)

    def test_requested_skill_without_a_matching_runtime_read_is_unverifiable(self):
        skill_dir = self.target / ".agents" / "skills" / "api-patterns"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("---\nname: api-patterns\n---\n", encoding="utf-8")
        failed_read = json.dumps({"type": "item.completed", "item": {"id": "failed-read",
            "type": "command_execution", "command": f"cat {skill_file}", "status": "completed",
            "exit_code": 1, "aggregated_output": "partial content"}})
        with patch.object(orchestrator, "_skill_roots", return_value=[self.target / ".agents" / "skills"]):
            trace = orchestrator._skill_trace("SELF_REVIEW", self.target,
                {"requested_sources": [str(skill_file)]}, failed_read)
        self.assertEqual(trace["read_verified"], [])
        self.assertEqual(trace["read_unverifiable"][0]["reason"], "no_successful_matching_skill_md_read_event")
        self.assertEqual(trace["read_unverifiable"][0]["stage"], "SELF_REVIEW")

    def test_codex_stage_disables_mcp_and_plugins_without_changing_auth_config(self):
        self.write_state(self.runner)
        self.runner._save = lambda: None
        skill_dir = self.target / ".agents" / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("---\nname: test-skill\n---\nprivate skill text\n", encoding="utf-8")
        commands = []
        config_home = Path(self.temp.name) / "codex-home"
        config_home.mkdir()
        (config_home / "config.toml").write_text(
            '[mcp_servers.jira]\nenabled = true\nurl = "https://example.invalid/mcp"\n',
            encoding="utf-8",
        )

        def process_runner(cmd, **kwargs):
            commands.append(cmd)
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text('{"decision":"READY"}', encoding="utf-8")
            event = {"type": "item.completed", "item": {"id": "skill-read-event",
                "type": "command_execution", "command": f"cat {skill_file}", "status": "completed",
                "exit_code": 0, "aggregated_output": "private skill text"}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(event).encode(), b"")

        self.runner.process_runner = process_runner
        with patch.object(orchestrator.shutil, "which", return_value=sys.executable), \
                patch.dict(os.environ, {"CODEX_HOME": str(config_home)}), \
                patch.object(orchestrator, "_skill_roots", return_value=[self.target / ".agents" / "skills"]):
            result = self.runner._run_codex("PLANNING", {"requested_sources": [str(skill_file)]},
                Path(self.temp.name), "orchestrator-plan.schema.json")

        self.assertEqual(result["decision"], "READY")
        self.assertIn(["--disable", "plugins"], [commands[0][index:index + 2]
                                                    for index in range(len(commands[0]) - 1)])
        self.assertIn(["-c", "mcp_servers={}"], [commands[0][index:index + 2]
                                                   for index in range(len(commands[0]) - 1)])
        self.assertIn(["-c", "mcp_servers.jira.enabled=false"], [commands[0][index:index + 2]
                                                                   for index in range(len(commands[0]) - 1)])
        self.assertNotIn("--ignore-user-config", commands[0])
        trace = self.runner.state["orchestration"]["attempts"][-1]["trace"]
        self.assertRegex(trace["prompt_sha256"], r"^[a-f0-9]{64}$")
        self.assertRegex(trace["requirement_sha256"], r"^[a-f0-9]{64}$")
        self.assertRegex(trace["workflow_config_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(trace["permissions"]["sandbox"], "read-only")
        self.assertEqual(trace["capabilities"]["read_evidence"], "unavailable_without_codex_tool_event_log")
        self.assertEqual(trace["result_status"], "READY")
        self.assertEqual(trace["skills"]["read_verified"][0]["name"], "test-skill")
        self.assertEqual(trace["skills"]["read_verified"][0]["stage"], "PLANNING")
        self.assertNotIn("private skill text", json.dumps(trace))

    def test_plan_may_retain_controller_owned_workflow_artifacts(self):
        stored = plan()
        stored["increments"][0]["artifact_paths"] = [".engineering/workflows/flow-test-1"]
        validated = orchestrator._plan_valid(stored, orchestrator.validate_config(config()),
            controller_artifact_paths=[".engineering/workflows/flow-test-1"])
        self.assertEqual(validated["increments"][0]["artifact_paths"],
                         [".engineering/workflows/flow-test-1"])

    def test_workspace_policy_allows_relevant_project_files_regardless_of_extension_list(self):
        self.runner.config["code_file_extensions"] = [".cs"]
        self.runner.state = {"orchestration": {"preexisting_hashes": [], "increments": [],
            "workflow_created_paths": []}, "scope": {}}
        self.runner._save = lambda: None
        increment = {"increment_id": "inc-1", "code_paths": ["src"], "artifact_paths": [],
            "revision_paths": ["src"]}
        with patch.object(self.runner, "_assert_active_worktree"), \
                patch.object(orchestrator, "create_file", return_value={"operation": "create"}) as create:
            self.runner._apply_changes([{"path": "src/README.md", "content": "relevant\n"}], increment)
        create.assert_called_once()
        self.assertTrue(orchestrator._contained("src/README.md", increment["revision_paths"]))

    def test_stage_response_rejects_unknown_fields_and_bad_correction_decisions(self):
        value = {"status": "COMPLETED", "summary": "Done", "changes": [], "extra": True}
        with self.assertRaises(orchestrator.OrchestratorError):
            orchestrator._stage_valid(value)
        value = {"status": "COMPLETED", "summary": "Done", "changes": [], "finding_decisions": [
            {"finding_id": "F-1", "decision": "APPROVE", "reason": "ok", "evidence_references": []}
        ]}
        with self.assertRaises(orchestrator.OrchestratorError):
            orchestrator._stage_valid(value, correction=True)

    def test_implementation_stage_requires_empty_finding_decisions(self):
        value = {"status": "COMPLETED", "summary": "Done", "changes": [], "finding_decisions": []}
        self.assertEqual(orchestrator._stage_valid(value)["finding_decisions"], [])
        value["finding_decisions"] = [{"finding_id": "F-1"}]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "Only correction stages"):
            orchestrator._stage_valid(value)

    def test_implementation_cannot_modify_a_preexisting_user_change(self):
        existing = self.target / "src" / "user.txt"
        existing.write_text("keep this\n", encoding="utf-8")
        self.runner.state = {"orchestration": {"preexisting_hashes": [{"path": "src/user.txt"}]}}
        increment = {"code_paths": ["src"], "artifact_paths": []}
        with patch.object(self.runner, "_assert_active_worktree"):
            with self.assertRaisesRegex(orchestrator.OrchestratorError, "pre-existing user change"):
                self.runner._apply_changes([{"path": "src/user.txt", "content": "replace\n"}], increment)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep this\n")

    def test_new_parent_directory_is_created_inside_workspace_policy(self):
        self.runner.state = {"orchestration": {"preexisting_hashes": [], "increments": [],
            "workflow_created_paths": []}, "scope": {}}
        increment = {"code_paths": ["src/new/file.cs"], "artifact_paths": []}
        ready = {"overall_result": "READY", "target_path": str(self.target.resolve())}
        with patch.object(self.runner, "_assert_active_worktree"), \
                patch("safe_writer._run_preflight", return_value=ready):
            self.runner._apply_changes([{"path": "src/new/file.cs", "content": "class C {}\n"}], increment)
        self.assertTrue((self.target / "src" / "new/file.cs").exists())

    def test_workflow_created_files_can_be_deleted_or_renamed_without_touching_user_files(self):
        source = self.target / "src/generated.txt"
        source.write_text("generated\n", encoding="utf-8")
        user_file = self.target / "src/user.txt"
        user_file.write_text("user data\n", encoding="utf-8")
        self.runner.state = {"orchestration": {"preexisting_hashes": [], "increments": [],
                "workflow_created_paths": ["src/generated.txt"]}, "scope": {}}
        self.runner._save = lambda: None
        increment = {"increment_id": "inc-1", "code_paths": ["src"], "artifact_paths": [],
            "revision_paths": ["src"]}
        ready = {"overall_result": "READY", "target_path": str(self.target.resolve())}
        with patch.object(self.runner, "_assert_active_worktree"), \
                patch("safe_writer._run_preflight", return_value=ready):
            self.runner._apply_changes([{"path": "src/generated.txt", "operation": "rename",
                "destination": "src/generated-renamed.txt"}], increment)
            self.runner._apply_changes([{"path": "src/generated-renamed.txt", "operation": "delete"}], increment)
            with self.assertRaisesRegex(orchestrator.OrchestratorError, "Only files created"):
                self.runner._apply_changes([{"path": "src/user.txt", "operation": "delete"}], increment)
        self.assertFalse(source.exists())
        self.assertFalse((self.target / "src/generated-renamed.txt").exists())
        self.assertEqual(user_file.read_text(encoding="utf-8"), "user data\n")

    def test_workflow_store_safely_creates_nested_report_directories(self):
        store = orchestrator.WorkflowStore(self.target, self.flow)
        with patch("safe_writer._run_preflight", return_value={"overall_result": "READY", "target_path": str(self.target.resolve())}):
            store.ensure_directory()
            reference = f"{store.directory}/reviews/inc-1/round-1.json"
            store.write(reference, {"decision": "APPROVED", "findings": []})
        self.assertTrue((self.target / reference).is_file())
        self.assertEqual(json.loads((self.target / reference).read_text()),
                         {"decision": "APPROVED", "findings": []})

    def test_initialize_persists_preflight_and_target_baseline(self):
        target = Path(self.temp.name) / "git-target"
        target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feature/task", str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "Harness Test"], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "harness-test@example.invalid"], check=True)
        (target / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-q", "-m", "baseline"], check=True)
        runner = orchestrator.Orchestrator(str(target), "Add the requested backend behavior.",
            config(branch="feature/task"), "flow-initialize")
        runner.initialize()
        self.assertEqual(runner.state["status"], "ACTIVE")
        execution_policy = runner.state["execution_policy"]
        self.assertEqual(execution_policy["version"], 1)
        self.assertEqual(execution_policy["mode"], "workspace")
        self.assertEqual(execution_policy["workspace_root"], str(target.resolve()))
        self.assertEqual(execution_policy["workflow_id"], "flow-initialize")
        self.assertIn("workspace file operations", execution_policy["allowed"])
        self.assertIn("commit/push/merge/deploy/publish", execution_policy["retained_protections"])
        preflight_reference = f"{runner.store.directory}/preflight.json"
        self.assertIn(preflight_reference, runner.state["artifact_references"])
        self.assertEqual(runner.store.read_json(preflight_reference)["overall_result"], "READY")
        self.assertEqual(runner.state["baseline"]["commit"],
                         subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip())
        self.assertEqual(runner.state["orchestration"]["git_tracking"]["base_ref_at_start"], "HEAD")
        self.assertEqual(runner.state["orchestration"]["git_tracking"]["base_commit"],
                         runner.state["baseline"]["commit"])
        self.assertEqual(runner.state["orchestration"]["git_tracking"]["workflow_branch"], "feature/task")
        self.assertEqual(runner.state["orchestration"]["git_tracking"]["target_worktree"], str(target.resolve()))
        changed_config = config(branch="feature/task", claude_budget_usd=1.0)
        resumed = orchestrator.Orchestrator(str(target), "Add the requested backend behavior.",
            changed_config, "flow-initialize")
        resumed.initialize()
        self.assertEqual(resumed.state["orchestration"]["config_snapshot"]["claude_budget_usd"], 1.0)
        self.assertEqual(resumed.state["execution_policy"], execution_policy)
        changed_planning_config = config(branch="feature/task", claude_budget_usd=1.0,
            validations=[{"id": "other-tests", "argv": ["python3", "-m", "unittest", "tests.test_other"]}])
        planning_update = orchestrator.Orchestrator(str(target), "Add the requested backend behavior.",
            changed_planning_config, "flow-initialize")
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "configuration changed"):
            planning_update.initialize()
        changed_task = orchestrator.Orchestrator(str(target), "A materially revised task.",
            config(branch="feature/task"), "flow-initialize")
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "different task text"):
            changed_task.initialize()

    def test_preexisting_untracked_metadata_is_snapshotted_and_preserved_like_any_user_file(self):
        target, runner = self.new_git_workflow("untracked-metadata-preserved", files={".DS_Store": b"user metadata"})
        self.assertIn(".DS_Store", {row["path"] for row in runner.state["baseline"]["preexisting_changes"]})
        self.assertIn(".DS_Store", {row["path"] for row in runner.state["orchestration"]["preexisting_hashes"]})
        (target / ".DS_Store").write_bytes(b"changed metadata")
        resumed = orchestrator.Orchestrator(str(target), "Add a feature", config(), runner.workflow_id)
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "Pre-existing change changed since workflow start: \.DS_Store"):
            resumed.initialize()

    def test_preexisting_sensitive_file_is_fingerprinted_without_reading_and_preserved(self):
        target, runner = self.new_git_workflow("sensitive-file-preserved", files={
            ".env": b"token=first\n", "credentials/token.txt": b"credential=first\n",
            ".aws/credentials": b"secret-access-key=first\n"})
        saved = {row["path"]: row for row in runner.state["orchestration"]["preexisting_hashes"]}
        for path in (".env", "credentials/token.txt", ".aws/credentials"):
            with self.subTest(path=path):
                self.assertTrue(saved[path]["protected_path"])
                self.assertIsNone(saved[path]["worktree_sha256"])
                self.assertIsInstance(saved[path]["worktree_metadata"], dict)
        (target / "credentials/token.txt").write_text("credential=other\n", encoding="utf-8")
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "Pre-existing change was modified: credentials/token.txt"):
            runner._assert_increment_changes({"increment_id": "inc-1"})

    def test_other_untracked_files_remain_protected(self):
        target, runner = self.new_git_workflow("other-untracked-protected", files={"notes.txt": b"user notes"})
        self.assertIn("notes.txt", {row["path"] for row in runner.state["orchestration"]["preexisting_hashes"]})
        (target / "notes.txt").write_bytes(b"changed notes")
        resumed = orchestrator.Orchestrator(str(target), "Add a feature", config(), runner.workflow_id)
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "Pre-existing change changed since workflow start: notes.txt"):
            resumed.initialize()

    def test_remote_ref_movement_is_recorded_without_replacing_workflow_baseline(self):
        target, base = git_fixture(Path(self.temp.name) / "git-target-ref-movement")
        subprocess.run(["git", "-C", str(target), "update-ref", "refs/remotes/origin/main", base], check=True)
        subprocess.run(["git", "-C", str(target), "config", "remote.origin.fetch",
                        "+refs/heads/*:refs/remotes/origin/*"], check=True)
        subprocess.run(["git", "-C", str(target), "config", "branch.feature/task.remote", "origin"], check=True)
        subprocess.run(["git", "-C", str(target), "config", "branch.feature/task.merge", "refs/heads/main"], check=True)
        (target / "feature.txt").write_text("feature\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "feature.txt"], check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-q", "-m", "feature change"], check=True)
        feature_head = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
        runner = orchestrator.Orchestrator(str(target), "Add a feature", config(base_ref="origin/main"),
                                           "flow-ref-movement")
        runner.initialize()
        tracking = runner.state["orchestration"]["git_tracking"]
        self.assertEqual(tracking["base_ref_at_start"], "origin/main")
        self.assertEqual(tracking["base_commit"], base)
        self.assertEqual(runner.state["baseline"]["commit"], feature_head)

        tree = subprocess.check_output(["git", "-C", str(target), "write-tree"], text=True).strip()
        upstream = subprocess.check_output([
            "git", "-C", str(target), "commit-tree", tree, "-p", feature_head,
        ], input="upstream-only commit\n", text=True).strip()
        subprocess.run(["git", "-C", str(target), "update-ref", "refs/remotes/origin/main", upstream], check=True)

        resumed = orchestrator.Orchestrator(str(target), "Add a feature", config(base_ref="origin/main"),
                                            "flow-ref-movement")
        resumed.initialize()
        tracking = resumed.state["orchestration"]["git_tracking"]
        self.assertEqual(tracking["base_commit"], base)
        self.assertEqual(resumed.state["baseline"]["commit"], feature_head)
        self.assertEqual(len(tracking["reference_movements"]), 1)
        movement = tracking["reference_movements"][0]
        self.assertEqual((movement["reference"], movement["previous_commit"], movement["new_commit"],
                          movement["workflow_id"], movement["operation"]),
                         ("origin/main", base, upstream, "flow-ref-movement", "workflow_resume"))
        self.assertEqual(subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip(),
                         feature_head)
        status = resumed._result()["git_state"]
        self.assertEqual(status["workflow_baseline_commit"], feature_head)
        self.assertEqual(status["base_commit"], base)
        self.assertEqual(status["workflow_branch"], "feature/task")
        self.assertEqual(status["target_worktree"], str(target.resolve()))
        self.assertEqual(status["head"], feature_head)
        self.assertIsInstance(status["worktree_clean"], bool)
        self.assertEqual(status["observed_upstream_commit"], upstream)
        self.assertEqual(status["behind_observed_upstream"], 1)
        self.assertTrue(status["upstream_moved_since_workflow_start"])
        self.assertTrue(status["base_reference_moved_since_workflow_start"])
        self.assertIn("does not change", status["interpretation"])

    def test_workflow_base_ref_cannot_be_replaced_during_requirement_update(self):
        target, base = git_fixture(Path(self.temp.name) / "git-target-immutable-base-ref")
        subprocess.run(["git", "-C", str(target), "update-ref", "refs/remotes/origin/main", base], check=True)
        original = orchestrator.Orchestrator(str(target), "Add a feature", config(base_ref="origin/main"),
                                             "flow-immutable-base-ref")
        original.initialize()
        changed = orchestrator.Orchestrator(str(target), "Add a changed feature",
            config(base_ref="origin/other"), "flow-immutable-base-ref",
            update_requirements_reason="The task requirements changed.")
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "base_ref configuration is immutable"):
            changed.initialize()

    def test_branch_checked_out_in_another_worktree_is_rejected_without_reuse(self):
        target, _ = git_fixture(Path(self.temp.name) / "git-target-duplicate-branch")
        other = Path(self.temp.name) / "other-worktree"
        subprocess.run(["git", "-C", str(target), "worktree", "add", "--force", str(other), "feature/task"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        runner = orchestrator.Orchestrator(str(target), "Add a feature", config(), "flow-duplicate-branch")
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "only in the explicitly selected target worktree"):
            runner.initialize()
        self.assertTrue((other / ".git").is_file())
        self.assertFalse((target / runner.store.directory).exists())

    def test_active_worktree_is_rechecked_before_scoped_operations(self):
        target, _ = git_fixture(Path(self.temp.name) / "git-target-active-worktree-guard")
        runner = orchestrator.Orchestrator(str(target), "Add a feature", config(), "flow-active-worktree-guard")
        runner.initialize()
        other = Path(self.temp.name) / "other-worktree-active-guard"
        subprocess.run(["git", "-C", str(target), "worktree", "add", "--force", str(other), "feature/task"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "only in the explicitly selected target worktree"):
            runner._assert_active_worktree()
        self.assertTrue((other / ".git").is_file())

    def test_initialize_does_not_create_or_clean_up_worktrees_or_move_branch(self):
        target, base = git_fixture(Path(self.temp.name) / "git-target-worktree-stability")
        other = Path(self.temp.name) / "other-worktree"
        subprocess.run(["git", "-C", str(target), "worktree", "add", "-b", "other-task",
                        str(other), base], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        before = subprocess.check_output(["git", "-C", str(target), "worktree", "list", "--porcelain", "-z"])
        before_head = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
        before_branch = subprocess.check_output(["git", "-C", str(target), "branch", "--show-current"], text=True).strip()
        runner = orchestrator.Orchestrator(str(target), "Add a feature", config(), "flow-worktree-stability")
        runner.initialize()
        runner._result()
        after = subprocess.check_output(["git", "-C", str(target), "worktree", "list", "--porcelain", "-z"])
        self.assertEqual(after, before)
        self.assertTrue((other / ".git").is_file())
        self.assertEqual(subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip(),
                         before_head)
        self.assertEqual(subprocess.check_output(["git", "-C", str(target), "branch", "--show-current"], text=True).strip(),
                         before_branch)

    def test_explicit_commit_base_ref_records_exact_commit_without_checkout_mutation(self):
        target, base = git_fixture(Path(self.temp.name) / "git-target-explicit-commit-base")
        before_head = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
        runner = orchestrator.Orchestrator(str(target), "Add a feature", config(base_ref=base),
                                           "flow-explicit-commit-base")
        runner.initialize()
        tracking = runner.state["orchestration"]["git_tracking"]
        self.assertEqual(tracking["base_ref_at_start"], base)
        self.assertEqual(tracking["base_commit"], base)
        self.assertEqual(subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip(),
                         before_head)

    def test_legacy_workflow_migrates_from_recorded_head_without_guessing_old_base_ref(self):
        target, base = git_fixture(Path(self.temp.name) / "git-target-legacy-state")
        runner = orchestrator.Orchestrator(str(target), "Add a feature", config(), "flow-legacy-git-state")
        runner.initialize()
        runner.state["orchestration"].pop("git_tracking")
        runner.store.save(runner.state)
        resumed = orchestrator.Orchestrator(str(target), "Add a feature", config(), "flow-legacy-git-state")
        resumed.initialize()
        tracking = resumed.state["orchestration"]["git_tracking"]
        self.assertEqual(tracking["base_ref_at_start"], "legacy:HEAD")
        self.assertEqual(tracking["base_commit"], base)
        self.assertIn("original base reference was not recorded", tracking["migration"])
        self.assertIsNone(resumed._result()["git_state"]["upstream_moved_since_workflow_start"])

    def test_explicit_requirement_update_preserves_history_and_invalidates_dependent_records(self):
        target = Path(self.temp.name) / "git-target-requirement-update"
        target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feature/task", str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "Harness Test"], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "harness-test@example.invalid"], check=True)
        (target / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-q", "-m", "baseline"], check=True)
        original = orchestrator.Orchestrator(str(target), "Build the first behavior.",
            config(branch="feature/task"), "flow-requirement-update")
        original.initialize()
        store = original.store
        old_plan = f"{store.directory}/plan.json"
        with patch("safe_writer._run_preflight", return_value={"overall_result": "READY", "target_path": str(target.resolve())}):
            store.write(old_plan, {"plan": "old"})
        old = original.state
        old["scope"].update({"increment_id": "inc-old", "included_paths": ["src"],
            "code_paths": ["src"], "artifact_paths": [], "revision_paths": ["src"]})
        old["pending_decisions"].append({"decision_id": "DEC-OLD", "status": "open"})
        old["authorizations"].append({"authorization_id": "AUTH-OLD"})
        old["review_assignments"].append({"review_id": "REVIEW-OLD"})
        old["completion"] = {"gate_decision_reference": "gate.json", "gate_decision_id": "GATE-OLD",
            "gate_outcome": "APPROVE", "approved_revision_id": "content:" + "f" * 64,
            "final_evidence_ids": ["VAL-OLD"]}
        old["claude_review_cycles"].append({"cycle_id": "CYCLE-OLD", "increment_id": "inc-old"})
        old["orchestration"].update({"plan_reference": old_plan,
            "increments": [{"increment_id": "inc-old", "status": "validated"}],
            "validation_results": [{"validation_id": "VAL-OLD", "increment_id": "inc-old"}]})
        old["orchestration"].pop("self_reviews")
        with patch("safe_writer._run_preflight", return_value={"overall_result": "READY", "target_path": str(target.resolve())}):
            store.save(old)

        revised = orchestrator.Orchestrator(str(target), "Build the revised behavior.",
            config(branch="feature/task"), "flow-requirement-update",
            update_requirements_reason="Approved architecture decision changed.")
        revised.initialize()
        state = revised.state
        orchestration = state["orchestration"]
        self.assertEqual(orchestration["current_requirement_version"], 2)
        self.assertEqual(orchestration["self_reviews"], [])
        self.assertEqual([row["status"] for row in orchestration["requirement_history"]], ["superseded", "current"])
        self.assertEqual(state["status"], "ACTIVE")
        self.assertIsNone(orchestration["plan_reference"])
        self.assertEqual(state["scope"]["included_paths"], [])
        self.assertEqual(orchestration["increments"][0]["increment_id"], "inc-old")
        self.assertEqual(orchestration["increments"][0]["invalidated_by_requirement_version"], 2)
        self.assertEqual(orchestration["validation_results"][0]["validation_id"], "VAL-OLD")
        self.assertEqual(orchestration["validation_results"][0]["invalidated_by_requirement_version"], 2)
        event = orchestration["invalidations"][-1]
        self.assertEqual(event["obsolete_plan_reference"], old_plan)
        self.assertEqual(event["invalidated_decision_ids"], ["DEC-OLD"])
        self.assertEqual(event["invalidated_validation_ids"], ["VAL-OLD"])
        self.assertEqual(event["invalidated_authorization_ids"], ["AUTH-OLD"])
        self.assertEqual(event["invalidated_completion"]["gate_decision_id"], "GATE-OLD")
        self.assertTrue(state["authorizations"][0]["revoked"])
        self.assertNotIn("completion", state)
        self.assertEqual(state["review_assignments"][0]["review_id"], "REVIEW-OLD")
        self.assertEqual(state["review_assignments"][0]["invalidated_by_requirement_version"], 2)
        self.assertEqual(state["pending_decisions"][0]["invalidated_by_requirement_version"], 2)
        self.assertEqual(state["claude_review_cycles"][0]["invalidated_by_requirement_version"], 2)
        self.assertEqual(orchestrator.list_pending_decisions(str(target), "flow-requirement-update")["pending_decisions"], [])
        self.assertTrue((target / old_plan).is_file())
        self.assertEqual(revised.store.read_json(orchestration["requirement_history"][-1]["task_reference"])["task"],
                         "Build the revised behavior.")

    def test_implementation_context_carries_current_plan_and_authorization_context(self):
        increment = plan()["increments"][0]
        value = plan()
        self.runner.state = {"scope": {"increment_id": "inc-1", "included_paths": ["src"]},
            "pending_decisions": [{"decision_id": "DEC-1", "status": "resolved"}],
            "instruction_records": [{"source_reference": "AGENTS.md"}],
            "orchestration": {"current_requirement_version": 1, "task_sha256": "a" * 64,
                "requirement_history": [{"version": 1, "task_reference": "req-v1.json", "task_sha256": "a" * 64}],
                "invalidations": [], "increments": []}}
        context = self.runner._implementation_context(value, increment)
        self.assertEqual(context["requirement"]["version"], 1)
        self.assertEqual(context["requirement"]["reference"], "req-v1.json")
        self.assertEqual(context["approved_decisions"][0]["decision_id"], "DEC-1")
        self.assertEqual(context["architecture_context"], value["architecture_context"])
        self.assertEqual(context["acceptance_criteria"], value["acceptance_criteria"])
        self.assertEqual(context["validations"][0]["id"], "tests")
        self.assertIn("AGENTS.md", context["requested_sources"])

    def test_replanning_uses_versioned_plan_and_does_not_reuse_invalidated_decisions(self):
        self.write_state(self.runner, pending=[{"decision_id": "DEC-OLD", "status": "resolved"}])
        self.runner.state.update({"instruction_records": [], "artifact_references": [],
            "applied_capabilities": []})
        orchestration = self.runner.state["orchestration"]
        orchestration.update({"current_requirement_version": 2,
            "requirement_history": [{"version": 2, "task_reference": "requirements-v2.json", "task_sha256": "b" * 64}],
            "invalidations": [{"invalidated_decision_ids": ["DEC-OLD"]}],
            "plan_reference": None, "increments": [{"increment_id": "inc-old", "status": "validated"}]})
        self.runner._save = lambda: None
        captured = {}
        self.runner._run_codex = lambda stage, context, *args, **kwargs: captured.update(
            {"stage": stage, "context": context}) or plan()
        with patch.object(self.runner.store, "write", return_value={}):
            result = self.runner._planning(Path(self.temp.name))
        self.assertEqual(result["increments"][0]["increment_id"], "req2-inc-1")
        self.assertEqual(orchestration["plan_reference"], f"{self.runner.store.directory}/plan-v2.json")
        self.assertEqual([item["increment_id"] for item in orchestration["increments"]], ["inc-old", "req2-inc-1"])
        self.assertEqual(orchestration["increments"][-1]["requirement_version"], 2)
        self.assertEqual(captured["stage"], "PLANNING")
        self.assertEqual(captured["context"]["resolved_decisions"], [])
        self.assertEqual(captured["context"]["superseded_decisions_not_reusable"][0]["decision_id"], "DEC-OLD")

    def test_replanning_recovers_matching_versioned_plan_after_interruption(self):
        current_plan = plan()
        increment = current_plan["increments"][0]
        increment["increment_id"] = "req2-inc-1"
        increment["artifact_paths"] = [self.runner.store.directory]
        current_plan["increments"] = [increment]
        task_hash = "a" * 64
        config_hash = orchestrator._sha(orchestrator._json_bytes(self.runner.config))
        planning_hash = orchestrator._planning_config_sha256(self.runner.config)
        plan_path = f"{self.runner.store.directory}/plan-v2.json"
        wrapper = {"artifact_schema_version": 1, "requirement_version": 2,
            "task_sha256": task_hash, "config_sha256": planning_hash, "plan": current_plan}
        with patch("safe_writer._run_preflight", return_value={"overall_result": "READY", "target_path": str(self.target.resolve())}):
            self.runner.store.write(plan_path, wrapper)
        self.runner.state = {"status": "ACTIVE", "phase": "PLANNING", "updated_at": "",
            "workflow_id": self.flow, "artifact_references": [], "applied_capabilities": [],
            "orchestration": {"current_requirement_version": 2, "task_sha256": task_hash,
                "config_sha256": config_hash, "current_stage": "PLANNING", "plan_reference": None,
                "increments": [], "attempts": []}}
        self.runner._save = lambda: None
        self.runner._run_codex = lambda *_args, **_kwargs: self.fail("matching persisted plan should be resumed")
        recovered = self.runner._planning(Path(self.temp.name))
        self.assertEqual(recovered["increments"][0]["increment_id"], "req2-inc-1")
        self.assertEqual(self.runner.state["orchestration"]["plan_reference"], plan_path)
        self.assertEqual(self.runner.state["orchestration"]["increments"][0]["requirement_version"], 2)

    def test_authorization_only_config_change_preserves_existing_stored_plan(self):
        old_config = self.runner.config
        old_config_hash = orchestrator._sha(orchestrator._json_bytes(old_config))
        auth = persistent_claude_authorization(self.target)
        self.runner.config = orchestrator.validate_config(config(
            review={"enabled": True, "authorization": auth}))
        current_config_hash = orchestrator._sha(orchestrator._json_bytes(self.runner.config))
        plan_path = f"{self.runner.store.directory}/plan-v1.json"
        plan_path_abs = self.target / plan_path
        plan_path_abs.parent.mkdir(parents=True, exist_ok=True)
        expected_plan = plan()
        plan_path_abs.write_text(json.dumps({"artifact_schema_version": 1,
            "requirement_version": 1, "task_sha256": orchestrator._sha(b"Add a feature"),
            "config_sha256": old_config_hash, "plan": expected_plan}), encoding="utf-8")
        self.runner.state = {"orchestration": {"current_requirement_version": 1,
            "task_sha256": orchestrator._sha(b"Add a feature"), "config_sha256": current_config_hash}}

        recovered = self.runner._read_plan(plan_path)

        self.assertEqual(recovered, expected_plan)
        self.assertEqual(json.loads(plan_path_abs.read_text(encoding="utf-8"))["config_sha256"], old_config_hash)

    def test_reviewer_configuration_changes_do_not_change_planning_fingerprint(self):
        initial_config = self.runner.config
        planning_hash = orchestrator._planning_config_sha256(initial_config)
        auth = persistent_claude_authorization(self.target)
        changed_review_config = orchestrator.validate_config(config(
            claude_budget_usd=0.5,
            review={"enabled": True, "context_paths": ["docs/api.md"], "authorization": auth}))
        current_config_hash = orchestrator._sha(orchestrator._json_bytes(changed_review_config))
        plan_path = f"{self.runner.store.directory}/plan-v1.json"
        plan_path_abs = self.target / plan_path
        plan_path_abs.parent.mkdir(parents=True, exist_ok=True)
        expected_plan = plan()
        plan_path_abs.write_text(json.dumps({"artifact_schema_version": 1,
            "requirement_version": 1, "task_sha256": orchestrator._sha(b"Add a feature"),
            "config_sha256": planning_hash, "plan": expected_plan}), encoding="utf-8")
        self.runner.config = changed_review_config
        self.runner.state = {"orchestration": {"current_requirement_version": 1,
            "task_sha256": orchestrator._sha(b"Add a feature"), "config_sha256": current_config_hash}}

        self.assertEqual(self.runner._read_plan(plan_path), expected_plan)

    def test_genuine_planning_config_change_still_invalidates_stored_plan(self):
        original_config = self.runner.config
        planning_hash = orchestrator._planning_config_sha256(original_config)
        changed_config = orchestrator.validate_config(config(
            validations=[{"id": "different-tests", "argv": ["python3", "-m", "unittest", "tests.test_other"]}]))
        current_config_hash = orchestrator._sha(orchestrator._json_bytes(changed_config))
        plan_path = f"{self.runner.store.directory}/plan-v1.json"
        plan_path_abs = self.target / plan_path
        plan_path_abs.parent.mkdir(parents=True, exist_ok=True)
        plan_path_abs.write_text(json.dumps({"artifact_schema_version": 1,
            "requirement_version": 1, "task_sha256": orchestrator._sha(b"Add a feature"),
            "config_sha256": planning_hash, "plan": plan()}), encoding="utf-8")
        self.runner.config = changed_config
        self.runner.state = {"orchestration": {"current_requirement_version": 1,
            "task_sha256": orchestrator._sha(b"Add a feature"), "config_sha256": current_config_hash}}

        with self.assertRaisesRegex(orchestrator.OrchestratorError,
                                    "Stored plan does not match the current requirement/configuration version"):
            self.runner._read_plan(plan_path)

    def _recorded_legacy_plan_fixture(self, *, current_task="Add a feature", saved_config_sha=None,
                                      requirement_config_sha=None):
        historical_config = orchestrator.validate_config(config(claude_budget_usd=0.01))
        original_config_sha = orchestrator._legacy_plan_config_sha256(historical_config)
        auth = persistent_claude_authorization(self.target, authorization_id="AUTH-HISTORICAL-PLAN",
                                               max_budget_usd=2.0)
        current_config = orchestrator.validate_config(config(
            claude_budget_usd=2.0, review={"enabled": True, "authorization": auth}))
        self.runner.config = current_config

        requirement_reference = f"{self.runner.store.directory}/requirements-v2.json"
        plan_reference = f"{self.runner.store.directory}/plan-v2.json"
        requirement_path = self.target / requirement_reference
        plan_path = self.target / plan_reference
        requirement_path.parent.mkdir(parents=True, exist_ok=True)
        current_task_sha = orchestrator._sha(current_task.encode())
        original_task_sha = orchestrator._sha(b"Add a feature")
        requirement_path.write_text(json.dumps({
            "workflow_id": self.flow, "version": 2, "task": current_task,
            "task_sha256": current_task_sha,
            "configuration_sha256": requirement_config_sha or original_config_sha,
        }), encoding="utf-8")
        plan_path.write_text(json.dumps({
            "artifact_schema_version": 1, "requirement_version": 2,
            "task_sha256": original_task_sha,
            "config_sha256": saved_config_sha or original_config_sha,
            "plan": plan(),
        }), encoding="utf-8")
        self.runner.state = {
            "workflow_id": self.flow,
            "artifact_references": [requirement_reference],
            "orchestration": {
                "current_requirement_version": 2,
                "task_sha256": current_task_sha,
                "config_sha256": orchestrator._sha(orchestrator._json_bytes(current_config)),
                "requirement_history": [{
                    "version": 2, "task_reference": requirement_reference,
                    "task_sha256": current_task_sha, "status": "current",
                }],
                "claude_authorization": auth,
                "claude_authorization_uses": [{
                    "authorization_id": auth["authorization_id"],
                    "invocation_attempted": True, "status": "INVOCATION_FAILED",
                    "budget_limit_usd": 0.01, "cost_usd": 0.334493,
                }],
            },
        }
        return plan_reference, original_config_sha

    def test_recorded_legacy_plan_survives_review_budget_and_authorization_changes(self):
        plan_reference, original_config_sha = self._recorded_legacy_plan_fixture()

        recovered = self.runner._read_plan(plan_reference)

        self.assertEqual(recovered, plan())
        self.assertEqual(json.loads((self.target / self.runner.store.directory / "requirements-v2.json").read_text(
            encoding="utf-8"))["configuration_sha256"], original_config_sha)
        self.assertEqual(self.runner.config["claude_budget_usd"], 2.0)

    def test_recorded_legacy_plan_rejects_planning_config_and_requirement_changes(self):
        plan_reference, _original_config_sha = self._recorded_legacy_plan_fixture()
        changed_config = copy.deepcopy(self.runner.config)
        changed_config["validations"] = [{
            "id": "different-tests", "argv": ["python3", "-m", "unittest", "tests.test_other"],
        }]
        self.runner.config = orchestrator.validate_config(changed_config)
        with self.subTest(change="planning configuration"), self.assertRaisesRegex(
                orchestrator.OrchestratorError, "Stored plan does not match"):
            self.runner._read_plan(plan_reference)

        plan_reference, _original_config_sha = self._recorded_legacy_plan_fixture(current_task="A changed feature")
        with self.subTest(change="requirements"), self.assertRaisesRegex(
                orchestrator.OrchestratorError, "Stored plan does not match"):
            self.runner._read_plan(plan_reference)

    def test_recorded_legacy_plan_rejects_unknown_or_unrelated_fingerprints(self):
        for saved_sha, requirement_sha in [("f" * 64, None), ("e" * 64, "e" * 64)]:
            with self.subTest(saved_sha=saved_sha[:8]):
                plan_reference, _original_config_sha = self._recorded_legacy_plan_fixture(
                    saved_config_sha=saved_sha, requirement_config_sha=requirement_sha)
                with self.assertRaisesRegex(orchestrator.OrchestratorError, "Stored plan does not match"):
                    self.runner._read_plan(plan_reference)

    def test_self_review_corrects_findings_and_requires_a_passing_followup(self):
        increment = plan()["increments"][0]
        self.runner.state = {"scope": {"increment_id": "inc-1", "included_paths": ["src"]},
            "pending_decisions": [], "instruction_records": [],
            "orchestration": {"current_requirement_version": 1,
                "requirement_history": [{"version": 1, "task_reference": "requirements-v1.json", "task_sha256": "a" * 64}],
                "invalidations": [], "self_reviews": [], "increments": [{"increment_id": "inc-1", "changed_paths": ["src/a.py"]}],
                "attempts": [], "validation_results": [], "review_results": []}}
        self.runner._save = lambda: None
        finding = {"id": "SELF-1", "severity": "medium", "file": "src/a.py",
            "description": "Missing edge case.", "recommendation": "Handle the edge case."}
        responses = iter([
            {"decision": "CORRECTED", "summary": "Found and corrected a gap.", "findings": [finding],
             "changes": [{"path": "src/a.py", "content": "corrected\n"}]},
            {"decision": "PASS", "summary": "Correction verified.", "findings": [], "changes": []},
        ])
        contexts = []
        def run_review(_stage, context, *_args, **_kwargs):
            result = next(responses)
            self.record_mock_self_review_attempt(self.runner, result)
            contexts.append(json.loads(json.dumps(context)))
            return result
        with patch.object(self.runner, "_set_stage"), \
             patch.object(self.runner, "_revision_manifest", side_effect=[
                 ("content:" + "a" * 64, {}, "manifest-before.json"),
                 ("content:" + "b" * 64, {}, "manifest-after.json")]), \
             patch.object(self.runner, "_run_codex", side_effect=run_review), \
             patch.object(self.runner, "_apply_changes") as apply_changes:
            self.runner._self_review(plan(), increment, Path(self.temp.name))
        apply_changes.assert_called_once()
        self.assertEqual(len(contexts), 2)
        self.assertEqual(contexts[0]["review_mode"], "self-review; not independent review or approval")
        self.assertEqual(contexts[0]["evidence_cutoff"]["attempt_count"], 0)
        self.assertIn("prior_self_review_findings", contexts[1])
        record = self.runner.state["orchestration"]["self_reviews"][0]
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["findings"], [finding])
        self.assertEqual(record["correction_passes"], 1)
        self.assertEqual(record["classification"], "self_review_not_independent")

    def test_self_review_does_not_accept_unresolved_findings_or_out_of_scope_paths(self):
        increment = plan()["increments"][0]
        invalid = {"decision": "PASS", "summary": "Finished.", "changes": [], "findings": [{
            "id": "SELF-1", "severity": "high", "file": "elsewhere/file.py",
            "description": "A concern.", "recommendation": "Fix it.",
        }]}
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "outside the increment allowlist"):
            self.runner._self_review_valid(invalid, increment)
        managed_finding = {"decision": "CORRECTED", "summary": "Process document is stale.",
            "changes": [{"path": orchestrator.PROCESS_EVIDENCE_PATH, "content": "future evidence"}],
            "findings": [{"id": "SELF-EVIDENCE", "severity": "medium",
                "file": orchestrator.PROCESS_EVIDENCE_PATH,
                "description": "It omits this review.", "recommendation": "Update it."}]}
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "controller-managed process evidence"):
            self.runner._self_review_valid(managed_finding, increment)

    def test_self_review_uses_record_cutoff_and_finalizes_evidence_after_persisting_pass(self):
        target, runner, current_plan, increment = self.process_evidence_fixture("process-evidence-pass")
        before_id, before_manifest, _ = runner._revision_manifest(increment)
        response = self.self_review_response(summary="Implementation review passed.")
        contexts = []

        def run_review(_stage, context, *_args, **_kwargs):
            contexts.append(copy.deepcopy(context))
            self.record_mock_self_review_attempt(runner, response)
            return response

        original_sync = runner._sync_process_evidence
        observed_finalization = []

        def sync_after_persisted_review(item):
            review = runner.state["orchestration"]["self_reviews"][-1]
            attempt = runner.state["orchestration"]["attempts"][-1]
            persisted = runner.store.read_state()
            self.assertEqual(review["status"], "passed")
            self.assertEqual(attempt["trace"]["self_review_result"]["outcome"], "passed")
            self.assertEqual(persisted["orchestration"]["self_reviews"][-1]["status"], "passed")
            self.assertEqual(persisted["orchestration"]["attempts"][-1]["trace"]["self_review_result"]["outcome"], "passed")
            self.assertEqual(runner.state["orchestration"]["current_stage"], "SELF_REVIEW")
            observed_finalization.append(True)
            original_sync(item)

        with patch.object(runner, "_run_codex", side_effect=run_review), \
             patch.object(runner, "_sync_process_evidence", side_effect=sync_after_persisted_review):
            runner._self_review(current_plan, increment, Path(self.temp.name))

        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["evidence_cutoff"]["attempt_count"], 0)
        self.assertEqual(contexts[0]["constraints"]["revision_exclusions"], [orchestrator.PROCESS_EVIDENCE_PATH])
        self.assertEqual(contexts[0]["constraints"]["revision_paths"], ["src", "docs"])
        self.assertEqual(contexts[0]["review_checklist"][-2:], ["acceptance criteria", "authorized implementation paths"])
        self.assertEqual(observed_finalization, [True])
        after_id, after_manifest, _ = runner._revision_manifest(increment)
        self.assertEqual(after_id, before_id)
        self.assertNotIn(orchestrator.PROCESS_EVIDENCE_PATH,
                         {row["path"] for row in after_manifest["manifest"]})
        self.assertEqual(before_manifest["excluded_paths"], [orchestrator.PROCESS_EVIDENCE_PATH])
        evidence = (target / orchestrator.PROCESS_EVIDENCE_PATH).read_text(encoding="utf-8")
        self.assertIn("Controller-managed workflow evidence", evidence)
        self.assertIn("Implementation review passed.", evidence)
        self.assertIn("0 attempts", evidence)
        metadata = runner.state["orchestration"]["process_evidence"]
        self.assertEqual(metadata["attempt_count"], 1)
        self.assertEqual(metadata["self_review_count"], 1)
        self.assertEqual(metadata["sha256"], orchestrator._sha((target / orchestrator.PROCESS_EVIDENCE_PATH).read_bytes()))

    def test_failed_self_review_attempts_and_two_correction_round_limit_are_preserved(self):
        target, runner, current_plan, increment = self.process_evidence_fixture("process-evidence-failed-reviews")
        finding = {"id": "SELF-FAIL", "severity": "medium", "file": "src/feature.py",
            "description": "A concrete implementation issue remains.", "recommendation": "Correct the issue."}
        response = self.self_review_response("CORRECTED", findings=[finding],
            changes=[{"path": "src/feature.py", "content": "value = 2\n"}],
            summary="Correction proposed; review did not pass.")
        calls = []

        def run_review(_stage, _context, *_args, **_kwargs):
            calls.append(True)
            self.record_mock_self_review_attempt(runner, response)
            return response

        with patch.object(runner, "_run_codex", side_effect=run_review), \
             patch.object(runner, "_apply_changes"), \
             self.assertRaisesRegex(orchestrator.OrchestratorError, "bounded correction loop"):
            runner._self_review(current_plan, increment, Path(self.temp.name))

        attempts = runner.state["orchestration"]["attempts"]
        self.assertEqual(runner.config["max_iterations"], 2)
        self.assertEqual(len(calls), runner.config["max_iterations"] + 1)
        self.assertEqual([row["trace"]["self_review_result"]["correction_round"] for row in attempts], [0, 1, 2])
        self.assertEqual([row["trace"]["self_review_result"]["outcome"] for row in attempts],
                         ["corrections_applied", "corrections_applied", "correction_limit_reached"])
        self.assertTrue(all(row["trace"]["self_review_result"]["findings"] == [finding] for row in attempts))
        self.assertEqual(runner.state["orchestration"]["self_reviews"], [])
        evidence = (target / orchestrator.PROCESS_EVIDENCE_PATH).read_text(encoding="utf-8")
        self.assertEqual(evidence.count("Self-review evidence cutoff:"), 3)
        self.assertIn("correction_limit_reached", evidence)
        self.assertIn("A concrete implementation issue remains.", evidence)

    def test_increment_enters_testing_after_self_review_and_evidence_finalization(self):
        _target, runner, current_plan, increment = self.process_evidence_fixture("process-evidence-testing-stage")
        runner.state["orchestration"]["increments"][0].update({"status": "implemented", "requirement_version": 1})
        runner._save()
        response = self.self_review_response(summary="Implementation is ready for validation.")

        def run_review(_stage, _context, *_args, **_kwargs):
            self.record_mock_self_review_attempt(runner, response)
            return response

        def enter_testing(_plan, _increment, _revision_id):
            self.assertEqual(runner.state["orchestration"]["self_reviews"][-1]["status"], "passed")
            self.assertEqual(runner.state["orchestration"]["process_evidence"]["self_review_count"], 1)
            self.assertEqual(runner.state["orchestration"]["current_stage"], "SELF_REVIEW")
            runner._set_stage("VALIDATION", "TESTING")

        with patch.object(runner, "_run_codex", side_effect=run_review), \
             patch.object(runner, "_validate", side_effect=enter_testing):
            runner._increment(current_plan, increment, Path(self.temp.name))

        self.assertEqual(runner.state["orchestration"]["current_stage"], "VALIDATION")
        self.assertEqual(runner.state["phase"], "TESTING")
        self.assertEqual(runner.state["orchestration"]["increments"][0]["status"], "implemented")

    def test_resume_rejects_changed_target_head(self):
        target = Path(self.temp.name) / "git-target-changed-head"
        target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feature/task", str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "Harness Test"], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "harness-test@example.invalid"], check=True)
        source = target / "README.md"
        source.write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-q", "-m", "baseline"], check=True)
        runner = orchestrator.Orchestrator(str(target), "Add the requested backend behavior.",
            config(branch="feature/task"), "flow-head-change")
        runner.initialize()
        source.write_text("different baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-q", "-m", "unexpected new baseline"], check=True)
        resumed = orchestrator.Orchestrator(str(target), "Add the requested backend behavior.",
            config(branch="feature/task"), "flow-head-change")
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "HEAD changed"):
            resumed.initialize()

    def test_validation_execution_records_revision_bound_evidence(self):
        product = self.target / "src" / "feature.txt"
        product.write_text("feature\n", encoding="utf-8")
        increment = plan()["increments"][0]
        self.runner.state = {
            "status": "ACTIVE", "phase": "IMPLEMENTATION", "updated_at": "",
            "workflow_id": self.flow, "scope": {}, "artifact_references": [],
            "baseline": {"commit": "b" * 40}, "target": {"target_id": "target-test"},
            "orchestration": {"current_stage": "IMPLEMENTATION", "validation_results": [],
                "increments": [{"increment_id": "inc-1", "status": "implemented"}]},
        }
        revision_id, _, _ = self.runner._revision_manifest(increment)
        with patch("safe_writer._run_preflight", return_value={"overall_result": "READY", "target_path": str(self.target.resolve())}), \
             patch.object(self.runner, "_assert_active_worktree"):
            self.runner._validate(plan(), increment, revision_id)
        result = self.runner.state["orchestration"]["validation_results"][0]
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["revision_id"], revision_id)
        evidence = json.loads((self.target / result["artifact_reference"]).read_text())
        self.assertEqual(evidence["revision"]["revision_id"], revision_id)

    def test_claude_adapter_receives_exact_revision_scope_and_budget(self):
        product = self.target / "src" / "feature.txt"
        product.write_text("feature\n", encoding="utf-8")
        increment = plan()["increments"][0]
        store = self.runner.store
        evidence_reference = f"{store.directory}/evidence/VAL-0001.json"
        manifest_reference = f"{store.directory}/revision-manifest-inc-1.json"
        self.runner.state = {"baseline": {"commit": "b" * 40}}
        with patch("safe_writer._run_preflight", return_value={"overall_result": "READY", "target_path": str(self.target.resolve())}):
            store.write(evidence_reference, {"evidence_id": "VAL-0001"})
            revision_id, manifest, _ = self.runner._revision_manifest(increment)
            store.write(manifest_reference, manifest)
        authorization = persistent_claude_authorization(self.target)
        self.runner.state = {
            "workflow_id": self.flow, "status": "ACTIVE", "phase": "INDEPENDENT_VERIFICATION",
            "target": {"target_id": "target-test"}, "baseline": {"commit": "b" * 40},
            "scope": {"increment_id": "inc-1"}, "artifact_references": [evidence_reference, manifest_reference],
            "authorizations": [], "claude_review_cycles": [], "pending_decisions": [],
            "authorization_verifier": {"identity": "primary Codex session", "role": "Workflow Owner",
                "identity_assurance": "declared", "source_reference": f"task:{self.flow}"},
            "orchestration": {"revision_manifest_reference": manifest_reference,
                "config_snapshot": self.runner.config,
                "config_sha256": orchestrator._sha(orchestrator._json_bytes(self.runner.config)),
                "claude_authorization": None, "claude_authorization_history": [], "claude_authorization_uses": [],
                "claude_authorization_uses": [],
                "validation_results": [{"increment_id": "inc-1", "exit_code": 0, "revision_id": revision_id,
                    "evidence_id": "VAL-0001", "artifact_reference": evidence_reference,
                    "argv": ["dotnet", "test", "--no-restore"], "observed_result": "passed"}],
                "review_results": []},
        }
        self.runner._save = lambda: None
        store.read_state = lambda: self.runner.state
        observed = {}

        def adapter(request, **kwargs):
            observed["request"] = request
            observed["kwargs"] = kwargs
            return {"report_valid": False, "report_received": False,
                "failure_kind": "request_validation_failure", "error": "controlled test failure",
                "process": {"invocation_attempted_at": None, "model": None, "usage": None, "cost_usd": None}}

        with patch("orchestrator.claude._git_environment", return_value={}), \
             patch("orchestrator.claude._changed_path_names", return_value=["src/feature.txt"]), \
             patch("orchestrator.claude._authorized_review_paths",
                   return_value=["src/feature.txt", evidence_reference, manifest_reference]), \
             patch.object(self.runner, "_assert_active_worktree"), \
             patch("orchestrator.claude.run_claude_review", side_effect=adapter) as invoke:
            missing = self.runner._review_increment(plan(), {**increment, "artifact_paths": [store.directory]}, revision_id)
            self.assertIsNone(missing)
            self.assertEqual(self.runner.state["status"], "WAITING_FOR_HUMAN")
            self.assertEqual(invoke.call_count, 0)
            self.assertEqual(self.runner.state["claude_review_cycles"], [])
            self.runner.config = orchestrator.validate_config(config(review={"enabled": True, "authorization": authorization}))
            self.runner._apply_claude_authorization_config_change(self.runner.state)
            self.assertEqual(self.runner.state["pending_decisions"][0]["status"], "resolved")
            result = self.runner._review_increment(plan(), {**increment, "artifact_paths": [store.directory]}, revision_id)
        request = observed["request"]
        self.assertEqual(request["target_path"], str(self.target.resolve()))
        self.assertEqual(request["target_revision_id"], revision_id)
        self.assertEqual(request["scope_paths"], ["src"])
        self.assertEqual(request["max_budget_usd"], 2.0)
        self.assertEqual(observed["kwargs"]["aily_executable"], orchestrator.claude.AILY_EXECUTABLE)
        self.assertIsNone(result)
        self.assertEqual(self.runner.state["status"], "BLOCKED")
        self.assertEqual(self.runner.state["claude_review_cycles"][0]["round_count"], 0)
        use = self.runner.state["orchestration"]["claude_authorization_uses"][0]
        self.assertEqual(use["authorization_id"], authorization["authorization_id"])
        self.assertEqual(use["use_id"], "review-inc-1-1-round-1-attempt-1-use")
        self.assertFalse(use["invocation_attempted"])
        self.assertEqual(use["status"], "RESERVATION_RELEASED")

    def test_review_gate_dispatches_claude_automatically_after_validated_increment(self):
        revision_id = "content:" + "d" * 64
        self.runner.state = {"status": "ACTIVE", "phase": "TESTING", "workflow_id": self.flow,
            "target": {"target_id": "target-test"}, "baseline": {"commit": "b" * 40},
            "pending_decisions": [], "claude_review_cycles": [], "review_assignments": [],
            "orchestration": {"current_stage": "VALIDATION", "increments": [{"increment_id": "inc-1",
                "status": "validated", "revision_id": revision_id}], "validation_results": [],
                "review_results": [], "final_revision_id": revision_id}}
        self.runner._save = lambda: None
        dispatched = []
        with patch.object(self.runner, "_revision_manifest", return_value=(revision_id, {}, "manifest.json")), \
             patch.object(self.runner, "_review_increment", side_effect=lambda *args, **kwargs:
                 dispatched.append((args[1]["increment_id"], args[2])) or {"findings": []}):
            self.runner._review_and_gate(plan())
        self.assertEqual(dispatched, [("inc-1", revision_id)])
        self.assertEqual(self.runner.state["orchestration"]["increments"][0]["status"], "reviewed")
        self.assertEqual(self.runner.state["status"], "BLOCKED")
        self.assertEqual(self.runner.state["phase"], "VERIFICATION_GATE")

    def test_accepted_claude_finding_is_corrected_verified_and_sent_for_followup(self):
        import claude_reviewer

        finding = {"id": "CF-1", "severity": "medium", "file": "src/feature.txt",
                   "description": "A defect needs correction.", "recommendation": "Correct it."}
        managed_process = {"id": "CF-2", "severity": "medium", "file": orchestrator.PROCESS_EVIDENCE_PATH,
                   "description": "Controller process evidence is stale.", "recommendation": "Refresh from records."}
        old_revision = "content:" + "1" * 64
        new_revision = "content:" + "2" * 64
        cycle = claude_reviewer.new_review_cycle(
            cycle_id="cycle-test", scope_kind="increment", increment_id="inc-1",
            assignment_reference=f"task:{self.flow}", target_revision_id=old_revision,
            scope_paths=["src"],
        )
        cycle = claude_reviewer.reserve_review_round(cycle, target_revision_id=old_revision,
                                                     verification_evidence_references=["VAL-OLD"])
        cycle["rounds"][0].update({"status": "completed", "outcome": "REVIEW_COMPLETED",
            "report_valid": True, "envelope_valid": True, "reviewer_status": "CHANGES_REQUESTED",
            "finding_ids": ["CF-1", "CF-2"], "report_reference": ".engineering/reports/first.json"})
        cycle["round_count"] = 1
        cycle["status"] = "finding_analysis_pending"
        report = {"decision": "CHANGES_REQUESTED", "summary": "Two issues found.",
                  "findings": [finding, managed_process]}
        self.runner.state = {
            "status": "ACTIVE", "phase": "INDEPENDENT_VERIFICATION", "updated_at": "",
            "workflow_id": self.flow, "scope": {}, "artifact_references": [],
            "baseline": {"commit": "b" * 40}, "target": {"target_id": "target-test"},
            "claude_review_cycles": [cycle], "pending_decisions": [],
            "orchestration": {"current_stage": "REVIEW", "iteration": 0, "max_iterations": 2,
                "final_revision_id": old_revision, "revision_manifest_reference": "manifest.json",
                "validation_results": [], "increments": [{"increment_id": "inc-1", "status": "reviewed"}]},
        }
        self.runner._save = lambda: None
        corrected = {"status": "COMPLETED", "summary": "Corrected the defect.",
            "changes": [{"path": "src/feature.txt", "content": "corrected\n"}],
            "finding_decisions": [{"finding_id": "CF-1", "decision": "ACCEPT",
                "reason": "The finding is supported by the code.", "evidence_references": []}]}
        followup = {"decision": "APPROVED", "summary": "Correction verified.", "findings": []}
        captured = {}

        def validate(_plan, _increment, revision_id):
            self.assertEqual(revision_id, new_revision)
            self.runner.state["orchestration"]["validation_results"].append({
                "increment_id": "inc-1", "revision_id": new_revision, "exit_code": 0,
                "evidence_id": "VAL-NEW",
            })

        def followup_review(_plan, _increment, revision_id, *, cycle, previous_findings, prior_decisions):
            captured.update({"revision_id": revision_id, "cycle": cycle,
                             "previous_findings": previous_findings, "prior_decisions": prior_decisions})
            return followup

        observed_runtime = {}

        def run_correction(_stage, _context, runtime, _schema, **_kwargs):
            observed_runtime["path"] = runtime
            self.assertTrue((runtime / "runtime-marker.txt").is_file())
            captured["codex_finding_ids"] = [item["id"] for item in _context["findings"]]
            return corrected

        def prepare_runtime(destination):
            (destination / "runtime-marker.txt").write_text("policy snapshot", encoding="utf-8")
            return {"runtime-marker.txt": "marker-hash"}

        controller_decisions = {
            "CF-2": {"finding_id": "CF-2", "decision": "ACCEPT", "reason": "Controller refreshed evidence.", "evidence_references": []},
        }
        with patch("orchestrator._runtime_bundle_files", side_effect=prepare_runtime), \
             patch("orchestrator._runtime_bundle_unchanged", return_value=True), \
             patch.object(self.runner, "_run_codex", side_effect=run_correction), \
             patch.object(self.runner, "_controller_finding_decision",
                          side_effect=lambda item, _increment: controller_decisions.get(item["id"])), \
             patch.object(self.runner, "_apply_changes"), \
             patch.object(self.runner, "_self_review"), \
             patch.object(self.runner, "_revision_manifest", return_value=(new_revision, {"revision_id": new_revision}, ".engineering/workflows/flow-test-1/revision-manifest-inc-1.json")), \
             patch.object(self.runner.store, "write", return_value={}), \
             patch.object(self.runner, "_validate", side_effect=validate), \
             patch.object(self.runner, "_finalize_revision", return_value=new_revision), \
             patch.object(self.runner, "_sync_process_evidence"), \
             patch.object(self.runner, "_review_increment", side_effect=followup_review):
            correction_increment = {**plan()["increments"][0], "artifact_paths": ["docs"]}
            result = self.runner._correct_and_reverify(plan(), correction_increment, report, cycle)
        self.assertEqual(result, followup)
        self.assertEqual(captured["codex_finding_ids"], ["CF-1"])
        self.assertTrue(observed_runtime["path"].is_absolute())
        self.assertEqual(captured["revision_id"], new_revision)
        self.assertEqual(captured["previous_findings"], [finding, managed_process])
        self.assertEqual({row["finding_id"] for row in captured["prior_decisions"]}, {"CF-1", "CF-2"})
        self.assertTrue(all(row["decision"] == "ACCEPT" for row in captured["prior_decisions"]))
        self.assertTrue(all(row["resolution_evidence_references"] == ["VAL-NEW"]
                            for row in captured["prior_decisions"]))

    def test_multiple_increments_are_controlled_by_the_orchestrator(self):
        increments = plan()["increments"] + [{
            "increment_id": "inc-2", "title": "Document feature", "code_paths": ["docs"],
            "artifact_paths": [], "revision_paths": ["docs"],
            "acceptance_criteria_ids": ["AC-1"], "validation_ids": ["tests"],
            "blocked_by_decision": False,
        }]
        document = plan(increments=increments)
        self.write_state(self.runner)
        invoked = []
        self.runner.initialize = lambda: None
        self.runner._planning = lambda runtime: document
        self.runner._review_plan_and_wait_for_approval = lambda current_plan, _runtime: current_plan
        self.runner._increment = lambda p, item, runtime: invoked.append(item["increment_id"])
        self.runner._finalize_revision = lambda p: "content:" + "c" * 64
        self.runner._review_and_gate = lambda p: None
        with patch("orchestrator._runtime_bundle_files", return_value={}), \
             patch("orchestrator._runtime_bundle_unchanged", return_value=True):
            result = self.runner.run()
        self.assertEqual(invoked, ["inc-1", "inc-2"])
        self.assertEqual(result["status"], "ACTIVE")

    def test_approved_refined_plan_refreshes_only_pending_increment_records(self):
        self.write_state(self.runner)
        self.runner.state["orchestration"]["increments"] = [{
            **plan()["increments"][0], "status": "pending", "requirement_version": 1}]
        revised_increment = {**plan()["increments"][0], "title": "Implement refined behavior",
            "code_paths": ["src/refined"], "revision_paths": ["src/refined"]}
        with patch.object(self.runner, "_save"):
            self.runner._activate_plan(plan(increments=[revised_increment]),
                f"{self.runner.store.directory}/refined-plan.json", 1)
        record = self.runner.state["orchestration"]["increments"][0]
        self.assertEqual(record["title"], "Implement refined behavior")
        self.assertEqual(record["code_paths"], ["src/refined"])
        self.assertEqual(record["status"], "pending")

    def test_plan_approval_blocks_every_increment_before_product_decisions(self):
        increments = [
            {**plan()["increments"][0], "increment_id": "inc-independent", "blocked_by_decision": False},
            {**plan()["increments"][0], "increment_id": "inc-dependent", "blocked_by_decision": True},
        ]
        document = plan(increments=increments)
        document["decision"] = "NEEDS_HUMAN_INPUT"
        document["decision_context"] = {"category": "functional_decision",
            "question": "Should clients be able to cancel a running job?",
            "proposal": "Keep cancellation out of scope for this release.",
            "alternatives": ["Add cancellation", "Defer cancellation"],
            "reason": "The API capability changes client behavior and processing guarantees.",
            "policy_or_skill_reference": None, "required_authority": "product owner",
            "interactive_resolution_allowed": False}
        self.write_state(self.runner)
        self.runner.state["pending_decisions"] = [{"decision_id": "PLAN-CLARIFICATION", "status": "open"}]
        invoked = []
        self.runner.initialize = lambda: None
        self.runner._save = lambda: None
        self.runner._planning = lambda _runtime: document
        def require_plan_approval(_plan, _runtime):
            self.runner.state["pending_decisions"].append({"decision_id": "PLAN-APPROVAL",
                "decision_category": "implementation_plan_approval", "requirement_version": 1,
                "status": "open"})
            self.runner.state["status"] = "WAITING_FOR_HUMAN"
            self.runner.state["phase"] = "PLANNING"
            self.runner.state["orchestration"]["current_stage"] = "PLAN_APPROVAL"
            return None
        self.runner._review_plan_and_wait_for_approval = require_plan_approval
        self.runner._increment = lambda _plan, item, _runtime: invoked.append(item["increment_id"])
        self.runner._finalize_revision = lambda _plan: self.fail("must wait before finalization")
        with patch("orchestrator._runtime_bundle_files", return_value={}):
            outcome = self.runner.run()
        self.assertEqual(invoked, [])
        self.assertEqual(outcome["status"], "WAITING_FOR_HUMAN")
        self.assertEqual(self.runner.state["orchestration"]["current_stage"], "PLAN_APPROVAL")

    def test_waiting_for_human_is_not_automatically_reactivated(self):
        pending = [{"decision_id": "PLAN-CLARIFICATION", "status": "open"}]
        self.write_state(self.runner, status="WAITING_FOR_HUMAN", stage="PLANNING", pending=pending)
        self.runner.initialize = lambda: None
        self.runner._planning = lambda runtime: self.fail("must not progress past open human decision")
        result = self.runner.run()
        self.assertEqual(result["status"], "WAITING_FOR_HUMAN")

    @staticmethod
    def functional_decision(decision_id="PLAN-CLARIFICATION"):
        return {
            "decision_id": decision_id, "workflow_id": "flow-test-1",
            "decision_category": "functional_decision", "stage": "PLANNING",
            "question": "Which contract should be implemented?", "proposal": "Use the proposed contract.",
            "reason": "The target instructions require explicit acceptance.",
            "policy_or_skill_reference": ".agents/skills/api/references/contract.md",
            "interactive_resolution_allowed": True, "response_history": [],
            "question_or_action": "Which contract should be implemented?",
            "impact": "Planning is blocked.", "recommended_option": "Use the proposed contract.",
            "alternatives": ["Provide another contract."], "required_authority": "task owner",
            "affected_phase": "PLANNING", "requested_at": orchestrator._now(),
            "created_at": orchestrator._now(), "status": "open",
        }

    def test_planning_persists_full_pending_decision_context(self):
        self.write_state(self.runner)
        self.runner.state.update({"instruction_records": [], "artifact_references": [],
                                  "pending_decisions": []})
        self.runner._save = lambda: None
        response = {
            "decision": "NEEDS_HUMAN_INPUT", "summary": "Choose the API contract.",
            "requirements": [], "acceptance_criteria": [], "risk_band": "medium",
            "architecture_context": [], "documentation_context": [], "capabilities_used": [],
            "increments": [], "dependencies": [], "blockers": [],
            "engineering_discovery": plan()["engineering_discovery"],
            "decision_context": {
                "category": "functional_decision", "question": "Which contract should be implemented?",
                "proposal": "Use the proposed contract.", "alternatives": ["Provide another contract."],
                "reason": "The target skill requires explicit acceptance.",
                "policy_or_skill_reference": ".agents/skills/api/references/contract.md",
                "required_authority": "task owner", "interactive_resolution_allowed": True,
            },
        }
        captured = {}
        self.runner._run_codex = lambda stage, context, *args, **kwargs: captured.update(
            {"stage": stage, "context": context}) or response
        result = self.runner._planning(Path(self.temp.name))
        self.assertIsNone(result)
        self.assertEqual(captured["stage"], "PLANNING")
        self.assertEqual([item["id"] for item in captured["context"]["configured_validations"]], ["tests"])
        pending = self.runner.state["pending_decisions"][0]
        self.assertEqual(pending["workflow_id"], self.flow)
        self.assertEqual(pending["stage"], "PLANNING")
        self.assertEqual(pending["question"], response["decision_context"]["question"])
        self.assertEqual(pending["policy_or_skill_reference"],
                         ".agents/skills/api/references/contract.md")
        self.assertEqual(pending["response_history"], [])
        self.assertEqual(self.runner.state["status"], "WAITING_FOR_HUMAN")

    def test_plan_reports_unknown_validation_ids_and_configured_choices(self):
        invalid = plan()
        invalid["increments"][0]["validation_ids"] = ["guessed-test-id"]
        with self.assertRaisesRegex(orchestrator.OrchestratorError,
                                   "guessed-test-id.*configured IDs.*tests"):
            orchestrator._plan_valid(invalid, orchestrator.validate_config(config()))

    def test_response_is_persisted_with_declared_provenance_and_resumes_same_plan(self):
        store = orchestrator.WorkflowStore(self.target, self.flow)
        decision = self.functional_decision()
        state = {
            "workflow_id": self.flow, "status": "WAITING_FOR_HUMAN", "phase": "IMPLEMENTATION",
            "pending_decisions": [decision], "updated_at": orchestrator._now(),
            "orchestration": {
                "branch": "feature/task", "current_stage": "IMPLEMENTATION",
                "plan_reference": f"{store.directory}/plan.json", "task_sha256": "a" * 64,
                "config_sha256": "b" * 64, "max_iterations": 2, "iteration": 0,
                "increments": [{"increment_id": "inc-1", "status": "complete"}],
                "attempts": [{"stage": "PLANNING", "exit_code": 0}],
                "validation_results": [], "review_results": [], "correction_status": [],
                "preexisting_hashes": [], "commit_authorization_fingerprint": None,
            },
        }
        with patch("safe_writer._run_preflight", return_value={
                "overall_result": "READY", "target_path": str(self.target.resolve())}):
            store.ensure_directory()
            store.save(state)
            after_exit = orchestrator.WorkflowStore(self.target, self.flow)
            loaded = after_exit.read_state()
            receipt = orchestrator.record_decision_response(
                after_exit, loaded, "PLAN-CLARIFICATION", "APPROVE",
                "I approve the proposed contract.", operator_identity="local OS account: test-user",
                source_reference="interactive local CLI",
            )
            resumed_state = orchestrator.WorkflowStore(self.target, self.flow).read_state()
        self.assertTrue(receipt["resolved"])
        self.assertEqual(receipt["identity_assurance"], "declared")
        self.assertEqual(resumed_state["pending_decisions"][0]["response_history"][0]["source"],
                         "interactive_local_cli")
        self.assertEqual(resumed_state["pending_decisions"][0]["resolved_by_identity_assurance"], "declared")
        self.assertEqual(resumed_state["status"], "ACTIVE")

        resumed = orchestrator.Orchestrator(
            str(self.target), "Add a feature", config(), self.flow,
            process_runner=self.runner.process_runner,
        )
        resumed.state = resumed_state
        resumed.initialize = lambda: None
        resumed._run_codex = lambda *args, **kwargs: self.fail("a persisted plan must not be planned again")
        resumed._review_plan_and_wait_for_approval = lambda current_plan, _runtime: current_plan
        resumed._finalize_revision = lambda _plan: "content:" + "c" * 64
        resumed._review_and_gate = lambda _plan: None
        with patch.object(resumed.store, "read_json", return_value=plan()), \
             patch("orchestrator._runtime_bundle_files", return_value={}), \
             patch("orchestrator._runtime_bundle_unchanged", return_value=True):
            result = resumed.run()
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(resumed.state["orchestration"]["attempts"], [
            {"stage": "PLANNING", "exit_code": 0},
        ])
        self.assertEqual(resumed.state["orchestration"]["current_stage"], "IMPLEMENTATION")

    def test_response_rejects_unknown_decision_ids_and_preserves_existing_state(self):
        self.write_state(self.runner, status="WAITING_FOR_HUMAN", pending=[self.functional_decision()])
        before = json.dumps(self.runner.state, sort_keys=True)
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "missing, ambiguous"):
            orchestrator.record_decision_response(
                self.runner.store, self.runner.state, "UNRELATED", "APPROVE", "yes",
                operator_identity="local account", source_reference="interactive local CLI")
        self.assertEqual(json.dumps(self.runner.state, sort_keys=True), before)

    def test_nonfunctional_interactive_response_cannot_authorize_high_risk_approval(self):
        pending = self.functional_decision()
        pending.update({"decision_category": "high_risk_approval",
                        "interactive_resolution_allowed": False,
                        "policy_or_skill_reference": None})
        self.write_state(self.runner, status="WAITING_FOR_HUMAN", pending=[pending])
        saved = {}
        self.runner.store.save = lambda state: saved.update(json.loads(json.dumps(state)))
        receipt = orchestrator.record_decision_response(
            self.runner.store, self.runner.state, "PLAN-CLARIFICATION", "APPROVE",
            "I approve.", operator_identity="local account", source_reference="interactive local CLI")
        self.assertFalse(receipt["resolved"])
        self.assertEqual(self.runner.state["status"], "WAITING_FOR_HUMAN")
        self.assertEqual(self.runner.state["pending_decisions"][0]["status"], "open")
        self.assertEqual(saved["pending_decisions"][0]["response_history"][0]["identity_assurance"], "declared")

    def test_reject_and_request_changes_are_recorded_as_functional_direction(self):
        for action, text in (("REJECT", "Do not use that shape."), ("REQUEST_CHANGES", "Make the range nullable.")):
            pending = self.functional_decision()
            state = {"workflow_id": self.flow, "status": "WAITING_FOR_HUMAN",
                     "pending_decisions": [pending]}
            saved = {}
            store = type("Store", (), {
                "directory": f".engineering/workflows/{self.flow}",
                "save": lambda _self, value: saved.update(json.loads(json.dumps(value))),
            })()
            result = orchestrator.record_decision_response(
                store, state, "PLAN-CLARIFICATION", action, text,
                operator_identity="conversation participant", source_reference="conversation approval follow-up")
            self.assertTrue(result["resolved"])
            self.assertEqual(saved["pending_decisions"][0]["resolution_action"], action)
            self.assertEqual(saved["pending_decisions"][0]["resolution"], text)
            self.assertEqual(saved["pending_decisions"][0]["response_history"][0]["response"], text)
            self.assertEqual(saved["status"], "ACTIVE")

    def test_nonapproval_response_to_partially_blocked_plan_does_not_release_dependent_work(self):
        decision = self.functional_decision()
        state = {"workflow_id": self.flow, "status": "WAITING_FOR_HUMAN",
            "pending_decisions": [decision], "orchestration": {
                "plan_reference": f".engineering/workflows/{self.flow}/plan.json",
                "increments": [{"increment_id": "inc-dependent", "status": "pending",
                    "blocked_by_decision": True}]}}
        saved = {}
        store = type("Store", (), {"directory": f".engineering/workflows/{self.flow}",
            "save": lambda _self, value: saved.update(json.loads(json.dumps(value)))})()
        result = orchestrator.record_decision_response(store, state, "PLAN-CLARIFICATION", "ALTERNATIVE",
            "Support cancellation after item completion only.", operator_identity="conversation participant",
            source_reference="conversation response")
        self.assertFalse(result["resolved"])
        self.assertTrue(result["requires_requirement_update"])
        self.assertEqual(saved["status"], "WAITING_FOR_HUMAN")
        self.assertEqual(saved["pending_decisions"][0]["status"], "open")

    def test_cli_lists_pending_decisions_and_maps_response_actions(self):
        output = StringIO()
        listing = {"workflow_id": self.flow, "workflow_status": "WAITING_FOR_HUMAN",
                   "pending_decisions": [self.functional_decision()]}
        with patch.object(orchestrator, "list_pending_decisions", return_value=listing), redirect_stdout(output):
            self.assertEqual(orchestrator.main([
                "--target", str(self.target), "--workflow-id", self.flow, "--list-pending-decisions"
            ]), 0)
        self.assertEqual(json.loads(output.getvalue())["pending_decisions"][0]["decision_id"],
                         "PLAN-CLARIFICATION")

        output = StringIO()
        receipt = {"workflow_id": self.flow, "decision_id": "PLAN-CLARIFICATION",
                   "decision_status": "resolved", "workflow_status": "ACTIVE", "resolved": True,
                   "identity_assurance": "declared"}
        with patch.object(orchestrator, "submit_interactive_response", return_value=receipt) as submit, \
             patch.object(orchestrator.getpass, "getuser", return_value="codex-local"), \
             redirect_stdout(output):
            self.assertEqual(orchestrator.main([
                "--target", str(self.target), "--workflow-id", self.flow,
                "--respond-decision", "PLAN-CLARIFICATION", "--response-action", "changes",
                "--response", "Please revise the proposal.",
            ]), 0)
        self.assertEqual(submit.call_args.args[3], "REQUEST_CHANGES")
        self.assertIn("identity_assurance", json.loads(output.getvalue()))

    def test_cli_passes_requirement_update_reason_to_existing_workflow(self):
        config_file = Path(self.temp.name) / "config.json"
        config_file.write_text(json.dumps(config(branch="feature/task")), encoding="utf-8")
        constructed = {}

        class FakeOrchestrator:
            def __init__(self, *args, **kwargs):
                constructed.update({"args": args, "kwargs": kwargs})

            def run(self):
                return {"status": "ACTIVE", "workflow_id": "flow-test-1"}

        output = StringIO()
        with patch.object(orchestrator, "Orchestrator", FakeOrchestrator), redirect_stdout(output):
            result = orchestrator.main(["--target", str(self.target), "--branch", "feature/task",
                "--task", "Revise the requirement.", "--config", str(config_file),
                "--workflow-id", self.flow, "--update-requirements", "--update-reason",
                "The approved architecture decision changed."])
        self.assertEqual(result, 3)
        self.assertEqual(constructed["kwargs"]["update_requirements_reason"],
                         "The approved architecture decision changed.")

    def test_blocked_workflow_gets_one_auditable_resume_attempt(self):
        blocker = {"reason": "temporary", "affected_phase": "TESTING", "recorded_at": "now",
                   "safe_recovery_path": "retry"}
        self.write_state(self.runner, status="BLOCKED", stage="TESTING")
        self.runner.state["blocker"] = blocker
        self.runner.initialize = lambda: None
        self.runner._save = lambda: None
        self.runner._planning = lambda runtime: None
        blocker_hash = orchestrator._blocker_fingerprint(blocker)
        context = {"workflow_id": self.flow, "stage": "TESTING", "blocker_sha256": blocker_hash,
            "requirement_version": 1, "task_sha256": "a" * 64, "config_sha256": "b" * 64,
            "target_baseline_commit": None, "revision_id": None,
            "harness_orchestrator_sha256": "c" * 64, "runtime_bundle_sha256": "d" * 64,
            "codex": {"cli_version": "codex test", "model": "model test", "provider": "provider test",
                "python_runtime": "3.x"}}
        with patch.object(self.runner, "_recovery_context", return_value=context), \
             patch("orchestrator._runtime_bundle_files", return_value={}):
            result = self.runner.run()
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(self.runner.state["orchestration"]["attempts"][0]["stage"], "RESUME")
        self.assertRegex(self.runner.state["orchestration"]["attempts"][0]["recovery_context_sha256"], r"^[a-f0-9]{64}$")
        self.assertNotIn("blocker", self.runner.state)

    def test_repeated_same_blocker_does_not_loop_on_resume(self):
        blocker = {"reason": "temporary", "affected_phase": "TESTING", "recorded_at": "now",
                   "safe_recovery_path": "retry"}
        blocker_hash = orchestrator._blocker_fingerprint(blocker)
        self.write_state(self.runner, status="BLOCKED", stage="TESTING", attempts=[
            {"stage": "RESUME", "blocker_sha256": blocker_hash},
        ])
        self.runner.state["blocker"] = blocker
        self.runner.initialize = lambda: None
        self.runner._planning = lambda runtime: self.fail("repeated blocker must not retry")
        fixed_context = {"workflow_id": self.flow, "stage": "TESTING", "blocker_sha256": blocker_hash,
            "requirement_version": 1, "task_sha256": "a" * 64, "config_sha256": "b" * 64,
            "target_baseline_commit": None, "revision_id": None,
            "harness_orchestrator_sha256": "c" * 64, "runtime_bundle_sha256": "d" * 64,
            "codex": {"cli_version": "codex test", "model": "model test", "provider": "provider test",
                "python_runtime": "3.x"}}
        with patch.object(self.runner, "_recovery_context", return_value=fixed_context):
            result = self.runner.run()
        self.assertEqual(result["status"], "BLOCKED")

    def test_legacy_repeated_blocker_retries_once_after_execution_context_changes(self):
        blocker = {"reason": "Self-review findings remain", "affected_phase": "IMPLEMENTATION",
            "recorded_at": "now", "safe_recovery_path": "review the changed harness context"}
        blocker_hash = orchestrator._blocker_fingerprint(blocker)
        prior_resume = {"stage": "RESUME", "blocker_sha256": blocker_hash}
        prior_review = {"stage": "SELF_REVIEW", "trace": {
            "harness_orchestrator_sha256": "1" * 64, "runtime_bundle_sha256": "2" * 64,
            "reviewed_revision_id": "content:" + "a" * 64}}
        self.write_state(self.runner, status="BLOCKED", stage="SELF_REVIEW", attempts=[prior_resume, prior_review])
        self.runner.state["orchestration"]["current_stage"] = "SELF_REVIEW"
        self.runner.state["orchestration"]["current_increment_id"] = "inc-1"
        self.runner.state["blocker"] = blocker
        self.runner.initialize = lambda: None
        self.runner._save = lambda: None
        self.runner._planning = lambda runtime: None
        current = {"workflow_id": self.flow, "stage": "SELF_REVIEW", "blocker_sha256": blocker_hash,
            "requirement_version": 1, "task_sha256": "a" * 64, "config_sha256": "b" * 64,
            "target_baseline_commit": "base", "revision_id": "content:" + "a" * 64,
            "harness_orchestrator_sha256": "3" * 64, "runtime_bundle_sha256": "4" * 64,
            "codex": {"cli_version": "codex test", "model": "model test", "provider": "provider test",
                "python_runtime": "3.x"}}
        with patch.object(self.runner, "_recovery_context", return_value=current), \
             patch("orchestrator._runtime_bundle_files", return_value={}):
            result = self.runner.run()
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(self.runner.state["orchestration"]["attempts"][-1]["resume_basis"], "changed_execution_context")
        self.assertEqual(self.runner.state["orchestration"]["attempts"][-1]["recovery_context"], current)
        self.assertEqual(self.runner.state["orchestration"]["attempts"][0], prior_resume)
        self.runner.state["status"] = "BLOCKED"
        self.runner.state["blocker"] = blocker
        with patch.object(self.runner, "_recovery_context", return_value=current):
            result = self.runner.run()
        self.assertEqual(result["status"], "BLOCKED")

    def test_repeated_validation_blocker_resumes_only_after_revision_changes(self):
        blocker = {"reason": "Required validation tests failed (exit 1)", "affected_phase": "TESTING",
                   "recorded_at": "now", "safe_recovery_path": "correct the code and revalidate"}
        blocker_hash = orchestrator._blocker_fingerprint(blocker)
        previous = "content:" + "a" * 64
        current = "content:" + "b" * 64
        plan_reference = f"{self.runner.store.directory}/plan.json"
        self.runner.state = {
            "status": "BLOCKED", "phase": "TESTING", "workflow_id": self.flow,
            "blocker": blocker, "pending_decisions": [],
            "orchestration": {
                "current_stage": "TESTING", "plan_reference": plan_reference,
                "attempts": [{"stage": "RESUME", "blocker_sha256": blocker_hash}],
                "validation_results": [{"increment_id": "inc-1", "exit_code": 1, "revision_id": previous}],
            },
        }
        with patch.object(self.runner.store, "read_json", return_value={}), \
             patch("orchestrator._plan_valid", return_value={"increments": [{"increment_id": "inc-1"}]}), \
             patch.object(self.runner, "_revision_manifest", return_value=(current, {}, "manifest.json")):
            self.assertEqual(self.runner._changed_validation_revision(blocker), (previous, current))
            with patch.object(self.runner, "initialize", lambda: None), \
                 patch.object(self.runner, "_save", lambda: None), \
                 patch.object(self.runner, "_planning", return_value=None), \
                 patch("orchestrator._runtime_bundle_files", return_value={}):
                result = self.runner.run()

        self.assertEqual(result["status"], "ACTIVE")
        resume = self.runner.state["orchestration"]["attempts"][-1]
        self.assertEqual(resume["resume_basis"], "corrected_validation_revision")
        self.assertEqual(resume["previous_revision_id"], previous)
        self.assertEqual(resume["current_revision_id"], current)

    def test_repeated_validation_blocker_stays_blocked_without_revision_change(self):
        blocker = {"reason": "Required validation tests failed (exit 1)", "affected_phase": "TESTING",
                   "recorded_at": "now", "safe_recovery_path": "correct the code and revalidate"}
        blocker_hash = orchestrator._blocker_fingerprint(blocker)
        revision = "content:" + "a" * 64
        plan_reference = f"{self.runner.store.directory}/plan.json"
        self.runner.state = {
            "status": "BLOCKED", "phase": "TESTING", "workflow_id": self.flow,
            "blocker": blocker, "pending_decisions": [],
            "orchestration": {
                "current_stage": "TESTING", "plan_reference": plan_reference,
                "attempts": [{"stage": "RESUME", "blocker_sha256": blocker_hash}],
                "validation_results": [{"increment_id": "inc-1", "exit_code": 1, "revision_id": revision}],
            },
        }
        with patch.object(self.runner.store, "read_json", return_value={}), \
             patch("orchestrator._plan_valid", return_value={"increments": [{"increment_id": "inc-1"}]}), \
             patch.object(self.runner, "_revision_manifest", return_value=(revision, {}, "manifest.json")):
            with patch.object(self.runner, "initialize", lambda: None), \
                 patch.object(self.runner, "_planning", side_effect=AssertionError("must remain blocked")):
                result = self.runner.run()

        self.assertEqual(result["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()

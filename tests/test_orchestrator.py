import json
import subprocess
import sys
import tempfile
import unittest
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


def plan(*, increments=None, risk="medium"):
    return {
        "decision": "READY", "summary": "Plan is ready.", "risk_band": risk,
        "requirements": [{"id": "REQ-1", "text": "Implement the requested behavior.", "source": "task"}],
        "acceptance_criteria": [{"id": "AC-1", "text": "The behavior is verified.", "source": "task"}],
        "architecture_context": [], "documentation_context": [], "capabilities_used": [],
        "increments": increments or [{
            "increment_id": "inc-1", "title": "Implement feature", "code_paths": ["src"],
            "artifact_paths": [], "revision_paths": ["src"],
            "acceptance_criteria_ids": ["AC-1"], "validation_ids": ["tests"],
        }],
    }


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

    def test_validation_command_policy_rejects_shells_and_implicit_restore(self):
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "prohibited"):
            orchestrator._run_command(["bash", "-c", "echo unsafe"], self.target, 1,
                                      lambda *args, **kwargs: self.fail("must not execute"))
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "--no-restore"):
            orchestrator._run_command(["dotnet", "test"], self.target, 1,
                                      lambda *args, **kwargs: self.fail("must not execute"))

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

    def test_plan_cannot_expand_user_authorized_code_scope(self):
        bad = plan()
        bad["increments"][0]["code_paths"] = ["outside"]
        bad["increments"][0]["revision_paths"] = ["outside"]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "authorized code paths"):
            orchestrator._plan_valid(bad, orchestrator.validate_config(config()))

    def test_stored_plan_may_retain_only_the_controller_owned_artifact_path(self):
        stored = plan()
        stored["increments"][0]["artifact_paths"] = [".engineering/workflows/flow-test-1"]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "authorized artifact paths"):
            orchestrator._plan_valid(stored, orchestrator.validate_config(config()))
        validated = orchestrator._plan_valid(stored, orchestrator.validate_config(config()),
            controller_artifact_paths=[".engineering/workflows/flow-test-1"])
        self.assertEqual(validated["increments"][0]["artifact_paths"],
                         [".engineering/workflows/flow-test-1"])

    def test_code_file_extension_is_enforced_before_any_write(self):
        self.runner.config["code_file_extensions"] = [".cs"]
        self.runner.state = {"orchestration": {"preexisting_hashes": []}}
        increment = {"code_paths": ["src"], "artifact_paths": []}
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "extension is not authorized"):
            self.runner._apply_changes([{"path": "src/README.md", "content": "no\n"}], increment)
        self.assertFalse((self.target / "README.md").exists())

    def test_stage_response_rejects_unknown_fields_and_bad_correction_decisions(self):
        value = {"status": "COMPLETED", "summary": "Done", "changes": [], "extra": True}
        with self.assertRaises(orchestrator.OrchestratorError):
            orchestrator._stage_valid(value)
        value = {"status": "COMPLETED", "summary": "Done", "changes": [], "finding_decisions": [
            {"finding_id": "F-1", "decision": "APPROVE", "reason": "ok", "evidence_references": []}
        ]}
        with self.assertRaises(orchestrator.OrchestratorError):
            orchestrator._stage_valid(value, correction=True)

    def test_implementation_cannot_modify_a_preexisting_user_change(self):
        existing = self.target / "src" / "user.txt"
        existing.write_text("keep this\n", encoding="utf-8")
        self.runner.state = {"orchestration": {"preexisting_hashes": [{"path": "src/user.txt"}]}}
        increment = {"code_paths": ["src"], "artifact_paths": []}
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "pre-existing user change"):
            self.runner._apply_changes([{"path": "src/user.txt", "content": "replace\n"}], increment)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep this\n")

    def test_new_parent_directory_must_be_inside_an_explicit_write_scope(self):
        self.runner.state = {"orchestration": {"preexisting_hashes": []}}
        increment = {"code_paths": ["src/new/file.cs"], "artifact_paths": []}
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "planned write scope"):
            self.runner._apply_changes([{"path": "src/new/file.cs", "content": "class C {}\n"}], increment)
        self.assertFalse((self.target / "src" / "new").exists())

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
        preflight_reference = f"{runner.store.directory}/preflight.json"
        self.assertIn(preflight_reference, runner.state["artifact_references"])
        self.assertEqual(runner.store.read_json(preflight_reference)["overall_result"], "READY")
        self.assertEqual(runner.state["baseline"]["commit"],
                         subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip())
        changed_config = config(branch="feature/task", claude_budget_usd=1.0)
        resumed = orchestrator.Orchestrator(str(target), "Add the requested backend behavior.",
            changed_config, "flow-initialize")
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "configuration changed"):
            resumed.initialize()

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
        with patch("safe_writer._run_preflight", return_value={"overall_result": "READY", "target_path": str(self.target.resolve())}):
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
        self.runner.state = {
            "workflow_id": self.flow, "status": "ACTIVE", "phase": "INDEPENDENT_VERIFICATION",
            "target": {"target_id": "target-test"}, "baseline": {"commit": "b" * 40},
            "scope": {"increment_id": "inc-1"}, "artifact_references": [evidence_reference, manifest_reference],
            "authorizations": [], "claude_review_cycles": [], "pending_decisions": [],
            "authorization_verifier": {"identity": "primary Codex session", "role": "Workflow Owner",
                "identity_assurance": "declared", "source_reference": f"task:{self.flow}"},
            "orchestration": {"revision_manifest_reference": manifest_reference,
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
             patch("orchestrator.claude.run_claude_review", side_effect=adapter):
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

    def test_accepted_claude_finding_is_corrected_verified_and_sent_for_followup(self):
        import claude_reviewer

        finding = {"id": "CF-1", "severity": "medium", "file": "src/feature.txt",
                   "description": "A defect needs correction.", "recommendation": "Correct it."}
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
            "finding_ids": ["CF-1"], "report_reference": ".engineering/reports/first.json"})
        cycle["round_count"] = 1
        cycle["status"] = "finding_analysis_pending"
        report = {"decision": "CHANGES_REQUESTED", "summary": "One issue found.", "findings": [finding]}
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
            return corrected

        def prepare_runtime(destination):
            (destination / "runtime-marker.txt").write_text("policy snapshot", encoding="utf-8")
            return {"runtime-marker.txt": "marker-hash"}

        with patch("orchestrator._runtime_bundle_files", side_effect=prepare_runtime), \
             patch("orchestrator._runtime_bundle_unchanged", return_value=True), \
             patch.object(self.runner, "_run_codex", side_effect=run_correction), \
             patch.object(self.runner, "_apply_changes"), \
             patch.object(self.runner, "_revision_manifest", return_value=(new_revision, {"revision_id": new_revision}, ".engineering/workflows/flow-test-1/revision-manifest-inc-1.json")), \
             patch.object(self.runner.store, "write", return_value={}), \
             patch.object(self.runner, "_validate", side_effect=validate), \
             patch.object(self.runner, "_finalize_revision", return_value=new_revision), \
             patch.object(self.runner, "_review_increment", side_effect=followup_review):
            result = self.runner._correct_and_reverify(plan(), plan()["increments"][0], report, cycle)
        self.assertEqual(result, followup)
        self.assertTrue(observed_runtime["path"].is_absolute())
        self.assertEqual(captured["revision_id"], new_revision)
        self.assertEqual(captured["previous_findings"], [finding])
        self.assertEqual(captured["prior_decisions"][0]["decision"], "ACCEPT")
        self.assertEqual(captured["prior_decisions"][0]["resolution_evidence_references"], ["VAL-NEW"])

    def test_multiple_increments_are_controlled_by_the_orchestrator(self):
        increments = plan()["increments"] + [{
            "increment_id": "inc-2", "title": "Document feature", "code_paths": ["docs"],
            "artifact_paths": [], "revision_paths": ["docs"],
            "acceptance_criteria_ids": ["AC-1"], "validation_ids": ["tests"],
        }]
        document = plan(increments=increments)
        self.write_state(self.runner)
        invoked = []
        self.runner.initialize = lambda: None
        self.runner._planning = lambda runtime: document
        self.runner._increment = lambda p, item, runtime: invoked.append(item["increment_id"])
        self.runner._finalize_revision = lambda p: "content:" + "c" * 64
        self.runner._review_and_gate = lambda p: None
        with patch("orchestrator._runtime_bundle_files", return_value={}), \
             patch("orchestrator._runtime_bundle_unchanged", return_value=True):
            result = self.runner.run()
        self.assertEqual(invoked, ["inc-1", "inc-2"])
        self.assertEqual(result["status"], "ACTIVE")

    def test_waiting_for_human_is_not_automatically_reactivated(self):
        pending = [{"decision_id": "PLAN-CLARIFICATION", "status": "open"}]
        self.write_state(self.runner, status="WAITING_FOR_HUMAN", stage="PLANNING", pending=pending)
        self.runner.initialize = lambda: None
        self.runner._planning = lambda runtime: self.fail("must not progress past open human decision")
        result = self.runner.run()
        self.assertEqual(result["status"], "WAITING_FOR_HUMAN")

    def test_blocked_workflow_gets_one_auditable_resume_attempt(self):
        blocker = {"reason": "temporary", "affected_phase": "TESTING", "recorded_at": "now",
                   "safe_recovery_path": "retry"}
        self.write_state(self.runner, status="BLOCKED", stage="TESTING")
        self.runner.state["blocker"] = blocker
        self.runner.initialize = lambda: None
        self.runner._save = lambda: None
        self.runner._planning = lambda runtime: None
        with patch("orchestrator._runtime_bundle_files", return_value={}):
            result = self.runner.run()
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(self.runner.state["orchestration"]["attempts"][0]["stage"], "RESUME")
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
        result = self.runner.run()
        self.assertEqual(result["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()

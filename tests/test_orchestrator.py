import json
import os
import subprocess
import sys
import tempfile
import unittest
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
        _auth, _remaining, denied = self.runner._persistent_claude_authorization(["private/other.txt"])
        self.assertIn("outside the persistent Claude authorization", denied)
        self.runner.state["orchestration"]["claude_authorization_uses"].append({
            "authorization_id": auth["authorization_id"], "status": "REVIEW_COMPLETED",
            "invocation_attempted": True, "cost_usd": 0.75})
        _auth, remaining, error = self.runner._persistent_claude_authorization(["src/module.cs"])
        self.assertIsNone(error)
        self.assertEqual(remaining, 1.25)
        self.runner.state["orchestration"]["claude_authorization_uses"][0]["cost_usd"] = None
        _auth, _remaining, denied = self.runner._persistent_claude_authorization(["src/module.cs"])
        self.assertIn("unknown cost", denied)

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

    def test_plan_validation_preserves_unique_items_after_provider_schema_simplification(self):
        bad = plan()
        bad["increments"][0]["code_paths"] = ["src", "src"]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "code_paths values must be unique"):
            orchestrator._plan_valid(bad, orchestrator.validate_config(config()))
        bad = plan()
        bad["increments"][0]["validation_ids"] = ["tests", "tests"]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "validation_ids values must be unique"):
            orchestrator._plan_valid(bad, orchestrator.validate_config(config()))

    def test_provider_output_schemas_omit_unsupported_strict_schema_keywords(self):
        unsupported = {'"uniqueItems"', '"minItems"', '"minLength"', '"pattern"', '"allOf"', '"if"', '"then"'}
        for name in ("orchestrator-plan.schema.json", "orchestrator-stage-result.schema.json",
                     "orchestrator-self-review.schema.json"):
            encoded = json.dumps(json.loads((ROOT / "harness" / "schemas" / name).read_text()))
            for keyword in unsupported:
                self.assertNotIn(keyword, encoded, f"{keyword} is unsupported by the provider's strict schema")

    def test_plan_cannot_expand_user_authorized_code_scope(self):
        bad = plan()
        bad["increments"][0]["code_paths"] = ["outside"]
        bad["increments"][0]["revision_paths"] = ["outside"]
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "authorized code paths"):
            orchestrator._plan_valid(bad, orchestrator.validate_config(config()))

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
        changed_task = orchestrator.Orchestrator(str(target), "A materially revised task.",
            config(branch="feature/task"), "flow-initialize")
        with self.assertRaisesRegex(orchestrator.OrchestratorError, "different task text"):
            changed_task.initialize()

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
        plan_path = f"{self.runner.store.directory}/plan-v2.json"
        wrapper = {"artifact_schema_version": 1, "requirement_version": 2,
            "task_sha256": task_hash, "config_sha256": config_hash, "plan": current_plan}
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

    def test_self_review_corrects_findings_and_requires_a_passing_followup(self):
        increment = plan()["increments"][0]
        self.runner.state = {"scope": {"increment_id": "inc-1", "included_paths": ["src"]},
            "pending_decisions": [], "instruction_records": [],
            "orchestration": {"current_requirement_version": 1,
                "requirement_history": [{"version": 1, "task_reference": "requirements-v1.json", "task_sha256": "a" * 64}],
                "invalidations": [], "self_reviews": [], "increments": [{"increment_id": "inc-1", "changed_paths": ["src/a.py"]}],
                "attempts": []}}
        self.runner._save = lambda: None
        finding = {"id": "SELF-1", "severity": "medium", "file": "src/a.py",
            "description": "Missing edge case.", "recommendation": "Handle the edge case."}
        responses = iter([
            {"decision": "CORRECTED", "summary": "Found and corrected a gap.", "findings": [finding],
             "changes": [{"path": "src/a.py", "content": "corrected\n"}]},
            {"decision": "PASS", "summary": "Correction verified.", "findings": [], "changes": []},
        ])
        contexts = []
        with patch.object(self.runner, "_set_stage"), \
             patch.object(self.runner, "_revision_manifest", side_effect=[
                 ("content:" + "a" * 64, {}, "manifest-before.json"),
                 ("content:" + "b" * 64, {}, "manifest-after.json")]), \
             patch.object(self.runner, "_run_codex", side_effect=lambda _stage, context, *_args, **_kwargs:
                          contexts.append(json.loads(json.dumps(context))) or next(responses)), \
             patch.object(self.runner, "_apply_changes") as apply_changes:
            self.runner._self_review(plan(), increment, Path(self.temp.name))
        apply_changes.assert_called_once()
        self.assertEqual(len(contexts), 2)
        self.assertEqual(contexts[0]["review_mode"], "self-review; not independent review or approval")
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
        self.assertFalse(use["invocation_attempted"])

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
             patch.object(self.runner, "_self_review"), \
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

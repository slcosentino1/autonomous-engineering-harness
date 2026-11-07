import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "harness"))

from claude_reviewer import (  # noqa: E402
    AILY_EXECUTABLE,
    ReviewError,
    apply_review_cycle_to_workflow_state,
    _extract_report,
    _extract_report_diagnostics,
    _capture_diff,
    _authorized_review_paths,
    _aily_read_only_command,
    _git_environment,
    _parse_usd_limit,
    _recorded_review_spend,
    _read_context,
    _read_verified_evidence_artifacts,
    _review_manifest,
    _validate_request,
    authorization_scope_fingerprint,
    build_review_evidence_record,
    finalize_review_cycle,
    new_review_cycle,
    record_finding_decision,
    record_review_result,
    recover_uncompleted_review_round,
    reserve_review_round,
    run_claude_review,
    run_aily_connection_check,
    stable_review_cycle_id,
    validate_reviewer_report,
)


REVISION_1 = "content:" + "a" * 64
REVISION_2 = "content:" + "b" * 64


def clean_env():
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def git(root, *args):
    return subprocess.run(
        ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-C", str(root), *args],
        env=clean_env(), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode().strip()


class ClaudeReviewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-review-test-")
        self.base = Path(self.temp.name)
        self.target = self.base / "target"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q", str(self.target)], env=clean_env(), check=True)
        git(self.target, "config", "user.name", "Review Test")
        git(self.target, "config", "user.email", "review-test@example.invalid")
        (self.target / "src").mkdir()
        (self.target / "tests").mkdir()
        (self.target / "docs").mkdir()
        (self.target / "AGENTS.md").write_text("Target instructions: keep changes scoped.\n")
        (self.target / ".gitignore").write_text("src/ignored.py\n")
        (self.target / "src" / "app.py").write_text("def value():\n    return 1\n")
        (self.target / "tests" / "test_app.py").write_text("def test_value():\n    assert value() == 1\n")
        (self.target / "docs" / "architecture.md").write_text("The app returns one.\n")
        git(self.target, "add", ".")
        git(self.target, "commit", "-m", "baseline", "-q")
        self.baseline = git(self.target, "rev-parse", "HEAD")
        (self.target / "src" / "app.py").write_text("def value():\n    return 2\n")
        (self.target / "docs" / "architecture.md").write_text("The app returns two.\n")
        self.scope_paths = ["docs/architecture.md", "src/app.py", "tests/test_app.py"]
        self.context_paths = ["AGENTS.md", "docs/architecture.md", "src/app.py", "tests/test_app.py"]
        self.workflow_root = self.target / ".engineering" / "workflows"
        self.workflow_dir = self.workflow_root / "workflow-1"
        (self.workflow_dir / "evidence").mkdir(parents=True)
        (self.workflow_dir / "reports").mkdir()
        fake_aily = self.base / "aily"
        fake_aily.write_text("#!/bin/sh\nexit 0\n")
        fake_aily.chmod(0o755)
        self.fake_aily = str(fake_aily)

    def tearDown(self):
        self.temp.cleanup()

    def cycle(self, revision=REVISION_1):
        result = new_review_cycle(
            cycle_id=stable_review_cycle_id(str(self.target.resolve()), "INC-1"), scope_kind="increment", increment_id="INC-1",
            assignment_reference="ASSIGN-1", target_revision_id=revision,
            scope_paths=self.scope_paths,
        )
        return result

    def request(self, cycle, revision=None, previous_findings=None, decisions=None):
        revision = revision or cycle["target_revision_id"]
        evidence_artifacts = []
        for evidence_id, result in (("EV-TEST-1", "All relevant tests passed"),
                                    ("EV-TEST-2", "Correction verification passed")):
            relative = f".engineering/workflows/workflow-1/evidence/{evidence_id}.json"
            evidence_record = {
                "evidence_id": evidence_id, "target_id": "test-target-1", "increment_id": "INC-1",
                "kind": "test", "description": result,
                "revision": {"revision_id": revision},
            }
            raw = json.dumps(evidence_record, sort_keys=True).encode("utf-8")
            (self.target / relative).write_bytes(raw)
            evidence_artifacts.append({
                "evidence_id": evidence_id, "artifact_reference": relative,
                "sha256": hashlib.sha256(raw).hexdigest(), "media_type": "application/json",
            })
        permitted_paths = _authorized_review_paths(
            self.target, cycle["scope_paths"], self.context_paths,
            [row["artifact_reference"] for row in evidence_artifacts],
        )
        now = datetime.now(timezone.utc)
        iso = lambda value: value.isoformat(timespec="seconds").replace("+00:00", "Z")
        authorization = {
            "authorization_id": "AUTH-EXTERNAL-1", "operation": "contact_external_services",
            "resource": "Claude through Aily", "scope": "One read-only review for this target workflow.",
            "target_id": "test-target-1", "workflow_id": "workflow-1", "increment_id": "INC-1",
            "assignment_reference": "ASSIGN-1", "review_id": "review-ASSIGN-1",
            "reviewer": "Claude through Aily", "reviewer_role": "Code Reviewer",
            "permitted_paths": permitted_paths,
            "permitted_data": ["task", "requirements", "acceptance_criteria", "reviewer_contract",
                               "assigned_diff", "context_files", "verification_evidence", "workflow_identity",
                               "revision_manifest"],
            "limits": ["Transmit only the listed paths and review packet; no write or command execution."],
            "authorizer_identity": "User explicitly authorizing through the task prompt; account identity unavailable to runtime",
            "authorizer_kind": "human", "authorizer_role": "Requesting user",
            "authorizer_identity_assurance": "declared", "authorizer_authentication_reference": None,
            "authorizer_session_identity": None, "authority_reference": "Explicit authorization in task prompt",
            "operation_effects": {"may_mutate_state": False, "may_incur_cost": True,
                                  "cost_limit": "USD 1.00 for this review invocation"},
            "authorized_at": iso(now - timedelta(minutes=5)),
            "source_reference": "task-prompt:section-7",
            "expires_at": iso(now + timedelta(hours=1)),
            "expires_or_revocation": "Expires at the recorded expires_at; revocation status must be checked before invocation.",
            "revoked": False,
            "workflow_owner_verification": {
                "verified_by": "primary Codex session", "verified_at": iso(now - timedelta(minutes=1)),
                "source_reference": "task-prompt:section-7", "identity_assurance": "declared",
                "verified_scope_sha256": "",
            },
        }
        authorization["workflow_owner_verification"]["verified_scope_sha256"] = authorization_scope_fingerprint(authorization)
        request = {
            "target_path": str(self.target.resolve()),
            "target_id": "test-target-1",
            "workflow_id": "workflow-1",
            "increment_id": "INC-1",
            "assignment_reference": "ASSIGN-1",
            "reviewer_role": "Code Reviewer",
            "review_id": "review-ASSIGN-1",
            "review_scope_kind": cycle["scope_kind"],
            "target_revision_id": revision,
            "baseline_commit": self.baseline,
            "source_state": "working_tree",
            "scope_paths": cycle["scope_paths"],
            "workflow_states_root": ".engineering/workflows",
            "workflow_state_path": ".engineering/workflows/workflow-1/workflow-state.json",
            "evidence_artifacts": evidence_artifacts,
            "external_authorization_reference": "AUTH-EXTERNAL-1",
            "external_authorization": authorization,
            "task": "Change app behavior to satisfy the task.",
            "requirements": [{"id": "REQ-1", "text": "Return two."}],
            "acceptance_criteria": [{"id": "AC-1", "text": "value() returns 2."}],
            "architecture_context": ["The function is the public component API."],
            "context_paths": self.context_paths,
            "test_evidence": [
                {"command": "python -m unittest", "exit_code": 0,
                 "result": "All relevant tests passed", "reference": "EV-TEST-1"},
                {"command": "python -m unittest", "exit_code": 0,
                 "result": "Correction verification passed", "reference": "EV-TEST-2"},
            ],
            "documentation_context": ["docs/architecture.md is maintained with behavior changes."],
            "reviewer_contract_reference": "harness/verification/reviewer-contracts.md#independence-and-selection",
            "reviewer_contract": "Review only the assigned scope, report concrete evidence, and do not approve the gate.",
            "previous_findings": previous_findings or [],
            "codex_finding_decisions": decisions or [],
            "review_cycle": cycle,
        }
        state_path = self.target / ".engineering/workflows/workflow-1/workflow-state.json"
        prior_refs = []
        if state_path.exists():
            prior_refs = json.loads(state_path.read_text()).get("artifact_references", [])
        state = {
            "workflow_id": "workflow-1",
            "target": {"target_id": "test-target-1", "git_root": str(self.target.resolve())},
            "scope": {"increment_id": "INC-1"},
            "authorization_verifier": {
                "identity": "primary Codex session", "role": "Workflow Owner",
                "identity_assurance": "declared", "source_reference": "task-prompt:section-7",
                "authentication_reference": None, "authority_reference": None,
            },
            "authorizations": [authorization],
            "artifact_references": sorted(set(prior_refs + [row["artifact_reference"] for row in evidence_artifacts])),
            "claude_review_cycles": [cycle],
        }
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return request

    def finding(self, finding_id="F-1", file="src/app.py", severity="medium"):
        return {
            "id": finding_id,
            "severity": severity,
            "file": file,
            "description": "The implementation returns a value that disagrees with AC-1.",
            "recommendation": "Correct the return value and add a regression check.",
        }

    def report(self, cycle, findings=None, status="APPROVED", prior=None, revision=None):
        findings = list(findings or [])
        if prior:
            for item in prior:
                if item.get("status") in {"unresolved", "disputed"}:
                    findings.append(self.finding(item["id"]))
        decision = "CHANGES_REQUESTED" if findings else status
        return {
            "decision": decision,
            "summary": "Reviewed the assigned paths against the task and evidence.",
            "findings": findings,
        }

    @staticmethod
    def envelope(report, *, subtype="success", is_error=False, session_id="claude-session-1"):
        return json.dumps({
            "type": "result", "subtype": subtype, "is_error": is_error,
            "session_id": session_id, "model": "claude-test-model",
            "usage": {"input_tokens": 123, "output_tokens": 45},
            "total_cost_usd": 0.0123, "result": json.dumps(report),
        }).encode()

    def mocked_runner(self, stdout, stderr=b"", exit_code=0, side_effect=None):
        def run(command, **kwargs):
            if side_effect:
                side_effect()
            return subprocess.CompletedProcess(command, exit_code, stdout, stderr)
        return run

    def run_one(self, cycle, report, runner=None, aily=None):
        reserved = reserve_review_round(cycle, target_revision_id=cycle["target_revision_id"])
        request = self.request(reserved)
        return run_claude_review(
            request, aily_executable=aily or self.fake_aily,
            process_runner=runner or self.mocked_runner(self.envelope(report)),
        ), reserved, request

    def complete_round(self, cycle, report, report_ref="reports/r1.json", evidence_ref="evidence/r1.json"):
        report_rel = f".engineering/workflows/workflow-1/{report_ref}"
        evidence_rel = f".engineering/workflows/workflow-1/{evidence_ref}"
        report_path = self.target / report_rel
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        (self.target / evidence_rel).parent.mkdir(parents=True, exist_ok=True)
        result = {"report_valid": True, "envelope_valid": True,
                  "report_received": True, "failure_kind": None,
                  "process": {"exit_code": 0, "invocation_attempted_at": "2026-11-06T10:00:00Z",
                              "review_round": cycle["rounds"][-1]["round_number"],
                              "review_manifest_sha256": "c" * 64}, "report": report}
        return record_review_result(cycle, result, report_reference=report_rel,
                                    evidence_reference=evidence_rel, target_path=self.target,
                                    workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json")

    def persist_cycle(self, cycle, extra_references=()):
        state_path = self.workflow_dir / "workflow-state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            state = {
                "workflow_id": "workflow-1",
                "target": {"target_id": "test-target-1", "git_root": str(self.target.resolve())},
                "scope": {"increment_id": "INC-1"},
                "authorization_verifier": {
                    "identity": "primary Codex session", "role": "Workflow Owner",
                    "identity_assurance": "declared", "source_reference": "task-prompt:section-7",
                    "authentication_reference": None, "authority_reference": None,
                },
                "authorizations": [], "artifact_references": [], "claude_review_cycles": [],
            }
        state = apply_review_cycle_to_workflow_state(state, cycle)
        state["artifact_references"] = sorted(set(state.get("artifact_references", [])) | set(extra_references))
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return state

    def invoke_cycle(self, cycle, report, report_name, revision=None,
                     previous_findings=None, decisions=None):
        request = self.request(cycle, revision=revision, previous_findings=previous_findings, decisions=decisions)
        result = run_claude_review(
            request, aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(report)),
        )
        self.assertTrue(result["report_valid"], result.get("error"))
        report_ref = f".engineering/workflows/workflow-1/reports/{report_name}"
        evidence_ref = f".engineering/workflows/workflow-1/evidence/review-{report_name}"
        report_path = self.target / report_ref
        report_path.write_text(json.dumps(result["report"], ensure_ascii=False), encoding="utf-8")
        (self.target / evidence_ref).write_text("pending evidence artifact\n", encoding="utf-8")
        cycle = record_review_result(
            cycle, result, report_reference=report_ref, evidence_reference=evidence_ref,
            target_path=self.target,
            workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        state = self.persist_cycle(cycle, [report_ref, evidence_ref])
        return cycle, result, state

    def test_01_successful_aily_invocation_and_valid_report(self):
        cycle = self.cycle()
        reserved = reserve_review_round(cycle, target_revision_id=REVISION_1)
        report = self.report(reserved)
        request = self.request(reserved)
        result = run_claude_review(
            request, aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(report)),
        )
        self.assertTrue(result["report_valid"])
        self.assertEqual(result["report"]["decision"], "APPROVED")
        self.assertEqual(set(result["report"]), {"decision", "summary", "findings"})
        self.assertEqual(result["process"]["reviewer_session_identity"], "claude-session-1")

    def test_total_claude_budget_is_parsed_and_remaining_cost_is_aggregated(self):
        self.assertEqual(_parse_usd_limit("USD 2.00 total for this workflow"), 2.0)
        spent, known = _recorded_review_spend({
            "claude_review_cycles": [{"rounds": [{"invocation_attempts": [
                {"response_metadata": {"cost_usd": 0.63}},
                {"response_metadata": {"cost_usd": 0.12}},
            ]}]}]
        })
        self.assertEqual(spent, 0.75)
        self.assertTrue(known)
        unknown_spent, known = _recorded_review_spend({
            "claude_review_cycles": [{"rounds": [{"invocation_attempts": [
                {"response_metadata": {}},
            ]}]}]
        })
        self.assertEqual(unknown_spent, 0.0)
        self.assertFalse(known)

    def test_aily_invocation_accepts_a_hard_per_invocation_budget(self):
        command = _aily_read_only_command("/usr/bin/aily", "review prompt", 1.25)
        index = command.index("--max-budget-usd")
        self.assertEqual(command[index + 1], "1.25")

    def test_request_uses_lower_of_user_budget_and_authorized_cost_limit(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        request["max_budget_usd"] = 2.0
        with patch.dict(os.environ, {"HARNESS_CLAUDE_TOTAL_BUDGET_USD": "0.75"}):
            validated, _, _ = _validate_request(request)
        self.assertEqual(validated["_aily_max_budget_usd"], 0.75)

    def test_workflow_round_persists_review_cost_and_token_metadata(self):
        reserved = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        cycle, result, state = self.invoke_cycle(reserved, self.report(reserved), "budget-review.json")
        self.assertEqual(result["process"]["cost_usd"], 0.0123)
        self.assertEqual(result["process"]["usage"], {"input_tokens": 123, "output_tokens": 45})
        metadata = cycle["rounds"][0]["invocation_attempts"][0]["response_metadata"]
        self.assertEqual(metadata["cost_usd"], 0.0123)
        self.assertEqual(metadata["usage"]["input_tokens"], 123)
        persisted_round = json.loads((self.workflow_dir / "workflow-state.json").read_text(encoding="utf-8"))["claude_review_cycles"][0]["rounds"][0]
        persisted_state = json.loads((self.workflow_dir / "workflow-state.json").read_text(encoding="utf-8"))
        persisted_auth = persisted_state["authorizations"][0]
        self.assertEqual(persisted_auth["authorizer_identity_assurance"], "declared")
        self.assertEqual(persisted_auth["source_reference"], "task-prompt:section-7")
        self.assertIsNone(persisted_auth["authorizer_session_identity"])
        self.assertTrue(persisted_round["invocation_attempted_at"])
        self.assertEqual(persisted_round["review_manifest_sha256"], result["process"]["review_manifest_sha256"])

    def test_mcp_is_disabled_without_passing_invalid_empty_mcp_config(self):
        observed = {}

        def runner(command, **kwargs):
            observed["command"] = command
            observed["cwd"] = kwargs["cwd"]
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "session_id": "connection-session", "result": "CLAUDE_CONNECTION_OK",
            }).encode(), b"")

        result = run_aily_connection_check(aily_executable=self.fake_aily, process_runner=runner)
        command = observed["command"]
        self.assertEqual(result["status"], "CONNECTED")
        self.assertTrue(result["process"]["mcp_disabled_by_strict_config"])
        self.assertIn("--restricted", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertNotIn("--mcp-config", command)
        self.assertNotIn("--safe-mode", command)
        self.assertFalse(Path(observed["cwd"]).exists())

    def test_failed_invocation_does_not_consume_round_and_recovers_auditably(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        failure = {
            "report_valid": False, "report_received": False, "envelope_valid": False,
            "failure_kind": "process_launch_failure",
            "process": {"exit_code": None, "invocation_attempted_at": "2026-11-06T10:00:00Z",
                        "review_manifest_sha256": "d" * 64},
        }
        failed = record_review_result(
            cycle, failure, report_reference=None,
            evidence_reference=".engineering/workflows/workflow-1/evidence/failed-round-1.json",
            target_path=self.target,
            workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        self.assertEqual(failed["round_count"], 0)
        self.assertEqual(failed["rounds"][0]["outcome"], "INVOCATION_FAILED")
        self.assertEqual(failed["status"], "invocation_failed")
        with self.assertRaisesRegex(ReviewError, "explicit recovery"):
            reserve_review_round(failed, target_revision_id=REVISION_1)

        recovered = recover_uncompleted_review_round(
            failed, reason="Prior Aily process launch failed before producing a report.",
            recovered_by="primary Codex session", recovered_at="2026-11-06T10:05:00Z",
        )
        self.assertEqual(recovered["round_count"], 0)
        self.assertEqual(recovered["rounds"][0]["attempt_number"], 2)
        self.assertEqual(recovered["rounds"][0]["recovery_transitions"][0]["from_outcome"], "INVOCATION_FAILED")
        self.assertEqual(recovered["rounds"][0]["invocation_attempts"][0]["evidence_reference"],
                         ".engineering/workflows/workflow-1/evidence/failed-round-1.json")
        with self.assertRaises(ReviewError):
            recover_uncompleted_review_round(recovered, reason="duplicate recovery", recovered_by="Codex")

    def test_new_user_authorization_allows_exactly_one_final_audited_attempt(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        first_failure = {
            "report_valid": False, "report_received": False, "envelope_valid": False,
            "failure_kind": "process_launch_failure",
            "process": {"exit_code": None, "invocation_attempted_at": "2026-11-06T10:00:00Z",
                        "review_manifest_sha256": "a" * 64},
        }
        cycle = record_review_result(
            cycle, first_failure, report_reference=None,
            evidence_reference=".engineering/workflows/workflow-1/evidence/failed-1.json",
            target_path=self.target, workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        cycle = recover_uncompleted_review_round(
            cycle, reason="Recover the original process launch failure.",
            recovered_by="primary Codex session", recovered_at="2026-11-06T10:01:00Z",
        )
        second_failure = {
            "report_valid": False, "report_received": True, "envelope_valid": True,
            "failure_kind": "invalid_reviewer_report",
            "process": {"exit_code": 0, "invocation_attempted_at": "2026-11-06T10:02:00Z",
                        "review_manifest_sha256": "b" * 64},
        }
        cycle = record_review_result(
            cycle, second_failure, report_reference=None,
            evidence_reference=".engineering/workflows/workflow-1/evidence/rejected-2.json",
            target_path=self.target, workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        with self.assertRaisesRegex(ReviewError, "explicit user task authorization"):
            recover_uncompleted_review_round(
                cycle, reason="Attempt without new authorization.", recovered_by="Codex",
            )

        final = recover_uncompleted_review_round(
            cycle, reason="The current user task explicitly authorizes one final call after parser tests.",
            recovered_by="primary Codex session",
            authorization_reference="current-user-task:Make Claude Review Work — Stop Overengineering",
            recovered_at="2026-11-06T10:03:00Z",
        )
        round_record = final["rounds"][0]
        self.assertEqual(final["round_count"], 0)
        self.assertEqual(round_record["attempt_number"], 3)
        self.assertEqual(len(round_record["invocation_attempts"]), 2)
        self.assertEqual(round_record["recovery_transitions"][1]["from_outcome"], "REVIEW_REJECTED")
        self.assertEqual(
            round_record["recovery_transitions"][1]["authorization_reference"],
            "current-user-task:Make Claude Review Work — Stop Overengineering",
        )

        final_failure = {
            "report_valid": False, "report_received": True, "envelope_valid": True,
            "failure_kind": "invalid_reviewer_report",
            "process": {"exit_code": 0, "invocation_attempted_at": "2026-11-06T10:04:00Z",
                        "review_manifest_sha256": "c" * 64},
        }
        terminal = record_review_result(
            final, final_failure, report_reference=None,
            evidence_reference=".engineering/workflows/workflow-1/evidence/rejected-3.json",
            target_path=self.target, workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        with self.assertRaisesRegex(ReviewError, "all permitted recovery transitions"):
            recover_uncompleted_review_round(
                terminal, reason="No retry beyond the explicit final attempt.", recovered_by="Codex",
                authorization_reference="another-reference",
            )

    def test_final_attempt_recovery_reference_must_match_persisted_authorization(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        first_failure = {
            "report_valid": False, "report_received": False, "envelope_valid": False,
            "failure_kind": "process_launch_failure",
            "process": {"exit_code": None, "invocation_attempted_at": "2026-11-06T10:00:00Z",
                        "review_manifest_sha256": "a" * 64},
        }
        cycle = record_review_result(
            cycle, first_failure, report_reference=None,
            evidence_reference=".engineering/workflows/workflow-1/evidence/failed-1.json",
            target_path=self.target, workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        cycle = recover_uncompleted_review_round(
            cycle, reason="Recover first failed attempt.", recovered_by="Codex",
            recovered_at="2026-11-06T10:01:00Z",
        )
        second_failure = {
            "report_valid": False, "report_received": True, "envelope_valid": True,
            "failure_kind": "invalid_reviewer_report",
            "process": {"exit_code": 0, "invocation_attempted_at": "2026-11-06T10:02:00Z",
                        "review_manifest_sha256": "b" * 64},
        }
        cycle = record_review_result(
            cycle, second_failure, report_reference=None,
            evidence_reference=".engineering/workflows/workflow-1/evidence/rejected-2.json",
            target_path=self.target, workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        cycle = recover_uncompleted_review_round(
            cycle, reason="Current user task explicitly authorizes one final attempt.",
            recovered_by="Codex", authorization_reference="task-prompt:another-section",
            recovered_at="2026-11-06T10:03:00Z",
        )
        request = self.request(cycle)
        with self.assertRaisesRegex(ReviewError, "must match the persisted explicit user authorization source"):
            _validate_request(request)

    def test_rejected_report_does_not_consume_completed_round(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        rejected = {
            "report_valid": False, "report_received": True, "envelope_valid": True,
            "failure_kind": "invalid_reviewer_report",
            "process": {"exit_code": 0, "invocation_attempted_at": "2026-11-06T10:00:00Z",
                        "review_manifest_sha256": "e" * 64},
        }
        result = record_review_result(
            cycle, rejected, report_reference=None,
            evidence_reference=".engineering/workflows/workflow-1/evidence/rejected-round-1.json",
            target_path=self.target,
            workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        self.assertEqual(result["round_count"], 0)
        self.assertEqual(result["rounds"][0]["outcome"], "REVIEW_REJECTED")
        self.assertEqual(result["status"], "review_rejected")

    def test_report_valid_flag_alone_cannot_complete_round(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        forged = {
            "report_valid": True, "envelope_valid": False, "failure_kind": "invalid_json",
            "report": self.report(cycle),
            "process": {"exit_code": 1, "invocation_attempted_at": "2026-11-06T10:00:00Z",
                        "review_manifest_sha256": "f" * 64},
        }
        with self.assertRaisesRegex(ReviewError, "successful envelope"):
            record_review_result(
                cycle, forged, report_reference=".engineering/workflows/workflow-1/reports/forged.json",
                evidence_reference=".engineering/workflows/workflow-1/evidence/forged.json",
                target_path=self.target,
                workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
            )

    def test_02_extracts_nested_claude_json_from_output_envelope(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        expected = self.report(cycle)
        report, valid, failure = _extract_report(self.envelope(expected).decode())
        self.assertEqual(report, expected)
        self.assertTrue(valid)
        self.assertIsNone(failure)

    def test_02a_extracts_documented_aily_result_field_and_allowlisted_metadata(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        expected = self.report(cycle)
        envelope = json.loads(self.envelope(expected))
        envelope.update({
            "invocation_id": "aily-invocation-42",
            "usage": {
                "input_tokens": 123, "output_tokens": 45,
                "cache_read_input_tokens": 67,
                "output_tokens_details": {"thinking_tokens": 9, "unrelated": "discard"},
                "unexpected_private_payload": "discard",
            },
            "private_response_data": "must not escape the envelope",
        })
        report, envelope_valid, failure, metadata, received, diagnostic = _extract_report_diagnostics(
            json.dumps(envelope)
        )
        self.assertEqual(report, expected)
        self.assertTrue(envelope_valid)
        self.assertIsNone(failure)
        self.assertTrue(received)
        self.assertIsNone(diagnostic)
        self.assertEqual(metadata["response_field"], "result")
        self.assertEqual(metadata["invocation_id"], "aily-invocation-42")
        self.assertEqual(metadata["usage"], {
            "input_tokens": 123, "output_tokens": 45, "cache_read_input_tokens": 67,
            "output_tokens_details": {"thinking_tokens": 9},
        })
        self.assertNotIn("private_response_data", metadata)
        self.assertNotIn("unexpected_private_payload", json.dumps(metadata))

    def test_02b_rejects_undocumented_object_result_encoding(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        expected = self.report(cycle)
        envelope = {
            "type": "result", "subtype": "success", "is_error": False,
            "session_id": "session-2", "result": expected,
        }
        self.assertEqual(_extract_report_diagnostics(json.dumps(envelope))[2], "invalid_json")
        report = dict(expected)
        report["uncontracted_field"] = "not accepted"
        self.assertIn("unsupported report field: uncontracted_field",
                      validate_reviewer_report(report, self.request(cycle)))

    def test_02c_non_object_json_result_is_received_but_rejected(self):
        envelope = {
            "type": "result", "subtype": "success", "is_error": False,
            "session_id": "session-3", "result": "[]",
        }
        report, valid, failure, _metadata, received, diagnostic = _extract_report_diagnostics(json.dumps(envelope))
        self.assertIsNone(report)
        self.assertTrue(valid)
        self.assertEqual(failure, "invalid_reviewer_report")
        self.assertTrue(received)
        self.assertIn("must be a reviewer report object", diagnostic)

    def test_03_malformed_outer_or_nested_json_is_rejected(self):
        self.assertEqual(_extract_report("not-json")[2], "invalid_json")
        outer = json.dumps({"type": "result", "subtype": "success", "result": "{broken"})
        self.assertEqual(_extract_report(outer)[2], "invalid_json")
        no_session = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                 "result": "{}"})
        self.assertEqual(_extract_report(no_session)[2], "invalid_json")

    def test_04_missing_required_reviewer_fields_is_invalid_report(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        report = self.report(cycle)
        report.pop("summary")
        result = run_claude_review(
            self.request(cycle), aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(report)),
        )
        self.assertEqual(result["failure_kind"], "invalid_reviewer_report")
        self.assertFalse(result["report_valid"])
        self.assertIsNone(result["report"])
        self.assertIn("missing required report field: summary", result["validation_errors"])
        self.assertEqual(result["response_metadata"]["response_field"], "result")
        self.assertEqual(result["process"]["model"], "claude-test-model")
        self.assertEqual(result["process"]["usage"]["input_tokens"], 123)

    def test_invalid_report_diagnostics_survive_state_recording_and_evidence_errors(self):
        cycle = self.cycle()
        report = self.report(reserve_review_round(cycle, target_revision_id=REVISION_1))
        report.pop("summary")
        result, reserved, request = self.run_one(cycle, report)
        original_error = result["error"]
        self.assertTrue(result["validation_errors"])
        failed_cycle = record_review_result(
            reserved, result, report_reference=None,
            evidence_reference=".engineering/workflows/workflow-1/evidence/rejected.json",
            target_path=self.target,
            workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        self.assertEqual(result["error"], original_error)
        self.assertEqual(failed_cycle["rounds"][0]["diagnostic"], original_error)
        self.assertEqual(failed_cycle["rounds"][0]["validation_errors"], result["validation_errors"])
        self.assertEqual(failed_cycle["rounds"][0]["response_metadata"], result["response_metadata"])
        evidence = build_review_evidence_record(
            result, request, evidence_id="EV-REJECTED", report_reference=None,
        )
        self.assertEqual(evidence["review_execution"]["diagnostic"], original_error)
        self.assertEqual(evidence["review_execution"]["validation_errors"], result["validation_errors"])
        self.assertEqual(evidence["review_execution"]["response_metadata"], result["response_metadata"])
        with self.assertRaises(ReviewError):
            build_review_evidence_record(result, {}, evidence_id="EV-REJECTED")
        self.assertEqual(result["error"], original_error)

    def test_05_nonzero_process_exit_is_not_a_review_pass(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        result = run_claude_review(
            self.request(cycle), aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(b"", b"review failed", 7),
        )
        self.assertEqual(result["failure_kind"], "reviewer_execution_failure")
        self.assertEqual(result["process"]["exit_code"], 7)
        self.assertFalse(result["report_valid"])

    def test_06_missing_aily_executable_is_a_blocker_without_fallback(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        runner = Mock()
        request = self.request(cycle)
        result = run_claude_review(request, aily_executable=str(self.base / "missing-aily"), process_runner=runner)
        self.assertEqual(result["failure_kind"], "process_launch_failure")
        runner.assert_not_called()
        evidence = build_review_evidence_record(result, request, evidence_id="EV-FAILED-REVIEW")
        self.assertFalse(evidence["review_execution"]["report_valid"])
        self.assertIsNone(evidence["review_execution"]["report_reference"])

    def test_07_empty_reviewer_response_is_blocked(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        result = run_claude_review(
            self.request(cycle), aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(b"\n"),
        )
        self.assertEqual(result["failure_kind"], "empty_response")

    def test_08_accepted_finding_can_be_recorded_after_correction_evidence(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        cycle = self.complete_round(cycle, self.report(cycle, [self.finding()]))
        cycle = record_finding_decision(
            cycle, finding_id="F-1", decision="ACCEPT", reason="Reproduced against AC-1.",
            evidence_references=["EV-TEST-1"], resolution_evidence_references=["EV-TEST-2"],
        )
        self.assertEqual(cycle["finding_decisions"][0]["resolution_status"], "verified")

    def test_09_rejected_finding_requires_and_records_evidence(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        cycle = self.complete_round(cycle, self.report(cycle, [self.finding()]))
        with self.assertRaises(ReviewError):
            record_finding_decision(cycle, finding_id="F-1", decision="REJECT", reason="Incorrect.")
        cycle = record_finding_decision(
            cycle, finding_id="F-1", decision="REJECT", reason="Current implementation meets AC-1.",
            evidence_references=["EV-SOURCE-1", "EV-TEST-1"],
        )
        self.assertEqual(cycle["finding_decisions"][0]["resolution_status"], "resolved")

    def test_10_partial_finding_decision_requires_portion_evidence_and_resolution(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        cycle = self.complete_round(cycle, self.report(cycle, [self.finding()]))
        cycle = record_finding_decision(
            cycle, finding_id="F-1", decision="PARTIAL", reason="Only the test recommendation is valid.",
            evidence_references=["EV-TEST-1"], accepted_portion="Add a regression test.",
            resolution_evidence_references=["EV-TEST-2"],
        )
        self.assertEqual(cycle["finding_decisions"][0]["accepted_portion"], "Add a regression test.")
        self.assertEqual(cycle["finding_decisions"][0]["resolution_status"], "verified")

    def test_11_deferred_finding_requires_human_intervention(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        cycle = self.complete_round(cycle, self.report(cycle, [self.finding()]))
        cycle = record_finding_decision(
            cycle, finding_id="F-1", decision="DEFER", reason="Requires product-owner intent.",
        )
        self.assertEqual(cycle["status"], "human_intervention_required")
        self.assertTrue(cycle["human_escalation"])
        with self.assertRaises(ReviewError):
            reserve_review_round(cycle, target_revision_id=REVISION_2, verification_evidence_references=["EV-2"])

    def _round1_with_accepted_finding(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        cycle = self.complete_round(cycle, self.report(cycle, [self.finding()]))
        cycle = record_finding_decision(
            cycle, finding_id="F-1", decision="ACCEPT", reason="Confirmed defect.",
            evidence_references=["EV-TEST-1"], resolution_evidence_references=["EV-TEST-2"],
        )
        return cycle

    def test_12_successful_initial_and_followup_review_completes_cycle(self):
        cycle = self._round1_with_accepted_finding()
        cycle = reserve_review_round(cycle, target_revision_id=REVISION_2,
                                    verification_evidence_references=["EV-TEST-2"])
        followup = self.report(cycle, prior=[{
            "id": "F-1", "status": "resolved", "evidence_references": ["EV-TEST-2"],
        }], revision=REVISION_2)
        cycle = self.complete_round(cycle, followup, "reports/r2.json", "evidence/r2.json")
        cycle = finalize_review_cycle(cycle)
        self.assertEqual(cycle["round_count"], 2)
        self.assertEqual(cycle["status"], "complete")
        self.assertFalse(cycle["human_escalation"])

    def test_13_unresolved_finding_after_round_two_escalates(self):
        cycle = self._round1_with_accepted_finding()
        cycle = reserve_review_round(cycle, target_revision_id=REVISION_2,
                                    verification_evidence_references=["EV-TEST-2"])
        report = self.report(cycle, prior=[{
            "id": "F-1", "status": "unresolved", "evidence_references": ["EV-TEST-2"],
        }], revision=REVISION_2)
        cycle = self.complete_round(cycle, report, "reports/r2.json", "evidence/r2.json")
        cycle = finalize_review_cycle(cycle)
        self.assertEqual(cycle["status"], "human_intervention_required")
        self.assertEqual(cycle["unresolved_finding_ids"], ["F-1"])

    def test_14_material_disagreement_after_round_two_escalates(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        cycle = self.complete_round(cycle, self.report(cycle, [self.finding()]))
        cycle = record_finding_decision(
            cycle, finding_id="F-1", decision="REJECT", reason="Evidence shows no defect.",
            evidence_references=["EV-TEST-1"], material_disagreement=True,
        )
        cycle = reserve_review_round(cycle, target_revision_id=REVISION_1,
                                    verification_evidence_references=["EV-TEST-1"])
        report = self.report(cycle, status="CHANGES_REQUESTED", prior=[{
            "id": "F-1", "status": "disputed", "evidence_references": ["EV-TEST-1"],
        }])
        cycle = self.complete_round(cycle, report, "reports/r2.json", "evidence/r2.json")
        cycle = finalize_review_cycle(cycle)
        self.assertEqual(cycle["status"], "human_intervention_required")
        self.assertTrue(cycle["human_escalation"])

    def test_15_third_review_round_is_rejected(self):
        cycle = self._round1_with_accepted_finding()
        cycle = reserve_review_round(cycle, target_revision_id=REVISION_2,
                                    verification_evidence_references=["EV-TEST-2"])
        cycle = self.complete_round(cycle, self.report(cycle, prior=[{
            "id": "F-1", "status": "resolved", "evidence_references": ["EV-TEST-2"],
        }], revision=REVISION_2), "reports/r2.json", "evidence/r2.json")
        with self.assertRaisesRegex(ReviewError, "third round"):
            reserve_review_round(cycle, target_revision_id=REVISION_2,
                                verification_evidence_references=["EV-TEST-2"])

    def test_16_round_counter_survives_json_workflow_state_resume(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        cycle = self.complete_round(cycle, self.report(cycle, [self.finding()]))
        cycle = record_finding_decision(
            cycle, finding_id="F-1", decision="ACCEPT", reason="Confirmed.",
            evidence_references=["EV-1"], resolution_evidence_references=["EV-2"],
        )
        resumed = json.loads(json.dumps(cycle))
        resumed = reserve_review_round(resumed, target_revision_id=REVISION_2,
                                       verification_evidence_references=["EV-2"])
        self.assertEqual(resumed["round_count"], 1)
        self.assertEqual([row["round_number"] for row in resumed["rounds"]], [1, 2])
        resumed = self.complete_round(resumed, self.report(resumed, prior=[{
            "id": "F-1", "status": "resolved", "evidence_references": ["EV-2"],
        }], revision=REVISION_2), "reports/resumed-r2.json", "evidence/resumed-r2.json")
        self.assertEqual(resumed["round_count"], 2)

    def test_17_revision_change_during_review_invalidates_report(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        report = self.report(cycle)

        def mutate_target():
            (self.target / "src" / "app.py").write_text("changed while reviewing\n")

        result = run_claude_review(
            self.request(cycle), aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(report), side_effect=mutate_target),
        )
        self.assertEqual(result["failure_kind"], "stale_revision")
        self.assertFalse(result["report_valid"])
        self.assertIsNone(result["report"])

    def test_added_file_inside_directory_scope_invalidates_manifest(self):
        cycle = new_review_cycle(
            cycle_id=stable_review_cycle_id(str(self.target.resolve()), "INC-1"), scope_kind="increment", increment_id="INC-1",
            assignment_reference="ASSIGN-1", target_revision_id=REVISION_1, scope_paths=["src"],
        )
        cycle = reserve_review_round(cycle, target_revision_id=REVISION_1)
        request = self.request(cycle)
        report = self.report(cycle)

        def add_scoped_file():
            (self.target / "src" / "added.py").write_text("new scoped file\n", encoding="utf-8")

        result = run_claude_review(
            request, aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(report), side_effect=add_scoped_file),
        )
        self.assertEqual(result["failure_kind"], "stale_revision")
        self.assertFalse(result["report_valid"])

    def test_removed_file_inside_directory_scope_invalidates_manifest(self):
        cycle = new_review_cycle(
            cycle_id=stable_review_cycle_id(str(self.target.resolve()), "INC-1"), scope_kind="increment", increment_id="INC-1",
            assignment_reference="ASSIGN-1", target_revision_id=REVISION_1, scope_paths=["tests"],
        )
        cycle = reserve_review_round(cycle, target_revision_id=REVISION_1)
        (self.target / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
        request = self.request(cycle)
        report = self.report(cycle)

        def remove_scoped_file():
            (self.target / "tests" / "test_app.py").unlink()

        result = run_claude_review(
            request, aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(report), side_effect=remove_scoped_file),
        )
        self.assertEqual(result["failure_kind"], "stale_revision")
        self.assertFalse(result["report_valid"])

    def test_target_head_change_during_review_invalidates_report(self):
        git(self.target, "add", "src/app.py", "docs/architecture.md")
        git(self.target, "commit", "-m", "reviewed implementation", "-q")
        current_head = git(self.target, "rev-parse", "HEAD")
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        request["source_state"] = "commit"
        request["commit_sha"] = current_head
        report = self.report(cycle)

        def move_head():
            (self.target / "src/app.py").write_text("def value():\n    return 9\n", encoding="utf-8")
            git(self.target, "add", "src/app.py")
            git(self.target, "commit", "-m", "external target change", "-q")

        result = run_claude_review(
            request, aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(report), side_effect=move_head),
        )
        self.assertEqual(result["failure_kind"], "stale_revision")
        self.assertFalse(result["report_valid"])

    def test_snapshot_mutation_during_review_invalidates_report(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        report = self.report(cycle)

        def mutate_snapshot(command, **kwargs):
            (Path(kwargs["cwd"]) / "unauthorized.txt").write_text("extra file", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, self.envelope(report), b"")

        result = run_claude_review(request, aily_executable=self.fake_aily, process_runner=mutate_snapshot)
        self.assertEqual(result["failure_kind"], "stale_revision")
        self.assertFalse(result["report_valid"])

    def test_directory_scope_rejects_nested_sensitive_names_before_invocation(self):
        cycle = new_review_cycle(
            cycle_id=stable_review_cycle_id(str(self.target.resolve()), "INC-1"), scope_kind="increment", increment_id="INC-1",
            assignment_reference="ASSIGN-1", target_revision_id=REVISION_1, scope_paths=["src"],
        )
        cycle = reserve_review_round(cycle, target_revision_id=REVISION_1)
        request = self.request(cycle)
        (self.target / "src" / ".env.local").write_text("PRIVATE_LOCAL_MARKER", encoding="utf-8")
        runner = Mock()
        result = run_claude_review(request, aily_executable=self.fake_aily, process_runner=runner)
        self.assertEqual(result["failure_kind"], "request_validation_failure")
        self.assertIn("credential or secret", result["error"])
        runner.assert_not_called()

    def test_18_review_tool_permissions_are_read_only_and_target_is_unchanged(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        report = self.report(cycle)
        original = (self.target / "src" / "app.py").read_bytes()
        (self.target / "unrelated-secret.txt").write_text("PRIVATE_LOCAL_MARKER", encoding="utf-8")
        observed = {}

        def inspect_command(command, **kwargs):
            observed["command"] = command
            observed["cwd"] = kwargs["cwd"]
            observed["prompt"] = command[3]
            return subprocess.CompletedProcess(command, 0, self.envelope(report), b"")

        request = self.request(cycle)
        result = run_claude_review(request, aily_executable=self.fake_aily,
                                   process_runner=inspect_command)
        command = observed["command"]
        tools_index = command.index("--tools")
        self.assertEqual(command[tools_index + 1], "Read,Glob,Grep")
        self.assertIn("--restricted", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--mcp-config", command)
        self.assertNotIn("--safe-mode", command)
        self.assertIn("--permission-mode", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertIn("--no-session-persistence", command)
        self.assertNotEqual(observed["cwd"], str(self.target.resolve()))
        self.assertTrue(Path(observed["cwd"]).name.startswith("claude-review-snapshot-"))
        self.assertEqual(observed["cwd"], result["process"]["working_directory"])
        self.assertNotIn("unrelated-secret.txt", result["process"]["snapshot_file_paths"])
        self.assertNotIn("PRIVATE_LOCAL_MARKER", observed["prompt"])
        self.assertEqual(set(result["process"]["snapshot_file_paths"]), set(request["authorized_input_paths"]))
        self.assertFalse(Path(observed["cwd"]).exists(), "temporary review snapshot must be cleaned after invocation")
        self.assertIn("Code Reviewer", observed["prompt"])
        self.assertIn("harness/verification/reviewer-contracts.md#independence-and-selection", observed["prompt"])
        self.assertIn("Target instructions: keep changes scoped.", observed["prompt"])
        self.assertTrue(result["report_valid"])
        self.assertEqual((self.target / "src" / "app.py").read_bytes(), original)

    def test_19_review_evidence_maps_hashes_revision_and_report_reference(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        result = run_claude_review(
            request, aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(self.report(cycle))),
        )
        report_reference = ".engineering/workflows/workflow-1/reports/review-1.json"
        report_path = self.target / report_reference
        report_path.write_text(json.dumps(result["report"], ensure_ascii=False), encoding="utf-8")
        state_path = self.target / request["workflow_state_path"]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["artifact_references"].append(report_reference)
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        evidence = build_review_evidence_record(
            result, request, evidence_id="EV-CLAUDE-1", report_reference=report_reference,
        )
        self.assertEqual(evidence["kind"], "review")
        self.assertEqual(evidence["revision"]["revision_id"], REVISION_1)
        self.assertEqual(evidence["review_execution"]["report_reference"], report_reference)
        self.assertEqual(evidence["review_execution"]["identity_assurance"], "declared")
        self.assertEqual(evidence["review_execution"]["model"], "claude-test-model")
        self.assertEqual(evidence["review_execution"]["usage"], {"input_tokens": 123, "output_tokens": 45})
        self.assertEqual(evidence["review_execution"]["cost_usd"], 0.0123)
        self.assertEqual(evidence["review_execution"]["reviewer_contract_reference"],
                         "harness/verification/reviewer-contracts.md#independence-and-selection")
        self.assertRegex(evidence["review_execution"]["reviewer_contract_sha256"], r"^[a-f0-9]{64}$")
        self.assertNotIn("result", evidence["review_execution"])
        self.assertEqual(evidence["source_reference"], "AUTH-EXTERNAL-1")
        self.assertRegex(evidence["review_execution"]["review_manifest_sha256"], r"^[a-f0-9]{64}$")
        self.assertNotIn("api_key", json.dumps(evidence["review_execution"]).lower())
        self.assertNotIn("password", json.dumps(evidence["review_execution"]).lower())

    def test_approved_review_with_empty_findings_is_valid(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        report = self.report(cycle)
        result = run_claude_review(
            self.request(cycle), aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(report)),
        )
        self.assertTrue(result["report_valid"], result.get("error"))
        self.assertEqual(result["report"], {"decision": "APPROVED", "summary": report["summary"], "findings": []})

    def test_invalid_decisions_fail_with_field_specific_diagnostics(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        for decision in ("BLOCKED", [], None):
            report = self.report(cycle)
            report["decision"] = decision
            result = run_claude_review(
                self.request(cycle), aily_executable=self.fake_aily,
                process_runner=self.mocked_runner(self.envelope(report)),
            )
            self.assertEqual(result["failure_kind"], "invalid_reviewer_report")
            self.assertIn("decision must be APPROVED or CHANGES_REQUESTED", result["validation_errors"])

    def test_malformed_findings_fail_with_exact_field_diagnostic(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        report = self.report(cycle, [self.finding()])
        report["findings"][0].pop("recommendation")
        result = run_claude_review(
            self.request(cycle), aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(report)),
        )
        self.assertEqual(result["failure_kind"], "invalid_reviewer_report")
        self.assertIn("findings[0] missing required field: recommendation", result["validation_errors"])

    def test_finding_outside_assigned_scope_is_rejected(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        finding = self.finding(file="outside/app.py")
        result = run_claude_review(
            self.request(cycle), aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(self.report(cycle, [finding]))),
        )
        self.assertEqual(result["failure_kind"], "invalid_reviewer_report")

    def test_review_diff_includes_ignored_untracked_file_inside_explicit_scope(self):
        (self.target / "src" / "ignored.py").write_text("review this ignored target file\n")
        paths = sorted(self.scope_paths + ["src/ignored.py"])
        cycle = new_review_cycle(
            cycle_id=stable_review_cycle_id(str(self.target.resolve()), "INC-1"), scope_kind="increment", increment_id="INC-1",
            assignment_reference="ASSIGN-1", target_revision_id=REVISION_1, scope_paths=paths,
        )
        cycle = reserve_review_round(cycle, target_revision_id=REVISION_1)
        observed = {}

        def capture_prompt(command, **kwargs):
            observed["prompt"] = command[3]
            return subprocess.CompletedProcess(command, 0, self.envelope(self.report(cycle)), b"")

        result = run_claude_review(
            self.request(cycle), aily_executable=self.fake_aily,
            process_runner=capture_prompt,
        )
        self.assertTrue(result["report_valid"])
        self.assertIn("ignored.py", observed["prompt"])

    def test_review_does_not_follow_a_scope_symlink_outside_target(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        outside = self.base / "outside.py"
        outside.write_text("must not be disclosed\n")
        app_path = self.target / "src" / "app.py"
        app_path.unlink()
        app_path.symlink_to(outside)
        runner = Mock()
        result = run_claude_review(
            request, aily_executable=self.fake_aily, process_runner=runner,
        )
        self.assertFalse(result["report_valid"])
        self.assertEqual(result["failure_kind"], "request_validation_failure")
        runner.assert_not_called()

    def test_external_authorization_must_match_reference_and_operation(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        request["external_authorization"]["operation"] = "download_external_resources"
        result = run_claude_review(request, aily_executable=self.fake_aily)
        self.assertEqual(result["failure_kind"], "request_validation_failure")

    def test_external_authorization_must_allow_possible_cost(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        authorization = request["external_authorization"]
        authorization["operation_effects"]["may_incur_cost"] = False
        authorization["workflow_owner_verification"]["verified_scope_sha256"] = authorization_scope_fingerprint(authorization)
        runner = Mock()
        result = run_claude_review(request, aily_executable=self.fake_aily, process_runner=runner)
        self.assertEqual(result["failure_kind"], "request_validation_failure")
        self.assertIn("authorize possible cost", result["error"])
        runner.assert_not_called()

    def test_path_traversal_and_wrong_report_revision_are_rejected(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        request["scope_paths"] = ["../outside"]
        runner = Mock()
        result = run_claude_review(request, aily_executable=self.fake_aily, process_runner=runner)
        self.assertEqual(result["failure_kind"], "request_validation_failure")
        runner.assert_not_called()

        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        report = self.report(cycle)
        report["target_revision_id"] = REVISION_2
        result = run_claude_review(
            self.request(cycle), aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(report)),
        )
        self.assertEqual(result["failure_kind"], "invalid_reviewer_report")
        self.assertFalse(result["report_valid"])
        self.assertIn("unsupported report field: target_revision_id", result["validation_errors"])

    def test_external_authorization_requires_exact_live_workflow_scope(self):
        cases = (
            ("missing", lambda req: req.pop("external_authorization")),
            ("expired", lambda req: req["external_authorization"].update(expires_at="2000-01-01T00:00:00Z")),
            ("wrong target", lambda req: req["external_authorization"].update(target_id="other-target")),
            ("wrong workflow", lambda req: req["external_authorization"].update(workflow_id="other-workflow")),
            ("wrong reviewer", lambda req: req["external_authorization"].update(reviewer="another service")),
            ("revoked", lambda req: req["external_authorization"].update(revoked=True)),
            ("unauthorized path", lambda req: req["external_authorization"]["permitted_paths"].append("private/secret.txt")),
            ("remote mutation", lambda req: req["external_authorization"]["operation_effects"].update(may_mutate_state=True)),
            ("wrong workflow-owner identity", lambda req: req["external_authorization"]["workflow_owner_verification"].update(verified_by="unverified-person")),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
                request = self.request(cycle)
                mutate(request)
                if "external_authorization" in request:
                    auth = request["external_authorization"]
                    auth["workflow_owner_verification"]["verified_scope_sha256"] = authorization_scope_fingerprint(auth)
                runner = Mock()
                result = run_claude_review(request, aily_executable=self.fake_aily, process_runner=runner)
                self.assertFalse(result["report_valid"])
                self.assertEqual(result["failure_kind"], "request_validation_failure")
                runner.assert_not_called()

    def test_missing_evidence_artifact_blocks_before_external_invocation(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        (self.target / request["evidence_artifacts"][0]["artifact_reference"]).unlink()
        runner = Mock()
        result = run_claude_review(request, aily_executable=self.fake_aily, process_runner=runner)
        self.assertEqual(result["failure_kind"], "request_validation_failure")
        self.assertIn("does not exist", result["error"])
        runner.assert_not_called()

    def test_evidence_hash_mismatch_blocks_before_external_invocation(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        request["evidence_artifacts"][0]["sha256"] = "0" * 64
        runner = Mock()
        result = run_claude_review(request, aily_executable=self.fake_aily, process_runner=runner)
        self.assertEqual(result["failure_kind"], "request_validation_failure")
        self.assertIn("content hash", result["error"])
        runner.assert_not_called()

    def test_evidence_artifact_symlink_escape_is_rejected(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        artifact_path = self.target / request["evidence_artifacts"][0]["artifact_reference"]
        outside = self.base / "outside-evidence.json"
        outside.write_text("{}", encoding="utf-8")
        artifact_path.unlink()
        artifact_path.symlink_to(outside)
        runner = Mock()
        result = run_claude_review(request, aily_executable=self.fake_aily, process_runner=runner)
        self.assertEqual(result["failure_kind"], "request_validation_failure")
        self.assertIn("symlink", result["error"])
        runner.assert_not_called()

    def test_new_workflow_id_cannot_reset_increment_round_budget(self):
        prior = self.target / ".engineering/workflows/workflow-old"
        prior.mkdir()
        prior_state = {
            "workflow_id": "workflow-old",
            "target": {"target_id": "test-target-1", "git_root": str(self.target.resolve())},
            "scope": {"increment_id": "INC-1"},
            "claude_review_cycles": [{"cycle_id": "old-cycle", "increment_id": "INC-1", "round_count": 1}],
        }
        (prior / "workflow-state.json").write_text(json.dumps(prior_state), encoding="utf-8")
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        runner = Mock()
        result = run_claude_review(request, aily_executable=self.fake_aily, process_runner=runner)
        self.assertEqual(result["failure_kind"], "request_validation_failure")
        self.assertIn("another workflow", result["error"])
        runner.assert_not_called()

    def test_single_link_ds_store_in_workflow_root_does_not_block_review(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        metadata = self.workflow_root / ".DS_Store"
        metadata.write_bytes(b"Finder metadata")

        result = run_claude_review(
            request, aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(self.envelope(self.report(cycle))),
        )

        self.assertTrue(result["report_valid"], result.get("error"))
        self.assertEqual(metadata.read_bytes(), b"Finder metadata")

    def test_same_round_cannot_be_invoked_twice_after_failure(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        runner = self.mocked_runner(b"", b"transient failure", 1)
        first = run_claude_review(request, aily_executable=self.fake_aily, process_runner=runner)
        self.assertFalse(first["report_valid"])
        second_runner = Mock()
        second = run_claude_review(request, aily_executable=self.fake_aily, process_runner=second_runner)
        self.assertEqual(second["failure_kind"], "request_validation_failure")
        self.assertIn("reserved review cycle", second["error"])
        second_runner.assert_not_called()

    def test_review_manifest_is_order_independent_and_binds_target_round_and_inputs(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        request = self.request(cycle)
        normalized, target, env = _validate_request(request)
        diff, _ = _capture_diff(target, env, normalized["baseline_commit"], normalized["scope_paths"])
        context = _read_context(target, normalized["context_paths"])
        evidence = _read_verified_evidence_artifacts(normalized, target)
        original = _review_manifest(normalized, target, env, diff, context, evidence)["sha256"]
        reordered = dict(normalized)
        reordered["scope_paths"] = list(reversed(normalized["scope_paths"]))
        reordered["context_paths"] = list(reversed(normalized["context_paths"]))
        self.assertEqual(original, _review_manifest(reordered, target, env, diff, context, evidence)["sha256"])
        reordered["review_id"] = "other-review"
        self.assertNotEqual(original, _review_manifest(reordered, target, env, diff, context, evidence)["sha256"])

    def test_workflow_cycle_persistence_transitions_to_gate_or_human_blocker(self):
        cycle = self.cycle()
        state = {"claude_review_cycles": [], "status": "ACTIVE", "phase": "INDEPENDENT_VERIFICATION"}
        pending = apply_review_cycle_to_workflow_state(state, cycle, recorded_at="2026-11-06T12:00:00Z")
        self.assertEqual(pending["claude_review_cycles"][0]["cycle_id"], cycle["cycle_id"])
        blocked_cycle = dict(cycle, status="human_intervention_required", human_escalation=True,
                             human_escalation_reason="Unresolved review finding.")
        blocked = apply_review_cycle_to_workflow_state(pending, blocked_cycle, recorded_at="2026-11-06T12:01:00Z")
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["blocker"]["affected_phase"], "INDEPENDENT_VERIFICATION")
        rejected_cycle = dict(cycle, status="review_rejected", human_escalation=False)
        rejected = apply_review_cycle_to_workflow_state(pending, rejected_cycle, recorded_at="2026-11-06T12:01:30Z")
        self.assertEqual(rejected["status"], "BLOCKED")
        self.assertIn("valid report", rejected["blocker"]["reason"])
        self.assertIn("explicitly authorized recovery", rejected["blocker"]["safe_recovery_path"])
        complete = dict(cycle, status="complete", human_escalation=False)
        gate_ready = apply_review_cycle_to_workflow_state(pending, complete, recorded_at="2026-11-06T12:02:00Z")
        self.assertEqual(gate_ready["phase"], "VERIFICATION_GATE")
        self.assertEqual(gate_ready["status"], "ACTIVE")

    def test_e2e_scenario_a_two_round_correction_proceeds_to_verification_gate(self):
        cycle1 = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        first_report = self.report(cycle1, [self.finding()])
        cycle1, _, _ = self.invoke_cycle(cycle1, first_report, "scenario-a-r1.json")
        cycle1 = record_finding_decision(
            cycle1, finding_id="F-1", decision="ACCEPT", reason="Confirmed and corrected.",
            evidence_references=["EV-TEST-1"], resolution_evidence_references=["EV-TEST-2"],
        )
        self.persist_cycle(cycle1)
        (self.target / "src/app.py").write_text("def value():\n    return 3\n", encoding="utf-8")
        cycle2 = reserve_review_round(cycle1, target_revision_id=REVISION_2,
                                      verification_evidence_references=["EV-TEST-2"])
        prior = [{"id": "F-1", "status": "resolved", "evidence_references": ["EV-TEST-2"]}]
        second_report = self.report(cycle2, prior=prior, revision=REVISION_2)
        cycle2, _, state = self.invoke_cycle(
            cycle2, second_report, "scenario-a-r2.json", revision=REVISION_2,
            previous_findings=[self.finding()], decisions=cycle1["finding_decisions"],
        )
        cycle2 = finalize_review_cycle(cycle2)
        state = self.persist_cycle(cycle2)
        resumed = json.loads((self.workflow_dir / "workflow-state.json").read_text(encoding="utf-8"))
        self.assertEqual(resumed["claude_review_cycles"][0]["round_count"], 2)
        self.assertEqual(resumed["claude_review_cycles"][0]["status"], "complete")
        self.assertEqual(resumed["phase"], "VERIFICATION_GATE")
        self.assertEqual(resumed["status"], "ACTIVE")

    def test_e2e_scenario_b_unresolved_finding_blocks_after_round_two_without_third_call(self):
        invocations = []
        cycle1 = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        first_report = self.report(cycle1, [self.finding()])
        request1 = self.request(cycle1)
        result1 = run_claude_review(
            request1, aily_executable=self.fake_aily,
            process_runner=lambda command, **kwargs: (invocations.append(command) or
                subprocess.CompletedProcess(command, 0, self.envelope(first_report), b"")),
        )
        self.assertTrue(result1["report_valid"], result1.get("error"))
        report1_ref = ".engineering/workflows/workflow-1/reports/scenario-b-r1.json"
        (self.target / report1_ref).write_text(json.dumps(result1["report"]), encoding="utf-8")
        evidence1_ref = ".engineering/workflows/workflow-1/evidence/scenario-b-r1.json"
        (self.target / evidence1_ref).write_text("review evidence r1\n", encoding="utf-8")
        cycle1 = record_review_result(
            cycle1, result1, report_reference=report1_ref, evidence_reference=evidence1_ref,
            target_path=self.target,
            workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        cycle1 = record_finding_decision(
            cycle1, finding_id="F-1", decision="ACCEPT", reason="Correction attempted and verified.",
            evidence_references=["EV-TEST-1"], resolution_evidence_references=["EV-TEST-2"],
        )
        self.persist_cycle(cycle1, [report1_ref, evidence1_ref])
        (self.target / "src/app.py").write_text("def value():\n    return 3\n", encoding="utf-8")
        cycle2 = reserve_review_round(cycle1, target_revision_id=REVISION_2,
                                      verification_evidence_references=["EV-TEST-2"])
        unresolved = [{"id": "F-1", "status": "unresolved", "evidence_references": ["EV-TEST-2"]}]
        second_report = self.report(cycle2, status="CHANGES_REQUESTED", prior=unresolved, revision=REVISION_2)
        request2 = self.request(cycle2, revision=REVISION_2, previous_findings=[self.finding()],
                                decisions=cycle1["finding_decisions"])
        result2 = run_claude_review(
            request2, aily_executable=self.fake_aily,
            process_runner=lambda command, **kwargs: (invocations.append(command) or
                subprocess.CompletedProcess(command, 0, self.envelope(second_report), b"")),
        )
        self.assertTrue(result2["report_valid"], result2.get("error"))
        report2_ref = ".engineering/workflows/workflow-1/reports/scenario-b-r2.json"
        (self.target / report2_ref).write_text(json.dumps(result2["report"]), encoding="utf-8")
        evidence2_ref = ".engineering/workflows/workflow-1/evidence/scenario-b-r2.json"
        (self.target / evidence2_ref).write_text("review evidence r2\n", encoding="utf-8")
        cycle2 = record_review_result(
            cycle2, result2, report_reference=report2_ref, evidence_reference=evidence2_ref,
            target_path=self.target,
            workflow_state_path=".engineering/workflows/workflow-1/workflow-state.json",
        )
        cycle2 = finalize_review_cycle(cycle2)
        state = self.persist_cycle(cycle2, [report2_ref, evidence2_ref])
        resumed = json.loads((self.workflow_dir / "workflow-state.json").read_text(encoding="utf-8"))
        self.assertEqual(resumed["status"], "BLOCKED")
        self.assertEqual(resumed["phase"], "INDEPENDENT_VERIFICATION")
        self.assertEqual(resumed["claude_review_cycles"][0]["status"], "human_intervention_required")
        self.assertIn("F-1", resumed["claude_review_cycles"][0]["unresolved_finding_ids"])
        self.assertEqual(len(resumed["claude_review_cycles"][0]["finding_decisions"]), 1)
        self.assertTrue(resumed["blocker"]["reason"])
        self.assertEqual(len(invocations), 2)
        with self.assertRaisesRegex(ReviewError, "third round"):
            reserve_review_round(cycle2, target_revision_id=REVISION_2,
                                 verification_evidence_references=["EV-TEST-2"])


    def test_authentication_failure_is_distinguished_and_redacted(self):
        cycle = reserve_review_round(self.cycle(), target_revision_id=REVISION_1)
        result = run_claude_review(
            self.request(cycle), aily_executable=self.fake_aily,
            process_runner=self.mocked_runner(b"", b"401 Unauthorized; api_key=super-secret-value", 1),
        )
        self.assertEqual(result["failure_kind"], "authentication_failure")
        self.assertNotIn("super-secret-value", result["process"]["stderr_summary"])


if __name__ == "__main__":
    unittest.main()

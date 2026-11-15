from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from workflow import traceability  # noqa: E402


def make_ledger(*, partial_policy: bool = False, externally_blocked: bool = False) -> dict:
    commitments = [
        {
            "id": "C-001",
            "class": "mandatory_user_requirement",
            "statement": "Process each accepted request.",
            "acceptance_condition": "The process function handles a valid request.",
            "implementation_expectations": ["Implement the processing path."],
            "verification_scenarios": [{"id": "scenario-process", "description": "Run a valid request."}],
            "availability": "active",
        },
        {
            "id": "C-002",
            "class": "optional_recommendation",
            "statement": "Consider an optional diagnostic endpoint.",
            "acceptance_condition": "Only required if selected by the user.",
            "implementation_expectations": ["No implementation is required unless promoted."],
            "verification_scenarios": [],
            "availability": "active",
        },
        {
            "id": "C-003",
            "class": "accepted_architectural_commitment",
            "statement": "Keep persistence behind the store module.",
            "acceptance_condition": "Processing calls the repository store boundary.",
            "implementation_expectations": ["Use the approved store module."],
            "verification_scenarios": [],
            "availability": "active",
        },
    ]
    if externally_blocked:
        commitments[0]["availability"] = "externally_blocked"
    ledger = {"schema_version": 1, "commitments": commitments}
    if partial_policy:
        ledger["partial_delivery_policy"] = {
            "reason": "A partial release is authorized when the processor integration is unavailable.",
            "authorizations": [{
                "commitment_id": "C-001",
                "allowed_dispositions": ["externally_blocked"],
            }],
        }
    return ledger


def proposal_for(ledger: dict) -> str:
    return "Proposal text.\n\n```commitment-ledger\n" + json.dumps(ledger) + "\n```\n"


def evidence_for(*, event_id: str = "event-1", test_id: str = "tests/test_service.py::test_process",
                 include_external_reason: bool = False) -> dict:
    entry = {
        "commitment_id": "C-001",
        "source_refs": [{"path": "src/service.py", "symbol": "process_request"}],
        "validation_refs": [{
            "scenario_id": "scenario-process",
            "test_ids": [test_id],
            "execution_event_ids": [event_id],
        }],
        "disposition": None,
        "reason": None,
        "operation_id": "op-1",
    }
    if include_external_reason:
        entry.update(disposition="externally_blocked", reason="Processor test dependency is unavailable.")
        entry["source_refs"] = []
        entry["validation_refs"] = []
    return {
        "schema_version": 1,
        "commitments": [entry, {
            "commitment_id": "C-002", "source_refs": [], "validation_refs": [],
            "disposition": None, "reason": None, "operation_id": "op-1",
        }, {
            "commitment_id": "C-003",
            "source_refs": [{"path": "src/service.py", "symbol": "process_request"}],
            "validation_refs": [], "disposition": None, "reason": None, "operation_id": "op-1",
        }],
    }


def assessment_for(*, c1: str = "fulfilled", verification_status: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "commitments": [
            {
                "id": "C-001", "implementation_status": "implemented",
                "verification_status": verification_status or ("passed" if c1 == "fulfilled" else "blocked"),
                "assessment": c1, "explanation": "Reviewed implementation and scenario evidence.",
                "evidence_refs": {"source": ["src/service.py#process_request"],
                                  "validation": ["scenario-process", "tests/test_service.py::test_process"]},
            },
            {
                "id": "C-003", "implementation_status": "implemented", "verification_status": "not_run",
                "assessment": "fulfilled", "explanation": "The approved store boundary is preserved.",
                "evidence_refs": {"source": ["src/service.py#process_request"], "validation": []},
            },
        ],
    }


def junit_artifact_hash() -> str:
    xml = b'<testsuite><testcase classname="tests.test_service" name="test_process" file="tests/test_service.py"/></testsuite>'
    return hashlib.sha256(xml).hexdigest()


def operation_for(events: list[dict], *, last_tool_event_index: int = 1, started_at: str = "2020-01-01T00:00:00+00:00") -> dict:
    return {
        "operation_id": "op-1", "operation_type": "codex", "stage": "implementation", "status": "completed",
        "started_at": started_at,
        "last_tool_event_index": last_tool_event_index,
        "target_fingerprint": {"status": "captured", "working_tree_sha256": "tree-1"},
        "test_execution_events": events,
    }


def junit_evidence_and_operation(target: Path, *, started_at: str = "2020-01-01T00:00:00+00:00") -> tuple[dict, dict]:
    artifact = target / "junit.xml"
    artifact.write_bytes(
        b'<testsuite><testcase classname="tests.test_service" name="test_process" file="tests/test_service.py"/></testsuite>',
    )
    fingerprint = {"status": "captured", "working_tree_sha256": "tree-1"}
    evidence = evidence_for()
    evidence["commitments"][0]["validation_refs"][0].update(
        artifact_path="junit.xml", artifact_sha256=junit_artifact_hash(),
    )
    receipt = {
        "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0,
        "tests": [], "junit_report_path": "junit.xml",
        "junit_artifact": {
            "path": "junit.xml", "sha256": junit_artifact_hash(),
            "mtime_ns": artifact.stat().st_mtime_ns, "event_id": "event-1",
            "operation_id": "op-1", "target_fingerprint": fingerprint,
        },
    }
    operation = operation_for([receipt], started_at=started_at)
    operation["target_fingerprint"] = fingerprint
    return evidence, operation


class CommitmentTraceabilityTests(unittest.TestCase):
    def test_partial_delivery_policy_uses_one_explicit_canonical_shape(self):
        ledger = make_ledger(partial_policy=True)
        parsed = traceability.parse_commitment_ledger(proposal_for(ledger))
        self.assertEqual(parsed["partial_delivery_policy"], ledger["partial_delivery_policy"])

        for invalid_policy, expected_error in (
            (None, "must be an object"),
            ({"authorizations": [{"commitment_id": "C-001", "allowed_dispositions": ["deferred"]}]},
             "reason must be a non-empty string at the policy level"),
            ({"reason": "approved", "authorizations": []}, "must be a non-empty array"),
            ({"reason": "approved"}, "must be a non-empty array"),
            ({"reason": "approved", "authorizations": "C-001"}, "must be a non-empty array"),
            ({"reason": "approved", "authorizations": [{"commitment_id": "C-001"}]},
             "allowed_dispositions must be a non-empty array"),
            ({"reason": "approved", "authorizations": [{
                "commitment_id": "C-001", "allowed_dispositions": ["deferred"], "comment": "extra",
            }]}, "contains unsupported fields"),
            ({"reason": "approved", "authorizations": [{
                "commitment_id": "C-001", "allowed_dispositions": ["deferred", "deferred"],
            }]}, "contains duplicates"),
            ({"reason": "approved", "authorizations": [{
                "commitment_id": "C-001", "allowed_dispositions": ["deferred"], "reason": "nested",
            }]}, "cannot contain reason"),
        ):
            with self.subTest(policy=invalid_policy):
                invalid_ledger = make_ledger()
                invalid_ledger["partial_delivery_policy"] = invalid_policy
                with self.assertRaisesRegex(traceability.TraceabilityError, expected_error):
                    traceability.parse_commitment_ledger(proposal_for(invalid_ledger))

    def test_no_partial_authorization_is_canonically_represented_by_omission(self):
        ledger = make_ledger()
        self.assertNotIn("partial_delivery_policy", traceability.parse_commitment_ledger(proposal_for(ledger)))
        ledger["partial_delivery_policy"] = None
        with self.assertRaisesRegex(traceability.TraceabilityError, "must be an object"):
            traceability.parse_commitment_ledger(proposal_for(ledger))

    def test_ledger_parses_material_classes_and_rejects_duplicate_ids(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        self.assertEqual(ledger["commitments"][0]["class"], "mandatory_user_requirement")
        self.assertEqual(ledger["commitments"][1]["class"], "optional_recommendation")

        duplicate = make_ledger()
        duplicate["commitments"][1]["id"] = "C-001"
        with self.assertRaises(traceability.TraceabilityError):
            traceability.parse_commitment_ledger(proposal_for(duplicate))

    def test_required_validation_scenario_must_define_scenario(self):
        ledger = make_ledger()
        ledger["commitments"][0]["class"] = "required_validation_scenario"
        ledger["commitments"][0]["verification_scenarios"] = []
        with self.assertRaisesRegex(traceability.TraceabilityError, "has no scenario"):
            traceability.parse_commitment_ledger(proposal_for(ledger))

    def test_availability_is_required_and_enum_values_are_type_checked(self):
        ledger = make_ledger()
        del ledger["commitments"][0]["availability"]
        with self.assertRaisesRegex(traceability.TraceabilityError, "availability"):
            traceability.parse_commitment_ledger(proposal_for(ledger))

        ledger = make_ledger()
        ledger["commitments"][0]["availability"] = []
        with self.assertRaisesRegex(traceability.TraceabilityError, "availability"):
            traceability.parse_commitment_ledger(proposal_for(ledger))

    def test_codex_evidence_requires_stable_source_and_test_event_references(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        report = "```harness-evidence\n" + json.dumps(evidence_for()) + "\n```"
        parsed = traceability.parse_codex_evidence(report, ledger)
        self.assertEqual(parsed["commitments"][0]["validation_refs"][0]["execution_event_ids"], ["event-1"])

        invalid = evidence_for()
        invalid["commitments"][0]["source_refs"] = [{"path": "../outside.py", "symbol": "bad"}]
        parsed_invalid = traceability.parse_codex_evidence(
            "```harness-evidence\n" + json.dumps(invalid) + "\n```", ledger,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = traceability.evaluate_ledger(
                ledger, parsed_invalid, assessment_for(), target=Path(directory), reviewed_paths={"../outside.py"},
                operations=[], final_fingerprint={"working_tree_sha256": "tree"},
            )
        self.assertNotEqual(result["commitments"][0]["source_evidence_status"], "valid")
        self.assertEqual(result["disposition"], "blocked")

    def test_claude_assessment_requires_every_id_and_valid_independent_statuses(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        report = "```commitment-assessment\n" + json.dumps(assessment_for()) + "\n```"
        parsed = traceability.parse_claude_assessment(report, ledger)
        self.assertEqual(parsed["commitments"][0]["assessment"], "fulfilled")
        partial = assessment_for()
        partial["commitments"].pop()
        with self.assertRaisesRegex(traceability.TraceabilityError, "missing commitment IDs"):
            traceability.parse_claude_assessment(
                "```commitment-assessment\n" + json.dumps(partial) + "\n```", ledger,
            )

    def test_complete_requires_specific_passing_test_and_reviewed_source(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        evidence = evidence_for()
        assessment = assessment_for()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operations = [{
                "operation_id": "op-1", "operation_type": "codex", "stage": "implementation", "status": "completed",
                "last_tool_event_index": 1,
                "target_fingerprint": {"status": "captured", "working_tree_sha256": "tree-1"},
                "test_execution_events": [{"event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                ]}],
            }]
            result = traceability.evaluate_ledger(
                ledger, evidence, assessment, target=target, reviewed_paths={"src/service.py"},
                operations=operations, final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["disposition"], "complete")
        self.assertEqual(result["commitments"][0]["verification_status"], "passed")
        self.assertIsNone(result["commitments"][1]["claude_assessment"])

    def test_test_event_followed_by_another_tool_event_remains_valid(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operation = operation_for([
                {"event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                ]},
                {"event_id": "event-later", "event_index": 4, "runner": "unknown", "exit_code": None, "tests": []},
            ], last_tool_event_index=4)
            result = traceability.evaluate_ledger(
                ledger, evidence_for(), assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "passed")
        self.assertEqual(result["disposition"], "complete")

    def test_two_execution_events_can_contribute_to_one_scenario(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        evidence = evidence_for()
        evidence["commitments"][0]["validation_refs"][0]["test_ids"].append(
            "tests/test_service.py::test_secondary",
        )
        evidence["commitments"][0]["validation_refs"][0]["execution_event_ids"].append("event-2")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operation = operation_for([
                {"event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                ]},
                {"event_id": "event-2", "event_index": 3, "runner": "unittest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_secondary", "status": "passed"},
                ]},
            ], last_tool_event_index=5)
            result = traceability.evaluate_ledger(
                ledger, evidence, assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "passed")
        self.assertEqual(result["disposition"], "complete")

    def test_missing_event_cannot_be_masked_by_a_successful_event(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        evidence = evidence_for()
        evidence["commitments"][0]["validation_refs"][0]["execution_event_ids"].append("missing-event")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operation = operation_for([{
                "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                ],
            }])
            result = traceability.evaluate_ledger(
                ledger, evidence, assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
        self.assertEqual(result["disposition"], "blocked")

    def test_nonzero_test_command_with_only_passing_results_is_unverified(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operation = operation_for([{
                "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 1, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                ],
            }])
            result = traceability.evaluate_ledger(
                ledger, evidence_for(), assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
        self.assertEqual(result["disposition"], "blocked")

    def test_observed_failure_is_not_hidden_by_an_unsupported_runner_label(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operation = operation_for([{
                "event_id": "event-1", "event_index": 1, "runner": "unknown", "exit_code": 1, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "failed"},
                ],
            }])
            result = traceability.evaluate_ledger(
                ledger, evidence_for(), assessment_for(verification_status="failed"), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "failed")
        self.assertEqual(result["disposition"], "blocked")

    def test_failed_test_and_invalid_reference_preserves_failed_status(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        evidence = evidence_for()
        evidence["commitments"][0]["validation_refs"].append({
            "scenario_id": "missing-scenario",
            "test_ids": ["tests/test_service.py::test_missing"],
            "execution_event_ids": ["missing-event"],
        })
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operation = operation_for([{
                "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 1, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "failed"},
                ],
            }])
            result = traceability.evaluate_ledger(
                ledger, evidence, assessment_for(verification_status="failed"), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "failed")
        self.assertEqual(result["disposition"], "blocked")

    def test_failed_test_remains_visible_alongside_another_passing_event(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        evidence = evidence_for()
        evidence["commitments"][0]["validation_refs"][0]["test_ids"].append(
            "tests/test_service.py::test_secondary",
        )
        evidence["commitments"][0]["validation_refs"][0]["execution_event_ids"].append("event-2")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operation = operation_for([
                {"event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 1, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "failed"},
                ]},
                {"event_id": "event-2", "event_index": 2, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_secondary", "status": "passed"},
                ]},
            ], last_tool_event_index=3)
            result = traceability.evaluate_ledger(
                ledger, evidence, assessment_for(verification_status="failed"), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "failed")
        self.assertEqual(result["disposition"], "blocked")

    def test_failed_test_with_authorized_deferral_or_waiver_is_blocked(self):
        for disposition in ("deferred", "waived"):
            with self.subTest(disposition=disposition), tempfile.TemporaryDirectory() as directory:
                ledger_data = make_ledger()
                ledger_data["partial_delivery_policy"] = {
                    "reason": "The approved policy permits deferral only when no required test failed.",
                    "authorizations": [{
                        "commitment_id": "C-001", "allowed_dispositions": [disposition],
                    }],
                }
                ledger = traceability.parse_commitment_ledger(proposal_for(ledger_data))
                evidence = evidence_for()
                evidence["commitments"][0].update(
                    disposition=disposition, reason="Recorded approved disposition.",
                )
                target = Path(directory)
                (target / "src").mkdir()
                (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
                operation = operation_for([{
                    "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 1, "tests": [
                        {"test_id": "tests/test_service.py::test_process", "status": "failed"},
                    ],
                }])
                result = traceability.evaluate_ledger(
                    ledger, evidence, assessment_for(c1="partially_fulfilled", verification_status="failed"),
                    target=target, reviewed_paths={"src/service.py"}, operations=[operation],
                    final_fingerprint={"working_tree_sha256": "tree-1"},
                )
            self.assertEqual(result["commitments"][0]["verification_status"], "failed")
            self.assertEqual(result["disposition"], "blocked")

    def test_duplicate_test_ids_are_unverified(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operation = operation_for([{
                "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                ],
            }])
            result = traceability.evaluate_ledger(
                ledger, evidence_for(), assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
        self.assertEqual(result["disposition"], "blocked")

    def test_claude_verification_must_agree_with_harness(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        for claude_status in ("failed", "unverified"):
            with self.subTest(claude_status=claude_status), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                (target / "src").mkdir()
                (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
                operation = operation_for([{
                    "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                        {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                    ],
                }])
                result = traceability.evaluate_ledger(
                    ledger, evidence_for(), assessment_for(verification_status=claude_status),
                    target=target, reviewed_paths={"src/service.py"}, operations=[operation],
                    final_fingerprint={"working_tree_sha256": "tree-1"},
                )
            self.assertEqual(result["commitments"][0]["verification_status"], "passed")
            self.assertEqual(result["commitments"][0]["claude_verification_status"], claude_status)
            self.assertEqual(result["disposition"], "blocked")

    def test_aggregate_success_without_matching_test_is_unverified_and_blocked(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operations = [{
                "operation_id": "op-1", "operation_type": "codex", "stage": "implementation", "status": "completed",
                "last_tool_event_index": 1,
                "target_fingerprint": {"status": "captured", "working_tree_sha256": "tree-1"},
                "test_execution_events": [{"event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_unrelated", "status": "passed"},
                ]}],
            }]
            result = traceability.evaluate_ledger(
                ledger, evidence_for(), assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=operations,
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["disposition"], "blocked")
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")

    def test_invalid_validation_reference_cannot_be_masked_by_a_passing_reference(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        evidence = evidence_for()
        evidence["commitments"][0]["validation_refs"].append({
            "scenario_id": "unknown-scenario",
            "test_ids": ["tests/test_service.py::test_process"],
            "execution_event_ids": ["missing-event"],
        })
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operations = [{
                "operation_id": "op-1", "operation_type": "codex", "stage": "implementation", "status": "completed",
                "last_tool_event_index": 1,
                "target_fingerprint": {"status": "captured", "working_tree_sha256": "tree-1"},
                "test_execution_events": [{"event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                ]}],
            }]
            result = traceability.evaluate_ledger(
                ledger, evidence, assessment_for(), target=target, reviewed_paths={"src/service.py"},
                operations=operations, final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
        self.assertEqual(result["disposition"], "blocked")

    def test_stale_execution_ownership_is_unverified(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operations = [{
                "operation_id": "op-1", "operation_type": "codex", "stage": "implementation", "status": "completed",
                "last_tool_event_index": 1,
                "target_fingerprint": {"status": "captured", "working_tree_sha256": "stale-tree"},
                "test_execution_events": [{"event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                ]}],
            }]
            result = traceability.evaluate_ledger(
                ledger, evidence_for(), assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=operations,
                final_fingerprint={"working_tree_sha256": "current-tree"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
        self.assertEqual(result["disposition"], "blocked")

    def test_duplicate_operation_ids_are_ambiguous(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        event = {
            "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0,
            "tests": [{"test_id": "tests/test_service.py::test_process", "status": "passed"}],
        }
        operation = operation_for([event])
        duplicate = dict(operation)
        duplicate["test_execution_events"] = [dict(event)]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            result = traceability.evaluate_ledger(
                ledger, evidence_for(), assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation, duplicate],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
        self.assertEqual(result["disposition"], "blocked")

    def test_duplicate_event_ids_are_ambiguous(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        event = {
            "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0,
            "tests": [{"test_id": "tests/test_service.py::test_process", "status": "passed"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            result = traceability.evaluate_ledger(
                ledger, evidence_for(), assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation_for([event, dict(event)])],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
        self.assertEqual(result["disposition"], "blocked")

    def test_event_from_another_operation_cannot_satisfy_the_reference(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        evidence = evidence_for()
        evidence["commitments"][0]["operation_id"] = "op-2"
        evidence["commitments"][0]["validation_refs"][0]["execution_event_ids"] = ["event-1"]
        first_operation = operation_for([{
            "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                {"test_id": "tests/test_service.py::test_process", "status": "passed"},
            ],
        }])
        first_operation["operation_id"] = "op-1"
        second_operation = operation_for([{
            "event_id": "event-2", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                {"test_id": "tests/test_service.py::test_process", "status": "passed"},
            ],
        }])
        second_operation["operation_id"] = "op-2"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            result = traceability.evaluate_ledger(
                ledger, evidence, assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[first_operation, second_operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
        self.assertEqual(result["disposition"], "blocked")

    def test_implementation_evidence_is_stale_after_a_correction_operation(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        implementation = operation_for([{
            "event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                {"test_id": "tests/test_service.py::test_process", "status": "passed"},
            ],
        }])
        correction = operation_for([{
            "event_id": "event-2", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                {"test_id": "tests/test_service.py::test_process", "status": "passed"},
            ],
        }])
        correction.update(operation_id="op-2", stage="correction")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            result = traceability.evaluate_ledger(
                ledger, evidence_for(), assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[implementation, correction],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
        self.assertEqual(result["disposition"], "blocked")

    def test_source_path_traversal_and_symlink_escape_do_not_verify(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            target = Path(directory)
            (target / "src").mkdir()
            (Path(outside) / "secret.py").write_text("secret", encoding="utf-8")
            (target / "src/link.py").symlink_to(Path(outside) / "secret.py")
            for path in ("../secret.py", "src/link.py"):
                evidence = evidence_for()
                evidence["commitments"][0]["source_refs"] = [{"path": path, "symbol": "process_request"}]
                result = traceability.evaluate_ledger(
                    ledger, evidence, assessment_for(), target=target,
                    reviewed_paths={path}, operations=[], final_fingerprint={"working_tree_sha256": "tree"},
                )
                self.assertNotEqual(result["commitments"][0]["source_evidence_status"], "valid")
                self.assertEqual(result["disposition"], "blocked")

    def test_junit_results_bind_to_test_id_hash_and_command_event(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            evidence, operation = junit_evidence_and_operation(target)
            result = traceability.evaluate_ledger(
                ledger, evidence, assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["disposition"], "complete")

    def test_stale_junit_artifact_cannot_satisfy_a_new_execution_event(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            evidence, operation = junit_evidence_and_operation(target, started_at="2999-01-01T00:00:00+00:00")
            result = traceability.evaluate_ledger(
                ledger, evidence, assessment_for(), target=target,
                reviewed_paths={"src/service.py"}, operations=[operation],
                final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
        self.assertEqual(result["disposition"], "blocked")

    def test_junit_artifact_must_match_its_operation_and_target_snapshot(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        for field, value in (
            ("operation_id", "old-operation"),
            ("target_fingerprint", {"status": "captured", "working_tree_sha256": "stale-tree"}),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                (target / "src").mkdir()
                (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
                evidence, operation = junit_evidence_and_operation(target)
                operation["test_execution_events"][0]["junit_artifact"][field] = value
                result = traceability.evaluate_ledger(
                    ledger, evidence, assessment_for(), target=target,
                    reviewed_paths={"src/service.py"}, operations=[operation],
                    final_fingerprint={"working_tree_sha256": "tree-1"},
                )
            self.assertEqual(result["commitments"][0]["verification_status"], "unverified")
            self.assertEqual(result["disposition"], "blocked")

    def test_unavailable_external_dependency_requires_authorized_partial_policy(self):
        for authorized, expected in ((False, "blocked"), (True, "partial")):
            ledger = traceability.parse_commitment_ledger(
                proposal_for(make_ledger(partial_policy=authorized, externally_blocked=True)),
            )
            evidence = evidence_for(include_external_reason=True)
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                (target / "src").mkdir()
                (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
                result = traceability.evaluate_ledger(
                    ledger, evidence, assessment_for(c1="insufficient_evidence"),
                    target=target, reviewed_paths={"src/service.py"}, operations=[],
                    final_fingerprint={"working_tree_sha256": "tree-1"},
                )
            self.assertEqual(result["disposition"], expected)

    def test_authorized_deferral_stays_visible_and_does_not_become_fulfilled(self):
        ledger_data = make_ledger()
        ledger_data["partial_delivery_policy"] = {
            "reason": "The user approved deferring the optional processor integration in this release.",
            "authorizations": [{"commitment_id": "C-001", "allowed_dispositions": ["deferred"]}],
        }
        ledger = traceability.parse_commitment_ledger(proposal_for(ledger_data))
        evidence = evidence_for()
        evidence["commitments"][0].update(
            source_refs=[], disposition="deferred", reason="Deferred by approved release policy.",
        )
        assessment = assessment_for(c1="partially_fulfilled")
        assessment["commitments"][0]["implementation_status"] = "partial"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operations = [{
                "operation_id": "op-1", "operation_type": "codex", "stage": "implementation", "status": "completed",
                "last_tool_event_index": 1,
                "target_fingerprint": {"status": "captured", "working_tree_sha256": "tree-1"},
                "test_execution_events": [{"event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                ]}],
            }]
            result = traceability.evaluate_ledger(
                ledger, evidence, assessment, target=target, reviewed_paths={"src/service.py"},
                operations=operations, final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["disposition"], "partial")
        self.assertEqual(result["commitments"][0]["disposition"], "deferred")
        self.assertNotEqual(result["commitments"][0]["claude_assessment"], "fulfilled")

    def test_missing_independent_assessment_cannot_produce_complete_delivery(self):
        ledger = traceability.parse_commitment_ledger(proposal_for(make_ledger()))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "src").mkdir()
            (target / "src/service.py").write_text("def process_request(): pass\n", encoding="utf-8")
            operations = [{
                "operation_id": "op-1", "operation_type": "codex", "stage": "implementation", "status": "completed",
                "last_tool_event_index": 1,
                "target_fingerprint": {"status": "captured", "working_tree_sha256": "tree-1"},
                "test_execution_events": [{"event_id": "event-1", "event_index": 1, "runner": "pytest", "exit_code": 0, "tests": [
                    {"test_id": "tests/test_service.py::test_process", "status": "passed"},
                ]}],
            }]
            result = traceability.evaluate_ledger(
                ledger, evidence_for(), None, target=target, reviewed_paths={"src/service.py"},
                operations=operations, final_fingerprint={"working_tree_sha256": "tree-1"},
            )
        self.assertEqual(result["disposition"], "blocked")
        self.assertEqual(result["commitments"][0]["claude_assessment"], None)

    def test_ohpen_incomplete_worker_commitments_remain_blocked(self):
        ledger = {
            "schema_version": 1,
            "commitments": [
                {
                    "id": "C-POOL", "class": "accepted_architectural_commitment",
                    "statement": "Use a configurable bounded worker pool.",
                    "acceptance_condition": "Concurrent jobs are capped by configuration.",
                    "implementation_expectations": ["Implement bounded concurrent job workers."],
                    "verification_scenarios": [], "availability": "active",
                },
                {
                    "id": "C-RECOVERY", "class": "required_validation_scenario",
                    "statement": "Recover a job from Worker A to Worker B while A's processor call is in flight.",
                    "acceptance_condition": "B recovers safely, A's late result is stale, and no duplicate dispatch occurs.",
                    "implementation_expectations": ["Use lease ownership and per-attempt correlation."],
                    "verification_scenarios": [{
                        "id": "worker-a-b-late-response", "description": "Deterministically exercise lease loss and a late response.",
                    }], "availability": "active",
                },
                {
                    "id": "C-LEASE", "class": "accepted_architectural_commitment",
                    "statement": "Renew worker leases during long-running calls.",
                    "acceptance_condition": "Active work renews its lease before expiry.",
                    "implementation_expectations": ["Renew leases while processing."],
                    "verification_scenarios": [], "availability": "active",
                },
                {
                    "id": "C-TIMEOUT", "class": "mandatory_user_requirement",
                    "statement": "Bound processor call duration.",
                    "acceptance_condition": "A stalled processor call reaches the configured timeout behavior.",
                    "implementation_expectations": ["Configure processor-call timeout handling."],
                    "verification_scenarios": [], "availability": "active",
                },
            ],
        }
        assessment = {
            "schema_version": 1,
            "commitments": [{
                "id": item["id"], "implementation_status": "partial",
                "verification_status": "unverified", "assessment": "insufficient_evidence",
                "explanation": "The historical implementation did not fully deliver or validate this commitment.",
                "evidence_refs": {"source": [], "validation": []},
            } for item in ledger["commitments"]],
        }

        with tempfile.TemporaryDirectory() as directory:
            result = traceability.evaluate_ledger(
                ledger, None, assessment, target=Path(directory), reviewed_paths=set(),
                operations=[], final_fingerprint=None,
            )

        self.assertEqual(result["disposition"], "blocked")
        self.assertEqual(
            [item["id"] for item in result["commitments"]],
            ["C-POOL", "C-RECOVERY", "C-LEASE", "C-TIMEOUT"],
        )
        self.assertTrue(all(item["source_evidence_status"] == "missing" for item in result["commitments"]))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from workflow import review_partitioning, review_requests
from workflow import review_text
from workflow.review_campaign import (
    advance, block_campaign_for_authorization, create_campaign_snapshot, sync_campaign_coverage,
)
from workflow.review_models import (
    CampaignInput, CampaignPart, CampaignSnapshot, PreviousResult, ReviewDiff, ReviewSegment,
)
from workflow.review_validation import PART_REVIEW_VALIDATOR_VERSION, validate_part_review_report


def _segment(path: str, text: str) -> ReviewSegment:
    return review_partitioning.review_segment(path, text)


def _part_report(part_id: str, path: str) -> str:
    return (
        f"Decision: APPROVED\nPart ID: {part_id}\nFiles reviewed:\n- `{path}`\n"
        "Findings: None\nCross-component concerns: None\nOverall assessment: adequate."
    )


class ReviewPartitioningTests(unittest.TestCase):
    def test_partition_is_deterministic_lossless_and_keeps_source_order(self):
        diff = ReviewDiff(
            text="patches",
            included_paths=("src/b.py", "src/a.py"),
            excluded_files=(("assets/image.png", "binary"),),
            file_patches=(("src/b.py", "def b():\n    return 2\n"), ("src/a.py", "def a():\n    return 1\n")),
        )
        shared = {"Assignment": "Review changes", "Approved proposal": "Keep behavior"}
        kwargs = dict(campaign_id="campaign", proposal_version=3, limit=100000)

        first, multipart = review_partitioning.partition_review_segments(diff, shared, **kwargs)
        second, repeated_multipart = review_partitioning.partition_review_segments(diff, shared, **kwargs)

        self.assertFalse(multipart)
        self.assertEqual(multipart, repeated_multipart)
        self.assertEqual(first, second)
        self.assertEqual([segment.path for segment in first[0]["segments"]], ["src/b.py", "src/a.py"])
        self.assertIsNone(review_partitioning.review_partition_error(diff, first))
        self.assertEqual(
            review_partitioning.campaign_part_manifest(first),
            review_partitioning.campaign_part_manifest(second),
        )
        self.assertEqual(
            review_partitioning.campaign_source_hash(diff),
            review_partitioning.campaign_source_hash(copy.deepcopy(diff)),
        )

    def test_partition_handles_empty_and_boundary_sized_sources(self):
        empty = ReviewDiff("", (), (), ())
        parts, multipart = review_partitioning.partition_review_segments(
            empty, {}, campaign_id="empty", proposal_version=1, limit=100000,
        )
        self.assertFalse(multipart)
        self.assertEqual(parts, [{"part_id": "part-000001", "segments": []}])

        patch = "one line\n"
        diff = ReviewDiff(patch, ("src/one.py",), (), (("src/one.py", patch),))
        parts, _multipart = review_partitioning.partition_review_segments(
            diff, {}, campaign_id="boundary", proposal_version=1, limit=100000,
        )
        self.assertEqual(parts[0]["segments"][0], _segment("src/one.py", patch))
        self.assertEqual(parts[0]["segments"][0].patch_line_count, 1)

    def test_adaptive_partition_splits_only_at_stable_ranges(self):
        text = "".join(
            f"def operation_{index}():\n" + "    return 1\n" * 8 + "\n"
            for index in range(8)
        )
        diff = ReviewDiff(text, ("src/m.py",), (), (("src/m.py", text),))
        kwargs = dict(campaign_id="adaptive", proposal_version=1, limit=2200)

        parts, multipart = review_partitioning.partition_review_segments(diff, {}, **kwargs)
        repeated, _ = review_partitioning.partition_review_segments(diff, {}, **kwargs)

        self.assertTrue(multipart)
        self.assertEqual(parts, repeated)
        segments = sorted(
            (segment for part in parts for segment in part["segments"]),
            key=lambda segment: segment.patch_line_start,
        )
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(segment.path == "src/m.py" for segment in segments))
        self.assertEqual(segments[0].patch_line_start, 1)
        self.assertEqual(segments[-1].patch_line_end, segments[0].patch_line_count)
        self.assertTrue(all(
            left.patch_line_end + 1 == right.patch_line_start
            for left, right in zip(segments, segments[1:])
        ))
        self.assertIsNone(review_partitioning.review_partition_error(diff, parts))

    def test_exclusions_are_bound_into_source_identity(self):
        patch = "same patch\n"
        first = ReviewDiff(patch, ("src/a.py",), (), (("src/a.py", patch),))
        second = ReviewDiff(patch, ("src/a.py",), (("secret.txt", "sensitive"),), (("src/a.py", patch),))
        self.assertNotEqual(
            review_partitioning.campaign_source_hash(first),
            review_partitioning.campaign_source_hash(second),
        )


class ReviewRequestTests(unittest.TestCase):
    def test_provider_neutral_redaction_and_payload_validation_keep_adapter_behavior(self):
        import claude_reviewer

        self.assertEqual(review_text.redact("Bearer abcdefghijklmnop"), "[REDACTED]")
        self.assertEqual(review_requests.redact_review_text("password=abcdefghijk"), "[REDACTED]")
        with patch.dict("os.environ", {review_text.MAX_REVIEW_PAYLOAD_ENV: "4"}):
            self.assertEqual(review_text.validate_review_invocation("safe", "aily"), 4)
            with self.assertRaisesRegex(review_text.ReviewTextError, "above the configured"):
                review_text.validate_review_invocation("too long", "aily")
            with self.assertRaisesRegex(claude_reviewer.ReviewError, "above the configured"):
                claude_reviewer.validate_review_invocation("too long", "aily")
        self.assertNotIn("claude_reviewer", review_requests.__dict__)

    def test_part_request_has_stable_identity_and_exact_formatted_prompt_hash(self):
        part = CampaignPart("part-000001", (_segment("src/a.py", "def a():\n    return 1\n"),))
        request = review_requests.build_part_request(
            campaign_id="campaign", part=part, purpose="initial", proposal_version=7,
            payload_limit_characters=4000,
            report_contract_version=PART_REVIEW_VALIDATOR_VERSION,
            source_patch_sha256="source-hash", shared_context_sha256="shared-hash",
            shared_context={"Assignment": "Implement A", "Approved proposal": "Preserve behavior"},
            excluded_count=1,
        )
        safe_sections = {
            name: review_requests.redact_review_text(value) if name == "Complete patch"
            else review_requests.redact_review_text(value).strip()
            for name, value in request.section_mapping().items()
        }
        expected = review_requests.format_context_prompt(
            review_requests.redact_review_text(review_requests.review_header()), safe_sections,
        )

        self.assertEqual(request.prompt, expected)
        self.assertEqual(request.prompt_sha256, review_partitioning.review_patch_sha256(expected))
        self.assertEqual(
            request.prompt_sha256,
            "03a5307d6c80026f4aa1f6d5682f07f0455da0f22cd142458a2bad6101ce0ad2",
        )
        self.assertEqual(request.campaign_id, "campaign")
        self.assertEqual(request.part_id, "part-000001")
        self.assertEqual(request.proposal_version, 7)
        self.assertEqual(request.payload_limit_characters, 4000)
        self.assertEqual(request.segment_sha256s, (part.segments[0].patch_sha256,))
        self.assertEqual(request.part_patch_sha256, review_partitioning.review_patch_sha256(
            request.section_mapping()["Complete patch"],
        ))
        detached_sections = request.section_mapping()
        detached_sections["Assignment"] = "mutated"
        self.assertNotEqual(request.section_mapping()["Assignment"], "mutated")
        with self.assertRaises(FrozenInstanceError):
            request.prompt_sha256 = "changed"

    def test_report_validation_returns_typed_findings_and_keeps_snapshot_schema(self):
        segment = _segment("src/a.py", "def a():\n    return 1\n")
        report = (
            "Decision: CHANGES_REQUESTED\nPart ID: part-000001\nFiles reviewed:\n- `src/a.py`\n"
            "Findings:\n### Finding F1\n- Severity: Must fix\n- Category: Correctness\n"
            "- File/location: src/a.py:2\n- Description: The result is wrong.\n"
            "- Impact and reasoning: Callers receive an incorrect value.\n"
            "- Recommended correction: Return the required value.\n"
            "Cross-component concerns: None\nOverall assessment: blocked."
        )

        validation = validate_part_review_report(report, "part-000001", [segment])
        snapshot = CampaignSnapshot.from_mapping({"campaign_id": "c", "status": "planned"})

        self.assertTrue(validation.valid, validation.errors)
        self.assertEqual(validation.findings[0].severity, "Must fix")
        self.assertEqual(validation.findings[0].location, "src/a.py:2")
        self.assertEqual(snapshot.contract_version, 1)
        self.assertEqual(snapshot.to_mapping(), {"campaign_id": "c", "status": "planned"})


class ReviewTransitionTests(unittest.TestCase):
    def setUp(self):
        self.first = CampaignPart("part-000001", (_segment("src/first.py", "def first():\n    return 1\n"),))
        self.second = CampaignPart("part-000002", (_segment("src/second.py", "def second():\n    return 2\n"),))
        self.parts = (self.first, self.second)
        manifest = review_partitioning.campaign_part_manifest([
            {"part_id": part.part_id, "segments": list(part.segments)} for part in self.parts
        ])
        self.campaign = {
            "campaign_id": "campaign",
            "purpose": "initial",
            "header_id": "code-review-part-v2",
            "report_contract_version": PART_REVIEW_VALIDATOR_VERSION,
            "source_patch_sha256": "source-hash",
            "shared_context_sha256": "shared-hash",
            "payload_limit_characters": 4000,
            "parts": manifest,
            "part_reviews": [
                {"part_id": part.part_id, "status": "pending", "report": None, "calls": []}
                for part in self.parts
            ],
            "integration_review": {"required": True, "status": "pending", "report": None, "calls": []},
            "files_reviewed": [],
            "files_not_reviewed": [
                {"path": "src/first.py", "reason": "pending"},
                {"path": "src/second.py", "reason": "pending"},
            ],
            "excluded_files": [],
            "status": "in_progress",
            "consolidated_report": None,
        }
        self.snapshot = CampaignSnapshot.from_mapping(self.campaign)
        self.input = CampaignInput(
            campaign_id="campaign", purpose="initial", source_patch_sha256="source-hash",
            shared_context_sha256="shared-hash", parts=self.parts,
            shared_context=(
                ("Assignment", "Review the code"),
                ("Approved proposal", "Preserve behavior"),
                ("Repository/module map and dependencies", "src contains the changed code"),
            ),
            excluded_files=(), payload_limit_characters=4000, proposal_version=2,
            report_contract_version=PART_REVIEW_VALIDATOR_VERSION,
            part_header=review_requests.review_header(), transition_at="2026-01-01T00:00:00Z",
        )

    def _result(self, request, report=None, *, outcome="completed", call_state="completed", prompt_hash=None):
        return PreviousResult(
            part_id=request.part_id, outcome=outcome, call_state=call_state, report=report,
            prompt_sha256=request.prompt_sha256 if prompt_hash is None else prompt_hash,
            cost_usd=0.02, campaign_call_number=1, received_at="2026-01-01T00:01:00Z",
            request_identity=request.identity, operation_id="operation-1",
            result_sha256=hashlib.sha256(report.encode("utf-8")).hexdigest() if report is not None else None,
        )

    def test_transition_is_deterministic_and_does_not_mutate_inputs(self):
        original_snapshot = self.snapshot.serialized
        original_input = copy.deepcopy(self.input)

        first = advance(self.snapshot, self.input)
        repeated = advance(self.snapshot, self.input)

        self.assertEqual(first, repeated)
        self.assertEqual(first.decision, "request_part")
        self.assertEqual(first.part_id, "part-000001")
        self.assertEqual(first.request.prompt_sha256, repeated.request.prompt_sha256)
        self.assertEqual(self.snapshot.serialized, original_snapshot)
        self.assertEqual(self.input, original_input)

    def test_part_results_evolve_snapshot_then_request_integration_and_complete(self):
        initial = advance(self.snapshot, self.input)
        after_first = advance(self.snapshot, self.input, self._result(
            initial.request, _part_report("part-000001", "src/first.py"),
        ))
        self.assertEqual(after_first.decision, "request_part")
        self.assertEqual(after_first.part_id, "part-000002")
        first_record = after_first.snapshot.to_mapping()["part_reviews"][0]
        self.assertEqual(first_record["status"], "reviewed")
        self.assertEqual(first_record["calls"][0]["cost_usd"], 0.02)
        self.assertEqual(after_first.snapshot.to_mapping()["files_reviewed"], ["src/first.py"])

        after_second = advance(after_first.snapshot, self.input, self._result(
            after_first.request, _part_report("part-000002", "src/second.py"),
        ))
        self.assertEqual(after_second.decision, "request_integration")
        self.assertIn("Individual part reports", after_second.request.section_mapping())
        self.assertEqual(after_second.snapshot.to_mapping()["files_not_reviewed"], [])

        integration_report = (
            "Decision: APPROVED\nConsolidated findings: None\nIntegration findings: None\n"
            "Overall assessment: coherent."
        )
        completed = advance(after_second.snapshot, self.input, self._result(
            after_second.request, integration_report,
        ))
        self.assertEqual(completed.decision, "complete")
        saved = completed.snapshot.to_mapping()
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["consolidated_report"], saved["integration_review"]["report"])
        self.assertEqual(set(saved), set(self.campaign))
        self.assertNotIn("proposal_sha256", saved)
        self.assertIn("context_sha256", saved["part_reviews"][0])

    def test_invalid_and_uncertain_results_are_blocked_without_losing_reports(self):
        first = advance(self.snapshot, self.input)
        invalid = advance(self.snapshot, self.input, self._result(first.request, "not a structured report"))
        self.assertEqual(invalid.decision, "blocked")
        self.assertEqual(invalid.reason, "invalid_report")
        self.assertEqual(invalid.snapshot.to_mapping()["part_reviews"][0]["report"], "not a structured report")
        self.assertEqual(self.snapshot.to_mapping()["part_reviews"][0]["status"], "pending")
        retry = advance(invalid.snapshot, self.input)
        self.assertEqual(retry.decision, "request_part")
        self.assertEqual(retry.part_id, "part-000001")

        uncertain = advance(self.snapshot, self.input, self._result(
            first.request, outcome="uncertain", call_state="interrupted_unknown_cost",
        ))
        self.assertEqual(uncertain.decision, "blocked")
        self.assertEqual(uncertain.reason, "uncertain_result")
        self.assertEqual(uncertain.snapshot, self.snapshot)

        failed = advance(self.snapshot, self.input, self._result(
            first.request, outcome="failed", call_state="failed",
        ))
        self.assertEqual(failed.decision, "blocked")
        self.assertEqual(failed.reason, "provider_failed")
        self.assertEqual(failed.snapshot.to_mapping()["part_reviews"][0]["status"], "call_failed")

    def test_result_identity_and_report_hash_mismatches_do_not_mutate_snapshot(self):
        request = advance(self.snapshot, self.input).request
        valid = self._result(request, _part_report("part-000001", "src/first.py"))
        mismatched_identity = replace(
            valid, request_identity=replace(valid.request_identity, proposal_version=99),
        )
        mismatched_campaign = replace(
            valid, request_identity=replace(valid.request_identity, campaign_id="another-campaign"),
        )
        mismatched_part = replace(valid, part_id="part-000002")
        mismatched_report_hash = replace(valid, result_sha256="0" * 64)
        mismatched_prompt = replace(valid, prompt_sha256="different-prompt")

        for result in (
            mismatched_identity, mismatched_campaign, mismatched_part,
            mismatched_report_hash, mismatched_prompt,
        ):
            with self.subTest(result=result):
                transition = advance(self.snapshot, self.input, result)
                self.assertEqual(transition.decision, "blocked")
                self.assertEqual(transition.reason, "invalid_result")
                self.assertEqual(transition.snapshot, self.snapshot)

    def test_checkpoint_ids_are_unique_and_reordered_checkpoints_follow_part_identity(self):
        reordered = copy.deepcopy(self.campaign)
        reordered["part_reviews"].reverse()
        snapshot = CampaignSnapshot.from_mapping(reordered)
        first_request = advance(snapshot, self.input)
        self.assertEqual(first_request.part_id, "part-000001")
        transitioned = advance(snapshot, self.input, self._result(
            first_request.request, _part_report("part-000001", "src/first.py"),
        ))
        records = {item["part_id"]: item for item in transitioned.snapshot.to_mapping()["part_reviews"]}
        self.assertEqual(records["part-000001"]["status"], "reviewed")
        self.assertEqual(transitioned.part_id, "part-000002")

        duplicated = copy.deepcopy(self.campaign)
        duplicated["part_reviews"][1]["part_id"] = duplicated["part_reviews"][0]["part_id"]
        transition = advance(CampaignSnapshot.from_mapping(duplicated), self.input)
        self.assertEqual(transition.decision, "blocked")
        self.assertEqual(transition.reason, "input_mismatch")
        self.assertIn("duplicate", transition.findings[0])

        missing = copy.deepcopy(self.campaign)
        missing["part_reviews"].pop()
        transition = advance(CampaignSnapshot.from_mapping(missing), self.input)
        self.assertEqual(transition.decision, "blocked")
        self.assertEqual(transition.reason, "input_mismatch")
        self.assertIn("exactly match", transition.findings[0])

    def test_campaign_input_rejects_duplicate_part_ids(self):
        duplicate = CampaignPart("part-000001", self.first.segments)
        with self.assertRaisesRegex(ValueError, "must be unique"):
            CampaignInput(**{**self.input.__dict__, "parts": (self.first, duplicate)})

    def test_input_mismatch_is_blocked_without_snapshot_mutation(self):
        changed_input = CampaignInput(
            **{**self.input.__dict__, "source_patch_sha256": "different-source"},
        )
        transition = advance(self.snapshot, changed_input)
        self.assertEqual(transition.decision, "blocked")
        self.assertEqual(transition.reason, "input_mismatch")
        self.assertEqual(transition.snapshot, self.snapshot)

    def test_contracts_reject_mutable_nested_input_and_unknown_snapshot_version(self):
        with self.assertRaisesRegex(TypeError, "immutable tuple"):
            CampaignInput(
                campaign_id="c", purpose="initial", source_patch_sha256="s",
                shared_context_sha256="c", parts=[], shared_context=(), excluded_files=(),
                payload_limit_characters=100, proposal_version=1,
                report_contract_version=PART_REVIEW_VALIDATOR_VERSION,
                part_header="header", transition_at="now",
            )
        unsupported = CampaignSnapshot(self.snapshot.serialized, contract_version=2)
        transition = advance(unsupported, self.input)
        self.assertEqual(transition.decision, "blocked")
        self.assertEqual(transition.reason, "unsupported_snapshot_version")


class ReviewCoverageTests(unittest.TestCase):
    def test_campaign_creation_and_authorization_block_preserve_existing_state_shape(self):
        segment = _segment("src/a.py", "patch\n")
        parts = [{"part_id": "part-000001", "segments": [segment]}]
        manifest = review_partitioning.campaign_part_manifest(parts)
        campaign = create_campaign_snapshot(
            campaign_id="campaign", created_at="2026-01-01T00:00:00Z",
            payload_limit_characters=4000, source_patch_sha256="source",
            shared_context_sha256="shared", required_calls=1, authorized_call_limit=1,
            parts=manifest, integration_required=False, included_paths=("src/a.py",),
            excluded_files=(("asset.bin", "binary"),),
            report_contract_version=PART_REVIEW_VALIDATOR_VERSION,
        )
        blocked = block_campaign_for_authorization(campaign, ("src/a.py",))

        self.assertEqual(campaign["parts"], manifest)
        self.assertEqual(blocked["status"], "blocked_authorization")
        self.assertEqual(campaign["status"], "planned")
        self.assertEqual(set(blocked), {
            "campaign_id", "header_id", "report_contract_version", "status", "strategy", "created_at",
            "payload_limit_characters", "partition_limit_characters", "source_patch_sha256",
            "shared_context_sha256", "required_calls", "authorized_call_limit", "parts", "part_reviews",
            "integration_review", "files_reviewed", "files_not_reviewed", "excluded_files", "consolidated_report",
        })
        self.assertNotIn("proposal_sha256", blocked)

    def test_coverage_evolution_returns_a_new_campaign_and_keeps_segment_identity(self):
        first = {"path": "src/a.py", "patch_line_start": 1, "patch_line_end": 2, "patch_sha256": "same"}
        second = {"path": "src/b.py", "patch_line_start": 1, "patch_line_end": 2, "patch_sha256": "same"}
        campaign = {
            "parts": [{"part_id": "a", "segments": [first]}, {"part_id": "b", "segments": [second]}],
            "part_reviews": [
                {"part_id": "a", "status": "reviewed", "segments": [first]},
                {"part_id": "b", "status": "pending", "segments": [second]},
            ],
        }
        original = copy.deepcopy(campaign)

        result = sync_campaign_coverage(campaign, (("asset.bin", "binary"),))

        self.assertEqual(campaign, original)
        self.assertEqual(result["files_reviewed"], ["src/a.py"])
        self.assertEqual(result["files_not_reviewed"][0]["path"], "src/b.py")
        self.assertEqual(result["excluded_files"], [{"path": "asset.bin", "reason": "binary"}])


if __name__ == "__main__":
    unittest.main()

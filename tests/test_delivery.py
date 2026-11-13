from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from workflow.delivery import render_delivery_report  # noqa: E402


class DeliveryReportTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "completed_at": "2026-11-13T10:20:30Z",
            "codex_model": None,
            "implementation_summary": "Implemented the feature and ran its checks.",
            "authorization": {"claude_aily": {
                "spent_usd": 0.125,
                "budget_usd": 2.0,
                "budget_unknown": False,
                "max_plan_reviews": 2,
            }},
            "plan_reviews": [{
                "attempt": 1,
                "iteration": 1,
                "valid": True,
                "context_manifest": {"complete": True},
                "report": "Plan approved.",
            }],
            "plan_review_decisions": [{"iteration": 1, "dispositions": "No actionable findings."}],
            "claude_contexts": [{
                "stage": "plan_review_1",
                "iteration": 1,
                "proposal_version": 1,
                "characters": 120,
                "utf8_bytes": 122,
                "payload_limit_characters": 200_000,
                "call_state": "completed",
                "part_id": None,
                "prompt_sha256": "prompt-hash",
                "complete": True,
                "sections": [{"name": "Assignment"}],
            }],
            "review_included_files": ["src/feature.py"],
            "review_exclusions": [{"path": "config/credentials.json", "reason": "sensitive path is excluded"}],
            "review_campaign": {
                "campaign_id": "campaign-1",
                "strategy": "single_call",
                "status": "completed",
                "parts": [{"part_id": "part-1"}],
                "part_reviews": [{
                    "part_id": "part-1", "status": "reviewed", "report": "Part review report.",
                    "calls": [{"call_state": "completed"}],
                }],
                "integration_review": {"status": "not_required", "calls": []},
                "authorized_call_limit": 16,
                "shared_context_sha256": "shared-hash",
                "payload_limit_characters": 200_000,
                "files_reviewed": ["src/feature.py"],
                "files_not_reviewed": [],
            },
            "code_review": "No acceptance-blocking findings.",
            "correction_summary": "No correction run was needed.",
            "proposal": "# Approved proposal\n\nImplement the feature.",
        }

    def render(
        self, state=None, *, valid_plan_review_count=1,
        structured_review_feedback="## Structured feedback\n\nNo blocking findings.",
    ):
        return render_delivery_report(
            self.state if state is None else state,
            workflow_id="workflow-1",
            target=Path("/tmp/target"),
            fallback_completed_at="fallback-time",
            valid_plan_review_count=valid_plan_review_count,
            structured_review_feedback=structured_review_feedback,
            claude_max_payload_env="HARNESS_CLAUDE_MAX_PAYLOAD_CHARS",
            default_claude_payload_chars=200_000,
        )

    def test_normal_completed_report_preserves_sections_and_format(self):
        original = copy.deepcopy(self.state)

        report = self.render()

        self.assertTrue(report.startswith("# Workflow delivery\n\n"))
        self.assertTrue(report.endswith("\n"))
        self.assertEqual(report, self.render())
        self.assertIn("- Workflow: `workflow-1`", report)
        self.assertIn("- Target: `/tmp/target`", report)
        self.assertIn("- Completed: 2026-11-13T10:20:30Z", report)
        self.assertIn("- Codex model: Codex CLI default", report)
        self.assertIn("- Claude spend recorded: $0.1250 / $2.00", report)
        self.assertIn("## Implementation\n\nImplemented the feature and ran its checks.", report)
        self.assertIn("Claude plan-review iterations: 1 valid of 2 maximum.", report)
        self.assertIn("## Structured code-review feedback", report)
        self.assertIn("## Approved proposal\n\n# Approved proposal", report)
        self.assertEqual(self.state, original)

    def test_missing_optional_fields_and_legacy_state_use_existing_fallbacks(self):
        legacy = {
            "authorization": {"claude_aily": {
                "spent_usd": 0.0,
                "budget_usd": 1.0,
                "budget_unknown": False,
                "max_plan_reviews": 2,
            }},
            "plan_reviews": [],
        }

        report = self.render(legacy, valid_plan_review_count=0, structured_review_feedback="")

        self.assertIn("- Completed: fallback-time", report)
        self.assertIn("- Codex model: Codex CLI default", report)
        self.assertIn("No implementation summary was recorded.", report)
        self.assertIn("No Claude context was submitted.", report)
        self.assertIn("No code-review file coverage was recorded.", report)
        self.assertIn("No code review campaign was recorded.", report)
        self.assertIn("No code review report was recorded.", report)
        self.assertIn("No correction summary was recorded.", report)
        self.assertIn("- Change summary: unavailable", report)

    def test_fingerprint_and_execution_change_summary_sections(self):
        self.state["execution_fingerprint"] = {
            "algorithm": "sha256-test-v1",
            "initial": {
                "harness": {"status": "captured", "commit_sha": "harness-commit", "working_tree_sha256": "harness-tree"},
                "target": {
                    "status": "captured", "commit_sha": "target-before", "working_tree_sha256": "target-tree-before",
                    "working_tree_clean": False,
                },
            },
            "final": {
                "target": {
                    "status": "captured", "commit_sha": "target-after", "working_tree_sha256": "target-tree-after",
                    "working_tree_clean": True,
                },
                "change_summary": {
                    "status": "captured",
                    "counts_by_change": {"added": 1, "modified": 2, "deleted": 0, "untracked": 1, "type_changed": 0},
                    "counts_by_attribution": {"preexisting_or_overlapping": 1, "changed_after_initial_capture": 3},
                    "attribution_limit": "Snapshots cannot establish authorship.",
                    "examples_by_attribution": {
                        "preexisting_or_overlapping": [{"path": "README.md", "change": "modified"}],
                        "changed_after_initial_capture": [{"path": "src/new.py", "change": "added"}],
                    },
                    "examples_truncated": {"preexisting_or_overlapping": False, "changed_after_initial_capture": True},
                },
            },
        }

        report = self.render()

        self.assertIn("- Algorithm: `sha256-test-v1`.", report)
        self.assertIn("- Initial harness: commit `harness-commit`; working tree SHA-256 `harness-tree`.", report)
        self.assertIn("- Initial target: commit `target-before`; working tree SHA-256 `target-tree-before`.", report)
        self.assertIn("- Final target: commit `target-after`; working tree SHA-256 `target-tree-after`; clean=yes.", report)
        self.assertIn("Changes relative to the starting baseline: added 1, modified 2, deleted 0, untracked 1, type_changed 0.", report)
        self.assertIn("1 preexisting or overlapping; 3 final-state paths not marked preexisting at initialization.", report)
        self.assertIn("- Preexisting or overlapping path examples: `README.md` (modified).", report)
        self.assertIn("- Final-state paths not marked preexisting at initialization: `src/new.py` (added); more omitted.", report)

    def test_sensitive_path_exclusion_and_pre_redacted_content_render_without_file_contents(self):
        self.state["review_exclusions"] = [{
            "path": "config/credentials.json",
            "reason": "sensitive path is excluded from external review",
        }]
        self.state["code_review"] = "Detected credential [REDACTED]."
        self.state["proposal"] = "Use the sanitized configuration value."
        report = self.render()

        self.assertIn("`config/credentials.json` — sensitive path is excluded from external review", report)
        self.assertIn("Detected credential [REDACTED].", report)
        self.assertNotIn("actual-test-secret-value", report)

    def test_report_fallbacks_for_unavailable_fingerprints_are_stable(self):
        self.state["execution_fingerprint"] = {
            "initial": {"target": {"status": "unavailable"}},
            "final": {"target": {"status": "unavailable"}, "change_summary": {"status": "unavailable"}},
        }

        report = self.render()

        self.assertIn("- Algorithm: `unavailable`.", report)
        self.assertIn("Initial target: commit `unavailable`; working tree SHA-256 `unavailable`.", report)
        self.assertIn("- Change summary: unavailable", report)
        self.assertIn("- Target dirty at initialization: unknown.", report)
        self.assertIn("clean=unknown.", report)


if __name__ == "__main__":
    unittest.main()

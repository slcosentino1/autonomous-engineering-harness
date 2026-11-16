from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from workflow import execution, planning, planning_review  # noqa: E402


class PlanningPromptBuilderTests(unittest.TestCase):
    def test_planning_prompt_is_deterministic_and_orders_context(self):
        inputs = {
            "architectural_decision_output": "ARCHITECTURE MARKER",
            "commitment_ledger_guidance": "LEDGER MARKER",
            "skill_guidance": "SKILL MARKER",
            "planning_principles": "PRINCIPLES MARKER",
            "target": "/tmp/target marker",
            "assignment": "ASSIGNMENT MARKER",
        }
        prompt = planning.build_planning_prompt(**inputs)

        self.assertEqual(prompt, planning.build_planning_prompt(**inputs))
        ordered = [
            "ARCHITECTURE MARKER", "Understand the assignment and this repository.",
            "LEDGER MARKER", "SKILL MARKER", "PRINCIPLES MARKER", "Target: /tmp/target marker",
            "Assignment:\nASSIGNMENT MARKER",
        ]
        positions = [prompt.index(part) for part in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Do not modify files or run tests.", prompt)

    def test_plan_refinement_prompt_keeps_review_and_proposal_context_order(self):
        inputs = {
            "architectural_decision_output": "ARCHITECTURE MARKER",
            "planning_principles": "PRINCIPLES MARKER",
            "skill_guidance": "SKILL MARKER",
            "commitment_ledger_guidance": "LEDGER MARKER",
            "assignment": "ASSIGNMENT MARKER",
            "proposal_version": 9,
            "proposal": "PROPOSAL MARKER",
            "iteration": 2,
            "review_proposal_version": 8,
            "report": "REVIEW REPORT MARKER",
        }
        prompt = planning.build_plan_refinement_prompt(**inputs)

        self.assertEqual(prompt, planning.build_plan_refinement_prompt(**inputs))
        ordered = [
            "Evaluate every distinct finding", "ARCHITECTURE MARKER", "PRINCIPLES MARKER",
            "SKILL MARKER", "LEDGER MARKER", "Assignment:\nASSIGNMENT MARKER",
            "Current proposal (version 9):\nPROPOSAL MARKER",
            "Claude review (iteration 2, proposal version 8):\nREVIEW REPORT MARKER",
        ]
        positions = [prompt.index(part) for part in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Second plan review needed: yes", prompt)

    def test_human_revision_prompt_preserves_feedback_and_review_context_order(self):
        inputs = {
            "architectural_decision_output": "ARCHITECTURE MARKER",
            "planning_principles": "PRINCIPLES MARKER",
            "commitment_ledger_guidance": "LEDGER MARKER",
            "skill_guidance": "SKILL MARKER",
            "assignment": "ASSIGNMENT MARKER",
            "proposal_version": 4,
            "proposal": "PROPOSAL MARKER",
            "review_history": "REVIEW HISTORY MARKER",
            "human_feedback": "USER FEEDBACK MARKER",
        }
        prompt = planning.build_human_revision_prompt(**inputs)

        self.assertEqual(prompt, planning.build_human_revision_prompt(**inputs))
        ordered = [
            "Revise the saved proposal", "ARCHITECTURE MARKER", "PRINCIPLES MARKER",
            "LEDGER MARKER", "SKILL MARKER", "Assignment:\nASSIGNMENT MARKER",
            "Current proposal (version 4):\nPROPOSAL MARKER",
            "Prior review attempts:\nREVIEW HISTORY MARKER",
            "User feedback:\nUSER FEEDBACK MARKER",
        ]
        positions = [prompt.index(part) for part in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("do not implement code", prompt)

    def test_plan_review_input_is_explicit_ordered_and_withholds_dispositions(self):
        inputs = {
            "iteration": 2,
            "max_plan_reviews": 2,
            "proposal_version": 5,
            "assignment": "ASSIGNMENT MARKER",
            "proposal": "Visible proposal\n\n## Claude finding dispositions\nPrivate disposition marker",
            "target": "/tmp/repository marker",
            "repository_context": "REPOSITORY MARKER",
            "planning_principles": "PRINCIPLES MARKER",
            "relevant_skills": "SKILLS MARKER",
            "review_guidance": "DUTIES MARKER",
        }
        result = planning_review.build_plan_review_input(**inputs)
        self.assertEqual(result, planning_review.build_plan_review_input(**inputs))
        header, sections, metadata = result

        self.assertIn("independent Senior Software Architect", header)
        self.assertEqual(list(sections), [
            "Review metadata", "Assignment", "Current proposal", "Repository context",
            "Workflow constraints", "Engineering Judgment & Proportional Design", "Relevant skills",
            "Review duties", "Independence boundary",
        ])
        self.assertEqual(sections["Current proposal"], "Visible proposal")
        self.assertNotIn("Private disposition marker", "\n".join(sections.values()))
        self.assertIn("implementation is not authorized until the user approves it", sections["Workflow constraints"])
        self.assertEqual(metadata, {"iteration": 2, "proposal_version": 5, "header_id": "plan-review-v2"})


class ExecutionPromptBuilderTests(unittest.TestCase):
    def test_implementation_prompt_preserves_policy_and_context_order(self):
        inputs = {
            "validation_baseline": "BASELINE MARKER",
            "commitment_ledger_guidance": "LEDGER MARKER",
            "planning_principles": "PRINCIPLES MARKER",
            "skill_guidance": "SKILL MARKER",
            "assignment": "ASSIGNMENT MARKER",
            "approved_proposal": "APPROVED PROPOSAL MARKER",
        }
        prompt = execution.build_implementation_prompt(**inputs)

        self.assertEqual(prompt, execution.build_implementation_prompt(**inputs))
        ordered = [
            "Implement the approved proposal", "Do not expose secrets or read unrelated repositories.",
            "BASELINE MARKER", "LEDGER MARKER", "PRINCIPLES MARKER", "SKILL MARKER",
            "Assignment:\nASSIGNMENT MARKER", "Approved proposal:\nAPPROVED PROPOSAL MARKER",
        ]
        positions = [prompt.index(part) for part in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("harness-evidence", prompt)
        self.assertIn("make it the last tool action", prompt)

    def test_correction_prompt_preserves_review_evidence_and_context_order(self):
        inputs = {
            "validation_baseline": "BASELINE MARKER",
            "commitment_ledger_guidance": "LEDGER MARKER",
            "planning_principles": "PRINCIPLES MARKER",
            "skill_guidance": "SKILL MARKER",
            "assignment": "ASSIGNMENT MARKER",
            "approved_proposal": "APPROVED PROPOSAL MARKER",
            "code_review": "CODE REVIEW MARKER",
            "individual_reports": "PART REPORTS MARKER",
            "implementation_summary": "IMPLEMENTATION SUMMARY MARKER",
        }
        prompt = execution.build_correction_prompt(**inputs)

        self.assertEqual(prompt, execution.build_correction_prompt(**inputs))
        ordered = [
            "Evaluate the independent Claude code review", "BASELINE MARKER", "LEDGER MARKER",
            "PRINCIPLES MARKER", "SKILL MARKER", "Assignment:\nASSIGNMENT MARKER",
            "Approved proposal:\nAPPROVED PROPOSAL MARKER", "Claude code review:\nCODE REVIEW MARKER",
            "Individual Claude part reports:\nPART REPORTS MARKER",
            "Prior implementation summary:\nIMPLEMENTATION SUMMARY MARKER",
        ]
        positions = [prompt.index(part) for part in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Do not perform unrelated refactoring", prompt)
        self.assertIn("Do not repeat the review", prompt)


if __name__ == "__main__":
    unittest.main()

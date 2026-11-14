from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from workflow import planning_review  # noqa: E402


def report_with_scorecard(scores: dict[str, tuple[int, str]]) -> str:
    lines = ["## Planning quality scorecard"]
    lines.extend(
        f"- {criterion}: {score}/5 — Evidence: {evidence}"
        for criterion, (score, evidence) in scores.items()
    )
    return "Review findings.\n\n" + "\n".join(lines)


class PlanningScorecardTests(unittest.TestCase):
    def test_calculates_mean_and_retains_concrete_evidence(self):
        scores = {
            criterion: (score, f"Proposal section `Decision {index}` compares two options.")
            for index, (criterion, score) in enumerate(zip(
                planning_review.SCORECARD_CRITERIA, (5, 4, 4, 3, 3, 4, 5), strict=True,
            ), start=1)
        }

        normalized, scorecard = planning_review.normalize_scorecard(report_with_scorecard(scores))

        self.assertTrue(scorecard["complete"])
        self.assertEqual(scorecard["mean"], 4.0)
        self.assertEqual(scorecard["scores"], {key: value[0] for key, value in scores.items()})
        self.assertEqual(scorecard["evidence"], {key: value[1] for key, value in scores.items()})
        self.assertIn("Harness-calculated arithmetic mean:** 4.00/5", normalized)
        self.assertIn("not an approval threshold", normalized)
        for evidence in scorecard["evidence"].values():
            self.assertIn(evidence, normalized)

    def test_missing_score_or_evidence_is_reported_without_an_aggregate(self):
        scores = {
            criterion: (4, "proposal section has a concrete choice")
            for criterion in planning_review.SCORECARD_CRITERIA[:-1]
        }
        scores[planning_review.SCORECARD_CRITERIA[0]] = (4, "")

        normalized, scorecard = planning_review.normalize_scorecard(report_with_scorecard(scores))

        self.assertFalse(scorecard["complete"])
        self.assertIsNone(scorecard["mean"])
        self.assertIn(planning_review.SCORECARD_CRITERIA[-1], scorecard["missing"])
        self.assertIn("INCOMPLETE; no mean was calculated", normalized)
        self.assertNotIn("Harness-calculated arithmetic mean", normalized)

    def test_missing_scorecard_is_visible_and_does_not_invent_a_score(self):
        normalized, scorecard = planning_review.normalize_scorecard("The proposal is simple and clear.")

        self.assertFalse(scorecard["complete"])
        self.assertIsNone(scorecard["mean"])
        self.assertIn("INCOMPLETE; no mean was calculated", normalized)
        self.assertEqual(scorecard["missing"], list(planning_review.SCORECARD_CRITERIA))


if __name__ == "__main__":
    unittest.main()

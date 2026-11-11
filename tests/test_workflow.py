from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import claude_reviewer  # noqa: E402
import orchestrator  # noqa: E402
from workflow import review as review_contracts  # noqa: E402
from workflow import workspace as workspace_ops  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def make_target(root: Path) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git(root, "config", "user.name", "Harness Test")
    git(root, "config", "user.email", "harness-test@example.invalid")
    (root / "README.md").write_text("target\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "baseline")
    return root


def prompt_section(prompt: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^## {re.escape(name)}\n(.*?)(?=^## |\Z)", prompt,
    )
    return match.group(1).strip() if match else ""


class FakeCodex:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.commands: list[list[str]] = []
        self.prompts: list[str] = []
        self.on_call = None

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.prompts.append(kwargs["input"].decode("utf-8"))
        if not self.responses:
            raise AssertionError("Unexpected Codex call")
        output_path = Path(command[command.index("--output-last-message") + 1])
        response = self.responses.pop(0)
        if self.on_call is not None:
            self.on_call(len(self.commands) - 1, command, kwargs)
        output_path.write_text(response, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")


class FakeClaude:
    def __init__(self, *results, response_factory=None):
        self.results = list(results)
        self.response_factory = response_factory
        self.prompts: list[str] = []
        self.budgets: list[float] = []
        self.auto_structure = True

    def __call__(self, prompt, *, budget_usd, executable):
        self.prompts.append(prompt)
        self.budgets.append(budget_usd)
        if self.results:
            result = self.results.pop(0)
        elif self.response_factory is not None:
            result = self.response_factory(prompt, budget_usd, executable)
        else:
            raise AssertionError("Unexpected Claude call")
        if isinstance(result, Exception):
            raise result
        if self.auto_structure and "final independent integration review" in prompt:
            decision = "CHANGES_REQUESTED" if "Decision: CHANGES_REQUESTED" in result.text else "APPROVED"
            report = (
                f"Decision: {decision}\n\n## Consolidated findings\n{result.text}\n\n"
                f"## Integration findings\n{result.text if decision == 'CHANGES_REQUESTED' else 'None'}\n\n"
                f"## Overall assessment\n{result.text}"
            )
            return claude_reviewer.ReviewResult(report, result.cost_usd)
        part_match = re.search(r"(?m)^Part ID: (part-\d+)$", prompt)
        if part_match and self.auto_structure:
            part_id = part_match.group(1)
            decision = "CHANGES_REQUESTED" if "Decision: CHANGES_REQUESTED" in result.text else "APPROVED"
            paths = []
            for raw_path in re.findall(r"(?m)^FILE: (.+?); patch lines", prompt):
                try:
                    path = json.loads(raw_path)
                except json.JSONDecodeError:
                    continue
                if path not in paths:
                    paths.append(path)
            findings = result.text if decision == "CHANGES_REQUESTED" else "None"
            severity = "**Severity:** Must fix\n" if decision == "CHANGES_REQUESTED" else ""
            if decision == "APPROVED" and "Optional improvement" in result.text:
                findings = result.text
                severity = "**Severity:** Optional improvement\n"
            if findings != "None":
                findings = (
                    f"### Finding F1\n{severity}**Category:** Correctness\n**File/location:** "
                    f"{paths[0] if paths else 'unknown'}\n**Description:** {result.text}\n"
                    "**Impact and reasoning:** This affects the requested behavior.\n"
                    "**Recommended correction:** Correct the affected behavior."
                )
            report = (
                f"Decision: {decision}\n\nPart ID: {part_id}\n\nFiles reviewed:\n"
                + "".join(f"- `{path}`\n" for path in paths)
                + f"\nFindings:\n{findings}\n\nCross-component concerns:\n- None\n\n"
                f"Overall assessment:\n{result.text}"
            )
            return claude_reviewer.ReviewResult(report, result.cost_usd)
        return result


class PartReviewParserTests(unittest.TestCase):
    def setUp(self):
        self.segment = orchestrator.ReviewSegment("src/first.py", "patch", 1, 1, 1, "hash")

    def report(self, *, part_id="part-000001", decision="APPROVED", findings=None,
               prefix_part="Part ID:", final="Sound implementation.", extra=""):
        if findings is None:
            findings = "None"
        return (
            f"Decision: {decision}\n{prefix_part} {part_id}\n\n## Files reviewed\n"
            "- `src/first.py` — complete patch\n\n## Findings\n"
            f"{findings}\n\n## Cross-component concerns\nNone\n\n"
            f"## Overall assessment\n{final}{extra}"
        )

    @staticmethod
    def finding(identifier, severity="Should fix", missing=None):
        fields = {
            "Severity": severity,
            "Category": "Reliability",
            "File/location": "`src/first.py:12`",
            "Description": "The worker can stop after a transient store error.",
            "Impact and reasoning": "Accepted jobs would remain queued.",
            "Recommended correction": "Catch and log the iteration error, then continue.",
        }
        fields.pop(missing, None)
        return f"### Finding {identifier}\n" + "\n".join(
            f"- **{name}:** {value}" for name, value in fields.items()
        )

    def test_accepts_plain_and_markdown_part_id(self):
        for prefix in ("Part ID:", "## Part ID:"):
            parsed = orchestrator.parse_part_review_report(
                self.report(prefix_part=prefix), "part-000001", [self.segment],
            )
            self.assertTrue(parsed["valid"], parsed["errors"])

    def test_accepts_markdown_decision_and_section_labels(self):
        report = self.report().replace("Decision: APPROVED", "**Decision:** APPROVED")
        report = report.replace("## Overall assessment", "## **Overall assessment:**")
        parsed = orchestrator.parse_part_review_report(report, "part-000001", [self.segment])
        self.assertTrue(parsed["valid"], parsed["errors"])

    def test_missing_or_wrong_part_id_is_rejected(self):
        report = self.report().replace("Part ID: part-000001\n", "")
        parsed = orchestrator.parse_part_review_report(report, "part-000001", [self.segment])
        self.assertTrue(any("Part ID" in error for error in parsed["errors"]))
        parsed = orchestrator.parse_part_review_report(
            self.report(part_id="part-000002"), "part-000001", [self.segment],
        )
        self.assertTrue(any("expected 'part-000001'" in error for error in parsed["errors"]))

    def test_missing_and_invalid_decisions_are_rejected(self):
        missing = self.report().replace("Decision: APPROVED\n", "")
        invalid = self.report(decision="MAYBE")
        self.assertFalse(orchestrator.parse_part_review_report(
            missing, "part-000001", [self.segment],
        )["valid"])
        parsed = orchestrator.parse_part_review_report(invalid, "part-000001", [self.segment])
        self.assertTrue(any("Decision must be" in error for error in parsed["errors"]))

    def test_missing_final_assessment_is_rejected(self):
        report = self.report().replace("## Overall assessment\nSound implementation.", "")
        parsed = orchestrator.parse_part_review_report(report, "part-000001", [self.segment])
        self.assertTrue(any("Overall assessment" in error for error in parsed["errors"]))

    def test_validates_multiple_findings_individually(self):
        findings = "\n\n".join([
            self.finding("F1"), self.finding("F2", "Optional improvement"),
        ])
        parsed = orchestrator.parse_part_review_report(
            self.report(findings=findings), "part-000001", [self.segment],
        )
        self.assertTrue(parsed["valid"], parsed["errors"])
        self.assertEqual(len(parsed["findings"]), 2)

    def test_rejects_a_finding_missing_a_required_field(self):
        report = self.report(findings=self.finding("F1", missing="Impact and reasoning"))
        parsed = orchestrator.parse_part_review_report(report, "part-000001", [self.segment])
        self.assertTrue(any("missing required field 'Impact and reasoning'" in error
                            for error in parsed["errors"]))

    def test_accepts_explicit_empty_findings(self):
        for marker in ("None", "No findings.", "N/A"):
            parsed = orchestrator.parse_part_review_report(
                self.report(findings=marker), "part-000001", [self.segment],
            )
            self.assertTrue(parsed["valid"], parsed["errors"])
            self.assertEqual(parsed["findings"], [])
        parsed = orchestrator.parse_part_review_report(
            self.report(findings=""), "part-000001", [self.segment],
        )
        self.assertTrue(any("explicitly say None" in error for error in parsed["errors"]))

    def test_rejects_ambiguous_duplicate_fields_parts_and_decisions(self):
        finding = self.finding("F1") + "\n- **Category:** Another category"
        report = self.report(findings=finding).replace(
            "Part ID: part-000001", "Part ID: part-000001\n## Part ID: part-000001",
        ).replace("Decision: APPROVED", "Decision: APPROVED\nDecision: APPROVED")
        parsed = orchestrator.parse_part_review_report(report, "part-000001", [self.segment])
        self.assertTrue(any("Decision field" in error for error in parsed["errors"]))
        self.assertTrue(any("Part ID field" in error for error in parsed["errors"]))
        self.assertTrue(any("repeats field 'Category'" in error for error in parsed["errors"]))

    def test_rejects_contradictory_decision_and_finding(self):
        report = self.report(decision="APPROVED", findings=self.finding("F1", "Must fix"))
        parsed = orchestrator.parse_part_review_report(report, "part-000001", [self.segment])
        self.assertTrue(any("contradicts a Must fix" in error for error in parsed["errors"]))
        valid_change_request = self.report(decision="CHANGES_REQUESTED", findings=self.finding("F1", "Must fix"))
        parsed = orchestrator.parse_part_review_report(
            valid_change_request, "part-000001", [self.segment],
        )
        self.assertTrue(parsed["valid"], parsed["errors"])

    def test_review_prompt_requests_stable_report_and_feedback_keeps_detail(self):
        header = orchestrator.Orchestrator._code_review_header()
        self.assertIn("### Finding F1", header)
        self.assertIn("Recommended correction", header)
        runner = object.__new__(orchestrator.Orchestrator)
        runner.state = {"proposal_version": 7}
        sections = runner._part_sections(
            {"Assignment": "Full assignment", "Approved proposal": "Full proposal"},
            [self.segment], campaign_id="campaign", part_id="part-000001", excluded_count=0,
        )
        self.assertEqual(list(sections)[:3], ["Review metadata", "Assignment", "Approved proposal"])
        self.assertEqual(list(sections)[-2:], ["Review file coverage", "Complete patch"])
        self.assertIn("patch", sections["Complete patch"])
        self.assertIn("Proposal version: 7", sections["Review metadata"])
        report = self.report(findings=self.finding("F1"), final="Positive observation. Limitation: no runtime test.")
        parsed = orchestrator.parse_part_review_report(report, "part-000001", [self.segment])
        rendered = orchestrator.render_review_feedback([{
            "part_id": "part-000001", "attempt": 1, "report": report, "validation": parsed,
        }])
        for expected in ("Non-blocking findings", "src/first.py:12", "Accepted jobs would remain queued",
                         "Catch and log", "Positive observation", "no runtime test", "Original reviewer reports"):
            self.assertIn(expected, rendered)


class ReviewContractExtractionTests(unittest.TestCase):
    def test_orchestrator_reexports_review_contracts_for_compatibility(self):
        self.assertIs(orchestrator.ReviewDiff, review_contracts.ReviewDiff)
        self.assertIs(orchestrator.ReviewSegment, review_contracts.ReviewSegment)
        self.assertIs(orchestrator.parse_part_review_report, review_contracts.parse_part_review_report)
        self.assertIs(orchestrator.render_review_feedback, review_contracts.render_review_feedback)

    def test_partition_validation_keeps_workflow_error_and_message(self):
        patch = "first line\nsecond line\n"
        segment_text = "first line\n"
        segment = review_contracts.ReviewSegment(
            "src/module.py", segment_text, 1, 1, 2, review_contracts._review_patch_sha256(segment_text),
        )
        review_diff = review_contracts.ReviewDiff(
            patch, ("src/module.py",), (), (("src/module.py", patch),),
        )
        parts = [{"part_id": "part-000001", "segments": [segment]}]
        expected = "Review partition does not cover the complete changed file patch: src/module.py."
        self.assertEqual(review_contracts._review_partition_error(review_diff, parts), expected)
        with self.assertRaisesRegex(orchestrator.WorkflowError, expected.replace(".", r"\.")):
            orchestrator.Orchestrator._validate_review_partition(review_diff, parts)


class WorkspaceModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workspace-module-tests-")
        self.root = Path(self.temp.name)
        self.target = make_target(self.root / "target")

    def tearDown(self):
        self.temp.cleanup()

    def test_git_and_canonical_target_compatibility_surfaces(self):
        canonical_target = self.target.resolve(strict=True)
        self.assertEqual(workspace_ops.canonical_target(str(self.target)), canonical_target)
        self.assertEqual(orchestrator.canonical_target(str(self.target)), canonical_target)
        baseline = git(self.target, "rev-parse", "HEAD")
        self.assertEqual(workspace_ops.git(self.target, "rev-parse", "HEAD").decode().strip(), baseline)
        self.assertEqual(orchestrator.git(self.target, "rev-parse", "HEAD").decode().strip(), baseline)

    def test_changed_and_protected_paths_match_compatibility_wrappers(self):
        baseline = git(self.target, "rev-parse", "HEAD")
        (self.target / "README.md").write_text("updated\n", encoding="utf-8")
        (self.target / "src").mkdir()
        (self.target / "src" / "new.py").write_text("value = 1\n", encoding="utf-8")
        generated_lock = self.target / ".engineering" / "workflows" / ".workflow.lock"
        generated_lock.parent.mkdir(parents=True)
        generated_lock.write_text("", encoding="utf-8")

        expected = workspace_ops.changed_paths(self.target, baseline)
        self.assertEqual(orchestrator._changed_paths(self.target, baseline), expected)
        self.assertNotIn(".engineering/workflows/.workflow.lock", expected[1])
        self.assertEqual(
            orchestrator._protected_paths(self.target, baseline),
            workspace_ops.protected_paths(self.target, baseline),
        )

    def test_project_guidance_discovery_is_available_through_both_surfaces(self):
        (self.target / "AGENTS.md").write_text("Follow repository guidance.\n", encoding="utf-8")
        skill = self.target / ".agents" / "skills" / "sample" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: sample\ndescription: Sample project guidance.\n---\nUse the sample skill.\n",
            encoding="utf-8",
        )
        expected = workspace_ops.project_guidance(self.target, "Make a local code change")
        self.assertEqual(orchestrator._project_guidance(self.target, "Make a local code change"), expected)
        self.assertEqual(expected["instructions"][0]["path"], "AGENTS.md")
        self.assertEqual(expected["skills"][0]["name"], "sample")

    def test_symlink_component_check_is_available_through_both_surfaces(self):
        source = self.root / "outside.txt"
        source.write_text("outside workspace\n", encoding="utf-8")
        link = self.target / "src" / "external.txt"
        link.parent.mkdir()
        link.symlink_to(source)
        self.assertTrue(workspace_ops.has_symlink_component(self.target, "src/external.txt"))
        self.assertTrue(orchestrator._has_symlink_component(self.target, "src/external.txt"))

    def test_workspace_errors_keep_workflow_error_compatibility(self):
        with self.assertRaisesRegex(workspace_ops.WorkspaceError, "absolute repository path"):
            workspace_ops.canonical_target("relative/path")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "absolute repository path"):
            orchestrator.canonical_target("relative/path")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="simple-harness-")
        self.root = Path(self.temp.name)
        self.target = make_target(self.root / "target")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def refined(
        second_review: str = "no",
        dispositions: str = "- No actionable findings.",
    ) -> str:
        return (
            "# Refined proposal\n\n## Scope\nAdd the requested behavior.\n\n"
            f"## Claude finding dispositions\n{dispositions}\n\nSecond plan review needed: {second_review}\n"
        )

    def implement_files(self, codex: FakeCodex, changed: dict[str, str]) -> None:
        def implementation(index, _command, _kwargs):
            if index != 2:
                return
            for relative, content in changed.items():
                path = self.target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        codex.on_call = implementation

    def create_completed_review_for_recovery(self, workflow_id: str):
        codex = FakeCodex(
            "# Proposal\nReview the saved implementation.\n", self.refined(), "Implemented a small change.\n",
        )
        self.implement_files(codex, {"src/recovery.py": "def answer():\n    return 42\n"})
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=lambda _prompt, _budget, _executable: claude_reviewer.ReviewResult(
                "Decision: APPROVED\nNo acceptance-blocking findings.", 0.05,
            ),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Implement a small change", budget_usd=1.0, workflow_id=workflow_id,
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        completed = runner.approve()
        self.assertEqual(completed["status"], "complete")
        # The original failure happened before a delivery file was written; keep the
        # reconstructed diff identical to the saved code-review campaign.
        runner.delivery_path.unlink()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        part = state["review_campaign"]["part_reviews"][0]
        original_report = part["report"].replace("\nPart ID:", "\n## Part ID:")
        part["report"] = original_report
        for context in state["claude_contexts"]:
            if context.get("stage") == "code_review" and context.get("review_kind") == "part":
                context["report"] = original_report
        part["status"] = "invalid_report"
        part.setdefault("validation_history", []).append({
            "validator_version": "part-review-v1", "valid": False,
            "errors": ["Markdown Part ID heading was rejected by the previous validator."],
        })
        state["review_campaign"]["status"] = "in_progress"
        state["review_campaign"]["files_reviewed"] = []
        state["review_campaign"]["files_not_reviewed"] = [
            {"path": "src/recovery.py", "reason": "review part is invalid_report"},
        ]
        state["review_files_not_reviewed"] = state["review_campaign"]["files_not_reviewed"]
        state["status"] = "failed"
        state["stage"] = "code_review"
        state["error"] = "Old part report validation failed."
        state["code_review"] = None
        runner.state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return runner, claude, state

    def test_missing_budget_uses_two_dollars_and_persists_it(self):
        codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", workflow_id="default-budget",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        authorization = state["authorization"]["claude_aily"]
        self.assertEqual(authorization["budget_usd"], 2.0)
        persisted = json.loads((runner.workflow_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["authorization"]["claude_aily"]["budget_usd"], 2.0)
        self.assertEqual(claude.budgets, [2.0])
        self.assertIsNone(state["codex_model"])
        self.assertTrue(all("--model" not in command for command in codex.commands))

    def test_cli_reports_default_budget_without_requesting_it(self):
        fake_state = {
            "workflow_id": "cli-default-budget", "target": str(self.target),
            "status": "awaiting_approval", "stage": "implementation", "error": None,
            "authorization": {
                "authorization_id": "auth-test",
                "claude_aily": {"budget_usd": 2.0, "spent_usd": 0.0},
            },
        }
        output = StringIO()
        with patch.object(orchestrator, "Orchestrator") as factory:
            factory.return_value.budget_usd = 2.0
            factory.return_value.max_code_review_calls = orchestrator.DEFAULT_MAX_CODE_REVIEW_CALLS
            factory.return_value.start.return_value = fake_state
            with redirect_stdout(output):
                result = orchestrator.main([
                    "start", "--target", str(self.target), "--task", "Add a feature",
                ])
        self.assertEqual(result, 0)
        self.assertEqual(factory.call_args.kwargs["budget_usd"], 2.0)
        self.assertEqual(
            factory.call_args.kwargs["max_code_review_calls"], orchestrator.DEFAULT_MAX_CODE_REVIEW_CALLS,
        )
        self.assertIn("Effective Claude/Aily budget: $2.00", output.getvalue())
        self.assertIn('"effective_budget_usd": 2.0', output.getvalue())
        self.assertIsNone(factory.call_args.kwargs["model"])

    def test_cli_budget_override_and_optional_model(self):
        fake_state = {
            "workflow_id": "cli-override", "target": str(self.target),
            "status": "awaiting_approval", "stage": "implementation", "error": None,
            "authorization": {
                "authorization_id": "auth-test",
                "claude_aily": {"budget_usd": 0.75, "spent_usd": 0.0},
            },
        }
        output = StringIO()
        with patch.object(orchestrator, "Orchestrator") as factory:
            factory.return_value.budget_usd = 0.75
            factory.return_value.max_code_review_calls = 12
            factory.return_value.start.return_value = fake_state
            with redirect_stdout(output):
                result = orchestrator.main([
                    "start", "--target", str(self.target), "--task", "Add a feature",
                    "--budget-usd", "0.75", "--model", "codex-model-test",
                    "--max-code-review-calls", "12",
                ])
        self.assertEqual(result, 0)
        self.assertEqual(factory.call_args.kwargs["budget_usd"], 0.75)
        self.assertEqual(factory.call_args.kwargs["model"], "codex-model-test")
        self.assertEqual(factory.call_args.kwargs["max_code_review_calls"], 12)
        self.assertIn("Effective Claude/Aily budget: $0.75", output.getvalue())

    def test_plan_is_saved_and_implementation_waits_for_explicit_approval(self):
        codex = FakeCodex(
            "# Proposal\n\n## Approach\nImplement the request and run the unit tests.\n",
            self.refined(),
            "Implemented the change.\nTests: `python -m unittest`; passed.\n",
            "No valid findings required changes. Tests remain passing.\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nThe proposal covers the request.", 0.12),
            claude_reviewer.ReviewResult("Decision: APPROVED\nNo code findings.", 0.18),
        )
        (self.target / "src").mkdir()
        codex.on_call = lambda index, _command, _kwargs: (
            (self.target / "src" / "feature.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
            if index == 2 else None
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="approval-flow",
            model="codex-model-test", process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertTrue((runner.workflow_dir / "proposal.md").is_file())
        self.assertEqual(len(codex.commands), 2)
        self.assertFalse(any("workspace-write" in command for command in codex.commands))
        self.assertIsNone(state["implementation_approved_at"])
        authorization_id = state["authorization"]["authorization_id"]
        authorization = state["authorization"]
        self.assertEqual(authorization["target"], str(self.target.resolve()))
        self.assertIn("run_development_commands_builds_tests_linters_and_static_analysis",
                      authorization["workspace_operations"])
        self.assertEqual(authorization["claude_aily"]["permitted_data"], orchestrator.CLAUDE_DATA_SCOPE)
        self.assertEqual(authorization["claude_aily"]["budget_usd"], 1.0)

        approved = orchestrator.Orchestrator(
            self.target, workflow_id="approval-flow", process_runner=codex, claude_runner=claude,
        ).approve()
        self.assertEqual(approved["status"], "complete")
        self.assertEqual(approved["review_campaign"]["strategy"], "single_call")
        self.assertEqual(len(approved["review_campaign"]["parts"]), 1)
        self.assertEqual(approved["review_campaign"]["integration_review"]["status"], "not_required")
        self.assertEqual(approved["review_campaign_call_count"], 1)
        self.assertEqual(len(codex.commands), 3)
        self.assertEqual([command[command.index("--sandbox") + 1] for command in codex.commands],
                         ["read-only", "read-only", "workspace-write"])
        self.assertTrue(all(prompt.startswith("Persistent workflow authorization is already active")
                            for prompt in codex.prompts))
        self.assertEqual(approved["authorization"]["authorization_id"], authorization_id)
        self.assertEqual(approved["codex_model"], "codex-model-test")
        self.assertTrue(all(
            command[command.index("--model") + 1] == "codex-model-test" for command in codex.commands
        ))
        self.assertEqual(approved["authorization"]["claude_aily"]["spent_usd"], 0.3)
        self.assertEqual(claude.budgets, [1.0, 0.88])
        self.assertIn("return 42", claude.prompts[1])
        self.assertIn("commit, push", codex.prompts[2])
        self.assertIn("commit", approved["authorization"]["restricted_operations"])
        self.assertTrue((runner.workflow_dir / "delivery.md").is_file())
        self.assertIn("python -m unittest", (runner.workflow_dir / "delivery.md").read_text())

    def test_plan_review_is_limited_to_two_iterations(self):
        codex = FakeCodex(
            "# Proposal\nInitial plan.\n",
            self.refined("yes", "- Finding: clarify the data edge case | Decision: incorporated | "
                                  "Reason: cover the data boundary."),
            self.refined("yes"),
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Please clarify the data edge case.", 0.1),
            claude_reviewer.ReviewResult("The revised plan is adequate.", 0.2),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="two-reviews",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertEqual(state["stage"], "implementation")
        self.assertEqual(len(state["plan_reviews"]), 2)
        self.assertEqual(len(claude.prompts), 2)
        self.assertEqual(len(codex.commands), 3)
        self.assertIn("Please clarify the data edge case.", claude.prompts[1])
        self.assertIn("Decision: incorporated", claude.prompts[1])
        self.assertIn("Plan review iteration 2", claude.prompts[1])
        self.assertIn("proposal version 2", claude.prompts[1])
        self.assertEqual(state["claude_contexts"][1]["proposal_version"], 2)

    def test_user_revision_uses_remaining_plan_review_and_still_waits_for_approval(self):
        codex = FakeCodex(
            "# Proposal\nInitial plan.\n",
            self.refined(dispositions="- Finding: clarify empty input | Decision: incorporated | "
                                      "Reason: define zero-item behavior."),
            "# Revised proposal\nAdd explicit empty-input behavior.\n",
            "# Final proposal\nHandle the empty-input case.\n\n"
            "## Claude finding dispositions\n- No actionable findings.\n\nSecond plan review needed: no\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Clarify empty input.", 0.1),
            claude_reviewer.ReviewResult("The change addresses empty input.", 0.1),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="revision-flow",
            process_runner=codex, claude_runner=claude,
        )
        self.assertEqual(runner.start()["status"], "awaiting_approval")
        (runner.workflow_dir / "proposal.md").write_text(
            "# User-edited proposal\nCall out empty input.\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "proposal file changed"):
            orchestrator.Orchestrator(
                self.target, workflow_id="revision-flow", process_runner=codex, claude_runner=claude,
            ).approve()
        revised = runner.revise("Document the empty-input behavior.")
        self.assertEqual(revised["status"], "awaiting_approval")
        self.assertEqual(len(revised["plan_reviews"]), 2)
        self.assertEqual(len(claude.prompts), 2)
        self.assertFalse(any("workspace-write" in command for command in codex.commands))
        self.assertIn("User-edited proposal", codex.prompts[2])
        self.assertIn("uncertain outcomes", codex.prompts[2])
        self.assertIn("external dependencies", codex.prompts[2])
        self.assertIn("empty-input", (runner.workflow_dir / "proposal.md").read_text(encoding="utf-8"))

    def test_user_can_reject_without_implementation(self):
        codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Looks good.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="reject-flow",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        rejected = runner.reject("Out of scope.")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["rejection_reason"], "Out of scope.")
        self.assertFalse(any("workspace-write" in command for command in codex.commands))

    def test_code_findings_trigger_one_correction_without_a_review_loop(self):
        codex = FakeCodex(
            "# Proposal\nPlan.\n", self.refined(),
            "Implemented the feature.\nChecks passed.\n",
            "Handled the empty-input finding and reran the unit tests; they passed.\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Plan looks good.", 0.1),
            claude_reviewer.ReviewResult(
                "Decision: CHANGES_REQUESTED\nFinding: Handle empty input in the new parser.", 0.2,
            ),
        )
        (self.target / "src").mkdir()
        codex.on_call = lambda index, _command, _kwargs: (
            (self.target / "src" / "parser.py").write_text("def parse(value): return value\n", encoding="utf-8")
            if index == 2 else None
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="finding-flow",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        state = orchestrator.Orchestrator(
            self.target, workflow_id="finding-flow", process_runner=codex, claude_runner=claude,
        ).approve()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(len(claude.prompts), 2)
        self.assertEqual(len(codex.commands), 4)
        self.assertTrue(all(prompt.startswith("Persistent workflow authorization is already active")
                            for prompt in codex.prompts))
        self.assertIn("Handle empty input", codex.prompts[-1])
        self.assertIn("reran the unit tests", state["correction_summary"])

    def test_code_review_context_records_exclusions_and_preserves_all_relevant_files(self):
        metadata = self.target / ".DS_Store"
        metadata.write_bytes(b"baseline metadata")
        git(self.target, "add", "-f", ".DS_Store")
        git(self.target, "commit", "-q", "-m", "metadata baseline")
        codex = FakeCodex(
            "# Proposal\nPlan.\n", self.refined(), "Implementation complete.\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Plan looks good.", 0.1),
            claude_reviewer.ReviewResult("Decision: APPROVED\nNo actionable findings.", 0.1),
        )

        def implement(index, _command, _kwargs):
            if index != 2:
                return
            metadata.write_bytes(b"metadata changed\x00with binary data")
            for relative, content in (
                ("app/service.py", "def process(item):\n    return item\n"),
                ("tests/test_service.py", "def test_service():\n    assert True\n"),
                ("docs/api.md", "# API contract\n"),
                ("contracts/service.v1", "request: item\nresponse: status\n"),
            ):
                path = self.target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

        codex.on_call = implement
        runner = orchestrator.Orchestrator(
            self.target, task="Build an API", budget_usd=1.0, workflow_id="file-coverage-review",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        state = orchestrator.Orchestrator(
            self.target, workflow_id="file-coverage-review", process_runner=codex, claude_runner=claude,
        ).approve()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["review_included_files"], [
            "app/service.py", "contracts/service.v1", "docs/api.md", "tests/test_service.py",
        ])
        self.assertEqual(state["review_exclusions"], [
            {"path": ".DS_Store", "reason": "operating-system metadata"},
            {
                "path": ".engineering/workflows/file-coverage-review/proposal.md",
                "reason": "workflow state is not part of the implementation diff",
            },
            {
                "path": ".engineering/workflows/file-coverage-review/state.json",
                "reason": "workflow state is not part of the implementation diff",
            },
        ])
        code_context = state["claude_contexts"][-1]
        self.assertEqual(code_context["call_state"], "completed")
        self.assertEqual(code_context["included_files"], state["review_included_files"])
        self.assertEqual(code_context["excluded_files"], state["review_exclusions"])
        prompt = claude.prompts[-1]
        self.assertIn("app/service.py", prompt)
        self.assertIn("tests/test_service.py", prompt)
        self.assertIn("contracts/service.v1", prompt)
        self.assertNotIn(".DS_Store", prompt)
        self.assertEqual(
            state["review_campaign"]["excluded_files"][0]["reason"], "operating-system metadata",
        )
        self.assertNotIn("\x00", prompt)

    def test_large_review_uses_adaptive_parts_shared_context_and_integration(self):
        changed = {
            "services/ledger/engine.py": "def calculate():\n" + "    value = 1  # ledger operation\n" * 45,
            "services/ledger/model.py": "class LedgerModel:\n" + "    value = 'entry'\n" * 45,
            "checks/ledger/test_engine.py": "def test_calculate():\n" + "    assert 1 == 1\n" * 45,
            "contracts/ledger.contract": "ledger request contract\n" * 45,
            "plugins/codec/custom.data": "codec mapping row\n" * 45,
        }
        codex = FakeCodex("# Proposal\nA small service with explicit contracts.\n", self.refined(), "Implemented all changed modules.\n")

        def implementation(index, _command, _kwargs):
            if index == 2:
                for relative, content in changed.items():
                    path = self.target / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")

        codex.on_call = implementation
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan review passed.", 0.05),
            response_factory=lambda _prompt, _budget, _executable: claude_reviewer.ReviewResult(
                "Decision: APPROVED\nNo acceptance-blocking findings.", 0.05,
            ),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a ledger processing API", budget_usd=1.0,
            workflow_id="multipart-review", process_runner=codex, claude_runner=claude,
        )
        runner.start()
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "15000"}):
            state = orchestrator.Orchestrator(
                self.target, workflow_id="multipart-review", process_runner=codex,
                claude_runner=claude,
            ).approve()

        campaign = state["review_campaign"]
        self.assertEqual(state["status"], "complete")
        self.assertEqual(campaign["status"], "complete")
        self.assertEqual(campaign["strategy"], "multi_part_with_integration")
        self.assertGreaterEqual(len(campaign["parts"]), 2)
        self.assertEqual(campaign["integration_review"]["status"], "reviewed")
        self.assertEqual(set(campaign["files_reviewed"]), set(changed))
        self.assertEqual(campaign["files_not_reviewed"], [])

        flattened = [segment for part in campaign["parts"] for segment in part["segments"]]
        assignments = {}
        for segment in flattened:
            assignments.setdefault(segment["path"], 0)
            assignments[segment["path"]] += 1
        self.assertEqual(assignments, {path: 1 for path in changed})
        self.assertTrue(any(
            {"services/ledger/engine.py", "services/ledger/model.py"}
            <= {segment["path"] for segment in part["segments"]}
            for part in campaign["parts"]
        ))
        self.assertTrue(all(
            part["status"] == "reviewed" and f"Part ID: {part['part_id']}" in part["report"]
            for part in campaign["part_reviews"]
        ))
        code_calls = [item for item in state["claude_contexts"] if item["stage"] == "code_review"]
        self.assertEqual(len(code_calls), len(campaign["parts"]) + 1)
        self.assertEqual(state["review_campaign_call_count"], len(code_calls))
        self.assertEqual(
            state["authorization"]["claude_aily"]["spent_usd"],
            round(0.05 * (len(code_calls) + 1), 4),
        )
        self.assertLessEqual(
            state["review_campaign_call_count"],
            state["authorization"]["claude_aily"]["max_code_reviews"],
        )
        self.assertTrue(all(item["characters"] <= 15000 for item in code_calls))
        self.assertTrue(all(item["payload_limit_characters"] == 15000 for item in code_calls))
        self.assertEqual(
            {item["shared_context_sha256"] for item in code_calls},
            {campaign["shared_context_sha256"]},
        )
        for context, prompt in zip(code_calls, claude.prompts[1:], strict=True):
            self.assertEqual(context["characters"], len(prompt))
            self.assertEqual(context["utf8_bytes"], len(prompt.encode("utf-8")))
            self.assertEqual(context["prompt_sha256"], hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        part_prompts = [
            prompt for prompt in claude.prompts
            if "Part ID: part-" in prompt and "final independent integration review" not in prompt
        ]
        integration_prompt = next(prompt for prompt in claude.prompts if "final independent integration review" in prompt)
        self.assertEqual(len(part_prompts), len(campaign["parts"]))
        for section in (
            "Assignment", "Approved proposal", "Implementation summary", "Independent reviewer role",
            "Engineering Judgment & Proportional Design", "Repository/module map and dependencies",
        ):
            values = {prompt_section(prompt, section) for prompt in part_prompts}
            self.assertEqual(len(values), 1, section)
        self.assertIn("Individual part reports", integration_prompt)
        self.assertIn("Part ID: part-000001", integration_prompt)
        self.assertEqual(integration_prompt.count("Part ID: part-"), len(campaign["parts"]))
        self.assertIn("Deduplicate findings", integration_prompt)
        self.assertIn("Senior Software Architect", part_prompts[0])
        self.assertIn("Product Engineer", integration_prompt)
        delivery = (runner.workflow_dir / "delivery.md").read_text(encoding="utf-8")
        self.assertIn("multi_part_with_integration", delivery)
        self.assertIn("Integration report", delivery)
        self.assertIn("Claude invocation attempts", delivery)

    def test_oversized_file_is_split_only_at_safe_patch_boundaries(self):
        file_path = "modules/unusual/component.py"
        content = "".join(
            f"def operation_{index}():\n" + "    result = 'component operation value'\n" * 35 + "\n"
            for index in range(8)
        )
        codex = FakeCodex("# Proposal\nKeep the component modular.\n", self.refined(), "Implemented component.\n")
        def write_component(index, _command, _kwargs):
            if index == 2:
                (self.target / file_path).parent.mkdir(parents=True, exist_ok=True)
                (self.target / file_path).write_text(content, encoding="utf-8")

        codex.on_call = write_component
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan review passed.", 0.05),
            response_factory=lambda _prompt, _budget, _executable: claude_reviewer.ReviewResult(
                "Decision: APPROVED\nNo acceptance-blocking findings.", 0.05,
            ),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Review the component", budget_usd=1.0, workflow_id="safe-file-split",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "13500"}):
            state = orchestrator.Orchestrator(
                self.target, workflow_id="safe-file-split", process_runner=codex, claude_runner=claude,
            ).approve()
        campaign = state["review_campaign"]
        segment_ranges = sorted(
            (item["patch_line_start"], item["patch_line_end"], item["patch_line_count"])
            for part in campaign["parts"] for item in part["segments"]
        )
        self.assertGreater(len(segment_ranges), 1)
        self.assertEqual({item["path"] for part in campaign["parts"] for item in part["segments"]}, {file_path})
        self.assertEqual(segment_ranges[0][0], 1)
        self.assertEqual(segment_ranges[-1][1], segment_ranges[0][2])
        self.assertTrue(all(left[1] + 1 == right[0] for left, right in zip(segment_ranges, segment_ranges[1:])))
        source_patch = dict(orchestrator._review_diff(state, self.target).file_patches)[file_path]
        source_lines = source_patch.splitlines(keepends=True)
        reconstructed = "".join("".join(source_lines[start - 1:end]) for start, end, _total in segment_ranges)
        self.assertEqual(reconstructed, source_patch)
        self.assertEqual(state["review_campaign"]["status"], "complete")

    def test_resume_reuses_successful_parts_and_retries_only_failed_part(self):
        changed = {
            f"components/ledger/file_{index}.py": (
                f"def operation_{index}():\n" + "    result = 'component operation'\n" * 45
            )
            for index in range(5)
        }
        codex = FakeCodex("# Proposal\nKeep boundaries proportional.\n", self.refined(), "Implemented modules.\n")
        self.implement_files(codex, changed)
        attempts: dict[str, int] = {}
        failed_id: list[str | None] = [None]

        def respond(prompt, _budget, _executable):
            if "final independent integration review" in prompt:
                return claude_reviewer.ReviewResult("Decision: APPROVED\nNo integration defects.", 0.05)
            part_id = re.search(r"(?m)^Part ID: (part-\d+)$", prompt).group(1)
            attempts[part_id] = attempts.get(part_id, 0) + 1
            if failed_id[0] is None and part_id != "part-000001":
                failed_id[0] = part_id
                raise claude_reviewer.ReviewError("temporary Aily failure", cost_usd=0.03)
            return claude_reviewer.ReviewResult("Decision: APPROVED\nNo acceptance-blocking findings.", 0.05)

        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=respond,
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Process ledger records", budget_usd=1.0, workflow_id="part-resume",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "14000"}):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "temporary Aily failure"):
                orchestrator.Orchestrator(
                    self.target, workflow_id="part-resume", process_runner=codex, claude_runner=claude,
                ).approve()
            failed = json.loads(runner.state_path.read_text(encoding="utf-8"))
            campaign = failed["review_campaign"]
            self.assertEqual(campaign["part_reviews"][0]["status"], "reviewed")
            self.assertTrue(any(part["status"] == "call_failed" for part in campaign["part_reviews"]))
            before_resume_part_prompts = [
                prompt for prompt in claude.prompts if "Part ID: part-" in prompt
                and "final independent integration review" not in prompt
            ]
            self.assertGreaterEqual(len(campaign["parts"]), 2)
            self.assertEqual(len(before_resume_part_prompts), 2)

            completed = orchestrator.Orchestrator(
                self.target, workflow_id="part-resume", process_runner=codex, claude_runner=claude,
            ).resume()

        self.assertEqual(completed["status"], "complete")
        self.assertEqual(len(codex.commands), 3)
        final_part_prompts = [
            prompt for prompt in claude.prompts if "Part ID: part-" in prompt
            and "final independent integration review" not in prompt
        ]
        self.assertEqual(sum("Part ID: part-000001" in prompt for prompt in final_part_prompts), 1)
        failed_part = failed_id[0]
        self.assertIsNotNone(failed_part)
        self.assertEqual(sum(f"Part ID: {failed_part}" in prompt for prompt in final_part_prompts), 2)
        self.assertEqual(
            [part["status"] for part in completed["review_campaign"]["part_reviews"]],
            ["reviewed"] * len(completed["review_campaign"]["parts"]),
        )

    def test_local_recovery_dry_run_revalidates_reports_without_writing_or_calling_provider(self):
        runner, claude, saved = self.create_completed_review_for_recovery("local-review-dry-run")
        before = runner.state_path.read_bytes()
        prompts_before = len(claude.prompts)
        result = orchestrator.recover_code_review(
            self.target, "local-review-dry-run", apply=False,
        )
        self.assertEqual(result["provider_invocations"], 0)
        self.assertEqual(result["persisted_reports"], 1)
        self.assertEqual(result["valid_reports"], 1)
        self.assertEqual(result["invalid_reports"], 0)
        self.assertTrue(result["campaign_would_be_complete"])
        self.assertEqual(result["claude_spent_usd"], saved["authorization"]["claude_aily"]["spent_usd"])
        self.assertEqual(runner.state_path.read_bytes(), before)
        self.assertEqual(len(claude.prompts), prompts_before)

    def test_recover_review_cli_defaults_to_dry_run(self):
        output = StringIO()
        with patch.object(orchestrator, "recover_code_review", return_value={
            "mode": "dry-run", "provider_invocations": 0, "errors": [],
        }) as recovery, redirect_stdout(output):
            result = orchestrator.main([
                "recover-review", "--target", str(self.target), "--workflow-id", "cli-recovery",
            ])
        self.assertEqual(result, 0)
        self.assertFalse(recovery.call_args.kwargs["apply"])
        self.assertIn('"provider_invocations": 0', output.getvalue())

    def test_local_recovery_apply_is_auditable_idempotent_and_preserves_call_costs(self):
        runner, claude, saved = self.create_completed_review_for_recovery("local-review-apply")
        prompts_before = len(claude.prompts)
        original_contexts = copy.deepcopy(saved["claude_contexts"])
        original_calls = copy.deepcopy(saved["review_campaign"]["part_reviews"][0]["calls"])
        original_report = saved["review_campaign"]["part_reviews"][0]["report"]
        original_spend = saved["authorization"]["claude_aily"]["spent_usd"]
        result = orchestrator.recover_code_review(self.target, "local-review-apply", apply=True)
        self.assertTrue(result["applied"])
        self.assertTrue(result["campaign_completed"])
        recovered = json.loads(runner.state_path.read_text(encoding="utf-8"))
        part = recovered["review_campaign"]["part_reviews"][0]
        self.assertEqual(part["status"], "reviewed")
        self.assertEqual(part["report"], original_report)
        self.assertEqual(part["calls"], original_calls)
        self.assertEqual(recovered["claude_contexts"], original_contexts)
        self.assertEqual(recovered["authorization"]["claude_aily"]["spent_usd"], original_spend)
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["stage"], "code_review")
        self.assertEqual(recovered["review_campaign"]["recovery_history"][0]["old_validator_statuses"], {
            "part-000001": "invalid_report",
        })
        self.assertEqual(len(part["validated_reports"]), 1)
        before_second_apply = runner.state_path.read_bytes()
        second = orchestrator.recover_code_review(self.target, "local-review-apply", apply=True)
        self.assertTrue(second["already_applied"])
        self.assertFalse(second["applied"])
        self.assertEqual(runner.state_path.read_bytes(), before_second_apply)
        self.assertEqual(len(claude.prompts), prompts_before)

    def test_local_recovery_rejects_invalid_saved_report_without_mutation(self):
        runner, claude, saved = self.create_completed_review_for_recovery("local-review-invalid")
        part = saved["review_campaign"]["part_reviews"][0]
        bad_report = part["report"].replace("Findings:\nNone", "Findings:\nF1 has a problem")
        part["report"] = bad_report
        for context in saved["claude_contexts"]:
            if context.get("stage") == "code_review" and context.get("review_kind") == "part":
                context["report"] = bad_report
        runner.state_path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        before = runner.state_path.read_bytes()
        prompts_before = len(claude.prompts)
        result = orchestrator.recover_code_review(self.target, "local-review-invalid", apply=True)
        self.assertFalse(result["applied"])
        self.assertGreater(result["invalid_reports"], 0)
        self.assertTrue(any("individually headed finding blocks" in error for error in result["errors"]))
        self.assertEqual(runner.state_path.read_bytes(), before)
        self.assertEqual(len(claude.prompts), prompts_before)

    def test_local_recovery_rejects_changed_target_diff_without_mutation(self):
        runner, claude, _saved = self.create_completed_review_for_recovery("local-review-changed-diff")
        source = self.target / "src" / "recovery.py"
        source.write_text("def answer():\n    return 43\n", encoding="utf-8")
        before = runner.state_path.read_bytes()
        prompts_before = len(claude.prompts)
        result = orchestrator.recover_code_review(
            self.target, "local-review-changed-diff", apply=True,
        )
        self.assertFalse(result["applied"])
        self.assertTrue(any("target diff" in error or "patch segment hash" in error
                            for error in result["errors"]))
        self.assertEqual(runner.state_path.read_bytes(), before)
        self.assertEqual(len(claude.prompts), prompts_before)

    def test_local_recovery_does_not_complete_campaign_with_pending_integration_review(self):
        runner, _claude, saved = self.create_completed_review_for_recovery("local-review-incomplete")
        saved["review_campaign"]["integration_review"] = {
            "required": True, "status": "pending", "report": None, "calls": [],
        }
        runner.state_path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        result = orchestrator.recover_code_review(self.target, "local-review-incomplete", apply=True)
        recovered = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertTrue(result["applied"])
        self.assertFalse(result["campaign_would_be_complete"])
        self.assertEqual(recovered["review_campaign"]["status"], "in_progress")
        self.assertEqual(recovered["review_campaign"]["part_reviews"][0]["status"], "reviewed")
        self.assertIsNone(recovered["code_review"])
        self.assertEqual(recovered["stage"], "code_review")

    def test_saved_part_report_is_recovered_without_repeating_paid_call(self):
        changed = {
            f"modules/recovery/file_{index}.py": f"def operation_{index}():\n" + "    return 'recoverable'\n" * 45
            for index in range(5)
        }
        codex = FakeCodex("# Proposal\nKeep the report state durable.\n", self.refined(), "Implemented.\n")
        self.implement_files(codex, changed)
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=lambda _prompt, _budget, _executable: claude_reviewer.ReviewResult(
                "Decision: APPROVED\nNo acceptance-blocking findings.", 0.05,
            ),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Implement recovery", budget_usd=1.0, workflow_id="persisted-report-recovery",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()

        class InterruptedAfterSavedCall(orchestrator.Orchestrator):
            interrupted = False

            def _call_claude(self, header, stage, sections, *, metadata=None):
                result = super()._call_claude(header, stage, sections, metadata=metadata)
                if stage == "code_review" and (metadata or {}).get("review_kind") == "part" and not self.interrupted:
                    self.interrupted = True
                    raise orchestrator.WorkflowError("simulated interruption after report persistence")
                return result

        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "14000"}):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "simulated interruption"):
                InterruptedAfterSavedCall(
                    self.target, workflow_id="persisted-report-recovery", process_runner=codex,
                    claude_runner=claude,
                ).approve()
            state_before = json.loads(runner.state_path.read_text(encoding="utf-8"))
            part_record = state_before["review_campaign"]["part_reviews"][0]
            self.assertEqual(part_record["status"], "report_saved")
            self.assertTrue(any(
                context.get("part_id") == part_record["part_id"] and context.get("report")
                for context in state_before["claude_contexts"]
            ))
            prompts_before_resume = len(claude.prompts)
            completed = orchestrator.Orchestrator(
                self.target, workflow_id="persisted-report-recovery", process_runner=codex,
                claude_runner=claude,
            ).resume()
        self.assertEqual(completed["status"], "complete")
        first_id = part_record["part_id"]
        self.assertEqual(sum(
            f"Part ID: {first_id}" in prompt and "final independent integration review" not in prompt
            for prompt in claude.prompts
        ), 1)
        self.assertGreater(len(claude.prompts), prompts_before_resume)

    def test_saved_integration_report_is_recovered_without_repeating_paid_call(self):
        changed = {
            f"application/sync/file_{index}.py": f"def operation_{index}():\n" + "    return 'sync'\n" * 55
            for index in range(5)
        }
        codex = FakeCodex("# Proposal\nIntegrate the sync modules.\n", self.refined(), "Implemented.\n")
        self.implement_files(codex, changed)
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=lambda _prompt, _budget, _executable: claude_reviewer.ReviewResult(
                "Decision: APPROVED\nNo integration defects.", 0.05,
            ),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Implement sync processing", budget_usd=1.0, workflow_id="integration-recovery",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()

        class InterruptedAfterIntegration(orchestrator.Orchestrator):
            def _call_claude(self, header, stage, sections, *, metadata=None):
                result = super()._call_claude(header, stage, sections, metadata=metadata)
                if (stage == "code_review" and (metadata or {}).get("review_kind") == "integration"):
                    raise orchestrator.WorkflowError("simulated integration persistence boundary")
                return result

        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "14000"}):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "integration persistence boundary"):
                InterruptedAfterIntegration(
                    self.target, workflow_id="integration-recovery", process_runner=codex,
                    claude_runner=claude,
                ).approve()
            saved = json.loads(runner.state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["review_campaign"]["integration_review"]["status"], "report_saved")
            calls_before_resume = len(claude.prompts)
            completed = orchestrator.Orchestrator(
                self.target, workflow_id="integration-recovery", process_runner=codex,
                claude_runner=claude,
            ).resume()
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(len(claude.prompts), calls_before_resume)
        self.assertEqual(completed["review_campaign"]["integration_review"]["status"], "reviewed")

    def test_unknown_part_spend_blocks_all_later_campaign_calls(self):
        changed = {
            f"modules/records/part_{index}.py": (
                f"def operation_{index}():\n" + "    return 'record operation'\n" * 50
            ) for index in range(5)
        }
        codex = FakeCodex("# Proposal\nUse the existing contract.\n", self.refined(), "Implemented records.\n")
        self.implement_files(codex, changed)

        def respond(prompt, _budget, _executable):
            return claude_reviewer.ReviewResult("Decision: APPROVED\nNo acceptance-blocking findings.", None)

        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=respond,
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Implement records", budget_usd=1.0, workflow_id="unknown-part-spend",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "14000"}):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "previous call has no recorded cost"):
                orchestrator.Orchestrator(
                    self.target, workflow_id="unknown-part-spend", process_runner=codex,
                    claude_runner=claude,
                ).approve()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["authorization"]["claude_aily"]["budget_unknown"])
        self.assertEqual(len(claude.prompts), 2)  # plan plus one part; no later part or integration call
        self.assertEqual(state["review_campaign"]["status"], "in_progress")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "no recorded cost"):
            orchestrator.Orchestrator(
                self.target, workflow_id="unknown-part-spend", process_runner=codex,
                claude_runner=claude,
            ).resume()

    def test_existing_single_call_authorization_does_not_expand_for_multipart_review(self):
        changed = {
            f"areas/component/file_{index}.py": f"def item_{index}():\n" + "    return 3\n" * 80
            for index in range(5)
        }
        codex = FakeCodex("# Proposal\nPlan.\n", self.refined(), "Implementation.\n")
        self.implement_files(codex, changed)
        claude = FakeClaude(claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05))
        runner = orchestrator.Orchestrator(
            self.target, task="Add large feature", budget_usd=1.0, max_code_review_calls=1,
            workflow_id="legacy-review-limit", process_runner=codex, claude_runner=claude,
        )
        runner.start()
        external = runner.state["authorization"]["claude_aily"]
        external.pop("code_review_policy")
        external["max_code_reviews"] = 1
        with runner._workflow_lock():
            runner._save()
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "12000"}):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "requires .* Claude calls.*authorizes 1"):
                runner.approve()
        saved = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["authorization"]["claude_aily"]["max_code_reviews"], 1)
        self.assertNotIn("code_review_policy", saved["authorization"]["claude_aily"])
        self.assertEqual(len(claude.prompts), 1)
        self.assertEqual(saved["review_campaign"]["status"], "blocked_authorization")
        self.assertTrue(saved["review_campaign"]["files_not_reviewed"])

    def test_invalid_part_report_cannot_complete_campaign_or_omit_files(self):
        changed = {
            f"packages/suite/file_{index}.py": f"def item_{index}():\n" + "    return 'review me'\n" * 80
            for index in range(5)
        }
        codex = FakeCodex("# Proposal\nPlan.\n", self.refined(), "Implementation.\n")
        self.implement_files(codex, changed)
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=lambda _prompt, _budget, _executable: claude_reviewer.ReviewResult(
                "Decision: APPROVED\nUnstructured response.", 0.05,
            ),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Review all modules", budget_usd=1.0, workflow_id="incomplete-part-report",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        claude.auto_structure = False
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "14000"}):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "report for part-000001 is invalid"):
                runner.approve()
        saved = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "failed")
        self.assertNotEqual(saved["review_campaign"]["status"], "complete")
        self.assertEqual(saved["review_campaign"]["part_reviews"][0]["status"], "invalid_report")
        self.assertTrue(saved["review_campaign"]["files_not_reviewed"])
        self.assertFalse((runner.workflow_dir / "delivery.md").exists())

    def test_unsafe_single_file_split_is_recorded_and_can_resume_with_higher_limit(self):
        relative = "generated/one_line.py"
        codex = FakeCodex("# Proposal\nKeep generated content intact.\n", self.refined(), "Implemented.\n")
        def write_one_line(index, _command, _kwargs):
            if index == 2:
                (self.target / relative).parent.mkdir(parents=True, exist_ok=True)
                (self.target / relative).write_text("VALUE = '" + ("x" * 6000) + "'\n", encoding="utf-8")
        codex.on_call = write_one_line
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=lambda _prompt, _budget, _executable: claude_reviewer.ReviewResult(
                "Decision: APPROVED\nNo acceptance-blocking findings.", 0.05,
            ),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Implement generated data", budget_usd=1.0, workflow_id="unsafe-file-split",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "13000"}):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "cannot be split at a safe patch boundary"):
                runner.approve()
        failed = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(failed["review_files_not_reviewed"][0]["path"], relative)
        self.assertIn("cannot be split at a safe patch boundary", failed["review_files_not_reviewed"][0]["reason"])
        self.assertEqual(len(claude.prompts), 1)
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "20000"}):
            completed = runner.resume()
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["review_campaign"]["strategy"], "single_call")
        self.assertEqual(completed["review_files_not_reviewed"], [])
        self.assertEqual(len(claude.prompts), 2)

    def test_path_grouping_uses_repository_structure_without_architecture_assumptions(self):
        self.assertGreater(
            orchestrator._path_affinity("odd/component/alpha.py", "odd/component/beta.py")[0],
            orchestrator._path_affinity("views/alpha.py", "storage/beta.json")[0],
        )
        self.assertGreater(
            orchestrator._path_affinity("features/submit.py", "checks/test_submit.py")[0], 0,
        )

    def test_optional_review_findings_do_not_trigger_correction(self):
        codex = FakeCodex(
            "# Proposal\nPlan.\n", self.refined(), "Implemented.\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            claude_reviewer.ReviewResult(
                "Decision: APPROVED\nOptional improvement: consider a clearer variable name.", 0.05,
            ),
        )
        path = self.target / "src" / "feature.py"
        path.parent.mkdir()
        codex.on_call = lambda index, _command, _kwargs: (
            path.write_text("def value(): return 1\n", encoding="utf-8") if index == 2 else None
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="optional-review",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        state = orchestrator.Orchestrator(
            self.target, workflow_id="optional-review", process_runner=codex, claude_runner=claude,
        ).approve()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(len(codex.commands), 3)
        self.assertTrue(orchestrator._clearly_no_review_findings(state["code_review"]))
        delivery = (runner.workflow_dir / "delivery.md").read_text(encoding="utf-8")
        self.assertIn("## Structured code-review feedback", delivery)
        self.assertIn("## Suggestions", delivery)
        self.assertIn("src/feature.py", delivery)
        self.assertIn("consider a clearer variable name", delivery)
        output = StringIO()
        with redirect_stdout(output):
            orchestrator._print_summary(state)
        self.assertIn("code_review_feedback", output.getvalue())
        self.assertIn("Original reviewer reports", output.getvalue())

    def test_new_and_legacy_code_review_authorization_limits_are_distinct(self):
        new = orchestrator.new_workflow_authorization(self.target, "new-scope", 1.0, 9)
        self.assertEqual(new["claude_aily"]["max_code_reviews"], 9)
        self.assertEqual(new["claude_aily"]["code_review_policy"]["max_calls"], 9)
        old = orchestrator.new_workflow_authorization(self.target, "legacy-scope", 1.0, 1)
        old["claude_aily"].pop("code_review_policy")
        old["scope_sha256"] = orchestrator.authorization_scope_digest(old)
        state = {"authorization": old}
        self.assertEqual(
            orchestrator.validate_workflow_authorization(state, Path(old["target"]), "legacy-scope")
            ["claude_aily"]["max_code_reviews"], 1,
        )
        old["claude_aily"]["max_code_reviews"] = 9
        old["scope_sha256"] = orchestrator.authorization_scope_digest(old)
        with self.assertRaisesRegex(orchestrator.WorkflowError, "review limit"):
            orchestrator.validate_workflow_authorization(state, Path(old["target"]), "legacy-scope")

    def test_optional_improvements_and_structured_report_compatibility(self):
        self.assertTrue(orchestrator._clearly_no_review_findings(
            "Decision: APPROVED\nOptional improvements\n- Optional improvement: rename helper."
        ))
        self.assertFalse(orchestrator._clearly_no_review_findings(
            "Decision: CHANGES_REQUESTED\nFindings: Must fix missing authorization."
        ))
        self.assertFalse(orchestrator._clearly_no_review_findings(
            "Decision: APPROVED\nSeverity: Must fix\nFinding: broken required behavior."
        ))
        segment = orchestrator.ReviewSegment("src/a.py", "complete patch", 1, 1, 1, "hash")
        self.assertTrue(orchestrator.Orchestrator._part_report_is_structured(
            "Decision: APPROVED\nPart ID: part-000001\nFiles reviewed:\n- src/a.py\nFindings: None\n"
            "Cross-component concerns: None\nOverall assessment: adequate.",
            "part-000001", [segment],
        ))
        self.assertTrue(orchestrator.Orchestrator._integration_report_is_structured(
            "Decision: APPROVED\nConsolidated findings: None\nIntegration findings: None\n"
            "Overall assessment: coherent."
        ))

    def test_review_coverage_distinguishes_identical_segments_from_different_files(self):
        first = {
            "path": "src/first.py", "patch_line_start": 1, "patch_line_end": 3,
            "patch_sha256": "identical-content-hash",
        }
        second = {
            "path": "src/second.py", "patch_line_start": 1, "patch_line_end": 3,
            "patch_sha256": "identical-content-hash",
        }
        campaign = {
            "parts": [
                {"part_id": "part-1", "segments": [first]},
                {"part_id": "part-2", "segments": [second]},
            ],
            "part_reviews": [
                {"part_id": "part-1", "status": "reviewed", "segments": [first]},
                {"part_id": "part-2", "status": "pending", "segments": [second]},
            ],
        }
        runner = object.__new__(orchestrator.Orchestrator)
        runner.state = {}

        runner._sync_review_coverage(
            campaign,
            orchestrator.ReviewDiff("", ("src/first.py", "src/second.py"), (), ()),
        )

        self.assertEqual(campaign["files_reviewed"], ["src/first.py"])
        self.assertEqual(
            [item["path"] for item in campaign["files_not_reviewed"]], ["src/second.py"],
        )

    def test_file_coverage_waits_for_every_assigned_segment(self):
        first = {
            "path": "src/module.py", "patch_line_start": 1, "patch_line_end": 4,
            "patch_sha256": "first-segment",
        }
        second = {
            "path": "src/module.py", "patch_line_start": 5, "patch_line_end": 8,
            "patch_sha256": "second-segment",
        }
        campaign = {
            "parts": [
                {"part_id": "part-1", "segments": [first]},
                {"part_id": "part-2", "segments": [second]},
            ],
            "part_reviews": [
                {"part_id": "part-1", "status": "reviewed", "segments": [first]},
                {"part_id": "part-2", "status": "pending", "segments": [second]},
            ],
        }
        runner = object.__new__(orchestrator.Orchestrator)
        runner.state = {}
        review_diff = orchestrator.ReviewDiff("", ("src/module.py",), (), ())

        runner._sync_review_coverage(campaign, review_diff)
        self.assertEqual(campaign["files_reviewed"], [])
        self.assertEqual([item["path"] for item in campaign["files_not_reviewed"]], ["src/module.py"])

        campaign["part_reviews"][1]["status"] = "reviewed"
        runner._sync_review_coverage(campaign, review_diff)
        self.assertEqual(campaign["files_reviewed"], ["src/module.py"])
        self.assertEqual(campaign["files_not_reviewed"], [])

    def test_part_report_requires_each_assigned_file_in_files_reviewed_section(self):
        segments = [
            orchestrator.ReviewSegment("src/first.py", "patch 1", 1, 1, 1, "hash-1"),
            orchestrator.ReviewSegment("src/second.py", "patch 2", 1, 1, 1, "hash-2"),
        ]
        report = (
            "Decision: APPROVED\nPart ID: part-000001\nFiles reviewed:\n- `src/first.py`\n"
            "Findings:\n- **Severity:** Optional improvement\n- **Category:** Design\n"
            "- **File/location:** src/second.py\n- **Description:** Consider a clearer name.\n"
            "- **Impact and reasoning:** The second file has an optional naming refinement.\n"
            "- **Recommended correction:** Rename the helper.\n"
            "Cross-component concerns:\n- None\nOverall assessment: adequate."
        )

        self.assertFalse(orchestrator.Orchestrator._part_report_is_structured(
            report, "part-000001", segments,
        ))

    def test_part_report_accepts_all_assigned_files_listed_with_markdown_backticks(self):
        segments = [
            orchestrator.ReviewSegment("src/first.py", "patch 1", 1, 1, 1, "hash-1"),
            orchestrator.ReviewSegment("src/second.py", "patch 2", 1, 1, 1, "hash-2"),
        ]
        report = (
            "Decision: APPROVED\nPart ID: part-000001\n## Files reviewed\n"
            "- `src/first.py` — complete patch\n- `src/second.py` — complete patch\n"
            "Findings: None\nCross-component concerns: None\nOverall assessment: adequate."
        )

        self.assertTrue(orchestrator.Orchestrator._part_report_is_structured(
            report, "part-000001", segments,
        ))

    def test_failed_review_resume_retries_only_the_failed_stage(self):
        first_codex = FakeCodex("# Proposal\nInitial plan.\n")
        failed_claude = FakeClaude(claude_reviewer.ReviewError("Aily unavailable", cost_usd=0))
        first = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="resume-flow",
            process_runner=first_codex, claude_runner=failed_claude,
        )
        with self.assertRaises(orchestrator.WorkflowError):
            first.start()
        authorization_id = first.state["authorization"]["authorization_id"]
        self.assertEqual(first.state["stage"], "plan_review_1")
        self.assertEqual(first.state["status"], "failed")

        retry_codex = FakeCodex(self.refined())
        retry_claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="resume-flow", process_runner=retry_codex, claude_runner=retry_claude,
        ).resume()
        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertEqual(resumed["authorization"]["authorization_id"], authorization_id)
        self.assertEqual(len(retry_codex.commands), 1)
        self.assertEqual(retry_codex.commands[0][retry_codex.commands[0].index("--sandbox") + 1], "read-only")

    def test_resume_rejects_a_changed_authorization_budget(self):
        codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=0.5, workflow_id="tampered-auth",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        state_path = runner.workflow_dir / "state.json"
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        saved["authorization"]["claude_aily"]["budget_usd"] = 10.0
        state_path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "authorization changed"):
            orchestrator.Orchestrator(self.target, workflow_id="tampered-auth").status()

    def test_resume_rejects_workspace_or_external_scope_expansion(self):
        for workflow_id, mutation, expected_error in (
            ("expanded-workspace", lambda auth: auth["workspace_operations"].append("access_outside_target"),
             "workspace authorization"),
            ("expanded-aily", lambda auth: auth["claude_aily"]["permitted_data"].append("unrelated_files"),
             "Claude/Aily scope"),
        ):
            with self.subTest(workflow_id=workflow_id):
                codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
                claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
                runner = orchestrator.Orchestrator(
                    self.target, task="Add a feature", budget_usd=0.5, workflow_id=workflow_id,
                    process_runner=codex, claude_runner=claude,
                )
                runner.start()
                state_path = runner.workflow_dir / "state.json"
                saved = json.loads(state_path.read_text(encoding="utf-8"))
                mutation(saved["authorization"])
                state_path.write_text(json.dumps(saved), encoding="utf-8")
                with self.assertRaisesRegex(orchestrator.WorkflowError, expected_error):
                    orchestrator.Orchestrator(self.target, workflow_id=workflow_id).resume()

    def test_spend_unknown_stops_later_paid_reviews(self):
        codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", None))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="unknown-spend",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertTrue(state["authorization"]["claude_aily"]["budget_unknown"])
        self.assertEqual(state["authorization"]["claude_aily"]["spent_usd"], 0)
        self.assertEqual(len(claude.prompts), 1)

    def test_unresolved_active_call_blocks_resume_without_resetting_budget(self):
        codex = FakeCodex("# Proposal\nInitial plan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="active-review-call",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        runner.state["status"] = "failed"
        runner.state["stage"] = "code_review"
        runner.state["authorization"]["claude_aily"]["active_call"] = {
            "stage": "code_review", "started_at": "unknown", "cap_usd": 0.9,
        }
        with runner._workflow_lock():
            runner._save()

        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="active-review-call", process_runner=codex, claude_runner=claude,
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "prior Claude call ended without a recorded result"):
            resumed.resume()
        persisted = json.loads(runner.state_path.read_text(encoding="utf-8"))
        authorization = persisted["authorization"]["claude_aily"]
        self.assertTrue(authorization["budget_unknown"])
        self.assertIsNone(authorization["active_call"])
        self.assertEqual(authorization["spent_usd"], 0.1)
        self.assertEqual(len(claude.prompts), 1)

    def test_exhausted_budget_prevents_a_later_paid_review(self):
        codex = FakeCodex(
            "# Proposal\nInitial plan.\n", self.refined(),
            "Implementation complete.\n", "No corrections needed.\n",
        )
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.5))
        (self.target / "src").mkdir()
        codex.on_call = lambda index, _command, _kwargs: (
            (self.target / "src" / "feature.py").write_text("def answer(): return 1\n", encoding="utf-8")
            if index == 2 else None
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=0.5, workflow_id="budget-flow",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        with self.assertRaisesRegex(orchestrator.WorkflowError, "budget is exhausted"):
            orchestrator.Orchestrator(
                self.target, workflow_id="budget-flow", process_runner=codex, claude_runner=claude,
            ).approve()
        state = json.loads((runner.workflow_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], "code_review")
        self.assertEqual(state["authorization"]["claude_aily"]["spent_usd"], 0.5)
        self.assertEqual(len(claude.prompts), 1)

    def test_codex_command_uses_supported_global_approval_option(self):
        command = orchestrator.build_codex_command(
            "codex", self.target, "workspace-write", self.root / "last-message.md",
        )
        self.assertEqual(command[0:4], ["codex", "--ask-for-approval", "never", "exec"])
        self.assertLess(command.index("--ask-for-approval"), command.index("exec"))
        self.assertNotIn("--ask-for-approval", command[4:])
        self.assertIn("--sandbox", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--model", command)

        configured = orchestrator.build_codex_command(
            "codex", self.target, "workspace-write", self.root / "last-message.md", "codex-model-test",
        )
        model_index = configured.index("--model")
        self.assertEqual(configured[model_index + 1], "codex-model-test")

    def test_review_packet_excludes_sensitive_and_preexisting_paths_and_redacts_tokens(self):
        (self.target / "src").mkdir()
        (self.target / "src" / "app.py").write_text('TOKEN = "ghp_123456789012345678901234"\n', encoding="utf-8")
        (self.target / ".env").write_text("PASSWORD=do-not-send-this-value\n", encoding="utf-8")
        git(self.target, "add", "src/app.py", ".env")
        git(self.target, "commit", "-q", "-m", "files")
        baseline = git(self.target, "rev-parse", "HEAD")
        (self.target / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        (self.target / ".env").write_text("PASSWORD=changed-secret-value\n", encoding="utf-8")
        outside = self.root / "outside-secret.txt"
        outside.write_text("OUTSIDE_REPOSITORY_SECRET", encoding="utf-8")
        (self.target / "src" / "external.txt").symlink_to(outside)
        state = {"baseline_commit": baseline, "preexisting_paths": ["src/app.py"]}
        packet = orchestrator._review_diff(state, self.target)
        self.assertIn("No reviewable changes", packet.text)
        self.assertNotIn("changed-secret-value", packet.text)
        first_exclusions = dict(packet.excluded_files)
        self.assertEqual(first_exclusions["src/app.py"], "pre-existing target change is protected from review")
        self.assertEqual(first_exclusions[".env"], "sensitive path is excluded from external review")
        self.assertIn("src/external.txt", first_exclusions)

        state["preexisting_paths"] = []
        packet = orchestrator._review_diff(state, self.target)
        self.assertIn("[REDACTED]", packet.text)
        self.assertNotIn("ghp_123456789012345678901234", packet.text)
        self.assertNotIn("changed-secret-value", packet.text)
        self.assertNotIn("OUTSIDE_REPOSITORY_SECRET", packet.text)
        self.assertEqual(packet.included_paths, ("src/app.py",))

    def test_review_diff_filters_metadata_and_binary_but_preserves_relevant_text(self):
        root_metadata = self.target / ".DS_Store"
        nested_metadata = self.target / "nested" / "folder" / ".DS_Store"
        root_metadata.parent.mkdir(parents=True, exist_ok=True)
        nested_metadata.parent.mkdir(parents=True)
        root_metadata.write_bytes(b"root metadata")
        nested_metadata.write_bytes(b"nested metadata")
        git(self.target, "add", "-f", ".DS_Store", "nested/folder/.DS_Store")
        git(self.target, "commit", "-q", "-m", "metadata baseline")
        baseline = git(self.target, "rev-parse", "HEAD")
        root_metadata.write_bytes(b"changed root metadata\x00")
        nested_metadata.write_bytes(b"changed nested metadata")

        (self.target / "._FinderInfo").write_text("resource fork", encoding="utf-8")
        (self.target / "Thumbs.db").write_bytes(b"thumbnail cache")
        (self.target / "__MACOSX").mkdir()
        (self.target / "__MACOSX" / "._asset").write_bytes(b"resource fork")
        (self.target / "assets").mkdir()
        (self.target / "assets" / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nnot a real image")
        (self.target / "assets" / "opaque.bin").write_bytes(b"plain header\x00binary payload")
        (self.target / "assets" / "invalid-utf8.bin").write_bytes(b"\xff\xfe\xfd\xfc")
        (self.target / "assets" / "controls.bin").write_bytes(b"\x01\x02\x03")
        expected_text = {
            "src/service.py": "def process(item):\n    return item\n",
            "tests/test_contract.py": "def test_contract():\n    assert True\n",
            "docs/api-contract.md": "# API contract\n",
            "schemas/custom.contract": "job_type: bulk\n",
        }
        for relative, content in expected_text.items():
            path = self.target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        packet = orchestrator._review_diff(
            {"baseline_commit": baseline, "preexisting_paths": []}, self.target,
        )
        excluded = dict(packet.excluded_files)
        expected_excluded = {
            ".DS_Store", "nested/folder/.DS_Store", "._FinderInfo", "Thumbs.db",
            "__MACOSX/._asset", "assets/image.png", "assets/opaque.bin", "assets/invalid-utf8.bin",
            "assets/controls.bin",
        }
        self.assertEqual(set(excluded), expected_excluded)
        self.assertEqual(excluded[".DS_Store"], "operating-system metadata")
        self.assertEqual(excluded["nested/folder/.DS_Store"], "operating-system metadata")
        self.assertIn("NUL byte", excluded["assets/opaque.bin"])
        self.assertIn("binary file signature", excluded["assets/image.png"])
        self.assertIn("UTF-8", excluded["assets/invalid-utf8.bin"])
        self.assertIn("control bytes", excluded["assets/controls.bin"])
        self.assertEqual(set(packet.included_paths), set(expected_text))
        for relative, content in expected_text.items():
            for line in content.splitlines():
                self.assertIn(f"+{line}", packet.text)
        self.assertNotIn("not a real image", packet.text)
        changed = set(packet.included_paths) | set(excluded)
        self.assertEqual(changed, set(orchestrator._changed_paths(self.target, baseline)[0]
                                      + orchestrator._changed_paths(self.target, baseline)[1]))

    def test_long_assignment_and_complete_proposal_reach_claude_with_audit_hashes(self):
        task = "# Full assignment\n" + ("requirement detail " * 120) + "\nASSIGNMENT_END_MARKER"
        proposal = "# Complete proposal\n" + ("architecture detail " * 120) + "\nPROPOSAL_END_MARKER"
        codex = FakeCodex(proposal, self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Decision: CHANGES_REQUESTED\nCheck the edge cases.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task=task, budget_usd=1.0, workflow_id="complete-review-context",
            process_runner=codex, claude_runner=claude,
        )

        state = runner.start()
        sent = claude.prompts[0]
        self.assertGreater(len(sent), 1200)
        self.assertIn("ASSIGNMENT_END_MARKER", sent)
        self.assertIn("PROPOSAL_END_MARKER", sent)
        self.assertEqual(state["plan_reviews"][0]["valid"], True)
        manifest = state["plan_reviews"][0]["context_manifest"]
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["characters"], len(sent))
        self.assertEqual(manifest["prompt_sha256"], hashlib.sha256(sent.encode("utf-8")).hexdigest())
        section_hashes = {item["name"]: item["sha256"] for item in manifest["sections"]}
        self.assertEqual(section_hashes["Assignment"], hashlib.sha256(task.encode("utf-8")).hexdigest())
        current_proposal = orchestrator.clean_markdown(proposal).strip()
        self.assertEqual(section_hashes["Current proposal"], hashlib.sha256(current_proposal.encode("utf-8")).hexdigest())
        self.assertIn("must implement, should implement", codex.prompts[0])
        self.assertIn("These are analysis prompts, not automatic implementation requirements", codex.prompts[0])
        self.assertIn("State important unknowns", codex.prompts[0])
        self.assertIn("observable outcomes and acceptance criteria", codex.prompts[0])
        self.assertIn("future production considerations", claude.prompts[0])

    def test_missing_or_oversized_review_context_prevents_a_paid_call(self):
        codex = FakeCodex("# Proposal\nComplete.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="context-guards",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        with runner._workflow_lock():
            header, sections, metadata = runner._plan_review_prompt(2)
            incomplete = dict(sections)
            incomplete.pop("Current proposal")
            with self.assertRaisesRegex(orchestrator.WorkflowError, "missing mandatory sections"):
                runner._call_claude(header, "plan_review_2", incomplete, metadata=metadata)
            self.assertEqual(len(claude.prompts), 1)
            self.assertEqual(runner.state["claude_contexts"][-1]["call_state"], "validation_failed")

            nul_context = dict(sections)
            nul_context["Assignment"] += "\x00hidden"
            with self.assertRaisesRegex(orchestrator.WorkflowError, "contains a NUL byte"):
                runner._call_claude(header, "plan_review_2", nul_context, metadata=metadata)
            self.assertEqual(runner.state["claude_contexts"][-1]["call_state"], "validation_failed")

            with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "80"}):
                with self.assertRaisesRegex(orchestrator.WorkflowError, "no paid call was made"):
                    runner._call_claude(header, "plan_review_2", sections, metadata=metadata)
            self.assertEqual(len(claude.prompts), 1)
            self.assertEqual(len(runner.state["claude_contexts"]), 4)
            self.assertEqual(
                [item["call_state"] for item in runner.state["claude_contexts"]],
                ["completed", "validation_failed", "validation_failed", "validation_failed"],
            )
            self.assertEqual(runner.state["authorization"]["claude_aily"]["budget_unknown"], False)
            self.assertIsNone(runner.state["authorization"]["claude_aily"]["active_call"])

    def test_configured_higher_limit_is_used_for_exact_serialized_payload(self):
        codex = FakeCodex("# Proposal\nComplete.\n", self.refined())
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Plan looks good.", 0.1),
            claude_reviewer.ReviewResult("Second review complete.", 0.1),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="higher-review-limit",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        with runner._workflow_lock():
            header, sections, metadata = runner._plan_review_prompt(2)
            sections["Assignment"] += "é" * 110_000
            with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "120000"}):
                _report, manifest = runner._call_claude(
                    header, "plan_review_2", sections, metadata=metadata,
                )
        submitted = claude.prompts[-1]
        self.assertGreater(len(submitted), 100_000)
        self.assertEqual(manifest["characters"], len(submitted))
        self.assertEqual(manifest["utf8_bytes"], len(submitted.encode("utf-8")))
        self.assertGreater(manifest["utf8_bytes"], manifest["characters"])
        self.assertEqual(manifest["payload_limit_characters"], 120_000)
        self.assertEqual(manifest["prompt_sha256"], hashlib.sha256(submitted.encode("utf-8")).hexdigest())

    def test_validation_failure_can_resume_without_repeating_implementation(self):
        codex = FakeCodex(
            "# Proposal\nPlan.\n", self.refined(), "Implementation complete.\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Plan looks good.", 0.1),
            claude_reviewer.ReviewResult("Decision: APPROVED\nNo actionable findings.", 0.1),
        )
        (self.target / "src").mkdir()
        codex.on_call = lambda index, _command, _kwargs: (
            (self.target / "src" / "feature.py").write_text("def answer(): return 42\n", encoding="utf-8")
            if index == 2 else None
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="review-validation-resume",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "1000"}):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "shared code-review context cannot fit"):
                runner.approve()

        failed = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stage"], "code_review")
        self.assertEqual(failed["claude_contexts"][-1]["stage"], "plan_review_1")
        self.assertNotIn("review_campaign", failed)
        self.assertFalse(failed["authorization"]["claude_aily"]["budget_unknown"])
        self.assertIsNone(failed["authorization"]["claude_aily"]["active_call"])

        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "100000"}):
            resumed = runner.resume()
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(len(codex.commands), 3)
        self.assertEqual(len(claude.prompts), 2)
        self.assertEqual([item["stage"] for item in resumed["claude_contexts"]], ["plan_review_1", "code_review"])
        self.assertEqual(resumed["review_campaign"]["status"], "complete")

    def test_invalid_context_limit_values_are_rejected(self):
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value), patch.dict(os.environ, {
                claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: value,
            }):
                with self.assertRaisesRegex(claude_reviewer.ReviewError, "must be a positive integer"):
                    claude_reviewer.max_review_payload_chars()

    def test_revise_invalidates_unverified_legacy_review_and_reuses_iteration(self):
        codex = FakeCodex(
            "# Proposal\nCurrent plan.\n",
            self.refined(),
            "# Corrected proposal\nThe prior review is invalid.\n",
            self.refined(dispositions="- No actionable findings."),
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("The assignment and proposal are missing.", 0.1),
            claude_reviewer.ReviewResult("Decision: APPROVED\nThe complete proposal meets the assignment.", 0.2),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Build a REST API", budget_usd=1.0, workflow_id="invalidate-legacy-review",
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        state_path = runner.state_path
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        saved["plan_reviews"][0].pop("context_manifest")
        saved["plan_reviews"][0].pop("valid")
        state_path.write_text(json.dumps(saved), encoding="utf-8")

        revised = runner.revise(
            "Preserve the existing proposal. Mark the prior review invalid because its 1,200-character context "
            "was incomplete; do not count its findings. Apply the proportional-design rule and the relevant "
            "api-engineer skill, then request a complete review."
        )
        self.assertEqual(revised["status"], "awaiting_approval")
        self.assertFalse(revised["plan_reviews"][0]["valid"])
        self.assertIn("1,200 characters", revised["plan_reviews"][0]["invalidated_reason"])
        self.assertTrue(revised["plan_reviews"][1]["valid"])
        self.assertEqual(revised["plan_reviews"][1]["iteration"], 1)
        self.assertEqual(revised["plan_reviews"][1]["proposal_version"], 3)
        self.assertEqual(len([r for r in revised["plan_reviews"] if r.get("valid")]), 1)
        self.assertEqual(revised["authorization"]["claude_aily"]["spent_usd"], 0.3)
        self.assertIn("Invalidated attempt", claude.prompts[1])
        self.assertIn("Does the proposal satisfy every explicit assignment requirement?", claude.prompts[1])
        self.assertIn("Is each architectural component and abstraction justified", claude.prompts[1])
        self.assertIn("Mark the prior review invalid", codex.prompts[2])

    def test_codex_receives_target_instructions_and_applicable_skill_contents(self):
        codex_home = self.root / "codex-home"
        skill = codex_home / ".tmp" / "marketplaces" / "postman" / "skills" / "api-engineer" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: api-engineer\ndescription: Use when designing or implementing an API.\n---\n"
            "Establish the API contract first and match tests to behavior at risk.\n",
            encoding="utf-8",
        )
        target_skill = self.target / ".agents" / "skills" / "project-api" / "SKILL.md"
        target_skill.parent.mkdir(parents=True)
        target_skill.write_text(
            "---\nname: project-api\ndescription: Local API conventions.\n---\nUse snake_case field names.\n",
            encoding="utf-8",
        )
        (self.target / "AGENTS.md").write_text("Keep endpoints versioned.\n", encoding="utf-8")
        codex = FakeCodex("# Proposal\nUse the assignment contract.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            runner = orchestrator.Orchestrator(
                self.target, task="Build a REST API", budget_usd=1.0, workflow_id="project-skills",
                process_runner=codex, claude_runner=claude,
            )
            state = runner.start()
        self.assertIn("Keep endpoints versioned.", codex.prompts[0])
        self.assertIn("Establish the API contract first", codex.prompts[0])
        self.assertIn("Use snake_case field names.", codex.prompts[0])
        self.assertIn("api-engineer", claude.prompts[0])
        self.assertEqual({item["name"] for item in state["project_guidance"]["skills"]},
                         {"api-engineer", "project-api"})

    def test_no_applicable_project_skill_is_recorded_without_inventing_one(self):
        codex_home = self.root / "empty-codex-home"
        codex = FakeCodex("# Proposal\nImplement the requested behavior.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Plan looks good.", 0.1))
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            runner = orchestrator.Orchestrator(
                self.target, task="Add a arithmetic helper", budget_usd=1.0, workflow_id="no-skills",
                process_runner=codex, claude_runner=claude,
            )
            state = runner.start()
        self.assertEqual(state["project_guidance"]["skills"], [])
        self.assertIn("Applicable skills: none found", codex.prompts[0])
        self.assertIn("Relevant skills: none found", claude.prompts[0])

    def test_refinement_requires_explicit_disposition_for_findings(self):
        incomplete = (
            "# Proposal\n\n## Claude finding dispositions\n"
            "- Finding: missing empty-input behavior.\n\nSecond plan review needed: no\n"
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "decision and reason"):
            orchestrator.Orchestrator._refined_proposal(incomplete)

    def test_complete_diff_is_not_truncated_and_oversize_fails_explicitly(self):
        content = "# complete diff marker\n" + ("line of relevant code\n" * 120)
        (self.target / "large.py").write_text(content, encoding="utf-8")
        state = {"baseline_commit": git(self.target, "rev-parse", "HEAD"), "preexisting_paths": []}
        diff = orchestrator._review_diff(state, self.target)
        self.assertIn("+# complete diff marker", diff.text)
        self.assertEqual(diff.text.count("+line of relevant code"), 120)
        self.assertGreater(len(diff.text), 1200)
        self.assertNotIn("truncated", diff.text.lower())
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "100"}):
            with self.assertRaisesRegex(claude_reviewer.ReviewError, "no paid call was made"):
                claude_reviewer.validate_review_payload(diff.text)

    def test_redaction_preserves_long_text_and_adapter_refuses_oversize_before_call(self):
        long_context = "context " * 300
        self.assertEqual(claude_reviewer.redact(long_context), long_context)
        called = False
        observed = {}

        def process_runner(command, **kwargs):
            nonlocal called
            called = True
            observed["prompt"] = command[command.index("-p") + 1]
            envelope = {"type": "result", "subtype": "success", "is_error": False,
                        "result": "Decision: APPROVED", "total_cost_usd": 0.1}
            return subprocess.CompletedProcess(command, 0, json.dumps(envelope).encode(), b"")

        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "5000"}):
            claude_reviewer.review(long_context, budget_usd=0.5, process_runner=process_runner)
        self.assertEqual(observed["prompt"], long_context)

        called = False
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "100"}):
            with self.assertRaisesRegex(claude_reviewer.ReviewError, "no paid call was made"):
                claude_reviewer.review(long_context, budget_usd=0.5, process_runner=process_runner)
        self.assertFalse(called)

        with self.assertRaisesRegex(claude_reviewer.ReviewError, "contains a NUL byte"):
            claude_reviewer.review("safe prefix\x00unsafe suffix", budget_usd=0.5, process_runner=process_runner)
        self.assertFalse(called)

    def test_aily_default_executable_uses_configured_path(self):
        expected = "/Users/slcosentino/.local/bin/aily"
        self.assertEqual(claude_reviewer.DEFAULT_AILY_EXECUTABLE, expected)
        observed = {}

        def process_runner(command, **kwargs):
            observed["command"] = command
            envelope = {"type": "result", "subtype": "success", "is_error": False,
                        "result": "Decision: APPROVED", "total_cost_usd": 0.1}
            return subprocess.CompletedProcess(command, 0, json.dumps(envelope).encode(), b"")

        with patch.dict(os.environ, {}, clear=True), patch.object(claude_reviewer, "AILY_EXECUTABLE", expected):
            claude_reviewer.review("review safely", budget_usd=0.5, process_runner=process_runner)

        self.assertEqual(observed["command"][0], expected)

    def test_aily_adapter_uses_budget_cap_and_isolated_review_directory(self):
        configured_dir = self.root / "configured-aily"
        configured_dir.mkdir()
        configured_executable = configured_dir / "aily-wrapper"
        configured_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        configured_executable.chmod(0o755)
        path_dir = self.root / "path-bin"
        path_dir.mkdir()
        incompatible_aily = path_dir / "aily"
        incompatible_aily.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        incompatible_aily.chmod(0o755)

        observed = {}
        prompt = "review this sanitized patch"

        def process_runner(command, **kwargs):
            observed["command"] = command
            observed["cwd"] = kwargs["cwd"]
            envelope = {"type": "result", "subtype": "success", "is_error": False,
                        "result": "Decision: APPROVED", "total_cost_usd": 0.25}
            return subprocess.CompletedProcess(command, 0, json.dumps(envelope).encode(), b"")

        with patch.dict(os.environ, {
            "AILY_EXECUTABLE": str(configured_executable),
            "PATH": str(path_dir),
        }):
            self.assertEqual(shutil.which("aily"), str(incompatible_aily))
            result = claude_reviewer.review(prompt, budget_usd=0.5, process_runner=process_runner)

        self.assertEqual(result.text, "Decision: APPROVED")
        self.assertEqual(result.cost_usd, 0.25)
        command = observed["command"]
        self.assertEqual(command[:4], [str(configured_executable), "claude", "-p", prompt])
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertEqual(command[command.index("--permission-prompts") + 1], "none")
        self.assertIn("--restricted", command)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--disable-slash-commands", command)
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "0.50")
        self.assertNotEqual(Path(observed["cwd"]), self.target)

        missing_executable = self.root / "missing-aily" / "daily"
        with patch.dict(os.environ, {
            "AILY_EXECUTABLE": str(missing_executable),
            "PATH": str(path_dir),
        }):
            with self.assertRaisesRegex(claude_reviewer.ReviewError, "Aily executable is unavailable") as missing:
                claude_reviewer.review(prompt, budget_usd=0.5)
        self.assertIsNone(missing.exception.cost_usd)

        failed_calls = 0

        def unsupported_command_runner(command, **kwargs):
            nonlocal failed_calls
            failed_calls += 1
            return subprocess.CompletedProcess(
                command, 128, b"", b'Error: unknown command "claude" for "aily"',
            )

        def failing_claude(prompt_text, *, budget_usd, executable):
            return claude_reviewer.review(
                prompt_text, budget_usd=budget_usd, executable=executable,
                process_runner=unsupported_command_runner,
            )

        codex = FakeCodex("# Proposal\nUse the requested behavior.\n")
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="unsupported-aily-command",
            aily=str(configured_executable), process_runner=codex, claude_runner=failing_claude,
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "unknown command \\\"claude\\\""):
            runner.start()
        failed = runner.state
        self.assertEqual(failed["status"], "failed")
        authorization = failed["authorization"]["claude_aily"]
        self.assertTrue(authorization["budget_unknown"])
        self.assertEqual(authorization["spent_usd"], 0)
        self.assertEqual(failed_calls, 1)
        with self.assertRaisesRegex(orchestrator.WorkflowError, "Aily exited with status 128"):
            orchestrator.Orchestrator(
                self.target, workflow_id="unsupported-aily-command", aily=str(configured_executable),
                process_runner=codex, claude_runner=failing_claude,
            ).resume()
        self.assertEqual(failed_calls, 1)
        persisted = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertTrue(persisted["authorization"]["claude_aily"]["budget_unknown"])

    def test_invalid_refinement_output_is_persisted_redacted_and_never_approvable(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        proposal = "# Proposal\n\nImplement the requested behavior.\n"
        invalid_output = (
            "# Refined proposal\n\nCredential: " + secret + "\n"
            + ("x" * (orchestrator.MAX_PLANNING_FAILURE_OUTPUT_CHARS + 100))
        )
        codex = FakeCodex(proposal, invalid_output)
        claude = FakeClaude(claude_reviewer.ReviewResult("No actionable findings.", 0.1))
        runner = orchestrator.Orchestrator(
            self.target,
            task="Implement a jobs API",
            budget_usd=1.0,
            workflow_id="invalid-refinement-output",
            process_runner=codex,
            claude_runner=claude,
        )

        with self.assertRaisesRegex(orchestrator.WorkflowError, "Workflow stopped at plan_refine_1"):
            runner.start()

        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        record = state["planning_validation_failures"][0]
        self.assertEqual(state["status"], "failed")
        self.assertEqual(record["validation_status"], "failed_unvalidated")
        self.assertFalse(record["approval_eligible"])
        self.assertEqual(record["stage"], "plan_refine_1")
        self.assertEqual(record["proposal_version"], 2)
        self.assertIn("second plan review is needed", record["validation_error"])
        self.assertTrue(record["generated_output_truncated"])
        self.assertGreater(
            record["generated_output_characters"], orchestrator.MAX_PLANNING_FAILURE_OUTPUT_CHARS,
        )
        self.assertLessEqual(
            len(record["generated_output"]), orchestrator.MAX_PLANNING_FAILURE_OUTPUT_CHARS,
        )
        self.assertTrue(record["secrets_redacted"])
        self.assertNotIn(secret, record["generated_output"])
        self.assertIn("[REDACTED]", record["generated_output"])
        self.assertEqual(state["proposal_version"], 1)
        self.assertEqual(state["proposal"], proposal)
        self.assertEqual(len(claude.prompts), 1)
        with self.assertRaisesRegex(orchestrator.WorkflowError, "not waiting for implementation approval"):
            runner.approve()

        for version in (3, 4, 5):
            with runner._workflow_lock():
                runner._record_unvalidated_planning_output(
                    invalid_output,
                    orchestrator.WorkflowError("Repeated invalid output."),
                    stage="plan_refine_1",
                    proposal_version=version,
                )
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        records = state["planning_validation_failures"]
        self.assertEqual(len(records), orchestrator.MAX_PLANNING_FAILURE_RECORDS)
        self.assertEqual([item["proposal_version"] for item in records], [3, 4, 5])
        self.assertTrue(state["planning_validation_failure_history_truncated"])
        self.assertTrue(all(item["approval_eligible"] is False for item in records))


if __name__ == "__main__":
    unittest.main()

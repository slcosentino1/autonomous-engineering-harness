from __future__ import annotations

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


def prompt_discovery(prompt: str) -> dict:
    section = prompt_section(prompt, "Structured Discovery record")
    if not section:
        match = re.search(
            r"(?s)Structured Discovery record:\s*(\{.*?\})\s*\n\n(?:Claude review|Prior review attempts)", prompt,
        )
        section = match.group(1) if match else ""
    return json.loads(section)


def first_proposal_reference(proposal: str) -> tuple[str, str]:
    headings = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*#*\s*$", proposal))
    sections = [heading for heading in headings if len(heading.group(1)) > 1] or headings
    for index, heading in enumerate(sections):
        end = next((candidate.start() for candidate in headings if candidate.start() > heading.start()
                    and len(candidate.group(1)) <= len(heading.group(1))), len(proposal))
        body_lines = [
            line.strip() for line in proposal[heading.end():end].splitlines()
            if line.strip() and not re.match(r"^#{1,6}\s+", line.strip())
        ]
        if body_lines:
            return heading.group(2).strip(), body_lines[0]
    raise AssertionError("test proposal needs a Markdown heading with body text")


def fake_discovery(proposal: str) -> dict:
    heading, evidence = first_proposal_reference(proposal)
    return {
        "schema_version": 1,
        "requirements": [{
            "id": "REQ-001",
            "description": "Implement the requested assignment behavior.",
            "source": "assignment",
            "interpretation": "The task text is the acceptance baseline.",
            "design_decision": evidence,
            "proposal_reference": heading,
            "proposal_evidence": evidence,
            "expected_verification": "Run the focused workflow tests.",
        }],
        "repository_evidence": [],
        "concerns": [],
        "decisions": [],
    }


def update_discovery_evidence(discovery: dict, proposal: str) -> dict:
    heading, evidence = first_proposal_reference(proposal)
    discovery = json.loads(json.dumps(discovery))
    for item in discovery.get("requirements", []):
        item.update({"proposal_reference": heading, "proposal_evidence": evidence, "design_decision": evidence})
    for item in discovery.get("concerns", []):
        if item.get("disposition") in {"addressed", "documented_assumption", "requires_user_decision"}:
            item.update({"proposal_reference": heading, "proposal_evidence": evidence})
    for item in discovery.get("decisions", []):
        item.update({"proposal_reference": heading, "proposal_evidence": evidence})
    return discovery


class FakeCodex:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.commands: list[list[str]] = []
        self.prompts: list[str] = []
        self.on_call = None
        self.discovery_record: dict | None = None
        self.disposition_overrides: dict[str, str] = {}

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.prompts.append(kwargs["input"].decode("utf-8"))
        if not self.responses:
            raise AssertionError("Unexpected Codex call")
        output_path = Path(command[command.index("--output-last-message") + 1])
        response = self.responses.pop(0)
        if self.on_call is not None:
            self.on_call(len(self.commands) - 1, command, kwargs)
        prompt = kwargs["input"].decode("utf-8")
        if "<FINDING_DISPOSITIONS_JSON>" in prompt:
            response = self._refinement_output(response, prompt)
        elif "<DISCOVERY_RECORD_JSON>" in prompt and "User feedback:" in prompt:
            proposal = response
            current = prompt_discovery(prompt)
            discovery = update_discovery_evidence(current, proposal)
            response = self._discovery_output(discovery, proposal)
        elif "<DISCOVERY_RECORD_JSON>" in prompt:
            response = self._discovery_output(self.discovery_record or fake_discovery(response), response)
        output_path.write_text(response, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    @staticmethod
    def _discovery_output(discovery: dict, proposal: str) -> str:
        return (
            "<DISCOVERY_RECORD_JSON>\n" + json.dumps(discovery, ensure_ascii=False) + "\n"
            "</DISCOVERY_RECORD_JSON>\n<PROPOSAL_MARKDOWN>\n" + proposal.strip()
            + "\n</PROPOSAL_MARKDOWN>"
        )

    def _refinement_output(self, raw_proposal: str, prompt: str) -> str:
        review = orchestrator._parse_json_block(prompt, "PLAN_REVIEW_JSON")
        proposal = raw_proposal
        second = re.search(r"(?im)^\s*Second plan review needed:\s*(yes|no)\s*$", proposal)
        if second:
            second_needed = second.group(1).lower()
            proposal = proposal[:second.start()].rstrip()
        else:
            second_needed = "no"
        findings = review.get("findings", [])
        incorporated_lines = [
            f"{finding['id']}: {finding['recommended_action']}"
            for finding in findings
            if self.disposition_overrides.get(finding["id"], "incorporated") == "incorporated"
        ]
        if incorporated_lines:
            proposal += "\n\n## Review disposition references\n" + "\n".join(incorporated_lines) + "\n"
        old_discovery = prompt_discovery(prompt)
        discovery = update_discovery_evidence(old_discovery, proposal)
        dispositions = []
        for finding in findings:
            disposition = self.disposition_overrides.get(finding["id"], "incorporated")
            item = {
                "finding_id": finding["id"],
                "disposition": disposition,
                "reason": "The proposal was revised to address this finding.",
            }
            if disposition == "incorporated":
                item.update({
                    "proposal_reference": "Review disposition references",
                    "proposal_evidence": f"{finding['id']}: {finding['recommended_action']}",
                })
            dispositions.append(item)
        return (
            "<DISCOVERY_RECORD_JSON>\n" + json.dumps(discovery, ensure_ascii=False) + "\n"
            "</DISCOVERY_RECORD_JSON>\n<FINDING_DISPOSITIONS_JSON>\n"
            + json.dumps({"schema_version": 1, "dispositions": dispositions}, ensure_ascii=False)
            + "\n</FINDING_DISPOSITIONS_JSON>\n<PROPOSAL_MARKDOWN>\n" + proposal
            + "\n</PROPOSAL_MARKDOWN>\n<SECOND_PLAN_REVIEW_NEEDED>" + second_needed
            + "</SECOND_PLAN_REVIEW_NEEDED>"
        )


class FakeClaude:
    def __init__(self, *results, response_factory=None):
        self.results = list(results)
        self.response_factory = response_factory
        self.prompts: list[str] = []
        self.budgets: list[float] = []
        self.auto_structure = True
        self.finding_blocks_approval = False
        self.finding_severity = None

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
        if self.auto_structure and "<PLAN_REVIEW_JSON>" in prompt:
            discovery = json.loads(prompt_section(prompt, "Structured Discovery record"))
            proposal = prompt_section(prompt, "Current proposal")
            needs_finding = bool(re.search(
                r"(?i)please clarify|clarify |check the edge cases|missing|changes_requested", result.text,
            ))
            requirements = []
            for index, requirement in enumerate(discovery["requirements"]):
                row = {
                    "requirement_id": requirement["id"],
                    "status": "unclear" if needs_finding and index == 0 else "covered",
                    "proposal_reference": requirement["proposal_reference"],
                    "proposal_evidence": requirement["proposal_evidence"],
                    "reason": "The assignment edge case needs clarification." if needs_finding and index == 0 else "",
                }
                requirements.append(row)
            concerns = []
            for concern in discovery["concerns"]:
                status = "not_applicable" if concern["applicability"] == "not_applicable" else "reflected"
                concerns.append({
                    "concern_id": concern["id"],
                    "status": status,
                    "proposal_reference": concern.get("proposal_reference", ""),
                    "proposal_evidence": concern.get("proposal_evidence", ""),
                    "reason": concern.get("not_applicable_reason") if status == "not_applicable" else "",
                })
            findings = []
            if needs_finding:
                findings.append({
                    "id": "F-001",
                    "category": "requirements",
                    "severity": "must_fix" if self.finding_blocks_approval else (self.finding_severity or "should_fix"),
                    "description": result.text.strip(),
                    "evidence": "The proposal does not yet make this point clear.",
                    "engineering_impact": "The client-visible behavior could be ambiguous.",
                    "recommended_action": "Clarify the behavior in the proposal.",
                    "blocks_approval": self.finding_blocks_approval,
                    "related_ids": [discovery["requirements"][0]["id"]],
                })
            structured = {
                "schema_version": 1,
                "decision": "changes_requested" if needs_finding else "ready_for_approval",
                "assessment": result.text.strip(),
                "requirement_coverage": requirements,
                "discovery_coverage": concerns,
                "findings": findings,
            }
            return claude_reviewer.ReviewResult(
                "<PLAN_REVIEW_JSON>\n" + json.dumps(structured, ensure_ascii=False)
                + "\n</PLAN_REVIEW_JSON>", result.cost_usd,
            )
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
                + f"\nFindings:\n{severity}{findings}\n\nCross-component concerns:\n- None\n\n"
                f"Overall assessment:\n{result.text}"
            )
            return claude_reviewer.ReviewResult(report, result.cost_usd)
        return result


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
            f"Second plan review needed: {second_review}\n"
        )

    def waiting_workflow(self, workflow_id: str, decisions: list[dict] | None = None,
                         *, blocking_review_finding: bool = False,
                         blocking_disposition: str = "incorporated", material_finding: bool = False,
                         finding_severity: str | None = None):
        proposal = (
            "# Migration proposal\n\n## Scope\n"
            "Empty jobs are accepted by default. Processing continues after item failure. "
            "The API returns item-level results.\n"
        )
        discovery = fake_discovery(proposal)
        discovery["decisions"] = decisions or []
        for decision in discovery["decisions"]:
            decision.setdefault("proposal_reference", "Scope")
            decision.setdefault("proposal_evidence", "Empty jobs are accepted by default.")
        codex = FakeCodex(proposal, self.refined())
        codex.discovery_record = discovery
        if blocking_review_finding or material_finding:
            codex.disposition_overrides["F-001"] = blocking_disposition
        has_finding = blocking_review_finding or material_finding or finding_severity is not None
        claude_text = "Please clarify the data edge case." if has_finding else "Plan looks good."
        claude = FakeClaude(claude_reviewer.ReviewResult(claude_text, 0.1))
        claude.finding_blocks_approval = blocking_review_finding
        claude.finding_severity = "must_fix" if material_finding else finding_severity
        runner = orchestrator.Orchestrator(
            self.target, task="Implement a jobs API", budget_usd=1.0, workflow_id=workflow_id,
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        return runner, codex, claude

    @staticmethod
    def required_decision(decision_id: str = "DEC-EMPTY") -> dict:
        return {
            "id": decision_id,
            "classification": "required_user_decision",
            "description": "Should an empty job be accepted?",
            "reason": "This changes externally visible behavior.",
            "source_ids": ["REQ-001"],
            "proposal_reference": "Scope",
            "proposal_evidence": "Empty jobs are accepted by default.",
            "choices": ["accept empty jobs", "reject empty jobs"],
            "proposed_default": "accept empty jobs",
        }

    def approve_without_running(self, runner, decisions: list[str]):
        with patch.object(runner, "run_pending", side_effect=lambda: runner.state):
            return runner.approve(decisions)

    def implement_files(self, codex: FakeCodex, changed: dict[str, str]) -> None:
        def implementation(index, _command, _kwargs):
            if index != 2:
                return
            for relative, content in changed.items():
                path = self.target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        codex.on_call = implementation

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
        self.assertIn("F-001: incorporated", claude.prompts[1])
        self.assertIn("Plan review iteration 2", claude.prompts[1])
        self.assertIn("proposal version 2", claude.prompts[1])
        self.assertEqual(state["claude_contexts"][1]["proposal_version"], 2)

    def test_user_revision_uses_remaining_plan_review_and_still_waits_for_approval(self):
        codex = FakeCodex(
            "# Proposal\nInitial plan.\n",
            self.refined(dispositions="- Finding: clarify empty input | Decision: incorporated | "
                                      "Reason: define zero-item behavior."),
            "# Revised proposal\nAdd explicit empty-input behavior.\n",
            "# Final proposal\n\n## Scope\nHandle the empty-input case.\n",
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
                "path": ".engineering/workflows/file-coverage-review/discovery-v1.json",
                "reason": "workflow state is not part of the implementation diff",
            },
            {
                "path": ".engineering/workflows/file-coverage-review/discovery-v2.json",
                "reason": "workflow state is not part of the implementation diff",
            },
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
            with self.assertRaisesRegex(orchestrator.WorkflowError, "incomplete structured report"):
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
        self.assertIn("Review requirement coverage, architecture and boundaries", claude.prompts[1])
        self.assertIn("Compare the structured Discovery record with the proposal", claude.prompts[1])
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

    def test_structured_discovery_validation_and_contextual_unknowns(self):
        proposal = "# Proposal\n\n## Scope\nImplement the requested behavior.\n"
        valid = fake_discovery(proposal)
        concern = {
            "id": "RISK-001", "category": "external_dependency", "description": "Processor contract unknown.",
            "source": "assignment", "interpretation_impact": "Error and timeout behavior is not specified.",
            "applicability": "unknown", "disposition": "deferred", "expected_verification": "Use a local stub.",
            "material": True, "related_requirement_ids": ["REQ-001"],
        }
        valid["concerns"].append(concern)
        checked = orchestrator.validate_discovery_record(valid, proposal, proposal_version=7)
        self.assertEqual(checked["proposal_version"], 7)
        self.assertEqual(checked["concerns"][0]["applicability"], "unknown")

        missing_mapping = json.loads(json.dumps(fake_discovery(proposal)))
        missing_mapping["requirements"][0]["design_decision"] = "A decision absent from the plan."
        with self.assertRaisesRegex(orchestrator.WorkflowError, "design decision"):
            orchestrator.validate_discovery_record(missing_mapping, proposal, proposal_version=1)

        missing_reason = json.loads(json.dumps(fake_discovery(proposal)))
        not_applicable = dict(concern, applicability="not_applicable", disposition="not_applicable")
        missing_reason["concerns"].append(not_applicable)
        with self.assertRaisesRegex(orchestrator.WorkflowError, "has no reason"):
            orchestrator.validate_discovery_record(missing_reason, proposal, proposal_version=1)

        missing_disposition = json.loads(json.dumps(fake_discovery(proposal)))
        invalid_risk = dict(concern)
        del invalid_risk["disposition"]
        missing_disposition["concerns"].append(invalid_risk)
        with self.assertRaisesRegex(orchestrator.WorkflowError, "needs non-empty disposition"):
            orchestrator.validate_discovery_record(missing_disposition, proposal, proposal_version=1)

    def test_discovery_persistence_is_versioned_and_linked_to_the_proposal(self):
        runner, _codex, _claude = self.waiting_workflow("discovery-persist")
        state = runner.state
        artifact = runner.workflow_dir / state["discovery_artifact"]
        stored = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema_version"], orchestrator.DISCOVERY_SCHEMA_VERSION)
        self.assertEqual(stored["proposal_version"], state["proposal_version"])
        self.assertEqual(stored, state["discovery"])
        current_version = next(
            item for item in state["proposal_versions"] if item["version"] == state["proposal_version"]
        )
        self.assertEqual(current_version["discovery_artifact"], artifact.name)
        self.assertEqual(current_version["discovery_sha256"], state["discovery_sha256"])

        # Simulate a crash after writing a new artifact but before saving its state association.
        next_proposal = "# Updated proposal\n\n## Scope\nImplement the requested behavior with a retry.\n"
        next_discovery = update_discovery_evidence(state["discovery"], next_proposal)
        next_version = state["proposal_version"] + 1
        orphan = runner.workflow_dir / f"discovery-v{next_version}.json"
        orphan.write_text("orphaned pre-save artifact\n", encoding="utf-8")
        runner._persist_planning_version(next_discovery, next_proposal, next_version)
        self.assertEqual(json.loads(orphan.read_text(encoding="utf-8")), runner.state["discovery"])
        self.assertEqual(runner.state["proposal_versions"][-1]["version"], next_version)

    @staticmethod
    def _review_fixture(proposal: str):
        discovery = fake_discovery(proposal)
        heading, evidence = first_proposal_reference(proposal)
        finding = {
            "id": "F-001", "category": "requirements", "severity": "must_fix",
            "description": "Document the response to an empty request.",
            "evidence": "The response status is part of the client contract.",
            "engineering_impact": "Clients need the status to handle empty submissions consistently.",
            "recommended_action": "State that empty requests return HTTP 400 and create no job.",
            "blocks_approval": False,
            "related_ids": ["REQ-001"],
        }
        report = {
            "schema_version": 1, "decision": "ready_for_approval", "assessment": "The plan is complete.",
            "requirement_coverage": [{
                "requirement_id": "REQ-001", "status": "covered", "proposal_reference": heading,
                "proposal_evidence": evidence, "reason": "",
            }],
            "discovery_coverage": [], "findings": [finding],
        }
        return discovery, report, finding

    def test_structured_review_and_disposition_are_reconciled_by_stable_id(self):
        proposal = (
            "# Proposal\n\n## Scope\nEmpty requests return HTTP 400 and create no job.\n"
            "Implement the requested behavior.\n"
        )
        discovery, report, finding = self._review_fixture(proposal)
        parsed = orchestrator._validate_plan_review(
            "<PLAN_REVIEW_JSON>" + json.dumps(report) + "</PLAN_REVIEW_JSON>", discovery, proposal,
        )
        self.assertEqual(parsed["findings"][0]["id"], finding["id"])
        valid = {
            "schema_version": 1, "dispositions": [{
                "finding_id": "F-001", "disposition": "incorporated", "reason": "Added the client behavior.",
                "proposal_reference": "Scope",
                "proposal_evidence": "Empty requests return HTTP 400 and create no job.",
            }],
        }
        dispositions, unresolved = orchestrator._validate_finding_dispositions(valid, [finding], proposal)
        self.assertEqual(dispositions[0]["disposition"], "incorporated")
        self.assertTrue(dispositions[0]["reference_verified"])
        self.assertFalse(dispositions[0]["resolution_verified"])
        self.assertEqual(unresolved, [])

    def test_unrelated_exact_quote_is_only_a_verified_reference(self):
        proposal = (
            "# Proposal\n\n## Scope\nEmpty requests return HTTP 400 and create no job.\n"
            "Implement the requested behavior.\n"
        )
        _discovery, _report, finding = self._review_fixture(proposal)
        dispositions, _unresolved = orchestrator._validate_finding_dispositions({
            "schema_version": 1,
            "dispositions": [{
                "finding_id": "F-001", "disposition": "incorporated",
                "reason": "The quote exists in the proposal.", "proposal_reference": "Scope",
                "proposal_evidence": "Implement the requested behavior.",
            }],
        }, [finding], proposal)
        self.assertTrue(dispositions[0]["reference_verified"])
        self.assertFalse(dispositions[0]["resolution_verified"])
        self.assertEqual(dispositions[0]["proposal_evidence"], "Implement the requested behavior.")

    def test_plan_review_rejects_duplicate_finding_ids_and_unverifiable_coverage(self):
        proposal = "# Proposal\n\n## Scope\nImplement the requested behavior.\n"
        discovery, report, _finding = self._review_fixture(proposal)
        duplicate = json.loads(json.dumps(report))
        duplicate["findings"].append(dict(duplicate["findings"][0]))
        with self.assertRaisesRegex(orchestrator.WorkflowError, "duplicates finding id"):
            orchestrator._validate_plan_review(
                "<PLAN_REVIEW_JSON>" + json.dumps(duplicate) + "</PLAN_REVIEW_JSON>", discovery, proposal,
            )
        bad_coverage = json.loads(json.dumps(report))
        bad_coverage["requirement_coverage"][0]["proposal_evidence"] = "Not in the proposal."
        with self.assertRaisesRegex(orchestrator.WorkflowError, "without verifiable proposal evidence"):
            orchestrator._validate_plan_review(
                "<PLAN_REVIEW_JSON>" + json.dumps(bad_coverage) + "</PLAN_REVIEW_JSON>", discovery, proposal,
            )

    def test_dispositions_reject_missing_duplicate_unknown_invalid_and_unverified_entries(self):
        proposal = "# Proposal\n\n## Scope\nImplement the requested behavior.\n"
        finding = {"id": "F-001", "blocks_approval": False, "description": "Clarify the outcome."}
        valid_item = {
            "finding_id": "F-001", "disposition": "accepted_risk", "reason": "Low impact for this assignment.",
        }
        cases = [
            ({"schema_version": 1, "dispositions": []}, "omitted dispositions"),
            ({"schema_version": 1, "dispositions": [valid_item, valid_item]}, "duplicates the disposition"),
            ({"schema_version": 1, "dispositions": [dict(valid_item, finding_id="F-999")]}, "unknown finding id"),
            ({"schema_version": 1, "dispositions": [dict(valid_item, disposition="ignored")]}, "invalid value"),
            ({"schema_version": 1, "dispositions": [{
                "finding_id": "F-001", "disposition": "incorporated", "reason": "Done.",
                "proposal_reference": "Scope", "proposal_evidence": "Absent quote.",
            }]}, "no verifiable proposal reference"),
        ]
        for raw, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(orchestrator.WorkflowError, message):
                orchestrator._validate_finding_dispositions(raw, [finding], proposal)

    def test_blocking_finding_requires_an_explicit_approval_resolution(self):
        proposal = "# Proposal\n\n## Scope\nImplement the requested behavior.\n"
        finding = {"id": "F-001", "blocks_approval": True, "description": "Resolve this risk."}
        raw = {"schema_version": 1, "dispositions": [{
            "finding_id": "F-001", "disposition": "accepted_risk", "reason": "Retained for this scope.",
        }]}
        _dispositions, unresolved = orchestrator._validate_finding_dispositions(raw, [finding], proposal)
        self.assertEqual(unresolved[0]["finding"]["id"], "F-001")

    def test_blocking_finding_must_be_resolved_by_the_user_before_approval(self):
        runner, _codex, _claude = self.waiting_workflow(
            "approval-blocking-finding", blocking_review_finding=True, blocking_disposition="accepted_risk",
        )
        self.assertEqual(runner.state["proposal_readiness"]["status"], "human_review_required")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "REVIEW-review-1:F-001"):
            runner.approve()
        approved = self.approve_without_running(runner, ["REVIEW-review-1:F-001=accept_risk"])
        self.assertEqual(approved["approved_decision_resolutions"][0]["answer"], "accept_risk")

    def test_material_must_fix_finding_requires_human_semantic_verification(self):
        runner, _codex, _claude = self.waiting_workflow(
            "approval-material-finding", material_finding=True,
        )
        finding = runner.state["plan_reviews"][0]["material_findings_for_human_review"][0]
        self.assertTrue(finding["reference_verified"])
        self.assertFalse(finding["resolution_verified"])
        self.assertEqual(runner.state["proposal_readiness"]["status"], "human_review_required")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "verify_resolved"):
            runner.approve()

        approved = self.approve_without_running(runner, ["REVIEW-review-1:F-001=verify_resolved"])
        resolution = approved["approved_decision_resolutions"][0]
        self.assertTrue(resolution["reference_verified"])
        self.assertTrue(resolution["resolution_verified"])
        self.assertTrue(approved["plan_reviews"][0]["material_findings_for_human_review"][0]["resolution_verified"])

    def test_should_fix_finding_does_not_automatically_block_approval(self):
        runner, _codex, _claude = self.waiting_workflow(
            "approval-minor-finding", finding_severity="should_fix",
        )
        self.assertEqual(runner.state["proposal_readiness"]["status"], "ready_for_approval")
        self.assertEqual(runner.state["proposal_readiness"]["material_findings_for_human_review"], [])
        self.assertFalse(any(
            item["id"] == "REVIEW-review-1:F-001" for item in orchestrator._approval_decisions_for_display(runner.state)
        ))

    def test_older_structured_state_derives_material_review_without_migration(self):
        runner, _codex, _claude = self.waiting_workflow(
            "approval-material-legacy-shape", material_finding=True,
        )
        runner.state["plan_reviews"][0].pop("material_findings_for_human_review")
        disposition = runner.state["plan_review_decisions"][0]["parsed_dispositions"][0]
        disposition.pop("reference_verified", None)
        disposition.pop("resolution_verified", None)
        runner._save()
        decisions = orchestrator._approval_decisions_for_display(runner.state)
        material = next(item for item in decisions if item["id"] == "REVIEW-review-1:F-001")
        self.assertTrue(material["reference_verified"])
        self.assertFalse(material["resolution_verified"])
        with self.assertRaisesRegex(orchestrator.WorkflowError, "verify_resolved"):
            runner.approve()

    def test_unambiguous_legacy_finding_decision_remains_readable(self):
        runner, _codex, _claude = self.waiting_workflow(
            "legacy-finding-decision", material_finding=True,
        )
        runner.state["decision_resolutions"].append({
            "decision_id": "REVIEW-F-001",
            "answer": "verify_resolved",
            "proposal_version": runner.state["proposal_version"],
            "discovery_sha256": runner.state["discovery_sha256"],
        })
        resolved = runner._resolve_approval_items([])
        self.assertEqual(resolved[0]["decision_id"], "REVIEW-review-1:F-001")
        self.assertTrue(resolved[0]["resolution_verified"])
        self.assertTrue(any(
            row.get("decision_id") == "REVIEW-F-001" for row in runner.state["decision_resolutions"]
        ))

    def test_material_finding_survives_a_later_review_iteration(self):
        proposal = "# Proposal\n\n## Scope\nImplement the requested behavior.\n"
        codex = FakeCodex(proposal, self.refined("yes"), self.refined())
        codex.discovery_record = fake_discovery(proposal)
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Please clarify the data edge case.", 0.1),
            claude_reviewer.ReviewResult("Plan looks good.", 0.1),
        )
        claude.finding_severity = "must_fix"
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="material-across-reviews",
            process_runner=codex, claude_runner=claude,
        )
        state = runner.start()
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertEqual(len(state["plan_reviews"]), 2)
        pending = state["material_findings_pending"][0]
        self.assertEqual(pending["finding_id"], "F-001")
        self.assertEqual(pending["finding_identity"], "review-1:F-001")
        self.assertEqual(pending["proposal_version"], state["proposal_version"])
        self.assertFalse(pending["reference_verified"])
        self.assertEqual(
            {item["finding_identity"] for item in state["material_findings_pending"]},
            {"review-1:F-001"},
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "REVIEW-review-1:F-001"):
            runner.approve()

    def test_duplicate_finding_ids_across_reviews_get_independent_decisions(self):
        runner, _codex, _claude = self.waiting_workflow(
            "duplicate-finding-identities", material_finding=True,
        )
        state = runner.state
        first = dict(state["material_findings_pending"][0])
        second = dict(first)
        second.update({
            "review_iteration": 2,
            "finding_identity": "review-2:F-001",
            "description": "Restart recovery is unclear.",
            "human_resolution": None,
            "resolution_verified": False,
        })
        second_review = json.loads(json.dumps(state["plan_reviews"][0]))
        second_review.update({"iteration": 2, "attempt": 2})
        second_review["material_findings_for_human_review"] = [second]
        state["plan_reviews"].append(second_review)
        state["material_findings_pending"].append(second)
        state["decision_resolutions"].append({
            "decision_id": "REVIEW-F-001",
            "answer": "verify_resolved",
            "proposal_version": state["proposal_version"],
            "discovery_sha256": state["discovery_sha256"],
        })
        runner._save()

        item_ids = {item["id"] for item in runner._required_approval_items()
                    if item["kind"] == "material_finding_resolution"}
        self.assertEqual(item_ids, {"REVIEW-review-1:F-001", "REVIEW-review-2:F-001"})
        with self.assertRaisesRegex(orchestrator.WorkflowError, "REVIEW-review-2:F-001"):
            runner._resolve_approval_items(["REVIEW-review-1:F-001=verify_resolved"])
        pending = {item["finding_identity"]: item for item in runner.state["material_findings_pending"]}
        self.assertTrue(pending["review-1:F-001"]["resolution_verified"])
        self.assertIsNone(pending["review-2:F-001"].get("human_resolution"))

        runner._resolve_approval_items(["REVIEW-review-2:F-001=accept_risk"])
        pending = {item["finding_identity"]: item for item in runner.state["material_findings_pending"]}
        self.assertTrue(pending["review-1:F-001"]["resolution_verified"])
        self.assertEqual(pending["review-2:F-001"]["human_resolution"], "accept_risk")
        self.assertFalse(pending["review-2:F-001"]["resolution_verified"])

        old_version = runner.state["proposal_version"]
        revised_proposal = runner.state["proposal"] + "\n## More\nRecovery is specified.\n"
        revised_discovery = update_discovery_evidence(runner.state["discovery"], revised_proposal)
        runner._persist_planning_version(revised_discovery, revised_proposal, old_version + 1)
        refreshed = {item["finding_identity"]: item for item in
                     orchestrator._material_findings_pending_for_state(runner.state, old_version + 1)}
        self.assertEqual(set(refreshed), {"review-1:F-001", "review-2:F-001"})
        self.assertFalse(any(item.get("resolution_verified") for item in refreshed.values()))
        self.assertTrue(all("human_resolution" not in item for item in refreshed.values()))
        required_ids = {item["id"] for item in runner._required_approval_items()}
        self.assertTrue({"REVIEW-review-1:F-001", "REVIEW-review-2:F-001"} <= required_ids)

    def test_material_resolution_is_invalidated_after_a_proposal_revision(self):
        runner, _codex, _claude = self.waiting_workflow(
            "material-decision-stale", material_finding=True,
        )
        runner._resolve_approval_items(["REVIEW-review-1:F-001=verify_resolved"])
        runner._save()
        previous_version = runner.state["proposal_version"]
        revised_proposal = runner.state["proposal"] + "\n## Additional behavior\nA new case is documented.\n"
        revised_discovery = update_discovery_evidence(runner.state["discovery"], revised_proposal)
        runner._persist_planning_version(revised_discovery, revised_proposal, previous_version + 1)
        runner.state["status"] = "awaiting_approval"
        runner.state["proposal_readiness"] = {
            "status": "human_review_required", "proposal_version": previous_version + 1,
        }
        runner._save()
        with self.assertRaisesRegex(orchestrator.WorkflowError, "REVIEW-review-1:F-001"):
            runner._resolve_approval_items([])
        pending = runner.state["material_findings_pending"][0]
        self.assertFalse(pending.get("resolution_verified", False))
        self.assertNotIn("human_resolution", pending)

    def test_successful_review_call_with_invalid_json_is_not_a_valid_review(self):
        codex = FakeCodex("# Proposal\n\n## Scope\nImplement the request.\n")
        claude = FakeClaude(claude_reviewer.ReviewResult("not structured JSON", 0.17))
        claude.auto_structure = False
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="invalid-review-output",
            process_runner=codex, claude_runner=claude,
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "missing the PLAN_REVIEW_JSON block"):
            runner.start()
        review = runner.state["plan_reviews"][0]
        self.assertTrue(review["call_succeeded"])
        self.assertTrue(review["context_valid"])
        self.assertFalse(review["findings_parsed"])
        self.assertFalse(review["reconciled"])
        self.assertFalse(review["valid"])
        self.assertEqual(runner.state["proposal_readiness"]["status"], "review_parse_failed")
        self.assertEqual(runner.state["authorization"]["claude_aily"]["spent_usd"], 0.17)
        self.assertEqual(len(claude.prompts), 1)

    def test_required_approval_decisions_show_default_and_block_until_explicit(self):
        runner, _codex, _claude = self.waiting_workflow(
            "approval-required", [self.required_decision()],
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "Proposed default: accept empty jobs"):
            runner.approve()
        self.assertIsNone(runner.state["implementation_approved_at"])
        self.assertEqual(runner.state["decision_resolutions"], [])

    def test_proposed_default_and_alternative_decisions_are_explicitly_saved(self):
        runner, _codex, _claude = self.waiting_workflow(
            "approval-default", [self.required_decision()],
        )
        approved = self.approve_without_running(runner, ["DEC-EMPTY=accept empty jobs"])
        self.assertEqual(approved["status"], "running")
        self.assertEqual(approved["approved_proposal_version"], 2)
        saved = approved["approved_decision_resolutions"][0]
        self.assertEqual(saved["answer"], "accept empty jobs")
        self.assertEqual(saved["proposal_version"], approved["proposal_version"])

        alternative, _codex, _claude = self.waiting_workflow(
            "approval-alternative", [self.required_decision()],
        )
        approved_alternative = self.approve_without_running(
            alternative, ["DEC-EMPTY=reject empty jobs"],
        )
        self.assertEqual(approved_alternative["approved_decision_resolutions"][0]["answer"], "reject empty jobs")

    def test_partial_decision_answers_persist_and_stale_answers_do_not_carry_forward(self):
        first = self.required_decision("DEC-EMPTY")
        second = self.required_decision("DEC-FAILURE")
        second["description"] = "Should an item failure stop processing?"
        second["choices"] = ["stop on failure", "continue after failure"]
        second["proposed_default"] = "stop on failure"
        second["proposal_evidence"] = "Processing continues after item failure."
        runner, _codex, _claude = self.waiting_workflow("approval-partial", [first, second])
        with self.assertRaisesRegex(orchestrator.WorkflowError, "DEC-FAILURE"):
            runner.approve(["DEC-EMPTY=accept empty jobs"])
        saved = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["decision_resolutions"][0]["answer"], "accept empty jobs")

        self.approve_without_running(runner, ["DEC-FAILURE=stop on failure"])
        revised_proposal = "# Updated proposal\n\n## Scope\nEmpty jobs are rejected by default. The API returns item-level results.\n"
        revised_discovery = update_discovery_evidence(runner.state["discovery"], revised_proposal)
        runner._persist_planning_version(revised_discovery, revised_proposal, 3)
        runner.state["status"] = "awaiting_approval"
        runner.state["proposal_readiness"] = {"status": "ready_for_approval", "proposal_version": 3}
        runner._save()
        with self.assertRaisesRegex(orchestrator.WorkflowError, "DEC-EMPTY.*Proposed default"):
            runner._resolve_approval_items([])
        self.assertEqual(runner.state["decision_resolutions"][0]["proposal_version"], 2)

    def test_reversible_assumptions_and_future_work_do_not_block_approval(self):
        assumption = self.required_decision("DEC-MODULE")
        assumption["classification"] = "reversible_assumption"
        assumption.pop("choices")
        assumption.pop("proposed_default")
        future = self.required_decision("DEC-HA")
        future["classification"] = "future_production"
        future.pop("choices")
        future.pop("proposed_default")
        runner, _codex, _claude = self.waiting_workflow("approval-nonblocking", [assumption, future])
        approved = self.approve_without_running(runner, [])
        self.assertEqual(approved["status"], "running")
        self.assertEqual(approved["approved_decision_resolutions"], [])

    def test_legacy_approval_fails_without_rewriting_workflow_state(self):
        runner, _codex, _claude = self.waiting_workflow("legacy-approval")
        legacy = json.loads(runner.state_path.read_text(encoding="utf-8"))
        for key in (
            "discovery", "discovery_schema_version", "discovery_sha256", "discovery_artifact",
            "proposal_versions", "decision_resolutions",
        ):
            legacy.pop(key, None)
        runner.state_path.write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")
        before = hashlib.sha256(runner.state_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(orchestrator.WorkflowError, "predates structured Discovery"):
            orchestrator.Orchestrator(self.target, workflow_id="legacy-approval").approve()
        after = hashlib.sha256(runner.state_path.read_bytes()).hexdigest()
        self.assertEqual(after, before)

    def test_refinement_requires_explicit_disposition_for_each_finding(self):
        finding = {"id": "F-001", "blocks_approval": False, "description": "Clarify the behavior."}
        proposal = "# Proposal\n\n## Scope\nClarify the behavior.\n"
        with self.assertRaisesRegex(orchestrator.WorkflowError, "omitted dispositions"):
            orchestrator._validate_finding_dispositions(
                {"schema_version": 1, "dispositions": []}, [finding], proposal,
            )

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


if __name__ == "__main__":
    unittest.main()

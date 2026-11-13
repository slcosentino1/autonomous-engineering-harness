from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import claude_reviewer  # noqa: E402
import orchestrator  # noqa: E402
from observability import langfuse as langfuse_adapter  # noqa: E402
from observability.tracer import safe_workflow_reference  # noqa: E402
from tools import read_documentation_url  # noqa: E402
from workflow import review as review_contracts  # noqa: E402
from workflow import workspace as workspace_ops  # noqa: E402
from workflow import persistence as persistence_ops  # noqa: E402


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


class CodexGuidanceTests(unittest.TestCase):
    def test_read_only_codex_calls_isolate_user_mcp_configuration_even_without_url_access(self):
        with tempfile.TemporaryDirectory() as root:
            command = orchestrator.build_codex_command(
                "codex", Path(root), "read-only", Path(root) / "result.md",
            )

        self.assertIn("--ignore-user-config", command)
        self.assertIn("mcp_servers={}", command)

    def test_codex_guidance_keeps_small_file_inventory(self):
        prompt = orchestrator._guidance_for_codex({"files": ["README.md", "src/api.py"]})

        self.assertIn("Files: README.md, src/api.py", prompt)

    def test_provider_guidance_omits_oversized_file_inventory(self):
        files = [f"src/module-{index:05d}/file.py" for index in range(5_000)]

        codex_prompt = orchestrator._guidance_for_codex({"files": files})
        claude_prompt = orchestrator._guidance_for_claude({"files": files})

        for prompt, label in ((codex_prompt, "Files"), (claude_prompt, "Repository files")):
            self.assertIn(f"{label}: full inventory omitted (5,000 paths;", prompt)
            self.assertIn("Use `rg --files` and focused `rg` searches", prompt)
            self.assertNotIn(files[0], prompt)
            self.assertLess(len(prompt), orchestrator.MAX_PROMPT_FILE_INVENTORY_CHARS)

    def test_claude_repository_context_omits_oversized_file_inventory(self):
        files = [f"src/module-{index:05d}/file.py" for index in range(5_000)]
        runner = object.__new__(orchestrator.Orchestrator)

        context = runner._repository_context_for_claude({"files": files})

        self.assertIn("Files: full inventory omitted (5,000 paths;", context)
        self.assertNotIn(files[0], context)

    def test_default_claude_payload_limit_is_increased(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(claude_reviewer.max_review_payload_chars(), 200_000)

    def test_exact_documentation_urls_are_authorized_only_in_read_only_plan_stages(self):
        self.assertEqual(
            orchestrator._planning_read_only_authorized_urls("planning", "Read https://example.com/docs"),
            ["https://example.com/docs"],
        )
        self.assertEqual(
            orchestrator._planning_read_only_authorized_urls(
                "plan_refine_1", "Read https://example.com/docs and https://api.example.org/openapi.json",
            ),
            ["https://api.example.org/openapi.json", "https://example.com/docs"],
        )
        self.assertEqual(
            orchestrator._planning_read_only_authorized_urls("human_revision", "Read https://example.com/docs"),
            ["https://example.com/docs"],
        )
        self.assertEqual(orchestrator._planning_read_only_authorized_urls(
            "implementation", "Read https://example.com/docs",
        ), [])
        self.assertEqual(orchestrator._planning_read_only_authorized_urls(
            "planning", "Read http://example.com/docs",
        ), [])
        self.assertEqual(orchestrator._planning_read_only_authorized_urls("planning", "No external documentation"), [])

    def test_project_mcp_config_blocks_documentation_tool_isolation(self):
        with tempfile.TemporaryDirectory() as temporary_target:
            target = Path(temporary_target)
            config_dir = target / ".codex"
            config_dir.mkdir()
            (config_dir / "config.toml").write_text(
                '[mcp_servers.unapproved]\ncommand = "unapproved"\n', encoding="utf-8",
            )

            with self.assertRaisesRegex(orchestrator.WorkflowError, "declares MCP servers"):
                orchestrator._assert_no_project_mcp_servers(target)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is unavailable")
    def test_installed_codex_mcp_config_merges_user_servers_and_exec_flag_is_supported(self):
        with tempfile.TemporaryDirectory() as temporary_target, tempfile.TemporaryDirectory() as codex_home:
            target = Path(temporary_target)
            command = orchestrator.build_codex_command(
                "codex", target, "read-only", target / "result.md",
                read_only_authorized_urls=["https://docs.example.com/api"],
            )
            config = command[command.index("-c") + 1]
            (Path(codex_home) / "config.toml").write_text(
                '[mcp_servers.unrelated]\ncommand = "must-not-load"\nargs = []\n', encoding="utf-8",
            )
            environment = dict(os.environ, CODEX_HOME=codex_home)
            listed = subprocess.run(
                ["codex", "mcp", "list", "--json", "-c", config],
                env=environment, capture_output=True, text=True, timeout=10, check=True,
            )
            effective_without_isolation = {item["name"] for item in json.loads(listed.stdout)}
            self.assertEqual(effective_without_isolation, {"documentation_reader", "unrelated"})
            help_result = subprocess.run(
                ["codex", "exec", "--ignore-user-config", "--help"],
                env=environment, capture_output=True, text=True, timeout=10, check=True,
            )
            self.assertIn("Do not load `$CODEX_HOME/config.toml`", help_result.stdout)
        self.assertIn("--ignore-user-config", command)


class DocumentationReaderTests(unittest.TestCase):
    def test_documentation_url_tool_declares_read_only_non_destructive_mcp_annotations(self):
        tool = read_documentation_url._tool_definition()
        self.assertEqual(
            tool["annotations"],
            {
                "title": "Read an authorized documentation URL",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": True,
            },
        )

    def test_tls_context_uses_certifi_when_no_system_trust_store_exists(self):
        certifi_module = SimpleNamespace(where=lambda: "/tmp/ca-bundle.pem")
        with (
            patch(
                "tools.read_documentation_url.ssl.get_default_verify_paths",
                return_value=SimpleNamespace(cafile=None, capath=None),
            ),
            patch.dict(sys.modules, {"certifi": certifi_module}),
            patch("tools.read_documentation_url.ssl.create_default_context", return_value="verified-context") as create_context,
        ):
            self.assertEqual(read_documentation_url._default_tls_context(), "verified-context")
        create_context.assert_called_once_with(cafile="/tmp/ca-bundle.pem")

    def test_url_validation_requires_https_allowlisted_standard_host(self):
        url = "https://docs.example.com/api"
        authorized = {read_documentation_url.authorized_url_sha256(url)}
        self.assertEqual(
            read_documentation_url._validate_url(url, authorized), url,
        )
        for url in (
            "http://docs.example.com/api",
            "https://docs.example.com/other",
            "https://docs.example.com/api?repository=secret",
            "https://other.example.com/api",
            "https://user:password@docs.example.com/api",
            "https://docs.example.com:8443/api",
            "https://127.0.0.1/api",
            "https://docs.example.com/api?access_token=secret",
        ):
            with self.subTest(url=url), self.assertRaises(read_documentation_url.ReadOnlyDocsError):
                read_documentation_url._validate_url(url, authorized)

    def test_dns_policy_accepts_only_global_ipv4_and_ipv6_addresses(self):
        for address in ("8.8.8.8", "2001:4860:4860::8888"):
            with self.subTest(address=address), patch(
                "tools.read_documentation_url.socket.getaddrinfo",
                return_value=[(socket.AF_INET6 if ":" in address else socket.AF_INET, 1, 6, "", (address, 443))],
            ):
                self.assertEqual(read_documentation_url._resolve_public_addresses("docs.example.com"), [address])

        for address in (
            "10.20.30.40", "172.16.0.10", "192.168.1.10", "fc00::10", "127.0.0.1", "::1",
            "169.254.169.254", "fe80::1", "224.0.0.1", "ff02::1", "0.0.0.0", "::", "192.0.2.1",
        ):
            with self.subTest(address=address), patch(
                "tools.read_documentation_url.socket.getaddrinfo",
                return_value=[(socket.AF_INET6 if ":" in address else socket.AF_INET, 1, 6, "", (address, 443))],
            ), self.assertRaises(read_documentation_url.ReadOnlyDocsError):
                read_documentation_url._resolve_public_addresses("docs.example.com")

    def test_mixed_dns_answers_fail_closed(self):
        with patch(
            "tools.read_documentation_url.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, 1, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, 1, 6, "", ("10.0.0.8", 443)),
            ],
        ), self.assertRaisesRegex(read_documentation_url.ReadOnlyDocsError, "non-public"):
            read_documentation_url._resolve_public_addresses("docs.example.com")

    def test_openapi_filter_returns_matching_operations_and_referenced_schemas(self):
        document = {
            "openapi": "3.0.0",
            "info": {"title": "API"},
            "paths": {
                "/gra-milestones/v2/filters": {
                    "get": {
                        "tags": ["gra-milestones"],
                        "responses": {"200": {"$ref": "#/components/responses/Success"}},
                    },
                },
                "/users": {"get": {"tags": ["users"], "responses": {}}},
            },
            "components": {
                "responses": {"Success": {"content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/FilterOptions"},
                }}}},
                "schemas": {"FilterOptions": {"type": "array", "items": {
                    "$ref": "#/components/schemas/FilterOption",
                }}, "FilterOption": {"type": "object", "properties": {"id": {"type": "string"}}}},
                "securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}},
            },
        }

        filtered = json.loads(read_documentation_url._filter_openapi(document, "gra-milestones"))

        self.assertEqual(list(filtered["paths"]), ["/gra-milestones/v2/filters"])
        self.assertIn("Success", filtered["components"]["responses"])
        self.assertIn("FilterOptions", filtered["components"]["schemas"])
        self.assertIn("FilterOption", filtered["components"]["schemas"])
        self.assertIn("BearerAuth", filtered["components"]["securitySchemes"])


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


class ReviewCampaignBoundaryTests(unittest.TestCase):
    def test_campaign_runs_through_state_and_provider_callbacks_without_orchestrator(self):
        patch = "diff --git a/src/feature.py b/src/feature.py\n+value = 1\n"
        segment = review_contracts.ReviewSegment(
            "src/feature.py", patch, 1, 1, 1, review_contracts._review_patch_sha256(patch),
        )
        review_diff = review_contracts.ReviewDiff(
            patch, (segment.path,), (), ((segment.path, patch),),
        )
        part = {"part_id": "part-000001", "segments": [segment]}
        campaign = {
            "campaign_id": "callback-campaign",
            "status": "planned",
            "shared_context_sha256": "shared-hash",
            "payload_limit_characters": 10000,
            "parts": review_contracts.ReviewCampaign._campaign_part_manifest([part]),
            "part_reviews": [{"part_id": part["part_id"], "status": "pending", "report": None, "calls": []}],
            "integration_review": {"required": False, "status": "not_required", "report": None, "calls": []},
            "files_reviewed": [],
            "files_not_reviewed": [{"path": segment.path, "reason": "pending"}],
            "excluded_files": [],
        }
        state = {"review_campaign": campaign, "claude_contexts": [], "proposal_version": 1}
        report = (
            "Decision: APPROVED\nPart ID: part-000001\n\n"
            "## Files reviewed\n- `src/feature.py`\n\n"
            "## Findings\nNone\n\n"
            "## Cross-component concerns\nNone\n\n"
            "## Overall assessment\nThe changed file was reviewed."
        )
        saves = []
        calls = []

        def request_review(header, stage, sections, *, metadata):
            calls.append((header, stage, sections, metadata))
            return report, {
                "prompt_sha256": "prompt-hash", "cost_usd": 0.05, "call_state": "completed",
                "campaign_call_number": 1,
            }

        campaign_ops = review_contracts.ReviewCampaign(
            state,
            save_state=lambda: saves.append("saved"),
            request_review=request_review,
        )

        result = campaign_ops._run_review_campaign(
            campaign, {"Assignment": "Review this change"}, [part], review_diff,
        )

        self.assertEqual(result, report)
        self.assertEqual(campaign["status"], "complete")
        self.assertEqual(campaign["files_reviewed"], [segment.path])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "code_review")
        self.assertGreater(len(saves), 1)


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

    def test_target_change_inventory_classifies_baseline_changes_and_filters_workflows(self):
        modified = self.target / "modified.py"
        deleted = self.target / "deleted.py"
        modified.write_text("baseline\n", encoding="utf-8")
        deleted.write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.target), "add", "modified.py", "deleted.py"], check=True)
        subprocess.run(["git", "-C", str(self.target), "commit", "-q", "-m", "inventory baseline"], check=True)
        baseline = git(self.target, "rev-parse", "HEAD")

        modified.write_text("changed after baseline\n", encoding="utf-8")
        deleted.unlink()
        added = self.target / "added.py"
        added.write_text("staged addition\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.target), "add", "added.py"], check=True)
        (self.target / "untracked.py").write_text("untracked\n", encoding="utf-8")
        artifact = self.target / ".engineering" / "workflows" / "sample" / "state.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("{}\n", encoding="utf-8")

        inventory = workspace_ops.target_change_inventory(
            self.target,
            baseline,
            ["modified.py", "untracked.py"],
        )
        by_path = {item["path"]: item for item in inventory}

        self.assertEqual(by_path, {
            "added.py": {"path": "added.py", "change": "added", "preexisting": "no"},
            "deleted.py": {"path": "deleted.py", "change": "deleted", "preexisting": "no"},
            "modified.py": {"path": "modified.py", "change": "modified", "preexisting": "yes"},
            "untracked.py": {"path": "untracked.py", "change": "untracked", "preexisting": "yes"},
        })
        self.assertNotIn(".engineering/workflows/sample/state.json", by_path)

        runner = object.__new__(orchestrator.Orchestrator)
        runner.target = self.target
        runner.state = {"baseline_commit": baseline, "preexisting_paths": ["modified.py", "untracked.py"]}
        self.assertEqual(runner._target_change_inventory(), inventory)

    def test_target_change_inventory_is_empty_without_a_valid_baseline(self):
        self.assertEqual(workspace_ops.target_change_inventory(self.target, None, []), [])
        self.assertEqual(workspace_ops.target_change_inventory(self.target, "", []), [])


class RepositoryFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="repository-fingerprint-tests-")
        self.root = Path(self.temp.name)
        self.target = make_target(self.root / "target")

    def tearDown(self):
        self.temp.cleanup()

    def fingerprint(self):
        return workspace_ops.repository_fingerprint(self.target)

    def test_clean_fingerprint_and_repeated_execution_are_deterministic(self):
        first = self.fingerprint()
        second = self.fingerprint()

        self.assertEqual(first["working_tree_sha256"], second["working_tree_sha256"])
        self.assertEqual(first["commit_sha"], git(self.target, "rev-parse", "HEAD"))
        self.assertTrue(first["working_tree_clean"])
        self.assertFalse(first["staged_changes"])
        self.assertFalse(first["unstaged_changes"])
        self.assertFalse(first["untracked_files"])
        self.assertEqual(first["tracked_path_count"], 1)

    def test_fingerprinting_does_not_refresh_or_write_the_git_index(self):
        tracked_file = self.target / "README.md"
        metadata = tracked_file.stat()
        os.utime(tracked_file, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 5_000_000_000))
        index_path = self.target / ".git" / "index"
        index_before = (index_path.read_bytes(), index_path.stat().st_mtime_ns)

        result = self.fingerprint()

        index_after = (index_path.read_bytes(), index_path.stat().st_mtime_ns)
        self.assertEqual(index_after, index_before)
        self.assertTrue(result["working_tree_clean"])

    def test_modified_tracked_file_and_different_content_change_fingerprint(self):
        baseline = self.fingerprint()["working_tree_sha256"]
        (self.target / "README.md").write_text("changed content\n", encoding="utf-8")

        changed = self.fingerprint()
        self.assertNotEqual(changed["working_tree_sha256"], baseline)
        self.assertFalse(changed["working_tree_clean"])
        self.assertTrue(changed["unstaged_changes"])

    def test_staged_and_mixed_staged_unstaged_modifications_are_detected(self):
        (self.target / "README.md").write_text("staged content\n", encoding="utf-8")
        git(self.target, "add", "README.md")
        staged = self.fingerprint()
        self.assertTrue(staged["staged_changes"])
        self.assertFalse(staged["unstaged_changes"])

        (self.target / "README.md").write_text("later worktree content\n", encoding="utf-8")
        mixed = self.fingerprint()
        self.assertTrue(mixed["staged_changes"])
        self.assertTrue(mixed["unstaged_changes"])
        self.assertNotEqual(staged["working_tree_sha256"], mixed["working_tree_sha256"])

    def test_untracked_and_deleted_files_are_detected(self):
        (self.target / "new.py").write_text("value = 1\n", encoding="utf-8")
        untracked = self.fingerprint()
        self.assertTrue(untracked["untracked_files"])
        self.assertEqual(untracked["untracked_path_count"], 1)
        self.assertNotEqual(untracked["working_tree_sha256"], self.fingerprint_after_removal("new.py"))

        (self.target / "README.md").unlink()
        deleted = self.fingerprint()
        self.assertFalse(deleted["working_tree_clean"])
        self.assertTrue(deleted["unstaged_changes"])
        self.assertEqual(deleted["tracked_path_count"], 1)

    def fingerprint_after_removal(self, relative: str) -> str:
        path = self.target / relative
        content = path.read_bytes()
        path.unlink()
        value = self.fingerprint()["working_tree_sha256"]
        path.write_bytes(content)
        return value

    def test_ignored_untracked_sensitive_and_workflow_artifact_exclusions(self):
        (self.target / ".gitignore").write_text("ignored-cache\n", encoding="utf-8")
        git(self.target, "add", ".gitignore")
        git(self.target, "commit", "-q", "-m", "ignore generated cache")
        baseline = self.fingerprint()["working_tree_sha256"]
        (self.target / "ignored-cache").write_text("ignored\n", encoding="utf-8")
        (self.target / ".engineering" / "workflows" / "sample").mkdir(parents=True)
        (self.target / ".engineering" / "workflows" / "sample" / "state.json").write_text("{}\n")
        (self.target / ".engineering" / "workflows" / "sample" / ".env").write_text("workflow secret\n")
        excluded = self.fingerprint()
        self.assertEqual(excluded["working_tree_sha256"], baseline)
        self.assertTrue(excluded["working_tree_clean"])
        self.assertFalse(excluded["untracked_files"])
        self.assertEqual(excluded["excluded_sensitive_path_count"], 0)

        (self.target / ".env").write_text("secret value\n", encoding="utf-8")
        with_sensitive = self.fingerprint()
        self.assertEqual(with_sensitive["working_tree_sha256"], baseline)
        self.assertFalse(with_sensitive["working_tree_clean"])
        self.assertTrue(with_sensitive["untracked_files"])
        self.assertEqual(with_sensitive["untracked_path_count"], 0)
        self.assertEqual(with_sensitive["excluded_sensitive_path_count"], 1)
        self.assertNotIn("secret value", json.dumps(with_sensitive))
        (self.target / ".env").write_text("different secret value\n", encoding="utf-8")
        changed_secret = self.fingerprint()
        self.assertEqual(changed_secret["working_tree_sha256"], baseline)
        self.assertEqual(changed_secret["excluded_sensitive_path_count"], 1)

    def test_sensitive_worktree_changes_are_omitted_but_git_status_is_reported(self):
        secret = self.target / ".env"
        secret.write_text("secret v1\n", encoding="utf-8")
        git(self.target, "add", ".env")
        git(self.target, "commit", "-q", "-m", "add sensitive file")
        clean = self.fingerprint()
        self.assertTrue(clean["working_tree_clean"])
        self.assertEqual(clean["excluded_sensitive_path_count"], 1)

        secret.write_text("secret v2\n", encoding="utf-8")
        unstaged = self.fingerprint()
        self.assertEqual(unstaged["working_tree_sha256"], clean["working_tree_sha256"])
        self.assertFalse(unstaged["working_tree_clean"])
        self.assertFalse(unstaged["staged_changes"])
        self.assertTrue(unstaged["unstaged_changes"])
        self.assertFalse(unstaged["untracked_files"])
        self.assertEqual(unstaged["excluded_sensitive_path_count"], 1)
        self.assertNotIn("secret v2", json.dumps(unstaged))

        git(self.target, "add", ".env")
        staged = self.fingerprint()
        self.assertEqual(staged["working_tree_sha256"], clean["working_tree_sha256"])
        self.assertFalse(staged["working_tree_clean"])
        self.assertTrue(staged["staged_changes"])
        self.assertFalse(staged["unstaged_changes"])
        self.assertEqual(staged["excluded_sensitive_path_count"], 1)

        git(self.target, "commit", "-q", "-m", "update sensitive file")
        committed = self.fingerprint()
        self.assertNotEqual(committed["commit_sha"], clean["commit_sha"])
        self.assertNotEqual(committed["working_tree_sha256"], clean["working_tree_sha256"])
        self.assertTrue(committed["working_tree_clean"])
        self.assertEqual(committed["excluded_sensitive_path_count"], 1)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_fifo_fallback_is_metadata_only_and_git_omits_it_from_repository_inventory(self):
        baseline = self.fingerprint()["working_tree_sha256"]
        fifo = self.target / "untracked.pipe"
        os.mkfifo(fifo)

        result = self.fingerprint()
        kind, _mode, size, content_hash = workspace_ops._hash_worktree_path(self.target, b"untracked.pipe", [])

        self.assertEqual(result["working_tree_sha256"], baseline)
        self.assertTrue(result["working_tree_clean"])
        self.assertFalse(result["untracked_files"])
        self.assertEqual(kind, b"fifo")
        self.assertEqual(size, b"0")
        self.assertEqual(content_hash, b"")

    def test_untracked_and_tracked_file_content_fingerprints_differ(self):
        first = self.fingerprint()["working_tree_sha256"]
        (self.target / "extra.txt").write_text("alpha\n", encoding="utf-8")
        alpha = self.fingerprint()["working_tree_sha256"]
        (self.target / "extra.txt").write_text("bravo\n", encoding="utf-8")
        bravo = self.fingerprint()["working_tree_sha256"]

        self.assertNotEqual(first, alpha)
        self.assertNotEqual(alpha, bravo)


class PersistenceModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="persistence-module-tests-")
        self.root = Path(self.temp.name)
        self.target = make_target(self.root / "target")

    def tearDown(self):
        self.temp.cleanup()

    def test_json_serialization_and_legacy_state_round_trip(self):
        legacy_state = {
            "version": 1,
            "workflow_id": "legacy-state",
            "target": str(self.target.resolve()),
            "status": "awaiting_approval",
            "stage": "implementation_approval",
        }
        expected = json.dumps(legacy_state, indent=2, ensure_ascii=False) + "\n"
        self.assertEqual(persistence_ops.serialize_json(legacy_state), expected)
        path = self.root / "state.json"
        path.write_text(expected, encoding="utf-8")
        original_bytes, loaded = persistence_ops.read_json_bytes(path)
        self.assertEqual(original_bytes, expected.encode("utf-8"))
        self.assertEqual(loaded, legacy_state)
        self.assertEqual(persistence_ops.read_json(path), legacy_state)

    def test_atomic_text_replaces_existing_file_and_removes_temporary_file(self):
        path = self.root / "state.json"
        path.write_text("old state\n", encoding="utf-8")
        with patch("workflow.persistence.os.replace", wraps=os.replace) as replace:
            persistence_ops.atomic_text(path, '{"status": "new"}\n')
        self.assertEqual(path.read_text(encoding="utf-8"), '{"status": "new"}\n')
        replace.assert_called_once()
        self.assertEqual(Path(replace.call_args.args[1]), path)
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ["state.json", "target"])

    def test_legacy_authorization_state_loads_without_rewriting_its_schema(self):
        runner = orchestrator.Orchestrator(self.target, workflow_id="legacy-persisted-state")
        authorization = orchestrator.new_workflow_authorization(runner.target, runner.workflow_id, 1.0, 1)
        authorization["claude_aily"].pop("code_review_policy")
        authorization["scope_sha256"] = orchestrator.authorization_scope_digest(authorization)
        legacy_state = {
            "version": 1,
            "workflow_id": runner.workflow_id,
            "target": str(runner.target),
            "status": "awaiting_approval",
            "stage": "implementation_approval",
            "authorization": authorization,
        }
        runner.state_path.parent.mkdir(parents=True)
        persistence_ops.atomic_text(runner.state_path, persistence_ops.serialize_json(legacy_state))
        saved_bytes = runner.state_path.read_bytes()

        loaded_authorization = runner._read_state()

        self.assertEqual(loaded_authorization, authorization)
        self.assertEqual(runner.state_path.read_bytes(), saved_bytes)
        self.assertNotIn("codex_model", runner.state)
        self.assertNotIn("code_review_policy", runner.state["authorization"]["claude_aily"])

    def test_malformed_state_keeps_workflow_error_contract(self):
        runner = orchestrator.Orchestrator(self.target, workflow_id="malformed-state")
        runner.state_path.parent.mkdir(parents=True)
        runner.state_path.write_text("{malformed", encoding="utf-8")
        with self.assertRaisesRegex(
                orchestrator.WorkflowError, "Workflow state is missing or invalid"):
            runner._read_state()

    def test_save_still_requires_workflow_lock_and_translates_write_failures(self):
        runner = orchestrator.Orchestrator(self.target, workflow_id="persist-under-lock")
        runner.workflow_dir.mkdir(parents=True)
        runner.state = {"version": 1}
        with self.assertRaisesRegex(orchestrator.WorkflowError, "state writes require the workflow process lock"):
            runner._save()
        self.assertFalse(runner.state_path.exists())

        with runner._workflow_lock():
            with patch("workflow.persistence.os.replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(orchestrator.WorkflowError, "disk full"):
                    orchestrator._atomic_text(runner.state_path, "state\n")
            with patch("workflow.persistence.os.replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(orchestrator.WorkflowError, "disk full"):
                    runner._save()


class CodexInvocationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-evidence-")
        self.root = Path(self.temp.name)
        self.target = make_target(self.root / "target")

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *, stdout=b"", stderr=b"", message=b"Codex response\n", returncode=0,
               model=None, process_error=None):
        process = self.Process(stdout, stderr, message, returncode, process_error)
        runner = orchestrator.Orchestrator(
            self.target, task="Test invocation evidence", workflow_id="codex-evidence",
            budget_usd=1.0, model=model, process_runner=process,
        )
        runner.workflow_dir.mkdir(parents=True, exist_ok=True)
        runner.state = {
            "version": 1,
            "workflow_id": runner.workflow_id,
            "target": str(runner.target),
            "task": "Test invocation evidence",
            "stage": "implementation",
            "authorization": orchestrator.new_workflow_authorization(
                runner.target, runner.workflow_id, 1.0,
            ),
            "operation_history": [],
            "active_operation_id": None,
        }
        return runner, process

    class Process:
        def __init__(self, stdout, stderr, message, returncode, process_error):
            self.stdout = stdout
            self.stderr = stderr
            self.message = message
            self.returncode = returncode
            self.process_error = process_error
            self.input = None
            self.command = None

        def __call__(self, command, **kwargs):
            self.command = command
            self.input = kwargs["input"]
            if self.process_error is not None:
                if isinstance(self.process_error, subprocess.TimeoutExpired):
                    raise subprocess.TimeoutExpired(
                        command, self.process_error.timeout,
                        output=self.process_error.output, stderr=self.process_error.stderr,
                    )
                raise self.process_error
            output_path = Path(command[command.index("--output-last-message") + 1])
            if self.message is not None:
                output_path.write_bytes(self.message)
            return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)

    def run_locked(self, runner, prompt="unique-private-prompt"):
        with runner._workflow_lock():
            return runner._run_codex(prompt, sandbox="workspace-write")

    @staticmethod
    def export_trace(tracer):
        class Client:
            def __init__(self):
                self.observations = []

            def start_as_current_observation(self, **kwargs):
                self.observations.append(kwargs)
                return nullcontext()

            def flush(self):
                return None

            def shutdown(self):
                return None

        client = Client()

        class SDK:
            @staticmethod
            def Langfuse(**_kwargs):
                return client

            @staticmethod
            def propagate_attributes(**_kwargs):
                return nullcontext()

        with patch.object(langfuse_adapter.importlib, "import_module", return_value=SDK()):
            tracer.flush()
        return client.observations

    def test_hashes_exact_input_and_captured_stream_bytes_without_persisting_raw_streams(self):
        stdout = (
            b'{"type":"thread.started"}\n'
            b'{"type":"item.started","item":{"type":"mcp_tool_call",'
            b'"tool_name":"repo.search","arguments":{"token":"private-tool-argument"}}}\n'
            b'{"type":"item.completed","item":{"type":"mcp_tool_call",'
            b'"tool_name":"repo.search","status":"completed"}}\n'
            b'{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":4},'
            b'"model":"codex-test-model"}\n'
        )
        stderr = b"private-stderr-diagnostic"
        message = "  Final response: café.  \n".encode("utf-8")
        runner, process = self.invoke(stdout=stdout, stderr=stderr, message=message)

        response = self.run_locked(runner)

        operation = runner.state["operation_history"][-1]
        state_text = runner.state_path.read_text(encoding="utf-8")
        self.assertEqual(response, "Final response: café.")
        self.assertEqual(operation["input_sha256"], hashlib.sha256(process.input).hexdigest())
        self.assertEqual(operation["input_bytes"], len(process.input))
        self.assertEqual(operation["input_characters"], len(process.input.decode("utf-8")))
        self.assertNotIn("unique-private-prompt", state_text)
        self.assertEqual(operation["stdout_sha256"], hashlib.sha256(stdout).hexdigest())
        self.assertEqual(operation["stdout_bytes"], len(stdout))
        self.assertEqual(operation["stderr_sha256"], hashlib.sha256(stderr).hexdigest())
        self.assertEqual(operation["stderr_bytes"], len(stderr))
        self.assertEqual(operation["last_message_file_sha256"], hashlib.sha256(message).hexdigest())
        self.assertEqual(operation["last_message_file_bytes"], len(message))
        self.assertEqual(operation["result_sha256"], hashlib.sha256(response.encode("utf-8")).hexdigest())
        self.assertEqual(operation["reported_usage"], {"input_tokens": 12, "output_tokens": 4})
        self.assertEqual(operation["usage_source"], "codex_jsonl_turn_completed")
        self.assertEqual(operation["model"], "codex-test-model")
        self.assertEqual(operation["model_source"], "codex_jsonl_event")
        self.assertEqual(operation["tool_invocations"][0]["tool_names"], ["repo.search"])
        self.assertEqual(operation["tool_invocations"][0]["started_events"], 1)
        self.assertEqual(operation["tool_invocations"][0]["completed_events"], 1)
        self.assertEqual(operation["tool_invocations"][0]["outcome_unreported_events"], 1)
        self.assertNotIn(stdout.decode("utf-8"), state_text)
        self.assertNotIn(stderr.decode("utf-8"), state_text)
        self.assertNotIn("private-tool-argument", state_text)
        self.assertNotIn("unique-private-prompt", state_text)
        # The normalized final message is retained temporarily for safe resume recovery.
        self.assertEqual(json.loads(state_text)["operation_history"][-1]["result_text"], response)
        summary_output = StringIO()
        with redirect_stdout(summary_output):
            orchestrator._print_summary(runner.state)
        summary = summary_output.getvalue()
        self.assertIn(operation["stdout_sha256"], summary)
        self.assertNotIn("private-tool-argument", summary)
        self.assertNotIn("private-stderr-diagnostic", summary)
        self.assertNotIn("unique-private-prompt", summary)
        self.assertIsInstance(operation["duration_ms"], int)
        self.assertEqual(operation["stdout_truncated"], False)
        self.assertEqual(operation["stderr_capture_representation"], "bytes")

    def test_missing_last_message_file_uses_only_explicit_jsonl_agent_message(self):
        stdout = (
            b'{"type":"item.completed","item":{"type":"reasoning","text":"private reasoning"}}\n'
            b'{"type":"item.completed","item":{"type":"agent_message","text":"Visible result"}}\n'
        )
        runner, _process = self.invoke(stdout=stdout, message=None)

        response = self.run_locked(runner)

        operation = runner.state["operation_history"][-1]
        state_text = runner.state_path.read_text(encoding="utf-8")
        self.assertEqual(response, "Visible result")
        self.assertEqual(operation["last_message_file_status"], "missing")
        self.assertEqual(operation["result_text"], "Visible result")
        self.assertNotIn("private reasoning", state_text)
        self.assertNotIn(stdout.decode("utf-8"), state_text)

    def test_missing_model_and_cli_metadata_remain_explicit(self):
        runner, process = self.invoke(
            stdout=b'{"type":"turn.completed","usage":{"total_tokens":0}}\n',
            model=None,
        )

        self.run_locked(runner)

        operation = runner.state["operation_history"][-1]
        self.assertIsNone(operation["model"])
        self.assertEqual(operation["model_source"], "not_reported")
        self.assertIsNone(operation["reported_model"])
        self.assertEqual(operation["cli_version_source"], "unavailable")
        self.assertEqual(operation["usage_source"], "codex_jsonl_turn_completed")
        self.assertEqual(operation["reported_usage"], {"total_tokens": 0})
        self.assertNotIn("--model", process.command)

    def test_text_capture_hashes_its_utf8_encoding_and_records_representation(self):
        stdout = '{"type":"item.completed","item":{"type":"agent_message","text":"Ready"}}\n'
        stderr = "diagnostic é"
        runner, _process = self.invoke(stdout=stdout, stderr=stderr, message=None)

        self.assertEqual(self.run_locked(runner), "Ready")

        operation = runner.state["operation_history"][-1]
        self.assertEqual(operation["stdout_sha256"], hashlib.sha256(stdout.encode("utf-8")).hexdigest())
        self.assertEqual(operation["stdout_bytes"], len(stdout.encode("utf-8")))
        self.assertEqual(operation["stdout_capture_representation"], "utf8_encoded_text")
        self.assertEqual(operation["stderr_sha256"], hashlib.sha256(stderr.encode("utf-8")).hexdigest())
        self.assertEqual(operation["stderr_capture_representation"], "utf8_encoded_text")

    def test_configured_model_and_cli_reported_model_keep_distinct_sources(self):
        runner, process = self.invoke(
            stdout=b'{"type":"turn.completed","model":"resolved-model"}\n',
            model="configured-model",
        )

        self.run_locked(runner)

        operation = runner.state["operation_history"][-1]
        self.assertEqual(process.command[process.command.index("--model") + 1], "configured-model")
        self.assertEqual(operation["model"], "configured-model")
        self.assertEqual(operation["model_source"], "workflow_configuration")
        self.assertEqual(operation["reported_model"], "resolved-model")
        self.assertEqual(operation["reported_model_source"], "codex_jsonl_event")

    def test_empty_or_unavailable_stream_and_empty_final_message_are_recorded(self):
        runner, _process = self.invoke(stdout=None, stderr=None, message=b"")

        with self.assertRaisesRegex(orchestrator.WorkflowError, "empty response"):
            self.run_locked(runner)

        operation = runner.state["operation_history"][-1]
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["stdout_capture_status"], "unavailable")
        self.assertEqual(operation["stderr_capture_status"], "unavailable")
        self.assertIsNone(operation["stdout_sha256"])
        self.assertEqual(operation["last_message_file_status"], "empty")
        self.assertEqual(operation["last_message_file_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertIsNone(operation["reported_usage"])
        self.assertEqual(operation["usage_source"], "not_reported")

    def test_nonzero_exit_records_streams_exit_status_and_failure(self):
        stdout = b'{"type":"item.completed","item":{"type":"command_execution"}}\n'
        stderr = b"Codex process failed"
        runner, _process = self.invoke(stdout=stdout, stderr=stderr, message=None, returncode=7)

        with self.assertRaisesRegex(orchestrator.WorkflowError, "status 7"):
            self.run_locked(runner)

        operation = runner.state["operation_history"][-1]
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["exit_code"], 7)
        self.assertEqual(operation["error_category"], "CodexExitError")
        self.assertEqual(operation["stdout_sha256"], hashlib.sha256(stdout).hexdigest())
        self.assertEqual(operation["stderr_sha256"], hashlib.sha256(stderr).hexdigest())
        self.assertEqual(operation["last_message_file_status"], "missing")
        self.assertIsNone(operation["result_text"])

    def test_reported_failed_turn_is_not_marked_completed_when_process_exit_is_zero(self):
        stdout = (
            b'{"type":"turn.failed","error":{"message":"failed"}}\n'
            b'{"type":"item.completed","item":{"type":"agent_message","text":"Partial response"}}\n'
        )
        runner, _process = self.invoke(stdout=stdout, message=b"Partial response")

        with self.assertRaisesRegex(orchestrator.WorkflowError, "reported a failed turn"):
            self.run_locked(runner)

        operation = runner.state["operation_history"][-1]
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["error_category"], "CodexReportedTurnFailure")
        self.assertEqual(operation["codex_cli_turn_status"], "failed")
        self.assertIsNone(operation["result_text"])

    def test_timeout_records_partial_output_and_interruptions_leave_started_operation_for_recovery(self):
        partial_stdout = b"partial-jsonl-event"
        timeout = subprocess.TimeoutExpired("codex", 1, output=partial_stdout, stderr=b"partial-error")
        runner, _process = self.invoke(process_error=timeout)

        with self.assertRaisesRegex(orchestrator.WorkflowError, "timed out"):
            self.run_locked(runner)

        operation = runner.state["operation_history"][-1]
        self.assertEqual(operation["status"], "interrupted")
        self.assertEqual(operation["stdout_capture_status"], "partial")
        self.assertTrue(operation["stdout_truncated"])
        self.assertEqual(operation["stdout_sha256"], hashlib.sha256(partial_stdout).hexdigest())
        self.assertEqual(operation["stderr_capture_status"], "partial")

        interrupted, _process = self.invoke(process_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_locked(interrupted)
        interrupted_operation = interrupted.state["operation_history"][-1]
        self.assertEqual(interrupted_operation["status"], "started")
        self.assertEqual(interrupted_operation["launch_state"], "launching")
        self.assertEqual(interrupted_operation["stdout_capture_status"], "not_captured")
        self.assertEqual(interrupted_operation["tool_event_coverage"], "not_captured")

    def test_process_exit_before_evidence_persistence_leaves_operation_unresolved(self):
        runner, _process = self.invoke(stdout=b'{"type":"turn.completed"}\n')
        finish = runner._finish_operation

        def interrupt_before_save(operation_id, **kwargs):
            if kwargs.get("status") == "completed":
                raise KeyboardInterrupt()
            finish(operation_id, **kwargs)

        runner._finish_operation = interrupt_before_save
        with self.assertRaises(KeyboardInterrupt):
            self.run_locked(runner)

        operation = runner.state["operation_history"][-1]
        self.assertEqual(operation["status"], "started")
        self.assertEqual(operation["stdout_capture_status"], "not_captured")
        self.assertIsNone(operation["stdout_sha256"])
        self.assertIsNone(operation["result_sha256"])
        self.assertIsNone(operation["result_text"])

    def test_repeated_operations_keep_separate_evidence_and_bounded_tool_names(self):
        runner, _first_process = self.invoke(stdout=b"first stream", message=b"First result")
        self.run_locked(runner)
        second_process = self.Process(b"second stream", b"", b"Second result", 0, None)
        runner.process_runner = second_process
        runner.state["stage"] = "correction"
        with runner._workflow_lock():
            self.assertEqual(runner._run_codex("second private prompt", sandbox="workspace-write"), "Second result")

        first, second = runner.state["operation_history"]
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(first["stdout_sha256"], hashlib.sha256(b"first stream").hexdigest())
        self.assertEqual(second["stdout_sha256"], hashlib.sha256(b"second stream").hexdigest())
        self.assertEqual(first["stage"], "implementation")
        self.assertEqual(second["stage"], "correction")
        self.assertIsNone(first["result_text"])
        self.assertEqual(second["result_text"], "Second result")

        events = b"\n".join(
            json.dumps({
                "type": "item.completed",
                "item": {"type": "mcp_tool_call", "tool_name": f"tool-{index}", "status": "succeeded"},
            }).encode("utf-8")
            for index in range(orchestrator.MAX_CODEX_TOOL_NAMES + 1)
        )
        evidence = orchestrator._codex_jsonl_evidence(events)
        tool = evidence["tool_invocations"][0]
        self.assertEqual(len(tool["tool_names"]), orchestrator.MAX_CODEX_TOOL_NAMES)
        self.assertTrue(tool["tool_names_truncated"])
        self.assertEqual(tool["success_reported_events"], orchestrator.MAX_CODEX_TOOL_NAMES + 1)

    def test_langfuse_codex_spans_correlate_to_persisted_operations_without_content(self):
        first_stdout = (
            b'{"type":"item.completed","item":{"type":"mcp_tool_call",'
            b'"tool_name":"repo.search","status":"succeeded"}}\n'
            b'{"type":"turn.completed","usage":{"input_tokens":14,"output_tokens":5},'
            b'"model":"codex-reported-model"}\n'
        )
        runner, _first_process = self.invoke(stdout=first_stdout, message=b"Safe result")
        tracer = langfuse_adapter.BufferedLangfuseTracer(
            session_id="", public_key="public", secret_key="secret", base_url="https://langfuse.invalid",
        )
        runner._tracer = tracer
        session_id = safe_workflow_reference(runner.workflow_id)
        with tracer.execution(name="workflow.execution", session_id=session_id, metadata={"command": "approve"}):
            self.assertEqual(self.run_locked(runner, "first private prompt"), "Safe result")
            runner.state["stage"] = "correction"
            second_process = self.Process(b"second", b"", b"Second result", 0, None)
            runner.process_runner = second_process
            with runner._workflow_lock():
                self.assertEqual(runner._run_codex("second private prompt", sandbox="workspace-write"), "Second result")

        observations = self.export_trace(tracer)
        root, first_span, second_span = observations
        self.assertEqual(root["metadata"]["session_id"], session_id)
        self.assertEqual(first_span["name"], "provider.codex")
        self.assertEqual(first_span["as_type"], "generation")
        first_op, second_op = runner.state["operation_history"]
        first_metadata = first_span["metadata"]
        self.assertEqual(first_metadata["operation_id"], first_op["operation_id"])
        self.assertEqual(first_metadata["stage"], "implementation")
        self.assertEqual(first_metadata["status"], "completed")
        self.assertEqual(first_metadata["exit_code"], 0)
        self.assertEqual(first_metadata["input_sha256"], first_op["input_sha256"])
        self.assertEqual(first_metadata["output_sha256"], hashlib.sha256(first_stdout).hexdigest())
        self.assertEqual(first_metadata["output_bytes"], len(first_stdout))
        self.assertEqual(first_metadata["output_capture_status"], "complete")
        self.assertEqual(first_metadata["output_hash_representation"], "bytes")
        self.assertEqual(first_span["model"], "codex-reported-model")
        self.assertEqual(first_metadata["model_source"], "codex_jsonl_event")
        self.assertEqual(first_metadata["reported_model_source"], "codex_jsonl_event")
        self.assertEqual(first_metadata["tool_names"], ["repo.search"])
        self.assertEqual(first_metadata["tool_invocation_count"], 0)
        self.assertEqual(first_metadata["tool_completed_count"], 1)
        self.assertEqual(first_metadata["input_tokens"], 14)
        self.assertEqual(first_metadata["output_tokens"], 5)
        self.assertEqual(first_metadata["usage_source"], "codex_jsonl_turn_completed")
        self.assertIn("invocation_at", first_metadata)
        self.assertIn("finished_at", first_metadata)
        self.assertIn("duration_ms", first_metadata)
        self.assertIn("codex_duration_ms", first_metadata)
        self.assertNotIn("first private prompt", str(observations))
        self.assertNotIn("Safe result", str(observations))
        self.assertNotIn("arguments", str(observations))
        self.assertNotIn("start_time", first_span)
        self.assertNotIn("end_time", first_span)
        self.assertNotIn("cost_details", first_span)
        self.assertNotEqual(first_op["operation_id"], second_op["operation_id"])
        self.assertEqual(second_span["metadata"]["operation_id"], second_op["operation_id"])
        self.assertEqual(second_span["metadata"]["stage"], "correction")

    def test_langfuse_finalizes_failed_and_interrupted_codex_spans_truthfully(self):
        failed, _process = self.invoke(
            stdout=b"failed output", stderr=b"diagnostic", message=None, returncode=4,
        )
        failed_tracer = langfuse_adapter.BufferedLangfuseTracer(
            session_id="", public_key="public", secret_key="secret", base_url="https://langfuse.invalid",
        )
        failed._tracer = failed_tracer
        try:
            with failed_tracer.execution(
                name="workflow.execution", session_id="failed-session", metadata={"command": "approve"},
            ):
                with failed._workflow_lock():
                    failed._run_codex("private prompt", sandbox="workspace-write")
        except orchestrator.WorkflowError:
            pass
        else:
            self.fail("non-zero Codex status should remain a workflow failure")
        failed_observations = self.export_trace(failed_tracer)
        failed_span = failed_observations[1]["metadata"]
        self.assertEqual(failed_span["status"], "failed")
        self.assertEqual(failed_span["exit_code"], 4)
        self.assertEqual(failed_span["error_category"], "CodexExitError")

        interrupted, _process = self.invoke(process_error=KeyboardInterrupt())
        interrupted_tracer = langfuse_adapter.BufferedLangfuseTracer(
            session_id="", public_key="public", secret_key="secret", base_url="https://langfuse.invalid",
        )
        interrupted._tracer = interrupted_tracer
        try:
            with interrupted_tracer.execution(
                name="workflow.execution", session_id="interrupted-session", metadata={"command": "approve"},
            ):
                self.run_locked(interrupted)
        except KeyboardInterrupt:
            pass
        else:
            self.fail("process interruption should propagate")
        interrupted_observations = self.export_trace(interrupted_tracer)
        interrupted_span = interrupted_observations[1]["metadata"]
        self.assertEqual(interrupted_span["status"], "unknown")
        self.assertEqual(interrupted_span["error_category"], "KeyboardInterrupt")
        self.assertEqual(interrupted.state["operation_history"][-1]["status"], "started")

    def test_langfuse_export_failure_does_not_change_codex_result(self):
        runner, _process = self.invoke(stdout=b"safe stream", message=b"Success stays success")
        tracer = langfuse_adapter.BufferedLangfuseTracer(
            session_id="", public_key="public", secret_key="secret", base_url="https://langfuse.invalid",
        )
        runner._tracer = tracer
        with tracer.execution(name="workflow.execution", session_id="safe-session", metadata={"command": "approve"}):
            response = self.run_locked(runner)

        class BrokenSDK:
            @staticmethod
            def Langfuse(**_kwargs):
                raise RuntimeError("export offline")

        with patch.object(langfuse_adapter.importlib, "import_module", return_value=BrokenSDK()), \
             redirect_stderr(StringIO()):
            tracer.flush()
        self.assertEqual(response, "Success stays success")
        self.assertEqual(runner.state["operation_history"][-1]["status"], "completed")

    def test_legacy_operation_summary_remains_readable_without_new_evidence_fields(self):
        output = StringIO()
        with redirect_stdout(output):
            orchestrator._print_summary({
                "workflow_id": "legacy-workflow",
                "operation_history": [{
                    "operation_id": "legacy-operation", "operation_type": "codex",
                    "stage": "implementation", "status": "completed",
                }],
            })

        summary = json.loads(output.getvalue())
        self.assertEqual(summary["recent_operations"][0]["operation_id"], "legacy-operation")
        self.assertIsNone(summary["recent_operations"][0]["stdout_sha256"])


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

    def interrupted_implementation(self, workflow_id: str, *, preexisting: bool = False, mode: str = "during"):
        if preexisting:
            (self.target / "README.md").write_text("user change before workflow\n", encoding="utf-8")
        codex = FakeCodex(
            "# Proposal\nImplement recovery safely.\n", self.refined(), "Implementation summary.\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=lambda *_args: claude_reviewer.ReviewResult(
                "Decision: APPROVED\nNo acceptance-blocking findings.", 0.05,
            ),
        )

        def interrupt_implementation(index, _command, _kwargs):
            if index != 2:
                return
            changed = self.target / "src" / "recovery.py"
            changed.parent.mkdir(parents=True, exist_ok=True)
            changed.write_text("def recover():\n    return 'partial'\n", encoding="utf-8")
            if mode == "during":
                raise KeyboardInterrupt()

        codex.on_call = interrupt_implementation
        runner = orchestrator.Orchestrator(
            self.target, task="Implement crash recovery", budget_usd=1.0, workflow_id=workflow_id,
            process_runner=codex, claude_runner=claude,
        )
        runner.start()
        if mode == "during":
            with self.assertRaises(KeyboardInterrupt):
                runner.approve()
        elif mode == "after-process":
            finish_operation = runner._finish_operation

            def interrupt_before_result_save(operation_id, **kwargs):
                if kwargs.get("status") == "completed":
                    raise KeyboardInterrupt()
                finish_operation(operation_id, **kwargs)

            runner._finish_operation = interrupt_before_result_save
            with self.assertRaises(KeyboardInterrupt):
                runner.approve()
        elif mode == "after-result-persisted":
            finish_operation = runner._finish_operation

            def interrupt_after_result_save(operation_id, **kwargs):
                finish_operation(operation_id, **kwargs)
                if kwargs.get("status") == "completed":
                    raise KeyboardInterrupt()

            runner._finish_operation = interrupt_after_result_save
            with self.assertRaises(KeyboardInterrupt):
                runner.approve()
        else:
            raise AssertionError(f"Unknown interruption mode: {mode}")
        return runner, codex, claude

    def test_crash_before_operation_start_can_resume_normally(self):
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="crash-before-operation",
        )
        with patch.object(runner, "_run_codex", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                runner.start()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["stage"], "planning")
        self.assertEqual(state["operation_history"], [])

        codex = FakeCodex("# Proposal\nPlan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05))
        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="crash-before-operation", process_runner=codex, claude_runner=claude,
        ).resume()
        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertEqual(len(codex.commands), 2)

    def test_crash_after_started_before_subprocess_launch_is_safe_to_resume(self):
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="crash-before-launch",
            process_runner=FakeCodex("# Proposal\nPlan.\n", self.refined()),
        )
        with patch.object(runner, "_run_process_with_pid", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                runner.start()
        persisted = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["operation_history"][-1]["status"], "started")
        self.assertEqual(persisted["operation_history"][-1]["launch_state"], "not_started")
        self.assertEqual(persisted["active_operation_id"], persisted["operation_history"][-1]["operation_id"])

        replacement = FakeCodex("# Proposal\nPlan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05))
        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="crash-before-launch", process_runner=replacement, claude_runner=claude,
        ).resume()
        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertEqual(len(replacement.commands), 2)
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertEqual(state["operation_history"][0]["status"], "failed")
        self.assertEqual(state["operation_history"][0]["launch_state"], "not_started")

    def test_crash_after_launch_attempt_before_child_identity_requires_inspection(self):
        calls = 0

        def interrupted_runner(_command, **_kwargs):
            nonlocal calls
            calls += 1
            raise KeyboardInterrupt()

        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="crash-during-launch",
            process_runner=interrupted_runner,
        )
        with self.assertRaises(KeyboardInterrupt):
            runner.start()
        persisted = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["operation_history"][-1]["status"], "started")
        self.assertEqual(persisted["operation_history"][-1]["launch_state"], "launching")

        replacement = FakeCodex("# Proposal\nPlan.\n")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "requires recovery"):
            orchestrator.Orchestrator(
                self.target, workflow_id="crash-during-launch", process_runner=replacement,
            ).resume()
        self.assertEqual(calls, 1)
        self.assertEqual(replacement.commands, [])
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "recovery_required")
        self.assertEqual(state["recovery"]["operation_status"], "interrupted")

    def test_interrupted_codex_exposes_partial_target_changes_without_rerunning(self):
        runner, original_codex, _claude = self.interrupted_implementation("crash-during-codex")
        replacement = FakeCodex("must not run")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "requires recovery"):
            orchestrator.Orchestrator(
                self.target, workflow_id="crash-during-codex", process_runner=replacement,
            ).resume()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "recovery_required")
        self.assertEqual(state["operation_history"][-1]["status"], "interrupted")
        self.assertEqual(state["recovery"]["target_changes"], [
            {"path": "src/recovery.py", "change": "untracked", "preexisting": "no"},
        ])
        self.assertEqual(len(original_codex.commands), 3)
        self.assertEqual(replacement.commands, [])

    def test_completed_codex_process_without_saved_result_is_not_repeated(self):
        runner, _original_codex, _claude = self.interrupted_implementation(
            "crash-after-codex-exit", mode="after-process",
        )
        persisted = json.loads(runner.state_path.read_text(encoding="utf-8"))
        operation = persisted["operation_history"][-1]
        self.assertEqual(operation["status"], "started")
        self.assertIsNone(operation["result_text"])
        replacement = FakeCodex("must not run")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "requires recovery"):
            orchestrator.Orchestrator(
                self.target, workflow_id="crash-after-codex-exit", process_runner=replacement,
            ).resume()
        self.assertEqual(replacement.commands, [])

    def test_completed_codex_result_is_reused_after_parent_stops_before_stage_advance(self):
        runner, original_codex, claude = self.interrupted_implementation(
            "reuse-persisted-codex-result", mode="after-result-persisted",
        )
        persisted = json.loads(runner.state_path.read_text(encoding="utf-8"))
        operation = persisted["operation_history"][-1]
        self.assertEqual(operation["status"], "completed")
        self.assertEqual(operation["result_text"], "Implementation summary.")

        replacement = FakeCodex()
        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="reuse-persisted-codex-result", process_runner=replacement,
            claude_runner=claude,
        ).resume()
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(replacement.commands, [])
        self.assertEqual(len(original_codex.commands), 3)

    def test_interrupted_claude_call_marks_cost_unknown_and_is_not_repeated(self):
        calls = 0

        def interrupted_claude(_prompt, *, budget_usd, executable):
            nonlocal calls
            calls += 1
            raise KeyboardInterrupt()

        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="crash-during-claude",
            process_runner=FakeCodex("# Proposal\nPlan.\n"), claude_runner=interrupted_claude,
        )
        with self.assertRaises(KeyboardInterrupt):
            runner.start()
        replacement = FakeClaude()
        with self.assertRaisesRegex(orchestrator.WorkflowError, "no recorded cost"):
            orchestrator.Orchestrator(
                self.target, workflow_id="crash-during-claude", process_runner=FakeCodex(),
                claude_runner=replacement,
            ).resume()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["authorization"]["claude_aily"]["budget_unknown"])
        self.assertEqual(state["operation_history"][-1]["status"], "interrupted")
        self.assertEqual(calls, 1)
        self.assertEqual(replacement.prompts, [])

    def test_crash_before_claude_launch_retries_without_unknown_spend(self):
        claude = FakeClaude(claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05))
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="crash-before-claude-launch",
            process_runner=FakeCodex("# Proposal\nPlan.\n"), claude_runner=claude,
        )
        operation_launching = runner._operation_launching

        def interrupt_before_claude_launch(operation_id):
            if runner._operation(operation_id).get("operation_type") == "claude":
                raise KeyboardInterrupt()
            operation_launching(operation_id)

        with patch.object(runner, "_operation_launching", side_effect=interrupt_before_claude_launch):
            with self.assertRaises(KeyboardInterrupt):
                runner.start()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["operation_history"][-1]["launch_state"], "not_started")
        self.assertFalse(state["authorization"]["claude_aily"]["budget_unknown"])

        resumed_claude = FakeClaude(claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05))
        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="crash-before-claude-launch", process_runner=FakeCodex(self.refined()),
            claude_runner=resumed_claude,
        ).resume()
        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertEqual(len(resumed_claude.prompts), 1)
        self.assertFalse(resumed["authorization"]["claude_aily"]["budget_unknown"])

    def test_interrupted_claude_retry_requires_verified_cost_and_stays_within_budget(self):
        calls = 0

        def interrupted_claude(_prompt, *, budget_usd, executable):
            nonlocal calls
            calls += 1
            raise KeyboardInterrupt()

        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="verified-claude-recovery",
            process_runner=FakeCodex("# Proposal\nPlan.\n"), claude_runner=interrupted_claude,
        )
        with self.assertRaises(KeyboardInterrupt):
            runner.start()

        def resume_with(**arguments):
            return orchestrator.Orchestrator(
                self.target, workflow_id="verified-claude-recovery", process_runner=FakeCodex(self.refined()),
                claude_runner=FakeClaude(claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05)),
            ).resume(recovery_action="retry", **arguments)

        with self.assertRaisesRegex(orchestrator.WorkflowError, "verify the final charge"):
            resume_with(verified_claude_cost_usd=0.20)
        with self.assertRaisesRegex(orchestrator.WorkflowError, "exceeds the active call cap"):
            resume_with(verified_claude_cost_usd=1.01, confirm_provider_inactive=True)
        blocked = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertTrue(blocked["authorization"]["claude_aily"]["budget_unknown"])
        self.assertEqual(calls, 1)

        resumed = resume_with(
            verified_claude_cost_usd=0.20, confirm_provider_inactive=True,
        )
        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertEqual(resumed["authorization"]["claude_aily"]["spent_usd"], 0.25)
        self.assertFalse(resumed["authorization"]["claude_aily"]["budget_unknown"])
        self.assertEqual(calls, 1)

    def test_resume_rejects_while_another_process_holds_workflow_lock(self):
        runner, _codex, _claude = self.interrupted_implementation("lock-contention-recovery")
        replacement = orchestrator.Orchestrator(self.target, workflow_id="lock-contention-recovery")
        with runner._workflow_lock():
            with self.assertRaisesRegex(orchestrator.WorkflowError, "already being modified"):
                replacement.resume()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "running")

    def test_running_workflow_without_active_operation_resumes_from_saved_stage(self):
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="stale-running-no-operation",
        )
        with patch.object(runner, "_run_codex", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                runner.start()
        codex = FakeCodex("# Proposal\nPlan.\n", self.refined())
        claude = FakeClaude(claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05))
        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="stale-running-no-operation", process_runner=codex, claude_runner=claude,
        ).resume()
        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertEqual(len(codex.commands), 2)

    def test_interrupted_codex_recovery_requires_explicit_process_and_target_confirmation(self):
        runner, _codex, claude = self.interrupted_implementation("codex-recovery-confirmations")
        recovering = orchestrator.Orchestrator(
            self.target, workflow_id="codex-recovery-confirmations", process_runner=FakeCodex("retry summary"),
            claude_runner=claude,
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "confirm-process-inactive"):
            recovering.resume(recovery_action="retry")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "confirm-target-reviewed"):
            recovering.resume(recovery_action="retry", confirm_process_inactive=True)
        retry_codex = FakeCodex("Retry completed after inspection.")
        resumed = orchestrator.Orchestrator(
            self.target, workflow_id="codex-recovery-confirmations", process_runner=retry_codex,
            claude_runner=claude,
        ).resume(
            recovery_action="retry", confirm_process_inactive=True, confirm_target_reviewed=True,
        )
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(len(retry_codex.commands), 1)
        implementation_operations = [
            operation for operation in resumed["operation_history"]
            if operation.get("operation_type") == "codex" and operation.get("stage") == "implementation"
        ]
        self.assertEqual(len(implementation_operations), 2, implementation_operations)
        retried_operation = implementation_operations[-1]
        self.assertEqual(retried_operation["status"], "completed")
        self.assertEqual(retried_operation["attempt"], 2)
        self.assertEqual(
            implementation_operations[0]["resolution"], "user_approved_retry_after_target_inspection",
        )

    def test_accepting_partial_implementation_skips_codex_and_runs_independent_review(self):
        runner, original_codex, claude = self.interrupted_implementation("accept-partial-implementation")
        replacement = FakeCodex()
        completed = orchestrator.Orchestrator(
            self.target, workflow_id="accept-partial-implementation", process_runner=replacement,
            claude_runner=claude,
        ).resume(recovery_action="accept-implementation", confirm_target_reviewed=True)
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["stage"], "complete")
        self.assertEqual(replacement.commands, [])
        self.assertEqual(len(original_codex.commands), 3)
        accepted = next(
            operation for operation in completed["operation_history"]
            if operation.get("resolution") == "human_accepted_current_target_for_independent_review"
        )
        self.assertEqual(accepted["operation_type"], "codex")

    def test_accepting_partial_implementation_rejects_preexisting_user_changes(self):
        runner, _codex, claude = self.interrupted_implementation(
            "preserve-preexisting-changes", preexisting=True,
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "already changed before this workflow"):
            orchestrator.Orchestrator(
                self.target, workflow_id="preserve-preexisting-changes", process_runner=FakeCodex(),
                claude_runner=claude,
            ).resume(recovery_action="accept-implementation", confirm_target_reviewed=True)
        self.assertEqual((self.target / "README.md").read_text(encoding="utf-8"), "user change before workflow\n")
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "recovery_required")

    def test_repeated_recovery_attempts_remain_blocked_without_repeating_operations(self):
        runner, original_codex, _claude = self.interrupted_implementation("repeat-recovery-safely")
        replacement = FakeCodex("must not run")
        recovering = orchestrator.Orchestrator(
            self.target, workflow_id="repeat-recovery-safely", process_runner=replacement,
        )
        for _ in range(2):
            with self.assertRaisesRegex(orchestrator.WorkflowError, "requires recovery"):
                recovering.resume()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "recovery_required")
        self.assertEqual(len(state["operation_history"]), 4)
        self.assertEqual(len(original_codex.commands), 3)
        self.assertEqual(replacement.commands, [])

    def test_existing_pid_is_reported_as_unverified_not_proof_of_process_identity(self):
        runner, _codex, _claude = self.interrupted_implementation("pid-reuse-is-ambiguous")
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        state["operation_history"][-1]["process_id"] = os.getpid()
        runner.state_path.write_text(json.dumps(state), encoding="utf-8")
        recovering = orchestrator.Orchestrator(self.target, workflow_id="pid-reuse-is-ambiguous")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "requires recovery"):
            recovering.resume()
        self.assertEqual(recovering.status()["recovery"]["process_presence"], "present_unverified")

    def test_legacy_running_state_without_operation_records_remains_blocked(self):
        runner = orchestrator.Orchestrator(
            self.target, task="Add a feature", budget_usd=1.0, workflow_id="legacy-running-workflow",
        )
        with patch.object(runner, "_run_codex", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                runner.start()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        state.pop("operation_tracking_version")
        state.pop("operation_history")
        state.pop("active_operation_id")
        runner.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(orchestrator.WorkflowError, "requires recovery"):
            orchestrator.Orchestrator(
                self.target, workflow_id="legacy-running-workflow",
            ).resume()
        persisted = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "recovery_required")
        self.assertIsNone(persisted["recovery"]["operation_id"])

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

    def test_cli_dispatches_each_workflow_command_through_runner_methods(self):
        commands = (
            ("approve", "approve", []),
            ("resume", "resume", []),
            ("status", "status", []),
            ("revise", "revise", ["--feedback", "Clarify the plan."]),
            ("reject", "reject", []),
        )
        for command, method, extra in commands:
            with self.subTest(command=command):
                state = {
                    "workflow_id": "cli-dispatch", "target": str(self.target),
                    "status": "rejected" if command == "reject" else "complete",
                    "stage": command, "authorization": {}, "plan_reviews": [],
                }
                output = StringIO()
                with patch.object(orchestrator, "Orchestrator") as factory, redirect_stdout(output):
                    getattr(factory.return_value, method).return_value = state
                    result = orchestrator.main([
                        command, "--target", str(self.target), "--workflow-id", "cli-dispatch", *extra,
                    ])
                self.assertEqual(result, 0)
                getattr(factory.return_value, method).assert_called_once()
                summary = json.loads(output.getvalue())
                self.assertEqual(summary["workflow_id"], "cli-dispatch")
                self.assertIn("claude_contexts", summary)

    def test_cli_passes_explicit_recovery_confirmations_to_resume(self):
        state = {
            "workflow_id": "cli-recovery", "target": str(self.target),
            "status": "recovery_required", "stage": "implementation", "authorization": {},
            "plan_reviews": [], "operation_history": [],
        }
        output = StringIO()
        with patch.object(orchestrator, "Orchestrator") as factory, redirect_stdout(output):
            factory.return_value.resume.return_value = state
            result = orchestrator.main([
                "resume", "--target", str(self.target), "--workflow-id", "cli-recovery",
                "--recovery-action", "retry", "--verified-claude-cost-usd", "0.20",
                "--confirm-provider-inactive", "--confirm-process-inactive", "--confirm-target-reviewed",
            ])
        self.assertEqual(result, 1)
        factory.return_value.resume.assert_called_once_with(
            recovery_action="retry", verified_claude_cost_usd=0.20,
            confirm_provider_inactive=True, confirm_process_inactive=True, confirm_target_reviewed=True,
        )

    def test_cli_reads_task_and_feedback_files_and_reports_missing_files(self):
        task_file = self.root / "assignment.md"
        task_file.write_text("Implement from this task file.\n", encoding="utf-8")
        feedback_file = self.root / "feedback.md"
        feedback_file.write_text("Clarify the acceptance criteria.\n", encoding="utf-8")
        state = {
            "workflow_id": "cli-files", "target": str(self.target),
            "status": "awaiting_approval", "authorization": {}, "plan_reviews": [],
        }
        output = StringIO()
        with patch.object(orchestrator, "Orchestrator") as factory, redirect_stdout(output):
            factory.return_value.budget_usd = 2.0
            factory.return_value.max_code_review_calls = orchestrator.DEFAULT_MAX_CODE_REVIEW_CALLS
            factory.return_value.start.return_value = state
            self.assertEqual(orchestrator.main([
                "start", "--target", str(self.target), "--task-file", str(task_file),
            ]), 0)
        self.assertEqual(factory.call_args.kwargs["task"], "Implement from this task file.\n")

        with patch.object(orchestrator, "Orchestrator") as factory, redirect_stdout(StringIO()):
            factory.return_value.revise.return_value = state
            self.assertEqual(orchestrator.main([
                "revise", "--target", str(self.target), "--workflow-id", "cli-files",
                "--feedback-file", str(feedback_file),
            ]), 0)
        factory.return_value.revise.assert_called_once_with("Clarify the acceptance criteria.\n")

        errors = StringIO()
        with patch.object(orchestrator, "Orchestrator"), redirect_stderr(errors):
            result = orchestrator.main([
                "revise", "--target", str(self.target), "--workflow-id", "cli-files",
                "--feedback-file", str(self.root / "missing-feedback.md"),
            ])
        self.assertEqual(result, 2)
        self.assertIn("Could not read feedback file", errors.getvalue())

        errors = StringIO()
        with patch.object(orchestrator, "Orchestrator"), redirect_stderr(errors):
            result = orchestrator.main([
                "start", "--target", str(self.target), "--task-file", str(self.root / "missing-task.md"),
            ])
        self.assertEqual(result, 2)
        self.assertIn("Could not read task file", errors.getvalue())

    def test_cli_argument_requirements_and_direct_entrypoint_help(self):
        errors = StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as missing_task:
            orchestrator.main(["start", "--target", str(self.target)])
        self.assertEqual(missing_task.exception.code, 2)

        errors = StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as conflicting_task:
            orchestrator.main([
                "start", "--target", str(self.target), "--task", "inline",
                "--task-file", str(self.root / "assignment.md"),
            ])
        self.assertEqual(conflicting_task.exception.code, 2)

        errors = StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as missing_feedback:
            orchestrator.main(["revise", "--target", str(self.target), "--workflow-id", "cli-revise"])
        self.assertEqual(missing_feedback.exception.code, 2)

        errors = StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as conflicting_feedback:
            orchestrator.main([
                "revise", "--target", str(self.target), "--workflow-id", "cli-revise",
                "--feedback", "inline", "--feedback-file", str(self.root / "feedback.md"),
            ])
        self.assertEqual(conflicting_feedback.exception.code, 2)

        errors = StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as invalid_limit:
            orchestrator.main([
                "start", "--target", str(self.target), "--task", "inline",
                "--max-code-review-calls", "0",
            ])
        self.assertEqual(invalid_limit.exception.code, 2)

        script = ROOT / "harness" / "orchestrator.py"
        help_result = subprocess.run(
            [sys.executable, str(script), "--help"], cwd=ROOT,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for command in ("start", "approve", "resume", "status", "recover-review", "revise", "reject"):
            self.assertIn(command, help_result.stdout)
            command_help = subprocess.run(
                [sys.executable, str(script), command, "--help"], cwd=ROOT,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(command_help.returncode, 0, command_help.stderr)

    def test_cli_redacts_errors_and_preserves_error_exit_code(self):
        secret = "api_key=very-secret-value"
        errors = StringIO()
        with patch.object(orchestrator, "Orchestrator") as factory, redirect_stderr(errors):
            factory.return_value.budget_usd = 2.0
            factory.return_value.max_code_review_calls = orchestrator.DEFAULT_MAX_CODE_REVIEW_CALLS
            factory.return_value.start.side_effect = orchestrator.WorkflowError(f"failed with {secret}")
            result = orchestrator.main([
                "start", "--target", str(self.target), "--task", "Add a feature",
            ])
        self.assertEqual(result, 2)
        self.assertNotIn(secret, errors.getvalue())
        self.assertIn("[REDACTED]", errors.getvalue())

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
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "24000"}):
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
        self.assertTrue(all(item["characters"] <= 24000 for item in code_calls))
        self.assertTrue(all(item["payload_limit_characters"] == 24000 for item in code_calls))
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
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "14500"}):
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
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "13500"}):
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
        with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "15000"}):
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

    def test_legacy_authorization_can_call_claude_without_skill_bodies(self):
        codex = FakeCodex("# Proposal\nAdd the requested behavior.\n", self.refined())
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nThe proposal is adequate.", 0.05),
            claude_reviewer.ReviewResult("Decision: APPROVED\nNo material plan concerns.", 0.05),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Refactor a workflow module and add tests.", budget_usd=1.0,
            workflow_id="legacy-skill-authorization", process_runner=codex, claude_runner=claude,
        )
        runner.start()

        with runner._workflow_lock():
            authorization = runner.state["authorization"]
            authorization["claude_aily"]["permitted_data"] = (
                orchestrator._authorization.LEGACY_CLAUDE_DATA_SCOPE.copy()
            )
            authorization["scope_sha256"] = orchestrator.authorization_scope_digest(authorization)
            runner._save()
            header, sections, metadata = runner._plan_review_prompt(2)
            report, manifest = runner._call_claude(header, "plan_review_2", sections, metadata=metadata)

        self.assertIn("No material plan concerns.", report)
        self.assertTrue(manifest["complete"])
        self.assertNotIn("Before changing a coordinator, orchestrator, or large module", claude.prompts[-1])

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
        self.assertIn("mcp_servers={}", command)
        self.assertIn("--sandbox", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--model", command)

        configured = orchestrator.build_codex_command(
            "codex", self.target, "workspace-write", self.root / "last-message.md", "codex-model-test",
        )
        model_index = configured.index("--model")
        self.assertEqual(configured[model_index + 1], "codex-model-test")

        documentation_reader = orchestrator.build_codex_command(
            "codex", self.target, "read-only", self.root / "last-message.md",
            read_only_authorized_urls=["https://docs.example.com/api"],
        )
        self.assertEqual(
            documentation_reader[:4],
            ["codex", "--ask-for-approval", "never", "exec"],
        )
        self.assertIn("--ignore-user-config", documentation_reader)
        self.assertEqual(documentation_reader[documentation_reader.index("--sandbox") + 1], "read-only")
        config = next(item for item in documentation_reader if item.startswith("mcp_servers={"))
        grant_hash = read_documentation_url.authorized_url_sha256("https://docs.example.com/api")
        docs_args = [
            str(Path(orchestrator.__file__).resolve().parent / "tools" / "read_documentation_url.py"),
            "--authorized-url-sha256", grant_hash,
        ]
        self.assertEqual(
            config,
            "mcp_servers={ documentation_reader = { "
            f"command = {json.dumps(sys.executable)}, args = {json.dumps(docs_args)}"
            " } }",
        )
        self.assertTrue(docs_args[0].endswith("read_documentation_url.py"))

        write_docs = orchestrator.build_codex_command(
            "codex", self.target, "workspace-write", self.root / "last-message.md",
            read_only_authorized_urls=["https://docs.example.com/api"],
        )
        write_config = next(item for item in write_docs if item.startswith("mcp_servers="))
        self.assertEqual(json.loads(write_config.removeprefix("mcp_servers=")), {})

    def test_documentation_reader_is_allowlisted_and_only_used_for_url_planning(self):
        task = "Read https://docs.example.com/api"
        codex = FakeCodex("Planning result", "Implementation result")
        runner = orchestrator.Orchestrator(
            self.target, task=task, budget_usd=1.0, workflow_id="planning-read-only-docs",
            process_runner=codex, claude_runner=FakeClaude(),
        )
        runner.state = {
            "authorization": {
                "target": str(self.target), "workspace_operations": [], "restricted_operations": [],
            },
            "stage": "planning",
            "task": task,
        }

        runner.workflow_dir.mkdir(parents=True)
        with runner._workflow_lock():
            runner._run_codex_impl("Read the API documentation.", sandbox="read-only")
            runner.state["stage"] = "implementation"
            runner._run_codex_impl("Implement the approved change.", sandbox="workspace-write")

        planning_command, implementation_command = codex.commands
        planning_config = next(item for item in planning_command if item.startswith("mcp_servers={"))
        grant_hash = read_documentation_url.authorized_url_sha256("https://docs.example.com/api")
        self.assertIn(f'"--authorized-url-sha256", "{grant_hash}"', planning_config)
        self.assertIn(orchestrator.READ_ONLY_WEB_ACCESS_INSTRUCTION, codex.prompts[0])
        self.assertEqual(planning_command[planning_command.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", planning_command)
        implementation_config = next(item for item in implementation_command if item.startswith("mcp_servers="))
        self.assertEqual(json.loads(implementation_config.removeprefix("mcp_servers=")), {})
        self.assertNotIn("--ignore-user-config", implementation_command)

    def test_project_mcp_servers_fail_closed_before_codex_runs(self):
        task = "Read https://docs.example.com/api"
        codex = FakeCodex("Planning result")
        runner = orchestrator.Orchestrator(
            self.target, task=task, budget_usd=1.0, workflow_id="planning-mcp-project-config",
            process_runner=codex, claude_runner=FakeClaude(),
        )
        runner.state = {
            "authorization": {
                "target": str(self.target), "workspace_operations": [], "restricted_operations": [],
            },
            "stage": "planning",
            "task": task,
        }
        config_dir = self.target / ".codex"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            '[mcp_servers.unapproved]\ncommand = "unapproved"\n', encoding="utf-8",
        )

        with self.assertRaisesRegex(orchestrator.WorkflowError, "declares MCP servers"):
            runner._run_codex_impl("Read the API documentation.", sandbox="read-only")
        self.assertEqual(codex.commands, [])

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
            with patch.dict(os.environ, {claude_reviewer.MAX_REVIEW_PAYLOAD_ENV: "125000"}):
                _report, manifest = runner._call_claude(
                    header, "plan_review_2", sections, metadata=metadata,
                )
        submitted = claude.prompts[-1]
        self.assertGreater(len(submitted), 100_000)
        self.assertEqual(manifest["characters"], len(submitted))
        self.assertEqual(manifest["utf8_bytes"], len(submitted.encode("utf-8")))
        self.assertGreater(manifest["utf8_bytes"], manifest["characters"])
        self.assertEqual(manifest["payload_limit_characters"], 125_000)
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
        self.assertIn("Establish the API contract first", claude.prompts[0])
        self.assertIn("Use snake_case field names.", claude.prompts[0])
        self.assertTrue({"api-engineer", "project-api"}.issubset(
            {item["name"] for item in state["project_guidance"]["skills"]}
        ))

    def test_skill_guidance_reaches_each_provider_stage_and_persists_for_status(self):
        (self.target / "AGENTS.md").write_text(
            "Repository instruction sentinel: preserve workflow state ordering.\n", encoding="utf-8",
        )
        git(self.target, "add", "AGENTS.md")
        git(self.target, "commit", "-q", "-m", "add repository instructions")
        codex = FakeCodex(
            "# Proposal\nRefactor the workflow coordinator into cohesive modules.\n",
            self.refined(),
            "Implemented the parser behavior.\n",
            "Fixed the reported finding and reran the focused tests.\n",
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=lambda *_args: claude_reviewer.ReviewResult(
                "Decision: CHANGES_REQUESTED\nFinding: Handle empty input in the parser.", 0.05,
            ),
        )

        def implement(index, _command, _kwargs):
            if index == 2:
                path = self.target / "src" / "parser.py"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def parse(value):\n    return value\n", encoding="utf-8")

        codex.on_call = implement
        runner = orchestrator.Orchestrator(
            self.target, task="Refactor the workflow orchestrator and add regression tests.",
            budget_usd=1.0, workflow_id="skill-propagation", process_runner=codex, claude_runner=claude,
        )
        started = runner.start()
        completed = orchestrator.Orchestrator(
            self.target, workflow_id="skill-propagation", process_runner=codex, claude_runner=claude,
        ).approve()

        codex_markers = (
            "Help choose an architecture that fits the actual problem.",
            "Make the smallest cohesive change that satisfies the assignment",
            "Test the behavior at risk; passing tests and line coverage alone do not establish correctness.",
        )
        for index in (0, 1, 2, 3):
            for marker in codex_markers:
                self.assertIn(marker, codex.prompts[index], (index, marker))
        self.assertIn("Compare the actual change with the assignment and existing contracts.", codex.prompts[3])
        self.assertIn("Repository instruction sentinel", codex.prompts[0])
        self.assertIn("Repository instruction sentinel", codex.prompts[3])
        self.assertIn("Engineering Judgment & Proportional Design", codex.prompts[0])
        self.assertIn("Engineering Judgment & Proportional Design", codex.prompts[3])
        self.assertIn("Treat its findings as the complete correction assignment", codex.prompts[3])
        self.assertIn("Modify only files necessary", codex.prompts[3])
        self.assertIn("Do not perform unrelated refactoring", codex.prompts[3])

        self.assertIn("Help choose an architecture that fits the actual problem.", claude.prompts[0])
        self.assertIn("Make the smallest cohesive change that satisfies the assignment", claude.prompts[0])
        self.assertIn("Test the behavior at risk; passing tests and line coverage alone do not establish correctness.", claude.prompts[0])
        self.assertIn("Compare the actual change with the assignment and existing contracts.", claude.prompts[1])
        self.assertIn("Make the smallest cohesive change that satisfies the assignment", claude.prompts[1])
        self.assertIn("Test the behavior at risk; passing tests and line coverage alone do not establish correctness.", claude.prompts[1])

        self.assertEqual(completed["status"], "complete")
        saved_guidance = started["project_guidance"]
        self.assertTrue(any(skill["source"] == "harness" for skill in saved_guidance["skills"]))
        reloaded = orchestrator.Orchestrator(
            self.target, workflow_id="skill-propagation", process_runner=codex, claude_runner=claude,
        ).status()
        self.assertEqual(reloaded["project_guidance"], saved_guidance)

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
        self.assertFalse(any(skill["source"] == "project" for skill in state["project_guidance"]["skills"]))
        self.assertIn("Make the smallest cohesive change that satisfies the assignment", codex.prompts[0])
        self.assertIn("Test the behavior at risk; passing tests and line coverage alone do not establish correctness.", claude.prompts[0])
        self.assertNotIn("Help choose an architecture that fits the actual problem.", codex.prompts[0])

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
        self.assertEqual(failed["status"], "recovery_required")
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


class WorkflowExecutionFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-fingerprint-tests-")
        self.target = make_target(Path(self.temp.name) / "target")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def refined():
        return (
            "# Refined proposal\n\n## Scope\nImplement the requested behavior.\n\n"
            "## Claude finding dispositions\n- No actionable findings.\n\nSecond plan review needed: no\n"
        )

    def make_runner(self, workflow_id: str, *, on_implementation=None):
        codex = FakeCodex(
            "# Proposal\nImplement the requested behavior.\n", self.refined(), "Implementation summary.\n",
        )
        codex.on_call = lambda index, command, kwargs: (
            on_implementation(index, command, kwargs) if on_implementation is not None and index == 2 else None
        )
        claude = FakeClaude(
            claude_reviewer.ReviewResult("Decision: APPROVED\nPlan is adequate.", 0.05),
            response_factory=lambda _prompt, _budget, _executable: claude_reviewer.ReviewResult(
                "Decision: APPROVED\nNo acceptance-blocking findings.", 0.05,
            ),
        )
        runner = orchestrator.Orchestrator(
            self.target, task="Implement the requested behavior", budget_usd=1.0,
            workflow_id=workflow_id, process_runner=codex, claude_runner=claude,
        )
        return runner, codex, claude

    def test_initial_harness_and_target_fingerprints_are_persisted_before_artifacts(self):
        (self.target / "README.md").write_text("pre-existing user change\n", encoding="utf-8")
        expected_target = workspace_ops.repository_fingerprint(self.target)
        runner, codex, _claude = self.make_runner("initial-fingerprints")
        observed_before_provider = []
        codex.on_call = lambda index, _command, _kwargs: observed_before_provider.append(
            json.loads(runner.state_path.read_text(encoding="utf-8"))["execution_fingerprint"]["initial"]
        ) if index == 0 else None

        state = runner.start()

        initial = state["execution_fingerprint"]["initial"]
        self.assertEqual(initial["target"]["status"], "captured")
        self.assertEqual(initial["target"]["working_tree_sha256"], expected_target["working_tree_sha256"])
        self.assertFalse(initial["target"]["working_tree_clean"])
        self.assertEqual(initial["harness"]["status"], "captured")
        self.assertEqual(
            initial["harness"]["repository_root"], str(Path(orchestrator.__file__).resolve().parents[1]),
        )
        persisted = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["execution_fingerprint"]["initial"], initial)
        self.assertEqual(observed_before_provider[0], initial)

    def test_final_fingerprint_and_summary_keep_preexisting_changes_unattributed(self):
        (self.target / "README.md").write_text("pre-existing user change\n", encoding="utf-8")

        def implement(_index, _command, _kwargs):
            generated = self.target / "src" / "generated.py"
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text("value = 42\n", encoding="utf-8")

        runner, _codex, _claude = self.make_runner("final-fingerprints", on_implementation=implement)
        runner.start()
        state = orchestrator.Orchestrator(
            self.target, workflow_id="final-fingerprints", process_runner=runner.process_runner,
            claude_runner=runner.claude_runner,
        ).approve()

        final = state["execution_fingerprint"]["final"]
        self.assertEqual(state["status"], "complete")
        self.assertEqual(final["target"]["status"], "captured")
        self.assertEqual(final["target"]["commit_sha"], git(self.target, "rev-parse", "HEAD"))
        summary = final["change_summary"]
        self.assertEqual(summary["status"], "captured")
        self.assertEqual(summary["counts_by_attribution"]["preexisting_or_overlapping"], 1)
        self.assertEqual(summary["counts_by_attribution"]["changed_after_initial_capture"], 1)
        self.assertIn("README.md", [
            item["path"] for item in summary["examples_by_attribution"]["preexisting_or_overlapping"]
        ])
        self.assertIn("src/generated.py", [
            item["path"] for item in summary["examples_by_attribution"]["changed_after_initial_capture"]
        ])
        self.assertIn("concurrent edits cannot be excluded", summary["attribution_limit"])
        delivery = (runner.workflow_dir / "delivery.md").read_text(encoding="utf-8")
        self.assertIn("Initial harness: commit", delivery)
        self.assertIn("Initial target: commit", delivery)
        self.assertIn("Final target: commit", delivery)
        self.assertIn("preexisting or overlapping", delivery)
        self.assertIn("concurrent edits cannot be excluded", delivery)

    def test_final_snapshot_does_not_claim_transient_changes_as_operation_history(self):
        runner, _codex, _claude = self.make_runner("transient-fingerprint-change")
        started = runner.start()
        transient = self.target / "created-and-removed.py"
        transient.write_text("temporary\n", encoding="utf-8")
        transient.unlink()

        rejected = runner.reject("Stop after the snapshot check")

        initial_target = started["execution_fingerprint"]["initial"]["target"]
        final = rejected["execution_fingerprint"]["final"]
        self.assertEqual(final["target"]["working_tree_sha256"], initial_target["working_tree_sha256"])
        self.assertTrue(all(value == 0 for value in final["change_summary"]["counts_by_change"].values()))
        self.assertIn("Transient changes absent from the final snapshot are not recorded", final["change_summary"]["attribution_limit"])

    def test_resume_preserves_initial_fingerprints_without_recalculating_them(self):
        runner, _codex, _claude = self.make_runner("fingerprint-resume")
        initial_state = runner.start()
        original_initial = copy.deepcopy(initial_state["execution_fingerprint"]["initial"])
        (self.target / "README.md").write_text("changed after workflow start\n", encoding="utf-8")

        resumed = orchestrator.Orchestrator(self.target, workflow_id="fingerprint-resume").resume()

        self.assertEqual(resumed["execution_fingerprint"]["initial"], original_initial)
        self.assertIsNone(resumed["execution_fingerprint"].get("final"))

    def test_fingerprint_errors_are_recorded_without_blocking_start(self):
        runner, _codex, _claude = self.make_runner("fingerprint-error")
        with patch.object(workspace_ops, "repository_fingerprint", side_effect=workspace_ops.WorkspaceError("hidden")):
            state = runner.start()

        initial = state["execution_fingerprint"]["initial"]
        self.assertEqual(state["status"], "awaiting_approval")
        for repository in ("harness", "target"):
            self.assertEqual(initial[repository]["status"], "unavailable")
            self.assertEqual(initial[repository]["error_category"], "WorkspaceError")
            self.assertNotIn("hidden", json.dumps(initial[repository]))

    def test_final_fingerprint_failure_does_not_discard_completed_evidence(self):
        def implement(_index, _command, _kwargs):
            generated = self.target / "generated.py"
            generated.write_text("value = 1\n", encoding="utf-8")

        runner, _codex, _claude = self.make_runner("final-fingerprint-error", on_implementation=implement)
        runner.start()
        with patch.object(workspace_ops, "repository_fingerprint", side_effect=workspace_ops.WorkspaceError("hidden")):
            state = orchestrator.Orchestrator(
                self.target, workflow_id="final-fingerprint-error", process_runner=runner.process_runner,
                claude_runner=runner.claude_runner,
            ).approve()

        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["implementation_summary"], "Implementation summary.")
        self.assertEqual(state["execution_fingerprint"]["final"]["target"]["status"], "unavailable")
        self.assertTrue((runner.workflow_dir / "delivery.md").is_file())
        persisted = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "complete")
        self.assertEqual(persisted["implementation_summary"], "Implementation summary.")

    def test_rejected_and_recovery_required_workflows_capture_final_target(self):
        rejected_runner, _codex, _claude = self.make_runner("rejected-final-fingerprint")
        rejected_runner.start()
        rejected = rejected_runner.reject("Out of scope")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["execution_fingerprint"]["final"]["target"]["status"], "captured")

        recovery_runner, _codex, _claude = self.make_runner("recovery-final-fingerprint")
        recovery_runner.start()
        with recovery_runner._workflow_lock():
            recovery_runner._read_state()
            recovery_runner._require_recovery(None, "simulated interrupted operation")
        recovery_state = json.loads(recovery_runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(recovery_state["status"], "recovery_required")
        self.assertEqual(
            recovery_state["execution_fingerprint"]["final"]["target"]["status"], "captured",
        )

    def test_failed_workflow_captures_final_target_fingerprint(self):
        def failing_codex(_command, **_kwargs):
            raise RuntimeError("simulated command failure")

        runner = orchestrator.Orchestrator(
            self.target, task="Fail safely", workflow_id="failed-final-fingerprint",
            process_runner=failing_codex, claude_runner=FakeClaude(),
        )
        with self.assertRaisesRegex(orchestrator.WorkflowError, "Workflow stopped at planning"):
            runner.start()

        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["execution_fingerprint"]["final"]["target"]["status"], "captured")

    def test_legacy_state_without_fingerprints_remains_resumable(self):
        runner, _codex, _claude = self.make_runner("legacy-no-fingerprints")
        runner.start()
        with runner._workflow_lock():
            runner._read_state()
            runner.state.pop("execution_fingerprint", None)
            runner._save()

        resumed = orchestrator.Orchestrator(self.target, workflow_id="legacy-no-fingerprints").resume()

        self.assertEqual(resumed["status"], "awaiting_approval")
        self.assertNotIn("execution_fingerprint", resumed)


if __name__ == "__main__":
    unittest.main()

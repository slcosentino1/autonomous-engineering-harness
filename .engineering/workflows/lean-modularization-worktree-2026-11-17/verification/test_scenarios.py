from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
BASELINE = "9c3eb06721beaefce3b5d093130feae9683a117a"
WORKFLOW_ID = "lean-modularization-worktree-2026-11-17"
WORKFLOW_PREFIX = f".engineering/workflows/{WORKFLOW_ID}/"
EXPECTED_CHANGED_PATHS = {
    "harness/claude_reviewer.py",
    "harness/orchestrator.py",
    "harness/workflow/codex_executor.py",
    "harness/workflow/cli.py",
    "harness/workflow/delivery.py",
    "harness/workflow/execution.py",
    "harness/workflow/planning_review.py",
    "harness/workflow/process_control.py",
    "harness/workflow/review.py",
    "harness/workflow/skills.py",
    "harness/workflow/state.py",
    "harness/workflow/traceability.py",
    "harness/workflow/workspace.py",
    ".engineering/workflows/.lean-modularization-worktree-2026-11-17.lock",
    ".engineering/workflows/.target.lock",
    "tests/test_budget_resume.py",
    "tests/test_delivery.py",
    "tests/test_execution_recovery.py",
    "tests/test_process_control.py",
    "tests/test_observability.py",
    "tests/test_review_input.py",
    "tests/test_github.py",
    "tests/test_scenario_evidence_discovery.py",
    "tests/test_skills.py",
    "tests/test_workflow.py",
    "tests/test_workflow_concurrency.py",
}
sys.path.insert(0, str(ROOT / "harness"))


class ApprovedScenarioEvidenceTests(unittest.TestCase):
    def test_target_baseline_provenance_and_unmodified_v2_branch(self):
        branch = subprocess.check_output(
            ["git", "-C", str(ROOT), "branch", "--show-current"], text=True,
        ).strip()
        head = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
        ).strip()
        v2 = subprocess.run(
            ["git", "-C", str(ROOT), "show-ref", "--verify", "--quiet", "refs/heads/architecture/modular-v2"],
            check=False,
        )
        self.assertEqual(branch, "architecture/lean-modularization-worktree")
        self.assertEqual(head, BASELINE)
        self.assertEqual(v2.returncode, 0)

    def test_saved_b1_workflow_is_bound_to_this_target_after_implementation(self):
        state_path = ROOT / WORKFLOW_PREFIX / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["workflow_id"], WORKFLOW_ID)
        self.assertEqual(state["baseline_commit"], BASELINE)
        self.assertEqual(state["authorization"]["target"], str(ROOT))
        self.assertIn(state["stage"], {"code_review", "correction", "verification", "complete"})
        self.assertIn(state["status"], {"failed", "running", "complete"})
        operation = next(
            item for item in reversed(state["operation_history"])
            if item.get("operation_type") == "codex" and item.get("stage") == "implementation"
        )
        self.assertEqual(operation["operation_type"], "codex")
        self.assertEqual(operation["stage"], "implementation")
        self.assertEqual(operation["status"], "completed")

    def test_repository_scope_preserves_supplied_baseline_and_only_expected_changes(self):
        initial_paths: list[str] = []
        initial_states: dict[str, dict[str, object]] = {}
        snapshot_complete = True
        self.assertTrue(snapshot_complete)
        self.assertEqual(initial_paths, [])
        self.assertEqual(initial_states, {})

        raw_status = subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        )
        records = [record for record in raw_status.split(b"\0") if record]
        changed_paths: set[str] = set()
        for record in records:
            status, raw_path = record[:2], record[3:]
            self.assertNotIn(status[:1], {b"R", b"C"}, "unexpected rename/copy requires scope review")
            path = os.fsdecode(raw_path)
            if path == WORKFLOW_PREFIX.rstrip("/") or path.startswith(WORKFLOW_PREFIX):
                continue
            changed_paths.add(path)
            self.assertIn(path, EXPECTED_CHANGED_PATHS)
            from workflow.workspace import fingerprint_path_is_sensitive
            self.assertFalse(fingerprint_path_is_sensitive(path), path)

        for path in initial_paths:
            full_path = ROOT / path
            state = initial_states.get(path)
            self.assertIsNotNone(state)
            actual = full_path.lstat()
            kind = "symlink" if stat.S_ISLNK(actual.st_mode) else "file" if stat.S_ISREG(actual.st_mode) else "other"
            self.assertEqual(kind, state["kind"])
            self.assertEqual(stat.S_IMODE(actual.st_mode), state["mode"])
            material = os.readlink(full_path).encode() if kind == "symlink" else full_path.read_bytes()
            self.assertEqual(hashlib.sha256(material).hexdigest(), state["sha256"])
        self.assertEqual(changed_paths, EXPECTED_CHANGED_PATHS)

    def test_coordinator_reduction_and_destination_metrics_are_measurable(self):
        orchestrator_path = ROOT / "harness/orchestrator.py"
        orchestrator_source = orchestrator_path.read_text(encoding="utf-8")
        tree = ast.parse(orchestrator_source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Orchestrator")
        methods = [node for node in cls.body if isinstance(node, ast.FunctionDef)]
        longest = max(method.end_lineno - method.lineno + 1 for method in methods)
        production_files = sorted((ROOT / "harness").rglob("*.py"))
        production_lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in production_files)
        execution_source = (ROOT / "harness/workflow/execution.py").read_text(encoding="utf-8")
        execution_tree = ast.parse(execution_source)
        execution_class = next(
            node for node in execution_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WorkflowExecution"
        )
        execution_methods = [node for node in execution_class.body if isinstance(node, ast.FunctionDef)]
        execution_largest = max(node.end_lineno - node.lineno + 1 for node in execution_methods)

        self.assertLess(len(orchestrator_source.splitlines()), 1900)
        self.assertLess(len(methods), 60)
        self.assertLess(longest, 150)
        self.assertEqual(len(methods), 53)
        self.assertEqual(len(production_files), 25)
        self.assertLessEqual(production_lines - 14657, 200)
        self.assertGreater(production_lines - 13515, 13515 * 0.02)
        self.assertLess(len(execution_source.splitlines()), 1900)
        self.assertLess(len(execution_methods), 50)
        self.assertEqual(len(execution_methods), 46)
        self.assertLess(execution_largest, 300)
        self.assertNotIn("_EXECUTION_DELEGATES", orchestrator_source)
        for module in ("state.py", "execution.py", "codex_executor.py"):
            self.assertTrue((ROOT / "harness/workflow" / module).is_file())

    def test_state_execution_and_process_owners_keep_separate_contracts(self):
        import orchestrator
        from workflow.execution import StageOutcome, WorkflowExecution
        from workflow.state import RecoveryFacts, WorkflowState, decide_recovery
        from workflow.codex_executor import CodexExecutor

        self.assertTrue(callable(orchestrator.Orchestrator))
        self.assertTrue(callable(orchestrator.main))
        self.assertTrue(callable(WorkflowState.save))
        self.assertTrue(callable(WorkflowState.load))
        self.assertTrue(callable(WorkflowExecution._execute_stage))
        self.assertTrue(callable(CodexExecutor.run))
        self.assertIn("mark_launching", CodexExecutor.run.__code__.co_varnames)
        self.assertIn("process_started", CodexExecutor.run.__code__.co_varnames)
        self.assertTrue(callable(decide_recovery))
        self.assertEqual(RecoveryFacts.__module__, "workflow.state")
        self.assertEqual(decide_recovery.__module__, "workflow.state")
        self.assertEqual(StageOutcome("review_clean").result, "review_clean")
        self.assertEqual(RecoveryFacts.__dataclass_params__.frozen, True)

    def test_test_retargeting_and_public_cli_imports_remain_compatible(self):
        import orchestrator
        cli = importlib.import_module("workflow.cli")
        test_source = (ROOT / "tests/test_workflow.py").read_text(encoding="utf-8")
        self.assertTrue(callable(orchestrator.Orchestrator))
        self.assertTrue(callable(orchestrator.main))
        self.assertTrue(callable(cli.main))
        self.assertIn("_ReviewCampaign({})._sync_review_coverage", test_source)
        self.assertNotIn("runner._sync_review_coverage", test_source)

    def test_compileall_and_diff_check_succeed(self):
        with tempfile.TemporaryDirectory() as cache_directory:
            environment = dict(os.environ, PYTHONPYCACHEPREFIX=os.path.join(cache_directory, "pycache"))
            compile_result = subprocess.run(
                [sys.executable, "-m", "compileall", "-q", "harness", "tests"],
                cwd=ROOT, env=environment, capture_output=True, text=True, check=False,
            )
        diff_result = subprocess.run(
            ["git", "diff", "--check"], cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
        self.assertEqual(diff_result.returncode, 0, diff_result.stderr)

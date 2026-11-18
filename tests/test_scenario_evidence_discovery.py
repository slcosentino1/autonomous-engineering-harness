"""Expose workflow-local scenario tests to the required default unittest discovery."""

import importlib.util
from pathlib import Path
import sys

verification_path = (
    Path(__file__).resolve().parents[1]
    / ".engineering/workflows/lean-modularization-worktree-2026-11-17/verification/test_scenarios.py"
)
spec = importlib.util.spec_from_file_location("workflow_verification.test_scenarios", verification_path)
if spec is None or spec.loader is None:
    raise ImportError("Workflow verification scenarios are unavailable")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
ApprovedScenarioEvidenceTests = module.ApprovedScenarioEvidenceTests

from __future__ import annotations

import copy
import hashlib
import json

from workflow import planning_review as _planning_review
from workflow.errors import WorkflowError
from workflow.review import _safe_review_path
from workflow.engine import StageInput
from workflow.stage_services import ProductionStageServices


def _execute_delivery(stage_input: StageInput, services, stage: str) -> None:
    delivery_verification = services.verify_delivery_integrity()
    services.capture_final_target_fingerprint()
    final_target = services.state.get("execution_fingerprint", {}).get("final", {}).get("target", {})
    if (final_target.get("status") != "captured"
            or final_target.get("commit_sha") != delivery_verification.get("target_commit_sha")
            or final_target.get("working_tree_sha256") != delivery_verification.get("target_working_tree_sha256")):
        raise WorkflowError("Delivery is blocked because the target changed during final integrity checks.")
    services.state["status"] = "complete"
    services.state["completed_at"] = services.now()
    services.evaluate_commitment_delivery()
    if isinstance(services.state.get("commitment_gate"), dict):
        services.state["commitment_gate"]["workflow_status"] = "complete"
    services.atomic_text(services.delivery_path, services.delivery_report())
    delivery_hash = hashlib.sha256(services.read_bytes(services.delivery_path)).hexdigest()
    services.state.setdefault("integrity", {})["delivery_artifact_sha256"] = delivery_hash
    services.save()

"""Delivery integrity verification and report rendering."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import claude_reviewer
from workflow import persistence as _persistence
from workflow import traceability as _traceability
from workflow import workspace as _workspace
from workflow.state import WorkflowError
from workflow.review import (
    build_review_diff,
    review_reports_sha256,
    review_source_hash_matching_evidence,
)


def _clean_markdown(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.strip().splitlines()).strip() + "\n"


def verify_delivery_integrity(
    state: Mapping[str, Any],
    *,
    target: Path,
    proposal_path: Path,
) -> dict:
    """Verify saved review, proposal, target, and commitment evidence before delivery."""
    integrity = state.get("integrity")
    evidence = integrity.get("review_evidence") if isinstance(integrity, dict) else None
    if not isinstance(evidence, dict):
        raise WorkflowError("Delivery is blocked because no completed review provenance was recorded.")
    correction_attempts = state.get("correction_attempts", [])
    expected_purpose = "verification" if correction_attempts else "initial"
    if evidence.get("purpose") != expected_purpose:
        raise WorkflowError("Delivery is blocked because the final review does not verify the corrected source.")
    if correction_attempts:
        verification = integrity.get("verification_result") if isinstance(integrity, dict) else None
        if (not isinstance(verification, dict) or verification.get("status") != "verified"
                or verification.get("campaign_id") != evidence.get("campaign_id")
                or not isinstance(verification.get("original_finding_dispositions"), dict)
                or not verification.get("original_finding_dispositions")):
            raise WorkflowError("Delivery is blocked because post-correction verification is incomplete.")
    try:
        proposal_bytes = _persistence.read_bytes(proposal_path)
    except OSError as exc:
        raise WorkflowError("Delivery is blocked because the saved proposal is missing or unreadable.") from exc
    try:
        proposal = _persistence.read_text(proposal_path)
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowError(f"Saved proposal is unavailable: {proposal_path}") from exc
    if not proposal.strip():
        raise WorkflowError("Saved proposal is empty.")
    proposal_text = _clean_markdown(proposal)
    proposal_hash = hashlib.sha256(proposal_bytes).hexdigest()
    expected_proposal_hash = state.get("proposal_artifact_sha256")
    version_hashes = state.get("proposal_version_hashes", {})
    version_hash = version_hashes.get(str(state.get("proposal_version"))) if isinstance(version_hashes, dict) else None
    if (not isinstance(expected_proposal_hash, str) or proposal_hash != expected_proposal_hash
            or proposal_hash != version_hash
            or proposal_text != _clean_markdown(str(state.get("proposal", "")))):
        raise WorkflowError("Delivery is blocked because the saved proposal changed after review.")
    approved_ledger_snapshot = state.get("commitment_ledger_approval")
    if state.get("traceability_required") is True:
        try:
            current_ledger = _traceability.parse_commitment_ledger(proposal_text)
        except _traceability.TraceabilityError as exc:
            raise WorkflowError(f"Delivery is blocked because the approved commitment ledger is invalid: {exc}") from exc
        if (not isinstance(approved_ledger_snapshot, dict)
                or approved_ledger_snapshot.get("proposal_version") != state.get("proposal_version")
                or approved_ledger_snapshot.get("proposal_sha256") != proposal_hash
                or approved_ledger_snapshot.get("ledger_sha256") != _traceability.canonical_sha256(current_ledger)
                or approved_ledger_snapshot.get("ledger") != current_ledger):
            raise WorkflowError("Delivery is blocked because the approved commitment ledger binding changed.")
    if review_reports_sha256(state) != evidence.get("review_reports_sha256"):
        raise WorkflowError("Delivery is blocked because the final review evidence changed after verification.")
    if state.get("traceability_required") is True:
        approved_ledger = state.get("commitment_ledger_approval", {})
        commitment_evidence = state.get("commitment_evidence")
        if isinstance(commitment_evidence, dict):
            evidence_payload = dict(commitment_evidence)
            expected_evidence_hash = evidence_payload.pop("evidence_sha256", None)
            evidence_record_matches = (
                expected_evidence_hash == _traceability.canonical_sha256(evidence_payload)
                and evidence.get("commitment_evidence_sha256") == expected_evidence_hash
            )
        else:
            evidence_record_matches = (
                commitment_evidence is None and evidence.get("commitment_evidence_sha256") is None
            )
        if (evidence.get("commitment_ledger_sha256") != approved_ledger.get("ledger_sha256")
                or not evidence_record_matches
                or evidence.get("commitment_assessment_sha256") != _traceability.canonical_sha256(
                    state.get("commitment_assessment"),
                )):
            raise WorkflowError("Delivery is blocked because commitment traceability evidence changed after review.")
    if (hashlib.sha256(str(state.get("implementation_summary") or "").encode("utf-8")).hexdigest()
            != evidence.get("implementation_summary_sha256")
            or hashlib.sha256(str(state.get("correction_summary") or "").encode("utf-8")).hexdigest()
            != evidence.get("correction_summary_sha256")):
        raise WorkflowError("Delivery is blocked because implementation or correction evidence changed after verification.")
    current_diff = build_review_diff(state, target)
    current_patch_hash = review_source_hash_matching_evidence(state, current_diff, evidence)
    if current_patch_hash is None:
        raise WorkflowError("Delivery is blocked because target code changed after its final review.")
    execution_fingerprint = state.get("execution_fingerprint")
    initial_target = (
        execution_fingerprint.get("initial", {}).get("target", {})
        if isinstance(execution_fingerprint, dict) else {}
    )
    baseline_snapshot = state.get("baseline_snapshot")
    if isinstance(baseline_snapshot, dict) and (
            baseline_snapshot.get("baseline_fingerprint_sha256") != initial_target.get("working_tree_sha256")
            or evidence.get("baseline_fingerprint_sha256") != initial_target.get("working_tree_sha256")
            or evidence.get("baseline_snapshot_sha256") != baseline_snapshot.get("sha256")
            or evidence.get("review_scope_sha256") != current_patch_hash
            or evidence.get("review_scope_paths") != state.get("review_scope_files", [])
            or evidence.get("workflow_change_paths") != state.get("review_workflow_paths", [])):
        raise WorkflowError("Delivery is blocked because baseline or review-scope provenance changed.")
    try:
        fingerprint = _workspace.repository_fingerprint(target, redactor=claude_reviewer.redact)
        current_fingerprint = {
            "status": "captured",
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            **fingerprint,
        }
    except Exception:
        current_fingerprint = {
            "status": "unavailable",
            "error_category": "FingerprintError",
            "error": "Fingerprint calculation failed; repository state is unavailable.",
        }
    if (current_fingerprint.get("status") != "captured"
            or current_fingerprint.get("commit_sha") != evidence.get("target_commit_sha")
            or current_fingerprint.get("working_tree_sha256") != evidence.get("target_working_tree_sha256")):
        raise WorkflowError("Delivery is blocked because the target fingerprint changed after its final review.")
    result = {
        "status": "verified",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "proposal_sha256": proposal_hash,
        "proposal_version": state.get("proposal_version"),
        "source_patch_sha256": current_patch_hash,
        "target_commit_sha": current_fingerprint["commit_sha"],
        "target_working_tree_sha256": current_fingerprint["working_tree_sha256"],
        "review_campaign_id": evidence.get("campaign_id"),
    }
    integrity["delivery_verification"] = result
    return result

def render_delivery_report(
    state: Mapping[str, Any],
    *,
    workflow_id: str,
    target: str | Path,
    fallback_completed_at: str,
    valid_plan_review_count: int,
    structured_review_feedback: str,
    claude_max_payload_env: str,
    default_claude_payload_chars: int,
) -> str:
    """Render a delivery report from supplied workflow data without side effects."""
    reviews = state.get("plan_reviews", [])
    external_authorization = state["authorization"]["claude_aily"]
    sections = [
        "# Workflow delivery",
        "",
        f"- Workflow: `{workflow_id}`",
        f"- Workflow status: `{state.get('workflow_status', state.get('status', 'unknown'))}`",
        f"- Delivery disposition: `{state.get('delivery_disposition') or 'not recorded (legacy workflow)'}`",
        f"- Target: `{target}`",
        f"- Completed: {state.get('completed_at', fallback_completed_at)}",
        f"- Codex model: {state.get('codex_model') or 'Codex CLI default'}",
        f"- Claude spend recorded: ${external_authorization['spent_usd']:.4f} / ${external_authorization['budget_usd']:.2f}",
        f"- Spend metadata complete: {'no' if external_authorization['budget_unknown'] else 'yes'}",
        "",
        "## Implementation",
        "",
        state.get("implementation_summary") or "No implementation summary was recorded.",
        "",
        "## Plan review",
        "",
        f"Claude plan-review iterations: {valid_plan_review_count} valid of {external_authorization['max_plan_reviews']} maximum.",
        f"Invalidated plan-review attempts: {len(reviews) - valid_plan_review_count}.",
        "",
        "## Commitment traceability",
        "",
    ]
    gate = state.get("commitment_gate")
    if isinstance(gate, dict):
        sections.extend([
            f"Disposition: `{gate.get('disposition', 'unavailable')}`.",
            f"Approved ledger SHA-256: `{gate.get('approved_ledger_sha256') or 'unavailable'}`.",
            "Workflow completion and delivery disposition are recorded independently.",
            gate.get("rationale") or "No disposition rationale was recorded.",
            "",
        ])
        integrity_errors = gate.get("evidence_integrity_errors", [])
        if isinstance(integrity_errors, list) and integrity_errors:
            sections.append("Evidence integrity and provenance errors:")
            sections.extend(f"- {error}" for error in integrity_errors if isinstance(error, str))
            sections.append("")
        elif gate.get("evidence_integrity_errors") == []:
            sections.extend(["Evidence integrity and provenance: valid.", ""])
        policy = gate.get("partial_delivery_policy")
        if isinstance(policy, dict):
            sections.extend([
                f"Approved partial-delivery policy: {policy.get('reason') or 'reason unavailable'}",
                "",
            ])
        commitments = gate.get("commitments", [])
        if isinstance(commitments, list) and commitments:
            for item in commitments:
                if not isinstance(item, dict):
                    continue
                source_references = ", ".join(
                    f"`{ref.get('path')}` ({ref.get('symbol')})"
                    for ref in item.get("source_refs", []) if isinstance(ref, dict)
                ) or "none"
                validation_references = "; ".join(
                    f"`{ref.get('scenario_id')}`: {', '.join(ref.get('test_ids', []))}"
                    for ref in item.get("validation_refs", []) if isinstance(ref, dict)
                ) or "none"
                sections.extend([
                    f"### {item.get('id', 'unknown')} — `{item.get('class', 'unknown')}`",
                    "",
                    f"- Availability: `{item.get('availability', 'unknown')}`.",
                    f"- Acceptance condition: {item.get('acceptance_condition') or 'not recorded'}",
                    f"- Implementation status: `{item.get('implementation_status') or 'unassessed'}`.",
                    f"- Implementation attribution: `{item.get('implementation_evidence_status', 'unavailable')}`.",
                    f"- Implementation operation: `{item.get('implementation_operation_id') or 'unavailable'}`.",
                    f"- Verification status: `{item.get('verification_status', 'unverified')}`.",
                    f"- Claude assessment: `{item.get('claude_assessment') or 'insufficient_evidence'}`.",
                    f"- Claude evidence references: `{'valid' if item.get('claude_evidence_refs_valid') else 'invalid or missing'}`.",
                    f"- Source evidence: `{item.get('source_evidence_status', 'unavailable')}`.",
                    "- Source provenance: " + ("; ".join(
                        f"`{ref.get('path')}#{ref.get('symbol')}` = `{ref.get('origin')}`"
                        for ref in item.get("source_provenance", []) if isinstance(ref, dict)
                    ) or "unavailable") + ".",
                    f"- Source references: {source_references}.",
                    f"- Validation references: {validation_references}.",
                    f"- Independent assessment: {item.get('claude_explanation') or 'not recorded'}",
                ])
                sections.extend(f"- Gap: {reason}" for reason in item.get("reasons", []) if isinstance(reason, str))
                if item.get("disposition"):
                    sections.append(
                        f"- Authorized disposition claim: `{item.get('disposition')}` — "
                        f"{item.get('disposition_reason') or 'no reason recorded'}"
                    )
                sections.append("")
        else:
            sections.append("No commitment results were recorded.")
    else:
        sections.append("Traceability is unavailable or was not recorded for this legacy workflow.")
    sections.append("")
    execution_fingerprint = state.get("execution_fingerprint", {})
    if not isinstance(execution_fingerprint, dict):
        execution_fingerprint = {}
    initial_fingerprints = execution_fingerprint.get("initial", {})
    if not isinstance(initial_fingerprints, dict):
        initial_fingerprints = {}
    initial_harness = initial_fingerprints.get("harness", {})
    initial_target = initial_fingerprints.get("target", {})
    if not isinstance(initial_harness, dict):
        initial_harness = {}
    if not isinstance(initial_target, dict):
        initial_target = {}
    final_fingerprints = execution_fingerprint.get("final", {}) if isinstance(
        execution_fingerprint, dict,
    ) else {}
    if not isinstance(final_fingerprints, dict):
        final_fingerprints = {}
    final_target = final_fingerprints.get("target", {})
    if not isinstance(final_target, dict):
        final_target = {}

    def fingerprint_value(record: dict, key: str) -> str:
        if record.get("status") != "captured":
            return "unavailable"
        value = record.get(key)
        return value if isinstance(value, str) and value else "unavailable"

    sections.extend([
        "## Execution fingerprints",
        "",
        f"- Algorithm: `{execution_fingerprint.get('algorithm', 'unavailable')}`.",
        f"- Initial harness: commit `{fingerprint_value(initial_harness, 'commit_sha')}`; working tree SHA-256 "
        f"`{fingerprint_value(initial_harness, 'working_tree_sha256')}`.",
        f"- Initial target: commit `{fingerprint_value(initial_target, 'commit_sha')}`; working tree SHA-256 "
        f"`{fingerprint_value(initial_target, 'working_tree_sha256')}`.",
        f"- Target dirty at initialization: "
        f"{'yes' if initial_target.get('status') == 'captured' and not initial_target.get('working_tree_clean') else 'no' if initial_target.get('status') == 'captured' else 'unknown'}.",
        f"- Final target: commit `{fingerprint_value(final_target, 'commit_sha')}`; working tree SHA-256 "
        f"`{fingerprint_value(final_target, 'working_tree_sha256')}`; "
        f"clean={'yes' if final_target.get('status') == 'captured' and final_target.get('working_tree_clean') else 'no' if final_target.get('status') == 'captured' else 'unknown'}.",
    ])
    change_summary = final_fingerprints.get("change_summary", {})
    if not isinstance(change_summary, dict):
        change_summary = {}
    if change_summary.get("status") == "captured":
        change_counts = change_summary.get("counts_by_change", {})
        attribution_counts = change_summary.get("counts_by_attribution", {})
        if not isinstance(change_counts, dict):
            change_counts = {}
        if not isinstance(attribution_counts, dict):
            attribution_counts = {}
        examples_by_attribution = change_summary.get("examples_by_attribution", {})
        examples_truncated = change_summary.get("examples_truncated", {})
        if not isinstance(examples_by_attribution, dict):
            examples_by_attribution = {}
        if not isinstance(examples_truncated, dict):
            examples_truncated = {}
        sections.extend([
            "- Changes relative to the starting baseline: " + ", ".join(
                f"{name} {change_counts.get(name, 0)}"
                for name in ("added", "modified", "deleted", "untracked", "type_changed")
            ) + ".",
            "- Path attribution: "
            f"{attribution_counts.get('preexisting_or_overlapping', 0)} preexisting or overlapping; "
            f"{attribution_counts.get('changed_after_initial_capture', 0)} final-state paths not marked "
            "preexisting at initialization.",
            "- Attribution limit: " + change_summary.get(
                "attribution_limit", "Concurrent edits cannot be excluded.",
            ),
        ])
        for category, label in (
            ("preexisting_or_overlapping", "Preexisting or overlapping path examples"),
            ("changed_after_initial_capture", "Final-state paths not marked preexisting at initialization"),
        ):
            examples = examples_by_attribution.get(category, [])
            if not isinstance(examples, list):
                examples = []
            if examples:
                sections.append(f"- {label}: " + ", ".join(
                    f"`{item['path']}` ({item['change']})" for item in examples
                    if isinstance(item, dict) and isinstance(item.get("path"), str)
                ) + ("; more omitted" if examples_truncated.get(category) else "") + ".")
    else:
        sections.append("- Change summary: " + change_summary.get(
            "reason", "unavailable",
        ))
    sections.extend([
        "- Fingerprint scope excludes `.git/**`, `.engineering/workflows/**`, ignored untracked files, and "
        "known sensitive paths; tracked ignored files remain included.",
        "",
    ])
    for record in reviews:
        validity = "valid" if record.get("valid") is True else "invalidated"
        sections.extend([f"### Attempt {record.get('attempt', record.get('iteration'))} ({validity})", "",
                         record.get("invalidated_reason", ""), "", record["report"], ""])
    decisions = state.get("plan_review_decisions", [])
    for decision in decisions:
        sections.extend([
            f"### Codex dispositions after iteration {decision['iteration']}", "",
            decision["dispositions"], "",
        ])
    sections.extend([
        "## Claude context audit",
        "",
    ])
    for context in state.get("claude_contexts", []):
        sections.append(
            f"- `{context.get('stage')}` iteration {context.get('iteration')} proposal version "
            f"{context.get('proposal_version')}: {context.get('characters')} characters, "
            f"{context.get('utf8_bytes', 'unknown')} UTF-8 bytes against a "
            f"{context.get('payload_limit_characters', 'unknown')}-character limit; "
            f"call_state={context.get('call_state')}; "
            f"part={context.get('part_id', 'n/a')}; "
            f"SHA-256 `{context.get('prompt_sha256')}`; complete={context.get('complete')}; "
            f"sections: {', '.join(item['name'] for item in context.get('sections', []))}."
        )
    if not state.get("claude_contexts"):
        sections.append("No Claude context was submitted.")
    sections.extend(["", "## Code review file coverage", ""])
    included_files = state.get("review_included_files", [])
    excluded_files = state.get("review_exclusions", [])
    sections.append(f"Included changed files: {len(included_files)}.")
    sections.extend(f"- `{item}`" for item in included_files)
    sections.append(f"Excluded changed files: {len(excluded_files)}.")
    sections.extend(f"- `{item.get('path')}` — {item.get('reason')}" for item in excluded_files)
    if not included_files and not excluded_files:
        sections.append("No code-review file coverage was recorded.")
    campaign = state.get("review_campaign")
    sections.extend(["", "## Code review campaign", ""])
    campaign_history = state.get("review_campaign_history", [])
    campaigns = [item for item in campaign_history if isinstance(item, dict)] if isinstance(campaign_history, list) else []
    if campaign:
        campaigns.append(campaign)
    if campaigns:
        if structured_review_feedback:
            sections.extend([
                "## Structured code-review feedback", "", structured_review_feedback, "",
            ])
        for campaign_number, item in enumerate(campaigns, start=1):
            invocation_count = sum(
                call.get("call_state") != "validation_failed"
                for record in item.get("part_reviews", []) for call in record.get("calls", [])
            ) + sum(
                call.get("call_state") != "validation_failed"
                for call in item.get("integration_review", {}).get("calls", [])
            )
            sections.extend([
                f"### Campaign {campaign_number}: {item.get('purpose', 'initial')} review", "",
                f"- Campaign `{item.get('campaign_id')}`; strategy `{item.get('strategy')}`; "
                f"status `{item.get('status')}`.",
                f"- Parts: {len(item.get('parts', []))}; Claude invocation attempts: {invocation_count}; "
                f"authorized call maximum: {item.get('authorized_call_limit')}.",
                f"- Shared context SHA-256: `{item.get('shared_context_sha256')}`; per-call payload limit: "
                f"{item.get('payload_limit_characters')} characters.",
                f"- Files reviewed: {len(item.get('files_reviewed', []))}.",
            ])
            sections.extend(f"  - `{path}`" for path in item.get("files_reviewed", []))
            sections.append(f"- Files not reviewed: {len(item.get('files_not_reviewed', []))}.")
            sections.extend(
                f"  - `{record.get('path')}` — {record.get('reason')}"
                for record in item.get("files_not_reviewed", []) if isinstance(record, dict)
            )
            sections.append(
                f"- Integration review: {item.get('integration_review', {}).get('status')}."
            )
            for record in item.get("part_reviews", []):
                sections.extend([
                    f"#### {record.get('part_id')} — {record.get('status')}", "",
                    record.get("report") or "No report recorded.", "",
                ])
            integration_report = item.get("integration_review", {}).get("report")
            if integration_report:
                sections.extend(["#### Integration report", "", integration_report, ""])
    else:
        sections.append("No code review campaign was recorded.")
    sections.extend([
        "## Code review",
        "",
        state.get("code_review") or "No code review report was recorded.",
        "",
        "## Independent post-correction verification",
        "",
        f"Verification status: {(state.get('integrity') or {}).get('verification_result', {}).get('status', 'not required or unavailable')}.",
        state.get("verification_report") or "No post-correction verification was required.",
        "",
        "## Corrections and validation",
        "",
        state.get("correction_summary") or "No correction summary was recorded.",
        "",
        "## Review limits",
        "",
        f"Claude context payload limit: {claude_max_payload_env} (default "
        f"{default_claude_payload_chars:,} characters). Required sections are validated; "
        "oversized contexts stop before a paid call. Prompt and section hashes are recorded in state.json.",
        "",
        "## Approved proposal",
        "",
        state.get("proposal") or "",
    ])
    integrity = state.get("integrity", {})
    if isinstance(integrity, dict):
        evidence = integrity.get("review_evidence", {})
        delivery_verification = integrity.get("delivery_verification", {})
        sections.extend([
            "",
            "## Artifact integrity and provenance",
            "",
            f"- Proposal version: {evidence.get('proposal_version', 'unavailable')}; SHA-256 `{evidence.get('proposal_sha256', 'unavailable')}`.",
            f"- Reviewed source patch SHA-256: `{evidence.get('source_patch_sha256', 'unavailable')}`.",
            f"- Review campaign: `{evidence.get('campaign_id', 'unavailable')}` ({evidence.get('purpose', 'unknown')}).",
            f"- Delivery verification: `{delivery_verification.get('status', 'unavailable')}`.",
            f"- Delivery file SHA-256 is saved in state.json after this report is written.",
        ])
    return "\n".join(sections).rstrip() + "\n"

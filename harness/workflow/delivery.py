"""Pure rendering for completed workflow delivery reports."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


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
    ]
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
    if campaign:
        if structured_review_feedback:
            sections.extend([
                "## Structured code-review feedback", "", structured_review_feedback, "",
            ])
        invocation_count = sum(
            call.get("call_state") != "validation_failed"
            for record in campaign.get("part_reviews", []) for call in record.get("calls", [])
        ) + sum(
            call.get("call_state") != "validation_failed"
            for call in campaign.get("integration_review", {}).get("calls", [])
        )
        sections.extend([
            f"- Campaign `{campaign.get('campaign_id')}`; strategy `{campaign.get('strategy')}`; "
            f"status `{campaign.get('status')}`.",
            f"- Parts: {len(campaign.get('parts', []))}; Claude invocation attempts: {invocation_count}; "
            f"authorized call maximum: {campaign.get('authorized_call_limit')}.",
            f"- Shared context SHA-256: `{campaign.get('shared_context_sha256')}`; per-call payload limit: "
            f"{campaign.get('payload_limit_characters')} characters.",
            f"- Files reviewed: {len(campaign.get('files_reviewed', []))}.",
        ])
        sections.extend(f"  - `{path}`" for path in campaign.get("files_reviewed", []))
        sections.append(f"- Files not reviewed: {len(campaign.get('files_not_reviewed', []))}.")
        sections.extend(
            f"  - `{item.get('path')}` — {item.get('reason')}"
            for item in campaign.get("files_not_reviewed", [])
        )
        sections.append(
            f"- Integration review: {campaign.get('integration_review', {}).get('status')}."
        )
        for record in campaign.get("part_reviews", []):
            sections.extend([
                f"### {record.get('part_id')} — {record.get('status')}", "",
                record.get("report") or "No report recorded.", "",
            ])
        integration_report = campaign.get("integration_review", {}).get("report")
        if integration_report:
            sections.extend(["### Integration report", "", integration_report, ""])
    else:
        sections.append("No code review campaign was recorded.")
    sections.extend([
        "## Code review",
        "",
        state.get("code_review") or "No code review report was recorded.",
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
    return "\n".join(sections).rstrip() + "\n"

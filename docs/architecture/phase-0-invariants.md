# Phase 0 — Behavioral and recovery invariants

These invariants are derived from the current implementation and tests at V1/V2 base `9c3eb06721beaefce3b5d093130feae9683a117a`. They are characterization records, not new requirements or test results. The suite was inspected but not executed in Phase 0.

## Lifecycle and authorization

| ID | Invariant | Evidence in code/tests |
|---|---|---|
| L-01 | Starting a workflow creates a target-bound state and authorization before provider execution. | `Orchestrator._start_locked`; initial state and fingerprints are saved before `run_pending`. |
| L-02 | Planning ends at `awaiting_approval`; implementation cannot run before an explicit `approve`. | `_approve_locked`; `test_plan_is_saved_and_implementation_waits_for_explicit_approval`. |
| L-03 | Approval verifies current target baseline and the proposal/Commitment Ledger binding before moving to implementation. | `_require_current_approval_baseline`, `_approve_locked`; `test_proposal_approval_binds_the_user_visible_ledger_snapshot`, `test_approval_rejects_stale_target_baseline_after_another_workflow_changes_target`. |
| L-04 | Rejection and approval are separate explicit transitions. A proposal revision invalidates or reuses plan-review iterations according to saved review validity; it does not imply implementation approval. | `_revise_locked`, `_reject_locked`, plan-review branches; `test_user_revision_uses_remaining_plan_review_and_still_waits_for_approval`. |
| L-05 | Workflow and target mutations are serialized with process locks. | `_execute_locked`, `workflow/process_locks.py`; `test_second_mutating_process_cannot_interfere_with_active_call`, `test_target_lock_blocks_another_process_for_a_distinct_workflow`. |

## Persistence and provider operations

| ID | Invariant | Evidence in code/tests |
|---|---|---|
| P-01 | Authoritative state writes are atomic and require the workflow lock. | `workflow/persistence.py::atomic_text`, `Orchestrator._save`; `test_save_still_requires_workflow_lock_and_translates_write_failures`. |
| P-02 | A provider operation record and input identity are persisted before launch; a process PID is saved when observed. | `_new_operation`, `_operation_launching`, `_operation_process_started`, `_run_codex_impl`, `_call_claude_impl`; process interruption tests. |
| P-03 | Completed provider results can be reused after a crash between result persistence and stage advancement rather than rerunning the provider. | Codex recovery path; `test_completed_codex_result_is_reused_after_parent_stops_before_stage_advance`. Review context reuse is keyed by campaign/part/prompt hash. |
| P-04 | Timeout and cancellation terminate provider process groups and record partial/uncertain outcomes rather than claiming success. | `workflow/process_control.py`; `test_timeout_cancellation_terminates_child_processes`, `test_timeout_records_partial_output_and_interruptions_leave_started_operation_for_recovery`. |
| P-05 | A launched provider call with no saved terminal result is uncertain. It is not automatically retried. A human can resolve supported recovery paths only with explicit confirmations; Claude retry additionally requires verified final cost and provider inactivity. | `_require_recovery`, `_resolve_recovery`; `test_interrupted_claude_call_marks_cost_unknown_and_is_not_repeated`, `test_interrupted_claude_retry_requires_verified_cost_and_stays_within_budget`. Exact-once provider execution is not guaranteed by this local contract. |
| P-06 | Unknown Claude/Aily spend blocks later paid review calls and budget changes until resolved; one unknown multipart call blocks later parts and integration review. | `_call_claude_impl`, `_resume_locked`; `test_unknown_part_spend_blocks_all_later_campaign_calls`, `test_unknown_spend_blocks_budget_change_without_mutation`, `test_spend_unknown_stops_later_paid_reviews`. |
| P-07 | Authorization, provider call counts, time limits, runtime, and budget are persisted and validated on resume. | `workflow/authorization.py`; `tests/test_authorization.py`, `tests/test_budget_resume.py`, workflow limit tests. |

## Review and evidence

| ID | Invariant | Evidence in code/tests |
|---|---|---|
| R-01 | Review scope is computed against the saved baseline and distinguishes preexisting target changes from workflow-attributed changes. Sensitive, binary, metadata, symlink, and unsupported files are excluded with reasons rather than silently treated as reviewed. | `workflow/review_input.py::build_review_diff`; `test_baseline_snapshot_includes_staged_unstaged_and_untracked_context_but_attributes_only_new_deltas`, `test_review_packet_excludes_sensitive_and_preexisting_paths_and_redacts_tokens`. |
| R-02 | Every included review patch segment is assigned and hashed; multipart review requires a final integration review. | `workflow/review.py::ReviewCampaign`; `test_large_review_uses_adaptive_parts_shared_context_and_integration`, coverage/partition tests. |
| R-03 | Completed validated parts are reused; an exact persisted completed report can be recovered by its campaign/part/prompt hash. A failed or unknown part is not treated as reviewed. | `_run_review_campaign`, `recover_review_campaign`; `test_resume_reuses_successful_parts_and_retries_only_failed_part`, review recovery tests. |
| R-04 | A malformed or incomplete reviewer report cannot satisfy coverage or approval. | `parse_part_review_report`; parser/coverage tests in `test_workflow.py`. |
| E-01 | Proposal approval binds the user-visible ledger snapshot; implementation output alone does not establish commitment fulfillment. | `_validate_proposal_ledger`, `_approve_locked`, `workflow/traceability.py`; `test_proposal_approval_binds_the_user_visible_ledger_snapshot`. |
| E-02 | Evidence references must identify operations and observed test events. Duplicate/ambiguous operation IDs, stale evidence, and mismatched event or target fingerprints fail closed. | `workflow/traceability.py::evaluate_ledger`; `test_duplicate_operation_ids_are_ambiguous`, `test_event_from_another_operation_cannot_satisfy_the_reference`, `test_implementation_evidence_is_stale_after_a_correction_operation`. |
| E-03 | JUnit artifacts must match the recorded path, hash, event, operation, target snapshot, and capture time. | `_junit_receipt_matches`; `test_junit_artifact_must_match_its_operation_and_target_snapshot`, `test_stale_junit_artifact_cannot_satisfy_a_new_execution_event`. |
| E-04 | Post-correction review is independent of the correction claim; unresolved or new blocking findings prevent completion. | `verification` stage in `_run_pending_stages`; `test_unresolved_verification_blocks_delivery_without_a_second_correction`, `test_verification_detects_a_new_blocking_regression_from_the_correction`. |

## Delivery, workspace, and observability

| ID | Invariant | Evidence in code/tests |
|---|---|---|
| D-01 | Delivery integrity is checked against the approved proposal, review provenance, and final target fingerprint; target or proposal changes after final review block completion. | `_verify_delivery_integrity`; `test_code_change_between_final_review_and_delivery_blocks_completion`, `test_proposal_change_between_review_and_delivery_blocks_completion`. |
| D-02 | Commitment disposition is `complete`, `partial`, or `blocked` according to the approved policy and evidence; workflow completion and delivery disposition are separate. | `workflow/traceability.py::evaluate_ledger`, `workflow/delivery.py`; delivery and traceability tests. |
| W-01 | Initial target/harness fingerprints, protected preexisting paths, and workflow artifacts are kept separate. Sensitive content is not persisted in fingerprints. | `workflow/workspace.py`; tests for fingerprints, ignored/sensitive files, and snapshots. |
| O-01 | Observability is optional and failure-isolated; when disabled, the no-op tracer is used and trace export does not determine workflow success. | `observability/tracer.py`, `observability/langfuse.py`; `tests/test_observability.py`, trace failure tests in `test_workflow.py`. |
| C-01 | Existing workflow state remains readable, including older shapes lacking newer operation metadata. | `_read_state`, `_load`; `test_json_serialization_and_legacy_state_round_trip`, `test_legacy_operation_summary_remains_readable_without_new_evidence_fields`, authorization compatibility tests. |

## Phase 1 regression focus

Phase 1 changes prompts and stage structure only. It must preserve L-01 through L-04 and P-01 through P-03 exactly. The following tests are the minimum transition regression set:

- `test_plan_is_saved_and_implementation_waits_for_explicit_approval`
- `test_plan_review_is_limited_to_two_iterations`
- `test_user_revision_uses_remaining_plan_review_and_still_waits_for_approval`
- `test_proposal_approval_binds_the_user_visible_ledger_snapshot`
- `test_completed_codex_result_is_reused_after_parent_stops_before_stage_advance`

New deterministic builder tests should assert the output for explicit inputs and that builders do not mutate their inputs. They should not invoke provider CLIs.

## Known limitation

The current API permits a human-authorized Claude retry after reconciling final cost and provider inactivity. If an external provider completed a request but its response was lost, such a retry could repeat provider work. Automatic retries remain blocked; the code cannot promise exactly-once calls without provider idempotency/result lookup. Preserve and test the existing explicit recovery policy unless a separate product decision changes it.

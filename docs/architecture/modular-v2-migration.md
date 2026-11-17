# Modular V2 migration log

- **Repository:** `/Users/slcosentino/Documents/Preparacion/autonomous-engineering-harness-modular`
- **Branch:** `architecture/modular-v2`
- **Base HEAD:** `9c3eb06721beaefce3b5d093130feae9683a117a`

## Phase 1A — Stage coordination decomposition

**Status:** Complete; checkpoint passed before Phase 1B.

`Orchestrator._run_pending_stages` retains the running loop, stage tracing and timing, workflow runtime limit, cancellation/failure handling, and unknown-stage error. It dispatches the existing lifecycle stages to eight named methods on the same class: planning, plan review, plan refinement, human revision, implementation, code review/verification, correction, and completion. The class was not relocated, and no generic handler framework was introduced.

Each stage method contains its original branch body. The implementation approval guard still saves `awaiting_approval` and returns before implementation guidance or a provider call. The exception handling and persistence locations remain under the same stage-level wrapper. No provider calls, workflow executions, operation rules, state fields, or serialization formats were added or changed.

### Phase 1A verification

- `python3 -m py_compile harness/orchestrator.py` — passed.
- `git diff --check` — passed.
- Mechanical source comparison — all eight extracted stage bodies match their original branch bodies; the implementation guard's loop `break` is represented as an immediate method `return` after the same save.
- Five focused workflow tests — passed: approval gate, two-review limit, revision approval gate, proposal ledger approval binding, and completed Codex result recovery.
- `python3 -m unittest discover -s tests -p 'test_workflow.py'` — **199 tests passed** in 313.282 seconds.

## Phase 1B — Prompt and request builders

**Status:** Complete.

### Files changed for Phase 1

- `harness/orchestrator.py` — delegates existing planning, refinement, human revision, implementation, correction, and plan-review input assembly to pure builders. Provider execution, authorization, operation tracking, state transitions, persistence, recovery, and review context gathering remain in the Orchestrator.
- `harness/workflow/planning.py` — deterministic proposal-generation, plan-refinement, and human-revision prompt builders. Each takes the exact text and values needed; none receives workflow state or an Orchestrator.
- `harness/workflow/planning_review.py` — retains scorecard normalization and adds deterministic plan-review header, sections, and metadata construction from explicit values.
- `harness/workflow/execution.py` — deterministic implementation and correction prompt builders from explicit validation, proposal, review, and guidance text.
- `tests/test_prompt_builders.py` — six focused tests for deterministic output, required content, section/context ordering, approval constraints, and disposition withholding.
- `docs/architecture/modular-v2-migration.md` — this migration record.

The existing `_plan_review_prompt(iteration)` method remains as the Orchestrator-facing boundary. It gathers authorized skills and repository context, then calls `planning_review.build_plan_review_input`; it does not pass mutable workflow state to the builder. Prompt builders perform no provider calls, file writes, authorization, budget checks, operation creation, or state mutation.

### Prompt compatibility

A one-off comparison evaluated the original prompt expressions from base HEAD and the extracted builders with representative explicit fixtures. The planning, plan refinement, human revision, implementation, correction, and plan-review header/sections/metadata outputs were byte-identical. Builders preserve prompt wording, dynamic values, and context ordering. No prompt improvements or defect fixes were made.

### Phase 1B and final verification

- `python3 -m unittest discover -s tests -p 'test_prompt_builders.py'` — **6 tests passed**.
- Focused post-extraction regression set — **8 tests passed**: five workflow approval/planning/recovery tests plus three plan-review scorecard tests.
- `python3 -m unittest discover -s tests` — **398 tests passed** in 340.424 seconds.
- `python3 -m compileall -q harness tests` — passed.
- `git diff --check` — passed.

## Invariants, deviations, debt, and risks

No deviations from the Phase 0 recommendations. The stage loop and Orchestrator class were not relocated. Provider selection and arguments, operation IDs, save-before-launch ordering, state JSON, approval/rejection gates, plan-review limits, recovery behavior, timeouts, evidence provenance, delivery gates, and optional observability were not intentionally changed. The full test suite and byte-identical prompt comparison passed. No Harness workflow or external provider call was executed; workflow tests use local fakes.

Remaining technical debt is intentionally outside Phase 1: the Orchestrator still owns lifecycle and Codex execution mechanics; `ReviewCampaign` remains coupled to mutable workflow state and callbacks; evidence and delivery gates remain in the lifecycle owner. Later phases should continue with dedicated recovery characterization before extracting those stateful responsibilities. Phase 2 has not started.

## Phase 2 — Codex executor extraction

**Status:** Complete; final verification passed.

### Lifecycle traced and preserved

1. The stage builds its prompt. _run_codex opens the Codex trace span, then calls _run_codex_impl.
2. _run_codex_impl builds the persistent authorization prefix, applies the existing read-only documentation and MCP isolation rules, builds the Codex command through workflow/codex_executor.py, encodes and hashes the effective prompt, and checks for an eligible completed operation to reuse.
3. For a new invocation, the Orchestrator checks the Codex call limit, derives the timeout, reads optional CLI version metadata, creates the operation ID and operation record, and persists the record and invocation_at before execution.
4. The Orchestrator prepares the isolated provider environment and request, marks the operation launching, and persists that transition before calling the executor.
5. With the real subprocess runner, the executor starts Codex in a new process group/session. It immediately calls the narrow on_process_started(pid) callback. The Orchestrator associates the PID with the request's operation ID, marks it launched, and persists it before the executor sends prompt bytes or collects stdout/stderr.
6. The executor captures both streams and normalizes the observed outcome. Timeouts and cancellation terminate and reap the process group through workflow/process_control.py; timeout results retain partial stdout/stderr. An injected test runner has no process-start hook, so its optional PID can only be reported after that runner returns, matching the previous injected-runner behavior.
7. The Orchestrator validates the operation ID and persisted PID, applies the existing timeout/cancellation/launch/uncertain policy, fingerprints target changes, parses JSONL and stream evidence, reads the last-message file, evaluates exit and turn status, and persists the terminal result, usage/evidence, and accounting.
8. A completed result remains reusable after a parent interruption before stage advancement. A launched operation without a trustworthy persisted terminal result remains recovery-required and is not automatically replayed. Launch failure before a process is created is recorded as not started and remains safely retryable under existing recovery behavior.

### Contract and responsibility split

workflow/codex_executor.py defines frozen CodexExecutionRequest and CodexExecutionResult dataclasses.

The request contains only the already-constructed operation ID, command, prompt bytes, working directory, timeout, and prepared environment. The result associates observations with that operation ID and reports one of completed, launch_failed, nonzero_exit, timed_out, cancelled, or result_unavailable, plus exit code, captured streams, duration, PID, and error details.

The Codex executor owns command construction, Codex configuration/environment preparation, optional CLI version lookup, subprocess invocation, output capture, timeout/cancellation mechanics, and normalized process results. It imports the existing process-control module for process-group behavior.

The Orchestrator continues to own authorization and budget checks, operation IDs and records, all authoritative persistence (including launch and PID checkpoints), workflow state, accounting, evidence/provenance, recovery decisions, trace association, and stage transitions. It does not pass workflow state or itself to the executor. The PID callback accepts only an integer process identifier and closes over the already-created operation ID.

The executor imports no Orchestrator, CLI, or workflow state, and contains no lifecycle, authorization, budget, retry, or evidence-association policy. Claude/Aily execution and ReviewCampaign were not changed. The persisted workflow schema, prompts, stages, and transitions were not changed.

### Phase 2 files and verification

- harness/workflow/codex_executor.py — explicit execution contract, Codex command/configuration helpers, and process invocation.
- harness/orchestrator.py — delegates execution to the executor while retaining operation creation, persistence, evidence, accounting, and recovery.
- tests/test_codex_executor.py — contract, command/process inputs, normalized outcomes, PID ordering, timeout, cancellation, and process-group child cleanup.
- tests/test_workflow.py — save-before-launch/PID ordering, launch-failure versus uncertain-outcome behavior, unreadable result recovery, and the updated launch-checkpoint test.
- docs/architecture/modular-v2-migration.md — Phase 2 lifecycle, contract, ownership, and recovery record.
- Focused verification: 9 executor/process-control tests, 30 Codex evidence/guidance tests, and 5 launch/recovery tests passed.
- python3 -m unittest discover -s tests — 409 tests passed in 181.619 seconds.
- python3 -m compileall -q harness tests — passed.
- git diff --check — passed.

No external provider calls or Harness workflow were executed. Tests use controlled local subprocesses, fakes, and temporary repositories.

### Deviations and remaining risks

No Phase 0 invariant or persisted-state schema changed. A regression test that previously treated an injected-runner exception after the persisted launching checkpoint as an ordinary failed workflow now requires recovery: without a PID or terminal result, execution is uncertain and must not be retried automatically. The test-injected runner interface can report a PID only after it returns; the production Popen path persists its PID before output collection, as before. ReviewCampaign, evidence/delivery ownership, and Coordinator relocation remain later-phase work. Phase 3 has not started.

## Phase 3A — ReviewCampaign characterization and recovery tests

**Status:** Characterization and focused regression tests complete; no production refactor performed.

### Findings

- `ReviewCampaign` still receives and mutates Orchestrator's authoritative workflow-state dictionary. It owns campaign planning/partitioning, report validation, coverage, and per-part/integration snapshot evolution; save, provider, call-count authorization, and trace callbacks reach back into Orchestrator.
- Orchestrator owns authorization, budget, operation IDs/history, provider invocation, process identity, terminal Claude context persistence, recovery resolution, and stage transitions. Terminal provider context/operation results are persisted before the campaign part checkpoint.
- A completed report whose campaign checkpoint is absent can be reused by normal resume using campaign ID, part ID, completed call state, report, and prompt-hash de-duplication, followed by report structure validation. This path does not reconstruct and compare the prompt or verify the context `operation_id` against operation history. Local `recover_code_review` has broader prompt/source/report checks but requires call metadata to be internally consistent and is not the same repair path.
- A launched Claude operation with no terminal result is recovery-required. Default resume does not repeat it. Unknown spend blocks later paid calls. Explicit retry requires verified cost, confirmation that the prior provider call is inactive, and cap/remaining-budget validation; it records resolution on the old operation and creates a new operation ID. Paid work may repeat; exactly-once execution is not guaranteed.

See `phase-3a-review-campaign-characterization.md` for lifecycle, state/callback inventory, sequence diagrams, test gap analysis, and a proposed snapshot/result boundary. See `phase-3a-review-recovery-matrix.md` for the scenario matrix.

### Tests and validation

- Added `WorkflowTests.test_three_part_uncertain_campaign_call_requires_explicit_retry`: checks a completed first part, uncertain second operation, pending later parts, default-resume blocking, cost/inactive confirmation, and explicit retry with retained old operation history and a new ID.
- Added `WorkflowTests.test_completed_review_report_recovers_when_campaign_checkpoint_is_missing`: interrupts after the fake provider result and operation/context are saved but before the campaign checkpoint; resume recovers the exact report without a second call.
- The missing-checkpoint test also changes the reviewed source before resume, verifies the target fingerprint gate rejects reuse without another call, restores the source, then verifies recovery.
- Focused run of both new tests — **2 passed**.
- `python3 -m unittest discover -s tests` — **411 tests passed** in 206.279 seconds.
- `PYTHONPYCACHEPREFIX=/private/tmp/phase3a-compileall-cache python3 -m compileall -q harness tests` — passed. The exact command without the cache prefix was attempted, but the sandbox denied writing `tests/__pycache__`; the external cache prefix compiled the same sources without writing into the checkout.
- `git diff --check` — passed after the final changes.

### Preconditions for Phase 3B

Agree on the minimum immutable campaign inputs/result and Coordinator-owned operation/persistence contract. Decide whether normal saved-context reuse must validate operation ID, proposal version, and reconstructed prompt/section hashes. Add focused tests for reviewed-record tampering, the campaign-level source-hash guard independent of the earlier target-fingerprint guard, and resume after all review checkpoints but before stage advancement. Then extract deterministic transitions without changing authorization, budget, provider invocation, operation history, or workflow schema.

No provider calls or Harness workflows were executed. Tests use temporary repositories and local fake providers.


## Phase 3B.1 — ReviewCampaign contract and recovery rules

**Status:** Design direction accepted for Phase 3B.2 extraction. The stricter report identity and recovery proposals remain deferred to Phase 3B.4; Phase 3B.3 hardens live execution results only.

See `docs/architecture/phase-3b1-review-campaign-contract.md` for the proposed capability contract, report identity and recovery rules, persistence boundary, transition protocol, test matrix, and decisions requiring approval. No production code, tests, or persisted state formats were changed in Phase 3B.1.

## Phase 3B.2 — ReviewCampaign pure capability extraction

**Status:** Deterministic review capabilities extracted; at the end of Phase 3B.2, workflow lifecycle and recovery remained in the compatibility runner and Orchestrator.

### Modules and contracts

- Added `workflow/review_models.py` for immutable review inputs, source segments, campaign snapshots, requests, provider outcomes, validations, and transitions. The snapshot wrapper uses the existing persisted campaign JSON shape; its contract version is in memory only.
- Added `workflow/review_partitioning.py` for adaptive lossless partitioning, source and segment hashes, manifests, ordering, and partition validation.
- Added `workflow/review_requests.py` for byte-stable part and integration prompts, ordered sections, redaction, request hashes, and ephemeral proposal/segment identity.
- Added `workflow/review_validation.py` for part/integration report checks, typed `ReportValidation` findings, validated-report rendering, and feedback formatting. The existing dictionary parser remains available as a compatibility API.
- Added `workflow/review_campaign.py` for campaign snapshot creation, authorization-blocked snapshot values, coverage evolution, and pure `advance(snapshot, input, previous_result)` decisions.
- `workflow/review_input.py` now imports `ReviewDiff` directly from the value module, removing its dependency on the campaign runner.

`ReviewCampaign` remains the compatibility runner for current provider callbacks, trace spans, workflow saves, and report recovery. The Orchestrator continues to own authorization, budget and spend, operation IDs/history, Claude context persistence, process identity, provider execution, workflow locks and stages, retry/recovery decisions, and target fingerprint checks. Phase 3B.2 does not add strict operation-history joins or change existing recovery trust rules.

### Compatibility and tests

- Compared a representative part request with the pre-extraction implementation at `HEAD`: section mapping and final prompt bytes match; the prompt SHA-256 remains `03a5307d6c80026f4aa1f6d5682f07f0455da0f22cd142458a2bad6101ce0ad2`.
- Compared adaptive partition output against the pre-extraction implementation: the partition manifest matches for a three-part, three-segment fixture.
- Compared part and integration request sections from a two-part fixture: all three provider requests match the pre-extraction implementation.
- Added `tests/test_review_capability.py` with 13 focused tests for deterministic partitioning, boundaries and exclusions, immutable requests/snapshots, report validation, coverage, transitions, invalid and uncertain outcomes, and existing campaign JSON keys.
- Added a workflow regression verifying part trace results follow part identity when persisted part checkpoints are reordered.
- Focused capability, trace, and workflow recovery set — **17 tests passed**, including both Phase 3A recovery tests.
- Final full-suite run, `PYTHONPYCACHEPREFIX=/private/tmp/phase3b2-final-cache python3 -m unittest discover -s tests` — **425 tests passed** in 179.135 seconds. This includes both Phase 3A recovery tests.
- `PYTHONPYCACHEPREFIX=/private/tmp/phase3b2-final-cache python3 -m compileall -q harness tests` — passed.
- `git diff --check` — passed.
- No real external providers or Harness workflows were invoked; workflow integration tests use fakes and temporary repositories.
- No persisted schema fields or migrations were added. Proposal hashes and snapshot versions exist only in capability values; the serialized campaign keys remain unchanged. The final review also corrected trace status lookup to resolve the transitioned part by `part_id`, preserving accurate trace metadata when saved checkpoints are reordered.

### Remaining coupling and Phase 3B.3

At the end of Phase 3B.2, the `ReviewCampaign` compatibility runner still received workflow state and narrow callbacks, and `orchestrator.py` retained compatibility wrappers. Existing context fallback and local recovery still use their current identity checks. Phase 3B.3 moves live campaign orchestration and execution-result association to the Coordinator; strict reconciliation of recovered reports and persisted operation history remains Phase 3B.4 work.

## Phase 3B.3 — Review campaign hardening and Coordinator ownership

**Status:** Implementation complete; final repository validation recorded below.

### Ownership and boundaries

- `Orchestrator` now owns review campaign preparation and execution sequencing, authorization checks, tracing, saves, provider invocation, and live result association. The stateful preparation and execution runner was removed from `workflow/review.py::ReviewCampaign`; its remaining adapter surface is limited to construction/request helpers and local recovery compatibility.
- `workflow/review_requests.py` now depends on provider-neutral `workflow/review_text.py` for redaction and payload validation. `claude_reviewer` keeps its public wrappers and `ReviewError` behavior by translating the neutral error type at the adapter boundary.
- `ReviewRequest.identity` is a frozen value containing campaign/part/purpose, proposal version and hash, report contract and header identity, source/shared/part/segment hashes, and the exact prompt hash. `PreviousResult` carries that identity, the execution operation ID, and report hash. Pure `advance()` rejects mismatches without mutating the supplied snapshot.
- Campaign inputs reject duplicate part IDs. Snapshot validation requires one checkpoint for each manifest ID and accepts checkpoint reordering by stable ID. Manifest construction rejects missing and duplicate IDs before provider work.
- Before applying a live result, the Coordinator checks returned request metadata, the unique terminal Claude operation, result hash, and the unique linked context record, including its request sections and header hashes. This uses existing state fields and adds no workflow JSON keys.

### Recovery and compatibility boundaries

- Context fallback and local saved-report recovery retain their Phase 3B.2 trust policy. Reconstructing historical requests and joining recovered contexts to operation history are deferred to Phase 3B.4.
- The persisted campaign/workflow JSON schema is unchanged. No migrations, external provider execution, or Harness workflows were introduced.
- Prompt construction and the existing redaction patterns remain byte-compatible for valid requests. Approval, budget, authorization, persistence, retry, and recovery ordering remain under the Coordinator's existing policy.

### Files and validation

- Added `harness/workflow/review_text.py`; modified `claude_reviewer.py`, `workflow/review_requests.py`, `workflow/review_models.py`, `workflow/review_partitioning.py`, `workflow/review_campaign.py`, `workflow/review.py`, `orchestrator.py`, `tests/test_review_capability.py`, `tests/test_workflow.py`, and `tests/test_observability.py`.
- Baseline focused suite — **24 tests passed** before implementation.
- Focused capability and Coordinator-boundary suites — **20 tests passed**; focused live review, recovery, authorization, retry, budget, and verification regressions also passed.
- `PYTHONPYCACHEPREFIX=/private/tmp/phase3b3-final-cache python3 -m unittest discover -s tests` — **430 tests passed** in 197.928 seconds.
- `PYTHONPYCACHEPREFIX=/private/tmp/phase3b3-final-cache python3 -m compileall -q harness tests` — passed.
- `git diff --check` — passed.
- No lint or type-check configuration is present in the repository. Tests use fake providers and temporary targets; no real providers or Harness workflow were launched.

### Remaining debt and Phase 3B.4

Saved-context fallback still selects completed reports by campaign and part, then runs current report validation. It does not yet reconstruct the historical prompt or join recovered context IDs against operation history. The Coordinator now rejects mismatches on the live execution path. Phase 3B.4 should characterize and implement strict recovered-report reconciliation without changing the saved workflow schema unless separately approved.

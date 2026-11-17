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

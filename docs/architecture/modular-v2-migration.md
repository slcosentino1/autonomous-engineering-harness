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

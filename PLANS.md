# Plan and Status

## Current status

**Codex task launcher, single-session operating procedure, read-only preflight, and bounded Claude reviewer adapter implemented; full autonomous completion remains blocked.** The launcher starts one primary Codex CLI session and checks target workflow completion state. Codex still interprets lifecycle transitions; the runner is not a stage engine or automatic cross-record gate evaluator. In the isolated demo, Codex implemented a Python function, wrote focused tests and documentation, ran 3 passing tests, performed self-review, and recorded READY scoped preflight evidence. The integrated Aily call then failed before returning a Claude report because its wrapper could not create a temporary file under `~/.cache/uv`; workflow state remains `BLOCKED` at `INDEPENDENT_VERIFICATION`. No gate approval, commit, or PR was recorded. The Aily attempt has no model, token, or cost metadata, so aggregate spend cannot be established and no retry was made. The earlier standalone CLI probe with user config ignored returned 401; the final task launcher succeeded after normal local filesystem permission escalation.

## Roadmap

### 1. Reusable manual workflow foundation — complete

- Define repository boundaries and target-repository discovery.
- Document discovery, specification, planning, implementation, validation, review, correction, and closeout.
- Provide generic prompts, evidence contracts, review roles, gates, schemas, and templates.

### 2. Validate contracts with representative projects — planned

- Exercise the templates and JSON Schemas against independent sample projects.
- Tighten schema validation and clarify portability only where evidence identifies a gap.
- Keep sample product data out of this repository unless a deliberately generic fixture is needed.

### 3. Codex single-agent operating procedure — complete

- Make `harness/prompts/06-autonomous-single-agent.md` the canonical task entrypoint.
- Define the primary session's complete increment loop, scope derivation, structured self-review, correction, gate, closeout, and resume procedure.
- Define a target-local default workflow-state location without replacing target conventions or creating a custom state engine.
- Keep approval and external-operation boundaries from the existing policy.

### 4. Executable single-agent workflow orchestration — partial; E2E blocked

- `harness/runner.py` implements explicit-target preflight, one ephemeral Codex CLI invocation, resume ID selection, and a fail-closed final workflow-state check.
- Still required: runner-managed stage transitions and durable state updates, technically scoped write access beyond the target-root CLI sandbox, and a successful real Codex → validation → Aily/Claude → correction → gate demonstration.
- Automate evidence collection and selected policy checks only after the manual workflow is stable.
- Enforce command execution, working directory, approvals, and failure handling against the documented contracts.
- An earlier standalone CLI probe failed before task execution with an API `401 Unauthorized` due to a missing bearer credential; a later runner invocation did start Codex after normal local filesystem permission escalation and reached the Aily cache-write failure described above. Do not mark this roadmap item complete until the real E2E succeeds and the remaining authority boundaries are preserved.
- No custom execution framework was introduced.

### 5. Bounded Claude reviewer integration — complete

- Invoke Claude through the configured Aily CLI for an authorized, read-only independent Medium-risk review.
- Persist reserved review rounds, validated reports, linked execution evidence, finding decisions, and resume information in target-local workflow artifacts.
- Limit each review cycle to two rounds and escalate unresolved findings or disagreements; preserve existing High-risk human review and approval requirements.
- Do not make external calls without a verified target-specific human authorization record. This adapter does not implement a policy evaluator or identity provider.

### 6. Broader multi-agent orchestration — future, not started

- Reuse the reviewer and handoff contracts defined here.
- Add a coordinator and role-specific agents only after the single-agent path works and explicit authorization is given.
- Do not use LangGraph or introduce an orchestration dependency by default.

## Completion policy

A roadmap item is complete only when its stated outputs exist and evidence is recorded. Planned automation is not to be described as implemented. Update this file when scope or status changes, preserving decisions in `docs/decisions.md`.

# Plan and Status

## Current status

**Codex single-session workflow, read-only preflight, and bounded Claude reviewer adapter implemented.** One primary Codex session owns orchestration and product changes. The Aily adapter can perform a policy-required Medium-risk read-only review after target-specific external-service authorization; it does not approve gates or replace High-risk human reviewers. The workflow remains instruction-driven, not a general orchestrator: there is no state store, write broker, identity provider, cross-record evaluator, or automatic gate evaluator. Self-review is not independent review or approval.

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

### 4. Executable single-agent workflow orchestration — planned

- Implement invocation, target resolution, state persistence, and scoped access using the documented contracts in `docs/workflow.md` and `harness/config/verification-policy.yaml` only if a future task authorizes an execution component.
- Automate evidence collection and selected policy checks only after the manual workflow is stable.
- Enforce command execution, working directory, approvals, and failure handling against the documented contracts.
- No custom execution engine or orchestration framework is part of the current operating-procedure increment.

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

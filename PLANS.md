# Plan and Status

## Current status

**The explicit stage orchestrator is implemented in the current worktree and its harness suite passes; the BPHA-4727 demonstration has not started.** Local validation of the harness completed with 153 passing tests. The target preflight was read-only and returned `READY`, but the orchestrator launch was rejected before process creation because the external review payload was not authorized precisely enough for Claude/Aily. No target workflow artifacts or product changes were created by that launch. The target's pre-existing working-tree change remains outside the authorized scopes. Continue only after explicit approval of the target code payload and destination; preserve the USD 2 review budget. The orchestrator does not approve High-risk work or stage, commit, push, or create PRs.

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

### 4. Executable single-agent workflow orchestration — implemented locally; E2E blocked

- `harness/orchestrator.py` implements stage transitions, target-local workflow state, explicit plan/result schemas, allowlisted safe writes, configured `--no-restore` validations, Claude adapter integration, finding correction, and revalidation.
- The orchestrator binds task, branch, baseline commit, and configuration identity on resume; correction iterations and repeated blockers are bounded.
- The complete harness suite passes locally. The real BPHA-4727 run remains pending because automatic review rejected the launch at the external code-disclosure boundary before any process began.
- To resume, obtain explicit authorization for the exact task-scoped backend code/test/evidence payload to be sent to Claude through Aily, retaining the USD 2 aggregate budget. Do not route around this boundary.
- Even after the demonstration, High-risk policy still requires authenticated human review and a distinct authenticated gate approver. The orchestrator does not implement a gate evaluator, commits, or PR publication; do not report completion until all required authorities are recorded.
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

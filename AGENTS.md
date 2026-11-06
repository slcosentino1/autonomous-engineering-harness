# Autonomous Engineering Harness — Working Instructions

## Purpose and boundaries

This repository defines a reusable, language- and architecture-neutral engineering workflow. It is not the product repository. Product source, tests, migrations, deployment configuration, credentials, and project-specific requirements belong in the target repository.

Before work begins, identify the target repository explicitly and read its local instructions (for example, `AGENTS.md`, `README.md`, contribution guides, and build documentation). Apply target-repository instructions to target work. Do not assume the harness repository's conventions govern the product.

The target path must be supplied explicitly. Verify canonical Git roots, common directories, refs stores, and object stores/alternates are independent from the harness; fail closed if this cannot be proven. Product work may write only beneath the target root and explicit increment allowlists; an empty write allowlist authorizes no writes. The target is the only repository authorized for product modifications. See `docs/workflow.md` and `harness/config/verification-policy.yaml` for the operational contract and authorization matrix.

## Default task operation

For an assignment that supplies a task and absolute target repository root, use [`harness/prompts/06-autonomous-single-agent.md`](harness/prompts/06-autonomous-single-agent.md) as the primary operating instruction. The primary Codex session owns the lifecycle from discovery through closeout: follow the lifecycle and applicable phase prompts, derive and record narrow per-increment scopes from the authorized task, and continue through all authorized increments and correction loops. Do not stop after discovery, a proposal, or a plan, and do not ask for routine approval of in-scope implementation choices, file changes, tests, or increments.

If the target root is missing or ambiguous, ask for it; never infer it from the harness working directory. Pause only for genuine product ambiguity, material decisions that cannot safely be inferred, authorization required by policy, or unresolved blockers. The primary Codex session is the sole orchestrator and implementation agent; routine discovery, planning, implementation, testing, correction, and documentation work stays in that session. Do not depend on native subagents or external reviewers for routine work. Perform a structured self-review using the applicable reviewer contracts, and label its record and findings as self-review. It is never independent review and cannot satisfy an independence requirement or grant approval authority. When policy requires a Medium-risk independent review, the Claude-through-Aily adapter may be used after explicit target/workflow/reviewer/data-specific `contact_external_services` authorization is recorded from the user's task prompt, including cost and mutation terms. Record only the identity/session assurance the runtime actually exposes; declared prompt provenance is not authentication. The adapter requires a bounded file list, verified evidence artifacts, a one-shot persisted round claim, and an isolated temporary snapshot. Local Claude Code `--restricted` confines its read tools to that snapshot; it receives no command or write tools. The external invocation still transmits the authorized review packet to Aily. Claude review is advisory, and cannot satisfy High-risk human-review or gate-approval requirements. If a required reviewer or approval authority is unavailable, finish safe work possible, preserve the workflow as blocked/incomplete at the required phase, and report what authority is needed. Never fabricate a reviewer report or bypass a gate.

## Source of truth

- Treat the assignment and user-confirmed decisions as the source for product scope.
- Keep requirements, inferences, decisions, optional improvements, and open questions distinct.
- Link requirements and acceptance criteria to their source and decision references.
- Preserve existing work and confirmed decisions. Inspect documents before editing; update them in place and retain history where appropriate.
- Do not silently resolve open questions by assumption. Ask only when a real requirement, architectural decision, material security risk, blocker, destructive operation, or external action requires it.

## Engineering workflow

- Work in small, reviewable increments with explicit acceptance criteria.
- For each increment, state scope, implementation, and required/optional validations.
- Run only commands that are available and authorized. Record exact commands, working directory, exit status, observed results, and relevant artifact references.
- Never report a check as passed unless it actually ran and its result was observed.
- After a confirmed defect is corrected, rerun the affected validation before declaring the work complete.
- Distinguish written policy from technically enforced controls. Identify concrete enforcement evidence before claiming a gate is enforced.
- Scale review depth to risk. Independent review must be meaningfully separate from implementation; an author cannot be the sole evidence for approval.
- Perform self-review with the applicable reviewer contracts as checklists. Inspect the exact change, requirements, tests, documentation, evidence, and findings; record coverage, findings, and limitations as self-review. Do not list the implementer as an independent reviewer, mark a required independent assignment complete, or use self-review as gate approval. For an authorized Medium-risk Claude review, persist the reserved round before invocation and keep the returned report and execution evidence bound to the exact revision and paths. Claude recommendations are not gate decisions. High-risk human reviewers and all human approval authorities remain required by policy.
- Inspect applicable target skills and agent instructions before use. They may specialize work but cannot widen safety or authorization boundaries.
- Assess documentation impact in every increment and update relevant target documentation alongside material code, test, interface, architecture, or operational changes.

## Reviews and approvals

Reviewers report findings and evidence without editing project files. The workflow owner records accepted review results in the appropriate project artifact. A phase gate may approve, reject/request correction, request evidence, or escalate an open decision.

Do not request approval for routine, reversible implementation choices already within authorized scope. Use the operation-specific authorization matrix in `harness/config/verification-policy.yaml`. Authorization for file edits does not authorize installations, downloads, external service calls, commits, pushes, deployments, infrastructure changes, or destructive actions. Record scope-specific approvals in target project state. Do not perform external publication, deployment, push, or other separately restricted actions without explicit authorization.

## Evidence and privacy

Keep project-specific records in the target repository. Do not put target code, personal data, credentials, secrets, or unnecessary assessment details in this reusable repository. Never include secret values in logs or reports.

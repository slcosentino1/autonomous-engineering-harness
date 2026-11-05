# Autonomous Engineering Harness — Working Instructions

## Purpose and boundaries

This repository defines a reusable, language- and architecture-neutral engineering workflow. It is not the product repository. Product source, tests, migrations, deployment configuration, credentials, and project-specific requirements belong in the target repository.

Before work begins, identify the target repository explicitly and read its local instructions (for example, `AGENTS.md`, `README.md`, contribution guides, and build documentation). Apply target-repository instructions to target work. Do not assume the harness repository's conventions govern the product.

The target path must be supplied explicitly. Verify canonical Git roots, common directories, refs stores, and object stores/alternates are independent from the harness; fail closed if this cannot be proven. Product work may write only beneath the target root and explicit increment allowlists; an empty write allowlist authorizes no writes. The target is the only repository authorized for product modifications. See `docs/workflow.md` and `harness/config/verification-policy.yaml` for the operational contract and authorization matrix.

## Default task operation

For an assignment that supplies a task and absolute target repository root, use [`harness/prompts/06-autonomous-single-agent.md`](harness/prompts/06-autonomous-single-agent.md) as the primary operating instruction. The primary Codex session owns the lifecycle from discovery through closeout: follow the lifecycle and applicable phase prompts, derive and record narrow per-increment scopes from the authorized task, and continue through all authorized increments and correction loops. Do not stop after discovery, a proposal, or a plan, and do not ask for routine approval of in-scope implementation choices, file changes, tests, or increments.

If the target root is missing or ambiguous, ask for it; never infer it from the harness working directory. Pause only for genuine product ambiguity, material decisions that cannot safely be inferred, authorization required by policy, or unresolved blockers. When the policy requires independent review, use a separate read-only Codex session. The primary implementer must not present its own review as independent or issue an approval that policy assigns to another authority. If a required reviewer or approval authority is unavailable, preserve the current state and escalate rather than bypassing the gate.

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
- For a review that policy permits an agent session to perform, use a separate read-only Codex session and give it the exact revision, scope, requirements, instructions, and evidence. This does not replace any authenticated human reviewer required for High-risk work. If the required reviewer cannot be established, keep the phase incomplete and escalate.
- Inspect applicable target skills and agent instructions before use. They may specialize work but cannot widen safety or authorization boundaries.
- Assess documentation impact in every increment and update relevant target documentation alongside material code, test, interface, architecture, or operational changes.

## Reviews and approvals

Reviewers report findings and evidence without editing project files. The workflow owner records accepted review results in the appropriate project artifact. A phase gate may approve, reject/request correction, request evidence, or escalate an open decision.

Do not request approval for routine, reversible implementation choices already within authorized scope. Use the operation-specific authorization matrix in `harness/config/verification-policy.yaml`. Authorization for file edits does not authorize installations, downloads, external service calls, commits, pushes, deployments, infrastructure changes, or destructive actions. Record scope-specific approvals in target project state. Do not perform external publication, deployment, push, or other separately restricted actions without explicit authorization.

## Evidence and privacy

Keep project-specific records in the target repository. Do not put target code, personal data, credentials, secrets, or unnecessary assessment details in this reusable repository. Never include secret values in logs or reports.

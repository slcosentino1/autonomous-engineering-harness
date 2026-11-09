# Autonomous Engineering Harness — Working Instructions

## Purpose and boundaries

This repository defines a reusable, language- and architecture-neutral engineering workflow. It is not the product repository. Product source, tests, migrations, deployment configuration, credentials, and project-specific requirements belong in the target repository.

Before work begins, identify the target repository explicitly and read its local instructions (for example, `AGENTS.md`, `README.md`, contribution guides, and build documentation). Apply target-repository instructions to target work. Do not assume the harness repository's conventions govern the product.

The target path must be supplied explicitly. Verify canonical Git roots, common directories, refs stores, and object stores/alternates are independent from the harness; fail closed if this cannot be proven. At workflow initialization, `harness/orchestrator.py` records one versioned execution policy bound to the canonical target and workflow ID. Reuse it for ordinary in-workspace engineering operations across all internal stages and recovery; do not introduce per-file or per-operation approvals for those actions. The target is the only repository authorized for product modifications. See `docs/orchestrator.md` and `harness/config/verification-policy.yaml` for the operational contract and retained restrictions.

The orchestrator applies Codex file changes with `harness/safe_writer.py` under the persisted workspace policy. Ordinary assignment-related source, tests, configuration, documentation, metadata, generated files, and workflow artifacts can be created or edited anywhere beneath the target root without path-prefix or extension allowlists. Workflow-created files can be renamed or deleted. Preserve unrelated pre-existing files and user changes; never write outside the target, into Git metadata, sensitive credential/environment paths, or nested repositories. Safe Writer retains canonical-root, no-follow, hard-link, expected-content, identity, and operation-evidence checks. Directory creation may traverse verified existing directories. Its final atomic rename is not a compare-and-swap and cannot prevent an arbitrary writer racing after its last check; stop and revalidate if an unexpected change appears.

## Default task operation

For a shell-launched, stage-controlled assignment with an absolute target root, existing branch, workflow configuration, and task file, use `python3 <harness-root>/harness/orchestrator.py` as the stage controller. It runs read-only preflight, persists target-local state, launches bounded read-only Codex planning/implementation/correction sessions, executes configured validations, and calls Claude only through `harness/claude_reviewer.py`. The controller, not a Codex child session, advances stages. See [`docs/orchestrator.md`](docs/orchestrator.md) for the CLI and configuration contract.

For a task being handled directly in an interactive Codex session, use [`harness/prompts/06-autonomous-single-agent.md`](harness/prompts/06-autonomous-single-agent.md) as the primary operating instruction. That direct-session mode owns discovery through closeout and follows the lifecycle and phase prompts. In either mode, do not stop after discovery, a proposal, or a plan, and do not ask for routine approval of in-scope implementation choices, file changes, tests, or increments.

`harness/orchestrator.py` is the only supported harness workflow engine. `harness/runner.py` is deprecated legacy code and must not be used as an alternate execution route. The workspace policy authorizes ordinary development commands, approved project dependency installation, local Git inspection and exact-path staging, internal transitions, and bounded recovery. It does not authorize secret exposure, unrelated path access, privileged execution, paid external operations, arbitrary external service access, commits, pushes, merges, deployments, publication, or infrastructure changes. Command filtering is a guardrail, not OS-level process confinement. Neither the engine nor a Codex session overrides OS or organization/provider policy.

If the target root is missing or ambiguous, ask for it; never infer it from the harness working directory. Pause only for genuine product ambiguity, material decisions that cannot safely be inferred, authorization required by policy, or unresolved blockers. In stage-controlled mode, Codex children do only the bounded task they receive and return the requested JSON; they never change workflow state, invoke Claude directly, or control transitions. The adapter invokes Claude in read-only review mode and records the result. Codex's review of Claude findings is self-review, never independent review. It cannot satisfy a policy requirement for an authenticated human reviewer or gate approver. If required human authority is unavailable, preserve the workflow as blocked/incomplete at the required phase. Never fabricate a report, identity, approval, or gate result.

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

Do not request approval for routine workspace edits, development commands, approved package-manager installs, workflow evidence, normal internal transitions, or bounded review corrections already covered by the initialized policy. Use the operation-specific authorization matrix in `harness/config/verification-policy.yaml` for actions outside it. Preserve pre-existing unrelated user data. Do not expose or transmit secrets or perform external publication, deployment, push, commit, merge, paid work, or other separately restricted actions without explicit authorization.

## Evidence and privacy

Keep project-specific records in the target repository. Do not put target code, personal data, credentials, secrets, or unnecessary assessment details in this reusable repository. Never include secret values in logs or reports.

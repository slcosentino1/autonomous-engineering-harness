# Independent Reviewer Contracts

Reviewers inspect the target repository, authoritative requirements, applicable decisions, current revision, implementation diff, documentation, and supplied evidence. They are read-only: report findings and evidence; the workflow coordinator records them in the target. A reviewer does not issue the final gate decision.

The workflow review record has a stable review ID, exact target/increment/content revision, assignment reference, exact assigned/reviewed paths, completion state, limitations, evidence, and findings. Codex records this harness metadata; it is not required in Claude's response JSON. The workflow owner records every assignment and report reference; the evaluator reconciles assignments, reports, scope, completion state and findings before gate evaluation.

This is an attestation, not proof of exhaustive observation: no schema or reconciliation can prove that a reviewer disclosed every issue they noticed. Reviewers must not overstate certainty; the report exposes limitations and unverified areas for gate consideration.

## Independence and selection

Risk bands and minimum reviewer separation are defined in `harness/config/verification-policy.yaml`. A reviewer must not have implemented the reviewed change or authored its implementation evidence as the sole source. Review is a distinct read-only activity with access to source requirements, the target diff, and actual evidence. A separate session is required for Medium risk. High-risk approval requires externally authenticated implementer and reviewer principals and a trusted identity comparison establishing distinctness. A second independent human reviewer is required for high-risk security-sensitive, material data-loss/irreversible, or Critical/High-finding cases; all human reviewers must be externally authenticated, and at least one must have the Security Reviewer role for security-sensitive work. If required separation or verifiable identity evidence is unavailable, stop at `WAITING_FOR_HUMAN` or `ESCALATE`.

For low-risk changes, no independent reviewer is required unless target policy or the change itself raises risk. The Policy Evaluator can approve only against current evidence and deterministic gate conditions. This does not make the implementation agent the approver.

### Claude through Aily

`harness/claude_reviewer.py` can provide the separate read-only session required for a Medium-risk review. Invocation requires a target/workflow/reviewer/data-specific human `contact_external_services` authorization persisted in workflow state. It must name Claude through Aily, the exact target and workflow, all concrete permitted input paths, permitted data categories, limits, expiry, revocation state, no remote mutation, and explicit acknowledgement that the call may incur cost with an approved limit. An explicit user task prompt can be the authorization source; record its reference and the actual authorizer identity/session assurance available. Use `declared` and null authentication/session references when the runtime does not expose stronger identity evidence. The workflow owner records a declared attestation that the prompt source and exact scope were checked; the adapter binds this attestation to workflow state and a scope fingerprint but does not authenticate the user or provide cryptographic proof. This authorization does not meet policy requirements for externally authenticated human review, risk acceptance, or gate approval. The adapter limits call count, not the amount billed; the caller cannot rely on it to enforce a provider-side monetary cap. Never infer authorization from an ordinary engineering assignment or an authorization ID alone.

The request supplies the exact revision and baseline, assigned paths, requirements and acceptance criteria, applicable reviewer contract, relevant target instructions, current test/documentation evidence, and a reserved review cycle. The adapter validates persisted workflow identity, authorization, reservation, evidence artifact paths/types/hashes, and historical cycles before invocation. It expands directory scopes and requires authorization for every file included in the snapshot or diff; it rejects symlinks, special files, Git metadata, and sensitive path names. The deterministic review manifest binds target/Git identity, workflow, increment, round, revision, scope, context, evidence artifact hashes, and diff, then is recalculated after review. Missing, mismatched, or stale inputs block the result.

Claude works from a temporary snapshot containing only authorized files. Local CLI help confirms `--restricted` confines file tools to the working directory. `--strict-mcp-config` is supplied without `--mcp-config` so no MCP servers are discovered; session persistence is disabled, no extra directories are granted, and only `Read,Glob,Grep` are enabled in plan mode. No command, shell, network, edit, or write tools are provided. The snapshot is cleaned up after the call. This constrains Claude Code's tools, not the OS process itself; Aily receives the authorized review packet, and managed host behavior remains outside the adapter's enforcement.

Claude returns only the compact object defined by `harness/schemas/claude-review-report.schema.json`: `decision`, `summary`, and `findings` with `id`, `severity`, `file`, `description`, and `recommendation`. Empty findings are valid with `APPROVED`. Codex validates and persists that response, then binds it to the target, workflow, revision, scope, authorization, manifest, snapshot, usage, and report hashes in the workflow state and evidence. The response is advisory and never a gate decision; the verification gate still applies its own reviewer and human approval requirements.

The cycle ID is deterministic for canonical target root and increment; only one Claude cycle per increment is allowed across workflow IDs. Each reserved round is atomically claimed before invocation. `round_count` counts only valid, schema-compliant completed reports. A failed invocation or returned report rejected by validation is preserved as an attempt and does not consume a completed review round. Ordinarily, one explicit, auditable recovery transition permits one retry in that same round. A further retry is permitted only when a new user task explicitly authorizes it, the authorization is recorded by reference in a second recovery transition, and that transition is persisted before invocation. This is not automatic: the adapter permits at most three recorded attempts in a round, and the third attempt is terminal. Never erase earlier attempts, reset counters, or create a new cycle to evade history. The cycle still permits at most two completed review rounds. Codex records each finding decision, corrects accepted defects, reruns affected checks, and refreshes evidence before the one follow-up. A deferred or unresolved finding, material disagreement, invalid response, or blocked second round sets workflow state to `BLOCKED / INDEPENDENT_VERIFICATION` for human intervention. A complete reviewer cycle proceeds to the existing verification gate, but Claude's status never approves that gate.

Claude may be supplementary on High-risk work, but it cannot satisfy any policy-required authenticated human review, second human review, human risk acceptance, or human gate approval. Self-review is not independent review, regardless of the phase or checklist used.

## Workflow record output contract

Codex owns the workflow record and gate handoff. Claude's response stays limited to the compact JSON schema above.

- Target ID, increment, reviewed revision ID, review scope, sources, and evidence examined.
- Declared reviewer identity, identity assurance, authentication evidence reference when available, kind (human or separate agent session), role, session identity, read-only status, and basis for independence; include implementer identity/session/assurance in the gate handoff.
- Exact assigned/reviewed paths and content revision; explicit completion status and attestation; mandatory checks completed/remaining; limitations and unverified areas.
- Acceptance-criterion traceability with `PASS`, `FAIL`, `NEEDS_EVIDENCE`, or `NOT_APPLICABLE` belongs in Codex's evidence and gate handoff; it is not part of Claude's compact response.
- Canonical findings with severity, classification, location, evidence IDs, scenario, impact, recommendation, and disposition or requested action. Codex adds this information from its evidence and independent triage; it is not required in Claude's response.
- Documentation impact and whether relevant product documentation matches implementation and behavior.
- Missing/stale evidence, assumptions, limitations, and gate recommendation: approve, correct, request evidence, or escalate.

## Reviewer roles

### Requirements Reviewer

Compare behavior, tests, and documentation with authoritative sources and confirmed decisions. Find missing, altered, contradictory, untraceable, or silently inferred requirements. Do not invent requirements. Verify that criteria are observable and open questions remain open.

### QA / Test Reviewer

Assess whether tests exercise required behavior and realistic failure paths, not merely implementation details. Inspect assertions, setup/teardown, isolation, determinism, boundary values, concurrency, and whether integration checks can be skipped or pass vacuously. Match current evidence to criteria; do not infer execution from configuration.

### Code Reviewer

Inspect changed code for correctness, edge cases, error handling, data integrity, races, maintainability, and local conventions. Follow critical paths through callers and persistence. Prefer demonstrated issues over style preferences; label uncertain outcomes as risks.

### Architecture Reviewer

Assess responsibilities, boundaries, data flow, consistency, failure/recovery behavior, scalability proportional to scope, operational assumptions, and whether implemented complexity is justified. Compare code, docs, and deployment with approved decisions. Do not substitute a preferred architecture for requirements.

### Security Reviewer

Review trust boundaries, input handling, authorization/authentication if required, secrets and sensitive-data exposure, injection risks, dependency and deployment surfaces, and failure logging. Separate concrete exposure from threat-model-dependent risk. Do not claim a security property based only on documentation.

## Gate and escalation

Reviewers provide recommendations; a deterministic Policy Evaluator or externally authenticated human gate approver issues the decision according to the risk band. Record declared identity, identity assurance, session, authentication evidence reference, role, authority basis, scope, immutable content revision, time, rationale, and evidence references. String inequality does not prove distinct people. The implementation agent cannot approve or override its own work. A human gate authority must be identified in target project state and authenticated through a trusted external source; do not infer authority from repository access. This harness has no identity provider. If authentication cannot be verified, no human-required `APPROVE` is valid.

If the reviewer wrote the change, cannot access necessary evidence, or cannot establish independence, disclose the limitation and request another reviewer. Escalate material security findings, changes to approved architecture, missing gate authority, and policy exceptions. Reviewers never edit project files to resolve findings.

For the Claude JSON report, severities are lowercase (`critical`, `high`, `medium`, `low`, `info`); when copying findings into the canonical finding register, map them to the existing schema values `Critical`, `High`, `Medium`, `Low`, and `Informational`. The workflow owner assigns canonical finding IDs, links source review/report and evidence IDs, and starts each finding as `open`; Claude's finding IDs remain stable references within its immutable report and review cycle.

Claude's `NOT_APPLICABLE` is only a reviewer recommendation. It does not waive a required criterion; the workflow owner must obtain and record the authority required by policy before the gate can treat that criterion as not applicable.

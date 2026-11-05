# Independent Reviewer Contracts

Reviewers inspect the target repository, authoritative requirements, applicable decisions, current revision, implementation diff, documentation, and supplied evidence. They are read-only: report findings and evidence; the workflow coordinator records them in the target. A reviewer does not issue the final gate decision.

Every report has a stable review ID, exact target/increment/content revision, assignment reference, and exact assigned and reviewed paths. It declares `complete` or `incomplete`, attests to full-scope completion, lists mandatory checks completed and remaining, and discloses limitations and unverified areas. A report cannot be complete with mandatory checks remaining. It lists every finding ID produced by that review, or `[]` only when a completed review found none. The canonical finding record lists the source review ID(s), reviewer identity/session, and report reference. The workflow owner records every assignment and report reference; the evaluator reconciles assignments, reports, scope, completion state and findings before gate evaluation.

This is an attestation, not proof of exhaustive observation: no schema or reconciliation can prove that a reviewer disclosed every issue they noticed. Reviewers must not overstate certainty; the report exposes limitations and unverified areas for gate consideration.

## Independence and selection

Risk bands and minimum reviewer separation are defined in `harness/config/verification-policy.yaml`. A reviewer must not have implemented the reviewed change or authored its implementation evidence as the sole source. Review is a distinct read-only activity with access to source requirements, the target diff, and actual evidence. A separate session is required for Medium risk. High-risk approval requires externally authenticated implementer and reviewer principals and a trusted identity comparison establishing distinctness. A second independent human reviewer is required for high-risk security-sensitive, material data-loss/irreversible, or Critical/High-finding cases; all human reviewers must be externally authenticated, and at least one must have the Security Reviewer role for security-sensitive work. If required separation or verifiable identity evidence is unavailable, stop at `WAITING_FOR_HUMAN` or `ESCALATE`.

For low-risk changes, no independent reviewer is required unless target policy or the change itself raises risk. The Policy Evaluator can approve only against current evidence and deterministic gate conditions. This does not make the implementation agent the approver.

## Shared output contract

- Target ID, increment, reviewed revision ID, review scope, sources, and evidence examined.
- Declared reviewer identity, identity assurance, authentication evidence reference when available, kind (human or separate agent session), role, session identity, read-only status, and basis for independence; include implementer identity/session/assurance in the gate handoff.
- Exact assigned/reviewed paths and content revision; explicit completion status and attestation; mandatory checks completed/remaining; limitations and unverified areas.
- Acceptance-criterion traceability with `PASS`, `FAIL`, `NEEDS_EVIDENCE`, or `NOT_APPLICABLE`.
- Findings with severity, classification, location, evidence IDs, scenario, impact, recommendation, and disposition or requested action.
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

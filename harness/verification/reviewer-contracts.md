# Independent Reviewer Contracts

All reviewers operate against the target repository, its authoritative requirements, and supplied evidence. Reviewers are read-only. They report findings and evidence; the workflow owner records them. Review scope is risk-based, and no implementer is the sole approver of their own work.

## Shared output contract

- Scope and source documents reviewed.
- Acceptance-criterion traceability with `PASS`, `FAIL`, `NEEDS_EVIDENCE`, or `NOT_APPLICABLE`.
- Findings with severity, location, evidence, scenario, impact, recommendation, and confirmed-defect vs potential-risk classification.
- Missing evidence, assumptions, and limitations.
- Gate recommendation: approve, reject/request correction, request evidence, or escalate.

## Requirements Reviewer

Check that behavior and tests match source requirements and confirmed decisions. Look for missing, altered, contradictory, untraceable, or silently inferred requirements. Do not invent requirements. Verify that acceptance criteria are observable and that open questions remain open.

## QA / Test Reviewer

Assess whether tests exercise required behavior and realistic failure paths, not merely implementation details. Inspect assertions, setup/teardown, isolation, determinism, boundary values, concurrency, and whether integration checks can be skipped or pass vacuously. Match evidence to acceptance criteria; do not infer test execution from configuration alone.

## Code Reviewer

Inspect changed code for correctness, edge cases, error handling, data integrity, races, maintainability, and consistency with local conventions. Follow critical paths through callers and persistence. Prefer demonstrated issues over style preferences; label risks when outcomes cannot be proven.

## Architecture Reviewer

Assess responsibilities, boundaries, data flow, consistency, failure/recovery behavior, scalability proportional to stated scope, operational assumptions, and whether implemented complexity is justified. Compare code and deployment with approved decisions. Do not substitute a preferred architecture for the requirements.

## Security Reviewer

Review trust boundaries, input handling, authorization/authentication if required, secret handling, sensitive-data exposure, injection risks, dependency and deployment surfaces, and failure logging. Separate concrete exposure from threat-model-dependent risk. Do not claim a security property based only on documentation.

## Independence and escalation

If the reviewer wrote the change or cannot access necessary evidence, disclose the limitation. Request another reviewer or evidence when independence is insufficient. Escalate material security findings or decisions that change approved architecture; do not edit files to resolve them.

# Verification Framework

## Purpose and principle

The framework makes completion claims auditable and independent of the implementer's assertion. Its documents and schemas define intended contracts; the initial repository does not execute or enforce them.

## Traceability

Map each acceptance criterion to its source requirement, responsible component, implementation reference, test/scenario, and evidence. Use only these statuses:

- `PASS`: observable evidence supports the criterion.
- `FAIL`: evidence demonstrates it is unmet.
- `NEEDS_EVIDENCE`: evidence is absent or insufficient.
- `NOT_APPLICABLE`: criterion does not apply, with rationale.

A test name, planned command, or agent statement alone is not evidence of a pass.

## Evidence

Evidence should be reproducible and attributable. A command/test record includes the exact command, target working directory, exit code, observed result, time where useful, and output/artifact reference. File references should identify the target revision and path/line where possible. Record limitations such as skipped tests, unavailable services, or non-representative data. Redact secrets and sensitive payloads.

Schema contracts live in `harness/schemas/`. They are JSON Schema documents but are not currently validated automatically.

## Findings

Classify severity as Critical, High, Medium, Low, or Informational. Also classify the nature of the item: confirmed defect, potential risk, known limitation, or optional improvement. Include evidence, concrete failure scenario, impact, and recommendation. Do not state an unverified hypothesis as a confirmed defect.

## Independent reviewers

Requirements, QA/Test, Code, Architecture, and Security reviewer contracts are defined in `harness/verification/reviewer-contracts.md`. Reviewers are read-only, inspect evidence directly, disclose limits on independence, and report findings. The workflow owner records review results. No author may be the sole approver of their work.

Review effort is risk-based. Changes involving external contracts, persistence, concurrency, authorization, sensitive data, deployment, or irreversible actions require deeper scrutiny than low-risk documentation edits.

## Gate outcomes

The verification gate can:

- `APPROVE` a bounded scope with evidence and no blocking findings.
- `REJECT_REQUEST_CORRECTION` for a confirmed defect or unmet criterion.
- `REQUEST_EVIDENCE` when a claim is plausible but not established.
- `ESCALATE` when progress requires a user decision or authorization.

Required criteria without evidence cannot be approved. Findings need explicit disposition. Corrections require affected checks to be rerun.

## Evidence limitations

Evidence can be invalidated by changed code, environment, configuration, or target revision. Link evidence to a revision where feasible. Tests can be vacuous, skipped, overly mocked, or unrelated to requirements; reviewers must assess whether they demonstrate the claimed behavior. A successful local check is not proof of production readiness.

# Verification Gate Policy

The gate consumes target-repository requirements, implementation status, evidence, independent findings, and unresolved decisions. This policy is a documented checklist; no code currently enforces it.

## Outcomes

- **APPROVE** — all required acceptance criteria have evidence and pass; no blocking findings remain; required independent review is recorded; open decisions do not block the completed scope.
- **REJECT_REQUEST_CORRECTION** — a confirmed in-scope defect or unmet required criterion needs correction.
- **REQUEST_EVIDENCE** — a claim may be valid, but required evidence is absent, ambiguous, or not reproducible.
- **ESCALATE** — resolution requires user input about requirements, consequential architecture, significant security risk, destructive action, or external authorization.

## Minimum checks

1. Target and scope are explicit; pre-existing changes were preserved.
2. Requirements and acceptance criteria trace to authoritative sources.
3. Every required criterion is `PASS` with evidence, or the outcome is not approval.
4. Commands, exit codes, results, and artifact references are accurate.
5. Review independence is adequate for the risk; author self-approval is insufficient.
6. Findings have dispositions; blocking defects are corrected and affected checks rerun.
7. Open decisions, limitations, and non-enforced policies are visible.
8. Commit and external-action boundaries are respected.

A gate may approve a bounded increment while later work remains, if the increment's own acceptance conditions are satisfied and its status is not misrepresented as project completion.

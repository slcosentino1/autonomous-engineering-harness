# Independent Review Report

- **Scope:** `<changes/increment>`
- **Target repository and immutable content revision:** `<path and content:<sha256>; commit SHA is provenance only>`
- **Target ID / increment:** `<...>`
- **Review ID:** `<stable unique ID for this read-only review>`
- **Assignment reference:** `<target-local approved reviewer assignment>`
- **Assigned scope paths:** `<exact repository-relative paths from assignment>`
- **Reviewed scope paths:** `<exact repository-relative paths actually examined>`
- **Implementation agent/session:** `<declared identity / session identity / assurance>`
- **Reviewer:** `<declared identity, kind, role, session identity, assurance, authentication evidence reference if available>`
- **Independence basis:** `<not implementer; separate read-only activity/session>`
- **Review date:** `<date>`
- **Read-only:** Yes / No (explain if no)

## Scope completion
- **Review completion:** `complete` / `incomplete`
- **Scope-completion attestation:** `<I completed the full assigned review scope: Yes/No>`
- **Mandatory checks assigned/completed:** `<list and evidence references>`
- **Mandatory checks remaining:** `<list; must be empty for complete>`
- **Limitations:** `<specific access, evidence, tool, or time limitations; none if none>`
- **Areas not verified:** `<specific areas; none if none>`

A reviewer may mark the review complete only when every mandatory check in the assignment was performed or was explicitly ruled not applicable by an authorized decision. An empty findings list is valid only for a completed review; an incomplete review must remain `incomplete` even when it found no issues.

## Evidence reviewed
- `<file, diff, command result, artifact>`

## Documentation reviewed
- `<documentation reference, accuracy against implementation, impact>`

## Traceability status
| Criterion | Status | Evidence | Gap |
|---|---|---|---|
| AC-001 | NEEDS_EVIDENCE | — | `<why>` |

## Findings
List the complete set of finding IDs produced by this report, including `[]` when a completed review found no findings. The canonical register must record this report as a source for each listed finding. The report must not equate record reconciliation with proof that every substantive observation was disclosed.

- **Complete finding IDs:** `<[F-001, ...] or []>`

| ID | Severity | Classification | Location | Evidence IDs / scenario | Impact | Recommendation | Disposition / authority |
|---|---|---|---|---|---|---|---|---|
| F-001 | Medium | potential_risk | `<file:line>` | `<evidence IDs and scenario>` | `<impact>` | `<action>` | `<status; human authority if accepted>` |

## Missing evidence and limitations
- `<item>`

## Gate recommendation
`APPROVE` / `REJECT_REQUEST_CORRECTION` / `REQUEST_EVIDENCE` / `ESCALATE` — recommendation only; `<rationale and blocking severities>`

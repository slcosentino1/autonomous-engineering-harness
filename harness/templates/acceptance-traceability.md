# Acceptance Traceability

| Criterion | Requirement | Decision | Source | Component | Implementation reference | Documentation reference / impact | Test / scenario | Evidence IDs | Status | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| AC-001 | REQ-001 | `<DEC ID or —>` | `<source ref>` | `<component>` | `<file:line>` | `<path / updated or rationale for not applicable>` | `<test/command>` | `<evidence ID>` | NEEDS_EVIDENCE | — |

Allowed status values: `PASS`, `FAIL`, `NEEDS_EVIDENCE`, `NOT_APPLICABLE`. A `PASS` requires current evidence IDs for this target, increment, and revision. Every criterion records documentation impact as `updated`, `not_applicable` with rationale, or `needs_update`.

Record the immutable `content:<sha256>` revision identity for the traceability snapshot. Evidence and reviewer reports bind to that identity; a commit SHA is separate provenance and cannot replace it.

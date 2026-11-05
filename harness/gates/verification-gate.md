# Verification Gate Policy

This gate consumes target-local requirements, implementation and documentation state, current evidence, review findings, risk dispositions, and unresolved decisions. It is a documented policy; no code currently enforces it. The detailed defaults are in `harness/config/verification-policy.yaml` and `docs/verification-framework.md`.

## Decision authority

- The **Policy Evaluator** can approve a low-risk increment when all automatic criteria below pass. It can approve medium-risk work only after the required separate review and when no risk acceptance is requested. The implementation agent cannot issue or override approval.
- Medium-risk work needs a separate-agent/session read-only review. The Policy Evaluator may approve only when that review and all other gate conditions pass and no risk acceptance is requested.
- High-risk work needs externally authenticated implementer and human reviewer principals plus an externally authenticated human gate approver; compare trusted subject identifiers to prove distinctness. Security-sensitive, material data-loss/irreversible, or Critical/High-finding cases need a second independent human reviewer; security-sensitive work also needs a Security Reviewer.
- Human approval is required to accept Medium risk, approve High-risk work, waive a criterion, resolve consequential product/security decisions, or authorize a restricted operation.
- Declared identity and session strings are not authentication. Human approvals only count when a trusted external source verifies identity and authority; no such source is implemented in the current harness. If evidence cannot be checked, `ESCALATE` and do not `APPROVE`.
- If target gate authority is not configured, `ESCALATE`; repository access alone does not confer approval authority.

## Blocking rules

- Open `Critical` or `High` findings always block `APPROVE`; they cannot be risk-accepted under this default policy.
- `Medium` findings block unless fixed or accepted as documented risk by an authorized human. Acceptance requires scenario, impact, compensating controls, residual risk, owner, review date, and authorizer; it cannot waive a failed required criterion or legal, regulatory, or security obligation.
- `Low` and `Informational` findings may be deferred with rationale, owner, and review date. All finding dispositions remain visible.
- Missing, stale, or mismatched required evidence prevents approval. Required criteria must be `PASS` on the reviewed revision.

Before review starts, workflow state records expected reviewer assignments and exact scopes. The evaluator derives required roles and any second-review condition, then checks each report against assignment, target, increment, immutable content identity, and exact paths. Reports explicitly declare complete/incomplete status, full-scope attestation, mandatory checks completed/remaining, limitations, and unverified areas. Incomplete reports or reports with required checks remaining block approval; an empty findings list is meaningful only for a completed review. The evaluator reconciles assignments, all reports, finding-ID summaries/tables, and the canonical register. This proves record consistency, not substantive exhaustiveness: a schema cannot prove a reviewer disclosed every observation.

The gate decision and all evidence bind to `content:<sha256>` computed from the original baseline commit retained in workflow state and a canonical manifest of `revision_paths`. Commit SHAs are separate provenance. Any revision identity change makes prior evidence and reports ineligible for the new gate; rerun checks selected by impact analysis, create current evidence for every required criterion, and obtain every required review on the final identity. After an optional authorized commit, the workflow records equivalence only if a fresh manifest using the same original baseline matches the approved content identity. `COMPLETED` cannot refer to a different identity.

The evaluator then derives blocking and accepted-risk ID sets from every canonical record and compares the exact sets with the decision. It must not trust caller-supplied lists. `open` findings block at every severity; `deferred` Critical, High, and Medium findings remain unresolved and block; `deferred` Low/Informational findings may be non-blocking only with owner, rationale, and review date. Critical/High can be resolved only by a valid `fixed` or `not_reproducible` disposition with current evidence and reviewer disposition.

## Outcomes

- **APPROVE** — required criteria pass with current evidence; required reviews and documentation checks are recorded; no blocking finding or pending decision remains; decision authority is valid for the risk band.
- **REJECT_REQUEST_CORRECTION** — confirmed in-scope defect, failed required criterion, or open Critical/High finding requires correction.
- **REQUEST_EVIDENCE** — evidence is missing, stale, ambiguous, or not reproducible.
- **ESCALATE** — resolution requires user input, authorized risk acceptance, a target gate authority, a consequential decision, or permission for a restricted action.

## Minimum checks

1. Target canonical root, working directory, scope, baseline, and pre-existing work are recorded and valid.
2. Applicable target instructions and selected skills are recorded; no instruction has been used to widen authorization.
3. Requirements and criteria trace to authoritative sources and applicable decisions.
4. Implementation and documentation references trace to criteria; documentation matches the reviewed behavior.
5. Evidence IDs exist exactly once for this target/increment and are bound to the reviewed content identity and evidence scope. The revision manifest reference resolves and its path list and fingerprint match the approved `revision_paths`. Validate cross-record links outside JSON Schema.
6. Commands, working directories, exit codes, observed results, and artifact references are accurate and authorized.
7. Reviewer declared identity, identity assurance, role, kind, session, assigned/reviewed scope and revision, completion attestation, limitations, and approval identity/role/authentication evidence meet the risk-band rules. Do not treat unequal strings as proof of different people.
8. The complete findings register reconciles with every reviewer report; the evaluator derives blocking/accepted-risk lists, which exactly match the gate decision. Findings meet severity/disposition rules; after a revision identity change, all required reviewer reports must address the final identity and every required criterion must have current evidence.
9. Pending decisions, limitations, authorizations, and non-enforced controls are visible.
10. Commit and external-action boundaries are respected; any optional commit is separately authorized and its manifest is proven equivalent to the approved content identity before completion.

A bounded increment may pass while later increments remain, provided its own criteria pass and the decision is not represented as project completion.

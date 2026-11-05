# Engineering Build Log

## Current status
- Target repository: `<path/name>`
- Canonical Git root / target ID: `<...>`
- Authorized working directory and scope: `<...>`
- Baseline commit / working-tree fingerprint: `<...>`
- Verification content identity / manifest: `<content:<sha256> / target-local manifest reference>`
- Current increment: `<ID/name>`
- Overall workflow status: `<ACTIVE / WAITING_FOR_HUMAN / BLOCKED / COMPLETED>`
- Current phase: `<phase>`
- Gate authority and risk band: `<identity/role or pending / low|medium|high>`

## Activity — `<date and increment>`
- **Changes:** `<what changed, with references>`
- **Requirements:** `<IDs addressed>`
- **Instructions and capabilities applied:** `<source references, capability, phase; none if absent>`
- **Documentation impact:** `<documents updated or rationale why none were needed>`
- **Authorizations:** `<operation, exact scope, declared human identity/role, external authentication reference, authority reference, source, time, expiry/revocation>`
- **Validations run:**
  - Command: `<exact command>`
  - Working directory: `<path>`
  - Exit code/result: `<observed result>`
  - Evidence: `<artifact or output reference>`
- **Evidence revision/scope:** `<content:<sha256>, source state, optional commit SHA, scope-specific fingerprint and covered paths>`
- **Review reports and complete finding sets:** `<review IDs, assignments, content revision, exact scope paths, declared/authenticated identity, session, completion attestation, mandatory checks, limitations, unverified areas, exact finding IDs including [] only for complete empty reviews>`
- **Review findings and disposition:** `<finding IDs and status>`
- **Canonical findings register:** `<target-local path and fingerprint>`
- **Gate decision:** `<outcome, implementer declared identity/session/assurance, reviewer IDs/assignments/completion and identity assurance, externally authenticated authority and evidence, approved content revision, manifest reference, rationale>`
- **Commit equivalence:** `<optional commit SHA and reference proving its revision_paths manifest equals the approved content identity>`
- **Open decisions / blockers:** `<none or detail>`
- **Next step:** `<authorized next action>`

Never record an unexecuted check as passed. Keep historical entries; append or correct with an explicit note rather than erasing relevant evidence.

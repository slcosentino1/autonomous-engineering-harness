# Harness Decisions

These decisions govern the reusable harness repository. They do not dictate a target project's stack or architecture.

## DEC-H-001 — Keep the harness separate from target products
- **Status:** Accepted
- **Context:** The workflow must be reusable across existing and new projects without mixing its own instructions with product code or requirements.
- **Decision:** Store generic workflow assets here; keep product code, tests, deployment, secrets, requirements, and project records in each target repository.
- **Consequences:** Target discovery and path selection must be explicit. Examples should not become implicit product dependencies.

## DEC-H-002 — Begin document-first and language-neutral
- **Status:** Accepted
- **Context:** A reusable workflow needs to work across technology stacks before automation assumptions are known.
- **Decision:** The initial implementation consists of documentation, prompts, contracts, schemas, and templates. Do not select a language, framework, database, or orchestrator for the harness runtime.
- **Consequences:** Procedures are not technically enforced; executable automation remains planned.

## DEC-H-003 — One agent first; future roles use explicit contracts
- **Status:** Accepted
- **Context:** Initial use should be simple while leaving room for later independent review and coordination.
- **Decision:** Design the workflow for one Codex session and define reusable reviewer/handoff responsibilities; do not implement multi-agent coordination yet.
- **Consequences:** Independent review may require a separate session or person. No framework such as LangGraph is introduced.

## DEC-H-004 — Evidence-backed completion and independent review
- **Status:** Accepted
- **Context:** Completion cannot rely only on an implementer's report.
- **Decision:** Require traceable criteria, observed evidence, read-only independent review proportional to risk, and explicit gate outcomes. The author cannot be the sole approver.
- **Consequences:** Missing evidence remains visible as `NEEDS_EVIDENCE`; review findings are recorded by the workflow owner.

## DEC-H-005 — External actions require distinct authorization
- **Status:** Accepted
- **Context:** Local engineering work and external side effects have different authorization boundaries.
- **Decision:** Use the operation-specific matrix in `harness/config/verification-policy.yaml`. Authorization is limited to a named operation, resource, scope, and period. File-write authorization does not authorize installs, downloads, Git history/remote operations, deployments, external service calls, or destructive changes.
- **Consequences:** A local commit does not imply authorization to push or deploy. Missing or ambiguous authorization stops the operation and is recorded as a pending decision.

## DEC-H-006 — Explicit and disjoint target boundary
- **Status:** Accepted
- **Context:** The harness must operate across external repositories without mixing product work into reusable assets.
- **Decision:** Require an explicit repository-root target path, canonical Git root, authorized working directory, and explicit code/artifact write allowlists. The canonical target and harness roots, common Git directories, refs stores, object stores/alternates, and hard-linked Git data must be disjoint; inability to prove this fails closed. Product artifacts belong in the target repository and follow its existing layout. An empty included-path allowlist grants no writes.
- **Consequences:** A future runner must enforce Git-store, path, symlink, hard-link, and allowlist boundaries before every write. A nested independent repository is a separate target; distinct worktree paths do not prove independent Git repositories.

## DEC-H-007 — Risk-based gate authority
- **Status:** Accepted
- **Context:** Requiring a human for every small increment is unnecessary, but an implementer cannot approve their own work.
- **Decision:** A deterministic policy evaluator may approve eligible low-risk work; medium-risk work needs a separate agent/session review; high-risk work needs an externally authenticated human reviewer and designated externally authenticated human gate authority. Human authorization is required for risk acceptance and consequential decisions.
- **Consequences:** The target must identify its gate authority and provide verifiable authentication evidence for human decisions. Missing authority or authentication escalates rather than being inferred from repository access or unequal strings.

## DEC-H-008 — Continuous, change-based product documentation
- **Status:** Accepted
- **Context:** Product documentation must remain useful and reflect implementation without creating redundant files.
- **Decision:** Assess documentation impact in every increment and update the smallest relevant existing target document alongside material behavior, architecture, interface, data, security, operation, or maintenance changes.
- **Consequences:** Documentation references and accuracy are part of traceability, review, and the gate when relevant; trivial changes do not require new documentation.

## DEC-H-009 — Gate decisions bind to immutable content identity
- **Status:** Accepted
- **Context:** A worktree revision and a Git commit SHA identify different things. A later commit or worktree edit must not silently change the subject of an approval.
- **Decision:** The primary revision identifier is `content:<sha256>` over a canonical manifest of explicit product code, tests, configuration, product documentation, source requirements, and approved decisions (`revision_paths`) plus the baseline commit. Evidence, reviews, and the Verification Gate bind to that identity. An optional later commit is provenance only and is equivalent to the approved revision only if a fresh manifest over the exact approved path list matches exactly.
- **Alternatives considered:** Committing before final verification and binding the gate directly to the commit SHA would require a separately authorized commit before verification. The content identity model is selected because local commits are optional and separately authorized, while it still gives the gate an immutable subject.
- **Consequences:** The original baseline commit remains fixed when recomputing identity, including after an optional commit. A changed revision path makes prior evidence and review reports ineligible for the new gate; checks may be selected by impact analysis, but current evidence is required for every criterion and all required reviewers must report on the final identity. Workflow records and evidence are excluded from the content manifest to avoid a self-referential revision hash, but remain bound to that revision by references.

## DEC-H-010 — Identity strings are declarations, not authentication
- **Status:** Accepted
- **Context:** Reviewer and authority records need identity and session information, but a schema cannot authenticate a person or prove two different strings represent different people.
- **Decision:** Record declared identity and session separately from external authentication evidence. Where policy requires a human reviewer, authorization, risk acceptance, or gate approval, require a verifiable reference to a trusted external authentication source. If that evidence cannot be verified, do not treat the approval as authenticated; escalate. Do not select or implement an identity provider in the document-only harness.
- **Consequences:** Current policies and schemas describe required evidence but cannot verify it. Selecting an identity source is a future implementation decision.

## Open questions

No runtime, automation, or external identity provider is selected. The trusted identity source and target-specific human gate authority must be identified for each engagement before human-required approvals can be considered verified. Revisit runtime choices only when evidence from representative projects establishes a concrete need.

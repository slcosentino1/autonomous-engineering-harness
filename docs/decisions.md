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
- **Decision:** Establish one workspace-scoped execution policy at workflow initialization. It authorizes ordinary project edits, development commands, manifest-based dependency installation through approved package managers, local Git inspection and exact-path staging, workflow artifacts, and bounded internal review/correction. Separate authorization remains required for direct downloads, arbitrary external calls, paid services, commits, pushes, merges, deployments, publishing, infrastructure changes, and destructive operations outside workflow-created files.
- **Consequences:** A local commit does not imply authorization to push or deploy. Missing or ambiguous authorization stops the operation and is recorded as a pending decision.

## DEC-H-006 — Explicit and disjoint target boundary
- **Status:** Accepted
- **Context:** The harness must operate across external repositories without mixing product work into reusable assets.
- **Decision:** Require an explicit repository-root target path, canonical Git root, and one versioned workspace policy bound to the workflow ID. The canonical target and harness roots, common Git directories, refs stores, object stores/alternates, and hard-linked Git data must be disjoint; inability to prove this fails closed. Product artifacts belong in the target repository and follow its existing layout. Exact code/artifact path allowlists and extension restrictions are not required in workspace mode.
- **Consequences:** Every normal project file operation is contained by the canonical workspace and retained exclusions. Preserve pre-existing user changes; permit relevant existing source, tests, configuration, and documentation to change under the assignment. Do not access unrelated paths or nested repositories. Legacy explicit-scope mode remains for standalone preflight compatibility.

## DEC-H-007 — Risk-based gate authority
- **Status:** Accepted
- **Context:** Requiring a human for every small increment is unnecessary, but an implementer cannot approve their own work.
- **Decision:** A deterministic policy evaluator may approve eligible low-risk work; medium-risk work needs a separate agent/session review; high-risk work needs an externally authenticated human reviewer and designated externally authenticated human gate authority. Functional/product decisions require an explicit human response when project instructions say so; that response may have declared provenance. External data-transfer authorization, restricted operations, security-risk acceptance, criterion waivers, High-risk review, and gate approval retain their separate authorization and authentication requirements.
- **Consequences:** Record the decision category, applicable project instruction, response provenance, and actual identity assurance. Missing required external authority or authentication escalates rather than being inferred from repository access or unequal strings. A declared functional response never satisfies a High-risk gate.

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
- **Decision:** Record declared identity and session separately from external authentication evidence. Where policy requires a human reviewer, operational authorization, risk acceptance, or gate approval, require a verifiable reference to a trusted external authentication source. A functional/product decision may use declared provenance when the applicable target instruction permits explicit interactive acceptance; it does not grant operational authority or satisfy a High-risk gate. If required authentication evidence cannot be verified, do not treat the approval as authenticated; escalate. Do not select or implement an identity provider in the document-only harness.
- **Consequences:** Current policies and schemas describe required evidence but cannot verify it. Selecting an identity source is a future implementation decision.

## Open questions

No runtime, automation, or external identity provider is selected. The trusted identity source and target-specific human gate authority must be identified for each engagement before human-required approvals can be considered verified. Revisit runtime choices only when evidence from representative projects establishes a concrete need.

## DEC-H-011 — Conditional replacement of existing files on macOS
- **Status:** Superseded by DEC-H-012
- **Context:** The immutable write-safety rule in `harness/config/verification-policy.yaml` permits replacing an existing single-link file only when replacement is atomic and conditional on the destination still having the validated device/inode and link count, or when an exclusive lock is proven to cover every writer. Atomic rename alone does not establish that condition. The current harness has a read-only preflight and no target file writer.
- **Investigation:** The examined macOS primitives do not provide a destination-identity compare-and-swap for replacement. `RENAME_SWAP` atomically exchanges names but does not compare the displaced destination with the previously validated identity; a concurrent replacement can therefore be displaced. `RENAME_EXCL` safely supports create-only behavior and rejects an existing destination. `flock`/`fcntl` locks are advisory and do not constrain non-cooperating processes. `O_NOFOLLOW` and descriptor-relative path resolution address path traversal, not concurrent destination replacement. A post-swap check and rollback is not safe because another writer can race with the rollback.
- **Historical decision:** Under the then-current arbitrary-writer requirement, do not implement replacement using ordinary rename, swap-and-rollback, an advisory lock, or an unverified filesystem assumption. The user later explicitly selected the single-writer operating model in DEC-H-012.
- **Historical alternative considered:** A narrowly scoped write broker with an OS-enforced sole-writer boundary was considered but not selected under DEC-H-012. A cooperative lock, a second worktree, or a sandbox applied only to a broker would not establish that boundary.
- **Historical evidence requirement:** The prior arbitrary-writer model required protection from a non-cooperating writer racing after validation. That guarantee was explicitly dropped by DEC-H-012; current tests must cover changes detected before the final check and must not claim protection after it.
- **Historical consequence:** Existing-file replacement was blocked while arbitrary concurrent writers had to be covered. DEC-H-012 records the changed operational assumption and its narrower guarantee.

## DEC-H-012 — Single-writer file replacement model
- **Status:** Accepted
- **Context:** The platform does not provide the destination-identity compare-and-swap required by DEC-H-011. The authorized operating model is one primary Codex implementation process per active target workflow, with Claude review read-only and human/automation edits paused during writes.
- **Decision:** Permit workspace-authorized source, test, documentation, configuration, metadata, and generated-file operations after fresh preflight, canonical workspace validation, no-follow directory-relative resolution, expected-content verification for replacement, descriptor/entry identity and link-count checks, same-directory temporary write and fsync, an immediate recheck, atomic rename, directory fsync, and post-write verification. Workflow-created files may be renamed or deleted with recorded operation evidence. Do not add a writer broker or distributed lock.
- **Explicit limitation:** Atomic rename guarantees readers see a complete old or new file; it does not condition replacement on the old destination inode and does not prevent a non-cooperating writer racing after the final check. The policy depends on the single-writer assumption and must not claim protection against arbitrary concurrent writers. If unexpected changes are observed before a write, stop and revalidate. The helper preserves POSIX permission bits and owner/group where the OS permits; it does not copy extended attributes or ACLs.
- **Consequences:** `harness/safe_writer.py` is the supported write path for workspace-authorized creates, replacements, directory creation, and workflow-created-file rename/delete operations. It reruns read-only preflight, verifies the policy is bound to the target, uses stable directory descriptors and no-follow operations, and emits JSON evidence. Existing hard-link rejection, target/harness isolation, pre-existing user-change preservation, and external-action authorization remain in force. Exact classification and extension allowlists are legacy-only.

## DEC-H-013 — One auditable workspace execution policy
- **Status:** Accepted
- **Context:** Per-file scopes and approvals for ordinary builds, tests, dependency setup, generated files, and internal transitions repeatedly interrupt authorized engineering work.
- **Decision:** At initialization, persist a versioned policy bound to the canonical target root, workflow ID, task reference, and authorization time. Reuse it through planning, implementation, validation, self-review, independent review, correction, and recovery. Allow normal project operations without per-operation approval. Keep product/business decisions and separately restricted external, paid, publication, destructive, and identity-gated operations distinct from workspace authority.
- **Consequences:** `revision_paths` describe review evidence and are not write permissions. Pre-existing user changes are protected; source files may change when relevant to the assignment, and workflow-created generated files may be cleaned up. Plans can run increments independent of an open product decision before pausing dependent work. Commands are filtered guardrails, not OS-level confinement. Recovery follows changed-context and validation evidence without a bespoke one-use authorization.

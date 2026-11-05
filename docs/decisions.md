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
- **Decision:** Treat push, deployment, publication, external service contact, and destructive operations as separately authorized actions when required by the user or target policy.
- **Consequences:** A local commit does not imply authorization to push or deploy.

## Open questions

No runtime or automation technology is selected. Revisit only when evidence from representative projects establishes a concrete need.

# Engineering Lifecycle

This workflow is a reusable operating contract, not an executable pipeline. Apply it in the target repository and adapt validation to that project's language, tools, risk, and instructions.

## 0. Establish context

- Receive the assignment and explicit target path. Resolve the canonical Git root and verify it is disjoint from the harness; default the working directory to the target root.
- Read target instructions, assignment, current plans, status, and relevant source. Inspect applicable instructions along the path and inventory skills/agent guidance without installing or modifying them.
- Inspect working-tree changes before editing. Identify user work and preserve it.
- Record constraints, operation-specific authorization, available validation, documentation conventions, applied instructions/skills, and unresolved questions in target-local workflow state.

**Exit:** target, source of truth, constraints, baseline state, and working directory are known.

## 1. Discovery

Inventory relevant files, existing behavior, conventions, tests, external dependencies, skills, specialized agent instructions, and documentation. Inspect the purpose, scope, invocation conditions, and side effects of each capability before use. Separate facts from assumptions. Identify stale, incomplete, or duplicated docs and audit them before editing; do not replace established information wholesale.

**Exit:** concise evidence-backed current-state report and explicit gaps.

## 2. Requirements

Extract requirements from authoritative sources. Assign stable identifiers; link each to source location. Define testable acceptance criteria, constraints, and non-goals. Classify each statement as requirement, inference, confirmed decision, optional improvement, or open question. Resolve only questions authorized for resolution.

**Exit:** traceability baseline and open questions are visible; product owner approval is recorded where required.

## 3. Architecture and decisions

Describe components, boundaries, data ownership, interfaces, failure behavior, security, and deployment assumptions at the level needed to plan. Record consequential choices with context, alternatives, decision, and consequences. Preserve confirmed decisions.

**Exit:** implementation-critical behavior is decided or explicitly left open with a plan for handling it.

## 4. Implementation planning

Break work into small increments. For each, identify requirements, decisions, files/components/docs likely affected, dependencies, validations, documentation impact, and acceptance conditions. Sequence high-risk foundations first. Avoid committing future scope as present implementation.

**Exit:** plan is reviewable and approvals required by the target process are explicit.

## 5. Incremental implementation

For one authorized increment at a time: inspect relevant code and docs, make focused changes, update tests, and update relevant product documentation alongside material behavior, architecture, interface, data, security, operational, or maintenance changes. Avoid unrelated refactors and new Markdown files where existing docs suffice. Record implementation choices that resolve minor ambiguity. Recheck scope before closing the increment. Use `harness/safe_writer.py` with the approved scope config for target writes. It rejects multiply linked files, creates exclusively, and replaces existing files only after checking the workflow baseline hash and revalidating path, identity, link count, and content immediately before an atomic rename. This relies on one Codex writer; it does not protect against a non-cooperating writer racing after the final check. Pause other edits and stop to revalidate when unexpected changes are observed.

**Exit:** increment acceptance conditions are met or blockers and remaining work are recorded.

## 6. Validation and evidence

Discover project commands; inspect their effects and distinguish required from optional. Run only authorized checks in the correct target directory and environment. Record actual command, exit code, result, artifact reference, exact `content:<sha256>` identity, and paths covered. The identity hashes the original baseline commit, retained unchanged, and a canonical manifest of explicit `revision_paths`; a later Git commit SHA is separate provenance. A changed identity makes old evidence ineligible for the new gate. Use impact analysis to rerun the relevant checks, but produce current evidence for every required criterion. If a check is unavailable or blocked, use `NEEDS_EVIDENCE`; do not infer success.

**Exit:** every required criterion has evidence or an explicit unresolved status.

## 7. Independent verification

Assign reviewers by the risk bands in `harness/config/verification-policy.yaml` before review starts, with exact content revision and paths. Reviewers inspect read-only and declare complete/incomplete status, full-scope attestation, mandatory checks completed/remaining, limitations, unverified areas, and findings; no mandatory check may remain for a complete report. Any changed content identity requires every required reviewer to report against the final identity. High-risk security-sensitive, irreversible/data-loss, or Critical/High-finding cases require a second independent human reviewer. Reconcile assignments and reports, but do not claim this proves substantive exhaustiveness. Human-required approvals need verifiable external authentication evidence; identity/session strings alone are declarations. Include documentation accuracy in scope.

**Exit:** findings are classified and each is corrected, accepted with rationale, deferred, or escalated.

## 8. Correction loop and gate

Correct confirmed in-scope defects and rerun checks selected by impact analysis; any changed content identity makes earlier evidence and reviews ineligible for the new gate. Produce current evidence for every required criterion and obtain all required reviewer reports on the final identity. The gate applies severity rules, verifies evidence links and immutable content identity, checks documentation, and validates reviewer completion/identity assurance and approver authority. A Policy Evaluator may approve eligible low-risk and reviewed medium-risk increments; externally authenticated human approval is required for accepted risk and high-risk work. The implementation author cannot issue or override approval.

**Exit:** gate decision and evidence are recorded; no required item is silently waived.

## 9. Commit and closeout

The primary sequence gates an immutable content identity before any optional commit. Stage exact paths and commit only with separate, operation-specific, verifiably authenticated human authorization. Review the index, staged diff, pre-existing staged files, secrets, generated artifacts, and status before committing. Recompute the `revision_paths` manifest using the original baseline commit retained in workflow state and record commit SHA as provenance; only mark it equivalent if its content identity exactly matches the approved gate. Otherwise produce current evidence for every required criterion, obtain all required reviews on the new identity, and repeat the gate. Commit authorization does not authorize push. Do not push, deploy, publish, or perform other external actions unless separately authorized.

**Exit:** increment or project closeout reports scope, validations, unresolved limitations, and repository state.

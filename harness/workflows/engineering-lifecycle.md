# Engineering Lifecycle

This workflow is a reusable operating contract, not an executable pipeline. Apply it in the target repository and adapt validation to that project's language, tools, risk, and instructions.

## 0. Establish context

- Confirm the target repository path and whether it is existing or new.
- Read target instructions, assignment, current plans, status, and relevant source.
- Inspect working-tree changes before editing. Identify user work and preserve it.
- Record constraints, authorization boundaries, available validation, and unresolved questions.

**Exit:** target, source of truth, constraints, baseline state, and working directory are known.

## 1. Discovery

Inventory relevant files, existing behavior, conventions, tests, and external dependencies. Separate observed facts from assumptions. Audit existing documents before proposing edits. Do not replace established information wholesale.

**Exit:** concise evidence-backed current-state report and explicit gaps.

## 2. Requirements

Extract requirements from authoritative sources. Assign stable identifiers; link each to source location. Define testable acceptance criteria, constraints, and non-goals. Classify each statement as requirement, inference, confirmed decision, optional improvement, or open question. Resolve only questions authorized for resolution.

**Exit:** traceability baseline and open questions are visible; product owner approval is recorded where required.

## 3. Architecture and decisions

Describe components, boundaries, data ownership, interfaces, failure behavior, security, and deployment assumptions at the level needed to plan. Record consequential choices with context, alternatives, decision, and consequences. Preserve confirmed decisions.

**Exit:** implementation-critical behavior is decided or explicitly left open with a plan for handling it.

## 4. Implementation planning

Break work into small increments. For each, identify requirements, files/components likely affected, dependencies, validations, and acceptance conditions. Sequence high-risk foundations first. Avoid committing future scope as present implementation.

**Exit:** plan is reviewable and approvals required by the target process are explicit.

## 5. Incremental implementation

For one authorized increment at a time: inspect relevant code, make focused changes, add or update tests, and avoid unrelated refactors. Record implementation choices that resolve minor ambiguity. Recheck scope before closing the increment.

**Exit:** increment acceptance conditions are met or blockers and remaining work are recorded.

## 6. Validation and evidence

Discover project commands; distinguish required from optional. Run authorized checks in the correct target directory and environment. Record actual command, exit code, result, and artifact reference. If a check is unavailable or blocked, use `NEEDS_EVIDENCE`; do not infer success.

**Exit:** every required criterion has evidence or an explicit unresolved status.

## 7. Independent verification

Assign reviewers based on change risk. Reviewers inspect the target state and report evidence-based findings without modifying files. Reviewers should challenge requirements coverage, tests, races, failure behavior, security, maintainability, and deployment as relevant. The workflow owner records findings and disposition.

**Exit:** findings are classified and each is corrected, accepted with rationale, deferred, or escalated.

## 8. Correction loop and gate

Correct confirmed in-scope defects and rerun affected checks. The gate checks traceability, evidence, blocking findings, open decisions, and authorization. It can approve, reject/request corrections, request evidence, or escalate. The implementation author cannot be the sole approval evidence.

**Exit:** gate decision and evidence are recorded; no required item is silently waived.

## 9. Commit and closeout

Commit only if authorized. Review staged files, diff, secrets, generated artifacts, and status before committing. Record commit hash and final working-tree state. Do not push, deploy, publish, or perform other external actions unless separately authorized.

**Exit:** increment or project closeout reports scope, validations, unresolved limitations, and repository state.

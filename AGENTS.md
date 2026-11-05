# Autonomous Engineering Harness — Working Instructions

## Purpose and boundaries

This repository defines a reusable, language- and architecture-neutral engineering workflow. It is not the product repository. Product source, tests, migrations, deployment configuration, credentials, and project-specific requirements belong in the target repository.

Before work begins, identify the target repository explicitly and read its local instructions (for example, `AGENTS.md`, `README.md`, contribution guides, and build documentation). Apply target-repository instructions to target work. Do not assume the harness repository's conventions govern the product.

## Source of truth

- Treat the assignment and user-confirmed decisions as the source for product scope.
- Keep requirements, inferences, decisions, optional improvements, and open questions distinct.
- Link requirements and acceptance criteria to their source and decision references.
- Preserve existing work and confirmed decisions. Inspect documents before editing; update them in place and retain history where appropriate.
- Do not silently resolve open questions by assumption. Ask only when a real requirement, architectural decision, material security risk, blocker, destructive operation, or external action requires it.

## Engineering workflow

- Work in small, reviewable increments with explicit acceptance criteria.
- For each increment, state scope, implementation, and required/optional validations.
- Run only commands that are available and authorized. Record exact commands, working directory, exit status, observed results, and relevant artifact references.
- Never report a check as passed unless it actually ran and its result was observed.
- After a confirmed defect is corrected, rerun the affected validation before declaring the work complete.
- Distinguish written policy from technically enforced controls. Identify concrete enforcement evidence before claiming a gate is enforced.
- Scale review depth to risk. Independent review must be meaningfully separate from implementation; an author cannot be the sole evidence for approval.

## Reviews and approvals

Reviewers report findings and evidence without editing project files. The workflow owner records accepted review results in the appropriate project artifact. A phase gate may approve, reject/request correction, request evidence, or escalate an open decision.

Do not request approval for routine, reversible implementation choices already within authorized scope. Do not perform external publication, deployment, push, or other separately restricted actions without explicit authorization.

## Evidence and privacy

Keep project-specific records in the target repository. Do not put target code, personal data, credentials, secrets, or unnecessary assessment details in this reusable repository. Never include secret values in logs or reports.

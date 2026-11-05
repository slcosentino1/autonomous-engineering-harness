# Workflow Guide

## Operating model

Use one Codex session to move the target project through a documented lifecycle. Treat the harness as reusable guidance and the target repository as the only location for product-specific work and evidence.

## Target setup

1. Resolve the target path and confirm it is the intended repository.
2. Read target-local instructions and inspect current status before editing.
3. Identify assignment sources, existing requirements/decisions, validation commands, and authorization boundaries.
4. Keep existing user changes separate from new work.

## Phase progression

Discovery establishes facts. Requirements converts authoritative sources into traceable criteria. Architecture and decisions specify consequential behavior. Planning creates small increments. Implementation changes one authorized increment. Validation records actual evidence. Independent verification challenges the result. Correction reruns affected checks. A gate records an outcome. Commit and external actions follow explicit authorization.

A project may use different names or combine low-risk phases, but the evidence and approval boundaries must remain clear.

## Autonomy

Proceed without interrupting for local, reversible details that do not alter requirements or approved architecture. Record relevant implementation choices. Escalate genuine ambiguity, a consequential decision, a material security concern, a real blocker, or an action requiring additional authorization. Continue unrelated work while waiting.

## Project record

Adapt templates into the target repository. Maintain stable requirement/decision identifiers. The workflow owner, not a read-only reviewer, records review findings in the target build log. Preserve history and do not claim completion while required evidence is missing.

## Completion

For a bounded increment, close only its own acceptance criteria. For project completion, include an end-to-end traceability review, unresolved risks, final validation evidence, review disposition, and repository state. A local commit is separate from push, deployment, or publication.

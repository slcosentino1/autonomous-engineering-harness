# Harness Goals

## Mission

Enable one engineering agent to take a software assignment, work in a separately identified target repository, and deliver incremental changes with traceable requirements, risk-appropriate validation, independent verification, and clear evidence.

## Goals

1. Preserve target-repository instructions, existing work, and user-confirmed decisions.
2. Translate source assignments into traceable requirements and testable acceptance criteria.
3. Plan work as small increments with observable completion conditions.
4. Verify changes using project-appropriate commands without claiming unrun checks.
5. Separate implementation from independent review and gate decisions.
6. Keep project data and product artifacts in the target repository.
7. Support autonomous progress on authorized, reversible work while escalating genuine blockers and consequential decisions.
8. Keep the initial implementation simple, portable, and dependency-free.

## Non-goals for this foundation

- Building or changing a product.
- Running a generic shell-command engine or autonomous service.
- Enforcing policies technically through CI or permissions.
- Installing dependencies or choosing a target stack.
- Implementing multi-agent coordination.
- Publishing, deploying, or pushing to remote services.

## Success signals

A new target project can use the workflow to identify its source of truth, preserve existing decisions, prepare a plan, execute one increment, capture trustworthy evidence, receive independent review, correct confirmed defects, and record a defensible gate result without mixing target files into this repository.

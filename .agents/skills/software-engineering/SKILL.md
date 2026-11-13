---
name: software-engineering
description: Apply practical engineering judgment when changing existing code, especially around maintainability, contracts, compatibility, and scope.
metadata:
  short-description: Cohesive, compatible implementation
---

# Software Engineering

Make the smallest cohesive change that satisfies the assignment and fits the repository.

- Read the relevant code, callers, instructions, and tests. Trace the real call path; filenames alone do not establish ownership.
- Keep responsibilities together when they change for the same reason. Extract only when a clear input/output contract reduces coupling or enables useful independent tests.
- Keep dependencies explicit. Avoid passing a whole coordinator, callback bundles, service classes, or interfaces without a concrete need.
- Make side effects and errors clear. Handle failures where the owning component can act; preserve existing error behavior.
- Preserve public, persisted, and legacy contracts unless the assignment requires a change.
- Modify only files needed for the requested behavior. Avoid opportunistic cleanup, speculative configuration, and dependencies.

**Example:** Keep a save-before-provider transition in its coordinator; extract a pure parser only when it has a direct contract and tests.

**Boundary:** This skill guides implementation quality, not architecture selection, test strategy, independent review, or scope approval. Use `architecture-design`, `testing-strategy`, and `code-review` for those tasks.

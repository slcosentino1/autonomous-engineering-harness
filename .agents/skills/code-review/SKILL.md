---
name: code-review
description: Independently review changes for requirement gaps, defects, regressions, unsafe behavior, and missing verification using concrete evidence.
metadata:
  short-description: Evidence-based independent review
---

# Code Review

Compare the actual change with the assignment and existing contracts. A proposal or implementation summary is context, not proof. Do not implement fixes during review.

- Trace changed code through relevant callers, state, side effects, failure paths, and tests.
- Check behavior, compatibility, security, concurrency, resource handling, and persistence where relevant.
- Report only evidence-backed issues. Give the file/location, trigger, concrete impact, and specific correction. Identify missing regression coverage when it matters.
- Separate confirmed defects from uncertain risks and optional improvements. Style preferences and hypothetical concerns are not defects.

**Severity:** Must fix means a demonstrated requirement, compatibility, security/state, or correctness failure. Should fix means a concrete non-blocking issue. Optional means advisory improvement without current acceptance failure.

**Example:** Cite a stage that advances before saving its artifact and explain how resume then skips required work.

**Boundary:** This skill is read-only. In a correction stage, use its evidence criteria while following that stage's repair assignment. Use `testing-strategy` for test design and `architecture-design` for broader design decisions.

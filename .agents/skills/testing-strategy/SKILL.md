---
name: testing-strategy
description: Choose focused, deterministic tests for behavior changes, defects, integrations, state transitions, and failure paths.
metadata:
  short-description: Tests that establish behavior
---

# Testing Strategy

Test the behavior at risk; passing tests and line coverage alone do not establish correctness.

- **Unit:** Pure decisions, parsing, rendering, and calculations.
- **Integration:** Real module boundaries, persistence, filesystems, subprocess contracts, and state ordering. Use temporary repositories.
- **Contract:** Compatibility of requests, responses, serialized data, and errors.
- **End-to-end:** Sparingly; fake providers and networks unless real calls are authorized.

Choose the smallest level that observes the guarantee. A unit test does not replace integration coverage for locks or recovery.

1. Assert observable outcomes, not incidental implementation details.
2. Add a regression case for each fixed defect. Cover relevant malformed inputs, limits, legacy data, failures, retries, interruption, and concurrency.
3. Mock external boundaries; do not mock away the behavior under test.
4. Keep tests deterministic and isolated from networks, user config, and execution order.
5. For stateful flows, assert resulting state and save/transition ordering. Retain integration tests when extracting helpers.
6. Do not duplicate suites or add tests only to raise coverage.

**Example:** Unit-test a partitioner, and retain a workflow test proving persisted coverage blocks premature completion.

**Boundary:** This skill guides test design, not architecture, review findings, or production scope. Use `architecture-design` or `code-review` where applicable.

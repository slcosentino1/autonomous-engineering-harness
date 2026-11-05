# Generalized Lessons Learned

These lessons are process guidance distilled from an earlier project. Product-specific requirements, endpoints, technology choices, and assessment details are intentionally excluded.

## Preserve and reconcile context

- Read existing instructions, decisions, requirements, and plans before editing them.
- Audit a document before replacement; update narrowly and retain historical evidence.
- Keep stable IDs and distinguish requirements from assumptions, decisions, suggestions, and open questions.
- Written policy does not prove technical enforcement.

## Make evidence trustworthy

- State exact commands, working directories, exit codes, and observed outcomes.
- Do not count skipped integration checks as passing; make mandatory integration suites fail fast when configuration is missing.
- A passing unit suite does not demonstrate persistence, deployment, or failure recovery unless those behaviors are exercised.
- Record limitations, environment constraints, and warnings without overstating their significance.

## Design for failure, not only the happy path

- For asynchronous work, define ownership, concurrency, interruption, and external outcome uncertainty before implementation.
- Fencing can protect durable state from stale writers but cannot reverse side effects in an external system.
- Tests should assert user-visible outcomes and not merely internal implementation details.

## Keep review independent

- A read-only reviewer should return findings and evidence; the workflow owner records them.
- Rerun validations affected by confirmed fixes.
- A commit boundary should be explicit, with staged diff and generated files reviewed first.

## Keep scope proportional

- Build only the infrastructure needed by requirements.
- State what is implemented versus planned.
- Do not turn a successful experiment's product-specific architecture into a universal default.

---
name: architecture-design
description: Evaluate software architecture choices when boundaries, dependencies, deployment, scale, or operational constraints make design material. Use selectively for greenfield systems and significant architectural changes; skip routine changes with clear existing patterns.
metadata:
  short-description: Context-driven software architecture analysis
---

# Architecture Design

Help choose an architecture that fits the actual problem. This skill is a reasoning aid, not a template or architecture-selection rule. A simple modular application may be the best design; sometimes a formal pattern adds no value.

## When to use it

Use this skill when a task creates a system, changes important boundaries, introduces substantial integrations, or raises material questions about scale, reliability, deployment, data ownership, or operations. Do not force an architecture review onto a routine feature that fits the established design. Inspect the repository and follow its sound existing conventions before proposing a change.

## Reason from the problem

1. **Clarify the outcome.** Identify explicit functional requirements, users and workflows, data and business rules, and observable acceptance criteria. Separate confirmed requirements, implicit constraints, assumptions, unknowns, and decisions that can safely wait. Ask only about uncertainty that could change product behavior or a material design decision.
2. **Discover material decisions.** Trace the main request, data, concurrency, failure, and recovery paths to identify the choices the design actually has to make. Consider isolation, idempotency, persistence, recovery, operational limits, and failure behavior where relevant. Do not mistake the absence of an explicit implementation instruction for evidence that a decision has no consequence.
3. **Identify relevant quality needs and constraints.** Consider the non-functional requirements that affect this system, such as latency, throughput, availability, security, consistency, recoverability, privacy, cost, and change rate. Account for expected scale, team ownership, delivery timeline, deployment environment, operational skills, existing technology, and migration limits. Use stated targets; do not invent numerical targets.
4. **Map the system.** Trace important request, data, and failure paths. Identify cohesive domain responsibilities, external systems, contracts, trust boundaries, and who owns each data set. For an existing application, examine actual dependency direction and runtime behavior; directory names alone do not establish the architecture.
5. **Compare viable alternatives for each material decision.** Start with the simplest approach that could adequately satisfy the functional and operational needs. Compare realistic alternatives by their benefits, implementation and operating costs, limitations, risks, and user impact. Do not document trivial choices exhaustively. Read [the approach guide](references/approaches.md) when detailed pattern trade-offs matter. Use [the scenario examples](references/scenario-examples.md) only when they clarify a similar decision.
6. **Recommend and explain.** Choose the simplest architecture that adequately satisfies the functional and operational requirements. Do not optimize for minimum component count at the expense of correctness, reliability, concurrency, scalability, or operational constraints. Explain why the selected option is preferable, its accepted risks and limitations, and what evidence or change in context could make another option preferable.
7. **Evaluate deferrals before making them.** For every material capability postponed, explain why it is unnecessary now, what risk is accepted, the likely cost of adding it later, and what concrete condition should trigger reconsideration. “Future consideration” is not itself a trade-off analysis.
8. **Check the consequences.** Verify that boundaries support required change and failure paths, tests can observe important behavior, and the team can deploy and operate the result. Prefer explicit evolution triggers over speculative infrastructure.

## Design principles

- Never select Clean, Hexagonal, Onion, microservices, event-driven, CQRS, serverless, or another style by default. Explain why a style fits this problem before recommending it.
- Do not add layers, interfaces, adapters, services, brokers, databases, or abstractions without a concrete benefit that outweighs their maintenance and operational cost.
- Judge architecture by responsibilities, cohesion, coupling, dependency direction, data ownership, and runtime boundaries—not by folder count or pattern names.
- Recognize that Clean, Hexagonal, and Onion approaches overlap in their inward dependency direction and domain isolation. Use only the concepts that solve a demonstrated problem; do not stack labels or ceremony.
- Prefer a modular monolith when clear module boundaries are useful but independent deployment or scaling is not required. A single deployable can have strong internal boundaries.
- Recommend independently deployed services only when independent ownership, release cadence, scaling, fault isolation, or another demonstrated constraint justifies distributed-system costs.
- Treat asynchronous messaging and separate read/write models as explicit consistency and operations choices, not automatic upgrades.
- Do not design for every identified risk. Address risks that matter to the assignment and explain why lower-impact scenarios do not change the design.
- Distinguish a sequencing constraint within one unit of work from a global concurrency limit. When scheduling work, consider bounded concurrency, fairness, queue delay, and head-of-line blocking where those affect users or operating needs.
- Distinguish an accidental retry of the same request from an intentional new request. Consider downstream idempotency and its effect on retry, uncertain outcomes, and recovery when work crosses a service boundary.
- Compare storage and worker choices against the workload, consistency, deployment, and operations that are actually known. A familiar lightweight database or a single worker is not justified solely by being simple to start with.
- These are prompts for relevant decisions, not a requirement to add a worker pool, idempotency layer, database, broker, or other component. A deliberately simple design remains preferable when it adequately meets the requirements and its consequences are understood.
- If no formal architecture pattern is needed, say so and describe the small set of boundaries or conventions that are sufficient.

## Changes to existing systems

Before changing a coordinator, orchestrator, or large module:

1. Trace the relevant workflow stage and current call path. List the responsibilities and state each method reads or changes; verify this from code rather than inferring it from filenames.
2. Identify which component owns the requested behavior. Keep stage ordering, lifecycle transitions, persistence ordering, authorization, and recovery decisions in the coordinator when correctness depends on that ordering or shared state.
3. Extract logic only when it has a cohesive responsibility, an independent reason to change, explicit inputs and outputs, and a useful test boundary. Keep a helper in its current module when it is inseparable from its owning stage or requires passing the whole coordinator or many callbacks.
4. Map each proposed file change to an explicit requirement or demonstrated risk. Preserve compatibility contracts and existing failure behavior. Do not include adjacent cleanup or unrelated enhancements.
5. Compare the smallest local change with extraction. A smaller line count is not evidence that a new module is better; extract when the boundary reduces coupling or clarifies ownership enough to justify its cost.

For a modularization, document the methods and callers that move, their state and persistence dependencies, and the tests that cover their ordering and failure paths. Keep compatibility wrappers only where existing callers depend on them.

### Examples

- If a new workflow stage needs a state transition and must save it before invoking a provider, keep that decision in the coordinator. Extract only a pure prompt formatter or parser when it has a clear contract.
- If several stages need the same Git inventory calculation, a workspace module can own it when it accepts explicit repository inputs and returns data without deciding workflow transitions.
- Do not add repository inventory, security scanning, or reporting to an orchestrator merely because it is the easiest file to reach. First show that the requested behavior belongs to that workflow responsibility.

## Boundaries

This skill guides architecture and responsibility decisions. It does not authorize expanding an assignment, prescribe a directory structure, replace project instructions, or decide product behavior. For test selection and test doubles, follow `testing-strategy` when available. For implementation-level maintainability beyond boundaries, follow `software-engineering` when available.

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

1. **Clarify the outcome.** Identify explicit functional requirements, users and workflows, data and business rules, and observable acceptance criteria. Separate confirmed requirements from assumptions and open questions. Ask only about uncertainty that could change product behavior or a material design decision.
2. **Identify relevant quality needs.** Consider only the non-functional requirements that affect this system, such as latency, throughput, availability, security, consistency, recoverability, privacy, cost, and change rate. Use stated targets where available; do not invent numerical targets.
3. **Map the system.** Trace important request, data, and failure paths. Identify cohesive domain responsibilities, external systems, contracts, trust boundaries, and who owns each data set. For an existing application, examine actual dependency direction and runtime behavior; directory names alone do not establish the architecture.
4. **Understand constraints.** Account for expected scale, team size and ownership, delivery timeline, deployment environment, operational skills, existing technology, and migration limits. Distinguish a current need from a hypothetical future need.
5. **Compare only viable alternatives.** Start with the smallest approach that could satisfy the requirements. Evaluate other styles only when they address a real requirement or risk. Styles can be combined selectively; they are not a checklist and need not be mutually exclusive. Read [the approach guide](references/approaches.md) when detailed pattern trade-offs matter. Use [the scenario examples](references/scenario-examples.md) only when they help clarify a similar decision.
6. **Recommend and explain.** Describe responsibilities and boundaries, important dependency directions, deployable units, data ownership, and integration style where relevant. Explain why this is preferable to realistic alternatives, including the complexity and operating costs it introduces. State material limitations, assumptions, risks, and what evidence or requirement would justify a different choice later.
7. **Check the consequences.** Verify that boundaries support the required change and failure paths, tests can observe important behavior, and the team can deploy and operate the result. Prefer explicit evolution triggers over speculative infrastructure.

## Design principles

- Never select Clean, Hexagonal, Onion, microservices, event-driven, CQRS, serverless, or another style by default. Explain why a style fits this problem before recommending it.
- Do not add layers, interfaces, adapters, services, brokers, databases, or abstractions without a concrete benefit that outweighs their maintenance and operational cost.
- Judge architecture by responsibilities, cohesion, coupling, dependency direction, data ownership, and runtime boundaries—not by folder count or pattern names.
- Recognize that Clean, Hexagonal, and Onion approaches overlap in their inward dependency direction and domain isolation. Use only the concepts that solve a demonstrated problem; do not stack labels or ceremony.
- Prefer a modular monolith when clear module boundaries are useful but independent deployment or scaling is not required. A single deployable can have strong internal boundaries.
- Recommend independently deployed services only when independent ownership, release cadence, scaling, fault isolation, or another demonstrated constraint justifies distributed-system costs.
- Treat asynchronous messaging and separate read/write models as explicit consistency and operations choices, not automatic upgrades.
- Do not design for every identified risk. Address risks that matter to the assignment and explain why lower-impact scenarios do not change the design.
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

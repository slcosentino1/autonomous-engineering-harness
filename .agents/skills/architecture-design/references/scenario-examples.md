# Scenario examples

These short examples illustrate context-driven choices. They are starting points, not templates.

## Small CRUD API

**Situation:** A small team needs create, read, update, and delete endpoints over one relational database. Rules are straightforward and the API ships as one application.

**Reasoning:** A small layered or modular structure can keep request handling, basic application logic, and persistence understandable. The system has no stated need for independent deployment, a broker, or extensive infrastructure substitution. Clear tests around API behavior and persistence are more valuable than adding an interface for every class.

**Likely choice:** Keep one deployable with a few cohesive modules. Use the existing framework and conventions. Do not add Clean Architecture, microservices, CQRS, or event-driven messaging without a requirement that pays for their extra boundaries and operations.

## Backend with complex rules and multiple integrations

**Situation:** A backend coordinates a complex order lifecycle and integrates with payment, inventory, and shipping providers whose APIs change independently.

**Reasoning:** Domain rules and provider-specific behavior have different reasons to change. Isolating the order policy from transport and provider details improves focused tests and reduces integration leakage. The system can still be one deployable; provider boundaries do not by themselves require separate services.

**Likely choice:** Use a modular monolith with an explicit domain/application core and ports for integrations that need substitution or isolation. Hexagonal or Clean Architecture concepts may help at these boundaries. Add asynchronous events only for workflows that benefit from durable background processing, and define retry, idempotency, and consistency behavior.

## Independently scalable services

**Situation:** Distinct capabilities have separate owning teams and release schedules, and measured traffic shows one capability needs much more capacity than the others. Each team can operate production services.

**Reasoning:** Independent deployment and scaling now solve demonstrated ownership and capacity needs. The network and data boundaries will also create failure, observability, and consistency work, so service extraction is justified only where ownership is cohesive and contracts can be explicit.

**Likely choice:** Consider microservices for those capabilities, with service-owned data and versioned contracts. Use asynchronous events for genuinely asynchronous workflows, not every interaction. Keep synchronous calls limited and resilient. CQRS is a separate decision and should be added only if read/write needs warrant projections.

## Greenfield project with substantial uncertainty

**Situation:** A new product has evolving user needs, unknown adoption, and unresolved partner contracts. The team is small and must learn from an early release.

**Reasoning:** Strong early decomposition would encode guesses and create migration work before the boundaries are understood. At the same time, putting all behavior in one unstructured module would make learning harder.

**Likely choice:** Start with a thin modular monolith, simple seams around volatile integrations, and observable acceptance criteria. Prototype contract uncertainty where possible. Record assumptions and concrete triggers—such as separate team ownership, measured independent scaling, or a stable domain boundary—that would justify stronger isolation later. Revisit when evidence appears rather than building hypothetical capacity in advance.

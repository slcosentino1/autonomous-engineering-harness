# Plan and Status

## Current status

**Foundation initialized.** This repository contains reusable workflow documentation, prompts, reviewer contracts, gate policy, schemas, and templates. No executable automation or multi-agent orchestration is implemented.

## Roadmap

### 1. Reusable manual workflow foundation — complete

- Define repository boundaries and target-repository discovery.
- Document discovery, specification, planning, implementation, validation, review, correction, and closeout.
- Provide generic prompts, evidence contracts, review roles, gates, schemas, and templates.

### 2. Validate contracts with representative projects — planned

- Exercise the templates and JSON Schemas against independent sample projects.
- Tighten schema validation and clarify portability only where evidence identifies a gap.
- Keep sample product data out of this repository unless a deliberately generic fixture is needed.

### 3. Single-agent workflow automation — planned

- Define a minimal invocation and target-path contract.
- Automate evidence collection and selected policy checks only after the manual workflow is stable.
- Make command execution, working directory, approvals, and failure handling explicit.
- Avoid adding an orchestration framework unless a demonstrated need warrants it.

### 4. Multi-agent support — future, not started

- Reuse the reviewer and handoff contracts defined here.
- Add a coordinator and role-specific agents only after the single-agent path works and explicit authorization is given.
- Do not use LangGraph or introduce an orchestration dependency by default.

## Completion policy

A roadmap item is complete only when its stated outputs exist and evidence is recorded. Planned automation is not to be described as implemented. Update this file when scope or status changes, preserving decisions in `docs/decisions.md`.

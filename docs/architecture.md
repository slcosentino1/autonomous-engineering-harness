# Harness Architecture

## Purpose

The harness is a reusable process and contract repository. Product code is owned by a separate target repository. The workflow remains document-first and dependency-free; its first executable component is a read-only target preflight CLI.

## Repository boundary

```mermaid
flowchart LR
  H[Harness repository\nReusable workflow, prompts, schemas, templates]
  A[Engineering agent\nCurrently one Codex session]
  T[Target repository\nAssignment, product, tests, project records]
  V[Verification evidence\nCommands, tests, reviews, artifacts]
  A -->|reads and follows| H
  A -->|works within target instructions| T
  T -->|produces| V
  V -->|supports gate decision| A
```

The harness does not contain a target product or receive copied target source. An invocation supplies an explicit target path; the preflight uses a sanitized Git environment and verifies canonical working roots, Git common/ref directories, object stores/alternates, and shared refs/object inodes are disjoint from the harness. It reads repository status, worktrees, and submodule state, inventories authorized instruction roots and project capabilities, validates configured code/artifact scopes, and reports filesystem observations. Writes are restricted by policy to explicit target code and artifact allowlists; an empty allowlist means no writes. `harness/safe_writer.py` re-runs preflight and provides narrow scope-checked directory creation plus file creation/replacement. It traverses with descriptor-relative no-follow operations, authorizes every missing directory component, and reports identities for created directories. File replacement uses hard-link checks, baseline hashes, identity/content rechecks, atomic rename, and post-write verification. The writer relies on the single-writer assumption; atomic rename is not a compare-and-swap and does not protect against arbitrary non-cooperating writers.

## Current components

- **Instruction layer:** repository-wide working boundaries and source-of-truth rules.
- **Workflow layer:** lifecycle from discovery to closeout.
- **Prompt layer:** reusable task instructions for one agent.
- **Verification layer:** reviewer contracts and evidence/status definitions.
- **Gate layer:** decision outcomes and minimum approval checks.
- **Contract layer:** JSON Schemas for workflow state, evidence, traceability, findings, and gate decisions.
- **Template layer:** project-specific documents to adapt in the target repository.
- **Read-only preflight:** `harness/preflight.py`, a Python 3.10+ standard-library CLI with a JSON report and optional JSON scope configuration. It does not execute discovered instructions or write target artifacts.

The Verification Gate binds to an immutable content fingerprint over explicit `revision_paths`; a later commit SHA is provenance and must be shown to represent the same content. Reviewer completion reports and assigned scopes are reconciled, but no schema can prove a reviewer disclosed every substantive observation. Identity/session strings are declared data. Human approvals that policy requires need verifiable external authentication evidence; no identity provider is selected or implemented.

The lifecycle, state, evidence, identity, and gate contracts are not enforced by an orchestrator. The preflight CLI remains read-only. The safe writer enforces only its own target/scope/path/directory and file checks; it does not force the primary agent to use the helper, authenticate humans, persist workflow state, validate evidence, or make gate decisions. Its single-writer concurrency assumption is operational and does not provide protection against a non-cooperating race after the last check.

## Future-compatible role boundaries

The workflow distinguishes Implementer, Workflow Coordinator, Policy Evaluator, and independent Requirements, QA/Test, Code, Architecture, and Security Reviewer responsibilities. A policy evaluator may approve only low-risk increments that satisfy all automatic conditions; medium-risk work requires an independent review; high-risk work requires a human reviewer and authorized human gate approver. Reviewers are read-only. A handoff carries target ID/revision, scope, requirements, evidence references, applicable instructions/skills, and decision authority. These roles can later map to separate processes or agents, but no runtime, agent framework, or coordination protocol is implemented here.

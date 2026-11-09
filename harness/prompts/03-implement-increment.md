# Implement One Increment Prompt

Implement the agreed increment inside the initialized target workspace.

1. Re-read target instructions, requirements, decisions, acceptance criteria, the workspace execution policy, and current Git status. Preserve pre-existing user changes.
2. Make the smallest maintainable change that satisfies the agreed behavior. The increment's paths describe review scope; they are not per-file write permissions. Relevant existing source, tests, configuration, and documentation may be changed inside the canonical workspace.
3. Update the smallest relevant existing documentation alongside material behavior, architecture, interface, data, security, operational, or maintenance changes.
4. Use `harness/safe_writer.py` through the orchestrator for target file operations. The workspace policy permits ordinary in-workspace creates, replacements, directories, metadata, and generated files without exact path or extension allowlists. Files created by this workflow may be renamed or deleted; preserve unrelated pre-existing user files. Never write outside the canonical target, into Git metadata, sensitive credential/environment paths, or a nested repository.
5. Safe Writer re-runs preflight, checks canonical workspace binding, resolves parents without following symlinks, rejects hard-linked existing files, checks expected content and identity before replacement, and records operation evidence. The documented single-writer assumption does not prevent a non-cooperating writer from racing after the final check; stop and revalidate if an unexpected change appears.
6. Run configured project builds, tests, linters, formatters, and static analysis through the orchestrator. Approved package managers may install manifest-declared project dependencies without per-install approval. The command filter is a guardrail, not OS-level process confinement. Do not expose or transmit secrets, use arbitrary download URLs or custom registries, contact unrelated services, mutate shared infrastructure, or perform paid external operations without explicit authorization and budget.
7. Correct confirmed defects within the agreed assignment and run affected validations again. A changed content identity makes prior evidence and review reports stale for the new identity; obtain current evidence for each required criterion and reviewer assignment.
8. Record actual changes, documentation impact, evidence, decisions, limitations, and operation/command results in workflow state. Internal transitions and recovery use the initialized workspace policy without another approval request.

Report changed files, behavior, tests and observed results, remaining risks, and repository status. Do not claim an unrun check passed or report an incomplete reviewer/gate as complete.

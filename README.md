# Autonomous Engineering Harness

A Python-based engineering workflow that coordinates Codex and Claude across planning, human approval, implementation, independent review, bounded correction, verification, and delivery. It is designed to make agent-driven changes auditable and controlled rather than simply chaining CLI calls.

## Requirements

- Python 3.11 or newer
- Codex CLI (`codex`)
- A Git target repository

The harness core uses only the Python standard library and has no `requirements.txt`. Optional Langfuse tracing uses the pinned SDK in [`requirements-tracing.txt`](requirements-tracing.txt). The harness and tracing SDK require Python 3.11 or newer. The SDK supports Langfuse Cloud or self-hosted Langfuse v4.59.0 or newer; if the project is on an older self-hosted server, upgrade the server before installing this tracing SDK.

The harness runs under the Python interpreter used to invoke `orchestrator.py`; it does not create or switch environments. For a persistent environment, create and activate a virtual environment in the harness checkout:

```sh
python3 -m venv .venv
source .venv/bin/activate
```

After activation, use `python` both to launch the harness and to install packages. If you enable tracing, install its optional dependency once into that same environment:

```sh
python -m pip install -r requirements-tracing.txt
```

There are no core packages to install. `.venv/` is ignored by Git. If you use another persistent environment, activate it before running the harness and install the optional file with that environment's `python -m pip`.

## Workflow

1. Codex reads the assignment and target repository, then saves a Markdown proposal.
2. Claude reviews the proposal. Codex evaluates the feedback and refines it. One second Claude review is allowed only when the refined plan requests it; there are at most two plan reviews.
3. The workflow stops in `awaiting_approval`. Read the proposal before running `approve`.
4. After approval, Codex implements the plan and runs relevant checks in the target workspace.
5. Claude independently reviews the change in one call when the complete context fits. Larger diffs are grouped into coherent parts and receive a final integration review. If that review requests changes, Codex may make one bounded correction and rerun affected checks. The Harness then starts a separate verification campaign over the resulting diff. That reviewer receives the original findings and must assess each one against the current code and tests while checking the affected scope for regressions. Incomplete verification or unresolved/new blocking findings stop the workflow; delivery is not written. There is no repeated correction loop. The Harness does not independently execute project-specific tests: Claude can inspect visible code and test changes plus Codex's reported validation, but the Harness does not attest that those commands actually ran or passed.
6. A completed workflow writes a human-readable delivery report.

Each stage is recorded in one small JSON state file. `resume` retries only the failed stage. It does not replan completed work or cross the approval gate.

Starting the workflow writes one persistent authorization into that state. It binds normal development operations to the exact target and workflow ID, authorizes the configured Claude reviews within the declared budget, and is reused on every stage and resume. The saved scope is checked before resuming; approval and resume commands do not request or replace the authorization.

## Start a workflow

Provide an absolute target path and assignment. Claude calls default to a total budget of USD 2.00; override it with `--budget-usd`. The workflow prints and stores the effective budget. A Codex model is optional; without `--model`, the CLI's configured default is used.

New workflows authorize at most 32 Claude code-review calls by default across the initial review, post-correction verification, parts, integration reviews, and retries. Set `--max-code-review-calls` from 1 to 64 to authorize a different total. The limit is saved at workflow start and never expands on resume. If the saved call budget cannot cover a minimum verification campaign, correction is stopped before Codex is invoked. A larger resulting diff can still require more calls than remain; in that case verification fails closed and delivery is blocked. Existing workflows keep their saved limit.

New workflows also save execution limits in the target-bound authorization: at most 8 Codex calls, a 3,600-second Codex call timeout, a 900-second Claude call timeout, and 24 hours of active workflow runtime by default. Override them at `start` with `--max-codex-calls`, `--codex-timeout-seconds`, `--claude-timeout-seconds`, or `--workflow-timeout-seconds`. Limits are persisted and reused on resume. Codex and Claude call counts, measured provider runtime, active workflow runtime, and correction attempts are persisted in `state.json`. Per-call timeouts and aggregate runtime limits stop later calls once exhausted. Existing authorizations without these limits remain readable and receive the current defaults for work they resume; their earlier consumption cannot be reconstructed when old state has no usage evidence.

```sh
python /path/to/autonomous-engineering-harness/harness/orchestrator.py start \
  --target /absolute/path/to/target-repository \
  --task-file /absolute/path/to/assignment.md
```

For example, `--budget-usd 0.75` overrides the budget, and `--model MODEL_NAME` selects a Codex model explicitly. The selected model is saved with the workflow and reused on approval/resume. If omitted, no `--model` argument is passed to Codex.

## GitHub collaboration (Phase 1)

GitHub Issues can supply task text to the local Harness. The Harness remains responsible for planning, local approval, implementation, review, recovery, and delivery. Install and authenticate the GitHub CLI (`gh auth login`), then configure an operator-controlled repository allowlist before importing:

The `harness/orchestrator.py` entry point loads the optional `harness/.env` file before reading configuration. Values already set in the parent environment take precedence. The file uses simple `KEY=value` assignments; shell commands and variable expansion are not evaluated. `.env` files are ignored by Git. GitHub CLI authentication can use `GH_TOKEN`, `GH_ENTERPRISE_TOKEN`, or `GITHUB_TOKEN` from this file or the parent environment. These authentication variables are passed only to the `gh` subprocess. Codex and Claude receive an isolated GitHub CLI config path, no GitHub or Langfuse credential variables, no SSH agent variables, and no global Git credential helpers. Langfuse credentials remain available to the Harness tracing client and its local preflight only.

```sh
export HARNESS_GITHUB_ALLOWED_REPOSITORIES="github.com/OWNER/REPO"
python /path/to/autonomous-engineering-harness/harness/orchestrator.py github import 42 \
  --target /absolute/path/to/local-repository \
  --repository OWNER/REPO
```

For GitHub Enterprise, use `HOST/OWNER/REPO` in both the allowlist and `--repository`. Multiple allowed repositories are comma-separated. The selected repository must match the allowlist before the Issue is queried. Import requires an absolute local Git repository target and accepts the same budget, model, tracing, and execution-limit options as `start`.

Import fetches one Issue through fixed `gh api` operations, validates its repository and Issue metadata, and places its title and body into untrusted task input. Issue text matching the Harness credential redaction patterns is rejected rather than stored or sent for planning. The Issue cannot select the local target or approve work. The command creates a normal workflow and reports only its workflow ID and status; use the local `approve` command after reviewing its proposal. Repeating an import returns its correlated workflow. If a process exits during import, retry uses the reserved workflow ID; partial or invalid workflow state fails closed and requires manual reconciliation. Later Issue edits do not alter the imported workflow.

To authorize a new Issue workflow for Pull Request publication, add `--publish-pr` when importing it. This binds the saved workflow authorization to that allowlisted repository and Issue. After the normal human approval, implementation, approved code review, delivery verification, and completed commitment gate, `approve` or `resume` publishes the reviewed changes. Publication is opt-in and does not merge the Pull Request. It requires a clean target at workflow start, an unchanged default branch, a matching GitHub origin, and push permission for the authenticated `gh` account. The publisher uses only files in the verified final review scope and creates the commit and branch through GitHub's Git API; it does not stage or commit local target files. Existing completed workflows without this saved authorization require a separate, explicit one-time authorization:

```sh
python /path/to/autonomous-engineering-harness/harness/orchestrator.py github publish WORKFLOW_ID \
  --target /absolute/path/to/local-repository --authorize
```

The command rechecks saved review and delivery evidence, the commitment gate, repository allowlist, origin, default-branch baseline, and current file contents before publishing. The workflow state records publication status, repository, Issue, branch, commit SHA, Pull Request number, and URL. Retries reuse the deterministic workflow branch and commit, reconcile uncertain branch or Pull Request creation, and verify the final Pull Request file set; conflicting existing publication data fails closed. `github publish` never merges. Set `HARNESS_GITHUB_ALLOWED_REPOSITORIES` and authenticate `gh` with push permission before running it. The origin must identify the same repository using a canonical HTTPS URL or SSH URL; custom SSH host aliases are not resolved by the Harness.

To explicitly publish a status comment for an imported workflow, run:

```sh
python /path/to/autonomous-engineering-harness/harness/orchestrator.py github report WORKFLOW_ID \
  --target /absolute/path/to/local-repository
```

Reporting is a GitHub comment write. It requires the correlated repository to remain allowlisted. It does not call the GitHub `/user` endpoint or need to discover the authenticated `gh` login. Existing status comments are matched by the unique workflow marker scoped to the exact Issue; a saved comment ID is preferred. If multiple matching comments exist without a saved ID, reporting fails closed for manual reconciliation rather than updating an arbitrary comment. If no matching comment exists, it creates one. It publishes only workflow ID, an enumerated status, stage, and delivery value. It does not start, approve, or resume a workflow. Retry the report command after publication errors.

The local `.engineering/workflows/github-registry.json` stores Issue/workflow identity, snapshot time and content hash, reservation state, and an optional comment ID hint; it never stores GitHub credentials. Same-target and per-Issue OS locks prevent overlapping target changes and duplicate imports on this machine. Planning may overlap because it is read-only, but approval rechecks the target fingerprint while holding the target lock and fails if the target changed. Sensitive paths are excluded from content hashing; only staged, unstaged, and untracked status flags and a path count are saved. Approval is blocked when sensitive paths are already dirty at workflow start or become dirty before approval. Clean or reconcile them and start a fresh workflow; older workflow snapshots without sensitive-path status fail closed at approval. Later Issue edits do not synchronize into the imported workflow. Phase 1 has no cross-machine registry, MCP server, polling, webhook, or GitHub-driven approval. Revisit MCP for direct ChatGPT access or unattended Issue intake. The adapter operations are kept in a module behind the CLI so a future MCP interface can reuse them without adding another workflow authority. The separate `human_revision` proposal validation ordering remains unchanged; revisit it before the next change to that path.

## Optional Langfuse tracing

Tracing is disabled by default. Enable it for a new workflow with `start --trace` or set `HARNESS_TRACING_ENABLED=true` before `start`; either setting enables tracing. Credentials come from `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`; `LANGFUSE_BASE_URL` is optional and defaults to `https://cloud.langfuse.com`.

Install the optional SDK once in the persistent environment with `python -m pip install -r requirements-tracing.txt`. When `--trace` is explicit, a local preflight checks that the SDK imports in the current interpreter, credentials and endpoint are configured, and the SDK client can initialize. If the SDK is missing, the command stops before creating a workflow and tells you to activate the same environment and install the optional requirements. This local check does not verify remote connectivity or guarantee trace delivery. Tracing enabled only through `HARNESS_TRACING_ENABLED=true` keeps the existing best-effort startup behavior without the explicit-flag preflight. Langfuse events contain operational metadata only: workflow command, a stable pseudonymous session ID, stage/provider/operation, configured model when known, elapsed duration, status, review-part index/count, and reported provider cost. The raw workflow ID, prompts, assignments, feedback, source, diffs, reports, CLI output, secrets, and filesystem paths are not sent. Target validation status is omitted unless the workflow has reliable structured evidence for it.

The descriptions shown in Langfuse Input and Output are generated from these safe fields (for example, provider, operation, stage, status, duration, and reported cost). Prompt and project content are not used to populate them. Current authorization scopes also permit Codex invocation IDs, invocation/finish timestamps, input/output hashes and sizes, exit status, CLI/model metadata, bounded tool names/counts, and CLI-reported token counts. Each `provider.codex` generation is correlated to the workflow's pseudonymous session and its `operation_id` in `state.json`. The Codex cost is not reported because the CLI does not provide a cost value to this integration.

Langfuse receives the invocation and finish timestamps as safe metadata. Because events are buffered until the workflow lock is released, the SDK observation timestamps reflect export time; `duration_ms` keeps the buffered monotonic span duration and `codex_duration_ms` records the process interval. A process crash before flush can lose buffered Langfuse events; the persisted workflow operation remains the audit source of truth. Older workflows with the version 1 observability scope remain valid and keep their narrower metadata allowlist; only newly authorized version 2 scopes export the additional Codex evidence.

The authorization saved at `start` records whether tracing is enabled, its fixed metadata scope, and a hash of the authorized Langfuse endpoint. It never stores credentials. Approval, revision, and resume reuse that saved setting and endpoint; older workflows without an observability scope remain tracing-disabled. Trace data is buffered locally while the workflow lock is held and exported only after the operation releases the lock. Langfuse initialization, export, and shutdown failures do not affect workflow results.

Elapsed `duration_ms` values come from the harness monotonic clock and cover root executions, stages, provider calls, and review parts. The adapter buffers observations until after execution and records elapsed duration as metadata; `duration_ms` is the authoritative duration. Provider-reported Claude cost is also attached as native generation `cost_details.total`; token usage and model names are omitted when the provider does not report them.

The workflow prints its ID and proposal path. Review the proposal, then explicitly approve it:

```sh
python /path/to/autonomous-engineering-harness/harness/orchestrator.py approve \
  --target /absolute/path/to/target-repository \
  --workflow-id WORKFLOW_ID
```

To ask for a proposal change, give concise feedback. The workflow uses a remaining Claude plan review when available, then returns to `awaiting_approval`:

```sh
python /path/to/autonomous-engineering-harness/harness/orchestrator.py revise \
  --target /absolute/path/to/target-repository \
  --workflow-id WORKFLOW_ID \
  --feedback "Include the empty-input case in the proposal."
```

Reject a proposal to stop without implementation:

```sh
python /path/to/autonomous-engineering-harness/harness/orchestrator.py reject \
  --target /absolute/path/to/target-repository --workflow-id WORKFLOW_ID \
  --reason "The proposal is out of scope."
```

Check or resume a workflow with:

```sh
python /path/to/autonomous-engineering-harness/harness/orchestrator.py status \
  --target /absolute/path/to/target-repository --workflow-id WORKFLOW_ID

python /path/to/autonomous-engineering-harness/harness/orchestrator.py resume \
  --target /absolute/path/to/target-repository --workflow-id WORKFLOW_ID
```

### Explicit budget changes on resume

An explicitly authorized resume can also change the saved Claude total budget:

```sh
python /path/to/autonomous-engineering-harness/harness/orchestrator.py resume \
  --target /absolute/path/to/target-repository --workflow-id WORKFLOW_ID \
  --extend-budget 2.00

python /path/to/autonomous-engineering-harness/harness/orchestrator.py resume \
  --target /absolute/path/to/target-repository --workflow-id WORKFLOW_ID \
  --set-budget-limit 3.71
```

Use one option per command. `--extend-budget` adds a positive finite USD amount with at most two decimal places to the current total; `--set-budget-limit` supplies the new absolute total, including prior spend. The absolute limit may be lowered or set equal to the current limit, but cannot be below accumulated spend. A saved change is recorded in the provider budget-change history in `state.json`, including its timestamp, operation, old and new limits, requested amount, spend, remaining budget, workflow and authorization IDs, command, and failed stage. The same history is available at `authorization.claude_aily.budget_changes` in the saved workflow authorization. The resume command prints the applied limit and remaining amount before retrying. If less than one cent remains, the change stays authorized and the failed stage remains untried. Changes also remain saved if the stage retry fails; a later plain `resume` uses the saved limit without extending it again. Check the applied summary or saved history before repeating an extension, since each repeated extension is a new addition. Omitting both options preserves the existing resume behavior.

Workflow files are stored under `.engineering/workflows/WORKFLOW_ID/` in the target: `state.json`, `proposal.md`, and, on completion, `delivery.md`.

Each Codex operation record in `state.json` includes the exact UTF-8 input hash and size, raw captured stdout/stderr hashes and sizes, the final-message file hash when available, exit status, timestamps, and monotonic duration. Stream hashes cover the bytes captured by the subprocess pipes; when a test runner supplies text instead of bytes, the harness hashes its UTF-8 encoding and records that representation. `result_sha256` remains the hash of the trimmed, normalized final response used by workflow recovery, so it is distinct from the raw stdout and final-message file hashes. Timeouts are marked partial, while streams unavailable after interruption are marked unavailable or not captured.

Provider CLIs run in a separate process group/session. On timeout or `KeyboardInterrupt`, the Harness sends a termination signal and escalates to a forceful kill after a short grace period where the platform supports it. This stops local child processes; it cannot prove that a remote provider request already accepted by a provider was cancelled. Interrupted Claude calls retain process-recovery protection, and interrupted write-capable Codex calls require explicit recovery and target inspection. A cancellation or timeout does not advance the workflow stage. The active workflow runtime limit pauses while awaiting human approval; crash recovery conservatively accounts for time spent while a saved workflow was still marked running.

Proposal versions, completed review reports, implementation/correction summaries, the reviewed patch, and the target fingerprint are associated by SHA-256 evidence in `state.json`. Before writing `delivery.md`, the Harness checks that the saved proposal and review reports still match their recorded hashes and that the eligible target patch and target fingerprint still match the final review snapshot. It records a hash of `delivery.md` after writing; `status` and `resume` check completed delivery integrity again. A mismatch, missing artifact, unavailable fingerprint, or incomplete final verification prevents delivery or is reported as an integrity failure. These hashes detect accidental or subsequent content changes when the state file remains trustworthy; they do not prove authenticity against someone able to rewrite both artifacts and state.

The target fingerprint and review package intentionally exclude known sensitive paths. Their contents are not sent to Claude, but changes confined to an excluded sensitive file's contents cannot be distinguished by the content fingerprint. Other excluded, non-sensitive changed files retain the fingerprint's existing status/content behavior. Operation start/end fingerprints detect edits outside the write operation; concurrent edits inside a Codex write window cannot be distinguished from Codex edits. Snapshots remain point-in-time records, not a complete operation log.

Codex JSONL events are summarized into bounded event counts, tool item types and names, turn status, and usage/model values only when the CLI explicitly reports them. The harness does not persist the JSONL stream, tool arguments, tool output, or prompts. `status` shows these safe evidence fields. The configured model and optional CLI version probe are labeled by source; the resolved model, token usage, or tool outcomes remain unavailable when the CLI does not report them. The final response text is retained temporarily in the operation record for safe resume recovery and is cleared when its stage advances; this existing recovery value is separate from the audit hashes.

New workflows record initial harness and target snapshots in `state.json` before provider execution. Startup fails closed if the initial target fingerprint cannot be captured or changes during baseline capture. The target is fingerprinted again at terminal outcomes; completed deliveries include that final snapshot in `delivery.md`. Version 1 hashes the complete HEAD commit ID plus a deterministic, sorted sequence of length-prefixed fields for each included relative path: HEAD tree mode/type/object ID, index mode/object ID/stage, untracked marker, and current worktree kind/mode/size/content hash. Regular files are streamed into SHA-256, symbolic links hash their link target, and submodules contribute their current commit, working-tree hash, and clean flag. Directories and special objects encountered for Git-reported paths contribute only kind and mode; special objects are never opened. Git does not normally enumerate untracked FIFOs or device nodes, so those are outside the repository fingerprint. `.git` directories/files, target `.engineering/workflows/**` artifacts, ignored untracked files, and known sensitive path components (including `.env.*` names) are omitted from per-path content hashing. Tracked ignored files remain included.

Sensitive-path handling intentionally has two scopes in v1. A sensitive path's content and per-path Git object IDs are omitted, but its presence in HEAD, the index, or non-ignored untracked paths contributes to `excluded_sensitive_path_count`, and its Git status contributes to `working_tree_clean`, `staged_changes`, `unstaged_changes`, and `untracked_files`. Internal workflow artifacts are excluded from both status and counters. These fields disclose status/count only, not path names or contents. The recorded `commit_sha` identifies the complete HEAD tree, so committing a change only to an excluded sensitive path changes both `commit_sha` and the v1 working-tree hash through that commit ID. Submodule commit IDs and clean flags similarly participate in the parent path hash. These are intentional v1 identity/status signals, not content hashing of the sensitive file.

Initial and final fingerprints are point-in-time snapshots, not an operation log. The final path summary compares the final target inventory against the starting baseline and starting pre-existing path list. It reports final-state paths not marked pre-existing at initialization; it cannot prove when or who changed them, or record transient changes such as a file created and deleted before the final snapshot. Workflow operation records provide separate process-level evidence, but they do not establish file authorship. Terminal fingerprint failures are recorded as unavailable; a new workflow cannot start without a stable target baseline. Older workflow states remain readable without fabricated baseline metadata.

## Workspace and review boundaries

- The target must be an absolute Git repository root. Codex runs with the target as its workspace and does not need per-file approvals for ordinary engineering work.
- The Codex CLI approval policy is supplied as a top-level option before `exec` (`codex --ask-for-approval never exec ...`); ordinary commands run under the selected sandbox without repeated human approval.
- Dependency installs are limited to dependencies declared by the repository and its package manager; arbitrary download URLs and custom registries are outside the workflow authorization.
- The Codex planning stage is read-only and retains the user configuration in `CODEX_HOME`, including the configured model provider and proxy. To isolate planning from user-configured MCP servers, the harness reads their names and passes explicit `enabled = false` overrides; malformed or unreadable user config fails closed before a provider call. When the assignment contains explicit HTTPS documentation URLs, planning, plan-refinement, and human proposal-revision calls can use a temporary `documentation_reader` MCP server. Each request is authorized against the exact canonical URLs supplied by the workflow; page links do not expand authorization, and redirects are followed only when their full destination URL was also supplied. OpenAPI `path_filter` is applied locally to the fetched response. Retrieved content is untrusted data and must not be treated as workflow instructions. The reader uses public Internet addresses only, connects to an IP address that passed validation while verifying TLS against the original hostname, uses a total request deadline, and does not use proxies or credentials. If the target's `.codex/config.toml` declares MCP servers, read-only planning fails closed because project MCP isolation is not supported by this invocation. Implementation and correction receive no documentation reader.
- Claude receives complete, secret-redacted review sections. The harness filters operating-system metadata and binary files before constructing a code-review diff, and lists each excluded changed path with its reason. It validates required sections and the final serialized payload before marking a call submitted. Required context is never truncated.
- Code review keeps one call when the complete prompt fits. Otherwise, changed files are grouped using their actual paths and names, and each part is sized from its complete serialized prompt. A file that still does not fit is split only at conservative patch boundaries; when no safe split fits, the campaign stops and records the file as unreviewed. Every eligible patch segment is assigned once, with a hash and line range.
- Every part receives the same hashed assignment, approved proposal, implementation summary, repository instructions, relevant skill application, inferred module map, and independent architecture/product/engineering review rubric. Multi-part campaigns review the individual reports together in a final integration call; the consolidated report is not complete until all assigned files and that integration review are complete.
- Claude reviews as an independent senior architect, backend engineer, product engineer, and technical reviewer. It verifies requirements and challenges the approved design, assesses proportionality, and distinguishes Must fix, Should fix, and Optional improvements. Optional improvements do not trigger the correction stage by themselves.
- `HARNESS_CLAUDE_MAX_PAYLOAD_CHARS` sets the positive-integer preflight limit for the complete serialized prompt after redaction and formatting. It defaults to 600,000 characters; set it explicitly before invoking the orchestrator when a different preflight limit is needed. The effective limit, character count, UTF-8 byte count, and prompt hash are recorded for each validated context attempt. This is a harness setting, not a claim about the provider's request limit; that limit is not established by this integration.
- Each Claude call records the prompt SHA-256, character count, proposal version, review iteration or campaign part, shared-context hash, per-section hashes, call status and reported cost in `state.json`.
- Planning and implementation prompts include applicable `AGENTS.md` contents, the Engineering Judgment & Proportional Design guidance, and the full content of stage-selected skills. Claude plan and code reviews receive the full selected skill content as prompt context; Claude does not load skills from its own filesystem.
- Codex and Claude prompts omit the full file-path inventory when it exceeds 32,000 characters and direct the reviewer to focused repository searches instead.
- Project skills are discovered from the target repository's `.agents/skills/**/SKILL.md` and `.codex/skills/**/SKILL.md`. The harness also supplies the skills bundled under its own `.agents/skills`, so they are available when the target is another repository; a target skill with the same name takes precedence. For skills that link to local `references/...` Markdown files, the harness includes those files with the skill content. Skill files are read when a workflow starts, saved in the existing `project_guidance` state, refreshed by `revise`, and reused by `resume`.
- Skill selection is provider-independent and stage-specific. Planning and plan review consider `architecture-design`, `software-engineering`, and `testing-strategy`; refinement and proposal revision use those skills when relevant to the proposal; implementation uses software engineering and testing guidance plus architecture guidance when the task concerns boundaries; code review uses `code-review`, software engineering, and testing; correction gets those same review and implementation skills plus relevant architecture guidance. The full skill body is injected only for selected skills. Custom target skills are selected deterministically when meaningful words from their description match the assignment and stage context.
- New workflow authorizations explicitly include relevant skill instructions in Claude's permitted prompt data. A saved workflow with the older authorization scope remains valid and receives skill metadata only; resuming it does not silently expand its external data scope.
- The harness currently discovers one user-installed skill, `api-engineer`, from known `CODEX_HOME` skill/plugin paths when the assignment mentions API, REST, or endpoint work. It does not search for arbitrary user-installed skills. To add a project skill, create a directory under target `.agents/skills/<name>/` or `.codex/skills/<name>/` containing a `SKILL.md` with `name` and `description` frontmatter; optional interface metadata belongs in `agents/openai.yaml`. Skills guide the work but do not override the assignment, repository instructions, or workflow authorization.
- Correction is a bounded repair step: Codex receives the original findings, assignment, proposal, implementation summary, repository instructions, and selected skills. It must address only valid assigned findings, avoid unrelated changes, preserve other behavior, and add or update focused tests where needed.
- The baseline is the exact start fingerprint plus `HEAD`, index, worktree, untracked paths, and submodule state. Dirty paths are snapshotted before workflow artifacts are created. Review diffs are calculated from that saved state, so edits made after the baseline remain visible even when they touch a path that was already dirty.
- Claude receives the attributable workflow diff and only commitment-selected baseline/final source files, including ledger `relevant_sources`, Codex source references, and direct Python imports when resolvable. Prompts label baseline content as context only. New-change commitments need a cited source path changed during the implementation operation; baseline code can satisfy a commitment only when its approved ledger explicitly sets `implementation_basis` to `existing_behavior_acceptable` and current validation passes. Tests and source references remain bound to their operation and target fingerprint.
- Review context excludes sensitive files, metadata, binaries, symlinks, and source files that exceed the bounded context limits. Pre-existing regular-file snapshots preserve up to 2 MiB per file and 16 MiB total; if relevant baseline content exceeds those limits, the Harness records it as unavailable and blocks evidence that depends on it. It does not stage, reset, stash, clean, or rewrite user changes.
- Code reviews receive the complete eligible patch across one or more requests. A payload limit applies to each request, including shared context and part content. No source is silently truncated. If the shared context or an indivisible file segment cannot fit, the workflow stops with an explicit unreviewed reason.
- The review report contract, part validation, and local `recover-review` behavior are documented in [docs/code-review-pipeline.md](docs/code-review-pipeline.md).
- The one `--budget-usd` limit applies across Claude calls. The configured provider receives the remaining amount based on recorded spend as a hard call cap. Actual reported costs are accumulated. A completed call without reported cost sets `budget_unknown` but temporarily does not block later calls; only known costs count toward the recorded budget, so actual spend may exceed the configured limit. Interrupted calls still require explicit recovery before the workflow can continue.
- **Temporary deviation (2026-11-18):** Aily sometimes returns a completed Claude review without `cost_usd`. The Harness records incomplete spend metadata and continues so workflows are not blocked. Restore the stop-on-unknown-cost guard after Aily reports reliable per-call costs; keep this exception visible in state and delivery reports until then.
- The workflow authorization allows at most two plan reviews and the saved maximum number of calls for one logical code-review campaign. Individual parts and the integration review share the same budget. Existing one-call authorizations are never silently expanded. The external review data scope remains the assignment, proposal, implementation/validation summary, and relevant redacted code changes.
- The workflow authorization does not include commits, pushes, merges, deployments, publication, or shared-infrastructure changes. Those require separate explicit authorization. Codex is instructed to preserve unrelated files and changes.

These are workflow guardrails. They do not override OS, provider, or organization permissions, and they are not an OS-level process sandbox.

## Deferred follow-ups

- Reduce code-review prompt size. In AskDoc issue 20, one review prompt contained 338,175 characters: 132,707 selected source context, 87,403 complete patch, 37,072 approved proposal, 27,534 commitment/evidence ledger, 23,776 implementation summary, and 14,376 relevant skills. Investigate removing redundant context while preserving full eligible patch coverage. Lowering the payload limit alone is not compression; it can split the review into more calls and repeat shared context.

## Development checks

```sh
python -m unittest discover -s tests
python -m compileall -q harness tests
git diff --check
```

The workflow tests use fake provider processes; they do not contact external services.


## Project ownership and permissions

Copyright © 2026 Sebastian Cosentino. All rights reserved.

This repository is public for viewing and evaluation. Unless you have separate, express written permission from the copyright holder, no license is granted to use, copy, modify, adapt, publish, distribute, sublicense, or create derivative works from this project's code or documentation. Public visibility on GitHub does not place this project in the public domain or make it open source.

The project may include references to and integrations with third-party tools. Their names and trademarks belong to their respective owners; this notice applies only to original material in this repository.

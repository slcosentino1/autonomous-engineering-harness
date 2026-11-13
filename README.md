# Autonomous Engineering Harness

A small Python workflow that plans an assignment, waits for approval, implements it with Codex, and reviews the resulting change with Claude through Aily.

## Requirements

- Python 3.11 or newer
- Codex CLI (`codex`)
- Aily CLI (`aily`) for Claude plan and code reviews
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
5. Claude reviews the change in one call when the complete context fits. Larger diffs are grouped into coherent parts and receive a final integration review. If the consolidated report requests changes, Codex evaluates the findings, corrects valid issues, and reruns affected checks once. The review is not repeated automatically.
6. A completed workflow writes a human-readable delivery report.

Each stage is recorded in one small JSON state file. `resume` retries only the failed stage. It does not replan completed work or cross the approval gate.

Starting the workflow writes one persistent authorization into that state. It binds normal development operations to the exact target and workflow ID, authorizes the configured Claude/Aily reviews within the declared budget, and is reused on every stage and resume. The saved scope is checked before resuming; approval and resume commands do not request or replace the authorization.

## Start a workflow

Provide an absolute target path and assignment. Claude/Aily calls default to a total budget of USD 2.00; override it with `--budget-usd`. The workflow prints and stores the effective budget. A Codex model is optional; without `--model`, the CLI's configured default is used.

New workflows authorize at most 16 Claude code-review campaign calls by default, including any final integration review. Set `--max-code-review-calls` from 1 to 64 to authorize a different maximum. The limit is saved at workflow start and applies to every part, integration review, and retry; it never expands on resume. Existing workflows that only authorize one code-review call retain that limit.

```sh
python /path/to/autonomous-engineering-harness/harness/orchestrator.py start \
  --target /absolute/path/to/target-repository \
  --task-file /absolute/path/to/assignment.md
```

For example, `--budget-usd 0.75` overrides the budget, and `--model MODEL_NAME` selects a Codex model explicitly. The selected model is saved with the workflow and reused on approval/resume. If omitted, no `--model` argument is passed to Codex.

## Optional Langfuse tracing

Tracing is disabled by default. Enable it for a new workflow with `start --trace` or set `HARNESS_TRACING_ENABLED=true` before `start`; either setting enables tracing. Credentials come from `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`; `LANGFUSE_BASE_URL` is optional and defaults to `https://cloud.langfuse.com`.

Install the optional SDK once in the persistent environment with `python -m pip install -r requirements-tracing.txt`. When `--trace` is explicit, a local preflight checks that the SDK imports in the current interpreter, credentials and endpoint are configured, and the SDK client can initialize. If the SDK is missing, the command stops before creating a workflow and tells you to activate the same environment and install the optional requirements. This local check does not verify remote connectivity or guarantee trace delivery. Tracing enabled only through `HARNESS_TRACING_ENABLED=true` keeps the existing best-effort startup behavior without the explicit-flag preflight. Langfuse events contain operational metadata only: workflow command, a stable pseudonymous session ID, stage/provider/operation, configured model when known, elapsed duration, status, review-part index/count, and reported provider cost. The raw workflow ID, prompts, assignments, feedback, source, diffs, reports, CLI output, secrets, and filesystem paths are not sent. Target validation status is omitted unless the workflow has reliable structured evidence for it.

The descriptions shown in Langfuse Input and Output are generated from these safe fields (for example, provider, operation, stage, status, duration, and reported cost). Prompt and project content are not used to populate them.

The authorization saved at `start` records whether tracing is enabled, its fixed metadata scope, and a hash of the authorized Langfuse endpoint. It never stores credentials. Approval, revision, and resume reuse that saved setting and endpoint; older workflows without an observability scope remain tracing-disabled. Trace data is buffered locally while the workflow lock is held and exported only after the operation releases the lock. Langfuse initialization, export, and shutdown failures do not affect workflow results.

Elapsed `duration_ms` values come from the harness monotonic clock and cover root executions, stages, provider calls, and review parts. The adapter buffers observations until after execution and records elapsed duration as metadata; `duration_ms` is the authoritative duration. Aily-reported Claude cost is also attached as native generation `cost_details.total`; token usage and model names are omitted when the provider does not report them.

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

Workflow files are stored under `.engineering/workflows/WORKFLOW_ID/` in the target: `state.json`, `proposal.md`, and, on completion, `delivery.md`.

## Workspace and review boundaries

- The target must be an absolute Git repository root. Codex runs with the target as its workspace and does not need per-file approvals for ordinary engineering work.
- The Codex CLI approval policy is supplied as a top-level option before `exec` (`codex --ask-for-approval never exec ...`); ordinary commands run under the selected sandbox without repeated human approval.
- Dependency installs are limited to dependencies declared by the repository and its package manager; arbitrary download URLs and custom registries are outside the workflow authorization.
- The Codex planning stage is read-only. When its assignment contains explicit HTTPS documentation URLs, planning, plan-refinement, and human proposal-revision calls can use a temporary `documentation_reader` MCP server. Each request is authorized against the exact canonical URLs supplied by the workflow; page links do not expand authorization, and redirects are followed only when their full destination URL was also supplied. OpenAPI `path_filter` is applied locally to the fetched response. Retrieved content is untrusted data and must not be treated as workflow instructions. The reader uses public Internet addresses only, connects to an IP address that passed validation while verifying TLS against the original hostname, uses a total request deadline, and does not use proxies or credentials. Read-only planning invocations use the supported `--ignore-user-config` option to exclude user-configured MCP servers while retaining authentication from `CODEX_HOME`; configured user model/provider preferences are not loaded for those planning stages, though an explicit workflow model is passed. If the target's `.codex/config.toml` declares MCP servers, read-only planning fails closed because project MCP isolation is not supported by this invocation. Implementation and correction receive no documentation reader.
- Claude receives complete, secret-redacted review sections. The harness filters operating-system metadata and binary files before constructing a code-review diff, and lists each excluded changed path with its reason. It validates required sections and the final serialized payload before marking a call submitted. Required context is never truncated.
- Code review keeps one call when the complete prompt fits. Otherwise, changed files are grouped using their actual paths and names, and each part is sized from its complete serialized prompt. A file that still does not fit is split only at conservative patch boundaries; when no safe split fits, the campaign stops and records the file as unreviewed. Every eligible patch segment is assigned once, with a hash and line range.
- Every part receives the same hashed assignment, approved proposal, implementation summary, repository instructions, relevant skill application, inferred module map, and independent architecture/product/engineering review rubric. Multi-part campaigns review the individual reports together in a final integration call; the consolidated report is not complete until all assigned files and that integration review are complete.
- Claude reviews as an independent senior architect, backend engineer, product engineer, and technical reviewer. It verifies requirements and challenges the approved design, assesses proportionality, and distinguishes Must fix, Should fix, and Optional improvements. Optional improvements do not trigger the correction stage by themselves.
- `HARNESS_CLAUDE_MAX_PAYLOAD_CHARS` sets the positive-integer preflight limit for the complete serialized prompt after redaction and formatting. It defaults to 200,000 characters; set it higher explicitly before invoking the orchestrator when needed. The effective limit, character count, UTF-8 byte count, and prompt hash are recorded for each validated context attempt. This is a harness setting, not a claim about Claude's or Aily's provider limit; those limits are not established by this integration.
- Each Claude call records the prompt SHA-256, character count, proposal version, review iteration or campaign part, shared-context hash, per-section hashes, call status and reported cost in `state.json`.
- Planning includes the Engineering Judgment & Proportional Design rule. Codex receives applicable `AGENTS.md` files and skills from target `.agents/skills` and `.codex/skills`; relevant installed skills under `CODEX_HOME` are supplied inline when the CLI invocation disables plugins.
- Codex and Claude prompts omit the full file-path inventory when it exceeds 32,000 characters and direct the reviewer to focused repository searches instead.
- This repository includes the optional [architecture-design skill](.agents/skills/architecture-design/SKILL.md). Codex discovers it when working in the harness repository. To use it in a future target with this workflow, place the skill folder under that target's `.agents/skills/architecture-design`; the controller will pass it to Codex, which should apply it only when its description matches the work. Direct Codex projects can also install it under `CODEX_HOME/skills/architecture-design`.
- Existing dirty and untracked paths are excluded from the Claude patch. Credential and environment paths are also excluded; common credential patterns are redacted.
- Code reviews receive the complete eligible patch across one or more requests. A payload limit applies to each request, including shared context and part content. No source is silently truncated. If the shared context or an indivisible file segment cannot fit, the workflow stops with an explicit unreviewed reason.
- The review report contract, part validation, and local `recover-review` behavior are documented in [docs/code-review-pipeline.md](docs/code-review-pipeline.md).
- The one `--budget-usd` limit applies across Claude calls. Aily receives the remaining amount as a hard call cap. Actual reported costs are accumulated. If a call's cost is unknown or an invocation is interrupted, further Claude calls stop.
- The workflow authorization allows at most two plan reviews and the saved maximum number of calls for one logical code-review campaign. Individual parts and the integration review share the same budget. Existing one-call authorizations are never silently expanded. The Aily data scope remains the assignment, proposal, implementation/validation summary, and relevant redacted code changes.
- The workflow authorization does not include commits, pushes, merges, deployments, publication, or shared-infrastructure changes. Those require separate explicit authorization. Codex is instructed to preserve unrelated files and changes.

These are workflow guardrails. They do not override OS, provider, or organization permissions, and they are not an OS-level process sandbox.

## Development checks

```sh
python -m unittest discover -s tests
python -m compileall -q harness tests
git diff --check
```

The workflow tests use fake Codex and Aily processes; they do not contact external services.

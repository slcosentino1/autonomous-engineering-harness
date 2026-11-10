# Autonomous Engineering Harness

A small Python workflow that plans an assignment, waits for approval, implements it with Codex, and reviews the resulting change with Claude through Aily.

## Requirements

- Python 3.10 or newer
- Codex CLI (`codex`)
- Aily CLI (`aily`) for Claude plan and code reviews
- A Git target repository

The harness has no Python package dependencies.

## Workflow

1. Codex reads the assignment and target repository, then saves a Markdown proposal.
2. Claude reviews the proposal. Codex evaluates the feedback and refines it. One second Claude review is allowed only when the refined plan requests it; there are at most two plan reviews.
3. The workflow stops in `awaiting_approval`. Read the proposal before running `approve`.
4. After approval, Codex implements the plan and runs relevant checks in the target workspace.
5. Claude reviews the change. If it reports findings, Codex evaluates them, corrects valid issues, and reruns affected checks once. The code review is not repeated automatically.
6. A completed workflow writes a human-readable delivery report.

Each stage is recorded in one small JSON state file. `resume` retries only the failed stage. It does not replan completed work or cross the approval gate.

Starting the workflow writes one persistent authorization into that state. It binds normal development operations to the exact target and workflow ID, authorizes the configured Claude/Aily reviews within the declared budget, and is reused on every stage and resume. The saved scope is checked before resuming; approval and resume commands do not request or replace the authorization.

## Start a workflow

Provide an absolute target path and assignment. Claude/Aily calls default to a total budget of USD 2.00; override it with `--budget-usd`. The workflow prints and stores the effective budget. A Codex model is optional; without `--model`, the CLI's configured default is used.

```sh
python3 /path/to/autonomous-engineering-harness/harness/orchestrator.py start \
  --target /absolute/path/to/target-repository \
  --task-file /absolute/path/to/assignment.md
```

For example, `--budget-usd 0.75` overrides the budget, and `--model MODEL_NAME` selects a Codex model explicitly. The selected model is saved with the workflow and reused on approval/resume. If omitted, no `--model` argument is passed to Codex.

The workflow prints its ID and proposal path. Review the proposal, then explicitly approve it:

```sh
python3 /path/to/autonomous-engineering-harness/harness/orchestrator.py approve \
  --target /absolute/path/to/target-repository \
  --workflow-id WORKFLOW_ID
```

To ask for a proposal change, give concise feedback. The workflow uses a remaining Claude plan review when available, then returns to `awaiting_approval`:

```sh
python3 /path/to/autonomous-engineering-harness/harness/orchestrator.py revise \
  --target /absolute/path/to/target-repository \
  --workflow-id WORKFLOW_ID \
  --feedback "Include the empty-input case in the proposal."
```

Reject a proposal to stop without implementation:

```sh
python3 /path/to/autonomous-engineering-harness/harness/orchestrator.py reject \
  --target /absolute/path/to/target-repository --workflow-id WORKFLOW_ID \
  --reason "The proposal is out of scope."
```

Check or resume a workflow with:

```sh
python3 /path/to/autonomous-engineering-harness/harness/orchestrator.py status \
  --target /absolute/path/to/target-repository --workflow-id WORKFLOW_ID

python3 /path/to/autonomous-engineering-harness/harness/orchestrator.py resume \
  --target /absolute/path/to/target-repository --workflow-id WORKFLOW_ID
```

Workflow files are stored under `.engineering/workflows/WORKFLOW_ID/` in the target: `state.json`, `proposal.md`, and, on completion, `delivery.md`.

## Workspace and review boundaries

- The target must be an absolute Git repository root. Codex runs with the target as its workspace and does not need per-file approvals for ordinary engineering work.
- The Codex CLI approval policy is supplied as a top-level option before `exec` (`codex --ask-for-approval never exec ...`); ordinary commands run under the selected sandbox without repeated human approval.
- Dependency installs are limited to dependencies declared by the repository and its package manager; arbitrary download URLs and custom registries are outside the workflow authorization.
- The Codex planning stage is read-only. Implementation and correction use the Codex workspace-write sandbox.
- Claude receives complete, secret-redacted review sections. The harness validates required sections, enforces `HARNESS_CLAUDE_MAX_PAYLOAD_CHARS` (default: 100,000), and refuses oversized or incomplete context before making a paid call. It never truncates required context.
- Each Claude call records the prompt SHA-256, character count, proposal version, review iteration, and per-section hashes in `state.json`; review records refer to that manifest.
- Planning includes the Engineering Judgment & Proportional Design rule. Codex receives applicable `AGENTS.md` files and skills from target `.agents/skills` and `.codex/skills`; relevant installed skills under `CODEX_HOME` are supplied inline when the CLI invocation disables plugins.
- This repository includes the optional [architecture-design skill](.agents/skills/architecture-design/SKILL.md). Codex discovers it when working in the harness repository. To use it in a future target with this workflow, place the skill folder under that target's `.agents/skills/architecture-design`; the controller will pass it to Codex, which should apply it only when its description matches the work. Direct Codex projects can also install it under `CODEX_HOME/skills/architecture-design`.
- Existing dirty and untracked paths are excluded from the Claude patch. Credential and environment paths are also excluded; common credential patterns are redacted.
- Code reviews receive the complete eligible patch. If a new file or full review context exceeds `HARNESS_CLAUDE_MAX_PAYLOAD_CHARS`, the review stops with an explicit error before a paid Claude call.
- The one `--budget-usd` limit applies across Claude calls. Aily receives the remaining amount as a hard call cap. Actual reported costs are accumulated. If a call's cost is unknown or an invocation is interrupted, further Claude calls stop.
- The workflow authorization allows at most two plan reviews and one independent code review, and only the assignment, proposal, implementation/validation summary, and relevant redacted code changes are in the Aily data scope.
- The workflow authorization does not include commits, pushes, merges, deployments, publication, or shared-infrastructure changes. Those require separate explicit authorization. Codex is instructed to preserve unrelated files and changes.

These are workflow guardrails. They do not override OS, provider, or organization permissions, and they are not an OS-level process sandbox.

## Development checks

```sh
python3 -m unittest discover -s tests
python3 -m compileall -q harness tests
git diff --check
```

The workflow tests use fake Codex and Aily processes; they do not contact external services.

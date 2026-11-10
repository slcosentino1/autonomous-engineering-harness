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

## Start a workflow

Provide an absolute target path, assignment, and one total budget for Claude/Aily calls:

```sh
python3 /path/to/autonomous-engineering-harness/harness/orchestrator.py start \
  --target /absolute/path/to/target-repository \
  --task-file /absolute/path/to/assignment.md \
  --budget-usd 2.00
```

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
- The Codex planning stage is read-only. Implementation and correction use the Codex workspace-write sandbox.
- Claude receives the assignment, proposal, summaries, and a redacted patch. It runs in a restricted temporary directory with read-only tools and no target files mounted.
- Existing dirty and untracked paths are excluded from the Claude patch. Credential and environment paths are also excluded; common credential patterns are redacted.
- New files over 500 KB are omitted from the review patch, and the patch is capped at 200 KB. The reviewer sees a note when either limit applies.
- The one `--budget-usd` limit applies across Claude calls. Aily receives the remaining amount as a hard call cap. Actual reported costs are accumulated. If a call's cost is unknown or an invocation is interrupted, further Claude calls stop.
- The workflow does not commit, push, merge, deploy, publish, or change shared infrastructure. Codex is instructed to preserve unrelated files and changes.

These are workflow guardrails. They do not override OS, provider, or organization permissions, and they are not an OS-level process sandbox.

## Development checks

```sh
python3 -m unittest discover -s tests
python3 -m compileall -q harness tests
git diff --check
```

The workflow tests use fake Codex and Aily processes; they do not contact external services.

# Autonomous Orchestrator

`harness/orchestrator.py` is the explicit stage controller. It runs read-only preflight, records the original task and baseline in target-local state, asks Codex for a structured plan, then sends one bounded implementation or correction task at a time to ephemeral `codex exec` sessions with `--sandbox read-only`. Codex returns a JSON result; the controller validates it and applies text changes through `safe_writer.py`. It runs only configured validations, binds their evidence to content identities, and invokes Claude only through `claude_reviewer.py`. Claude's report and invocation evidence are persisted in the target workflow directory. Accepted findings go to a bounded Codex correction call; affected validations run again and a second completed Claude review round is limited by the existing adapter.

The CLI does not stage or commit files, push, merge, create a PR, install dependencies, or deploy. `commit: true` is rejected because the immutable policy requires separate authorization for exact staging paths and the exact staged diff. High-risk work remains `WAITING_FOR_HUMAN` until the authenticated reviewer and distinct authenticated gate approver are recorded. Claude is supplementary for High risk. For Low/Medium work, the controller does not issue `APPROVE`; the cross-record policy gate evaluator is not part of this implementation, so missing gate evaluation remains incomplete.

## Start a task

Supply the absolute Git root, its existing branch, one task prompt, and a JSON file describing validations, review settings, and the exact user-authorized code/artifact path prefixes. The planner cannot widen these prefixes. `code_file_extensions` can further restrict the files Codex may return; an empty array explicitly permits any extension. The task text becomes a target-local `task.json`; requirements and acceptance criteria from planning are stored in `plan.json`.

The config shape is defined in [`orchestrator-config.schema.json`](../harness/schemas/orchestrator-config.schema.json). The CLI supplies the branch separately; the config must include a positive aggregate Claude budget and at least one validation. Review cannot be disabled, and commit/PR options are required to remain false.

```sh
python3 /absolute/path/to/autonomous-engineering-harness/harness/orchestrator.py \
  --target /absolute/path/to/target-repository \
  --branch existing-branch \
  --task-file /absolute/path/to/task.txt \
  --config /absolute/path/to/workflow-config.json \
  --workflow-id stable-task-id
```

Example config (replace the validation commands with commands discovered in the target and inspected for effects):

```json
{
  "minimum_risk_band": "medium",
  "max_iterations": 2,
  "stage_timeout_seconds": 3600,
  "claude_budget_usd": 2.0,
  "authorized_code_paths": ["src/features/example", "tests/features/example", "src/FeatureExtensions.cs"],
  "authorized_artifact_paths": [],
  "code_file_extensions": [".cs"],
  "validations": [
    {
      "id": "unit-tests",
      "argv": ["dotnet", "test", "path/to/tests.csproj", "--no-restore"],
      "cwd": ".",
      "timeout_seconds": 3600
    }
  ],
  "review": {
    "enabled": true,
    "context_paths": ["AGENTS.md", "README.md"]
  },
  "commit": false,
  "create_pr": false
}
```

Commands are argument arrays, never shell strings. The controller rejects shell, Git, known direct network utilities, package installation, restore, clean, and publication command shapes; `dotnet test` and `dotnet build` require `--no-restore`. Offline environment settings are hints, not an OS-level network sandbox. Inspect the effects of selected target tests before listing them, and do not configure tests that contact services or mutate shared state without the exact policy authorization. A config entry does not authorize a dependency installation, download, shared service, destructive operation, or external call. Explicit code and artifact path prefixes are the upper bound on every planned increment, and code extensions are checked again before safe-writer applies a file change.

Use the same target, task text, branch, workflow ID, and semantically identical workflow configuration to resume. The configuration fingerprint is persisted with the workflow; changing path scopes, validations, reviewer settings, or budgets requires a new workflow ID. The workflow state is `.engineering/workflows/<workflow-id>/workflow-state.json`. Completed plans and validated increments are reused after target identity and pre-existing changes are rechecked; review resumes only against the recorded content identity. Failed validation/Codex calls remain in state. A failed Claude result is recovered on the next run through the adapter's persisted `recover_uncompleted_review_round` transition, within its attempt and aggregate-budget limits. A round with a persisted invocation claim but no result is not repeated because it cannot be proven that the call did not run. Repeated identical blockers do not trigger an infinite retry loop. Unsupported or incomplete approvals remain `BLOCKED` or `WAITING_FOR_HUMAN`, not `COMPLETED`.

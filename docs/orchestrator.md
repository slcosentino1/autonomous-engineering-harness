# Autonomous Orchestrator

`harness/orchestrator.py` is the explicit stage controller. It runs read-only preflight, records the original task and baseline in target-local state, asks Codex for a structured plan, then sends bounded implementation, self-review, or correction tasks to ephemeral `codex exec` sessions with `--sandbox read-only`. Codex returns JSON; the controller validates it and applies text changes through `safe_writer.py`. It runs only configured validations, binds their evidence to content identities, and invokes Claude only through `claude_reviewer.py`. Claude's report and invocation evidence are persisted in the target workflow directory. Accepted findings go to a bounded Codex correction call; affected validations run again and a second completed Claude review round is limited by the existing adapter.

This is the sole supported harness workflow execution engine. `harness/runner.py` is deprecated legacy code and is not an execution entrypoint.

The CLI does not stage or commit files, push, merge, create a PR, install dependencies, or deploy. `commit: true` is rejected because the immutable policy requires separate authorization for exact staging paths and the exact staged diff. High-risk work remains `WAITING_FOR_HUMAN` until the authenticated reviewer and distinct authenticated gate approver are recorded. Claude is supplementary for High risk. For Low/Medium work, the controller does not issue `APPROVE`; the cross-record policy gate evaluator is not part of this implementation, so missing gate evaluation remains incomplete.

When a stage needs human input, the controller stores a categorized pending decision with its question, proposal, alternatives, reason, requesting stage, applicable policy/skill reference, authority, and append-only response history. It returns `WAITING_FOR_HUMAN`.

### Inspect and answer a pending decision

Use the same absolute target and workflow ID:

```sh
python3 /absolute/path/to/autonomous-engineering-harness/harness/orchestrator.py \
  --target /absolute/path/to/target-repository \
  --workflow-id stable-task-id \
  --list-pending-decisions
```

Submit an explicit response:

```sh
python3 /absolute/path/to/autonomous-engineering-harness/harness/orchestrator.py \
  --target /absolute/path/to/target-repository \
  --workflow-id stable-task-id \
  --respond-decision PLAN-CLARIFICATION \
  --response-action approve \
  --response "I approve the proposed behavior."
```

The response action accepts `approve`, `reject`, `changes`, or `alternative`. Include the answer or requested changes in `--response`. Every response is appended with its timestamp, source, local operator label, and `identity_assurance: declared`. The CLI does not authenticate the operator. For `functional_decision`, the controller resolves and resumes only when planning recorded a target instruction reference that explicitly permits interactive acceptance. Codex interprets the cited project instruction; the controller checks the recorded category and reference but does not independently parse that instruction's semantics. Rejection, requested changes, and alternatives are passed back to planning as the resolved human direction; the prior question and responses remain in state.

For external data-transfer authorization, restricted operations, security-risk acceptance, High-risk review/approval, and criterion waivers, the interactive response is recorded but does not satisfy the required authorization or authenticated authority. It cannot create an authorization record or approve a gate. Use the separately defined authorization and human-review procedures; if the trusted evidence is unavailable, the workflow stays waiting.

To continue, rerun the original workflow command with the same target, branch, task text, config, and workflow ID. The controller revalidates the target and reuses a persisted plan and completed increments when their inputs remain valid. A planning decision with no plan is sent back to planning together with its recorded response history. Do not edit workflow state manually to claim authentication or approval.

## Start a task

Supply the absolute Git root, its existing branch, one task prompt, and a JSON file describing validations, review settings, and the exact user-authorized code/artifact path prefixes. The planner cannot widen these prefixes. `code_file_extensions` can further restrict the files Codex may return; an empty array explicitly permits any extension. The initial prompt is retained in `task.json` and a versioned `requirements-v1.json`; the current requirements and plan are stored under the workflow directory.

The config shape is defined in [`orchestrator-config.schema.json`](../harness/schemas/orchestrator-config.schema.json). The CLI supplies the branch separately; the config must include a positive aggregate Claude budget and at least one validation. Review cannot be disabled, and commit/PR options are required to remain false.

Claude review runs automatically after Codex implementation, self-review, and successful configured validations when a currently valid `review.authorization` covers the canonical target repository and every exact file in the review packet. The authorization is a reusable configuration record; it is not requested again for each call. Without one, or when it is expired, revoked, over budget, or out of scope, the workflow enters `WAITING_FOR_HUMAN` before reserving a review round or contacting Aily. Supply the authorization in the workflow config and resume with the same workflow ID. An authorization-only config change is recorded without replanning; changes to task, validation, or write-scope configuration still require the explicit requirement-update flow. The authorization records the provider/destination, canonical repository allowlist, relative file prefixes, read-only mode, maximum USD budget, required sensitive-data exclusions, validity interval, revocation state, authorizer, assurance level, and source reference. Declared identity remains declared; the harness does not authenticate the user. The review grant is separate from functional decisions and from High-risk human review or gate approval.

The budget is the lesser of the persistent grant's `max_budget_usd` and this workflow's `claude_budget_usd`, accumulated from known Aily usage in this workflow. An attempted call with unknown cost blocks further use because the remaining budget cannot be established. Usage is logged in workflow state by authorization ID, review/round, revision, scope, attempt status, invocation/session ID, model, tokens, and cost when exposed. Usage is not aggregated across separate workflow-state files; use a distinct grant/budget for each workflow if a cross-workflow aggregate ceiling is required.

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
    "context_paths": ["AGENTS.md", "README.md"],
    "authorization": {
      "authorization_id": "CLAUDE-REVIEW-2026-01",
      "provider": "aily",
      "destination": "Claude through Aily",
      "permitted_repositories": ["/absolute/canonical/path/to/target-repository"],
      "permitted_paths": ["src/features/example", "tests/features/example", ".engineering/workflows/stable-task-id"],
      "mode": "read_only",
      "max_budget_usd": 2.0,
      "sensitive_exclusions": ["credentials", "environment_files", "private_configuration", "unrelated_files"],
      "authorized_at": "2026-01-01T00:00:00Z",
      "expires_at": "2027-01-01T00:00:00Z",
      "revoked": false,
      "authorization_reference": "user authorization record or task reference",
      "authorizer_identity": "declared user identity",
      "identity_assurance": "declared",
      "authorizer_authentication_reference": null
    }
  },
  "commit": false,
  "create_pr": false
}
```

Commands are argument arrays, never shell strings. The controller rejects shell, Git, known direct network utilities, package installation, restore, clean, and publication command shapes; `dotnet test` and `dotnet build` require `--no-restore`. Offline environment settings are hints, not an OS-level network sandbox. Inspect the effects of selected target tests before listing them, and do not configure tests that contact services or mutate shared state without the exact policy authorization. A config entry does not authorize a dependency installation, download, shared service, destructive operation, or external call. Explicit code and artifact path prefixes are the upper bound on every planned increment, and code extensions are checked again before safe-writer applies a file change.

Every Codex stage trace lists discovered skills, paths requested through stage context, successful skill/reference reads observed in the `codex exec --json` event stream, and requested skills whose reads remain unverifiable. The controller stores event IDs and hashes for the command/event output, not the returned skill text or raw event stream. A skill mentioned only in an agent response is not evidence that it was read. The recognizer is intentionally conservative: only completed successful direct read events with an attributable skill path count as verified; compound shell commands and ambiguous runtime events remain unverified.

Use the same target, task text, branch, workflow ID, and semantically identical workflow configuration to resume. The workflow state is `.engineering/workflows/<workflow-id>/workflow-state.json`. For ordinary resume, matching task and config fingerprints are required. If a requirement or architecture decision changes, explicitly update that existing workflow:

```sh
python3 /absolute/path/to/autonomous-engineering-harness/harness/orchestrator.py \
  --target /absolute/path/to/target-repository \
  --branch existing-branch \
  --task-file /absolute/path/to/revised-task.txt \
  --config /absolute/path/to/workflow-config.json \
  --workflow-id stable-task-id \
  --update-requirements \
  --update-reason "The approved timeline architecture changed to the CMS/DataView pattern."
```

The update appends `requirements-vN.json` and an invalidation event; it preserves older task snapshots, plans, decisions, scopes, validations, review cycles, and approval evidence. The event identifies the superseded plan, increments, decisions, scope, validations, review cycles, authorizations, assignments, and any prior completion record. Old authorizations are revoked. The current plan and active write scope are cleared. Configuration changes are accepted only with this explicit update operation; the supplied config is the new explicit upper bound, and the planner cannot expand it. Replanning uses a new versioned plan artifact and version-prefixed increment IDs so old review cycles cannot be reused. It rechecks architecture (including DataView/non-DataView where applicable), capabilities, dependencies, acceptance criteria, scopes, and configured validations before implementation resumes. Do not update a workflow whose target HEAD or protected pre-existing changes changed; that remains a separate blocker.

Each increment implementation receives a controller-built context containing the current requirement version and hash, applicable non-invalidated decisions, architecture and documentation context, selected capabilities and source references, acceptance criteria, current allowlists and scope, dependencies/blockers, and the configured validations. The orchestrator then runs a structured Codex self-review before any validation. Self-review can return scoped corrections, after which it repeats the review within the configured iteration bound; unresolved findings block validation. This is explicitly self-review, never an independent review or approval.

Every Codex stage attempt records a trace with harness revision and source/runtime fingerprints, CLI/model/provider when available, prompt hash, requested instructions/skills and available source hashes, the read-evidence limitation, sandbox/MCP/plugin settings, outcome and sanitized diagnostics, plus changed paths and content hashes. The prompt and returned file contents are not persisted in this trace. A source fingerprint proves which content was available at trace time, not that Codex read it; without a Codex tool-event log, read evidence is recorded as unavailable. Exact tool inventory is runtime-managed and not exposed by the controller.

Failed validation/Codex calls remain in state. A failed Claude result is recovered on the next run through the adapter's persisted `recover_uncompleted_review_round` transition, within its attempt and aggregate-budget limits. A round with a persisted invocation claim but no result is not repeated because it cannot be proven that the call did not run. Repeated identical blockers do not trigger an infinite retry loop. Unsupported or incomplete approvals remain `BLOCKED` or `WAITING_FOR_HUMAN`, not `COMPLETED`.

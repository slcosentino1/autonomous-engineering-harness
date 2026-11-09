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

Supply the absolute Git root, its existing branch, one task prompt, and a JSON file describing validations, review settings, and the exact user-authorized code/artifact path prefixes. The planner cannot widen these prefixes. `code_file_extensions` can further restrict the files Codex may return; an empty array explicitly permits any extension. Optionally set `base_ref` (for example, `origin/main`) to pin a requested base reference; if omitted, the controller uses the branch's configured upstream, or `HEAD` when no upstream exists. At workflow start, the controller resolves that reference to an exact commit and keeps that `base_commit` unchanged. The initial checked-out `HEAD` remains separately stored as `baseline.commit`. The initial prompt is retained in `task.json` and a versioned `requirements-v1.json`; the current requirements and plan are stored under the workflow directory.

### Git baseline and worktree identity

The workflow state records `orchestration.git_tracking.base_ref_at_start`, its immutable `base_commit`, the assigned `workflow_branch`, and the canonical `target_worktree`. On resume, the controller verifies that it is still using that worktree and branch, and rejects a branch that Git reports as checked out in any other worktree. It does not create, switch, reset, clean up, or remove worktrees or branches. There is no detached-worktree creation lifecycle in this orchestrator; start it in the intended named-branch worktree. Supplying a commit ID as `base_ref` pins that exact commit without checking it out.

Git refs and remotes are shared between linked worktrees. The controller does not fetch, but observes the pinned base ref and configured upstream at initialization, resume, and final status reporting. A changed remote-tracking ref is appended to `orchestration.git_tracking.reference_movements` with its old and new commit, observation time, workflow ID, and observing operation. This event is separate from `baseline.commit` and the pinned `base_commit`. The controller never pulls, resets, rebases, merges, fast-forwards, or replaces either baseline automatically. A remote ref can move while the workflow is running; movement is recorded the next time the controller observes it.

The CLI result's `git_state` distinguishes the workflow baseline commit, pinned base commit/reference, current branch and `HEAD`, worktree cleanliness, observed upstream commit, ahead/behind counts, and whether the upstream moved since workflow start. A branch behind the currently observed upstream is reported as such; it is not treated as worktree corruption or as evidence that the workflow baseline or files changed. Worktree cleanliness reflects Git status at the target path, including target-local workflow artifacts.

### Untracked `.DS_Store` snapshot policy

New workflows omit untracked regular files named `.DS_Store` from the initial Git status snapshot, later untracked-path checks, and revision manifests. These files are volatile macOS Finder metadata and can change without a project edit. The exclusion is limited to this exact basename. Other untracked files remain in the snapshot and keep their original content fingerprints. Tracked or staged `.DS_Store` files remain visible to status checks and revision manifests and receive the same protection as other tracked or staged files. Untracked symlinks named `.DS_Store` are also retained so path and link checks are not bypassed.

Workflows created before this policy keep their original behavior unless explicitly migrated: their status checks continue to include untracked `.DS_Store` files. The one supported historical migration is scoped to the named Ohpen workflow and its root `.DS_Store`. It appends a migration record to `orchestration.snapshot_migrations`, records the old and current SHA-256 values, reason, timestamp, unavailable original bytes, and authorization ID, and retains the original `baseline.preexisting_changes` and `orchestration.preexisting_hashes` entries unchanged. The migration adds a declared human authorization to the existing `authorizations` collection. The controller validates the linked record before it omits that exact path from integrity checks, and only while the file remains an untracked regular file. A missing, staged, or tracked file follows the ordinary integrity checks. Other pre-existing paths and fingerprints stay protected.

Apply that migration only after explicit authorization, with the exact recorded and current fingerprints:

```sh
python3 /absolute/path/to/autonomous-engineering-harness/harness/orchestrator.py \
  --target /absolute/path/to/migration-jobs-api-harness-v2 \
  --workflow-id migration-jobs-api-harness-v2 \
  --migrate-untracked-ds-store \
  --expected-old-sha256 <recorded-sha256> \
  --expected-current-sha256 <current-sha256> \
  --authorization-reference <explicit-user-task-reference>
```

This command changes only the existing workflow state through `WorkflowStore` and `safe_writer.py`; it does not resume the workflow. It is idempotent and refuses a mismatched target, workflow stage, path, hash, or authorization reference. Workflows without a migration record retain their previous snapshot semantics.

The config shape is defined in [`orchestrator-config.schema.json`](../harness/schemas/orchestrator-config.schema.json). The CLI supplies the branch separately; the config must include a positive aggregate Claude budget and at least one validation. Review cannot be disabled, and commit/PR options are required to remain false.

Claude review runs automatically after Codex implementation, self-review, and successful configured validations when a currently valid `review.authorization` covers the canonical target repository and every exact file in the review packet. The authorization is a reusable configuration record; it is not requested again for each call. Without one, or when it is expired, revoked, over budget, or out of scope, the workflow enters `WAITING_FOR_HUMAN` before reserving a review round or contacting Aily. Supply the authorization in the workflow config and resume with the same workflow ID. Stored-plan fingerprints cover planning-relevant settings; the reviewer configuration and Claude budget are excluded, so authorization or review-scope changes do not invalidate an implementation plan. Older plan artifacts with a full-config fingerprint remain readable when the current configuration differs only by adding or changing the reviewer authorization. Changes to task, validation, or write-scope configuration still require the explicit requirement-update flow. The authorization records the provider/destination, canonical repository allowlist, relative file prefixes, read-only mode, maximum USD budget, required sensitive-data exclusions, validity interval, revocation state, authorizer, assurance level, and source reference. Declared identity remains declared; the harness does not authenticate the user. The review grant is separate from functional decisions and from High-risk human review or gate approval.

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
  "base_ref": "origin/main",
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

Use the same target, task text, branch, workflow ID, and semantically identical workflow configuration to resume. The workflow state is `.engineering/workflows/<workflow-id>/workflow-state.json`. For ordinary resume, matching task and config fingerprints are required. A repeated blocker can be retried when its recorded execution context changes: the controller fingerprints the blocked increment revision, task/configuration and requirement version, orchestrator/runtime snapshot, and Codex runtime metadata. It records that fingerprint on the resume attempt and permits another retry only after a different fingerprint is observed; an unchanged repeated blocker stays blocked. Legacy resume records without context fingerprints can be retried once only when a subsequent recorded stage trace proves that the harness or execution context changed. For the existing blocked Ohpen SELF_REVIEW workflow only, `--authorize-ohpen-self-review-recovery --recovery-authorization-reference <source>` records and consumes a single explicit authorization when the workflow ID, stage, blocker fingerprint, and recorded 14-attempt state all match. The authorization and the resulting resume attempt remain in workflow history; it cannot authorize another workflow or be reused. If a requirement or architecture decision changes, explicitly update that existing workflow:

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

`docs/ai-process.md` is controller-managed process evidence. When the increment artifact scope includes it, the orchestrator prevents Codex from editing it or returning findings against it and excludes that exact file from revision manifests. Before its first full refresh, the controller preserves any authored narrative in a workflow evidence artifact; it then generates the document from persisted attempts, self-reviews, validations, Claude rounds, finding decisions, and controller actions. Each self-review receives an evidence cutoff containing the workflow record counts and timestamp available when that invocation starts. The controller refreshes the evidence after self-review, validation, and review-cycle persistence. These evidence-only updates do not change the implementation revision or trigger another self-review; all other configured documentation paths remain in ordinary review scope. Correction-stage findings against this file are resolved by the controller refresh, not sent to Codex.

The only correction-stage deletion supported by this harness is the explicitly authorized Ohpen `src/.DS_Store` case. It requires the exact workflow and path, a Finder metadata signature, an untracked and unstaged single-link regular file, and its expected SHA-256. Safe Writer records the removal operation after the controller preserves the original bytes in workflow evidence. No generic deletion permission is added; tracked, staged, and other untracked paths remain protected.

Claude authorization-use IDs include the review, round, and invocation attempt. The controller updates only the unique reserved row for that invocation; a call that never receives an invocation claim releases its reservation. For legacy duplicate IDs, it preserves the original rows and appends source-hashed reconciliation events. A missing legacy cost is reconciled from persisted review-cycle evidence and counted once; an unverified reservation or cost still blocks further use.

Every Codex stage attempt records a trace with harness revision and source/runtime fingerprints, CLI/model/provider when available, prompt hash, requested instructions/skills and available source hashes, the read-evidence limitation, sandbox/MCP/plugin settings, outcome and sanitized diagnostics, plus changed paths and content hashes. The prompt and returned file contents are not persisted in this trace. A source fingerprint proves which content was available at trace time, not that Codex read it; without a Codex tool-event log, read evidence is recorded as unavailable. Exact tool inventory is runtime-managed and not exposed by the controller.

Failed validation/Codex calls remain in state. A failed Claude result is recovered on the next run through the adapter's persisted `recover_uncompleted_review_round` transition, within its attempt and aggregate-budget limits. A round with a persisted invocation claim but no result is not repeated because it cannot be proven that the call did not run. Repeated identical blockers do not trigger an infinite retry loop. Unsupported or incomplete approvals remain `BLOCKED` or `WAITING_FOR_HUMAN`, not `COMPLETED`.

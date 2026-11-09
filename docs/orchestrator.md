# Autonomous Orchestrator

`harness/orchestrator.py` is the explicit stage controller. It runs read-only preflight, records the original task and baseline in target-local state, asks Codex for a structured plan, then sends the complete proposal to Claude through the existing read-only, authorized, budgeted `claude_reviewer.py` adapter for a mandatory pre-implementation critique. Codex dispositions every finding, refines the plan for useful recommendations, and submits the refined plan for a second and final Claude review. The orchestrator presents the resulting proposal, Claude findings, Codex dispositions, assumptions, limitations, and open product questions through the existing pending-decision interaction. Product implementation does not start until the user explicitly approves `PLAN-APPROVAL`. A rejected plan blocks implementation. Claude failure or an invalid plan report is recorded as blocked and is never reported as an independent review.

After plan approval, bounded implementation, self-review, correction, configured validations, independent code review, and delivery continue through the existing workflow. Codex returns JSON; the controller validates it and applies source changes through `safe_writer.py`. Claude's plan and code reports, invocation evidence, and Codex finding dispositions are persisted in the target workflow directory. Plan review has a maximum of two rounds; any changes accepted after the second report are identified to the human as not independently re-reviewed.

This is the sole supported harness workflow execution engine. `harness/runner.py` is deprecated legacy code and is not an execution entrypoint.

The CLI does not commit files, push, merge, create a PR, deploy, or publish. `commit: true` is rejected because local commits require separate authorization. At initialization, the orchestrator records one workspace execution policy bound to the canonical target and workflow ID. It permits normal project edits, configured development commands, approved package-manager dependency installation, local Git inspection and exact-path staging, workflow evidence, bounded internal review/correction, and recovery without per-operation approval. External, paid, publication, infrastructure, destructive, and policy-required human-authority actions remain separate.

When a stage needs human input, the controller stores a categorized pending decision with its question, proposal, alternatives, reason, requesting stage, applicable policy/skill reference, authority, and append-only response history. It returns `WAITING_FOR_HUMAN`. The final implementation proposal is available from `--list-pending-decisions` and is included in the workflow result while `PLAN-APPROVAL` is open. Use `--respond-decision PLAN-APPROVAL --response-action approve --response "I approve this implementation plan."` to explicitly authorize implementation. Other responses never authorize product code; revise the requirement version and replan when changes are needed.

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

Supply the absolute Git root, its existing branch, one task prompt, and a JSON file describing validations and review settings. The workspace execution policy authorizes ordinary assignment-related changes throughout that repository; plan paths describe implementation and review scope, not a second write-approval list. `authorized_code_paths`, `authorized_artifact_paths`, and `code_file_extensions` remain accepted for compatibility but do not restrict workspace-policy writes. Optionally set `base_ref` (for example, `origin/main`) to pin a requested base reference; if omitted, the controller uses the branch's configured upstream, or `HEAD` when no upstream exists. At workflow start, the controller resolves that reference to an exact commit and keeps that `base_commit` unchanged. The initial checked-out `HEAD` remains separately stored as `baseline.commit`. The initial prompt is retained in `task.json` and a versioned `requirements-v1.json`; the current requirements and plan are stored under the workflow directory.

### Git baseline and worktree identity

The workflow state records `orchestration.git_tracking.base_ref_at_start`, its immutable `base_commit`, the assigned `workflow_branch`, and the canonical `target_worktree`. On resume, the controller verifies that it is still using that worktree and branch, and rejects a branch that Git reports as checked out in any other worktree. It does not create, switch, reset, clean up, or remove worktrees or branches. There is no detached-worktree creation lifecycle in this orchestrator; start it in the intended named-branch worktree. Supplying a commit ID as `base_ref` pins that exact commit without checking it out.

Git refs and remotes are shared between linked worktrees. The controller does not fetch, but observes the pinned base ref and configured upstream at initialization, resume, and final status reporting. A changed remote-tracking ref is appended to `orchestration.git_tracking.reference_movements` with its old and new commit, observation time, workflow ID, and observing operation. This event is separate from `baseline.commit` and the pinned `base_commit`. The controller never pulls, resets, rebases, merges, fast-forwards, or replaces either baseline automatically. A remote ref can move while the workflow is running; movement is recorded the next time the controller observes it.

The CLI result's `git_state` distinguishes the workflow baseline commit, pinned base commit/reference, current branch and `HEAD`, worktree cleanliness, observed upstream commit, ahead/behind counts, and whether the upstream moved since workflow start. A branch behind the currently observed upstream is reported as such; it is not treated as worktree corruption or as evidence that the workflow baseline or files changed. Worktree cleanliness reflects Git status at the target path, including target-local workflow artifacts.

### Workspace execution policy

The target-local `execution_policy` record is created once during initialization and reused through internal workflow stages and recovery. It authorizes normal source, test, documentation, configuration, metadata, generated-file, temporary-file, and workflow-evidence operations beneath the canonical target. `revision_paths` determine which content is reviewed; they are not write allowlists. Pre-existing user changes are fingerprinted and preserved. Assignment-relevant existing project files may be edited. Files created by the workflow may be renamed or deleted with operation evidence. Existing unrelated user files are not removed.

Planning may return a complete plan together with `NEEDS_HUMAN_INPUT`. Each increment states whether it depends on the open decision. The controller runs independent increments first, then waits before dependent increments and final review. A decision-only response waits immediately. Product proposals remain pending until the user responds.

The controller excludes Git metadata, nested repositories, sensitive credential/environment paths, paths outside the target, and controller-owned workflow records from Codex edits. Safe Writer keeps canonical-root, no-follow, hard-link, expected-content, and identity checks. Ordinary metadata such as `.DS_Store` is treated like any other user file: it is included in the baseline and preserved when pre-existing; no product-specific omission or migration exists.

Standalone `preflight.py` and Safe Writer still accept legacy path-scope configuration for compatibility. The orchestrator's workspace policy does not require exact code/artifact prefixes or extension lists.

The workspace policy changes these gates and restrictions:

| Area | Changed behavior |
|---|---|
| Routine workspace writes | Removed per-file code/artifact allowlists, extension lists, and per-missing-directory-component authorization. Canonical-root, no-follow, hard-link, expected-content, and sensitive-path checks remain. |
| Project commands | Removed per-command approval for configured builds, tests, linters, formatters, static analysis, and project dependency installation through approved package managers. Arbitrary downloads, credentials, custom registries, external services, and paid work remain separately restricted. |
| Local Git | Removed the separate `git add` approval for exact paths changed by the active increment. Commit, checkout, branch creation, reset, clean, merge, and remote operations remain restricted. |
| Internal flow | Removed approvals between normal stages and the bespoke one-use self-review recovery authorization. Changed-context and changed-validation recovery is recorded automatically. Independent increments can proceed while a product decision is open. |
| Data preservation | Existing assignment-relevant files may be edited. Only workflow-created files may be renamed or deleted; pre-existing dirty changes and unrelated files are preserved. |
| External and risk authority | Retained explicit authorization and budget for Claude/Aily transfer, paid operations, external services, installs outside the approved package workflow, infrastructure, networked containers, deployment/publication, commits, pushes, security-risk acceptance, criterion waivers, and required authenticated High-risk human review and final approval. |

The command filter is a guardrail and does not provide OS-level process confinement. It strips credential-like environment variables and rejects known external, shell, and privilege-escalation entrypoints; project code may still have side effects under the current user account.

The config shape is defined in [`orchestrator-config.schema.json`](../harness/schemas/orchestrator-config.schema.json). The CLI supplies the branch separately; the config must include a positive aggregate Claude budget and at least one validation. Review cannot be disabled, and commit/PR options are required to remain false.

Claude review runs automatically after Codex implementation, self-review, and successful configured validations when a currently valid `review.authorization` covers the canonical target repository and every exact file in the review packet. The authorization is a reusable configuration record; it is not requested again for each call. Without one, or when it is expired, revoked, over budget, or out of scope, the workflow enters `WAITING_FOR_HUMAN` before reserving a review round or contacting Aily. Supply the authorization in the workflow config and resume with the same workflow ID. Stored-plan fingerprints cover planning-relevant settings; the reviewer configuration and Claude budget are excluded, so authorization or review-scope changes do not invalidate an implementation plan. Older plan artifacts with a full-config fingerprint remain readable when the current configuration differs only by adding or changing the reviewer authorization. Changes to task or validation configuration still require the explicit requirement-update flow; workspace authority remains bound to the same target and workflow. The authorization records the provider/destination, canonical repository allowlist, relative file prefixes, read-only mode, maximum USD budget, required sensitive-data exclusions, validity interval, revocation state, authorizer, assurance level, and source reference. Declared identity remains declared; the harness does not authenticate the user. The review grant is separate from functional decisions and from High-risk human review or gate approval.

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

Commands are argument arrays, never shell strings. The controller allows ordinary project checks and selected package-manager operations, including project dependency installation. It allows local Git inspection and staging only explicit workflow-changed paths from the active increment. It rejects commit, push, merge, checkout, branch creation, reset, clean, remote administration, shells, direct network utilities, publication tools, explicit package URLs and custom registries, and global/system install options. `dotnet test` and `dotnet build` require `--no-restore`. The command filter and filtered environment are guardrails, not OS-level process confinement; project scripts can have side effects, so inspect their behavior and never expose credentials or contact unrelated services. Exact path prefixes and extension allowlists are not required in workspace mode.

Every Codex stage trace lists discovered skills, paths requested through stage context, successful skill/reference reads observed in the `codex exec --json` event stream, and requested skills whose reads remain unverifiable. The controller stores event IDs and hashes for the command/event output, not the returned skill text or raw event stream. A skill mentioned only in an agent response is not evidence that it was read. The recognizer is intentionally conservative: only completed successful direct read events with an attributable skill path count as verified; compound shell commands and ambiguous runtime events remain unverified.

Use the same target, task text, branch, workflow ID, and semantically identical workflow configuration to resume. The workflow state is `.engineering/workflows/<workflow-id>/workflow-state.json`. For ordinary resume, matching task and config fingerprints are required. A repeated blocker can be retried when its recorded execution context changes: the controller fingerprints the blocked increment revision, task/configuration and requirement version, orchestrator/runtime snapshot, and Codex runtime metadata. It records that fingerprint on the resume attempt and permits another retry only after a different fingerprint is observed; an unchanged repeated blocker stays blocked. Legacy resume records without context fingerprints can be retried once only when a subsequent recorded stage trace proves that the harness or execution context changed. No one-use recovery authorization is required. If a requirement or architecture decision changes, explicitly update that existing workflow:

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

The update appends `requirements-vN.json` and an invalidation event; it preserves older task snapshots, plans, decisions, scopes, validations, review cycles, and approval evidence. The event identifies the superseded plan, increments, decisions, scope, validations, review cycles, authorizations, assignments, and any prior completion record. Old authorizations are revoked. The current plan and review scope are cleared; the workspace execution policy remains bound to the same target. Configuration changes are accepted only with this explicit update operation; the supplied configuration becomes the new planning and validation input; the workspace execution policy remains the file-operation boundary. Replanning uses a new versioned plan artifact and version-prefixed increment IDs so old review cycles cannot be reused. It rechecks architecture (including DataView/non-DataView where applicable), capabilities, dependencies, acceptance criteria, scopes, and configured validations before implementation resumes. Do not update a workflow whose target HEAD or protected pre-existing changes changed; that remains a separate blocker.

Each increment implementation receives a controller-built context containing the current requirement version and hash, applicable non-invalidated decisions, architecture and documentation context, selected capabilities and source references, acceptance criteria, workspace execution policy and review paths, dependencies/blockers, and the configured validations. The orchestrator then runs a structured Codex self-review before any validation. Self-review can return scoped corrections, after which it repeats the review within the configured iteration bound; unresolved findings block validation. This is explicitly self-review, never an independent review or approval.

`docs/ai-process.md` is controller-managed process evidence. When the increment artifact scope includes it, the orchestrator prevents Codex from editing it or returning findings against it and excludes that exact file from revision manifests. Before its first full refresh, the controller preserves any authored narrative in a workflow evidence artifact; it then generates the document from persisted attempts, self-reviews, validations, Claude rounds, finding decisions, and controller actions. Each self-review receives an evidence cutoff containing the workflow record counts and timestamp available when that invocation starts. The controller refreshes the evidence after self-review, validation, and review-cycle persistence. These evidence-only updates do not change the implementation revision or trigger another self-review; all other configured documentation paths remain in ordinary review scope. Correction-stage findings against this file are resolved by the controller refresh, not sent to Codex.

Correction may rename or delete files created by the active workflow. The controller supplies the persisted created-path list to Safe Writer, which also verifies current content and file identity and records operation evidence. Pre-existing user files remain protected from removal unless the agreed assignment explicitly covers that removal; unrelated files cannot be cleaned up as generated output.

Claude authorization-use IDs include the review, round, and invocation attempt. The controller updates only the unique reserved row for that invocation; a call that never receives an invocation claim releases its reservation. For legacy duplicate IDs, it preserves the original rows and appends source-hashed reconciliation events. A missing legacy cost is reconciled from persisted review-cycle evidence and counted once; an unverified reservation or cost still blocks further use.

Every Codex stage attempt records a trace with harness revision and source/runtime fingerprints, CLI/model/provider when available, prompt hash, requested instructions/skills and available source hashes, the read-evidence limitation, sandbox/MCP/plugin settings, outcome and sanitized diagnostics, plus changed paths and content hashes. The prompt and returned file contents are not persisted in this trace. A source fingerprint proves which content was available at trace time, not that Codex read it; without a Codex tool-event log, read evidence is recorded as unavailable. Exact tool inventory is runtime-managed and not exposed by the controller.

Failed validation/Codex calls remain in state. A failed Claude result is recovered on the next run through the adapter's persisted `recover_uncompleted_review_round` transition, within its attempt and aggregate-budget limits. A round with a persisted invocation claim but no result is not repeated because it cannot be proven that the call did not run. Repeated identical blockers do not trigger an infinite retry loop. Unsupported or incomplete approvals remain `BLOCKED` or `WAITING_FOR_HUMAN`, not `COMPLETED`.

# Phase 3B.1 — ReviewCampaign contract and recovery rules

**Status:** Design proposal; approval is required before Phase 3B.2.  
**Repository:** `/Users/slcosentino/Documents/Preparacion/autonomous-engineering-harness-modular`  
**Branch / HEAD verified:** `architecture/modular-v2` / `1afcb9e61e0ae7ed63ee2d06a15e90e4d143eb48`  
**Scope:** Analysis and design only. No production code, tests, or persisted workflow data were changed.

## 1. Current behavior verified against code

### Campaign and input identity

- `ReviewCampaign.__init__` takes the live workflow state plus save, review, authorization-count, and trace callbacks. Its callbacks close over Orchestrator behavior; this is not an independent capability boundary (`harness/workflow/review.py::ReviewCampaign.__init__`, lines 589–628; `harness/orchestrator.py::Orchestrator._review_campaign`, lines 3860–3869).
- For a new campaign, `_prepare_review_campaign` creates a UUID, calculates parts, and stores campaign ID, source patch/exclusion hash, shared-context hash, and a part manifest. For resume it recomputes and compares those hashes and the manifest before allowing more campaign work (`review.py::ReviewCampaign._campaign_source_hash`, lines 855–873; `_prepare_review_campaign`, lines 894–1048).
- The shared context is assembled by `Orchestrator._code_review_shared_context`, using the assignment, approved proposal, implementation summary, repository and module context, guidance, skills, coverage, and verification material (`orchestrator.py`, lines 3700–3857). The normal provider context records proposal version, campaign/part IDs, prompt/header/section hashes, shared-context and part-patch hashes, report, cost, and operation ID (`orchestrator.py::_call_claude_impl`, lines 3542–3572 and 3601–3698).
- A saved part with status `reviewed` is skipped without revalidating the report. `report_saved` and `invalid_report` records are validated. If a campaign checkpoint is still pending, the normal fallback searches completed Claude contexts by campaign ID and part ID, report presence, and prompt hash not already present in the campaign call list, then validates the report structure (`review.py::ReviewCampaign._run_review_campaign`, lines 1121–1181).
- **Verified gap:** that normal fallback does not join the saved context's `operation_id` to exactly one Claude operation-history record. It does not reconstruct the current request and compare its prompt hash, compare proposal version, or confirm the operation's terminal status/result hash before copying the report. The campaign-level source/shared hashes and manifest are still checked earlier (`review.py`, lines 963–985 and 1145–1181).
- Local recovery is a different path. It is dry-run by default, requires a failed workflow at `code_review`, and refuses an active call or unknown spend (`orchestrator.py::recover_code_review` / `_recover_code_review`, lines 4623–4670). It reconstructs source segments, partition, shared and part prompts, headers, section hashes, prompt hashes, file coverage, reports, and known costs; it also compares persisted campaign call metadata with contexts (`review.py::recover_review_campaign`, lines 1434–1605). It does not currently join operation IDs to operation history. A missing campaign call checkpoint therefore makes call metadata inconsistent and is not silently repaired by this local path.

### Provider operation and persistence ordering

1. `_call_claude_impl` validates saved authorization, budget state, request sections, and payload, then creates a Claude operation with `input_sha256`, `campaign_id`, `part_id`, `proposal_version`, and `context_index`. It writes the operation ID into the Claude context, appends both records, and saves them with `active_call` (`orchestrator.py`, lines 3430–3454, 3542–3572, and 3601–3627).
2. It persists `launch_state="launching"` before calling the adapter (lines 3627–3637). In the real adapter, `claude_reviewer.review` starts `Popen`, calls `process_started(pid)`, and waits for output only after that callback returns (`harness/claude_reviewer.py::review`, lines 153–184). `_operation_process_started` saves PID and `launch_state="launched"` (lines 1000–1007).
3. The adapter returns report and cost. Orchestrator updates the context, budget accounting, and terminal operation status/result hash, then `_finish_operation` saves the whole state (lines 3637–3698 and 1017–1054).
4. Only after that return does `ReviewCampaign` validate the report, update the campaign call/report/coverage record, and save its checkpoint (`review.py`, lines 1235–1261). Atomic JSON replacement is implemented by `workflow/persistence.py::atomic_text` / `write_json`, lines 23–50. The provider action and later campaign save are separate durable boundaries.

If a PID callback cannot durably record the PID after process creation, the adapter attempts local process-group termination, but the provider may already have accepted work. That remains uncertain and must not become an automatic replay (`claude_reviewer.py`, lines 157–183; `Orchestrator._require_recovery`, lines 1351–1407). A missing PID is not proof that launch did not happen.

### Current recovery and test evidence

`Orchestrator._load` safely distinguishes `launch_state="not_started"` from a launch attempt. A started operation with `not_started` can be marked safe to retry; `launching`/`launched` without a durable terminal result becomes recovery-required (lines 1622–1696). Recovery records PID presence as unverified/not running/unknown, not as provider identity. Claude interruptions set `budget_unknown`; ordinary resume blocks. Explicit retry needs verified cost, confirmation that the provider is inactive, and cost within the saved call cap and remaining budget; resolution is recorded on the old operation and a later call receives a new operation ID (`Orchestrator._resolve_recovery`, lines 1470–1523; `_resume_locked`, lines 2794–2813).

Existing evidence includes:

- `test_large_review_uses_adaptive_parts_shared_context_and_integration` — partitioning, shared context, coverage, integration, and call accounting.
- `test_resume_reuses_successful_parts_and_retries_only_failed_part` — reuses a completed part after a known-cost failure; it does not cover an uncertain launched operation.
- `test_saved_part_report_is_recovered_without_repeating_paid_call` and `test_saved_integration_report_is_recovered_without_repeating_paid_call` — recovery after ReviewCampaign's exception handler saved `report_saved`, not a missing campaign checkpoint.
- `test_unknown_part_spend_blocks_all_later_campaign_calls` — a returned report with unknown cost blocks later campaign calls.
- `test_interrupted_claude_call_marks_cost_unknown_and_is_not_repeated` and `test_interrupted_claude_retry_requires_verified_cost_and_stays_within_budget` — planning-call recovery and explicit retry conditions.
- `test_local_recovery_dry_run_revalidates_reports_without_writing_or_calling_provider`, `test_local_recovery_apply_is_auditable_idempotent_and_preserves_call_costs`, and `test_local_recovery_rejects_changed_target_diff_without_mutation` — local recovery behavior, distinct from normal resume.
- Phase 3A added `test_three_part_uncertain_campaign_call_requires_explicit_retry` and `test_completed_review_report_recovers_when_campaign_checkpoint_is_missing`. Neither asserts that production currently joins a saved report to operation history or reconstructs its prompt; those checks are absent in the current normal fallback.

## 2. Proposed immutable capability input

The smallest useful boundary is a frozen value snapshot plus a single normalized prior outcome. It contains no Orchestrator, live state dictionary, provider client, persistence callback, authorization callback, or service container.

Conceptual contract (not implementation code):

```text
advance(snapshot, input, previous_result) -> transition
```

`snapshot` and `input` are immutable for one call. The capability returns a new value; it never mutates caller-owned data. The Coordinator supplies an existing campaign ID on resume and generates one for a new campaign, since UUID generation is nondeterministic and belongs with workflow identity/persistence.

| Input field | Why the capability needs it | Immutability and source | Owner |
|---|---|---|---|
| `snapshot_contract_version` | Selects the adapter/shape understood by the pure capability. The persisted campaign has no monotonic revision counter; do not add one merely for extraction. | Constant for this API contract; not persisted as a workflow field in 3B.2. | ReviewCampaign contract. |
| `campaign_snapshot` | Current statuses, part manifest, integration state, previous attempt summaries, reports, and coverage determine the next transition. | Deep, immutable copy of the existing `review_campaign` value; not the live state dictionary. | Coordinator reads and persists it; ReviewCampaign returns its evolved value. |
| `campaign_snapshot_sha256` | Lets Coordinator reject a stale transition result if the saved campaign changed while a result was being prepared. Useful as an ephemeral compare value under the existing lock; not a new state field. | Derived canonically for this transition. | Coordinator computes/checks it. |
| `campaign_id` and `purpose` | Stable identity for prompts, report association, and initial versus verification semantics. | Existing saved campaign values, or an ID allocated by Coordinator before initial planning. | Coordinator owns campaign identity; capability consumes it. |
| `source` | Exact ordered source segment text, path/ranges, segment hashes, included paths, exclusions, relevant context files/exclusions, operation diffs, and canonical source hash are needed to partition, prompt, validate coverage, and reject changed inputs. | Immutable DTO reconstructed from `ReviewDiff`; segment order is explicit and preserved. Hashes are recomputed/checked, not trusted merely because they came from state. | Review input/workspace builds it; Coordinator binds it to the current target; capability consumes it. |
| `shared_context` and `shared_context_sha256` | Exact prompt sections for assignment, proposal, implementation evidence, repository/guidance context, and coverage. Hash binds those values. | Immutable ordered `(section_name, text)` values; redacted and normalized before capability entry. | Coordinator/repository-context builder gathers it; capability renders request. |
| `proposal_version`, `proposal_sha256`, and proposal text | Version catches proposal revisions; content hash catches changed content without a version bump; text is required to rebuild the exact review request. | Derived from the approved proposal artifact/state for each transition. The exact request hash also binds proposal content. | Coordinator loads and hashes proposal; capability consumes the immutable value. |
| `payload_limit_characters` | Partitioning and request validity depend on the campaign's saved limit. | Use the saved campaign limit on resume after Coordinator validates current configuration is compatible; do not silently repartition. | Coordinator validates config; capability consumes saved value. |
| `report_contract_version` and supported header ID | Determines report parser and prompt contract. A report must not be silently reinterpreted under a different parser. | Existing campaign/context contract values mapped through an explicit compatibility table. | ReviewCampaign/report capability defines supported contracts; Coordinator selects/adapts legacy input. |
| `previous_result` (separate argument) | Supplies the outcome of the request just executed or recovered so the pure function can evolve the correct part. | Immutable normalized outcome created only after Coordinator joins and checks persisted operation/context records. | Coordinator validates identity/status/accounting and constructs it; capability validates report content and evolves campaign data. |

The campaign snapshot already contains call summaries (`prompt_sha256`, call state, cost, campaign call number), so a second independent `prior_calls` list is unnecessary. If older snapshot formats omit a necessary summary, the versioned adapter must identify that explicitly rather than filling it from ambient workflow state.

The `previous_result` value should expose only the relevant attempt: operation ID, Claude stage/type/status, launch state, campaign/part/purpose/proposal identity, operation input hash, result hash, report text if terminal, cost state (`known`, `unknown`, or `verified_after_recovery`), effective reconciled cost when present, and whether the report was recovered from durable context. It does not contain PID-based process policy: Coordinator must resolve process/recovery state before calling the capability. `ReviewCampaign` may check that result identity matches the requested part and expected hashes, but it cannot inspect operation history itself.

### Fields deliberately excluded

- Target path, Git calls, target fingerprints, provider process handles, and raw operation history: owned by workspace/Coordinator boundaries.
- Budget, authorization scope/limits, call caps, tracing objects, or generic callbacks: cross-stage policy belongs to Coordinator.
- Provider client/executable/environment: provider adapter owns transport.
- A persisted transition sequence number, event log, or idempotency key: no demonstrated need justifies a new state schema or general event mechanism. A transient snapshot digest plus the existing lock is sufficient for this extraction.

## 3. Proposed transition result

Return a frozen `CampaignTransition` containing:

| Result field | Contract |
|---|---|
| `campaign_snapshot` | New immutable campaign snapshot, or an identical value when no campaign outcome has yet been committed. Existing serialized field names/shapes remain unchanged in Phase 3B.2. |
| `decision` | Exactly one of `request_part(part_id)`, `request_integration`, `complete`, or `blocked(reason_code, findings)`. |
| `request` | Present only for a request decision. Contains the exact final prompt string, deterministic ordered sections/metadata used to render it, `prompt_sha256`, campaign/part/purpose/proposal identity, source/shared/segment hashes, payload limit, included/excluded paths, and report contract ID. |
| `validation` | Report parse/coverage findings for a supplied completed result, including validator version, valid flag, errors, and report hash. |
| `identity` | The request's deterministic identity tuple: campaign, purpose, part/integration ID, proposal version/content hash, source/shared/part-manifest/segment hashes, report contract, and prompt hash. |

`request` must not contain an operation ID, provider budget/cap, PID, retry authorization, or operation status. The Coordinator adds an operation ID and cap only after authorization, persists it before launch, and binds the terminal outcome afterward. Exact-prompt hashing occurs over the same redacted and formatted bytes passed to the adapter; the Coordinator rechecks the hash before launch.

Capability-level `blocked` reasons are data/contract decisions such as `input_mismatch`, `unsupported_report_contract`, `corrupt_snapshot`, `operation_outcome_unresolved`, or `invalid_report`. Authorization exhaustion and budget unknown are Coordinator outcomes, not campaign policy.

An invalid report is a terminal provider result, not a transport failure: preserve its attempt and report, mark the part invalid, and return a typed blocked result. The workflow stage can then fail and a later ordinary resume can request another attempt, matching current retry timing. A known-cost provider failure is likewise recorded by the Coordinator/capability and surfaced as a failed stage; the later resume may request a new operation. An uncertain result must not be turned into a pending request by the capability.

## 4. Report identity and reuse rules

### Required identity tuple

A prompt hash by itself is insufficient. Reuse requires a one-to-one chain from current request to saved context to one terminal operation and then to the report:

```text
current ReviewRequest
  -> exact prompt SHA-256
  -> exactly one Claude context (campaign/part/attempt)
  -> context.operation_id
  -> exactly one matching Claude operation-history record
  -> terminal result SHA-256
  -> structurally valid report for the current assigned segments
```

The Coordinator owns the context/operation join. It passes the validated result to ReviewCampaign; the capability must not receive or query a generic operation-history service.

| Identity evidence | Classification | Rule |
|---|---|---|
| Campaign ID | Required | Must equal the current campaign in snapshot, context, operation, and reconstructed request. |
| Part ID / `integration` ID | Required | Must identify the same current part and request kind at every join. |
| Operation ID and operation-history association | Required | Context must name an ID matching exactly one operation record. Record must be Claude, `code_review`, and match campaign/part/purpose/proposal version and context index when that index is present. Missing, duplicate, or mismatched joins block reuse. |
| Proposal version | Required | Saved context and operation metadata must equal the current approved proposal version. |
| Proposal content hash | Required | Recompute from current proposal text. Require equality through reconstructed prompt identity and the saved proposal section hash. A separate persisted `proposal_sha256` key is not required if the prompt and section hashes are reconstructed exactly; adding that key would be a persisted-schema decision. |
| Prompt hash / operation input hash | Required | Rebuild the exact safe prompt from current inputs. Require `current_prompt_sha256 == context.prompt_sha256 == operation.input_sha256`. A stored prompt hash alone does not prove which current prompt was reviewed. |
| Source patch/exclusion hash | Required | Current canonical source hash must match campaign and request metadata. |
| Shared-context hash | Required | Current exact shared context must match campaign, context, and request metadata. |
| Part manifest and segment hash | Required | Reconstructed ordered part manifest, assigned ranges, patch text hashes, and part-specific patch hash must match. Any repartition invalidates reuse. |
| Report contract/header/validator version | Required | Must be a supported exact contract. For a legacy contract, use an explicit versioned adapter and its parser; otherwise block reuse rather than silently upgrading validation. |
| Terminal operation/context status | Required | Context must be completed with report; operation must be `completed` with `launch_state="completed"`. `started`, `failed`, `unknown`, or `interrupted` is not a reusable completed result. |
| Result/report hash | Required | Context report bytes must hash to operation `result_sha256`; campaign report, if present, must match that context. Then validate the report against the exact part and coverage. |
| Cost and usage | Required for clearing/continuing paid budget; conditional for report identity | Known context and operation costs must agree. Unknown cost remains unknown until explicit reconciliation; it cannot count as zero or permit another paid request. After reconciliation, use the Coordinator's verified effective cost without rewriting the original operation's null cost. Reuse itself must not increment call count/spend a second time. Provider token usage is not currently captured and is not an identity field. |
| Campaign call number / retry count | Required only to disambiguate attempt history when the same prompt was submitted more than once; otherwise diagnostic | It must map one-to-one when present. It is not a substitute for operation ID. |
| Context index | Required only when present in the saved operation; otherwise diagnostic for legacy compatibility | If present, it must point to the matching saved context. It cannot replace the operation ID join. |
| PID / process identity | Diagnostic for a terminal completed report; required only for process-recovery decision | A PID does not prove which remote work completed. A missing PID never proves no launch. |
| Duration / runtime usage | Diagnostic/accounting | Must not affect content identity. |

### Missing campaign checkpoint

The Coordinator may reconstruct a missing part checkpoint without provider execution only when all of these conditions hold:

1. The saved campaign input still matches: campaign/purpose, source/exclusion hash, shared-context hash, ordered manifest/segments, proposal version/content, payload limit, and report contract.
2. Exactly one completed Claude context exists for the missing attempt, with a non-empty report and complete prompt manifest.
3. The context's operation ID joins to exactly one terminal completed Claude operation with matching stage, campaign, part, proposal version, input hash, and result hash.
4. The reconstructed current safe prompt hash equals both the saved context prompt hash and operation input hash. Section/header/part patch hashes also match.
5. The report passes the current supported report contract for that exact part, including every assigned file/segment.
6. Cost is known and consistent, or the user's explicit recovery has reconciled the saved unknown cost. Unknown spend continues to block all additional paid work.
7. The campaign has no conflicting checkpoint, duplicate result, unmatched operation/context, or contradictory call metadata.

On success, the capability returns an evolved snapshot containing the report, attempt summary, validation, and coverage. The Coordinator saves that snapshot once under the existing lock. The operation/context history is retained and not duplicated. Integration recovery uses the same chain, with a reconstructed integration prompt bound to all part reports and coverage.

If any condition fails, do not guess, overwrite, or silently repair. Normal resume remains blocked. Local recovery dry-run can report the mismatch without mutation; local apply is permitted only after all the same checks pass and its existing preconditions (failed `code_review`, no active call, no unknown spend) hold. Where required historical identity is absent, the report may be inspected diagnostically but is not eligible for automatic reuse.

**Compatibility consequence:** current normal fallback is looser, while current local recovery checks prompts more thoroughly but lacks the operation-history join and rejects inconsistent call metadata. Making the gate strict can prevent automatic reuse of old/legacy reports that lack an operation ID, result hash, or supported contract reconstruction. Preserve read access to old state, but fail closed for reuse when required identity cannot be established; do not add an implicit schema migration in 3B.2.

## 5. Recovery decision table

“Normal resume” below means no explicit recovery action. An explicit retry is a deliberate new provider invocation and may repeat work; none of these rules promise exactly-once execution.

| Persisted operation/result state | Current interpretation | Normal resume | Explicit recovery | Provider call permitted? |
|---|---|---|---|---|
| No operation was created, or request validation failed before operation creation | No provider launch evidence exists. | Rebuild request and proceed under ordinary authorization. | Not required. | Yes, after normal Coordinator checks. |
| Operation is terminal failed with `launch_state="not_started"` | Positive evidence that launch did not occur. `_load` may convert a crash-left started/not-started operation to failed and clear it. | Retry saved stage safely with a fresh operation ID; retain old record. | Not required. | Yes, after normal authorization/budget checks. |
| Operation is `launching`, no PID persisted | Launch may have happened before the PID callback/save. Missing PID is not proof of no launch. | Block; set/retain recovery-required; no automatic replay. | Require verified final cost and user confirmation that provider is inactive; then a deliberate retry may create a new operation. | Only after explicit resolution. |
| Operation is `launched` with a PID | A local process was observed; PID presence is unverified and does not prove remote completion. | Block until terminal result or recovery resolution. | Same reconciliation and inactivity requirements. Do not infer provider inactivity solely from PID disappearance. | Only after explicit resolution if result remains unavailable. |
| Operation is `completed`, durable report and terminal hashes match current identity | A durable reusable provider result exists. | Reconstruct the campaign checkpoint and continue with the next action, without provider call. | Not needed. | No call for this result; future requested calls still need Coordinator authorization. |
| Operation is `completed`, report durable, but cost is null / `budget_unknown` | Content result may be durable but workflow spend is unresolved. | Current behavior blocks review resume while spend is unknown. Preserve that gate. | Reconcile cost and confirm provider inactive. If identity checks pass, reuse the report and do not invoke provider again; if no valid durable report exists, retry is a separate deliberate call. | No paid call until cost resolution; then only if transition requests a new call. |
| Operation status `unknown` or `interrupted`, with no durable terminal result | Provider outcome/cost is uncertain, whether PID exists or not. | Block, preserve operation history, no replay. | Cost and inactivity confirmation first; retry only as a new operation. | Only after explicit resolution. |
| Operation says completed but context/report/hash/join is missing or contradictory | Persisted state is inconsistent; a status bit alone is not a result. | Fail closed, no provider replay or silent repair. | Local dry-run may report evidence; apply only if identity is fully reconstructed and no state conflicts remain. Otherwise require a newly authorized review. | No call during diagnostic recovery. |
| Terminal failure with reliable known cost and no uncertainty | The invocation ended; its report may be absent/invalid. | Resume the failed stage according to existing policy; a new request may be made. | Not needed unless another recovery condition exists. | Yes, normal budget/call-limit gate applies. |

Local `recover_code_review` remains a separate provider-free inspection/apply operation. It must not resolve unknown spend, authorize a retry, or complete a campaign that still lacks integration review. The target fingerprint gate (`Orchestrator._require_write_operation_target`, lines 1141–1167) checks attribution to the completed write operation. Campaign/source/prompt hashes separately check that the prior report covers today's review input. They protect different invariants and should both remain.

## 6. Corrupted or inconsistent state policy

| Persisted inconsistency | Normal resume recommendation | Local recovery recommendation |
|---|---|---|
| `reviewed` status with missing report | Treat as corrupt; do not skip or mark complete. If one fully verified terminal context can reconstruct it, use the missing-checkpoint rule; otherwise block without mutation. | Dry-run reports the missing campaign report. Apply only if the full identity chain and report validate; otherwise no write. |
| `reviewed` status with invalid report or wrong file coverage | Revalidate before trust; block. Do not change it to reviewed or silently replace it. | Dry-run surfaces validator errors; apply rejected. A newly authorized provider review is a separate decision. |
| Inconsistent/missing/duplicate operation ID association | Block; do not treat prompt hash as substitute. | Surface mismatch; no apply unless a unique valid join can be proven from existing records without guessing. |
| Mismatched source/exclusion/shared hash | Block campaign reuse before provider or checkpoint changes. Do not create a second campaign inside the same saved authorization. | Dry-run reports source/context mismatch; no apply. A new authorized review is required for changed inputs. |
| Mismatched proposal version/content | Block reuse; exact current request is different even when target Git fingerprint is unchanged. | Dry-run reports identity mismatch; no apply. |
| Mismatched part segmentation/segment hash | Block affected campaign results; do not map an old report onto new segments. | No apply. Require a campaign authorized for the new partition. |
| Unsupported report contract | Block reuse; never silently run a newer parser against an unrecognized historical prompt contract. | Read-only display is allowed; apply only through an explicitly supported legacy adapter. |
| Conflicting saved result and campaign checkpoint | Preserve bytes/history and fail closed. No overwrite of the report/call that disagrees. | Dry-run identifies both values; apply rejected until manually reconciled or a new authorized campaign is created. |

The normal path may perform only the narrowly defined missing-checkpoint reconstruction after all checks pass. It must not become a general repair path for corrupted state. Local recovery stays explicit and auditable, with dry-run as its default.

## 7. Persistence boundary and ordering

### ReviewCampaign owns

- Pure adaptive partition and manifest calculation from explicit ordered source segments.
- Deterministic prompt/request rendering and request identity hashes.
- Report parsing and file/segment coverage validation.
- Choosing the next part/integration/complete decision from a snapshot and validated outcome.
- Returning a new campaign snapshot and validation findings; no save and no mutation of authoritative workflow state.

### Coordinator retains

- Workflow and target locks; authorization, call limits, budget and spend accounting.
- Operation IDs/history and Claude context creation/association.
- Persisting `started/not_started`, `launching`, PID/`launched`, terminal result/cost, and recovery state.
- Provider adapter invocation and process identity callback.
- Durable workflow saves, recovery resolution, retry authorization, and workflow-stage transitions.

### Required ordering

1. Coordinator asks `advance` for a request and validates authorization, budget, payload limits, and stage policy. If blocked, it persists the failure/blocked status using current Coordinator rules; no operation is created.
2. Coordinator creates a fresh operation ID and context, associates exact prompt/input hash, campaign/part/proposal metadata, call cap, and context index; it saves these before launch.
3. Coordinator saves `launching` before calling the provider adapter.
4. Real adapter starts the process. Immediately after `Popen`, it invokes the PID callback. Coordinator associates PID to operation ID, saves `launched`, and only then allows output collection. If the callback/save fails after process creation, adapter termination is best effort and outcome remains uncertain; no automatic replay.
5. On reliable terminal result, Coordinator saves context report/cost, operation status/result hash, and usage/accounting together through the existing atomic state save. If result is terminal but cost unknown, keep `budget_unknown` set.
6. Coordinator constructs a validated `previous_result` and calls `advance` again. ReviewCampaign returns the new campaign snapshot. Coordinator saves it separately under the same lock.
7. Only after campaign completion is durable does the current code-review stage store its top-level report and advance to the next workflow gate.

The unavoidable crash window between step 4 and a durable PID, and between step 5 and step 6, is handled differently: the former is uncertain and blocks; the latter can be repaired without a provider call only by the strict missing-checkpoint identity rule. Atomic file replacement does not make either provider execution and campaign snapshot one transaction.

## 8. Transition protocol

The small typed protocol is preferable to an event bus or generic workflow framework:

```text
transition = advance(campaign_snapshot, immutable_input, previous_result=None)
```

| Situation | `previous_result` | Capability response | Coordinator action |
|---|---|---|---|
| A part is ready; nothing for it has been executed | `None` | Same campaign snapshot plus `request_part` and exact `ReviewRequest`. No attempt is appended yet. | Authorize, persist operation/context, invoke adapter. |
| All parts are complete and integration is due | `None` | Same snapshot plus `request_integration`. | Authorize and execute like a part request. |
| Provider returned a valid report, or a verified durable report was recovered | Typed completed outcome with `recovered` flag | New snapshot records attempt/report/validation/coverage; choose next part, integration, or `complete`. | Save returned snapshot; no second provider call for recovered result. |
| Provider returned a report that fails the supported report contract | Typed completed outcome with report | New snapshot preserves report/attempt, marks invalid, and returns `blocked(invalid_report, findings)`. | Save checkpoint and fail the stage; a later resume may make a new paid request under normal limits. |
| Provider failed terminally with known cost | Typed `failed_known` outcome | New snapshot records the failed attempt and returns a typed blocked/deferred-retry result. | Save, surface stage failure; ordinary resume can request a new operation, subject to budget. |
| Operation is positively known not launched | Typed `not_started` outcome | Preserve attempt summary; return a new request decision when stage resumes. | New operation ID is used; old operation remains in history. |
| Operation may have launched but lacks reliable terminal result | **Do not pass to normal `advance` as a reusable result.** | Defensive direct-call behavior is `blocked(operation_outcome_unresolved)` with unchanged snapshot. | Coordinator enters recovery-required. Default resume makes no provider call. Explicit reconciliation may permit a new operation. |
| Correlated result was found in saved context and operation history | Same terminal completed outcome, marked `recovered=true` | Same transition as successful provider result after identity validation. | Save repaired snapshot; do not invoke provider. |
| All required reviews/checkpoints are complete | `None` or last completed result | `complete`, consolidated report, full coverage validation. | Save campaign result, then continue workflow stage. |

`previous_result` carries an outcome; it is not an instruction to retry. A `request_*` decision is a not-yet-executed proposal. The Coordinator decides whether it is authorized and persists the new operation before any launch. An explicit retry is represented by a new Coordinator operation after recovery resolution, not a special side effect inside ReviewCampaign.

## 9. Test matrix for Phase 3B.2 and 3B.3

Pure tests belong with the capability extraction in 3B.2. They use immutable fixtures and never create an Orchestrator, touch disk, or call a provider. Coordinator/integration tests belong in 3B.3 and use temporary state plus controlled adapters. No test in either phase should call real Aily/Claude/Codex.

| Phase / test | Initial state | Action | Expected result | Provider call permitted? | Durable save expected? |
|---|---|---|---|---|---|
| 3B.2 pure — deterministic request | Frozen campaign, ordered source, shared context, proposal, and saved limit | Call `advance` twice with equal values | Byte-identical request payload/hash/metadata and same decision; input/snapshot unchanged | No (pure test) | No |
| 3B.2 pure — next-part selection | Part 1 reviewed, Part 2 pending, Part 3 pending | `advance(snapshot, input, None)` | Request only Part 2; reviewed Part 1 is not reissued | No (pure test) | No |
| 3B.2 pure — integration/completion | All parts reviewed with full coverage; integration pending, then integration reviewed | Advance each state | First integration request, then `complete`; no missing file/segment | No (pure test) | No |
| 3B.2 pure — source/shared mismatch | Snapshot hashes differ from immutable source/shared input | Advance | Typed `blocked(input_mismatch)`, snapshot unchanged, no request | No | No |
| 3B.2 pure — proposal/segment/contract mismatch | Same campaign ID but changed proposal hash/version, assigned range/hash, or report contract | Advance with prior result | Typed blocked outcome; old report is not attached to new identity | No | No |
| 3B.2 pure — report validation and coverage | Completed result for exact part with valid then invalid/missing-file reports | Advance with each result | Valid report evolves to reviewed; invalid report is preserved and blocked with findings; all input values unchanged | No (adapter is not in capability) | No |
| 3B.2 pure — provider outcomes | Outcomes: not-started, failed-known, completed, recovered completed, uncertain | Advance | Correct immutable snapshot/decision per protocol; uncertain never returns an automatic request | No | No |
| 3B.2 pure — reviewed record tampering | `reviewed` record missing/invalid report or contradictory call summary | Advance | Typed corrupt-snapshot block, no skip/complete | No | No |
| 3B.3 Coordinator — missing checkpoint, valid join | Part pending; completed context/report and exactly one matching completed operation; exact prompt/hash/cost/report validate | Resume | Reconstruct only missing campaign checkpoint; preserve histories and advance stage | No for recovered part | Yes, campaign checkpoint and later stage state |
| 3B.3 Coordinator — operation association mismatch | Same as above, but operation ID missing/duplicate/wrong stage/campaign/part/input/result hash | Resume or local dry-run/apply | Fail closed; no mutation of campaign report/calls; actionable mismatch | No | No campaign save; failure status save may follow existing Coordinator failure policy |
| 3B.3 Coordinator — proposal-version mismatch | Durable report exists but context/operation proposal version differs from current version | Resume | Block before reusing report | No | No campaign save |
| 3B.3 Coordinator — prompt/segment identity mismatch | Change prompt section, shared context, payload contract, or segment text/range while retaining old report | Resume | Reconstructed hash/manifest mismatch; no report reuse | No | No campaign save |
| 3B.3 Coordinator — source/shared direct mismatch | Keep target fingerprint valid but alter saved/reconstructed campaign source or shared context | Resume | Campaign hash guard blocks before request; no silent new campaign | No | No campaign save |
| 3B.3 Coordinator — checkpoint already complete, stage save missing | Campaign and all part/integration checkpoints are complete; workflow still at `code_review` | Resume | Return stored report and advance stage without provider; no duplicate accounting | No | Yes, stage/report state only |
| 3B.3 Coordinator — unknown/uncertain operation | Persist `launching` or `launched`, no reliable terminal result; test both PID present and absent | Default resume, then explicit recovery | Default stays blocked. Missing PID does not change it. After verified cost and inactive confirmation, a deliberate new operation may be allowed; history remains. | Only after explicit resolution | Recovery resolution and new operation saves; never a silent replay |
| 3B.3 Coordinator — PID ordering | Controlled fake `Popen`; persist-save observer around launch/PID/communicate | Assert event order and inject failure between launch and PID durability | Operation/context saved, `launching` saved, PID saved before communicate; failed PID save remains uncertain and is not retried automatically | Fake subprocess only | Yes at each durable boundary; no external provider |
| 3B.3 Coordinator — known-cost terminal failure | Saved operation has terminal failed result and known cost | Resume saved stage | One new authorized operation ID; old history preserved; budget counted once per call | Yes, fake adapter under normal authorization | Yes, new operation/result/checkpoint saves |

The current `test_operation_and_pid_are_persisted_before_codex_result_finalization` covers Codex, not the Claude adapter. The proposed Claude PID-order test is justified because its callback occurs after `Popen` but before output collection, a distinct uncertainty boundary.

## 10. Compatibility risks and migration strategy

1. **Legacy operation association:** strict reuse may reject old contexts without an operation ID, a unique operation-history record, a result hash, or a supported header/validator version. Keep those states readable, but do not auto-reuse a report whose identity cannot be established. A new authorized review is safer than silently guessing.
2. **Existing reviewed records:** current ordinary resume trusts `status="reviewed"`. Revalidating every report and its operation chain is stricter and may expose persisted corruption previously skipped. Fail closed and preserve the data; don't overwrite it as part of extraction.
3. **Local recovery compatibility:** local recovery currently validates the prompt more deeply than normal fallback but does not validate the operation-history join, and requires campaign call lists to match contexts. Unify the identity predicate without turning local recovery into unknown-spend reconciliation or a provider retry tool.
4. **State schema:** recommended 3B.2 keeps the existing campaign/operation/context JSON shapes. Proposal content hash is computed from existing proposal text; exact prompt and section hashes bind the historical request. Do not add a campaign revision field, `operation_id` to every campaign call, or proposal hash field unless implementation shows existing context cannot support a one-to-one join. If a field proves necessary, stop and request a separate state-schema decision before writing it.
5. **Retry and cost:** a reconciled report with durable result can be reused without re-invoking the provider. If no trustworthy report exists, explicit retry is a new operation and may duplicate paid work. Do not convert provider retries to automatic retries or claim exactly-once.
6. **Input gates:** retain the target fingerprint check for attribution and the campaign/source/prompt hashes for review identity. They overlap in observed file changes but protect different evidence and should not be collapsed.
7. **Test placement:** pure deterministic tests are cheap and belong with 3B.2. Operation joins, PID timing, atomic saves, and interrupted stage advancement need Coordinator integration tests in 3B.3. No generic event bus, database, broker, migration framework, or distributed service is justified by the current single-process workflow.

## 11. Decisions requiring approval before Phase 3B.2

The following are recommendations, not approved behavior changes:

1. **Strict saved-report identity:** require the complete chain in Section 4, including one-to-one operation-history association, reconstructed exact prompt hash, proposal version/content, terminal result hash, and report-contract match. On any missing link, fail closed.
2. **Unknown cost with durable result:** keep current default-resume block. After explicit cost reconciliation/inactive confirmation, reuse a fully validated durable result without making another provider call; only make a new call if no valid result is recoverable.
3. **Corrupted reviewed record:** revalidate before trusting `reviewed`; do not silently repair invalid/missing/inconsistent records. Permit only the narrowly proven missing-checkpoint reconstruction. Local recovery remains explicit and provider-free.
4. **Legacy records:** retain read compatibility but deny automatic reuse if required identity is missing or the prompt/report contract is unsupported. Confirm whether this limitation is acceptable for old workflow states.
5. **Persisted schema:** keep schema unchanged in 3B.2. Use existing context/operation metadata plus derived proposal/prompt hashes; pause for separate approval if a persisted field becomes necessary.
6. **Phase split:** 3B.2 extracts immutable inputs, pure transitions, and pure tests only. 3B.3 wires the Coordinator, adds strict operation joins/recovery and PID ordering tests, and preserves all established authorization and save ordering.

Phase 3B.2 should not begin until these choices are approved. This ADR makes no runtime change and does not itself approve the proposed semantics.

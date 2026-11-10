# Code review pipeline: payload, validation, and recovery

This document describes the implementation in `harness/orchestrator.py` and
`harness/claude_reviewer.py`.

## What the reviewer receives

`run_pending()` enters `code_review` after Codex implementation. `_review_diff()`
collects changed paths relative to the saved baseline commit. Tracked files are
sent as their complete Git diff; new files are represented by a generated full
file patch. Paths are sorted before patch construction. The patch is redacted
before it is placed in the prompt.

The following changes are excluded and their paths/reasons are recorded:

- Files that existed as target changes before the workflow started.
- Workflow state under `.engineering/workflows/` and operating-system metadata
  such as `.DS_Store`.
- Sensitive paths, Git metadata, symlinks, non-regular files, binary data, and
  unsupported text encodings.

The shared context is built by `_code_review_shared_context()` in this order:

1. **Assignment** — the complete saved assignment (`state["task"]`).
2. **Approved proposal** — the complete approved proposal, not a summary.
3. **Implementation summary** — Codex's saved implementation summary.
4. **Repository context** — top-level repository inventory, changed text paths,
   and applicable `AGENTS.md` instructions.
5. **Relevant skills** — the applicable skill content collected for the target.
6. **Repository/module map and dependencies** — changed-file directory counts,
   contract/interface filename candidates, and cautions against inferring
   architecture from folder names.
7. **Independent reviewer role** — `CLAUDE_CODE_REVIEW_GUIDANCE`, which asks
   Claude to check requirements, behavior, design, reliability, security, and
   tests independently of the approved proposal.
8. **Engineering Judgment & Proportional Design** — the harness's shared
   proportionality principles.

The original assignment is present in full as section 1. The full approved
proposal is present as section 2. The prior Codex implementation summary is
present as section 3, including whatever commands and results Codex reported.
The raw Codex tool transcript/stdout and earlier Claude planning reports are
not included. Changed tests and process-evidence files are included in the
patch when they are reviewable changes, so Claude can inspect their actual
contents; there is no separate harness-injected test transcript or test-result
artifact.

The part-specific sections then follow:

1. **Review metadata** — campaign ID, part ID, proposal version, and the
   statement that all assigned patch lines are included.
2. The eight shared sections above.
3. **Review file coverage** — every assigned path, complete patch or exact line
   segment, and its SHA-256; also the count of excluded paths.
4. **Complete patch** — each assigned path and its complete patch segment.

`_format_context_prompt()` places the review header first, then each section as
`## <section name>` in insertion order. For a multi-part campaign, each changed
patch line belongs to one part; oversized files are split only at conservative
patch boundaries. No source lines are truncated. If mandatory context cannot
fit, the workflow stops before a paid call. The default limit is 100,000
characters (`HARNESS_CLAUDE_MAX_PAYLOAD_CHARS` can configure it). The saved
workflow context manifest records prompt length, UTF-8 byte length, section
hashes, file coverage, and the complete prompt SHA-256.

For one part, that part report is the campaign report and no integration review
is required. Multiple parts require a final integration review that receives
the shared context, coverage, and all individual part reports; it checks
cross-component behavior and preserves references to the original findings.

## Provider boundary

The report header and sections are passed as the `-p` prompt to Aily's Claude
CLI. There is no separate reviewer system-prompt argument in this code path.
`claude_reviewer.review()` invokes:

```text
aily claude -p <complete prompt> --output-format json --no-session-persistence
  --permission-mode plan --permission-prompts none --restricted
  --tools Read,Glob,Grep --strict-mcp-config --disable-slash-commands
  --max-budget-usd <remaining workflow budget>
```

The process runs with an empty temporary working directory, not the target
repository. Its declared tools are read-only; the target implementation is
provided in the redacted patch. Git-related environment variables are removed.
The Aily JSON envelope supplies the report text and cost. The full report, cost,
prompt hash, call number, and context manifest are saved in workflow state.

## Report contract and root cause

Current reports contain exactly one decision and part ID, followed by the
`Files reviewed`, `Findings`, `Cross-component concerns`, and `Overall
assessment` sections. Each finding is an individually headed block with one
non-empty `Severity`, `Category`, `File/location`, `Description`, `Impact and
reasoning`, and `Recommended correction` field. Empty findings must be stated
explicitly. The parser accepts harmless Markdown around labels and headings,
checks assigned-file coverage, and rejects missing, duplicate, unsupported, or
contradictory content with field-specific errors.

An earlier parser version accepted only a plain `Part ID:` line and rejected a
harmless Markdown heading prefix. The current parser accepts both forms while
also validating report sections, assigned-file coverage, finding fields,
severity values, and decision consistency. Regression tests cover these
formatting variations and invalid or incomplete reports.

The local recovery command revalidates saved reports against their saved
campaign parts and prompt manifests. It does not contact Aily or Claude.

## Recovery and feedback

Use the `recover-review` subcommand through the normal orchestrator entrypoint.
It is dry-run by default. It reconstructs the current target diff, verifies the
campaign/source/partition hashes, rebuilds each saved prompt, checks saved
section and call metadata, and validates every report. `--apply` writes an
auditable, idempotent recovery record to that workflow's state only when the
saved inputs and reports validate. It preserves source reports, timestamps,
prompt hashes, call records, and costs. It marks the campaign complete only
when every part covers its full patch and any required integration review is
already complete; it never changes the workflow stage or executes the next
stage. Applying recovery changes only the target workflow's `state.json`; use
the explicit `--apply` flag only after confirming the target and workflow ID.

The delivery and CLI summaries show decision, findings grouped as blocking,
non-blocking, and suggestions, each finding's location/reason/recommendation,
cross-component concerns, assessment, and the original report text. This keeps
positive observations and verification limits available without rewriting the
reviewer's claims.

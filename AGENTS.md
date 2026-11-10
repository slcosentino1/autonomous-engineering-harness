# Autonomous Engineering Harness

This repository contains a small workflow tool. Product code and project-specific artifacts belong in the explicitly supplied target repository.

## Use

- Use `python3 harness/orchestrator.py` as the only workflow entry point.
- Require an absolute Git repository root. Read its instructions before implementation.
- `start` creates a Markdown proposal and runs up to two Claude plan reviews through Aily. It stops for explicit human approval.
- `approve` is the only command that starts implementation. Codex works in the target workspace and runs relevant checks; Claude then reviews the change. If Claude reports findings, Codex evaluates them, corrects valid issues, and reruns affected checks.
- `revise` applies explicit proposal feedback and uses a remaining plan-review iteration when one is available. `reject` stops without implementation.
- `resume` retries only the saved failed stage. It does not restart completed stages or approve a proposal.
- `status` reports saved workflow state. A completed run writes `delivery.md` beside the proposal.
- `start` records one target-bound workflow authorization, including ordinary workspace operations and the Claude/Aily data scope and budget. Stages and resumes reuse it; they must not ask again or expand it.

## Boundaries

- Keep ordinary target work inside the explicit target. Preserve unrelated existing files and changes.
- Do not transmit credentials, environment files, or unrelated pre-existing changes to Claude. Claude receives a redacted patch and proposal in a restricted temporary review context.
- Claude/Aily calls use a workflow budget of USD 2.00 by default; `--budget-usd` overrides it. Each call is capped to the remaining amount. If a call has no recorded cost, stop further Claude calls.
- Do not commit, push, merge, deploy, publish, or change shared infrastructure without explicit authorization. Respect operating-system and organization permissions.
- Do not run a workflow against an Ohpen repository unless the user explicitly asks for it.

## Development

- Keep Python standard-library-only unless the workflow itself requires a dependency.
- Add focused tests for workflow behavior and remove tests for deleted mechanisms.
- Run `python3 -m unittest discover -s tests` once after the work is complete, plus `python3 -m compileall -q harness tests` and `git diff --check`.
- Do not commit or push unless explicitly requested.

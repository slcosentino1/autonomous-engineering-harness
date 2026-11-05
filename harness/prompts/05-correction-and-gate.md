# Correction Loop and Verification Gate Prompt

Use the independent review findings and current target state. Do not change requirements or confirmed decisions while fixing defects.

- Reproduce or otherwise verify each claimed defect where safe.
- Correct confirmed, in-scope defects with focused changes; identify proposed scope changes separately.
- Rerun the validations affected by each correction and record actual commands and outcomes.
- Update finding dispositions and requirement traceability with evidence.
- Check that every required criterion is PASS with evidence or explicitly unresolved; no silent waivers.
- Evaluate blocking findings, missing evidence, open decisions, and approval boundaries.

Return one gate outcome: `APPROVE`, `REJECT_REQUEST_CORRECTION`, `REQUEST_EVIDENCE`, or `ESCALATE`, with concise evidence and rationale. A policy statement is not proof of technical enforcement. The implementation author cannot be the only approver.

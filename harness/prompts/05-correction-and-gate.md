# Correction Loop and Verification Gate Prompt

Use the independent review findings and current target state. Do not change requirements or confirmed decisions while fixing defects.

- Reproduce or otherwise verify each claimed defect where safe.
- Correct confirmed, in-scope defects with focused changes; identify proposed scope changes separately.
- Rerun the validations affected by each correction and record actual commands and outcomes.
- Update finding dispositions and requirement-to-decision/implementation/documentation/test/evidence traceability.
- Recompute the immutable `content:<sha256>` identity over the original baseline commit and explicit `revision_paths`; the original baseline stays fixed. Any identity change makes earlier evidence and review reports ineligible for the new gate. Rerun checks selected by impact analysis, create current evidence for every required criterion, and obtain every required review on the final identity. Never relabel stale evidence or substitute a later commit SHA for the reviewed identity.
- Apply blocking rules: open Critical/High findings always block; Medium requires correction or documented human risk acceptance; Low/Informational deferrals need owner, rationale, and review date.
- Check that every required criterion is PASS with evidence or explicitly unresolved; no silent waivers.
- Evaluate blocking findings, missing evidence, open decisions, and approval boundaries.

Return one gate outcome: `APPROVE`, `REJECT_REQUEST_CORRECTION`, `REQUEST_EVIDENCE`, or `ESCALATE`, with immutable content revision and manifest reference, risk band, reviewer assignments/reports/completion attestations/limitations, identity assurance and authentication evidence, gate authority, evidence references, and rationale. Verify any optional commit against the approved content manifest. Apply risk-specific authority from policy; the implementation author cannot issue or override approval. A policy statement is not proof of technical enforcement.

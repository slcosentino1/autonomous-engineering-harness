"""Commitment ledger parsing, evidence validation, and delivery disposition rules."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from claude_reviewer import redact
from workflow import persistence as _persistence
from workflow import workspace as _workspace
from workflow.state import WorkflowError


LEDGER_SCHEMA_VERSION = 1
EVIDENCE_SCHEMA_VERSION = 1
COMMITMENT_CLASSES = frozenset({
    "mandatory_user_requirement",
    "accepted_architectural_commitment",
    "required_validation_scenario",
    "optional_recommendation",
    "future_production_consideration",
    "explicitly_out_of_scope",
})
AVAILABILITY_STATES = frozenset({"active", "externally_blocked"})
IMPLEMENTATION_STATES = frozenset({"not_started", "partial", "implemented"})
VERIFICATION_STATES = frozenset({"not_run", "passed", "failed", "unverified", "blocked"})
ASSESSMENT_STATES = frozenset({
    "fulfilled", "partially_fulfilled", "not_fulfilled", "insufficient_evidence",
})
AUTHORIZED_DISPOSITIONS = frozenset({"externally_blocked", "deferred", "waived", "unverified"})
REQUIRED_CLASSES = frozenset({
    "mandatory_user_requirement", "accepted_architectural_commitment", "required_validation_scenario",
})
_COMMITMENT_ID_RE = re.compile(r"C-[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_JSON_FENCE = re.compile(r"(?ms)^```(?P<label>[A-Za-z0-9_-]+)[ \t]*\n(?P<body>.*?)^```[ \t]*$")
MAX_JUNIT_ARTIFACT_BYTES = 2_000_000


class TraceabilityError(ValueError):
    """Raised when an approved ledger or evidence document is malformed."""


def _fenced_json(text: str, label: str) -> dict[str, Any] | None:
    matches = [match.group("body") for match in _JSON_FENCE.finditer(text) if match.group("label") == label]
    if not matches:
        return None
    if len(matches) != 1:
        raise TraceabilityError(f"expected exactly one `{label}` JSON block")
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise TraceabilityError(f"`{label}` block is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise TraceabilityError(f"`{label}` block must contain a JSON object")
    return value


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_commitment_ledger(proposal: str) -> dict[str, Any]:
    """Read and validate the approved proposal's single machine-readable ledger."""
    ledger = _fenced_json(proposal, "commitment-ledger")
    if ledger is None:
        raise TraceabilityError("proposal must include one ```commitment-ledger JSON block")
    if ledger.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise TraceabilityError(f"ledger schema_version must be {LEDGER_SCHEMA_VERSION}")
    commitments = ledger.get("commitments")
    if not isinstance(commitments, list) or not commitments:
        raise TraceabilityError("ledger commitments must be a non-empty array")
    ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, commitment in enumerate(commitments):
        label = f"commitments[{index}]"
        if not isinstance(commitment, dict):
            raise TraceabilityError(f"{label} must be an object")
        commitment_id = commitment.get("id")
        if not isinstance(commitment_id, str) or not _COMMITMENT_ID_RE.fullmatch(commitment_id):
            raise TraceabilityError(f"{label}.id must be a stable ID such as C-001")
        if commitment_id in ids:
            raise TraceabilityError(f"duplicate commitment ID {commitment_id}")
        ids.add(commitment_id)
        commitment_class = commitment.get("class")
        if not isinstance(commitment_class, str) or commitment_class not in COMMITMENT_CLASSES:
            raise TraceabilityError(f"{label}.class is not a supported commitment class")
        if not _nonempty_text(commitment.get("statement")):
            raise TraceabilityError(f"{label}.statement must be non-empty")
        if not _nonempty_text(commitment.get("acceptance_condition")):
            raise TraceabilityError(f"{label}.acceptance_condition must be non-empty")
        implementation = commitment.get("implementation_expectations")
        if not ((isinstance(implementation, str) and implementation.strip()) or
                (isinstance(implementation, list) and implementation and
                 all(_nonempty_text(item) for item in implementation))):
            raise TraceabilityError(f"{label}.implementation_expectations must be non-empty text or an array")
        availability = commitment.get("availability")
        if not isinstance(availability, str) or availability not in AVAILABILITY_STATES:
            raise TraceabilityError(f"{label}.availability is not supported")
        implementation_basis = commitment.get("implementation_basis")
        if implementation_basis is not None and implementation_basis not in {
                "new_change", "existing_behavior_acceptable"}:
            raise TraceabilityError(
                f"{label}.implementation_basis must be new_change or existing_behavior_acceptable",
            )
        relevant_sources = commitment.get("relevant_sources")
        if relevant_sources is not None:
            if not isinstance(relevant_sources, list):
                raise TraceabilityError(f"{label}.relevant_sources must be an array")
            normalized_sources = []
            for source_index, source in enumerate(relevant_sources):
                path = source.get("path") if isinstance(source, dict) else None
                symbol = source.get("symbol") if isinstance(source, dict) else None
                relative = PurePosixPath(path) if isinstance(path, str) else None
                if (relative is None or not path or "\\" in path or relative.is_absolute()
                        or any(part in {"", ".", ".."} for part in relative.parts)
                        or not _nonempty_text(symbol)):
                    raise TraceabilityError(
                        f"{label}.relevant_sources[{source_index}] needs a safe repository path and symbol",
                    )
                normalized_sources.append({"path": path, "symbol": symbol.strip()})
        scenarios = commitment.get("verification_scenarios", [])
        if not isinstance(scenarios, list):
            raise TraceabilityError(f"{label}.verification_scenarios must be an array")
        scenario_ids: set[str] = set()
        normalized_scenarios = []
        for scenario_index, scenario in enumerate(scenarios):
            if (not isinstance(scenario, dict) or not _nonempty_text(scenario.get("id"))
                    or not _nonempty_text(scenario.get("description"))):
                raise TraceabilityError(
                    f"{label}.verification_scenarios[{scenario_index}] needs an id and description",
                )
            scenario_id = scenario["id"].strip()
            if scenario_id in scenario_ids:
                raise TraceabilityError(f"duplicate scenario ID {scenario_id} in {commitment_id}")
            scenario_ids.add(scenario_id)
            normalized_scenarios.append({"id": scenario_id, "description": scenario["description"].strip()})
        if commitment_class == "required_validation_scenario" and not normalized_scenarios:
            raise TraceabilityError(f"{commitment_id} is a required validation scenario but has no scenario")
        normalized_commitment = {
            "id": commitment_id,
            "class": commitment_class,
            "statement": commitment["statement"].strip(),
            "acceptance_condition": commitment["acceptance_condition"].strip(),
            "implementation_expectations": implementation,
            "verification_scenarios": normalized_scenarios,
            "availability": availability,
        }
        if implementation_basis is not None:
            normalized_commitment["implementation_basis"] = implementation_basis
        if relevant_sources is not None:
            normalized_commitment["relevant_sources"] = normalized_sources
        normalized.append(normalized_commitment)

    policy = ledger.get("partial_delivery_policy")
    if "partial_delivery_policy" in ledger:
        if not isinstance(policy, dict):
            raise TraceabilityError("partial_delivery_policy must be an object")
        unexpected_policy_fields = set(policy) - {"reason", "authorizations"}
        if unexpected_policy_fields:
            raise TraceabilityError("partial_delivery_policy contains unsupported fields")
        if not _nonempty_text(policy.get("reason")):
            raise TraceabilityError(
                "partial_delivery_policy.reason must be a non-empty string at the policy level",
            )
        authorizations = policy.get("authorizations")
        if not isinstance(authorizations, list) or not authorizations:
            raise TraceabilityError(
                "partial_delivery_policy.authorizations must be a non-empty array; "
                "omit partial_delivery_policy when no partial delivery is authorized",
            )
        seen_authorizations: set[str] = set()
        for index, authorization in enumerate(authorizations):
            if not isinstance(authorization, dict):
                raise TraceabilityError(f"partial_delivery_policy.authorizations[{index}] must be an object")
            if "reason" in authorization:
                raise TraceabilityError(
                    f"partial_delivery_policy.authorizations[{index}] cannot contain reason; "
                    "reason belongs at the policy level",
                )
            if set(authorization) - {"commitment_id", "allowed_dispositions"}:
                raise TraceabilityError(
                    f"partial_delivery_policy.authorizations[{index}] contains unsupported fields",
                )
            commitment_id = authorization.get("commitment_id")
            allowed = authorization.get("allowed_dispositions")
            if not isinstance(commitment_id, str) or commitment_id not in ids:
                raise TraceabilityError(
                    f"partial_delivery_policy.authorizations[{index}].commitment_id must reference a known commitment",
                )
            if commitment_id in seen_authorizations:
                raise TraceabilityError(f"duplicate partial-delivery authorization for {commitment_id}")
            if (not isinstance(allowed, list) or not allowed or any(
                    not isinstance(item, str) or item not in AUTHORIZED_DISPOSITIONS for item in allowed)):
                raise TraceabilityError(
                    f"partial_delivery_policy.authorizations[{index}].allowed_dispositions must be a non-empty array of supported dispositions",
                )
            if len(set(allowed)) != len(allowed):
                raise TraceabilityError(
                    f"partial_delivery_policy.authorizations[{index}].allowed_dispositions contains duplicates",
                )
            seen_authorizations.add(commitment_id)

    return {"schema_version": LEDGER_SCHEMA_VERSION, "commitments": normalized, **(
        {"partial_delivery_policy": policy} if "partial_delivery_policy" in ledger else {}
    )}


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_codex_evidence(text: str, ledger: dict[str, Any]) -> dict[str, Any] | None:
    """Parse Codex's evidence references. The report itself never establishes fulfillment."""
    evidence = _fenced_json(text, "harness-evidence")
    if evidence is None:
        return None
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise TraceabilityError(f"harness-evidence schema_version must be {EVIDENCE_SCHEMA_VERSION}")
    entries = evidence.get("commitments")
    if not isinstance(entries, list):
        raise TraceabilityError("harness-evidence commitments must be an array")
    expected = {item["id"] for item in ledger["commitments"]}
    seen: set[str] = set()
    normalized = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TraceabilityError(f"harness-evidence commitments[{index}] must be an object")
        commitment_id = entry.get("commitment_id")
        if not isinstance(commitment_id, str) or commitment_id not in expected:
            raise TraceabilityError(f"harness-evidence references unknown commitment {commitment_id!r}")
        if commitment_id in seen:
            raise TraceabilityError(f"duplicate evidence entry for {commitment_id}")
        seen.add(commitment_id)
        sources = entry.get("source_refs", [])
        validations = entry.get("validation_refs", [])
        if not isinstance(sources, list) or not isinstance(validations, list):
            raise TraceabilityError(f"evidence arrays for {commitment_id} must be arrays")
        for source in sources:
            if not isinstance(source, dict) or not _nonempty_text(source.get("path")):
                raise TraceabilityError(f"source reference for {commitment_id} needs a repository-relative path")
            if not _nonempty_text(source.get("symbol")):
                raise TraceabilityError(f"source reference for {commitment_id} needs a symbol")
        for reference in validations:
            if not isinstance(reference, dict) or not _nonempty_text(reference.get("scenario_id")):
                raise TraceabilityError(f"validation reference for {commitment_id} needs a scenario_id")
            test_ids = reference.get("test_ids")
            event_ids = reference.get("execution_event_ids", [])
            if (not isinstance(test_ids, list) or not test_ids or
                    not all(_nonempty_text(item) for item in test_ids) or
                    not isinstance(event_ids, list) or
                    not all(_nonempty_text(item) for item in event_ids)):
                raise TraceabilityError(
                    f"validation reference for {commitment_id} needs test_ids and a valid execution_event_ids array",
                )
            if len(set(test_ids)) != len(test_ids):
                raise TraceabilityError(f"validation reference for {commitment_id} repeats a test ID")
            if len(set(event_ids)) != len(event_ids):
                raise TraceabilityError(f"validation reference for {commitment_id} repeats an execution event ID")
            artifact_path = reference.get("artifact_path")
            artifact_sha256 = reference.get("artifact_sha256")
            if (artifact_path is None) != (artifact_sha256 is None):
                raise TraceabilityError(f"JUnit artifact path and hash must both be supplied for {commitment_id}")
            if artifact_path is not None and (
                    not _nonempty_text(artifact_path)
                    or not isinstance(artifact_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256)):
                raise TraceabilityError(f"JUnit artifact reference for {commitment_id} is malformed")
        disposition = entry.get("disposition")
        if disposition is not None and (
                not isinstance(disposition, str)
                or disposition not in {"externally_blocked", "deferred", "waived", "unverified"}):
            raise TraceabilityError(f"evidence disposition for {commitment_id} is not supported")
        reason = entry.get("reason")
        if disposition is not None and not _nonempty_text(reason):
            raise TraceabilityError(f"evidence disposition for {commitment_id} needs a reason")
        normalized.append({
            "commitment_id": commitment_id,
            "source_refs": sources,
            "validation_refs": validations,
            "disposition": disposition,
            "reason": reason if isinstance(reason, str) else None,
        })
    return {"schema_version": EVIDENCE_SCHEMA_VERSION, "commitments": normalized}


def parse_claude_assessment(report: str, ledger: dict[str, Any]) -> dict[str, Any] | None:
    """Validate Claude's independent assessment block; missing/invalid blocks remain unassessed."""
    expected = {
        item["id"] for item in ledger["commitments"] if item["class"] in REQUIRED_CLASSES
    }
    if not expected:
        return {"schema_version": 1, "commitments": []}
    assessment = _fenced_json(report, "commitment-assessment")
    if assessment is None:
        return None
    if assessment.get("schema_version") != 1:
        raise TraceabilityError("commitment-assessment schema_version must be 1")
    entries = assessment.get("commitments")
    if not isinstance(entries, list):
        raise TraceabilityError("commitment-assessment commitments must be an array")
    seen: set[str] = set()
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise TraceabilityError("each commitment assessment must be an object")
        commitment_id = entry.get("id")
        if not isinstance(commitment_id, str) or commitment_id not in expected or commitment_id in seen:
            raise TraceabilityError(f"unknown or duplicate assessed commitment {commitment_id!r}")
        if (not isinstance(entry.get("implementation_status"), str)
                or entry["implementation_status"] not in IMPLEMENTATION_STATES):
            raise TraceabilityError(f"invalid implementation status for {commitment_id}")
        if (not isinstance(entry.get("verification_status"), str)
                or entry["verification_status"] not in VERIFICATION_STATES):
            raise TraceabilityError(f"invalid verification status for {commitment_id}")
        if not isinstance(entry.get("assessment"), str) or entry["assessment"] not in ASSESSMENT_STATES:
            raise TraceabilityError(f"invalid independent assessment for {commitment_id}")
        if not _nonempty_text(entry.get("explanation")):
            raise TraceabilityError(f"assessment for {commitment_id} needs an explanation")
        refs = entry.get("evidence_refs", {})
        if (not isinstance(refs, dict) or not isinstance(refs.get("source", []), list)
                or not isinstance(refs.get("validation", []), list)
                or not all(_nonempty_text(value) for value in refs.get("source", []))
                or not all(_nonempty_text(value) for value in refs.get("validation", []))):
            raise TraceabilityError(f"assessment evidence references for {commitment_id} are malformed")
        seen.add(commitment_id)
        normalized.append({
            "id": commitment_id,
            "implementation_status": entry["implementation_status"],
            "verification_status": entry["verification_status"],
            "assessment": entry["assessment"],
            "explanation": entry["explanation"].strip(),
            "evidence_refs": refs,
        })
    missing = expected - seen
    if missing:
        raise TraceabilityError("Claude assessment is missing commitment IDs: " + ", ".join(sorted(missing)))
    return {"schema_version": 1, "commitments": normalized}


def _source_ref_status(target: Path, source: dict[str, Any], reviewed_paths: set[str]) -> tuple[str, str | None]:
    raw_path = source.get("path")
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        return "invalid", "source path is not a safe repository-relative POSIX path"
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return "invalid", "source path is absolute or contains traversal"
    target_root = target.resolve()
    candidate = target_root
    try:
        for component in relative.parts:
            candidate = candidate / component
            if candidate.is_symlink():
                return "invalid", "source path contains a symlink"
        resolved = candidate.resolve(strict=True)
    except OSError:
        return "missing", "source path does not resolve in the target repository"
    if not resolved.is_relative_to(target_root) or not resolved.is_file():
        return "invalid", "source path resolves outside the target or is not a regular file"
    if raw_path not in reviewed_paths:
        return "unreviewed", "source path is not included in the reviewed patch"
    return "valid", None


def _resolve_junit_artifact(target: Path, artifact_path: str) -> Path:
    raw_path = artifact_path
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise TraceabilityError("JUnit artifact path is not a safe repository-relative POSIX path")
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise TraceabilityError("JUnit artifact path is absolute or contains traversal")
    root = target.resolve()
    candidate = root
    try:
        for component in relative.parts:
            candidate = candidate / component
            if candidate.is_symlink():
                raise TraceabilityError("JUnit artifact path contains a symlink")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise TraceabilityError("JUnit artifact resolves outside the target or is not a file")
    except OSError as exc:
        raise TraceabilityError("JUnit artifact is missing or unreadable") from exc
    return resolved


def junit_artifact_mtime_ns(target: Path, artifact_path: str) -> int:
    """Return mtime for a safe artifact path after applying the JUnit path checks."""
    try:
        return _resolve_junit_artifact(target, artifact_path).stat().st_mtime_ns
    except OSError as exc:
        raise TraceabilityError("JUnit artifact is missing or unreadable") from exc


def _timestamp_ns(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1_000_000_000)


def junit_test_results(target: Path, artifact_path: str, expected_sha256: str | None = None) -> tuple[list[dict], str]:
    """Read bounded JUnit XML from a safe target path and return only individual outcomes."""
    resolved = _resolve_junit_artifact(target, artifact_path)
    try:
        if resolved.stat().st_size > MAX_JUNIT_ARTIFACT_BYTES:
            raise TraceabilityError("JUnit artifact exceeds the supported size limit")
        content = resolved.read_bytes()
    except OSError as exc:
        raise TraceabilityError("JUnit artifact is missing or unreadable") from exc
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise TraceabilityError("JUnit artifact hash does not match the evidence reference")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise TraceabilityError("JUnit XML must not declare a document type or entity")
    try:
        root_element = ET.fromstring(content)
    except ET.ParseError as exc:
        raise TraceabilityError("JUnit artifact is not valid XML") from exc
    root_tag = root_element.tag.rsplit("}", 1)[-1]
    if root_tag not in {"testsuite", "testsuites"}:
        raise TraceabilityError("JUnit XML must have a testsuite or testsuites root")
    cases = [item for item in root_element.iter() if item.tag.rsplit("}", 1)[-1] == "testcase"]
    if not cases:
        raise TraceabilityError("JUnit artifact contains no testcase results")
    results = []
    for case in cases:
        name = case.get("name")
        if not _nonempty_text(name):
            continue
        file_name = case.get("file")
        classname = case.get("classname")
        if _nonempty_text(file_name):
            file_path = Path(file_name)
            if file_path.is_absolute():
                try:
                    file_name = file_path.resolve().relative_to(root).as_posix()
                except ValueError as exc:
                    raise TraceabilityError("JUnit testcase path resolves outside the target") from exc
            test_id = file_name.replace("\\", "/") + "::" + name
        elif _nonempty_text(classname):
            test_id = classname + "." + name
        else:
            test_id = name
        children = {child.tag.rsplit("}", 1)[-1] for child in case}
        status = "failed" if children & {"failure", "error"} else "skipped" if "skipped" in children else "passed"
        results.append({"test_id": test_id, "status": status})
    if not results:
        raise TraceabilityError("JUnit artifact contains no named testcase results")
    return results, digest


def _index_test_results(results: Any) -> dict[str, dict[str, Any]] | None:
    """Index a result list only when test IDs are unique and each result is well formed."""
    if not isinstance(results, list):
        return None
    indexed: dict[str, dict[str, Any]] = {}
    for result in results:
        status = result.get("status") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or not _nonempty_text(result.get("test_id"))
                or not isinstance(status, str) or status not in {"passed", "failed", "skipped"}):
            return None
        test_id = result["test_id"]
        if test_id in indexed:
            return None
        indexed[test_id] = result
    return indexed


def bind_observed_execution_events(evidence: dict[str, Any], receipts: list[Any]) -> None:
    """Fill empty event references only from unique observed pytest/unittest receipts."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("commitments"), list):
        return
    receipt_list = receipts if isinstance(receipts, list) else []
    event_counts: dict[str, int] = {}
    for receipt in receipt_list:
        if isinstance(receipt, dict) and isinstance(receipt.get("event_id"), str):
            event_counts[receipt["event_id"]] = event_counts.get(receipt["event_id"], 0) + 1

    test_events: dict[str, list[tuple[str, int]]] = {}
    for receipt in receipt_list:
        if (not isinstance(receipt, dict)
                or receipt.get("runner") not in {"pytest", "unittest"}
                or type(receipt.get("exit_code")) is not int):
            continue
        event_id = receipt.get("event_id")
        event_index = receipt.get("event_index")
        if (not isinstance(event_id, str) or not event_id or event_counts.get(event_id) != 1
                or type(event_index) is not int):
            continue
        results = _index_test_results(receipt.get("tests"))
        if results is None:
            continue
        for test_id in results:
            test_events.setdefault(test_id, []).append((event_id, event_index))

    for entry in evidence["commitments"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("validation_refs"), list):
            continue
        for reference in entry["validation_refs"]:
            if (not isinstance(reference, dict)
                    or reference.get("execution_event_ids") not in (None, [])):
                continue
            test_ids = reference.get("test_ids")
            if not isinstance(test_ids, list) or not test_ids:
                continue
            matches = [test_events.get(test_id, []) for test_id in test_ids]
            if any(len(events) != 1 for events in matches):
                continue
            events = sorted({event for group in matches for event in group}, key=lambda item: item[1])
            reference["execution_event_ids"] = [event_id for event_id, _index in events]


def _junit_receipt_matches(
    target: Path,
    artifact_path: str,
    artifact_sha256: str,
    receipt: dict[str, Any],
    *,
    event_id: str,
    operation: dict[str, Any],
    operation_fingerprint: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Require JUnit metadata captured by the Harness for this exact command and snapshot."""
    metadata = receipt.get("junit_artifact")
    if not isinstance(metadata, dict):
        return None, "JUnit artifact is not attached to the execution receipt"
    if (metadata.get("path") != artifact_path or metadata.get("sha256") != artifact_sha256
            or metadata.get("event_id") != event_id
            or metadata.get("operation_id") != operation.get("operation_id")
            or metadata.get("target_fingerprint") != operation_fingerprint
            or receipt.get("junit_report_path") != artifact_path):
        return None, "JUnit artifact is bound to a different event, operation, or target snapshot"
    operation_receipts = operation.get("test_execution_events", [])
    matching_reports = [
        item for item in operation_receipts
        if isinstance(item, dict) and item.get("junit_report_path") == artifact_path
    ] if isinstance(operation_receipts, list) else []
    if len(matching_reports) != 1 or matching_reports[0].get("event_id") != event_id:
        return None, "JUnit report path is ambiguous across execution events in the operation"
    captured_mtime = metadata.get("mtime_ns")
    start_ns = _timestamp_ns(operation.get("started_at"))
    if (not isinstance(captured_mtime, int) or isinstance(captured_mtime, bool)
            or start_ns is None or captured_mtime <= start_ns):
        return None, "JUnit artifact predates the execution operation or lacks a valid capture time"
    try:
        results, digest = junit_test_results(target, artifact_path, artifact_sha256)
        current_mtime = junit_artifact_mtime_ns(target, artifact_path)
    except TraceabilityError as exc:
        return None, str(exc)
    if digest != metadata.get("sha256") or current_mtime != captured_mtime:
        return None, "JUnit artifact changed after it was captured for the execution event"
    return results, None


def evaluate_ledger(
    ledger: dict[str, Any],
    evidence: dict[str, Any] | None,
    assessment: dict[str, Any] | None,
    *,
    target: Path,
    reviewed_paths: set[str],
    operations: list[dict[str, Any]],
    final_fingerprint: dict[str, Any] | None,
    workflow_changed_paths: set[str] | None = None,
    review_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return per-commitment evidence outcomes and a conservative delivery disposition."""
    evidence_by_id = {
        item["commitment_id"]: item for item in (evidence or {}).get("commitments", [])
    }
    assessment_by_id = {item["id"]: item for item in (assessment or {}).get("commitments", [])}
    operation_by_id: dict[str, list[dict[str, Any]]] = {}
    eligible_operations = []
    for operation in (operations if isinstance(operations, list) else []):
        if not isinstance(operation, dict):
            continue
        operation_id = operation.get("operation_id")
        if isinstance(operation_id, str):
            operation_by_id.setdefault(operation_id, []).append(operation)
        if (operation.get("operation_type") == "codex"
                and operation.get("stage") in {"implementation", "correction"}
                and operation.get("status") == "completed"):
            eligible_operations.append(operation)
    latest_operation_id = (
        eligible_operations[-1].get("operation_id") if eligible_operations else None
    )
    final_tree = (final_fingerprint or {}).get("working_tree_sha256")
    results: list[dict[str, Any]] = []
    incomplete_required: list[dict[str, Any]] = []
    for commitment in ledger["commitments"]:
        commitment_id = commitment["id"]
        item_evidence = evidence_by_id.get(commitment_id, {})
        item_assessment = assessment_by_id.get(commitment_id, {})
        source_refs = item_evidence.get("source_refs", [])
        source_results = [_source_ref_status(target, ref, reviewed_paths) for ref in source_refs]
        source_status = (
            "valid" if source_results and all(status == "valid" for status, _ in source_results)
            else "missing" if not source_results else "invalid"
        )
        source_reasons = [reason for _status, reason in source_results if reason]
        changed_paths = workflow_changed_paths
        if changed_paths is None:
            implementation_status = "unverified"
        elif any(
                isinstance(ref, dict) and ref.get("path") in changed_paths
                for ref in source_refs):
            implementation_status = "valid"
        elif commitment.get("implementation_basis", "new_change") == "existing_behavior_acceptable":
            implementation_status = "baseline_allowed"
        else:
            implementation_status = "missing"
            source_reasons.append("no referenced source file has an attributable workflow change")

        validation_refs = item_evidence.get("validation_refs", [])
        expected_scenarios = {item["id"] for item in commitment["verification_scenarios"]}
        matched_scenarios: set[str] = set()
        validation_failed = False
        validation_invalid = False
        for reference in validation_refs:
            scenario_id = reference.get("scenario_id")
            if scenario_id not in expected_scenarios:
                validation_invalid = True
                continue
            operation_id = item_evidence.get("operation_id")
            reference_operation_id = reference.get("operation_id")
            if (not isinstance(operation_id, str) or not operation_id
                    or operation_id != latest_operation_id
                    or (reference_operation_id is not None and reference_operation_id != operation_id)):
                validation_invalid = True
                continue
            operation_matches = operation_by_id.get(operation_id, [])
            if len(operation_matches) != 1:
                validation_invalid = True
                continue
            operation = operation_matches[0]
            if (not isinstance(operation, dict) or operation.get("status") != "completed"
                    or operation.get("stage") not in {"implementation", "correction"}):
                validation_invalid = True
                continue
            operation_fingerprint = operation.get("target_fingerprint")
            if (not _nonempty_text(final_tree) or not isinstance(operation_fingerprint, dict)
                    or operation_fingerprint.get("status") != "captured"
                    or operation_fingerprint.get("working_tree_sha256") != final_tree):
                validation_invalid = True
                continue
            receipt_list = operation.get("test_execution_events", [])
            receipts_by_id: dict[str, list[dict[str, Any]]] = {}
            if isinstance(receipt_list, list):
                for receipt in receipt_list:
                    if isinstance(receipt, dict) and isinstance(receipt.get("event_id"), str):
                        receipts_by_id.setdefault(receipt["event_id"], []).append(receipt)
            reference_test_ids = set(reference.get("test_ids", []))
            event_ids = reference.get("execution_event_ids", [])
            artifact_path = reference.get("artifact_path")
            artifact_sha256 = reference.get("artifact_sha256")
            if artifact_path and len(event_ids) != 1:
                validation_invalid = True
                continue

            all_passed = True
            event_test_results: dict[str, dict[str, Any]] = {}
            for event_id in event_ids:
                matches = receipts_by_id.get(event_id, [])
                if len(matches) != 1:
                    all_passed = False
                    validation_invalid = True
                    continue
                receipt = matches[0]
                exit_code = receipt.get("exit_code")
                runner = receipt.get("runner")
                event_successful = type(exit_code) is int and exit_code == 0
                if not event_successful:
                    all_passed = False
                    validation_invalid = True
                if not isinstance(runner, str) or runner not in {"pytest", "unittest"}:
                    all_passed = False
                    validation_invalid = True
                    observed_tests = _index_test_results(receipt.get("tests"))
                    if observed_tests is not None and any(
                            test_id in reference_test_ids and observed_tests[test_id].get("status") == "failed"
                            for test_id in observed_tests):
                        validation_failed = True
                    continue

                artifact_provenance_valid = False
                if artifact_path:
                    if runner == "pytest" and event_successful:
                        artifact_tests, artifact_error = _junit_receipt_matches(
                            target, artifact_path, artifact_sha256, receipt,
                            event_id=event_id, operation=operation,
                            operation_fingerprint=operation_fingerprint,
                        )
                    else:
                        artifact_tests, artifact_error = None, "JUnit needs a successful pytest event"
                    if artifact_error is None:
                        test_results = _index_test_results(artifact_tests)
                        artifact_provenance_valid = test_results is not None
                    else:
                        validation_invalid = True
                        test_results = _index_test_results(receipt.get("tests"))
                else:
                    test_results = _index_test_results(receipt.get("tests"))
                if test_results is None:
                    all_passed = False
                    validation_invalid = True
                    continue

                matched_test_ids = reference_test_ids & test_results.keys()
                if not matched_test_ids:
                    all_passed = False
                    validation_invalid = True
                    continue
                for test_id in matched_test_ids:
                    test = test_results[test_id]
                    if test.get("status") == "failed":
                        validation_failed = True
                    if test_id in event_test_results:
                        validation_invalid = True
                        continue
                    if event_successful and (not artifact_path or artifact_provenance_valid):
                        event_test_results[test_id] = test

            missing_test_ids = set(reference_test_ids) - event_test_results.keys()
            if missing_test_ids:
                if not validation_failed:
                    all_passed = False
                    validation_invalid = True
            for test in event_test_results.values():
                if test.get("status") != "passed":
                    all_passed = False
                    validation_failed = True
            if all_passed and set(reference_test_ids) == event_test_results.keys():
                matched_scenarios.add(scenario_id)

        if not expected_scenarios:
            verification_status = "not_run"
        elif validation_failed:
            verification_status = "failed"
        elif validation_invalid:
            verification_status = "unverified"
        elif expected_scenarios <= matched_scenarios:
            verification_status = "passed"
        elif commitment["availability"] == "externally_blocked":
            verification_status = "blocked"
        else:
            verification_status = "unverified"

        independent = item_assessment or {}
        allowed_source_refs = {
            ref.get("path") for ref in source_refs if isinstance(ref, dict)
        } | {
            f"{ref.get('path')}#{ref.get('symbol')}" for ref in source_refs if isinstance(ref, dict)
        }
        allowed_validation_refs = {
            value
            for ref in validation_refs if isinstance(ref, dict)
            for value in [ref.get("scenario_id"), *ref.get("test_ids", [])]
            if isinstance(value, str)
        }
        claude_refs = independent.get("evidence_refs", {})
        claude_refs_valid = bool(independent) and all(
            value in allowed_source_refs for value in claude_refs.get("source", [])
        ) and all(value in allowed_validation_refs for value in claude_refs.get("validation", []))
        if independent.get("assessment") == "fulfilled" and (
                not claude_refs.get("source") or (expected_scenarios and not claude_refs.get("validation"))):
            claude_refs_valid = False
        if independent.get("assessment") == "fulfilled" and expected_scenarios:
            validation_claims = set(claude_refs.get("validation", []))
            for scenario_ref in validation_refs:
                scenario_id = scenario_ref.get("scenario_id")
                cited_tests = set(scenario_ref.get("test_ids", []))
                if not ({scenario_id} | cited_tests) & validation_claims:
                    claude_refs_valid = False
        result = {
            "id": commitment_id,
            "class": commitment["class"],
            "statement": commitment["statement"],
            "acceptance_condition": commitment["acceptance_condition"],
            "availability": commitment["availability"],
            "implementation_status": independent.get("implementation_status"),
            "verification_status": verification_status,
            "claude_verification_status": independent.get("verification_status"),
            "claude_assessment": independent.get("assessment"),
            "claude_explanation": independent.get("explanation"),
            "claude_evidence_refs_valid": claude_refs_valid,
            "source_evidence_status": source_status,
            "implementation_evidence_status": implementation_status,
            "implementation_operation_id": item_evidence.get("operation_id"),
            "implementation_target_fingerprint": (
                operation_by_id.get(item_evidence.get("operation_id"), [{}])[0].get("target_fingerprint")
                if len(operation_by_id.get(item_evidence.get("operation_id"), [])) == 1 else None
            ),
            "source_provenance": [
                {
                    "path": ref.get("path"),
                    "symbol": ref.get("symbol"),
                    "origin": (
                        "unknown" if changed_paths is None else
                        "workflow_change" if ref.get("path") in changed_paths else "baseline_context"
                    ),
                }
                for ref in source_refs if isinstance(ref, dict)
            ],
            "source_refs": source_refs,
            "validation_refs": validation_refs,
            "validation_provenance": [
                {
                    "scenario_id": reference.get("scenario_id"),
                    "test_ids": reference.get("test_ids", []),
                    "execution_event_ids": reference.get("execution_event_ids", []),
                    "operation_id": item_evidence.get("operation_id"),
                    "target_fingerprint": (
                        operation_by_id.get(item_evidence.get("operation_id"), [{}])[0].get("target_fingerprint")
                        if len(operation_by_id.get(item_evidence.get("operation_id"), [])) == 1 else None
                    ),
                }
                for reference in validation_refs if isinstance(reference, dict)
            ],
            "review_evidence": {
                **(review_provenance or {}),
                "evidence_refs": claude_refs if isinstance(claude_refs, dict) else {},
            },
            "reasons": source_reasons,
            "disposition": item_evidence.get("disposition"),
            "disposition_reason": item_evidence.get("reason"),
        }
        if expected_scenarios and verification_status != "passed":
            result["reasons"].append(
                "required scenario evidence is missing, invalid, failed, or not bound to the reviewed target"
                if not validation_failed else "one or more required tests failed",
            )
        if validation_invalid:
            result["reasons"].append("one or more validation references lack valid execution provenance")
        if not independent:
            result["reasons"].append("Claude commitment assessment is missing or invalid")
        if independent.get("assessment") != "fulfilled":
            result["reasons"].append("Claude did not independently assess the commitment as fulfilled")
        if not claude_refs_valid:
            result["reasons"].append("Claude evidence references are missing or do not match Harness evidence")
        if independent.get("implementation_status") != "implemented":
            result["reasons"].append("implementation is not independently assessed as implemented")
        if (expected_scenarios
                and independent.get("verification_status") != verification_status):
            result["reasons"].append("Claude and Harness verification statuses disagree")
        if commitment["class"] in REQUIRED_CLASSES:
            fulfilled = (
                source_status == "valid"
                and implementation_status in {"valid", "baseline_allowed", "unverified"}
                and independent.get("implementation_status") == "implemented"
                and independent.get("assessment") == "fulfilled"
                and claude_refs_valid
                and (not expected_scenarios or (
                    verification_status == "passed"
                    and independent.get("verification_status") == "passed"
                ))
            )
            if not fulfilled:
                incomplete_required.append(result)
        results.append(result)

    if not incomplete_required:
        disposition = "complete"
        rationale = "All active required commitments have valid source evidence, required scenarios passed, and Claude assessed them as fulfilled."
    else:
        authorizations = {
            item["commitment_id"]: set(item["allowed_dispositions"])
            for item in ledger.get("partial_delivery_policy", {}).get("authorizations", [])
        }
        all_authorized = True
        for item in incomplete_required:
            if item.get("verification_status") == "failed":
                all_authorized = False
                break
            reason_disposition = item.get("disposition")
            if item["availability"] == "externally_blocked":
                reason_disposition = "externally_blocked"
            allowed = authorizations.get(item["id"], set())
            evidence_invalid = item["source_evidence_status"] in {"invalid", "unreviewed"}
            missing_source_disallowed = (
                item["source_evidence_status"] == "missing"
                and reason_disposition not in {"externally_blocked", "deferred", "waived"}
            )
            external_failure = (
                reason_disposition == "externally_blocked"
                and not item.get("disposition_reason")
            )
            unverified_without_source = (
                reason_disposition == "unverified" and item["source_evidence_status"] != "valid"
            )
            if (reason_disposition not in allowed or evidence_invalid or missing_source_disallowed
                    or external_failure or unverified_without_source or not item.get("disposition_reason")):
                all_authorized = False
                break
        if all_authorized:
            disposition = "partial"
            rationale = "The approved partial-delivery policy authorizes the recorded incomplete commitment dispositions."
        else:
            disposition = "blocked"
            rationale = "One or more required commitments are incomplete without a matching approved partial-delivery authorization."
    return {
        "schema_version": 1,
        "disposition": disposition,
        "rationale": rationale,
        "partial_delivery_policy": ledger.get("partial_delivery_policy"),
        "commitments": results,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _fingerprint_repository(target: Path) -> dict:
    try:
        fingerprint = _workspace.repository_fingerprint(target, redactor=redact)
    except _workspace.WorkspaceError as exc:
        raise WorkflowError(str(exc)) from exc
    return {"status": "captured", "captured_at": _now(), **fingerprint}


def capture_commitment_evidence(state, target: Path, report: str) -> None:
    """Bind Codex's references to the completed implementation/correction operation."""
    if state.get("traceability_required") is not True:
        return
    approval = state.get("commitment_ledger_approval")
    ledger = approval.get("ledger") if isinstance(approval, dict) else None
    if not isinstance(ledger, dict):
        state["commitment_evidence"] = {"error": "approved commitment ledger is unavailable"}
        return
    operation_history = state.get("operation_history", [])
    if not isinstance(operation_history, list):
        operation_history = []
    operation = next((
        item for item in reversed(operation_history)
        if isinstance(item, dict) and item.get("operation_type") == "codex"
        and item.get("stage") in {"implementation", "correction"} and item.get("status") == "completed"
    ), None)
    try:
        evidence = parse_codex_evidence(report, ledger)
        if evidence is None:
            raise TraceabilityError("Codex did not return a harness-evidence block")
        if operation is None:
            raise TraceabilityError("no completed implementation operation can own the evidence")
        operation_id = operation.get("operation_id")
        history = state.get("operation_history", [])
        if (not isinstance(operation_id, str)
                or not isinstance(history, list)
                or sum(isinstance(item, dict) and item.get("operation_id") == operation_id for item in history) != 1):
            raise TraceabilityError("implementation operation ID is missing or ambiguous")
        bind_observed_execution_events(
            evidence, operation.get("test_execution_events", []),
        )
        operation["target_fingerprint"] = _fingerprint_repository(target)
        receipts_by_id: dict[str, list[dict]] = {}
        for item in operation.get("test_execution_events", []):
            if isinstance(item, dict) and isinstance(item.get("event_id"), str):
                receipts_by_id.setdefault(item["event_id"], []).append(item)
        for entry in evidence["commitments"]:
            entry["operation_id"] = operation_id
            for reference in entry.get("validation_refs", []):
                if reference.get("operation_id") not in (None, operation_id):
                    raise TraceabilityError(
                        "validation reference names a different Codex operation",
                    )
                reference["operation_id"] = operation_id
                artifact_path = reference.get("artifact_path")
                if not artifact_path:
                    continue
                event_ids = reference.get("execution_event_ids", [])
                if len(event_ids) != 1:
                    reference["artifact_error"] = "one JUnit artifact must identify exactly one execution event"
                    continue
                try:
                    _tests, artifact_digest = junit_test_results(
                        target, artifact_path, reference.get("artifact_sha256"),
                    )
                    artifact_mtime_ns = junit_artifact_mtime_ns(target, artifact_path)
                except TraceabilityError as exc:
                    reference["artifact_error"] = str(exc)
                    continue
                event_id = event_ids[0]
                matching_receipts = receipts_by_id.get(event_id, [])
                receipt = matching_receipts[0] if len(matching_receipts) == 1 else None
                if (not isinstance(receipt, dict) or type(receipt.get("exit_code")) is not int
                        or receipt.get("exit_code") != 0
                        or receipt.get("runner") != "pytest"
                        or receipt.get("junit_report_path") != artifact_path):
                    reference["artifact_error"] = (
                        "JUnit artifact path is not the report output of its successful pytest execution event"
                    )
                    continue
                receipt["junit_artifact"] = {
                    "path": artifact_path,
                    "sha256": artifact_digest,
                    "mtime_ns": artifact_mtime_ns,
                    "event_id": event_id,
                    "operation_id": operation_id,
                    "target_fingerprint": operation.get("target_fingerprint"),
                }
        receipt_material = []
        for entry in evidence["commitments"]:
            for reference in entry.get("validation_refs", []):
                for event_id in reference.get("execution_event_ids", []):
                    matching_receipts = receipts_by_id.get(event_id, [])
                    receipt = matching_receipts[0] if len(matching_receipts) == 1 else None
                    if isinstance(receipt, dict):
                        receipt_material.append({
                            "operation_id": operation_id,
                            "operation_stdout_sha256": operation.get("stdout_sha256"),
                            "event_id": event_id,
                            "receipt": receipt,
                        })
        record = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "proposal_version": approval.get("proposal_version"),
            "proposal_sha256": approval.get("proposal_sha256"),
            "ledger_sha256": approval.get("ledger_sha256"),
            "operation_id": operation_id,
            "operation_stdout_sha256": operation.get("stdout_sha256"),
            "operation_target_fingerprint": operation.get("target_fingerprint"),
            "test_receipts_sha256": canonical_sha256(receipt_material),
            "evidence": evidence,
        }
        record["evidence_sha256"] = canonical_sha256(record)
        state["commitment_evidence"] = record
    except (TraceabilityError, KeyError, TypeError, AttributeError) as exc:
        state["commitment_evidence"] = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "proposal_version": approval.get("proposal_version"),
            "proposal_sha256": approval.get("proposal_sha256"),
            "ledger_sha256": approval.get("ledger_sha256"),
            "operation_id": operation.get("operation_id") if operation else None,
            "operation_stdout_sha256": operation.get("stdout_sha256") if operation else None,
            "operation_target_fingerprint": operation.get("target_fingerprint") if operation else None,
            "test_receipts_sha256": canonical_sha256([]),
            "evidence": None,
            "error": str(exc),
        }
        state["commitment_evidence"]["evidence_sha256"] = canonical_sha256(
            state["commitment_evidence"],
        )

def capture_commitment_assessment(state, report: str, *, purpose: str) -> None:
    if state.get("traceability_required") is not True:
        return
    approval = state.get("commitment_ledger_approval")
    ledger = approval.get("ledger") if isinstance(approval, dict) else None
    assessment = None
    error = None
    if not isinstance(ledger, dict):
        error = "approved commitment ledger is unavailable"
    else:
        try:
            assessment = parse_claude_assessment(report, ledger)
            if assessment is None:
                error = "Claude report did not include a commitment-assessment block"
        except (TraceabilityError, KeyError, TypeError, AttributeError) as exc:
            error = str(exc)
    state["commitment_assessment"] = {
        "schema_version": 1,
        "purpose": purpose,
        "proposal_version": state.get("proposal_version"),
        "proposal_sha256": approval.get("proposal_sha256") if isinstance(approval, dict) else None,
        "ledger_sha256": approval.get("ledger_sha256") if isinstance(approval, dict) else None,
        "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
        "assessment": assessment,
        "error": error,
    }

def evaluate_commitment_delivery(state, target: Path, proposal_path: Path) -> dict | None:
    if state.get("traceability_required") is not True:
        return None
    approval = state.get("commitment_ledger_approval")
    if not isinstance(approval, dict) or not isinstance(approval.get("ledger"), dict):
        result = {
            "schema_version": 1,
            "disposition": "blocked",
            "rationale": "No approved commitment ledger snapshot is available.",
            "commitments": [],
        }
        state["commitment_gate"] = result
        state["delivery_disposition"] = "blocked"
        return result
    evidence_record = state.get("commitment_evidence")
    evidence = None
    hard_errors: list[str] = []
    if approval.get("ledger_sha256") != canonical_sha256(approval["ledger"]):
        hard_errors.append("Approved commitment ledger snapshot changed after approval")
    if approval.get("proposal_sha256") != state.get("proposal_artifact_sha256"):
        hard_errors.append("Approved proposal identity changed after approval")
    try:
        current_proposal_sha256 = hashlib.sha256(_persistence.read_bytes(proposal_path)).hexdigest()
    except (OSError, WorkflowError):
        current_proposal_sha256 = None
    if current_proposal_sha256 != approval.get("proposal_sha256"):
        hard_errors.append("Proposal artifact changed or is unavailable after approval")
    ledger_commitments = approval["ledger"].get("commitments", [])
    if not isinstance(ledger_commitments, list):
        hard_errors.append("Approved commitment ledger commitments are malformed")
        ledger_commitments = []
    has_required_commitments = any(
        item.get("class") in REQUIRED_CLASSES
        for item in ledger_commitments if isinstance(item, dict)
    )
    operation_history = state.get("operation_history", [])
    if not isinstance(operation_history, list):
        operation_history = []
        hard_errors.append("Operation history is malformed")
    operations_by_id: dict[str, list[dict]] = {}
    completed_work_operations = []
    for operation in operation_history:
        if not isinstance(operation, dict):
            continue
        operation_id = operation.get("operation_id")
        if isinstance(operation_id, str):
            operations_by_id.setdefault(operation_id, []).append(operation)
        if (operation.get("operation_type") == "codex"
                and operation.get("stage") in {"implementation", "correction"}
                and operation.get("status") == "completed"):
            completed_work_operations.append(operation)
    duplicate_operation_ids = sorted(
        operation_id for operation_id, matches in operations_by_id.items() if len(matches) != 1
    )
    if duplicate_operation_ids:
        hard_errors.append("Duplicate operation IDs are present: " + ", ".join(duplicate_operation_ids))
    latest_operation = completed_work_operations[-1] if completed_work_operations else None
    if not isinstance(evidence_record, dict):
        if has_required_commitments:
            hard_errors.append("Codex commitment evidence record is missing")
    else:
        evidence_payload = dict(evidence_record)
        saved_digest = evidence_payload.pop("evidence_sha256", None)
        if saved_digest != canonical_sha256(evidence_payload):
            hard_errors.append("Codex commitment evidence digest does not match the saved record")
        elif (evidence_record.get("ledger_sha256") != approval.get("ledger_sha256")
              or evidence_record.get("proposal_version") != approval.get("proposal_version")
              or evidence_record.get("proposal_sha256") != approval.get("proposal_sha256")):
            hard_errors.append("Codex evidence is bound to a different approved proposal or ledger")
        else:
            evidence = evidence_record.get("evidence")
            if evidence is not None and not isinstance(evidence, dict):
                hard_errors.append("Codex commitment evidence payload is malformed")
                evidence = None
            if evidence is None and has_required_commitments and not evidence_record.get("error"):
                hard_errors.append("Codex commitment evidence payload is missing")
            if isinstance(evidence, dict):
                evidence_operation_id = evidence_record.get("operation_id")
                if has_required_commitments and (
                        not isinstance(latest_operation, dict)
                        or evidence_operation_id != latest_operation.get("operation_id")):
                    hard_errors.append("Codex evidence is stale; it is not bound to the latest implementation or correction operation")
                operation_matches = (
                    operations_by_id.get(evidence_operation_id, [])
                    if isinstance(evidence_operation_id, str) else []
                )
                if len(operation_matches) != 1:
                    if has_required_commitments:
                        hard_errors.append("Codex evidence operation ID is missing or ambiguous")
                    bound_operation = {}
                else:
                    bound_operation = operation_matches[0]
                if has_required_commitments and isinstance(latest_operation, dict) and bound_operation is latest_operation:
                    if (evidence_record.get("operation_stdout_sha256") != bound_operation.get("stdout_sha256")
                            or evidence_record.get("operation_target_fingerprint") != bound_operation.get("target_fingerprint")):
                        hard_errors.append("Codex evidence operation output or target fingerprint changed after capture")
                receipt_material = []
                evidence_entries = evidence.get("commitments", [])
                if not isinstance(evidence_entries, list):
                    hard_errors.append("Codex evidence commitments are malformed")
                    evidence_entries = []
                for item in evidence_entries:
                    if not isinstance(item, dict):
                        hard_errors.append("Codex evidence contains a malformed commitment entry")
                        continue
                    operation_id = item.get("operation_id") or evidence_operation_id
                    if has_required_commitments and operation_id != evidence_operation_id:
                        hard_errors.append("A commitment evidence entry references a different operation")
                        continue
                    operation_candidates = (
                        operations_by_id.get(operation_id, []) if isinstance(operation_id, str) else []
                    )
                    if len(operation_candidates) != 1:
                        continue
                    operation = operation_candidates[0]
                    receipt_groups: dict[str, list[dict]] = {}
                    receipts = operation.get("test_execution_events", [])
                    if isinstance(receipts, list):
                        for receipt in receipts:
                            if isinstance(receipt, dict) and isinstance(receipt.get("event_id"), str):
                                receipt_groups.setdefault(receipt["event_id"], []).append(receipt)
                    validation_refs = item.get("validation_refs", [])
                    if not isinstance(validation_refs, list):
                        hard_errors.append("Codex validation references are malformed")
                        continue
                    for reference in validation_refs:
                        if not isinstance(reference, dict):
                            hard_errors.append("Codex validation reference is malformed")
                            continue
                        event_ids = reference.get("execution_event_ids", [])
                        if not isinstance(event_ids, list):
                            hard_errors.append("Codex execution event references are malformed")
                            continue
                        for event_id in event_ids:
                            if not isinstance(event_id, str):
                                hard_errors.append("Codex execution event ID is malformed")
                                continue
                            matches = receipt_groups.get(event_id, [])
                            if len(matches) == 1:
                                receipt_material.append({
                                    "operation_id": operation_id,
                                    "operation_stdout_sha256": operation.get("stdout_sha256"),
                                    "event_id": event_id,
                                    "receipt": matches[0],
                                })
                if (evidence_record.get("test_receipts_sha256")
                        != canonical_sha256(receipt_material)):
                    hard_errors.append("Observed test execution receipts changed after capture")
    assessment_record = state.get("commitment_assessment")
    assessment = None
    expected_assessment_purpose = "verification" if state.get("correction_attempts") else "initial"
    report_text = (
        state.get("verification_report")
        if state.get("correction_attempts") else state.get("code_review")
    )
    if not isinstance(assessment_record, dict):
        hard_errors.append("Claude commitment assessment record is missing")
    elif (assessment_record.get("ledger_sha256") != approval.get("ledger_sha256")
          or assessment_record.get("proposal_version") != approval.get("proposal_version")
          or assessment_record.get("proposal_sha256") != approval.get("proposal_sha256")
          or assessment_record.get("purpose") != expected_assessment_purpose
          or assessment_record.get("report_sha256") != hashlib.sha256(str(report_text or "").encode("utf-8")).hexdigest()):
        hard_errors.append("Claude commitment assessment is stale or bound to a different approved proposal or review report")
    else:
        assessment = assessment_record.get("assessment")
        try:
            report_assessment = parse_claude_assessment(
                str(report_text or ""), approval["ledger"],
            )
        except (TraceabilityError, KeyError, TypeError, AttributeError):
            report_assessment = None
        if assessment != report_assessment:
            hard_errors.append("Saved Claude assessment does not match the bound review report")
            assessment = None
    integrity_record = state.get("integrity")
    review_evidence = integrity_record.get("review_evidence", {}) if isinstance(integrity_record, dict) else {}
    final_fingerprint = {
        "working_tree_sha256": review_evidence.get("target_working_tree_sha256")
        if isinstance(review_evidence, dict) else None,
    }
    review_provenance = {
        key: review_evidence.get(key)
        for key in (
            "campaign_id", "purpose", "review_reports_sha256", "target_commit_sha",
            "target_working_tree_sha256", "review_scope_sha256",
        )
    } if isinstance(review_evidence, dict) else None
    try:
        evaluation = evaluate_ledger(
            approval["ledger"], evidence, assessment,
            target=target,
            reviewed_paths=set(state.get("review_scope_files", state.get("review_included_files", []))),
            operations=state.get("operation_history", []),
            final_fingerprint=final_fingerprint,
            workflow_changed_paths=set(state.get("review_workflow_paths", [])),
            review_provenance=review_provenance,
        )
    except Exception as exc:
        hard_errors.append(f"Commitment evaluation failed: {type(exc).__name__}")
        evaluation = {"schema_version": 1, "disposition": "blocked", "commitments": []}
    if hard_errors:
        evaluation["disposition"] = "blocked"
        evaluation["rationale"] = "; ".join(hard_errors)
    evaluation["workflow_status"] = state.get("status")
    evaluation["approved_ledger_sha256"] = approval.get("ledger_sha256")
    evaluation["evidence_integrity_errors"] = hard_errors
    state["commitment_gate"] = evaluation
    state["delivery_disposition"] = evaluation["disposition"]
    return evaluation

def commitment_review_context(state) -> str:
    approval = state.get("commitment_ledger_approval")
    evidence_record = state.get("commitment_evidence")
    if not isinstance(approval, dict) or not isinstance(evidence_record, dict):
        return "No approved commitment ledger or Codex evidence record is available."
    evidence = evidence_record.get("evidence")
    referenced_events = {
        event_id
        for item in (evidence or {}).get("commitments", []) if isinstance(item, dict)
        for reference in item.get("validation_refs", []) if isinstance(reference, dict)
        for event_id in reference.get("execution_event_ids", []) if isinstance(event_id, str)
    }
    operation_id = evidence_record.get("operation_id")
    operation = next((
        item for item in state.get("operation_history", [])
        if isinstance(item, dict) and item.get("operation_id") == operation_id
    ), {})
    receipts = [
        receipt for receipt in operation.get("test_execution_events", [])
        if isinstance(receipt, dict) and receipt.get("event_id") in referenced_events
    ]
    ledger = approval.get("ledger") or {}
    relevant_ids = {
        item.get("id") for item in ledger.get("commitments", [])
        if isinstance(item, dict) and item.get("class") in REQUIRED_CLASSES
    }
    compact_ledger = [
        {
            key: item.get(key)
            for key in ("id", "class", "statement", "acceptance_condition", "implementation_basis", "relevant_sources", "availability", "verification_scenarios")
        }
        for item in ledger.get("commitments", [])
        if isinstance(item, dict) and item.get("id") in relevant_ids
    ]
    compact_evidence = [
        {
            "commitment_id": item.get("commitment_id"),
            "source_refs": item.get("source_refs", []),
            "validation_refs": item.get("validation_refs", []),
            "disposition": item.get("disposition"),
            "reason": item.get("reason"),
        }
        for item in (evidence or {}).get("commitments", [])
        if isinstance(item, dict) and item.get("commitment_id") in relevant_ids
    ]
    return json.dumps({
        "approved_ledger_sha256": approval.get("ledger_sha256"),
        "commitments_requiring_assessment": compact_ledger,
        "codex_evidence_references": compact_evidence,
        "codex_evidence_error": evidence_record.get("error"),
        "execution_operation_id": operation_id,
        "execution_target_fingerprint_before": operation.get("target_fingerprint_before"),
        "execution_target_fingerprint_after": operation.get("target_fingerprint_after"),
        "execution_target_fingerprint": operation.get("target_fingerprint"),
        "observed_test_execution_receipts": receipts,
    }, sort_keys=True, ensure_ascii=False, indent=2)

def commitment_validation_baseline(state, workflow_id: str) -> str:
    """Expose non-sensitive initial path and content hashes for scope checks."""
    preexisting = state.get("preexisting_paths", [])
    paths = sorted({
        path for path in preexisting
        if isinstance(path, str)
        and path != _workspace.WORKFLOW_ROOT
        and not path.startswith(_workspace.WORKFLOW_ROOT + "/")
        and not _workspace.fingerprint_path_is_sensitive(path)
    }) if isinstance(preexisting, list) else []
    path_states: dict[str, dict[str, object]] = {}
    snapshot = state.get("baseline_snapshot")
    execution = state.get("execution_fingerprint")
    initial_target = (
        execution.get("initial", {}).get("target", {}) if isinstance(execution, dict) else {}
    )
    if isinstance(snapshot, dict) and isinstance(snapshot.get("entries"), list):
        snapshot_body = {key: value for key, value in snapshot.items() if key != "sha256"}
        expected_snapshot_sha = hashlib.sha256(json.dumps(
            snapshot_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        if (snapshot.get("sha256") == expected_snapshot_sha
                and snapshot.get("baseline_fingerprint_sha256")
                == (initial_target.get("working_tree_sha256") if isinstance(initial_target, dict) else None)):
            for entry in snapshot["entries"]:
                if not isinstance(entry, dict):
                    continue
                path = entry.get("path")
                kind = entry.get("kind")
                mode = entry.get("mode")
                digest = entry.get("sha256")
                if (path not in paths or kind not in {"file", "symlink"}
                        or type(mode) is not int or not isinstance(digest, str)
                        or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                    continue
                path_states[path] = {"kind": kind, "mode": mode, "sha256": digest}
    return json.dumps({
        "baseline_commit": state.get("baseline_commit"),
        "preexisting_non_sensitive_paths": paths,
        "preexisting_path_states": path_states,
        "preexisting_snapshot_complete": set(path_states) == set(paths),
        "workflow_artifact_prefix": f"{_workspace.WORKFLOW_ROOT}/{workflow_id}/",
    }, ensure_ascii=False, sort_keys=True, indent=2)

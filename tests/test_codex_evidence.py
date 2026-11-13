from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from workflow import codex_evidence  # noqa: E402


class CodexEvidenceTests(unittest.TestCase):
    def test_malformed_and_incomplete_jsonl_are_counted_without_losing_valid_events(self):
        stdout = (
            b'{"type":"turn.started"}\n'
            b'{"type":\n'
            b'\xff\n'
            b'{"type":"future.event"}\n'
        )

        evidence = codex_evidence._codex_jsonl_evidence(stdout)

        self.assertEqual(evidence["jsonl_event_coverage"], "partial")
        self.assertEqual(evidence["jsonl_event_count"], 2)
        self.assertEqual(evidence["jsonl_parse_error_count"], 2)
        self.assertEqual(evidence["jsonl_unrecognized_event_count"], 1)
        self.assertEqual(evidence["codex_cli_event_counts"], {"turn.started": 1})

    def test_output_bytes_preserves_bytes_and_encodes_strings(self):
        self.assertEqual(codex_evidence._output_bytes(b"raw"), (b"raw", "bytes"))
        self.assertEqual(codex_evidence._output_bytes("café"), ("café".encode("utf-8"), "utf8_encoded_text"))
        self.assertEqual(codex_evidence._output_bytes(None), (None, "unavailable"))

    def test_empty_and_unavailable_output_keep_distinct_coverage(self):
        empty = codex_evidence._codex_jsonl_evidence(b"")
        unavailable = codex_evidence._codex_jsonl_evidence(None)

        self.assertEqual(empty["jsonl_event_coverage"], "empty")
        self.assertEqual(empty["tool_event_coverage"], "no_tool_events")
        self.assertEqual(empty["jsonl_event_count"], 0)
        self.assertEqual(unavailable["jsonl_event_coverage"], "unavailable")
        self.assertEqual(unavailable["tool_event_coverage"], "unavailable")

        stream = codex_evidence._codex_stream_evidence(
            b"", b"", stdout_status="complete", stderr_status="complete",
            stdout_truncated=False, stderr_truncated=False,
        )
        self.assertEqual(stream["stdout_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(stream["stderr_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(stream["stdout_bytes"], 0)
        self.assertEqual(stream["stderr_bytes"], 0)
        self.assertEqual(stream["jsonl_event_coverage"], "empty")

    def test_jsonl_agent_message_uses_last_explicit_message(self):
        stdout = "\n".join((
            "not json",
            json.dumps({"type": "message", "message": "First"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": " Final "}}),
        ))

        self.assertEqual(codex_evidence._json_line_message(stdout), "Final")

    def test_evidence_extraction_preserves_tools_usage_and_model(self):
        events = [
            {"type": "thread.started"},
            {"type": "item.started", "item": {"type": "mcp_tool_call", "tool_name": "repo.search"}},
            {"type": "item.completed", "item": {
                "type": "mcp_tool_call", "tool_name": "repo.search", "status": "succeeded",
            }},
            {"type": "turn.completed", "usage": {
                "input_tokens": 12, "output_tokens": 4, "untrusted_field": 100,
            }, "model": "codex-test-model"},
        ]
        stdout = b"\n".join(json.dumps(event).encode("utf-8") for event in events)

        evidence = codex_evidence._codex_stream_evidence(
            stdout, b"diagnostic", stdout_status="complete", stderr_status="complete",
            stdout_truncated=False, stderr_truncated=False,
        )

        self.assertEqual(evidence["stdout_sha256"], hashlib.sha256(stdout).hexdigest())
        self.assertEqual(evidence["stdout_bytes"], len(stdout))
        self.assertEqual(evidence["jsonl_event_coverage"], "parsed")
        self.assertEqual(evidence["jsonl_event_count"], 4)
        self.assertEqual(evidence["codex_cli_turn_status"], "completed")
        self.assertEqual(evidence["reported_usage"], {"input_tokens": 12, "output_tokens": 4})
        self.assertEqual(evidence["usage_source"], "codex_jsonl_turn_completed")
        self.assertEqual(evidence["reported_model"], "codex-test-model")
        tool = evidence["tool_invocations"][0]
        self.assertEqual(tool["item_type"], "mcp_tool_call")
        self.assertEqual(tool["tool_names"], ["repo.search"])
        self.assertEqual(tool["started_events"], 1)
        self.assertEqual(tool["completed_events"], 1)
        self.assertEqual(tool["success_reported_events"], 1)

    def test_langfuse_metadata_is_bounded_and_normalizes_values(self):
        operation = {
            "operation_id": "operation-1",
            "stage": "implementation",
            "status": "completed",
            "finished_at": "saved-finish-time",
            "duration_ms": 25,
            "stdout_sha256": "output-hash",
            "tool_invocations": [
                {
                    "item_type": "mcp_tool_call",
                    "tool_names": ["repo.search", "repo.search", None],
                    "tool_names_truncated": True,
                    "started_events": 2,
                    "completed_events": 1,
                    "success_reported_events": 1,
                    "failure_reported_events": 0,
                    "outcome_unreported_events": 0,
                },
                {
                    "item_type": "command_execution",
                    "tool_names": ["shell"],
                    "started_events": True,
                    "completed_events": 1,
                    "success_reported_events": 0,
                    "failure_reported_events": 1,
                    "outcome_unreported_events": 0,
                },
            ],
            "reported_usage": {"input_tokens": 8, "output_tokens": 3, "secret": "omit"},
            "private_prompt": "must not be exported",
        }

        metadata = codex_evidence._codex_langfuse_metadata(
            operation, status="interrupted", finished_at="override-finish-time",
        )

        self.assertEqual(metadata["status"], "interrupted")
        self.assertEqual(metadata["finished_at"], "override-finish-time")
        self.assertEqual(metadata["tool_item_types"], ["command_execution", "mcp_tool_call"])
        self.assertEqual(metadata["tool_names"], ["repo.search", "shell"])
        self.assertEqual(metadata["tool_names_truncated"], "true")
        self.assertEqual(metadata["tool_invocation_count"], 2)
        self.assertEqual(metadata["tool_completed_count"], 2)
        self.assertEqual(metadata["tool_success_count"], 1)
        self.assertEqual(metadata["tool_failure_count"], 1)
        self.assertEqual(metadata["input_tokens"], 8)
        self.assertEqual(metadata["output_tokens"], 3)
        self.assertNotIn("secret", metadata)
        self.assertNotIn("private_prompt", metadata)
        self.assertNotIn("stdout_capture_status", metadata)


if __name__ == "__main__":
    unittest.main()

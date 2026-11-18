from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from workflow.process_control import process_group_options, process_presence, terminate_process_group


class ProcessControlTests(unittest.TestCase):
    def test_process_presence_treats_a_live_pid_as_unverified(self):
        with patch("workflow.process_control.os.kill") as kill:
            self.assertEqual(process_presence(123), "present_unverified")
            kill.assert_called_once_with(123, 0)

    def test_process_presence_distinguishes_missing_permission_and_invalid_ids(self):
        with patch("workflow.process_control.os.kill", side_effect=ProcessLookupError):
            self.assertEqual(process_presence(123), "not_running")
        with patch("workflow.process_control.os.kill", side_effect=PermissionError):
            self.assertEqual(process_presence(123), "present_unverified")
        with patch("workflow.process_control.os.kill", side_effect=OSError):
            self.assertEqual(process_presence(123), "unknown")
        self.assertEqual(process_presence(True), "not_recorded")
        self.assertEqual(process_presence(None), "not_recorded")

    def test_provider_starts_in_a_separate_process_group(self):
        options = process_group_options()
        if os.name == "nt":
            self.assertIn("creationflags", options)
        else:
            self.assertEqual(options, {"start_new_session": True})

    @unittest.skipIf(os.name == "nt", "POSIX process-group descendant signaling only")
    def test_timeout_cancellation_terminates_child_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready_path = Path(temporary, "child-ready")
            terminated_path = Path(temporary, "child-terminated")
            child_source = (
                "import pathlib, signal, sys, time\n"
                "ready, terminated = map(pathlib.Path, sys.argv[1:])\n"
                "signal.signal(signal.SIGTERM, lambda *_: (terminated.write_text('yes'), sys.exit(0)))\n"
                "ready.write_text('ready')\n"
                "time.sleep(30)\n"
            )
            parent_source = (
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])\n"
                "time.sleep(30)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", parent_source, child_source, str(ready_path), str(terminated_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **process_group_options(),
            )
            deadline = time.monotonic() + 5
            while not ready_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready_path.exists(), "child process did not become ready")

            terminate_process_group(process, grace_seconds=0.2)

            self.assertIsNotNone(process.poll())
            self.assertTrue(terminated_path.exists(), "child process did not receive group termination")


if __name__ == "__main__":
    unittest.main()

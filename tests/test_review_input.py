from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import orchestrator  # noqa: E402
from workflow import review_input  # noqa: E402
from workflow import workspace  # noqa: E402


def git(target: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(target), *args], text=True).strip()


class ReviewInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-input-tests-")
        self.root = Path(self.temp.name)
        self.target = self.root / "target"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.target)], check=True)
        git(self.target, "config", "user.name", "Review Input Test")
        git(self.target, "config", "user.email", "review-input@example.invalid")
        (self.target / "README.md").write_text("baseline\n", encoding="utf-8")
        git(self.target, "add", "README.md")
        git(self.target, "commit", "-q", "-m", "baseline")
        self.baseline = git(self.target, "rev-parse", "HEAD")

    def tearDown(self):
        self.temp.cleanup()

    def add_baseline_files(self, files: dict[str, bytes | str]) -> None:
        for relative, content in files.items():
            path = self.target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
        git(self.target, "add", *files)
        git(self.target, "commit", "-q", "-m", "baseline files")
        self.baseline = git(self.target, "rev-parse", "HEAD")

    def build(self, *, preexisting_paths=(), redactor=None):
        return review_input.build_review_diff(
            self.target, self.baseline, preexisting_paths, redactor=redactor,
        )

    def test_tracks_added_modified_deleted_and_untracked_files_in_stable_complete_order(self):
        self.add_baseline_files({"modified.py": "old = 1\n", "deleted.py": "remove = True\n"})
        (self.target / "modified.py").write_text("new = 2\n", encoding="utf-8")
        (self.target / "deleted.py").unlink()
        (self.target / "added.py").write_text("added = 3\n", encoding="utf-8")
        git(self.target, "add", "added.py")
        (self.target / "untracked.py").write_text("untracked = 4\n", encoding="utf-8")

        packet = self.build()
        expected = ("added.py", "deleted.py", "modified.py", "untracked.py")

        self.assertEqual(packet.included_paths, expected)
        self.assertEqual(tuple(path for path, _patch in packet.file_patches), expected)
        self.assertEqual(packet.excluded_files, ())
        patches = dict(packet.file_patches)
        self.assertIn("new file mode", patches["added.py"])
        self.assertIn("deleted file mode", patches["deleted.py"])
        self.assertIn("-old = 1", patches["modified.py"])
        self.assertIn("+new = 2", patches["modified.py"])
        self.assertIn("+untracked = 4", patches["untracked.py"])

        changed = set(packet.included_paths) | {path for path, _reason in packet.excluded_files}
        self.assertEqual(changed, set(expected))

    def test_preexisting_sensitive_metadata_binary_and_redaction_filters_are_preserved(self):
        self.add_baseline_files({"src/preexisting.py": "before = True\n"})
        (self.target / "src" / "preexisting.py").write_text("after = True\n", encoding="utf-8")
        (self.target / ".env").write_text("PASSWORD=private-value\n", encoding="utf-8")
        (self.target / ".DS_Store").write_bytes(b"metadata")
        binary = self.target / "assets" / "image.png"
        binary.parent.mkdir()
        binary.write_bytes(b"\x89PNG\r\n\x1a\nnot-text")
        secret = "ghp_123456789012345678901234"
        (self.target / "src" / "visible.py").write_text(f'TOKEN = "{secret}"\n', encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("outside data\n", encoding="utf-8")
        (self.target / "src" / "external.txt").symlink_to(outside)

        packet = self.build(
            preexisting_paths=["src/preexisting.py"], redactor=orchestrator.redact,
        )
        exclusions = dict(packet.excluded_files)

        self.assertEqual(
            exclusions["src/preexisting.py"], "pre-existing target change is protected from review",
        )
        self.assertEqual(exclusions[".env"], "sensitive path is excluded from external review")
        self.assertEqual(exclusions[".DS_Store"], "operating-system metadata")
        self.assertIn("binary file signature", exclusions["assets/image.png"])
        self.assertIn("src/external.txt", exclusions)
        self.assertEqual(packet.included_paths, ("src/visible.py",))
        self.assertIn("[REDACTED]", packet.text)
        self.assertNotIn(secret, packet.text)
        self.assertNotIn("private-value", packet.text)

        changed = set(packet.included_paths) | {path for path, _reason in packet.excluded_files}
        self.assertEqual(changed, {
            "src/preexisting.py", ".env", ".DS_Store", "assets/image.png", "src/visible.py",
            "src/external.txt",
        })

    def test_large_text_file_is_kept_whole_for_campaign_payload_partitioning(self):
        content = "value = 1\n" * 3_000
        (self.target / "large.py").write_text(content, encoding="utf-8")

        packet = self.build()

        self.assertEqual(packet.included_paths, ("large.py",))
        self.assertEqual(packet.excluded_files, ())
        self.assertGreater(len(dict(packet.file_patches)["large.py"]), 20_000)
        self.assertIn("+value = 1", dict(packet.file_patches)["large.py"])

    def test_orchestrator_wrapper_matches_module_and_preserves_error_contract(self):
        self.add_baseline_files({"before.py": "old = True\n"})
        (self.target / "before.py").write_text("new = True\n", encoding="utf-8")
        (self.target / "new.py").write_text("fresh = True\n", encoding="utf-8")
        state = {"baseline_commit": self.baseline, "preexisting_paths": []}

        direct = review_input.build_review_diff(
            self.target, self.baseline, [], redactor=orchestrator.redact,
        )
        through_wrapper = orchestrator._review_diff(state, self.target)
        self.assertEqual(through_wrapper, direct)
        self.assertIs(orchestrator._baseline_blob, review_input._baseline_blob)
        self.assertIs(orchestrator._binary_content_reason, review_input._binary_content_reason)
        self.assertIs(orchestrator._os_metadata_reason, review_input._os_metadata_reason)
        self.assertIs(orchestrator.ReviewDiff, review_input.ReviewDiff)

        invalid_target = self.root / "not-a-git-repository"
        invalid_target.mkdir()
        with self.assertRaises(workspace.WorkspaceError) as direct_error:
            review_input.build_review_diff(invalid_target, self.baseline, [])
        with self.assertRaises(orchestrator.WorkflowError) as wrapper_error:
            orchestrator._review_diff(state, invalid_target)
        self.assertEqual(str(wrapper_error.exception), str(direct_error.exception))


if __name__ == "__main__":
    unittest.main()

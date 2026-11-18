from __future__ import annotations

import subprocess
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import orchestrator  # noqa: E402
from workflow import review_input  # noqa: E402
from workflow import review as review_contracts  # noqa: E402
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

    def capture_snapshot(self, paths):
        snapshot = workspace.capture_preexisting_snapshot(self.target, paths)
        snapshot["baseline_fingerprint_sha256"] = "test-initial-fingerprint"
        body = {key: value for key, value in snapshot.items() if key != "sha256"}
        snapshot["sha256"] = hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return snapshot

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

    def test_workflow_artifacts_do_not_change_review_hash_or_hide_adjacent_changes(self):
        self.add_baseline_files({"src/app.py": "value = 0\n"})
        snapshot = self.capture_snapshot([])
        (self.target / "src/app.py").write_text("value = 1\n", encoding="utf-8")
        (self.target / ".engineering" / "notes.md").parent.mkdir(parents=True)
        (self.target / ".engineering" / "notes.md").write_text("application note\n", encoding="utf-8")

        workflow = self.target / workspace.WORKFLOW_ROOT / "workflow-1"
        (workflow / "verification").mkdir(parents=True)
        (workflow / "proposal.md").write_text("proposal\n", encoding="utf-8")
        (workflow / "state.json").write_text("initial state\n", encoding="utf-8")
        (workflow / "verification" / "test_scope.py").write_text("pass\n", encoding="utf-8")
        registry = self.target / workspace.WORKFLOW_ROOT / "github-registry.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("registry\n", encoding="utf-8")

        before_delivery = review_input.build_review_diff(
            self.target, self.baseline, [], baseline_snapshot=snapshot,
            review_scope_paths={"src/app.py"},
        )
        before_hash = review_contracts.ReviewCampaign._campaign_source_hash(before_delivery)

        (workflow / "delivery.md").write_text("delivery report\n", encoding="utf-8")
        (workflow / "state.json").write_text("updated state\n", encoding="utf-8")
        (self.target / workspace.WORKFLOW_ROOT / "another-workflow").mkdir()
        (self.target / workspace.WORKFLOW_ROOT / "another-workflow" / "state.json").write_text(
            "other workflow state\n", encoding="utf-8",
        )

        after_delivery = review_input.build_review_diff(
            self.target, self.baseline, [], baseline_snapshot=snapshot,
            review_scope_paths={"src/app.py"},
        )
        after_hash = review_contracts.ReviewCampaign._campaign_source_hash(after_delivery)

        self.assertEqual(before_delivery.included_paths, (".engineering/notes.md", "src/app.py"))
        self.assertEqual(after_delivery.included_paths, before_delivery.included_paths)
        self.assertEqual(after_delivery.workflow_paths, before_delivery.workflow_paths)
        self.assertEqual(after_delivery.excluded_files, ())
        self.assertEqual(after_hash, before_hash)

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

    def test_baseline_snapshot_includes_staged_unstaged_and_untracked_context_but_attributes_only_new_deltas(self):
        self.add_baseline_files({
            "src/staged.py": "committed version = 0\n",
            "src/unstaged.py": "unstaged source before start\n",
        })
        (self.target / "src/staged.py").write_text("staged version = 1\n", encoding="utf-8")
        git(self.target, "add", "src/staged.py")
        (self.target / "src/unstaged.py").write_text("unstaged source before start\nlocal note\n", encoding="utf-8")
        (self.target / "docs").mkdir()
        (self.target / "docs/context.md").write_text("untracked context before start\n", encoding="utf-8")
        preexisting = workspace.protected_paths(self.target, self.baseline)
        snapshot = self.capture_snapshot(preexisting)

        (self.target / "src/staged.py").write_text("workflow version = 2\n", encoding="utf-8")
        (self.target / "src/feature.py").write_text("def added_feature():\n    return True\n", encoding="utf-8")
        packet = review_input.build_review_diff(
            self.target, self.baseline, preexisting, baseline_snapshot=snapshot,
            review_scope_paths={"src/staged.py", "src/unstaged.py", "docs/context.md"},
        )

        self.assertEqual(packet.workflow_paths, ("src/feature.py", "src/staged.py"))
        self.assertEqual(packet.included_paths, packet.workflow_paths)
        staged_patch = dict(packet.file_patches)["src/staged.py"]
        self.assertIn("-staged version = 1", staged_patch)
        self.assertIn("+workflow version = 2", staged_patch)
        self.assertNotIn("-committed version = 0", staged_patch)
        context = {path: (origin, before, after) for path, origin, before, after in packet.context_files}
        self.assertEqual(context["src/staged.py"][0], "pre-existing target state")
        self.assertIn("staged version = 1", context["src/staged.py"][1])
        self.assertIn("workflow version = 2", context["src/staged.py"][2])
        self.assertEqual(context["src/unstaged.py"][1], context["src/unstaged.py"][2])
        self.assertIn("local note", context["src/unstaged.py"][1])
        self.assertIn("untracked context before start", context["docs/context.md"][1])
        self.assertNotIn("src/unstaged.py", packet.workflow_paths)
        self.assertNotIn("docs/context.md", packet.workflow_paths)

    def test_changed_preexisting_untracked_file_uses_its_saved_content_as_the_patch_base(self):
        (self.target / "notes.md").write_text("pre-existing note\n", encoding="utf-8")
        preexisting = workspace.protected_paths(self.target, self.baseline)
        snapshot = self.capture_snapshot(preexisting)
        (self.target / "notes.md").write_text("pre-existing note\nworkflow addition\n", encoding="utf-8")

        packet = review_input.build_review_diff(
            self.target, self.baseline, preexisting, baseline_snapshot=snapshot,
            review_scope_paths={"notes.md"},
        )

        self.assertEqual(packet.workflow_paths, ("notes.md",))
        self.assertEqual(packet.included_paths, ("notes.md",))
        self.assertIn("+workflow addition", packet.text)
        self.assertNotIn("-pre-existing note", packet.text)

    def test_mutated_baseline_snapshot_fails_closed(self):
        (self.target / "src").mkdir()
        (self.target / "src/existing.py").write_text("baseline = True\n", encoding="utf-8")
        preexisting = workspace.protected_paths(self.target, self.baseline)
        snapshot = self.capture_snapshot(preexisting)
        snapshot["entries"][0]["content_base64"] = "Y29ycnVwdGVk"

        with self.assertRaisesRegex(review_input.ReviewInputError, "integrity"):
            review_input.build_review_diff(self.target, self.baseline, preexisting, baseline_snapshot=snapshot)

    def test_review_scope_adds_direct_python_dependency_without_scanning_repository(self):
        self.add_baseline_files({
            "src/pkg/service.py": "from .store import Store\n\ndef process(): return Store()\n",
            "src/pkg/store.py": "class Store: pass\n",
        })
        snapshot = self.capture_snapshot([])

        packet = review_input.build_review_diff(
            self.target, self.baseline, [], baseline_snapshot=snapshot,
            review_scope_paths={"src/pkg/service.py"},
        )

        self.assertEqual({path for path, *_ in packet.context_files}, {"src/pkg/service.py", "src/pkg/store.py"})

    def test_index_only_mutation_is_excluded_as_ambiguous_and_not_attributed(self):
        self.add_baseline_files({"src/staged.py": "committed baseline\n"})
        source = self.target / "src/staged.py"
        source.write_text("staged baseline\n", encoding="utf-8")
        git(self.target, "add", "src/staged.py")
        source.write_text("worktree baseline\n", encoding="utf-8")
        preexisting = workspace.protected_paths(self.target, self.baseline)
        snapshot = self.capture_snapshot(preexisting)

        new_blob = subprocess.check_output(
            ["git", "-C", str(self.target), "hash-object", "-w", "--stdin"],
            input=b"index-only change\n",
        ).decode().strip()
        git(self.target, "update-index", "--cacheinfo", f"100644,{new_blob},src/staged.py")
        packet = review_input.build_review_diff(
            self.target, self.baseline, preexisting, baseline_snapshot=snapshot,
        )

        self.assertEqual(packet.workflow_paths, ())
        self.assertNotIn("src/staged.py", packet.included_paths)
        self.assertIn("index changed without a worktree source delta", dict(packet.excluded_files)["src/staged.py"])

    def test_mode_only_change_is_reviewed_and_attributed(self):
        self.add_baseline_files({"src/runner.py": "print('run')\n"})
        snapshot = self.capture_snapshot([])
        path = self.target / "src/runner.py"
        path.chmod(0o755)

        packet = review_input.build_review_diff(
            self.target, self.baseline, [], baseline_snapshot=snapshot,
        )

        self.assertEqual(packet.workflow_paths, ("src/runner.py",))
        self.assertIn("old mode 100644", packet.text)
        self.assertIn("new mode 100755", packet.text)

    def test_correction_review_keeps_initial_and_correction_deltas_separate(self):
        self.add_baseline_files({"src/service.py": "def process(): return 'baseline'\n"})
        baseline_snapshot = self.capture_snapshot([])
        (self.target / "src/service.py").write_text("def process(): return 'implemented'\n", encoding="utf-8")
        implementation_snapshot = workspace.capture_preexisting_snapshot(self.target, ["src/service.py"])
        implementation_body = {key: value for key, value in implementation_snapshot.items() if key != "sha256"}
        implementation_snapshot["source_patch_sha256"] = "initial-review-hash"
        implementation_snapshot["target_fingerprint"] = {"status": "captured", "working_tree_sha256": "tree-1"}
        implementation_body = {key: value for key, value in implementation_snapshot.items() if key != "sha256"}
        implementation_snapshot["sha256"] = hashlib.sha256(json.dumps(
            implementation_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        (self.target / "src/service.py").write_text("def process(): return 'corrected'\n", encoding="utf-8")

        packet = review_input.build_review_diff(
            self.target, self.baseline, [], baseline_snapshot=baseline_snapshot,
            review_scope_paths={"src/service.py"}, implementation_snapshot=implementation_snapshot,
        )

        deltas = dict(packet.operation_diffs)
        self.assertIn("+def process(): return 'implemented'", deltas["Original implementation delta"])
        self.assertIn("-def process(): return 'baseline'", deltas["Original implementation delta"])
        self.assertIn("+def process(): return 'corrected'", deltas["Correction delta since initial review"])
        self.assertIn("-def process(): return 'implemented'", deltas["Correction delta since initial review"])


if __name__ == "__main__":
    unittest.main()

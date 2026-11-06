import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "harness"))
from safe_writer import SafeWriteError, create_directories, create_file, replace_file


def clean_git_env():
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


class SafeWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="safe-writer-")
        self.root = Path(self.temp.name) / "target"
        self.root.mkdir()
        (self.root / "src").mkdir()
        self.config_path = Path(self.temp.name) / "preflight.json"
        self.write_config({"src"})
        self.ready = {"overall_result": "READY", "target_path": str(self.root.resolve())}
        self.preflight_patch = patch("safe_writer._run_preflight", return_value=self.ready)
        self.preflight_patch.start()

    def tearDown(self):
        self.preflight_patch.stop()
        self.temp.cleanup()

    def write_config(self, paths):
        self.config_path.write_text(json.dumps({
            "target_path": str(self.root.resolve()),
            "included_paths": sorted(paths),
            "code_paths": sorted(paths),
            "artifact_paths": [],
            "excluded_paths": [],
            "authorized_working_directories": [str(self.root.resolve())],
        }), encoding="utf-8")

    @staticmethod
    def digest(data):
        return hashlib.sha256(data).hexdigest()

    def replace(self, path, old, new):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(old)
        result = replace_file(self.root, self.config_path, path, new, self.digest(old))
        return destination, result

    def test_replaces_existing_file_atomically_and_reports_evidence(self):
        destination, result = self.replace("src/app.py", b"old bytes\n", b"new bytes\n")
        self.assertEqual(destination.read_bytes(), b"new bytes\n")
        self.assertEqual(result["operation"], "replace")
        self.assertEqual(result["before_sha256"], self.digest(b"old bytes\n"))
        self.assertEqual(result["after_sha256"], self.digest(b"new bytes\n"))
        self.assertEqual(result["link_count"], 1)
        self.assertNotEqual(result["before_inode"], result["after_inode"])

    def test_replacement_preserves_posix_mode(self):
        destination = self.root / "src/mode.py"
        destination.write_bytes(b"old\n")
        destination.chmod(0o640)
        result = replace_file(self.root, self.config_path, "src/mode.py", b"new\n", self.digest(b"old\n"))
        self.assertEqual(result["mode"], 0o640)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o640)

    def test_creates_new_file_and_never_overwrites_existing_destination(self):
        created = create_file(self.root, self.config_path, "src/new.txt", b"new\n")
        self.assertEqual((self.root / "src/new.txt").read_bytes(), b"new\n")
        self.assertEqual(created["operation"], "create")
        with self.assertRaises(FileExistsError):
            create_file(self.root, self.config_path, "src/new.txt", b"replacement\n")
        self.assertEqual((self.root / "src/new.txt").read_bytes(), b"new\n")

    def test_creates_one_directory_and_reports_identity_evidence(self):
        result = create_directories(self.root, self.config_path, "src/new-dir")
        created = self.root / "src/new-dir"
        info = created.stat()
        self.assertTrue(created.is_dir())
        self.assertEqual(result["operation"], "mkdir")
        self.assertEqual(result["path"], "src/new-dir")
        self.assertEqual(result["created_directories"], [{
            "path": "src/new-dir", "device": info.st_dev, "inode": info.st_ino,
            "mode": info.st_mode & 0o7777, "owner_uid": info.st_uid, "owner_gid": info.st_gid,
        }])
        self.assertEqual((result["device"], result["inode"]), (info.st_dev, info.st_ino))
        json.dumps(result)

    def test_creates_nested_directories_one_component_at_a_time(self):
        result = create_directories(self.root, self.config_path, "src/new/types")
        self.assertTrue((self.root / "src/new/types").is_dir())
        self.assertEqual([entry["path"] for entry in result["created_directories"]],
                         ["src/new", "src/new/types"])
        self.assertTrue(all(entry["inode"] > 0 for entry in result["created_directories"]))

    def test_existing_requested_directory_is_never_reused_or_modified(self):
        destination = self.root / "src/existing"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        before = destination.stat()
        with self.assertRaises(FileExistsError):
            create_directories(self.root, self.config_path, "src/existing")
        after = destination.stat()
        self.assertEqual((after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_gid),
                         (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_gid))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
        self.assertEqual(sorted(item.name for item in destination.iterdir()), ["keep.txt"])

    def test_symlinks_in_existing_parent_and_requested_destination_are_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / "src/linked-parent").symlink_to(outside, target_is_directory=True)
        (self.root / "src/linked-destination").symlink_to(outside, target_is_directory=True)
        for relative in ("src/linked-parent/child", "src/linked-destination"):
            with self.subTest(relative=relative):
                with self.assertRaises((OSError, SafeWriteError)):
                    create_directories(self.root, self.config_path, relative)
        self.assertEqual(list(outside.iterdir()), [])

    def test_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(SafeWriteError, "not normalized"):
            create_directories(self.root, self.config_path, "src/../../outside")
        self.assertFalse((Path(self.temp.name) / "outside").exists())

    def test_each_missing_directory_component_requires_its_own_scope(self):
        self.write_config({"src/nested/leaf"})
        with self.assertRaisesRegex(SafeWriteError, "outside included_paths"):
            create_directories(self.root, self.config_path, "src/nested/leaf")
        self.assertFalse((self.root / "src/nested").exists())

    def test_unexpected_mkdir_error_fails_without_creating_destination(self):
        with patch("safe_writer.os.mkdir", side_effect=PermissionError("simulated mkdir denial")):
            with self.assertRaisesRegex(PermissionError, "simulated mkdir denial"):
                create_directories(self.root, self.config_path, "src/new-dir")
        self.assertFalse((self.root / "src/new-dir").exists())

    def test_later_mkdir_error_keeps_and_reports_prior_created_directories(self):
        original_mkdir = os.mkdir
        calls = 0

        def fail_second(component, mode=0o777, *, dir_fd=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("simulated second mkdir denial")
            return original_mkdir(component, mode, dir_fd=dir_fd)

        with patch("safe_writer.os.mkdir", side_effect=fail_second):
            with self.assertRaisesRegex(SafeWriteError, "partial result") as raised:
                create_directories(self.root, self.config_path, "src/new/types")
        self.assertTrue((self.root / "src/new").is_dir())
        self.assertFalse((self.root / "src/new/types").exists())
        evidence = raised.exception.evidence
        self.assertEqual(evidence["operation"], "mkdir")
        self.assertEqual([entry["path"] for entry in evidence["created_directories"]], ["src/new"])
        json.dumps(evidence)

    def test_existing_directory_metadata_and_contents_remain_intact_when_child_is_added(self):
        parent = self.root / "src"
        sentinel = parent / "existing.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        before = parent.stat()
        create_directories(self.root, self.config_path, "src/child")
        after = parent.stat()
        self.assertEqual((after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_gid),
                         (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_gid))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_stale_expected_content_is_rejected_without_change(self):
        destination = self.root / "src/app.py"
        destination.write_bytes(b"current\n")
        with self.assertRaisesRegex(SafeWriteError, "expected baseline hash"):
            replace_file(self.root, self.config_path, "src/app.py", b"replacement\n", self.digest(b"old\n"))
        self.assertEqual(destination.read_bytes(), b"current\n")

    def test_identity_change_before_final_check_is_detected_and_preserved(self):
        destination = self.root / "src/app.py"
        destination.write_bytes(b"old\n")
        original_stat = __import__("safe_writer")._stat_destination
        calls = 0

        def swap_before_final_check(parent_fd, name):
            nonlocal calls
            calls += 1
            if calls == 2:
                replacement = self.root / "src/concurrent.txt"
                replacement.write_bytes(b"other writer\n")
                os.replace(replacement, destination)
            return original_stat(parent_fd, name)

        with patch("safe_writer._stat_destination", side_effect=swap_before_final_check):
            with self.assertRaisesRegex(SafeWriteError, "changed before replacement"):
                replace_file(self.root, self.config_path, "src/app.py", b"ours\n", self.digest(b"old\n"))
        self.assertEqual(destination.read_bytes(), b"other writer\n")

    def test_in_place_content_change_before_final_check_is_detected(self):
        destination = self.root / "src/app.py"
        destination.write_bytes(b"old\n")
        import safe_writer
        original_hash = safe_writer._sha256_fd
        calls = 0

        def change_before_final_hash(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                destination.write_bytes(b"changed in place\n")
            return original_hash(fd)

        with patch("safe_writer._sha256_fd", side_effect=change_before_final_hash):
            with self.assertRaisesRegex(SafeWriteError, "changed before replacement"):
                replace_file(self.root, self.config_path, "src/app.py", b"ours\n", self.digest(b"old\n"))
        self.assertEqual(destination.read_bytes(), b"changed in place\n")

    def test_symlink_destination_and_parent_are_rejected(self):
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("keep\n", encoding="utf-8")
        (self.root / "src/link.txt").symlink_to(outside)
        with self.assertRaises((OSError, SafeWriteError)):
            create_file(self.root, self.config_path, "src/link.txt", b"bad\n")
        (self.root / "linked-dir").symlink_to(outside.parent, target_is_directory=True)
        self.write_config({"src", "linked-dir"})
        with self.assertRaises((OSError, SafeWriteError)):
            create_file(self.root, self.config_path, "linked-dir/escaped.txt", b"bad\n")
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")

    def test_hard_link_destination_is_rejected(self):
        destination = self.root / "src/app.py"
        destination.write_bytes(b"shared\n")
        alias = self.root / "src/alias.py"
        os.link(destination, alias)
        with self.assertRaisesRegex(SafeWriteError, "single-link"):
            replace_file(self.root, self.config_path, "src/app.py", b"replacement\n", self.digest(b"shared\n"))
        self.assertEqual(destination.read_bytes(), b"shared\n")
        self.assertEqual(alias.read_bytes(), b"shared\n")

    def test_unauthorized_path_and_empty_allowlist_are_rejected(self):
        with self.assertRaisesRegex(SafeWriteError, "outside included_paths"):
            create_file(self.root, self.config_path, "docs/new.md", b"no\n")
        self.write_config(set())
        with self.assertRaisesRegex(SafeWriteError, "missing or inconsistent"):
            create_file(self.root, self.config_path, "src/new.py", b"no\n")
        self.assertFalse((self.root / "src/new.py").exists())

    def test_excluded_destination_is_rejected(self):
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["included_paths"] = ["src"]
        config["code_paths"] = ["src"]
        config["excluded_paths"] = ["src/private"]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(SafeWriteError, "excluded"):
            create_file(self.root, self.config_path, "src/private/key.txt", b"no\n")

    def test_scope_config_cannot_authorize_its_own_replacement(self):
        in_target_config = self.root / "src/scopes.json"
        in_target_config.write_text(self.config_path.read_text(encoding="utf-8"), encoding="utf-8")
        scopes = json.loads(self.config_path.read_text(encoding="utf-8"))
        scopes["included_paths"] = ["src"]
        scopes["code_paths"] = ["src"]
        self.config_path.write_text(json.dumps(scopes), encoding="utf-8")
        in_target_config.write_text(self.config_path.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(SafeWriteError, "cannot be modified"):
            replace_file(self.root, in_target_config, "src/scopes.json", b"{}", self.digest(in_target_config.read_bytes()))

    def test_failed_temp_write_cleans_up_and_preserves_existing_file(self):
        destination = self.root / "src/app.py"
        destination.write_bytes(b"old\n")
        with patch("safe_writer._write_all", side_effect=OSError("simulated write failure")):
            with self.assertRaisesRegex(OSError, "simulated write failure"):
                replace_file(self.root, self.config_path, "src/app.py", b"ours\n", self.digest(b"old\n"))
        self.assertEqual(destination.read_bytes(), b"old\n")
        self.assertEqual([item.name for item in (self.root / "src").iterdir()], ["app.py"])

    def test_cleanup_error_preserves_original_write_error(self):
        destination = self.root / "src/app.py"
        destination.write_bytes(b"old\n")
        with patch("safe_writer._write_all", side_effect=SafeWriteError("original write failure")), \
                patch("safe_writer.os.unlink", side_effect=PermissionError("cleanup failure")):
            with self.assertRaisesRegex(SafeWriteError, "original write failure.*cleanup failure"):
                replace_file(self.root, self.config_path, "src/app.py", b"ours\n", self.digest(b"old\n"))
        self.assertEqual(destination.read_bytes(), b"old\n")

    def test_unrelated_preexisting_user_changes_are_preserved(self):
        destination = self.root / "src/app.py"
        unrelated = self.root / "src/user-work.txt"
        destination.write_bytes(b"old\n")
        unrelated.write_bytes(b"pre-existing edit\n")
        self.replace("src/app.py", b"old\n", b"new\n")
        self.assertEqual(unrelated.read_bytes(), b"pre-existing edit\n")

    def test_non_ready_preflight_blocks_without_writing(self):
        destination = self.root / "src/new.py"
        with patch("safe_writer._run_preflight", side_effect=SafeWriteError("Fresh target preflight did not return READY; no write was attempted")):
            with self.assertRaisesRegex(SafeWriteError, "did not return READY"):
                create_file(self.root, self.config_path, "src/new.py", b"new\n")
        self.assertFalse(destination.exists())

    def test_actual_preflight_is_required_and_scope_is_revalidated(self):
        repo = Path(self.temp.name) / "preflight-target"
        repo.mkdir()
        (repo / "src").mkdir()
        subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "core.hooksPath=/dev/null",
                        "init", "-q", str(repo)], env=clean_git_env(), check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "add", "README.md"],
                       env=clean_git_env(), check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c",
                        "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-m", "fixture", "-q"], env=clean_git_env(), check=True)
        config_path = Path(self.temp.name) / "actual-preflight.json"
        config_path.write_text(json.dumps({
            "target_path": str(repo.resolve()), "included_paths": ["src"],
            "code_paths": ["src"], "artifact_paths": [], "revision_paths": ["src"],
            "excluded_paths": [], "authorized_working_directories": [str(repo.resolve())],
        }), encoding="utf-8")
        self.preflight_patch.stop()
        try:
            result = create_file(repo, config_path, "src/new.py", b"created\n")
        finally:
            self.preflight_patch.start()
        self.assertEqual(result["preflight"], "READY")
        self.assertEqual((repo / "src/new.py").read_bytes(), b"created\n")


if __name__ == "__main__":
    unittest.main()

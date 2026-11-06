import json
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "harness" / "preflight.py"
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO_ROOT / "harness"))
from preflight import Preflight


def clean_env(extra=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    if extra:
        env.update(extra)
    return env


def run_git(root, *args):
    return subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                           "-C", str(root), *args], env=clean_env(), check=True,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def filesystem_snapshot(root):
    snapshot = {}
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        for name in list(dirs) + files:
            path = Path(current) / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            item = {"type": stat.S_IFMT(info.st_mode), "mode": stat.S_IMODE(info.st_mode),
                    "device": info.st_dev, "inode": info.st_ino, "nlink": info.st_nlink,
                    "mtime_ns": info.st_mtime_ns}
            if stat.S_ISLNK(info.st_mode):
                item["link"] = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[rel] = item
    return snapshot


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="target-preflight-")
        self.base = Path(self.temp.name)
        self.harness = self.base / "harness"
        self.target = self.base / "target"
        self.make_repo(self.harness)
        self.make_repo(self.target)

    def tearDown(self):
        self.temp.cleanup()

    def make_repo(self, root):
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                        "-c", "init.defaultBranch=main", "init", "-q", str(root)],
                       env=clean_env(), check=True)
        run_git(root, "config", "user.name", "Preflight Fixture")
        run_git(root, "config", "user.email", "preflight@example.invalid")
        (root / "README.txt").write_text("fixture\n", encoding="utf-8")
        run_git(root, "add", "README.txt")
        run_git(root, "commit", "-m", "fixture", "-q")

    def add_submodule(self, relative_path):
        source = self.base / ("submodule-source-" + relative_path.replace("/", "-"))
        self.make_repo(source)
        subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                        "-C", str(self.target), "-c", "protocol.file.allow=always",
                        "submodule", "add", "-q", str(source), relative_path], env=clean_env(), check=True)
        run_git(self.target, "add", ".gitmodules", relative_path)
        run_git(self.target, "commit", "-m", "add test submodule", "-q")
        return source, self.target / relative_path

    def invoke(self, target=None, *, harness=None, config=None, extra_env=None, workspace_root=None):
        command = [sys.executable, str(CLI), "--target", str(target or self.target), "--harness-root", str(harness or self.harness)]
        config_path = None
        if config is not None:
            config_path = self.base / "preflight.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            command.extend(["--config", str(config_path)])
        if workspace_root:
            command.extend(["--workspace-root", str(workspace_root)])
        result = subprocess.run(command, env=clean_env(extra_env), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertTrue(result.stdout, result.stderr)
        return result, json.loads(result.stdout)

    @staticmethod
    def status(report, check_id):
        return next(row["status"] for row in report["checks"] if row["check_id"] == check_id)

    def test_valid_independent_target_is_ready_and_unchanged(self):
        before = filesystem_snapshot(self.target)
        result, report = self.invoke()
        after = filesystem_snapshot(self.target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["overall_result"], "READY")
        self.assertEqual(before, after)
        self.assertEqual(self.status(report, "git.isolation"), "PASS")
        self.assertEqual(self.status(report, "scope.validation"), "PASS")
        self.assertEqual(report["scope"]["included_paths"], [])

    def test_nonexistent_target_is_blocked(self):
        _, report = self.invoke(self.base / "missing")
        self.assertEqual(report["overall_result"], "BLOCKED")
        self.assertEqual(self.status(report, "target.path"), "FAIL")
        self.assertIn("invalid_input", report["result_categories"])

    def test_non_git_directory_is_blocked(self):
        plain = self.base / "plain"
        plain.mkdir()
        _, report = self.invoke(plain)
        self.assertEqual(report["overall_result"], "BLOCKED")
        self.assertEqual(self.status(report, "git.identity.target"), "FAIL")

    def test_harness_repository_passed_as_target_is_blocked(self):
        _, report = self.invoke(self.harness)
        self.assertEqual(report["overall_result"], "BLOCKED")
        self.assertEqual(self.status(report, "git.roots"), "FAIL")

    def test_worktree_sharing_harness_common_directory_is_blocked(self):
        worktree = self.base / "linked-worktree"
        run_git(self.harness, "worktree", "add", "-q", "-b", "preflight-worktree", str(worktree))
        _, report = self.invoke(worktree)
        self.assertEqual(report["overall_result"], "BLOCKED")
        self.assertEqual(self.status(report, "git.isolation"), "FAIL")

    def test_independent_linked_worktree_is_accepted(self):
        worktree = self.base / "target-linked-worktree"
        run_git(self.target, "worktree", "add", "-q", "-b", "preflight-worktree", str(worktree))
        result, report = self.invoke(worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["overall_result"], "READY")
        self.assertEqual(self.status(report, "git.identity.target"), "PASS")
        self.assertEqual(self.status(report, "git.isolation"), "PASS")

    def test_shared_hard_linked_git_index_is_blocked(self):
        target_index = self.target / ".git" / "index"
        target_index.unlink()
        os.link(self.harness / ".git" / "index", target_index)
        _, report = self.invoke()
        self.assertEqual(report["overall_result"], "BLOCKED")
        self.assertEqual(self.status(report, "git.isolation"), "FAIL")
        isolation = next(row for row in report["checks"] if row["check_id"] == "git.isolation")
        self.assertTrue(any(item["target_file"].endswith("/.git/index") for item in isolation["evidence"]["shared_file_inodes"]))

    def test_shared_hard_linked_git_config_is_blocked(self):
        target_config = self.target / ".git" / "config"
        target_config.unlink()
        os.link(self.harness / ".git" / "config", target_config)
        _, report = self.invoke()
        self.assertEqual(report["overall_result"], "BLOCKED")
        self.assertEqual(self.status(report, "git.isolation"), "FAIL")
        isolation = next(row for row in report["checks"] if row["check_id"] == "git.isolation")
        self.assertTrue(any(item["target_file"].endswith("/.git/config") for item in isolation["evidence"]["shared_file_inodes"]))

    def test_symlinked_git_metadata_file_is_blocked(self):
        target_index = self.target / ".git" / "index"
        target_index.unlink()
        target_index.symlink_to(self.harness / ".git" / "index")
        _, report = self.invoke()
        self.assertEqual(report["overall_result"], "BLOCKED")
        self.assertEqual(self.status(report, "git.isolation"), "FAIL")
        isolation = next(row for row in report["checks"] if row["check_id"] == "git.isolation")
        self.assertTrue(any(item["path"].endswith("/target/.git/index")
                            for item in isolation["evidence"]["metadata_symlinks"]["target"]))

    def test_shared_object_alternate_is_blocked(self):
        shared = self.base / "shared-clone"
        subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                        "clone", "-q", "--shared", str(self.harness), str(shared)],
                       env=clean_env(), check=True)
        _, report = self.invoke(shared)
        self.assertEqual(report["overall_result"], "BLOCKED")
        self.assertEqual(self.status(report, "git.isolation"), "FAIL")
        self.assertIn("unsafe_condition", report["result_categories"])

    def test_dangerous_inherited_git_environment_is_rejected(self):
        _, report = self.invoke(extra_env={"GIT_DIR": str(self.harness / ".git")})
        self.assertEqual(report["overall_result"], "BLOCKED")
        row = next(row for row in report["checks"] if row["check_id"] == "git.environment")
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["values"]["rejected_names"], ["GIT_DIR"])

    def test_unknown_inherited_git_environment_is_rejected(self):
        _, report = self.invoke(extra_env={"GIT_UNREVIEWED_OVERRIDE": "present"})
        self.assertEqual(report["overall_result"], "BLOCKED")
        row = next(row for row in report["checks"] if row["check_id"] == "git.environment")
        self.assertEqual(row["values"]["unknown_names"], ["GIT_UNREVIEWED_OVERRIDE"])

    def test_local_git_includes_are_rejected_without_following_them(self):
        external = self.base / "external-git-config"
        external.write_text("[core]\nworktree = /outside\n", encoding="utf-8")
        run_git(self.target, "config", "--add", "include.path", str(external))
        _, report = self.invoke()
        self.assertEqual(report["overall_result"], "BLOCKED")
        row = next(row for row in report["checks"] if row["check_id"] == "git.identity.target")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("include directives", row["explanation"])

    def test_tracked_modifications_are_reported_without_change(self):
        (self.target / "README.txt").write_text("modified\n", encoding="utf-8")
        before = run_git(self.target, "status", "--porcelain=v1", "-z").stdout
        _, report = self.invoke()
        after = run_git(self.target, "status", "--porcelain=v1", "-z").stdout
        self.assertEqual(before, after)
        self.assertEqual(report["repository_state"]["tracked_modifications"][0]["path"], "README.txt")

    def test_untracked_files_are_reported(self):
        (self.target / "new.txt").write_text("new\n", encoding="utf-8")
        _, report = self.invoke()
        self.assertIn("new.txt", report["repository_state"]["untracked_files"])

    def test_initialized_submodule_state_is_reported(self):
        _, _ = self.add_submodule("vendor/module")
        _, report = self.invoke()
        self.assertEqual(report["repository_state"]["submodules"][0]["state"], "initialized", report["repository_state"]["submodules"])
        self.assertEqual(report["repository_state"]["submodules"][0]["path"], "vendor/module")

    def assert_submodule_unverified(self, report, expected_symlink):
        submodule = report["repository_state"]["submodules"][0]
        self.assertEqual(submodule["state"], "unverified", submodule)
        self.assertIn("Git was not invoked", submodule["reason"])
        self.assertTrue(submodule["symlink_components"])
        self.assertEqual(expected_symlink, submodule["symlink_components"][0]["path"])
        self.assertNotIn("head", submodule)
        self.assertEqual(self.status(report, "repository.state"), "NEEDS_HUMAN_INPUT")
        self.assertNotEqual(report["overall_result"], "READY")

    def test_submodule_path_symlink_outside_target_is_not_inspected(self):
        source, subpath = self.add_submodule("vendor/module")
        shutil.rmtree(subpath)
        subpath.symlink_to(source, target_is_directory=True)
        _, report = self.invoke()
        self.assertEqual(report["repository_state"]["submodules"][0]["canonical_submodule_path"], str(source.resolve()))
        self.assert_submodule_unverified(report, str(self.target.resolve() / "vendor" / "module"))

    def test_submodule_parent_symlink_outside_target_is_not_inspected(self):
        source, subpath = self.add_submodule("vendor/team/module")
        parent = self.target / "vendor" / "team"
        shutil.rmtree(parent)
        external_parent = self.base / "external-parent"
        external_parent.mkdir()
        (external_parent / "module").symlink_to(source, target_is_directory=True)
        parent.symlink_to(external_parent, target_is_directory=True)
        _, report = self.invoke()
        submodule = report["repository_state"]["submodules"][0]
        self.assertEqual(submodule["canonical_submodule_path"], str(source.resolve()))
        self.assert_submodule_unverified(report, str(self.target.resolve() / "vendor" / "team"))

    def test_path_traversal_scope_is_rejected(self):
        config = {"included_paths": ["../outside"], "code_paths": ["../outside"], "artifact_paths": []}
        _, report = self.invoke(config=config)
        self.assertEqual(report["overall_result"], "BLOCKED")
        self.assertEqual(self.status(report, "scope.validation"), "FAIL")

    def test_absolute_outside_scope_is_rejected(self):
        outside = str(self.base / "outside")
        config = {"included_paths": [outside], "code_paths": [outside], "artifact_paths": []}
        _, report = self.invoke(config=config)
        self.assertEqual(self.status(report, "scope.validation"), "FAIL")

    def test_symlink_in_write_scope_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.target / "linked").symlink_to(outside, target_is_directory=True)
        config = {"included_paths": ["linked/file.txt"], "code_paths": ["linked/file.txt"], "artifact_paths": []}
        _, report = self.invoke(config=config)
        self.assertEqual(self.status(report, "scope.validation"), "FAIL")

    def test_git_metadata_scopes_are_rejected(self):
        for path in (".git/config", ".git/hooks"):
            with self.subTest(path=path):
                config = {"included_paths": [path], "code_paths": [path], "artifact_paths": []}
                _, report = self.invoke(config=config)
                self.assertEqual(report["overall_result"], "BLOCKED")
                self.assertEqual(self.status(report, "scope.validation"), "FAIL")
                scope_check = next(row for row in report["checks"] if row["check_id"] == "scope.validation")
                self.assertIn("Git metadata", scope_check["explanation"])

    def test_nested_repository_scope_and_metadata_are_rejected(self):
        nested = self.target / "nested-repo"
        self.make_repo(nested)
        for path in ("nested-repo/.git/config", "nested-repo/README.txt"):
            with self.subTest(path=path):
                config = {"included_paths": [path], "code_paths": [path], "artifact_paths": []}
                _, report = self.invoke(config=config)
                self.assertEqual(report["overall_result"], "BLOCKED")
                self.assertEqual(self.status(report, "scope.validation"), "FAIL")

    def test_nested_bare_repository_scopes_are_rejected(self):
        bare = self.target / "src" / "internal-repo.git"
        bare.parent.mkdir()
        subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                        "init", "--bare", "-q", str(bare)], env=clean_env(), check=True)
        for path in ("src/internal-repo.git/objects", "src/internal-repo.git"):
            with self.subTest(path=path):
                config = {"included_paths": [path], "code_paths": [path], "artifact_paths": []}
                _, report = self.invoke(config=config)
                self.assertEqual(report["overall_result"], "BLOCKED")
                self.assertEqual(self.status(report, "scope.validation"), "FAIL")
                row = next(item for item in report["checks"] if item["check_id"] == "scope.validation")
                self.assertIn("nested Git repository", row["explanation"])

    def test_git_like_directory_without_bare_repository_structure_is_allowed(self):
        candidate = self.target / "src" / "git-like-files"
        (candidate / "objects").mkdir(parents=True)
        (candidate / "refs").mkdir()
        (candidate / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
        (candidate / "config").write_text("[core]\nrepositoryformatversion = 0\n", encoding="ascii")
        config = {"included_paths": ["src/git-like-files/objects"],
                  "code_paths": ["src/git-like-files/objects"], "artifact_paths": []}
        _, report = self.invoke(config=config)
        self.assertEqual(self.status(report, "scope.validation"), "PASS", report)

    def test_git_similar_path_components_are_not_blocked(self):
        for path in ("component.git/config", ".github/config", "git-config/hooks"):
            with self.subTest(path=path):
                config = {"included_paths": [path], "code_paths": [path], "artifact_paths": []}
                _, report = self.invoke(config=config)
                self.assertEqual(self.status(report, "scope.validation"), "PASS", report)

    def test_porcelain_z_parses_real_git_rename_and_newline_paths(self):
        destination = "renamed file with\nnewline.txt"
        run_git(self.target, "mv", "README.txt", destination)
        untracked = "untracked file with\nnewline.txt"
        (self.target / untracked).write_text("untracked\n", encoding="utf-8")
        raw = run_git(self.target, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        parsed = Preflight.parse_porcelain_status_z(raw)
        renamed = next(row for row in parsed["tracked_modifications"] if "R" in row["index"] + row["worktree"])
        self.assertEqual(renamed["source_path"], "README.txt")
        self.assertEqual(renamed["destination_path"], destination)
        self.assertIn(untracked, parsed["untracked_files"])

    def test_porcelain_z_parses_copy_source_and_destination(self):
        source = "source file with\nnewline.txt"
        destination = "copy file with\nnewline.txt"
        (self.target / source).write_text("copy-detectable-content\n", encoding="utf-8")
        run_git(self.target, "add", "--", source)
        run_git(self.target, "commit", "-m", "add copy source", "-q")
        (self.target / destination).write_text((self.target / source).read_text(encoding="utf-8"), encoding="utf-8")
        run_git(self.target, "add", "--", destination)
        copy_diff = run_git(self.target, "diff", "--cached", "--name-status", "-z", "--find-copies-harder", "HEAD").stdout.split("\0")
        self.assertTrue(copy_diff[0].startswith("C"), copy_diff)
        git_source, git_destination = copy_diff[1:3]
        self.assertEqual((git_source, git_destination), (source, destination))
        # Porcelain v1 -z stores the destination first, then the source, in a two-path R/C record.
        porcelain_record = f"C  {git_destination}\0{git_source}\0"
        parsed = Preflight.parse_porcelain_status_z(porcelain_record)
        copied = parsed["tracked_modifications"][0]
        self.assertEqual(copied["index"], "C")
        self.assertEqual(copied["destination_path"], destination)
        self.assertEqual(copied["source_path"], source)

    def test_existing_hard_links_are_reported(self):
        os.link(self.target / "README.txt", self.target / "alias.txt")
        _, report = self.invoke()
        row = next(row for row in report["checks"] if row["check_id"] == "filesystem.hardlinks")
        self.assertEqual(row["status"], "PASS")
        self.assertGreaterEqual(row["evidence"]["count"], 2)
        self.assertIn("safe_writer.py", " ".join(row["limitations"]))

    def test_missing_instructions_and_skills_are_not_errors(self):
        _, report = self.invoke()
        self.assertEqual(self.status(report, "discovery.instructions"), "NOT_APPLICABLE")
        self.assertEqual(self.status(report, "discovery.capabilities"), "NOT_APPLICABLE")

    def test_ancestor_instructions_are_read_only_with_explicit_workspace_root(self):
        (self.base / "AGENTS.md").write_text("Workspace guidance.\n", encoding="utf-8")
        _, default_report = self.invoke()
        self.assertEqual(self.status(default_report, "discovery.instructions"), "NOT_APPLICABLE")
        _, authorized_report = self.invoke(workspace_root=self.base.resolve())
        self.assertEqual(self.status(authorized_report, "discovery.instructions"), "PASS", authorized_report)
        self.assertTrue(any(item["source_path"] == str(self.base.resolve() / "AGENTS.md") for item in authorized_report["instructions"]))

    def test_existing_instructions_skills_agents_and_docs_are_discovered(self):
        (self.target / "AGENTS.md").write_text("Follow fixture guidance.\n", encoding="utf-8")
        (self.target / "docs").mkdir()
        (self.target / "docs" / "guide.md").write_text("Documentation.\n", encoding="utf-8")
        (self.target / ".agents" / "skills" / "sample").mkdir(parents=True)
        (self.target / ".agents" / "skills" / "sample" / "SKILL.md").write_text("Do not execute in preflight.\n", encoding="utf-8")
        (self.target / ".agents" / "agents").mkdir(parents=True)
        (self.target / ".agents" / "agents" / "reviewer.md").write_text("Agent details.\n", encoding="utf-8")
        _, report = self.invoke()
        self.assertEqual(self.status(report, "discovery.instructions"), "PASS")
        self.assertTrue(any(item["source_path"].endswith("AGENTS.md") for item in report["instructions"]))
        self.assertIn(".agents/skills/sample/SKILL.md", [item["path"] for item in report["capabilities"]])
        self.assertIn(".agents/agents/reviewer.md", [item["path"] for item in report["capabilities"]])
        documentation = next(row for row in report["checks"] if row["check_id"] == "discovery.documentation")
        self.assertIn("docs", documentation["paths"]["directories"])

    def test_external_instruction_symlink_requires_human_input(self):
        external = self.base / "external-guidance.md"
        external.write_text("External instructions are not read.\n", encoding="utf-8")
        (self.target / "AGENTS.md").symlink_to(external)
        _, report = self.invoke()
        self.assertEqual(report["overall_result"], "NEEDS_HUMAN_INPUT")
        self.assertIn("human_input_required", report["result_categories"])


if __name__ == "__main__":
    unittest.main()

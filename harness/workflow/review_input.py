"""Build the safe, complete file diff supplied to code-review campaigns."""

from __future__ import annotations

import json
import base64
import ast
import difflib
import hashlib
import subprocess
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Callable

from workflow import workspace
from workflow.review import ReviewDiff


OS_METADATA_NAMES = {
    ".ds_store", "thumbs.db", "ehthumbs.db", "desktop.ini", ".directory", ".appledouble",
}
KNOWN_BINARY_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"PK\x03\x04",
    b"%PDF-", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
)


class ReviewInputError(RuntimeError):
    """A repository change could not be accounted for in the review input."""


def _os_metadata_reason(relative: str) -> str | None:
    parts = Path(relative).parts
    name = Path(relative).name
    if any(part.lower() == "__macosx" for part in parts):
        return "Apple operating-system metadata"
    if any(part.lower() == ".appledouble" for part in parts):
        return "Apple operating-system metadata"
    if name.startswith("._") or name.lower() in OS_METADATA_NAMES:
        return "operating-system metadata"
    return None


def _binary_content_reason(content: bytes) -> str | None:
    if b"\x00" in content:
        return "contains a NUL byte"
    if any(content.startswith(signature) for signature in KNOWN_BINARY_SIGNATURES):
        return "matches a known binary file signature"
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return "is not valid UTF-8 text (binary or unsupported text encoding)"

    sample = content[:8192]
    controls = sum((byte < 32 and byte not in (9, 10, 12, 13)) or byte == 127 for byte in sample)
    if controls and controls / len(sample) > 0.10:
        return "contains a high proportion of non-text control bytes"
    return None


def _baseline_blob(target: Path, baseline: str, relative: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(target), "show", f"{baseline}:{relative}"],
        cwd=target,
        env=workspace.git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _baseline_entries(snapshot: dict | None) -> dict[str, dict]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        return {}
    entries = snapshot.get("entries")
    if not isinstance(entries, list):
        return {}
    return {
        item["path"]: item for item in entries
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }


def _snapshot_bytes(entry: dict | None) -> bytes | None:
    if not isinstance(entry, dict) or entry.get("kind") != "file":
        return None
    encoded = entry.get("content_base64")
    if not isinstance(encoded, str):
        return None
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
        return None
    return content


def _working_bytes(target: Path, relative: str) -> bytes | None:
    path = target / relative
    if path.is_symlink() or workspace.has_symlink_component(target, relative):
        return None
    try:
        if not path.exists():
            return None
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _working_mode(target: Path, relative: str) -> str | None:
    path = target / relative
    if path.is_symlink() or workspace.has_symlink_component(target, relative):
        return None
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return "100755" if metadata.st_mode & 0o111 else "100644"


def _entry_mode(entry: dict | None) -> str | None:
    mode = entry.get("mode") if isinstance(entry, dict) else None
    if type(mode) is not int:
        return None
    return "100755" if mode & 0o111 else "100644"


def _index_entries(target: Path, relative: str) -> list[list[str]]:
    output = workspace.git(target, "ls-files", "--stage", "-z", "--", f":(literal){relative}")
    records = []
    for record in output.split(b"\0"):
        if not record:
            continue
        header, separator, path = record.partition(b"\t")
        fields = header.split(b" ")
        if not separator or path != relative.encode("utf-8", "surrogateescape") or len(fields) != 3:
            raise ReviewInputError("Git returned an invalid index record during review scope construction.")
        mode, object_id, stage = fields
        records.append([mode.decode("ascii"), object_id.decode("ascii"), stage.decode("ascii")])
    return sorted(records)


def _head_index_entries(target: Path, baseline: str, relative: str) -> list[list[str]]:
    output = workspace.git(target, "ls-tree", "-z", baseline, "--", f":(literal){relative}")
    records = []
    for record in output.split(b"\0"):
        if not record:
            continue
        header, separator, path = record.partition(b"\t")
        fields = header.split(b" ")
        if not separator or path != relative.encode("utf-8", "surrogateescape") or len(fields) != 3:
            raise ReviewInputError("Git returned an invalid HEAD tree record during review scope construction.")
        mode, _object_type, object_id = fields
        records.append([mode.decode("ascii"), object_id.decode("ascii"), "0"])
    return sorted(records)


def _unified_patch(
    relative: str,
    before: bytes | None,
    after: bytes | None,
    *,
    before_mode: str | None = None,
    after_mode: str | None = None,
) -> str | None:
    mode_changed = before_mode is not None and after_mode is not None and before_mode != after_mode
    if before == after and not mode_changed:
        return None
    before_text = before.decode("utf-8") if before is not None else ""
    after_text = after.decode("utf-8") if after is not None else ""
    source_path = json.dumps(f"a/{relative}", ensure_ascii=True)
    destination_path = json.dumps(f"b/{relative}", ensure_ascii=True)
    old_lines = before_text.splitlines(keepends=True)
    new_lines = after_text.splitlines(keepends=True)
    if before_text and not before_text.endswith(("\n", "\r")):
        old_lines[-1] += "\n"
    if after_text and not after_text.endswith(("\n", "\r")):
        new_lines[-1] += "\n"
    old_name = f"a/{relative}" if before is not None else "/dev/null"
    new_name = f"b/{relative}" if after is not None else "/dev/null"
    body = ""
    if before != after:
        body = "".join(difflib.unified_diff(
            old_lines, new_lines, fromfile=old_name, tofile=new_name, lineterm="\n",
        ))
    old_mode = (
        f"new file mode {after_mode}\n" if before is None and after is not None and after_mode not in {None, '100644'} else
        f"old mode {before_mode}\n" if mode_changed else
        f"deleted file mode {before_mode or '100644'}\n" if after is None and before is not None else ""
    )
    new_mode = f"new mode {after_mode}\n" if mode_changed else ""
    return (
        f"diff --git {source_path} {destination_path}\n{old_mode}{new_mode}{body}"
    )


def _text_or_reason(content: bytes | None, *, label: str) -> tuple[str | None, str | None]:
    if content is None:
        return None, None
    reason = _binary_content_reason(content)
    if reason is not None:
        return None, f"{label} {reason}"
    try:
        return content.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, f"{label} is not valid UTF-8 text"


def _snapshot_integrity(snapshot: dict) -> bool:
    expected = snapshot.get("sha256")
    body = {key: value for key, value in snapshot.items() if key != "sha256"}
    actual = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return isinstance(expected, str) and expected == actual


def _operation_diffs(
    target: Path,
    baseline: str,
    protected_paths: set[str],
    baseline_snapshot: dict,
    implementation_snapshot: dict,
    final_workflow_paths: Iterable[str],
) -> tuple[tuple[str, str], ...]:
    if not _snapshot_integrity(implementation_snapshot):
        raise ReviewInputError("Saved implementation snapshot integrity check failed; correction attribution is inconclusive.")
    baseline_entries = _baseline_entries(baseline_snapshot)
    implementation_entries = _baseline_entries(implementation_snapshot)
    paths = sorted(set(implementation_entries) | set(final_workflow_paths))
    original_patches = []
    correction_patches = []
    for relative in paths:
        base_entry = baseline_entries.get(relative) if relative in protected_paths else None
        if relative in protected_paths and base_entry is None:
            raise ReviewInputError(f"Original baseline content for {relative} is unavailable; correction attribution is inconclusive.")
        before = _snapshot_bytes(base_entry) if base_entry else _baseline_blob(target, baseline, relative)
        baseline_mode = _entry_mode(base_entry)
        if baseline_mode is None:
            baseline_index = _head_index_entries(target, baseline, relative)
            baseline_mode = baseline_index[0][0] if baseline_index else None
        initial_entry = implementation_entries.get(relative)
        if initial_entry:
            initial = _snapshot_bytes(initial_entry)
            if initial is None and initial_entry.get("kind") == "file":
                raise ReviewInputError(f"Initial implementation content for {relative} is unavailable; correction attribution is inconclusive.")
            initial_mode = _entry_mode(initial_entry)
        else:
            initial = before
            initial_mode = baseline_mode
        final = _working_bytes(target, relative)
        final_mode = _working_mode(target, relative)
        for content, label in ((before, "Original implementation"), (initial, "Initial implementation"),
                               (final, "Final correction")):
            reason = _binary_content_reason(content) if content is not None else None
            if reason is not None:
                raise ReviewInputError(f"{label} content for {relative} is not reviewable: {reason}.")
        original_patch = _unified_patch(
            relative, before, initial, before_mode=baseline_mode, after_mode=initial_mode,
        )
        correction_patch = _unified_patch(
            relative, initial, final, before_mode=initial_mode, after_mode=final_mode,
        )
        if original_patch:
            original_patches.append(original_patch)
        if correction_patch:
            correction_patches.append(correction_patch)
    return (
        ("Original implementation delta", "\n".join(original_patches) or "No workflow delta was present in the initial implementation snapshot."),
        ("Correction delta since initial review", "\n".join(correction_patches) or "No further source delta was present after the initial review."),
    )


def _direct_python_dependencies(
    target: Path,
    baseline: str,
    scope_paths: set[str],
    protected_paths: set[str],
    entries: dict[str, dict],
    *,
    maximum: int = 25,
) -> set[str]:
    """Resolve one import hop from commitment-selected Python files without scanning the repository."""
    dependencies: set[str] = set()
    for source in sorted(scope_paths):
        if Path(source).suffix != ".py":
            continue
        entry = entries.get(source) if source in protected_paths else None
        content = _working_bytes(target, source)
        if content is None:
            content = _snapshot_bytes(entry) if entry else _baseline_blob(target, baseline, source)
        if content is None:
            continue
        try:
            tree = ast.parse(content.decode("utf-8"))
        except (UnicodeDecodeError, SyntaxError):
            continue
        module_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package = list(Path(source).parent.parts)
                    if node.level > 1:
                        package = package[:max(0, len(package) - node.level + 1)]
                    if node.module:
                        package.extend(node.module.split("."))
                    prefix = ".".join(package)
                else:
                    prefix = node.module or ""
                if prefix:
                    module_names.add(prefix)
                elif node.level:
                    module_names.update(
                        ".".join([*package, alias.name]) for alias in node.names
                    )
        for module_name in sorted(module_names):
            parts = module_name.split(".")
            candidates = ["/".join(parts) + ".py", "/".join([*parts, "__init__.py"])]
            for relative in candidates:
                if workspace.is_sensitive(relative) or workspace.has_symlink_component(target, relative):
                    continue
                path = target / relative
                exists_now = path.is_file() and not path.is_symlink()
                exists_before = bool(entries.get(relative)) or _baseline_blob(target, baseline, relative) is not None
                if exists_now or exists_before:
                    dependencies.add(relative)
                    if len(dependencies) >= maximum:
                        return dependencies
    return dependencies


def _legacy_review_diff(
    target: Path,
    baseline: str,
    preexisting_paths: Iterable[str],
    *,
    redactor: Callable[[str], str] | None,
) -> ReviewDiff:
    """Retain the exact v1 patch contract for persisted workflows without baseline snapshots."""
    tracked, untracked = workspace.changed_paths(target, baseline, redactor=redactor)
    protected = set(preexisting_paths)
    workflow_prefix = workspace.WORKFLOW_ROOT + "/"
    chunks: list[str] = []
    file_patches: list[tuple[str, str]] = []
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    tracked_paths = set(tracked)
    for relative in sorted(set(tracked) | set(untracked)):
        reason = _os_metadata_reason(relative)
        if reason is None and relative in protected:
            reason = "pre-existing target change is protected from review"
        if reason is None and relative.startswith(workflow_prefix):
            reason = "workflow state is not part of the implementation diff"
        if reason is None and workspace.is_sensitive(relative):
            reason = "sensitive path is excluded from external review"
        if reason is None and ".git" in Path(relative).parts:
            reason = "Git metadata is not part of the implementation diff"
        if reason is None and workspace.has_symlink_component(target, relative):
            reason = "symlink paths are excluded from external review"
        if reason is not None:
            excluded.append((relative, reason))
            continue
        path = target / relative
        if path.is_symlink():
            excluded.append((relative, "symlink files are excluded from external review"))
            continue
        try:
            working = path.read_bytes() if path.exists() and path.is_file() else None
        except OSError:
            excluded.append((relative, "working-tree content could not be read"))
            continue
        previous = _baseline_blob(target, baseline, relative) if relative in tracked_paths else None
        if working is None and previous is None:
            excluded.append((relative, "changed file content is unavailable in both the working tree and baseline"))
            continue
        binary_reason = _binary_content_reason(working) if working is not None else None
        if binary_reason is None and previous is not None:
            binary_reason = _binary_content_reason(previous)
            if binary_reason is not None:
                binary_reason = f"baseline version {binary_reason}"
        if binary_reason is not None:
            excluded.append((relative, binary_reason))
            continue
        try:
            relative.encode("utf-8")
            if relative in tracked_paths:
                raw = workspace.git(
                    target, "diff", "--no-ext-diff", "--no-textconv", "--no-color", baseline, "--", relative,
                    redactor=redactor,
                )
                patch = raw.decode("utf-8")
                if "\x00" in patch:
                    excluded.append((relative, "generated diff contains a NUL byte"))
                    continue
                if redactor is not None:
                    patch = redactor(patch)
            else:
                content = working.decode("utf-8")
                source_path = json.dumps(f"a/{relative}", ensure_ascii=True)
                destination_path = json.dumps(f"b/{relative}", ensure_ascii=True)
                lines = content.splitlines(keepends=True)
                if content and not content.endswith(("\n", "\r")):
                    lines[-1] += "\n"
                patch = (
                    f"diff --git {source_path} {destination_path}\nnew file mode 100644\n"
                    f"--- /dev/null\n+++ {destination_path}\n@@ -0,0 +1,{len(lines)} @@\n"
                    + "".join(f"+{line}" for line in lines)
                )
                if redactor is not None:
                    patch = redactor(patch)
            chunks.append(patch)
            file_patches.append((relative, patch))
            included.append(relative)
        except UnicodeDecodeError:
            excluded.append((relative, "generated diff is not valid UTF-8 text"))
        except UnicodeEncodeError:
            excluded.append((relative, "path cannot be safely encoded as UTF-8"))
    discovered = set(tracked) | set(untracked)
    if discovered != set(included) | {path for path, _reason in excluded}:
        raise ReviewInputError("Legacy review diff did not account for every changed path.")
    text = "\n".join(chunks)
    return ReviewDiff(text if text.strip() else "No reviewable changes were found.", tuple(included),
                      tuple(excluded), tuple(file_patches))


def build_review_diff(
    target: Path,
    baseline: str,
    preexisting_paths: Iterable[str],
    *,
    baseline_snapshot: dict | None = None,
    review_scope_paths: Iterable[str] = (),
    implementation_snapshot: dict | None = None,
    redactor: Callable[[str], str] | None = None,
) -> ReviewDiff:
    """Build an attributable baseline-to-final patch and bounded commitment-selected file context."""
    if baseline_snapshot is None:
        return _legacy_review_diff(target, baseline, preexisting_paths, redactor=redactor)
    if baseline_snapshot is not None:
        if not _snapshot_integrity(baseline_snapshot):
            raise ReviewInputError("Saved baseline snapshot integrity check failed; review attribution is inconclusive.")
    tracked, untracked = workspace.changed_paths(target, baseline, redactor=redactor)
    protected = set(preexisting_paths)
    workflow_prefix = workspace.WORKFLOW_ROOT + "/"
    chunks: list[str] = []
    file_patches: list[tuple[str, str]] = []
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    workflow_paths: list[str] = []
    entries = _baseline_entries(baseline_snapshot)
    discovered = set(tracked) | set(untracked)
    candidates = set(tracked) | set(untracked) | protected

    for relative in sorted(candidates):
        reason = _os_metadata_reason(relative)
        if reason is None and relative.startswith(workflow_prefix):
            reason = "workflow state is not part of the implementation diff"
        if reason is None and workspace.is_sensitive(relative):
            reason = "sensitive path is excluded from external review"
        if reason is None and ".git" in Path(relative).parts:
            reason = "Git metadata is not part of the implementation diff"
        if reason is None and workspace.has_symlink_component(target, relative):
            reason = "symlink paths are excluded from external review"
        if reason is not None:
            if relative in (set(tracked) | set(untracked)):
                excluded.append((relative, reason))
            continue

        entry = entries.get(relative) if relative in protected else None
        if relative in protected and entry is None:
            if relative in discovered:
                excluded.append((relative, "pre-existing baseline snapshot is unavailable; attribution is inconclusive"))
            continue
        baseline_index = (
            entry.get("index_entries") if isinstance(entry, dict)
            else _head_index_entries(target, baseline, relative)
        )
        if not isinstance(baseline_index, list):
            baseline_index = []
        index_changed = _index_entries(target, relative) != baseline_index
        baseline_mode = _entry_mode(entry)
        if baseline_mode is None and baseline_index:
            baseline_mode = baseline_index[0][0]
        current_mode = _working_mode(target, relative)
        mode_changed = (
            baseline_mode is not None and current_mode is not None and baseline_mode != current_mode
        ) or (baseline_mode is not None and current_mode is None and _working_bytes(target, relative) is None)
        previous_content = _snapshot_bytes(entry) if entry else _baseline_blob(target, baseline, relative)
        working_content = _working_bytes(target, relative)
        if entry and entry.get("kind") == "file" and previous_content is None:
            working_sha = hashlib.sha256(working_content).hexdigest() if working_content is not None else None
            if working_sha == entry.get("sha256") and not mode_changed:
                if index_changed:
                    excluded.append((relative, "Git index changed without a worktree source delta; attribution is inconclusive"))
                elif relative in discovered:
                    excluded.append((relative, "pre-existing change is unchanged since workflow start"))
                continue
            if working_sha != entry.get("sha256") or mode_changed:
                workflow_paths.append(relative)
            if relative in (set(tracked) | set(untracked)):
                excluded.append((relative, "pre-existing baseline content is unavailable; attribution is inconclusive"))
            continue
        if previous_content == working_content and not mode_changed:
            if index_changed:
                excluded.append((relative, "Git index changed without a worktree source delta; attribution is inconclusive"))
            elif relative in discovered:
                excluded.append((relative, "pre-existing change is unchanged since workflow start"))
            continue
        workflow_paths.append(relative)
        if relative in protected and entry is None:
            excluded.append((relative, "pre-existing baseline snapshot is unavailable; attribution is inconclusive"))
            continue
        before_reason = _binary_content_reason(previous_content) if previous_content is not None else None
        after_reason = _binary_content_reason(working_content) if working_content is not None else None
        if before_reason or after_reason:
            reason = before_reason or after_reason or "binary or unsupported text encoding"
            excluded.append((relative, f"baseline-to-final change {reason}"))
            continue
        try:
            relative.encode("utf-8")
            if relative in tracked and relative not in protected:
                raw = workspace.git(
                    target, "diff", "--no-ext-diff", "--no-textconv", "--no-color", baseline, "--", relative,
                    redactor=redactor,
                )
                patch = raw.decode("utf-8")
                if "\x00" in patch:
                    excluded.append((relative, "generated diff contains a NUL byte"))
                    continue
            else:
                patch = _unified_patch(
                    relative, previous_content, working_content,
                    before_mode=baseline_mode,
                    after_mode=current_mode,
                )
            if patch is None:
                continue
            if redactor is not None and not (relative in tracked and relative not in protected):
                patch = redactor(patch)
            if "\x00" in patch:
                excluded.append((relative, "generated diff contains a NUL byte"))
                continue
            chunks.append(patch)
            file_patches.append((relative, patch))
        except UnicodeDecodeError:
            excluded.append((relative, "generated diff is not valid UTF-8 text"))
            continue
        except UnicodeEncodeError:
            excluded.append((relative, "path cannot be safely encoded as UTF-8"))
            continue
        included.append(relative)

    accounted_for = set(included) | {path for path, _reason in excluded}
    if not discovered.issubset(accounted_for):
        missing = sorted(discovered - accounted_for)
        raise ReviewInputError(
            "Review diff file accounting is incomplete; changed paths were not included or recorded as excluded: "
            + ", ".join(json.dumps(path, ensure_ascii=True) for path in missing)
        )

    scope_paths = set(review_scope_paths)
    dependency_paths = _direct_python_dependencies(
        target, baseline, scope_paths, protected, entries,
    ) - scope_paths
    context_files = []
    context_exclusions = []
    context_total = 0
    for relative in sorted(scope_paths | dependency_paths):
        if (not isinstance(relative, str) or not relative or relative.startswith("/")
                or "\\" in relative or any(part in {"", ".", ".."} for part in Path(relative).parts)):
            context_exclusions.append((str(relative), "unsafe review-scope path"))
            continue
        reason = _os_metadata_reason(relative)
        if reason is None and (workspace.is_sensitive(relative) or ".git" in Path(relative).parts):
            reason = "sensitive or Git metadata paths are excluded from external review"
        if reason is None and relative.startswith(workflow_prefix):
            reason = "workflow state is not part of the review scope"
        if reason is None and workspace.has_symlink_component(target, relative):
            reason = "symlink paths are excluded from external review"
        if reason is not None:
            context_exclusions.append((relative, reason))
            continue

        entry = entries.get(relative)
        if relative in protected and entry is None:
            context_exclusions.append((relative, "legacy or unavailable baseline snapshot"))
            continue
        if entry:
            before = _snapshot_bytes(entry)
            provenance = "pre-existing target state"
            if entry.get("kind") == "file" and before is None:
                context_exclusions.append((relative, "pre-existing baseline content is unavailable"))
                continue
            if entry.get("kind") not in {"file", "missing"}:
                context_exclusions.append((relative, "pre-existing baseline path is not a regular file"))
                continue
        else:
            before = _baseline_blob(target, baseline, relative)
            provenance = "Git HEAD baseline" if before is not None else "not present at workflow start"
        after = _working_bytes(target, relative)
        if before is None and after is None:
            context_exclusions.append((relative, "baseline and final source content are unavailable"))
            continue
        before_text, before_reason = _text_or_reason(before, label="Baseline source")
        after_text, after_reason = _text_or_reason(after, label="Final source")
        if before_reason or after_reason:
            context_exclusions.append((relative, before_reason or after_reason or "unsupported source content"))
            continue
        cost = sum(len(item.encode("utf-8")) for item in (before_text or "", after_text or ""))
        if cost > 100_000 or context_total + cost > 200_000:
            context_exclusions.append((relative, "selected source exceeds the bounded review-context limit"))
            continue
        context_total += cost
        context_files.append((relative, provenance, before_text or "(file absent at baseline)",
                              after_text or "(file absent in final target)"))

    operation_diffs: tuple[tuple[str, str], ...] = ()
    if implementation_snapshot is not None:
        operation_diffs = _operation_diffs(
            target, baseline, protected, baseline_snapshot, implementation_snapshot, workflow_paths,
        )
    result = "\n".join(chunks)
    return ReviewDiff(
        result if result.strip() else "No reviewable changes were found.",
        tuple(included),
        tuple(excluded),
        tuple(file_patches),
        tuple(workflow_paths),
        tuple(context_files),
        tuple(context_exclusions),
        operation_diffs,
    )

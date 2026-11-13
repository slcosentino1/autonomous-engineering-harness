"""Build the safe, complete file diff supplied to code-review campaigns."""

from __future__ import annotations

import json
import subprocess
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


def build_review_diff(
    target: Path,
    baseline: str,
    preexisting_paths: Iterable[str],
    *,
    redactor: Callable[[str], str] | None = None,
) -> ReviewDiff:
    """Construct the same sorted, redacted review input from an explicit repository snapshot."""
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
        working_content: bytes | None = None
        try:
            if path.exists():
                if not path.is_file():
                    excluded.append((relative, "changed path is not a regular file"))
                    continue
                working_content = path.read_bytes()
        except OSError:
            excluded.append((relative, "working-tree content could not be read"))
            continue

        previous_content = _baseline_blob(target, baseline, relative) if relative in tracked_paths else None
        if working_content is None and previous_content is None:
            excluded.append((relative, "changed file content is unavailable in both the working tree and baseline"))
            continue

        binary_reason = _binary_content_reason(working_content) if working_content is not None else None
        if binary_reason is None and previous_content is not None:
            binary_reason = _binary_content_reason(previous_content)
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
                text = raw.decode("utf-8")
                if "\x00" in text:
                    excluded.append((relative, "generated diff contains a NUL byte"))
                    continue
                patch = redactor(text) if redactor is not None else text
            else:
                text = working_content.decode("utf-8")
                source_path = json.dumps(f"a/{relative}", ensure_ascii=True)
                destination_path = json.dumps(f"b/{relative}", ensure_ascii=True)
                content_lines = text.splitlines(keepends=True)
                if text and not text.endswith(("\n", "\r")):
                    content_lines[-1] += "\n"
                hunk = "".join(f"+{line}" for line in content_lines)
                patch = (
                    f"diff --git {source_path} {destination_path}\nnew file mode 100644\n"
                    f"--- /dev/null\n+++ {destination_path}\n"
                    f"@@ -0,0 +1,{len(content_lines)} @@\n{hunk}"
                )
                if redactor is not None:
                    patch = redactor(patch)
            chunks.append(patch)
            file_patches.append((relative, patch))
        except UnicodeDecodeError:
            excluded.append((relative, "generated diff is not valid UTF-8 text"))
            continue
        except UnicodeEncodeError:
            excluded.append((relative, "path cannot be safely encoded as UTF-8"))
            continue
        included.append(relative)

    discovered = set(tracked) | set(untracked)
    accounted_for = set(included) | {path for path, _reason in excluded}
    if discovered != accounted_for:
        missing = sorted(discovered - accounted_for)
        raise ReviewInputError(
            "Review diff file accounting is incomplete; changed paths were not included or recorded as excluded: "
            + ", ".join(json.dumps(path, ensure_ascii=True) for path in missing)
        )

    result = "\n".join(chunks)
    return ReviewDiff(
        result if result.strip() else "No reviewable changes were found.",
        tuple(included),
        tuple(excluded),
        tuple(file_patches),
    )

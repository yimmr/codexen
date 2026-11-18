"""Core filesystem operations: read, write, edit snippets, list, delete, and copy."""
from __future__ import annotations

import errno
import fnmatch
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from ..config import ProjectConfig
from ..exceptions import (
    FileServiceError,
    FileSizeLimitError,
    PathSecurityError,
    PermissionDeniedError,
    ReadOnlyProjectError,
    SnippetAmbiguousError,
    SnippetNotFoundError,
    UnsupportedNewlineModeError,
)
from .edit_diagnostics import diagnostic_summary, safe_diagnose_edit
from .gitignore import get_project_ignore_manager, is_path_authorized, log_penetration_audit
from .newline_policy import (
    NewlineStyle,
    convert_newline_text,
    profile_newlines,
    validate_newline_encoding,
)
from .security import (
    assert_not_sensitive_path,
    ensure_project_accessible,
    normalize_relative_path,
    safe_resolve_path,
)
from .stream_matcher import replace_streaming_auto


def utc_iso_now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


# Legacy compatibility baseline. Runtime pruning is driven by
# BASELINE_FALLBACK_IGNORED / project .gitignore in core/gitignore.py.
DEFAULT_FS_IGNORED: list[str] = [
    ".git", ".svn", ".hg", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".venv", "venv", "env", "node_modules", ".pnpm", ".pnpm-store",
    "target", "dist", "build", "out", "bin", "obj", ".next", ".nuxt", ".output",
    ".tmp", "tmp", "temp", ".idea", ".vscode", "*.pyc", "*.pyo", "*.pyd",
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "Cargo.lock",
    "*.map", "*.min.js", "*.min.css",
]


def matches_any_pattern(name: str, patterns: list[str]) -> bool:
    """Check if a filename or directory name matches any glob pattern."""
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def is_binary_file(path: Path) -> bool:
    """Quick heuristic check to see if a file contains binary / null bytes in its header."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
            return b"\x00" in chunk
    except Exception:
        return False


def count_file_lines_stream(file_path: Path) -> int:
    """Accurately count lines in a file in O(1) memory via chunked binary stream."""
    count = 0
    last_byte = b""
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                count += chunk.count(b"\n")
                last_byte = chunk[-1:]
        if last_byte and last_byte != b"\n":
            count += 1
        return count
    except Exception:
        return 0


def list_project_files(
    project: ProjectConfig,
    subpath: str = "",
    recursive: bool = False,
    max_depth: int = 5,
    ignored_patterns: list[str] | None = None,
    fallback_ignored_patterns: list[str] | None = None,
    max_entries: int = 1_000,
    pattern: str = "",
    extension: str = "",
    confirmed: bool = False,
    authorized_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List files and directories inside a project.

    Supports optional pattern (glob), extension filtering, and HITL penetration.
    """
    ensure_project_accessible(project)
    clean_subpath = normalize_relative_path(subpath)
    if clean_subpath:
        assert_not_sensitive_path(clean_subpath, "Listing")
    target_dir = safe_resolve_path(project.root, clean_subpath)

    if not target_dir.exists():
        raise FileServiceError(f"Target directory does not exist: '{subpath}' in project '{project.name}'")
    if not target_dir.is_dir():
        raise FileServiceError(f"Target path is a file, not a directory: '{subpath}' in project '{project.name}'")

    user_ignored = list(ignored_patterns or [])
    ignore_mgr = get_project_ignore_manager(
        project,
        fallback_ignored_patterns=fallback_ignored_patterns if fallback_ignored_patterns is not None else user_ignored,
    )

    clean_authorized = [p.replace("\\", "/").strip("/") for p in (authorized_paths or []) if p.strip()]

    # HITL Penetration Interception Protocol (RFC 0001 Phase 3 & Contract Compliance)
    if clean_subpath:
        decision = ignore_mgr.explain_ignored_decision(
            clean_subpath,
            is_dir=target_dir.is_dir(),
            caller_ignored=user_ignored,
            check_parents=True,
            authorized_paths=None,
        )
        is_subpath_normally_ignored = decision.ignored
        rule_source = decision.source
        matched_pat = decision.pattern
        if is_subpath_normally_ignored:
            authorized = confirmed and is_path_authorized(clean_subpath, clean_authorized)
            if not authorized:
                log_penetration_audit(
                    project_name=project.name,
                    requested_path=clean_subpath,
                    status="needs_confirmation",
                    authorized_paths=clean_authorized,
                    confirmed=confirmed,
                )
                source_display = ".gitignore" if rule_source == "project_gitignore" else rule_source
                if not confirmed:
                    if rule_source == "project_gitignore":
                        reason_msg = (
                            f"The directory '{clean_subpath}' is excluded by project .gitignore ({matched_pat}). "
                            f"Penetrating ignored build directories may scan thousands of files and degrade performance. "
                            f"You MUST ask the user in chat for confirmation. Once approved, "
                            f"re-call with confirmed=True and authorized_paths=['{clean_subpath}']."
                        )
                    elif rule_source == "global_fallback":
                        reason_msg = (
                            f"The directory '{clean_subpath}' is excluded by global fallback patterns ({matched_pat}). "
                            f"You MUST ask the user in chat for confirmation. Once approved, "
                            f"re-call with confirmed=True and authorized_paths=['{clean_subpath}']."
                        )
                    elif rule_source == "project_config":
                        reason_msg = (
                            f"The directory '{clean_subpath}' is excluded by project configuration ({matched_pat}). "
                            f"You MUST ask the user in chat for confirmation. Once approved, "
                            f"re-call with confirmed=True and authorized_paths=['{clean_subpath}']."
                        )
                    else:
                        reason_msg = (
                            f"The directory '{clean_subpath}' is excluded ({source_display}: {matched_pat}). "
                            f"You MUST ask the user in chat for confirmation. Once approved, "
                            f"re-call with confirmed=True and authorized_paths=['{clean_subpath}']."
                        )
                else:
                    reason_msg = (
                        f"Path '{clean_subpath}' is excluded by {source_display} ({matched_pat}) and not covered by authorized_paths {clean_authorized}. "
                        f"You MUST ask the user in chat for confirmation. Once approved, "
                        f"re-call with confirmed=True and authorized_paths=['{clean_subpath}']."
                    )

                result_payload = {
                    "name": target_dir.name,
                    "relative_path": clean_subpath,
                    "type": "directory",
                    "status": "needs_confirmation",
                    "confirmation_type": "ignored_directory_penetration",
                    "project": project.name,
                    "requested_subpath": clean_subpath,
                    "matched_rule_source": source_display,
                    "matched_pattern": matched_pat,
                    "message": reason_msg,
                }
                if decision.rule_file:
                    result_payload["rule_file"] = decision.rule_file
                return [result_payload]

    results: list[dict[str, Any]] = []
    root_resolved = project.root.resolve()
    clean_pattern = pattern.strip()
    clean_ext = extension.strip()
    if clean_ext and not clean_ext.startswith("."):
        clean_ext = f".{clean_ext}"

    effective_authorized = clean_authorized if confirmed else None

    def _get_rel_path(p: Path) -> str:
        try:
            return str(p.relative_to(project.root)).replace("\\", "/")
        except ValueError:
            try:
                return str(p.resolve().relative_to(root_resolved)).replace("\\", "/")
            except ValueError:
                return os.path.relpath(str(p.resolve()), str(root_resolved)).replace("\\", "/")

    def _walk(current: Path, current_depth: int) -> None:
        if len(results) >= max_entries:
            return

        try:
            entries = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError as e:
            results.append({
                "name": current.name,
                "relative_path": _get_rel_path(current),
                "type": "directory",
                "error": f"Permission denied: {e}",
            })
            return

        for entry in entries:
            if len(results) >= max_entries:
                break

            name = entry.name
            rel = _get_rel_path(entry)
            if ignore_mgr.is_ignored(
                rel,
                is_dir=entry.is_dir(),
                caller_ignored=user_ignored,
                check_parents=False,
                authorized_paths=effective_authorized,
            ):
                continue
            try:
                stat = entry.stat()
                mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            except OSError:
                mtime_iso = ""

            if entry.is_dir():
                # For directories: include if no pattern or pattern matches, and recurse
                include_dir = True
                if clean_pattern:
                    include_dir = fnmatch.fnmatch(name.lower(), clean_pattern.lower()) or fnmatch.fnmatch(rel.lower(), clean_pattern.lower())
                if clean_ext:
                    include_dir = False

                if include_dir:
                    results.append({
                        "name": name,
                        "relative_path": rel,
                        "type": "directory",
                        "modified_at": mtime_iso,
                    })
                if recursive and current_depth < max_depth:
                    _walk(entry, current_depth + 1)
            else:
                if clean_pattern and not (fnmatch.fnmatch(name.lower(), clean_pattern.lower()) or fnmatch.fnmatch(rel.lower(), clean_pattern.lower())):
                    continue
                if clean_ext and not name.lower().endswith(clean_ext.lower()):
                    continue

                results.append({
                    "name": name,
                    "relative_path": rel,
                    "type": "file",
                    "size_bytes": entry.stat().st_size if entry.exists() else 0,
                    "modified_at": mtime_iso,
                })

    _walk(target_dir, current_depth=1)

    if confirmed and clean_authorized:
        log_penetration_audit(
            project_name=project.name,
            requested_path=clean_subpath or "*",
            status="authorized_executed",
            authorized_paths=clean_authorized,
            confirmed=True,
            files_scanned=len(results),
        )

    return results


def read_project_file(
    project: ProjectConfig,
    relative_path: str,
    encoding: str = "utf-8",
    max_chars: int = 100_000,
    offset: int = 0,
    limit: int = 0,
    start_line: int = 0,
    end_line: int = 0,
    auto_paginate: bool = False,
) -> str | dict[str, Any]:
    """Read full or partial text content of a file within the project."""
    assert_not_sensitive_path(relative_path, "Reading")
    ensure_project_accessible(project)
    file_path = safe_resolve_path(project.root, relative_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"File not found: '{relative_path}' in project '{project.name}' (Root: {project.root})"
        )
    if file_path.is_dir():
        raise FileServiceError(
            f"Cannot read '{relative_path}': Path is a directory, not a regular file."
        )

    try:
        file_size = file_path.stat().st_size
    except OSError as e:
        raise FileServiceError(f"Failed to inspect file '{relative_path}': {e}") from e

    # 1. Line-based streaming slice takes precedence if specified (O(1) RAM)
    if start_line > 0 or end_line > 0:
        start_idx = max(0, start_line - 1)
        end_idx = end_line if end_line > 0 else 0
        slice_lines: list[str] = []
        cur_line_no = 0
        prior_chars = 0
        cur_chars = 0
        hit_char_limit = False
        fit_count = 0

        try:
            with open(file_path, encoding=encoding, errors="replace") as f:
                for line in f:
                    cur_line_no += 1
                    if cur_line_no <= start_idx:
                        prior_chars += len(line)
                        continue
                    if end_idx > 0 and cur_line_no > end_idx:
                        break

                    if cur_chars + len(line) > max_chars and len(slice_lines) > 0:
                        hit_char_limit = True
                        break

                    slice_lines.append(line)
                    cur_chars += len(line)
                    fit_count += 1
        except Exception as e_err:
            raise FileServiceError(f"Failed to read file '{relative_path}' with encoding '{encoding}': {e_err}") from e_err

        if hit_char_limit or cur_chars > max_chars:
            if not auto_paginate:
                raise FileSizeLimitError(
                    f"Selected lines in '{relative_path}' exceed max allowed limit of {max_chars} characters. "
                    f"Tip: Specify a narrower line window (e.g. 500 lines) or enable auto_paginate."
                )
            fit_count = max(1, fit_count)
            returned_end = start_idx + fit_count
            chunk_size = fit_count
            next_start = returned_end + 1
            total_lines = count_file_lines_stream(file_path)
            next_end = min(total_lines, returned_end + chunk_size)
            return {
                "status": "success",
                "project": project.name,
                "relative_path": relative_path,
                "encoding": encoding,
                "content": "".join(slice_lines),
                "truncated": True,
                "truncation_reason": "max_read_chars_limit_reached",
                "total_file_lines": total_lines,
                "returned_start_line": start_idx + 1,
                "returned_end_line": returned_end,
                "returned_chars": cur_chars,
                "next_start_line": next_start,
                "next_offset": prior_chars + cur_chars,
                "hint": (
                    f"File truncated due to size limit. "
                    f"Call read_file(project_id='{project.name}', relative_path='{relative_path}', start_line={next_start}, end_line={next_end}) to read the next segment. "
                    f"Tip: For large files, use bounded windows (e.g. 500 lines) to avoid context overflow."
                ),
            }

        return "".join(slice_lines)

    # 2. Character offset / limit slicing
    if offset > 0 or limit > 0:
        try:
            content = file_path.read_text(encoding=encoding, errors="replace")
        except Exception as e:
            raise FileServiceError(f"Failed to read file '{relative_path}' with encoding '{encoding}': {e}") from e
        end = offset + limit if limit > 0 else len(content)
        return content[offset:end]

    # 3. Full file read or auto-paginate
    if file_size > max_chars:
        if not auto_paginate:
            raise FileSizeLimitError(
                f"File '{relative_path}' ({file_size} bytes) exceeds the max allowed limit of {max_chars} characters. "
                f"Tip: Use 'read_file' with 'start_line' and 'end_line' (e.g. start_line=1, end_line=500) to inspect line ranges safely."
            )
        # Self-healing pagination on full file (streamed to avoid high RAM usage)
        cur_chars = 0
        slice_lines = []
        try:
            with open(file_path, encoding=encoding, errors="replace") as f:
                for line in f:
                    if cur_chars + len(line) > max_chars and len(slice_lines) > 0:
                        break
                    cur_chars += len(line)
                    slice_lines.append(line)
        except Exception as e:
            raise FileServiceError(f"Failed to read file '{relative_path}' with encoding '{encoding}': {e}") from e

        fit_count = max(1, len(slice_lines))
        total_lines = count_file_lines_stream(file_path)
        chunk_size = fit_count
        next_start = fit_count + 1
        next_end = min(total_lines, fit_count + chunk_size)
        return {
            "status": "success",
            "project": project.name,
            "relative_path": relative_path,
            "encoding": encoding,
            "content": "".join(slice_lines),
            "truncated": True,
            "truncation_reason": "max_read_chars_limit_reached",
            "total_file_lines": total_lines,
            "returned_start_line": 1,
            "returned_end_line": fit_count,
            "returned_chars": cur_chars,
            "next_start_line": next_start,
            "next_offset": cur_chars,
            "hint": (
                f"File truncated due to size limit. "
                f"Call read_file(project_id='{project.name}', relative_path='{relative_path}', start_line={next_start}, end_line={next_end}) to read the next segment. "
                f"Tip: For large files, specify start_line and end_line (e.g. start_line=1, end_line=500) to inspect in bounded windows."
            ),
        }

    try:
        content = file_path.read_text(encoding=encoding, errors="replace")
    except Exception as e:
        raise FileServiceError(f"Failed to read file '{relative_path}' with encoding '{encoding}': {e}") from e

    return content


def write_project_file(
    project: ProjectConfig,
    relative_path: str,
    content: str,
    overwrite: bool = True,
    append: bool = False,
    encoding: str = "utf-8",
    newline_mode: str = "auto",
) -> dict[str, Any]:
    """Write or append text content to a project file, creating parent directories if needed."""
    if project.read_only:
        raise ReadOnlyProjectError(
            f"Project '{project.name}' is configured as read-only. Write operation is rejected."
        )

    assert_not_sensitive_path(relative_path, "Writing to")
    ensure_project_accessible(project)
    file_path = safe_resolve_path(project.root, relative_path)

    if file_path == project.root:
        raise FileServiceError("Cannot write to the project root directory itself as a file.")

    if file_path.exists():
        if file_path.is_dir():
            raise FileServiceError(f"Cannot overwrite directory '{relative_path}' with a file.")
        if not overwrite and not append:
            raise FileExistsError(
                f"File '{relative_path}' already exists in project '{project.name}' and overwrite is False."
            )

    file_path.parent.mkdir(parents=True, exist_ok=True)

    if newline_mode not in ("auto", "raw"):
        raise UnsupportedNewlineModeError("newline_mode must be 'auto' or 'raw'")
    if newline_mode == "auto":
        validate_newline_encoding(encoding)
    newline_style: str | None = None
    if newline_mode == "auto" and file_path.exists() and file_path.stat().st_size:
        try:
            with open(file_path, encoding=encoding, errors="strict", newline="") as existing:
                sample = existing.read(1_048_576)
                if append:
                    # Inspect the complete stream in bounded memory: the last
                    # confirmed newline, rather than the prefix, controls append.
                    tail = sample[-1:]
                    last_style = profile_newlines(sample).last_style
                    while chunk := existing.read(1_048_576):
                        part = profile_newlines(tail + chunk)
                        last_style = part.last_style or last_style
                        tail = chunk[-1:]
                    if last_style is not None:
                        sample = "\r\n" if last_style == "crlf" else "\n"
            profile = profile_newlines(sample)
            newline_style = profile.last_style if append else profile.dominant_style
            newline_style = newline_style or profile.first_style
        except (OSError, UnicodeError) as e:
            raise FileServiceError(f"Failed to inspect existing file '{relative_path}': {e}") from e
    newline_style = newline_style or profile_newlines(content).first_style
    body = convert_newline_text(content, cast(NewlineStyle | None, newline_style)) if newline_mode == "auto" else content
    try:
        if append:
            fd, temp_name = tempfile.mkstemp(prefix=".codexen_write_", suffix=".tmp", dir=file_path.parent)
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as output:
                    if file_path.exists():
                        with file_path.open("rb") as source:
                            shutil.copyfileobj(source, output, length=1_048_576)
                    append_encoding = encoding
                    if file_path.exists() and file_path.stat().st_size and encoding.lower().replace("_", "-") == "utf-8-sig":
                        append_encoding = "utf-8"
                    output.write(body.encode(append_encoding))
                if file_path.exists():
                    shutil.copymode(file_path, temp_path)
                _atomic_replace_with_retry(temp_path, file_path)
            finally:
                temp_path.unlink(missing_ok=True)
        else:
            fd, temp_name = tempfile.mkstemp(prefix=".codexen_write_", suffix=".tmp", dir=file_path.parent)
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding=encoding, errors="strict", newline="") as f:
                    f.write(body)
                if file_path.exists():
                    shutil.copymode(file_path, temp_path)
                _atomic_replace_with_retry(temp_path, file_path)
            finally:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
    except Exception as e:
        raise FileServiceError(f"Failed to write file '{relative_path}': {e}") from e

    stat = file_path.stat()
    rel = str(file_path.relative_to(project.root)).replace("\\", "/")

    return {
        "status": "success",
        "action": "appended" if append else "written",
        "project": project.name,
        "relative_path": rel,
        "size_bytes": stat.st_size,
        "chars_written": len(content),
        "newline_style_used": newline_style,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


EDIT_CHUNK_CHARS = 1_048_576
EDIT_TEMP_PREFIX = ".codexen_edit_"
EDIT_TEMP_SUFFIX = ".tmp"
STALE_EDIT_TEMP_SECONDS = 3_600
EDIT_REPLACE_RETRY_LIMIT = 6
EDIT_REPLACE_RETRY_BASE_DELAY = 0.05
EDIT_REPLACE_RETRY_MAX_DELAY = 0.30
HUNK_MAX_CHARS = 320
HUNK_CONTEXT_LINES = 2
HUNK_CONTEXT_CHARS = 200


def _is_transient_replace_error(error: OSError) -> bool:
    """Return True for lock/permission failures worth retrying (not disk errors)."""
    if isinstance(error, PermissionError):
        return True
    if getattr(error, "winerror", None) in (32, 33):  # sharing / lock violation
        return True
    return error.errno in (errno.EACCES, errno.EBUSY)


def _atomic_replace_with_retry(src: Path, dst: Path) -> None:
    """Atomically replace ``dst`` with ``src``, retrying transient lock failures.

    Up to ``EDIT_REPLACE_RETRY_LIMIT`` retries with linear backoff (50..300 ms). Non
    transient errors (disk full, read-only target, etc.) fail immediately.
    """
    for attempt in range(EDIT_REPLACE_RETRY_LIMIT + 1):
        try:
            os.replace(src, dst)
            return
        except OSError as error:
            if attempt >= EDIT_REPLACE_RETRY_LIMIT or not _is_transient_replace_error(error):
                raise
            time.sleep(
                min(
                    EDIT_REPLACE_RETRY_BASE_DELAY * (attempt + 1),
                    EDIT_REPLACE_RETRY_MAX_DELAY,
                )
            )


def _split_block(text: str) -> list[str]:
    """Split a snippet into display lines, dropping the trailing empty line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _context_lines(text: str, limit: int, *, from_end: bool) -> list[str]:
    """Take up to ``limit`` lines from either end of a captured context window."""
    lines = text.split("\n")
    if text.endswith("\n") and lines:
        lines.pop()
    if not lines:
        return []
    return lines[-limit:] if from_end else lines[:limit]


def _build_snippet_hunk(
    before: str, search_text: str, replace_text: str, after: str, start_line: int
) -> tuple[str | None, str | None]:
    """Render a faithful snippet diff for a single replacement, or omit it.

    The hunk is all-or-nothing: it is returned only when the complete preview (all
    changed lines, full line content, two context lines on each side) fits the budget.
    Any clipping, line limiting or hard cut would make the hunk misleading, so in that
    case ``None`` is returned together with an omission reason and the caller points the
    Agent at ``git_diff`` / ``read_file`` for authoritative detail.
    """
    removed = _split_block(search_text)
    added = _split_block(replace_text)
    before_ctx = _context_lines(before, HUNK_CONTEXT_LINES, from_end=True)
    after_ctx = _context_lines(after, HUNK_CONTEXT_LINES, from_end=False)
    header = f"@@ -{start_line},{len(removed)} +{start_line},{len(added)} @@"

    parts = [header]
    parts.extend(f" {line}" for line in before_ctx)
    parts.extend(f"- {line}" for line in removed)
    parts.extend(f"+ {line}" for line in added)
    parts.extend(f" {line}" for line in after_ctx)
    hunk = "\n".join(parts)
    if len(hunk) <= HUNK_MAX_CHARS:
        return hunk, None
    return None, "exceeds_budget"


def _cleanup_stale_edit_temps(
    directory: Path, max_age_seconds: int = STALE_EDIT_TEMP_SECONDS
) -> int:
    """Best-effort removal of orphaned edit temp files left behind by a hard crash.

    Only files matching our own ``.codexen_edit_*.tmp`` prefix are considered, and only
    when older than ``max_age_seconds`` so that a concurrently running edit in the same
    directory is never disturbed. Returns the number of files removed.
    """
    removed = 0
    try:
        now = time.time()
        for candidate in directory.glob(f"{EDIT_TEMP_PREFIX}*{EDIT_TEMP_SUFFIX}"):
            try:
                if now - candidate.stat().st_mtime >= max_age_seconds:
                    candidate.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed


def _replace_snippet_streaming(
    src_path: Path,
    dst: Any,
    search_text: str,
    replace_text: str,
    replace_all: bool,
    encoding: str,
    chunk_chars: int,
    include_hunk: bool,
) -> tuple[int, list[dict[str, int]] | None, str | None, str | None]:
    """Stream-copy ``src_path`` into ``dst`` while replacing snippets in O(chunk) memory.

    The reader keeps a sliding window of ``len(search_text) - 1`` characters so a match
    spanning two read chunks is still found exactly once. Only the first occurrence is
    replaced when ``replace_all`` is False; later occurrences are still counted so the
    caller can reject ambiguous edits without a second full pass.

    Returns ``(occurrence_count, changed_lines, hunk, hunk_omitted_reason)``. Line numbers
    and the snippet hunk are only produced when exactly one replacement happens. The hunk
    is omitted (never truncated) when the faithful preview does not fit the budget.
    """
    match_len = len(search_text)
    carry = ""
    count = 0
    first_replaced = False
    out_newlines = 0
    tail_context = ""
    after_context = ""
    after_newlines = 0
    capture_after = False
    changed_lines: list[dict[str, int]] | None = None
    hunk_text: str | None = None
    hunk_omitted: str | None = None
    first_start_line = 0
    first_end_line = 0
    first_before_context = ""

    def _copy(segment: str) -> None:
        nonlocal out_newlines, tail_context, after_context, after_newlines
        if not segment:
            return
        dst.write(segment)
        out_newlines += segment.count("\n")
        if not include_hunk:
            return
        if capture_after:
            remaining = HUNK_CONTEXT_CHARS - len(after_context)
            if remaining > 0 and after_newlines < HUNK_CONTEXT_LINES:
                piece = segment[:remaining]
                after_context += piece
                after_newlines += piece.count("\n")
        else:
            tail_context = (tail_context + segment)[-HUNK_CONTEXT_CHARS:]

    def _write_replacement() -> None:
        nonlocal out_newlines, capture_after
        dst.write(replace_text)
        out_newlines += replace_text.count("\n")
        if include_hunk:
            capture_after = True

    with open(src_path, encoding=encoding, errors="strict", newline="") as src:
        while True:
            chunk = src.read(chunk_chars)
            eof = not chunk
            buf = carry + chunk
            carry = ""
            # Anything before `cut` is fully decidable using the current window.
            cut = len(buf) if eof else max(0, len(buf) - (match_len - 1))
            written = 0
            pos = 0
            while True:
                idx = buf.find(search_text, pos)
                if idx == -1:
                    break
                if idx + match_len > len(buf):
                    # Match straddles the window tail: leave it for the next round.
                    break
                if replace_all or not first_replaced:
                    _copy(buf[written:idx])
                    start_line = out_newlines + 1
                    _write_replacement()
                    if count == 0:
                        added_newlines = replace_text.count("\n")
                        end_line = start_line + added_newlines
                        if replace_text.endswith("\n") and added_newlines:
                            end_line -= 1
                        first_start_line = start_line
                        first_end_line = max(start_line, end_line)
                        if include_hunk:
                            first_before_context = tail_context
                    written = idx + match_len
                    first_replaced = True
                else:
                    # Consume counted matches even when only the first is
                    # replaced, so overlap carry cannot count them again.
                    _copy(buf[written:idx + match_len])
                    written = idx + match_len
                count += 1
                pos = idx + match_len

            keep_from = max(cut, written)
            _copy(buf[written:keep_from])
            carry = buf[keep_from:]
            if eof:
                break

    if count == 1:
        changed_lines = [{"start": first_start_line, "end": first_end_line}]
        if include_hunk:
            hunk_text, hunk_omitted = _build_snippet_hunk(
                before=first_before_context,
                search_text=search_text,
                replace_text=replace_text,
                after=after_context,
                start_line=first_start_line,
            )

    return count, changed_lines, hunk_text, hunk_omitted


def edit_project_file_snippet(
    project: ProjectConfig,
    relative_path: str,
    search_text: str,
    replace_text: str,
    replace_all: bool = False,
    encoding: str = "utf-8",
    chunk_chars: int = EDIT_CHUNK_CHARS,
    include_hunk: bool = False,
    newline_mode: str = "auto",
) -> dict[str, Any]:
    """Replace an exact substring snippet in a project file of any size.

    The file is rewritten through a sibling temporary file and swapped in atomically,
    so a failed or ambiguous edit never leaves a partially edited file behind. There is
    no read-size gate: arbitrarily large text files are edited with bounded memory.
    """
    if newline_mode not in ("auto", "raw"):
        raise UnsupportedNewlineModeError("newline_mode must be 'auto' or 'raw'")
    if project.read_only:
        raise ReadOnlyProjectError(
            f"Project '{project.name}' is configured as read-only. Edit operation is rejected."
        )

    assert_not_sensitive_path(relative_path, "Editing")
    ensure_project_accessible(project)

    if not search_text:
        raise FileServiceError("search_text cannot be empty.")

    file_path = safe_resolve_path(project.root, relative_path)
    if not file_path.exists():
        raise FileNotFoundError(
            f"File not found: '{relative_path}' in project '{project.name}' (Root: {project.root})"
        )
    if file_path.is_dir():
        raise FileServiceError(
            f"Cannot edit '{relative_path}': Path is a directory, not a regular file."
        )
    if is_binary_file(file_path):
        raise FileServiceError(
            f"Cannot edit '{relative_path}': the file looks binary (NUL bytes detected). "
            f"Use write_binary_file for binary payloads."
        )

    # Best-effort sweep of orphaned temp files from a previous crash (same directory,
    # older than an hour). Never touches a concurrent edit's live temp file.
    _cleanup_stale_edit_temps(file_path.parent)

    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=EDIT_TEMP_PREFIX, suffix=EDIT_TEMP_SUFFIX, dir=str(file_path.parent)
    )
    tmp_path = Path(tmp_name)
    committed = False
    try:
        try:
            dst_handle = os.fdopen(tmp_fd, "w", encoding=encoding, errors="strict", newline="")
        except Exception:
            os.close(tmp_fd)
            raise
        with dst_handle as dst:
            if newline_mode == "auto":
                scan = replace_streaming_auto(file_path, dst, search_text, replace_text, replace_all, encoding, chunk_chars, include_hunk)
                occurrences = scan["occurrences"]
                changed_lines, hunk, hunk_omitted = scan["changed_lines"], scan["hunk"], scan["hunk_omitted_reason"]
            else:
                occurrences, changed_lines, hunk, hunk_omitted = _replace_snippet_streaming(file_path, dst, search_text, replace_text, replace_all, encoding, max(1, chunk_chars), include_hunk)

        if occurrences == 0:
            diagnostic = safe_diagnose_edit(file_path, search_text)
            raise SnippetNotFoundError(
                f"search_text not found in file '{relative_path}'. Edit aborted. "
                f"{diagnostic_summary(diagnostic)}"
            )
        if occurrences > 1 and not replace_all:
            diagnostic = safe_diagnose_edit(file_path, search_text)
            raise SnippetAmbiguousError(
                f"search_text occurs {occurrences} times in '{relative_path}'. Specify replace_all=True or provide more unique context. "
                f"{diagnostic_summary(diagnostic)}"
            )

        try:
            shutil.copymode(file_path, tmp_path)
        except OSError:
            pass
        _atomic_replace_with_retry(tmp_path, file_path)
        committed = True
    except UnicodeDecodeError as e:
        raise FileServiceError(
            f"Failed to edit '{relative_path}': file is not valid {encoding} text ({e})."
        ) from e
    except OSError as e:
        raise FileServiceError(f"Failed to edit file '{relative_path}': {e}") from e
    finally:
        if not committed:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    stat = file_path.stat()
    rel = str(file_path.relative_to(project.root)).replace("\\", "/")
    result: dict[str, Any] = {
        "status": "success",
        "action": "edited",
        "project": project.name,
        "relative_path": rel,
        "size_bytes": stat.st_size,
        "occurrences_replaced": occurrences if replace_all else 1,
        "chars_written": (occurrences if replace_all else 1) * len(replace_text),
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }
    if changed_lines:
        result["changed_lines"] = changed_lines
        first, last = changed_lines[0]["start"], changed_lines[0]["end"]
        result["hint"] = (
            f"Edit applied at line {first}." if first == last else f"Edit applied at lines {first}-{last}."
        )
    if include_hunk and hunk is not None:
        result["hunk"] = hunk
    elif include_hunk and changed_lines:
        result["hunk_omitted"] = hunk_omitted or "exceeds_budget"
        first, last = changed_lines[0]["start"], changed_lines[0]["end"]
        result["hint"] = (
            f"{result.get('hint', '')} Hunk omitted ({result['hunk_omitted']}); inspect with "
            f"git_diff or read_file(start_line={first}, end_line={last})."
        ).strip()
    return result


def get_project_file_info(project: ProjectConfig, relative_path: str) -> dict[str, Any]:
    """Retrieve detailed metadata for a file or directory in the project."""
    assert_not_sensitive_path(relative_path, "Inspecting")
    ensure_project_accessible(project)
    target = safe_resolve_path(project.root, relative_path)

    if not target.exists():
        raise FileNotFoundError(f"Path '{relative_path}' does not exist in project '{project.name}'.")

    stat = target.stat()
    rel = str(target.relative_to(project.root)).replace("\\", "/") if target != project.root else ""

    info: dict[str, Any] = {
        "status": "success",
        "project": project.name,
        "relative_path": rel or ".",
        "is_directory": target.is_dir(),
        "is_file": target.is_file(),
        "size_bytes": stat.st_size if target.is_file() else None,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "read_only": project.read_only,
    }

    if target.is_file():
        try:
            if not is_binary_file(target):
                info["line_count"] = count_file_lines_stream(target)
        except Exception:
            pass

    return info


def create_project_directory(project: ProjectConfig, relative_path: str) -> dict[str, Any]:
    """Create a directory (and parents) in the project."""
    if project.read_only:
        raise ReadOnlyProjectError(
            f"Project '{project.name}' is configured as read-only. Cannot create directories."
        )

    ensure_project_accessible(project)
    clean_rel = normalize_relative_path(relative_path)
    assert_not_sensitive_path(clean_rel, "Creating directory")
    target_dir = safe_resolve_path(project.root, clean_rel)

    target_dir.mkdir(parents=True, exist_ok=True)

    rel = str(target_dir.relative_to(project.root)).replace("\\", "/")
    return {
        "status": "success",
        "project": project.name,
        "relative_path": rel,
        "created_at": utc_iso_now(),
    }


def delete_project_file(
    project: ProjectConfig,
    relative_path: str,
    allow_delete: bool = True,
    recursive: bool = False,
) -> dict[str, Any]:
    """Delete a file or directory in the project."""
    if not allow_delete:
        raise FileServiceError("File deletion is disabled by global server configuration.")

    if project.read_only:
        raise ReadOnlyProjectError(
            f"Project '{project.name}' is configured as read-only. Delete operation is rejected."
        )

    clean_rel = normalize_relative_path(relative_path)
    assert_not_sensitive_path(clean_rel, "Deleting")
    ensure_project_accessible(project)
    target = safe_resolve_path(project.root, clean_rel)

    if target == project.root or target.resolve() == project.root.resolve():
        raise PathSecurityError("Refusing to delete the project root directory.")

    if not target.exists():
        raise FileNotFoundError(f"Path '{relative_path}' not found in project '{project.name}'.")

    was_dir = target.is_dir()
    if was_dir:
        if recursive:
            shutil.rmtree(target)
        else:
            try:
                target.rmdir()
            except OSError as e:
                raise FileServiceError(
                    f"Cannot delete directory '{relative_path}': Directory is not empty ({e}). Pass recursive=True to delete non-empty directories."
                ) from e
    else:
        target.unlink()

    return {
        "status": "success",
        "action": "deleted",
        "project": project.name,
        "relative_path": clean_rel,
        "is_directory": was_dir,
        "recursive": recursive,
        "deleted_at": utc_iso_now(),
    }


def move_project_path(
    source_project: ProjectConfig,
    source_relative_path: str,
    target_project: ProjectConfig,
    target_relative_path: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Move or rename a file or directory within a project or across mapped projects."""
    if target_project.read_only:
        raise ReadOnlyProjectError(
            f"Target project '{target_project.name}' is read-only. Move operation rejected."
        )
    if source_project.read_only:
        raise ReadOnlyProjectError(
            f"Source project '{source_project.name}' is read-only. Move operation rejected."
        )

    clean_src = normalize_relative_path(source_relative_path)
    clean_tgt = normalize_relative_path(target_relative_path)

    assert_not_sensitive_path(clean_src, "Moving from")
    assert_not_sensitive_path(clean_tgt, "Moving to")

    ensure_project_accessible(source_project)
    ensure_project_accessible(target_project)

    src_resolved = safe_resolve_path(source_project.root, clean_src)
    tgt_resolved = safe_resolve_path(target_project.root, clean_tgt)

    if not src_resolved.exists():
        raise FileNotFoundError(
            f"Source path '{source_relative_path}' does not exist in project '{source_project.name}'."
        )

    if src_resolved == source_project.root or src_resolved == source_project.root.resolve():
        raise PathSecurityError("Refusing to move the project root directory itself.")

    # Destination resolution: if target path is an existing directory or ends in slash, move inside it
    dest = tgt_resolved
    if tgt_resolved.is_dir() or target_relative_path.endswith("/") or target_relative_path.endswith("\\"):
        dest = tgt_resolved / src_resolved.name

    if dest.exists():
        if not overwrite:
            raise FileExistsError(f"Target path already exists: '{dest}'. Set overwrite=True to replace.")
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_resolved), str(dest))

    tgt_root_res = target_project.root.resolve()
    try:
        final_tgt_rel = str(dest.resolve().relative_to(tgt_root_res)).replace("\\", "/")
    except ValueError:
        final_tgt_rel = os.path.relpath(str(dest.resolve()), str(tgt_root_res)).replace("\\", "/")

    return {
        "status": "success",
        "action": "moved",
        "source_project": source_project.name,
        "source_path": clean_src,
        "target_project": target_project.name,
        "target_path": final_tgt_rel,
        "moved_at": utc_iso_now(),
    }


def read_multiple_project_files(
    project: ProjectConfig,
    relative_paths: list[str],
    encoding: str = "utf-8",
    max_chars_per_file: int = 100_000,
) -> dict[str, Any]:
    """Read contents of multiple files in a single batch operation."""
    ensure_project_accessible(project)
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    total_chars = 0

    for rel in relative_paths:
        clean_rel = normalize_relative_path(rel)
        try:
            content = read_project_file(
                project=project,
                relative_path=clean_rel,
                encoding=encoding,
                max_chars=max_chars_per_file,
            )
            results[clean_rel] = content
            total_chars += len(content) if isinstance(content, str) else len(str(content.get("content", "")))
        except FileSizeLimitError as e:
            errors[clean_rel] = (
                f"{e} File is too large for batch reading. "
                f"Please inspect targeted segments using read_file with start_line and end_line."
            )
        except Exception as e:
            errors[clean_rel] = str(e)

    return {
        "status": "success" if not errors else ("partial_success" if results else "error"),
        "project": project.name,
        "files_read": len(results),
        "total_chars": total_chars,
        "contents": results,
        "errors": errors,
    }


def copy_project_path(
    source_project: ProjectConfig,
    source_relative_path: str,
    target_project: ProjectConfig,
    target_relative_path: str = "",
    overwrite: bool = False,
    recursive: bool = True,
    ignored_patterns: list[str] | None = None,
    allow_copy: bool = True,
) -> dict[str, Any]:
    """Copy a file or directory within a project or across mapped projects.

    Guarantees:
    - Verifies allow_copy permission switch.
    - Both source and target are strictly checked against project boundaries.
    - Rejects write if target project is configured as read_only.
    - Skips specified ignored patterns (e.g. .git, node_modules) during directory copy.
    """
    if not allow_copy:
        raise PermissionDeniedError("Cross-path or project copying is disabled by server configuration.")

    if target_project.read_only:
        raise ReadOnlyProjectError(
            f"Target project '{target_project.name}' is read-only. Copy operation rejected."
        )

    assert_not_sensitive_path(source_relative_path, "Copying from")
    assert_not_sensitive_path(target_relative_path, "Copying to")

    ensure_project_accessible(source_project)
    ensure_project_accessible(target_project)

    src_resolved = safe_resolve_path(source_project.root, source_relative_path)
    tgt_resolved = safe_resolve_path(target_project.root, target_relative_path)

    if not src_resolved.exists():
        raise FileNotFoundError(
            f"Source path '{source_relative_path}' does not exist in project '{source_project.name}'."
        )

    files_copied = 0
    dirs_copied = 0
    bytes_copied = 0

    if src_resolved.is_file():
        # Destination resolution
        dest_file = tgt_resolved
        if tgt_resolved.is_dir() or target_relative_path.endswith("/") or target_relative_path.endswith("\\"):
            dest_file = tgt_resolved / src_resolved.name

        if dest_file.exists():
            if dest_file.is_dir():
                raise FileServiceError(f"Cannot overwrite directory '{dest_file.name}' with file.")
            if not overwrite:
                raise FileExistsError(f"Target file already exists: '{dest_file}'. Specify overwrite=True.")

        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_resolved, dest_file)
        files_copied = 1
        bytes_copied = dest_file.stat().st_size
    else:
        # Directory recursive copy
        if not recursive:
            raise FileServiceError(
                f"Source '{source_relative_path}' is a directory. Set recursive=True to copy directories."
            )

        dest_dir = tgt_resolved
        # If target path exists and is a directory and differs from target root, place source directory inside it
        if dest_dir.exists() and dest_dir != target_project.root and src_resolved.name != dest_dir.name:
            dest_dir = dest_dir / src_resolved.name

        dest_dir.mkdir(parents=True, exist_ok=True)

        user_ignored = list(ignored_patterns or [])
        ignore_mgr = get_project_ignore_manager(source_project, fallback_ignored_patterns=user_ignored)

        for current_root, dirs, files in os.walk(src_resolved):
            cur_p = Path(current_root)
            # Filter subdirs in place
            filtered_dirs: list[str] = []
            for d in dirs:
                dir_full = cur_p / d
                try:
                    rel_d = str(dir_full.relative_to(source_project.root)).replace("\\", "/")
                except ValueError:
                    rel_d = d
                if ignore_mgr.is_ignored(rel_d, is_dir=True, caller_ignored=None, check_parents=False):
                    continue
                filtered_dirs.append(d)
            dirs[:] = filtered_dirs

            rel_to_src = cur_p.relative_to(src_resolved)
            target_curr = dest_dir / rel_to_src
            target_curr.mkdir(parents=True, exist_ok=True)
            dirs_copied += 1

            for f in files:
                file_path = cur_p / f
                try:
                    rel_file = str(file_path.relative_to(source_project.root)).replace("\\", "/")
                except ValueError:
                    rel_file = f
                if ignore_mgr.is_ignored(rel_file, is_dir=False, caller_ignored=None, check_parents=False):
                    continue
                src_f = cur_p / f
                tgt_f = target_curr / f

                if tgt_f.exists() and not overwrite:
                    continue

                shutil.copy2(src_f, tgt_f)
                files_copied += 1
                bytes_copied += tgt_f.stat().st_size

    return {
        "status": "success",
        "source_project": source_project.name,
        "source_path": str(src_resolved.relative_to(source_project.root)).replace("\\", "/") or ".",
        "target_project": target_project.name,
        "target_path": str(tgt_resolved.relative_to(target_project.root)).replace("\\", "/") or ".",
        "files_copied": files_copied,
        "dirs_copied": dirs_copied,
        "bytes_copied": bytes_copied,
    }


def read_project_binary_file(
    project: ProjectConfig,
    relative_path: str,
    max_bytes: int = 10_000_000,
) -> dict[str, Any]:
    """Read a binary file (images, binaries, documents) and return Base64 payload with SHA256 and MIME metadata."""
    assert_not_sensitive_path(relative_path, "Reading binary")
    ensure_project_accessible(project)
    file_path = safe_resolve_path(project.root, relative_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"File not found: '{relative_path}' in project '{project.name}' (Root: {project.root})"
        )
    if file_path.is_dir():
        raise FileServiceError(
            f"Cannot read '{relative_path}': Path is a directory, not a regular file."
        )

    file_size = file_path.stat().st_size
    if file_size > max_bytes:
        raise FileSizeLimitError(
            f"Binary file '{relative_path}' ({file_size} bytes) exceeds safety limit (max {max_bytes} bytes)."
        )

    raw = file_path.read_bytes()
    import base64
    import hashlib
    import mimetypes

    mime, _ = mimetypes.guess_type(str(file_path))
    sha256 = hashlib.sha256(raw).hexdigest()
    b64_str = base64.b64encode(raw).decode("ascii")

    return {
        "status": "success",
        "project": project.name,
        "relative_path": str(file_path.relative_to(project.root)).replace("\\", "/"),
        "size_bytes": file_size,
        "mime_type": mime or "application/octet-stream",
        "sha256": sha256,
        "content_base64": b64_str,
    }


def write_project_binary_file(
    project: ProjectConfig,
    relative_path: str,
    content_base64: str,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Write or overwrite a binary file using a Base64 encoded payload."""
    if project.read_only:
        raise ReadOnlyProjectError(
            f"Project '{project.name}' is configured as read-only. Write operation is rejected."
        )

    assert_not_sensitive_path(relative_path, "Writing binary")
    ensure_project_accessible(project)
    file_path = safe_resolve_path(project.root, relative_path)

    if file_path == project.root:
        raise FileServiceError("Cannot write to the project root directory itself as a file.")

    if file_path.exists():
        if file_path.is_dir():
            raise FileServiceError(f"Cannot overwrite directory '{relative_path}' with a binary file.")
        if not overwrite:
            raise FileExistsError(
                f"File '{relative_path}' already exists in project '{project.name}' and overwrite is False."
            )

    import base64
    import hashlib

    try:
        raw_bytes = base64.b64decode(content_base64)
    except Exception as e:
        raise ValueError(f"Invalid Base64 payload provided: {e}") from e

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(raw_bytes)

    stat = file_path.stat()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    return {
        "status": "success",
        "action": "written",
        "project": project.name,
        "relative_path": str(file_path.relative_to(project.root)).replace("\\", "/"),
        "size_bytes": stat.st_size,
        "sha256": sha256,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


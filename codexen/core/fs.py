"""Core filesystem operations: read, write, edit snippets, list, delete, and copy."""
from __future__ import annotations

from datetime import datetime, timezone
import fnmatch
import os
from pathlib import Path
import shutil
from typing import Any

from ..config import ProjectConfig
from ..exceptions import (
    FileServiceError,
    FileSizeLimitError,
    PathSecurityError,
    ReadOnlyProjectError,
)
from .security import (
    assert_not_sensitive_path,
    ensure_project_accessible,
    is_sensitive_path,
    normalize_relative_path,
    safe_resolve_path,
)


def utc_iso_now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


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


def list_project_files(
    project: ProjectConfig,
    subpath: str = "",
    recursive: bool = False,
    max_depth: int = 5,
    ignored_patterns: list[str] | None = None,
    max_entries: int = 1_000,
    pattern: str = "",
    extension: str = "",
) -> list[dict[str, Any]]:
    """List files and directories inside a project.
    
    Supports optional pattern (glob) and extension filtering.
    """
    ensure_project_accessible(project)
    clean_subpath = normalize_relative_path(subpath)
    target_dir = safe_resolve_path(project.root, clean_subpath)

    if not target_dir.exists():
        raise FileServiceError(f"Target directory does not exist: '{subpath}' in project '{project.name}'")
    if not target_dir.is_dir():
        raise FileServiceError(f"Target path is a file, not a directory: '{subpath}' in project '{project.name}'")

    ignored = ignored_patterns or []
    results: list[dict[str, Any]] = []
    root_resolved = project.root.resolve()
    clean_pattern = pattern.strip()
    clean_ext = extension.strip()
    if clean_ext and not clean_ext.startswith("."):
        clean_ext = f".{clean_ext}"

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
            if matches_any_pattern(name, ignored) or is_sensitive_path(name) or is_sensitive_path(rel):
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
    return results


def read_project_file(
    project: ProjectConfig,
    relative_path: str,
    encoding: str = "utf-8",
    max_chars: int = 1_000_000,
    offset: int = 0,
    limit: int = 0,
    start_line: int = 0,
    end_line: int = 0,
) -> str:
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

    if limit == 0 and end_line == 0 and file_size > max_chars * 4:
        raise FileSizeLimitError(
            f"File '{relative_path}' ({file_size} bytes) exceeds safety limit (max {max_chars} chars)."
        )

    try:
        content = file_path.read_text(encoding=encoding, errors="replace")
    except Exception as e:
        raise FileServiceError(f"Failed to read file '{relative_path}' with encoding '{encoding}': {e}") from e

    # Line-based slicing takes precedence if specified
    if start_line > 0 or end_line > 0:
        lines = content.splitlines(keepends=True)
        s = max(0, start_line - 1)
        e = end_line if end_line > 0 else len(lines)
        return "".join(lines[s:e])

    # Character offset / limit slicing
    if offset > 0 or limit > 0:
        end = offset + limit if limit > 0 else len(content)
        return content[offset:end]

    if len(content) > max_chars:
        raise FileSizeLimitError(
            f"File '{relative_path}' has {len(content)} characters, exceeding the max allowed limit of {max_chars} characters."
        )

    return content


def write_project_file(
    project: ProjectConfig,
    relative_path: str,
    content: str,
    overwrite: bool = True,
    append: bool = False,
    encoding: str = "utf-8",
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

    try:
        if append:
            with open(file_path, "a", encoding=encoding) as f:
                f.write(content)
        else:
            file_path.write_text(content, encoding=encoding)
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
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def edit_project_file_snippet(
    project: ProjectConfig,
    relative_path: str,
    search_text: str,
    replace_text: str,
    replace_all: bool = False,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Replace an exact substring snippet in a project file."""
    if project.read_only:
        raise ReadOnlyProjectError(
            f"Project '{project.name}' is configured as read-only. Edit operation is rejected."
        )

    assert_not_sensitive_path(relative_path, "Editing")

    if not search_text:
        raise FileServiceError("search_text cannot be empty.")

    current_content = read_project_file(project, relative_path, encoding=encoding)

    count = current_content.count(search_text)
    if count == 0:
        raise FileServiceError(
            f"search_text not found in file '{relative_path}'. Edit aborted."
        )

    if count > 1 and not replace_all:
        raise FileServiceError(
            f"search_text occurs {count} times in '{relative_path}'. Specify replace_all=True or provide more unique context."
        )

    new_content = current_content.replace(search_text, replace_text, -1 if replace_all else 1)
    return write_project_file(project, relative_path, new_content, overwrite=True, encoding=encoding)


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
            if stat.st_size <= 2_000_000:
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
                info["line_count"] = len(lines)
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
    target_dir = safe_resolve_path(project.root, relative_path)
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

    src_root_res = source_project.root.resolve()
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
    max_chars_per_file: int = 1_000_000,
) -> dict[str, Any]:
    """Read contents of multiple files in a single batch operation."""
    ensure_project_accessible(project)
    results: dict[str, str] = {}
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
            total_chars += len(content)
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
) -> dict[str, Any]:
    """Copy a file or directory within a project or across mapped projects.

    Guarantees:
    - Both source and target are strictly checked against project boundaries.
    - Rejects write if target project is configured as read_only.
    - Skips specified ignored patterns (e.g. .git, node_modules) during directory copy.
    """
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

    ignored = ignored_patterns or []
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

        for current_root, dirs, files in os.walk(src_resolved):
            cur_p = Path(current_root)
            # Filter subdirs in place
            dirs[:] = [d for d in dirs if not matches_any_pattern(d, ignored)]

            rel_to_src = cur_p.relative_to(src_resolved)
            target_curr = dest_dir / rel_to_src
            target_curr.mkdir(parents=True, exist_ok=True)
            dirs_copied += 1

            for f in files:
                rel_f_path = str(rel_to_src / f).replace("\\", "/")
                if matches_any_pattern(f, ignored) or is_sensitive_path(f) or is_sensitive_path(rel_f_path):
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


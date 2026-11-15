"""Code search engine across project files with token conservation and DoS protection."""
from __future__ import annotations

import os
from pathlib import Path
import re
import time
from typing import Any

from ..config import ProjectConfig
from ..exceptions import FileServiceError
from .fs import is_binary_file, matches_any_pattern
from .security import ensure_project_accessible, is_sensitive_path, safe_resolve_path

# Comprehensive baseline of ignored patterns for high-density build and cache directories
DEFAULT_SEARCH_IGNORED: list[str] = [
    # VCS & metadata
    ".git", ".svn", ".hg", ".bzr",
    # Build & compilation outputs
    "target", "dist", "build", "out", "bin", "obj", ".next", ".nuxt", ".output",
    # Package managers & dependencies
    "node_modules", ".pnpm", ".pnpm-store", "bower_components", "vendor",
    # Virtual environments
    ".venv", "venv", "env",
    # Caches & temporary folders
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".cache",
    ".tmp", "tmp", "temp",
    # IDE & tool configs
    ".idea", ".vscode",
    # Binary, bytecode & compiled artifacts
    "*.pyc", "*.pyo", "*.pyd", "*.exe", "*.dll", "*.so", "*.dylib", "*.pdb", "*.bin",
    # Huge lockfiles & sourcemaps
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "Cargo.lock",
    "*.map", "*.min.js", "*.min.css",
]

MAX_SEARCH_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2MB
MAX_SEARCH_LINE_CHARS = 5000                   # Defend against minified single-line bundles
MAX_SEARCH_FILES_SCANNED = 10_000              # Global scan limit to prevent infinite runaways
SEARCH_TIMEOUT_SECONDS = 15.0                  # Hard safety timeout per search execution


def search_project_code(
    project: ProjectConfig,
    query: str,
    subpath: str = "",
    is_regex: bool = False,
    case_sensitive: bool = False,
    file_extensions: list[str] | None = None,
    ignored_patterns: list[str] | None = None,
    max_results: int = 200,
    max_file_size_bytes: int = MAX_SEARCH_FILE_SIZE_BYTES,
    max_files_scanned: int = MAX_SEARCH_FILES_SCANNED,
    timeout_seconds: float = SEARCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Search code across files in a project with high efficiency and token conservation."""
    ensure_project_accessible(project)
    target_dir = safe_resolve_path(project.root, subpath)

    if not target_dir.exists():
        raise FileNotFoundError(f"Path '{subpath}' not found in project '{project.name}'.")

    if not query:
        raise FileServiceError("Search query cannot be empty.")

    flags = 0 if case_sensitive else re.IGNORECASE
    if is_regex:
        try:
            pattern = re.compile(query, flags)
        except re.error as e:
            raise FileServiceError(f"Invalid regular expression '{query}': {e}") from e
    else:
        escaped = re.escape(query)
        pattern = re.compile(escaped, flags)

    # Merge caller ignored patterns with default baseline protection
    user_ignored = list(ignored_patterns or [])
    combined_ignored = list(dict.fromkeys(user_ignored + DEFAULT_SEARCH_IGNORED))

    exts = [e.lower() if e.startswith(".") else f".{e.lower()}" for e in (file_extensions or []) if e]

    matches: list[dict[str, Any]] = []
    files_scanned = 0
    start_time = time.monotonic()
    limit_reached = False
    timed_out = False

    for root, dirs, files in os.walk(target_dir):
        cur_p = Path(root)

        # Smart directory pruning: filter both directory name and relative path
        filtered_dirs: list[str] = []
        for d in dirs:
            dir_full = cur_p / d
            try:
                rel_d = str(dir_full.relative_to(project.root)).replace("\\", "/")
            except ValueError:
                rel_d = d

            if (
                matches_any_pattern(d, combined_ignored)
                or matches_any_pattern(rel_d, combined_ignored)
                or is_sensitive_path(d)
                or is_sensitive_path(rel_d)
            ):
                continue
            filtered_dirs.append(d)

        dirs[:] = filtered_dirs

        for f in files:
            # Check timeout
            if (time.monotonic() - start_time) > timeout_seconds:
                timed_out = True
                limit_reached = True
                break

            # Check scan limit
            if files_scanned >= max_files_scanned:
                limit_reached = True
                break

            file_path = cur_p / f
            try:
                rel_file = str(file_path.relative_to(project.root)).replace("\\", "/")
            except ValueError:
                rel_file = f

            if (
                matches_any_pattern(f, combined_ignored)
                or matches_any_pattern(rel_file, combined_ignored)
                or is_sensitive_path(f)
                or is_sensitive_path(rel_file)
            ):
                continue

            if exts and file_path.suffix.lower() not in exts:
                continue

            # Skip large files beyond size limit
            try:
                if file_path.stat().st_size > max_file_size_bytes:
                    continue
            except OSError:
                continue

            # Skip binary files
            if is_binary_file(file_path):
                continue

            files_scanned += 1

            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                    for line_num, line in enumerate(fh, start=1):
                        searchable = line[:MAX_SEARCH_LINE_CHARS] if len(line) > MAX_SEARCH_LINE_CHARS else line
                        m = pattern.search(searchable)
                        if m:
                            matches.append({
                                "file": rel_file,
                                "line_number": line_num,
                                "line_content": line.strip()[:300],  # cap single line display
                                "match_span": list(m.span()),
                            })
                            if len(matches) >= max_results:
                                limit_reached = True
                                break
            except Exception:
                continue

            if limit_reached:
                break

        if limit_reached:
            break

    elapsed_ms = round((time.monotonic() - start_time) * 1000, 2)

    return {
        "status": "success",
        "project": project.name,
        "query": query,
        "is_regex": is_regex,
        "files_scanned": files_scanned,
        "total_matches": len(matches),
        "limit_reached": limit_reached or (len(matches) >= max_results),
        "timed_out": timed_out,
        "duration_ms": elapsed_ms,
        "matches": matches,
    }

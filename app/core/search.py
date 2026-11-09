"""Code search engine across project files with token conservation."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from ..config import ProjectConfig
from ..exceptions import FileServiceError
from .fs import is_binary_file, matches_any_pattern
from .security import ensure_project_accessible, is_sensitive_path, safe_resolve_path


def search_project_code(
    project: ProjectConfig,
    query: str,
    subpath: str = "",
    is_regex: bool = False,
    case_sensitive: bool = False,
    file_extensions: list[str] | None = None,
    ignored_patterns: list[str] | None = None,
    max_results: int = 200,
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

    ignored = ignored_patterns or []
    exts = [e.lower() if e.startswith(".") else f".{e.lower()}" for e in (file_extensions or [])]

    matches: list[dict[str, Any]] = []
    files_scanned = 0

    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if not matches_any_pattern(d, ignored)]
        cur_p = Path(root)

        for f in files:
            file_path = cur_p / f
            rel_file = str(file_path.relative_to(project.root)).replace("\\", "/")
            if matches_any_pattern(f, ignored) or is_sensitive_path(f) or is_sensitive_path(rel_file):
                continue

            if exts and file_path.suffix.lower() not in exts:
                continue

            # Skip binary files
            if is_binary_file(file_path):
                continue

            files_scanned += 1

            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                    for line_num, line in enumerate(fh, start=1):
                        m = pattern.search(line)
                        if m:
                            matches.append({
                                "file": rel_file,
                                "line_number": line_num,
                                "line_content": line.strip()[:300],  # cap single line display
                                "match_span": list(m.span()),
                            })
                            if len(matches) >= max_results:
                                break
            except Exception:
                continue

            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break

    return {
        "status": "success",
        "project": project.name,
        "query": query,
        "is_regex": is_regex,
        "files_scanned": files_scanned,
        "total_matches": len(matches),
        "limit_reached": len(matches) >= max_results,
        "matches": matches,
    }

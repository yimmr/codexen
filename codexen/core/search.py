"""Code search engine across project files with token conservation and DoS protection."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from ..config import ProjectConfig
from ..exceptions import FileServiceError
from .fs import is_binary_file
from .gitignore import get_project_ignore_manager, is_path_authorized, log_penetration_audit
from .security import (
    assert_not_sensitive_path,
    ensure_project_accessible,
    safe_resolve_path,
)

# Legacy compatibility baseline. Runtime pruning is driven by
# BASELINE_FALLBACK_IGNORED / project .gitignore in core/gitignore.py.
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
    fallback_ignored_patterns: list[str] | None = None,
    max_results: int = 200,
    max_file_size_bytes: int = MAX_SEARCH_FILE_SIZE_BYTES,
    max_file_size_mb: int = 2,
    max_files_scanned: int = MAX_SEARCH_FILES_SCANNED,
    timeout_seconds: float = SEARCH_TIMEOUT_SECONDS,
    confirmed: bool = False,
    authorized_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Search code across files in a project with high efficiency and token conservation."""
    ensure_project_accessible(project)
    # Credentials/VCS metadata are absolutely blocked, even with confirmed=True.
    if subpath:
        assert_not_sensitive_path(subpath, "Searching")
    target_dir = safe_resolve_path(project.root, subpath)

    if not target_dir.exists():
        raise FileNotFoundError(f"Path '{subpath}' not found in project '{project.name}'.")

    if not query:
        raise FileServiceError("Search query cannot be empty.")

    if max_file_size_mb <= 0:
        max_file_size_bytes = 2 * 1024 * 1024
    else:
        # Cap single file limit safely between 1MB and 50MB to prevent accidental OOM
        effective_mb = max(1, min(max_file_size_mb, 50))
        max_file_size_bytes = effective_mb * 1024 * 1024

    flags = 0 if case_sensitive else re.IGNORECASE
    if is_regex:
        try:
            pattern = re.compile(query, flags)
        except re.error as e:
            raise FileServiceError(f"Invalid regular expression '{query}': {e}") from e
    else:
        escaped = re.escape(query)
        pattern = re.compile(escaped, flags)

    # Initialize dynamic ignore manager following RFC 0001 rules
    user_ignored = list(ignored_patterns or [])
    ignore_mgr = get_project_ignore_manager(
        project,
        fallback_ignored_patterns=fallback_ignored_patterns if fallback_ignored_patterns is not None else user_ignored,
    )

    clean_subpath = subpath.replace("\\", "/").strip("/")
    clean_authorized = [p.replace("\\", "/").strip("/") for p in (authorized_paths or []) if p.strip()]

    # HITL Penetration Interception Protocol (RFC 0001 Phase 3 & RFC contract compliance)
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
                            f"re-call search_code with confirmed=True and authorized_paths=['{clean_subpath}']."
                        )
                    elif rule_source == "global_fallback":
                        reason_msg = (
                            f"The directory '{clean_subpath}' is excluded by global fallback patterns ({matched_pat}). "
                            f"You MUST ask the user in chat for confirmation. Once approved, "
                            f"re-call search_code with confirmed=True and authorized_paths=['{clean_subpath}']."
                        )
                    elif rule_source == "project_config":
                        reason_msg = (
                            f"The directory '{clean_subpath}' is excluded by project configuration ({matched_pat}). "
                            f"You MUST ask the user in chat for confirmation. Once approved, "
                            f"re-call search_code with confirmed=True and authorized_paths=['{clean_subpath}']."
                        )
                    else:
                        reason_msg = (
                            f"The directory '{clean_subpath}' is excluded ({source_display}: {matched_pat}). "
                            f"You MUST ask the user in chat for confirmation. Once approved, "
                            f"re-call search_code with confirmed=True and authorized_paths=['{clean_subpath}']."
                        )
                else:
                    reason_msg = (
                        f"Path '{clean_subpath}' is excluded by {source_display} ({matched_pat}) and not covered by authorized_paths {clean_authorized}. "
                        f"You MUST ask the user in chat for confirmation. Once approved, "
                        f"re-call search_code with confirmed=True and authorized_paths=['{clean_subpath}']."
                    )

                result_payload = {
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
                return result_payload

    exts = [e.lower() if e.startswith(".") else f".{e.lower()}" for e in (file_extensions or []) if e]

    matches: list[dict[str, Any]] = []
    files_scanned = 0
    start_time = time.monotonic()
    limit_reached = False
    timed_out = False

    large_files_skipped: list[dict[str, Any]] = []
    truncated_lines_count = 0
    directories_pruned: list[str] = []

    effective_authorized = clean_authorized if confirmed else None

    for root, dirs, files in os.walk(target_dir):
        cur_p = Path(root)

        # Smart directory pruning using RFC 0001 dynamic ignore engine
        filtered_dirs: list[str] = []
        for d in dirs:
            dir_full = cur_p / d
            try:
                rel_d = str(dir_full.relative_to(project.root)).replace("\\", "/")
            except ValueError:
                rel_d = d

            if ignore_mgr.is_ignored(
                rel_d,
                is_dir=True,
                caller_ignored=user_ignored,
                check_parents=False,
                authorized_paths=effective_authorized,
            ):
                if len(directories_pruned) < 100 and rel_d not in directories_pruned:
                    directories_pruned.append(rel_d)
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

            if ignore_mgr.is_ignored(
                rel_file,
                is_dir=False,
                caller_ignored=user_ignored,
                check_parents=False,
                authorized_paths=effective_authorized,
            ):
                continue

            if exts and file_path.suffix.lower() not in exts:
                continue

            # Skip large files beyond size limit
            try:
                st_size = file_path.stat().st_size
                if st_size > max_file_size_bytes:
                    suggested_mb = min(50, int(st_size / (1024 * 1024)) + 1)
                    limit_mb = round(max_file_size_bytes / (1024 * 1024))
                    large_files_skipped.append({
                        "file": rel_file,
                        "size_bytes": st_size,
                        "size_mb": round(st_size / (1024 * 1024), 2),
                        "reason": (
                            f"Exceeded max_file_size_mb limit ({limit_mb}MB). "
                            f"To inspect this file, re-run search_code with max_file_size_mb={suggested_mb}, "
                            f"or inspect targeted segments using read_file with start_line and end_line."
                        ),
                    })
                    continue
            except OSError:
                continue

            # Skip binary files
            if is_binary_file(file_path):
                continue

            files_scanned += 1

            try:
                with open(file_path, encoding="utf-8", errors="replace") as fh:
                    for line_num, line in enumerate(fh, start=1):
                        if line_num % 1000 == 0 and (time.monotonic() - start_time) > timeout_seconds:
                            timed_out = True
                            limit_reached = True
                            break

                        if len(line) > MAX_SEARCH_LINE_CHARS:
                            truncated_lines_count += 1
                            searchable = line[:MAX_SEARCH_LINE_CHARS]
                        else:
                            searchable = line
                        m = pattern.search(searchable)
                        if m:
                            full_stripped = line.strip()
                            is_truncated = len(full_stripped) > 300
                            matches.append({
                                "file": rel_file,
                                "line_number": line_num,
                                "line_content": full_stripped[:300],
                                "line_truncated": is_truncated,
                                "line_full_length": len(line),
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

    # Log successful penetration audit trail if authorization was granted
    if confirmed and clean_authorized:
        log_penetration_audit(
            project_name=project.name,
            requested_path=clean_subpath or "*",
            status="authorized_executed",
            authorized_paths=clean_authorized,
            confirmed=True,
            duration_ms=elapsed_ms,
            files_scanned=files_scanned,
            matches_count=len(matches),
        )

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
        "search_audit": {
            "large_files_skipped": large_files_skipped,
            "truncated_lines_count": truncated_lines_count,
            "directories_pruned": directories_pruned,
        },
    }

"""Code search and file discovery tools."""
from __future__ import annotations

from typing import Any

from pydantic import Field

from ..core import normalize_relative_path, search_project_code
from .decorators import ToolContext


def register_search_tools(ctx: ToolContext) -> None:
    """Register code search and query tools."""

    @ctx.register_tool()
    def search_code(
        project_id: str = "",
        project_name: str = "",
        query: str = "",
        subpath: str = "",
        path: str = "",
        is_regex: bool = False,
        case_sensitive: bool = False,
        file_extensions: list[str] = Field(default_factory=list),
        max_results: int = 200,
        max_file_size_mb: int = 2,
        confirmed: bool = False,
        authorized_paths: list[str] = Field(default_factory=list),
    ) -> dict[str, Any]:
        """High-performance code search across files in a project (conserves 90%+ LLM context tokens).

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            query: Substring or regular expression to search for.
            subpath: Subdirectory to scope the search into (default: root).
            path: Alias for subpath.
            is_regex: Whether query is a regular expression (default: False).
            case_sensitive: Whether match should be case-sensitive (default: False).
            file_extensions: List of extensions to include (e.g. ['.py', '.yaml', '.ts']).
            max_results: Maximum matches to return (default: 200).
            max_file_size_mb: Maximum single file size in megabytes to inspect (default: 2MB; effective range 1-50MB; <=0 falls back to 2MB).
            confirmed: Whether the user confirmed penetration of ignored directories.
            authorized_paths: Whitelisted paths explicitly authorized by the user for penetration.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_subpath = normalize_relative_path(subpath or path)
        exts = list(file_extensions) if isinstance(file_extensions, (list, tuple, set)) else []
        auth_paths = list(authorized_paths) if isinstance(authorized_paths, (list, tuple, set)) else []

        return search_project_code(
            project=proj,
            query=query,
            subpath=target_subpath,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            file_extensions=exts,
            fallback_ignored_patterns=ctx.config_mgr.settings.ignored_patterns,
            max_results=min(max_results, ctx.config_mgr.settings.max_search_results),
            max_file_size_mb=max_file_size_mb,
            confirmed=confirmed,
            authorized_paths=auth_paths,
        )

    ctx.register_alias(
        search_code,
        "search_files",
        "Alias for search_code. Search text or regex within project files.",
    )
    ctx.register_alias(
        search_code,
        "find_in_files",
        "Alias for search_code.",
    )

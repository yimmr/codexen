"""File read, write, edit snippet, and metadata inspection tools."""
from __future__ import annotations

from typing import Any

from ..core import (
    edit_project_file_snippet,
    get_project_file_info,
    normalize_relative_path,
)
from ..core import (
    read_project_file as _read_core,
)
from ..core import (
    write_project_file as _write_core,
)
from .decorators import ToolContext


def register_file_crud_tools(ctx: ToolContext) -> None:
    """Register core file text operations."""

    @ctx.register_tool()
    def read_project_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        encoding: str = "utf-8",
        offset: int = 0,
        limit: int = 0,
        start_line: int = 0,
        end_line: int = 0,
    ) -> dict[str, Any] | str:
        """Read a UTF-8 text file within a project. The path cannot escape the project root boundary.

        Args:
            project_id: The ID/name of the configured project (e.g. 'literary_workflow_v1').
            project_name: Alias for project_id.
            relative_path: Path to the file, relative to project root (e.g. 'config/settings.yaml').
            path: Alias for relative_path.
            encoding: Text encoding (default: 'utf-8').
            offset: Optional character offset to start reading from (default: 0).
            limit: Optional maximum number of characters to read (default: 0 for full file).
            start_line: Optional 1-based start line number for range reading.
            end_line: Optional 1-based end line number (inclusive) for range reading.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return _read_core(
            project=proj,
            relative_path=target_path,
            encoding=encoding or ctx.config_mgr.settings.default_encoding,
            max_chars=ctx.config_mgr.settings.max_read_chars,
            offset=offset,
            limit=limit,
            start_line=start_line,
            end_line=end_line,
            auto_paginate=True,
        )

    ctx.register_alias(
        read_project_file,
        "read_file",
        "Alias for read_project_file. Read full or partial text content of a project file.",
    )

    @ctx.register_tool()
    def write_project_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        content: str = "",
        overwrite: bool = True,
        append: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """Write text content to a project file. Automatically creates parent directories.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            relative_path: Target relative path in project.
            path: Alias for relative_path.
            content: Full text content to write.
            overwrite: Whether to overwrite existing file (default: True).
            append: Whether to append to existing file instead of rewriting (default: False).
            encoding: Text encoding (default: 'utf-8').
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return _write_core(
            project=proj,
            relative_path=target_path,
            content=content,
            overwrite=overwrite,
            append=append,
            encoding=encoding or ctx.config_mgr.settings.default_encoding,
        )

    ctx.register_alias(
        write_project_file,
        "write_file",
        "Alias for write_project_file. Write text content to a project file.",
    )

    @ctx.register_tool()
    def edit_file_snippet(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        search_text: str = "",
        replace_text: str = "",
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """Replace a specific text snippet in an existing project file without rewriting the entire file.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            relative_path: Path to the file, relative to the project root.
            path: Alias for relative_path.
            search_text: The exact substring to locate.
            replace_text: The new replacement string.
            replace_all: If True, replaces all occurrences; if False, replaces only if exactly one occurrence exists.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return edit_project_file_snippet(
            project=proj,
            relative_path=target_path,
            search_text=search_text,
            replace_text=replace_text,
            replace_all=replace_all,
            encoding=ctx.config_mgr.settings.default_encoding,
        )

    @ctx.register_tool()
    def get_file_info(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
    ) -> dict[str, Any]:
        """Get metadata about a file or directory in a configured project (size, modified time, line count).

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            relative_path: Path to the file or directory, relative to project root.
            path: Alias for relative_path.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return get_project_file_info(project=proj, relative_path=target_path)

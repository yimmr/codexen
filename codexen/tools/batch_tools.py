"""Batch file reading tools."""
from __future__ import annotations

from typing import Any

from pydantic import Field

from ..core import read_multiple_project_files
from .decorators import ToolContext


def register_batch_tools(ctx: ToolContext) -> None:
    """Register batch file reading tools."""

    @ctx.register_tool()
    def read_multiple_files(
        project_id: str = "",
        project_name: str = "",
        relative_paths: list[str] = Field(default_factory=list),
        paths: list[str] = Field(default_factory=list),
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """Read contents of multiple files within a project in a single batch call.

        NOTICE: Intended for small to medium files. If a file exceeds max_read_chars, an actionable error will be reported in the errors dictionary advising the use of read_file with start_line and end_line.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            relative_paths: List of relative file paths to read.
            paths: Alias for relative_paths.
            encoding: Text encoding (default: 'utf-8').
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        clean_rel = list(relative_paths) if isinstance(relative_paths, (list, tuple, set)) else []
        clean_p = list(paths) if isinstance(paths, (list, tuple, set)) else []
        target_paths = clean_rel or clean_p or []
        return read_multiple_project_files(
            project=proj,
            relative_paths=target_paths,
            encoding=encoding or ctx.config_mgr.settings.default_encoding,
            max_chars_per_file=ctx.config_mgr.settings.max_read_chars,
        )

    ctx.register_alias(
        read_multiple_files,
        "batch_read_files",
        "Alias for read_multiple_files. Read contents of multiple files within a project.",
    )

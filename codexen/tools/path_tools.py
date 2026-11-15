"""Path listing, directory creation, deletion, copy, and move tools."""
from __future__ import annotations

from typing import Any, cast

from mcp_server_core import ToolResultListDict
from pydantic import Field

from ..core import (
    copy_project_path,
    create_project_directory,
    delete_project_file,
    move_project_path,
    normalize_relative_path,
)
from ..core import (
    list_project_files as _list_project_files_core,
)
from .decorators import ToolContext


def register_path_tools(ctx: ToolContext) -> None:
    """Register directory and path operations tools."""

    @ctx.register_tool()
    def list_files(
        project_id: str = "",
        project_name: str = "",
        subpath: str = "",
        relative_path: str = "",
        path: str = "",
        recursive: bool = False,
        max_depth: int = 5,
        pattern: str = "",
        extension: str = "",
        confirmed: bool = False,
        authorized_paths: list[str] = Field(default_factory=list),
    ) -> dict[str, Any]:
        """List files and subdirectories inside a configured project.

        Returns an atomic dictionary envelope containing all matched files to guarantee
        zero-truncation across intermediate MCP gateways and LLM proxies:
        - status: "success"
        - total: Total number of files and directories found
        - count: Alias for total
        - project_id: ID of the inspected project
        - subpath: Relative path inspected
        - files: List of file and directory metadata
        - entries: Alias for files

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            subpath: Relative path inside the project root to inspect (defaults to root: "").
            relative_path: Alias for subpath.
            path: Alias for subpath.
            recursive: Whether to list nested files recursively (default: False).
            max_depth: Maximum directory depth when recursive is True (default: 5).
            pattern: Optional wildcard/glob pattern to filter filenames (e.g. '*.py', '*test*').
            extension: Optional extension filter (e.g. '.py' or 'py').
            confirmed: Whether the user confirmed penetration of ignored directories.
            authorized_paths: Whitelisted paths explicitly authorized by the user for penetration.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_subpath = normalize_relative_path(subpath or relative_path or path)
        auth_paths = list(authorized_paths) if isinstance(authorized_paths, (list, tuple, set)) else []
        limit = max(0, int(ctx.config_mgr.settings.max_list_entries))

        raw_result = _list_project_files_core(
            project=proj,
            subpath=target_subpath,
            recursive=recursive,
            max_depth=max_depth,
            pattern=pattern,
            extension=extension,
            max_entries=limit + 1 if limit > 0 else 0,
            confirmed=confirmed,
            authorized_paths=auth_paths,
        )

        # RFC 0001 Phase 3: HITL penetration interception envelope
        if (
            isinstance(raw_result, list)
            and len(raw_result) == 1
            and isinstance(raw_result[0], dict)
            and raw_result[0].get("status") == "needs_confirmation"
        ):
            hitl_info = dict(raw_result[0])
            hitl_info.setdefault("files", [])
            hitl_info.setdefault("total", 0)
            return hitl_info

        items: list[dict[str, Any]] = list(raw_result) if isinstance(raw_result, list) else []
        is_truncated = (limit > 0) and (len(items) > limit)
        if is_truncated:
            items = items[:limit]

        extra_meta: dict[str, Any] = {
            "truncated": is_truncated,
            "limit_reached": is_truncated,
            "max_entries": limit,
        }
        if is_truncated:
            extra_meta["hint"] = (
                f"Directory listing reached maximum limit ({limit} entries). "
                f"Use 'subpath', 'pattern', or 'extension' to narrow the scope."
            )

        return cast(
            dict[str, Any],
            ToolResultListDict(
                items,
                key="files",
                project_id=proj.name,
                subpath=target_subpath or ".",
                **extra_meta,
            ),
        )

    ctx.register_alias(
        list_files,
        "list_project_files",
        "Alias for list_files. List files and subdirectories inside a configured project.",
    )
    ctx.register_alias(
        list_files,
        "list_directory",
        "Alias for list_files.",
    )

    @ctx.register_tool()
    def create_directory(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
    ) -> dict[str, Any]:
        """Create a new directory (and any necessary parent directories) in a configured project.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            relative_path: Relative directory path to create.
            path: Alias for relative_path.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return create_project_directory(project=proj, relative_path=target_path)

    @ctx.register_tool()
    def delete_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        recursive: bool = False,
    ) -> dict[str, Any]:
        """Delete a file or directory in a configured project.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            relative_path: Relative path to the file or directory to remove.
            path: Alias for relative_path.
            recursive: If True, recursively removes directory contents (default: False).
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return delete_project_file(
            project=proj,
            relative_path=target_path,
            allow_delete=ctx.config_mgr.settings.allow_delete,
            recursive=recursive,
        )

    ctx.register_alias(delete_file, "delete_directory", "Alias for delete_file for directories.")

    @ctx.register_tool()
    def copy_path(
        source_project_id: str = "",
        source_project: str = "",
        source_path: str = "",
        target_project_id: str = "",
        target_project: str = "",
        target_path: str = "",
        overwrite: bool = False,
        recursive: bool = True,
    ) -> dict[str, Any]:
        """Copy a file or directory within a project or across two mapped projects.

        Args:
            source_project_id: Project ID of the source.
            source_project: Alias for source_project_id.
            source_path: Relative path to the file or directory in the source project.
            target_project_id: Project ID of destination. If omitted, copies within source_project_id.
            target_project: Alias for target_project_id.
            target_path: Relative destination path in the target project.
            overwrite: Whether to overwrite existing files (default: False).
            recursive: Whether to recursively copy subdirectories (default: True).
        """
        src_id = (source_project_id or source_project).strip()
        tgt_id = (target_project_id or target_project or src_id).strip()
        src_proj = ctx.resolve_project(project_id=src_id)
        tgt_proj = ctx.resolve_project(project_id=tgt_id)

        return copy_project_path(
            source_project=src_proj,
            source_relative_path=normalize_relative_path(source_path),
            target_project=tgt_proj,
            target_relative_path=normalize_relative_path(target_path),
            overwrite=overwrite,
            recursive=recursive,
            ignored_patterns=ctx.config_mgr.settings.ignored_patterns,
            allow_copy=ctx.config_mgr.settings.allow_copy_path,
        )

    @ctx.register_tool()
    def copy_directory(
        source_project_id: str = "",
        source_project: str = "",
        source_path: str = "",
        target_project_id: str = "",
        target_project: str = "",
        target_path: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Alias for copy_path with recursive=True. Recursively copies directory tree."""
        return cast(
            dict[str, Any],
            copy_path(
                source_project_id=source_project_id,
                source_project=source_project,
                source_path=source_path,
                target_project_id=target_project_id,
                target_project=target_project,
                target_path=target_path,
                overwrite=overwrite,
                recursive=True,
            ),
        )

    @ctx.register_tool()
    def copy_file(
        source_project_id: str = "",
        source_project: str = "",
        source_path: str = "",
        target_project_id: str = "",
        target_project: str = "",
        target_path: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Alias for copy_path for single files."""
        return cast(
            dict[str, Any],
            copy_path(
                source_project_id=source_project_id,
                source_project=source_project,
                source_path=source_path,
                target_project_id=target_project_id,
                target_project=target_project,
                target_path=target_path,
                overwrite=overwrite,
                recursive=False,
            ),
        )

    @ctx.register_tool()
    def move_path(
        source_project_id: str = "",
        source_project: str = "",
        source_path: str = "",
        target_project_id: str = "",
        target_project: str = "",
        target_path: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Move or rename a file or directory within a project or across mapped projects.

        Args:
            source_project_id: Project ID of the source.
            source_project: Alias for source_project_id.
            source_path: Relative path to the file or directory to move/rename.
            target_project_id: Project ID of destination (defaults to source_project_id).
            target_project: Alias for target_project_id.
            target_path: Relative destination path in target project.
            overwrite: Whether to overwrite existing destination (default: False).
        """
        src_id = (source_project_id or source_project).strip()
        tgt_id = (target_project_id or target_project or src_id).strip()
        src_proj = ctx.resolve_project(project_id=src_id)
        tgt_proj = ctx.resolve_project(project_id=tgt_id)

        return move_project_path(
            source_project=src_proj,
            source_relative_path=source_path,
            target_project=tgt_proj,
            target_relative_path=target_path,
            overwrite=overwrite,
        )

    ctx.register_alias(move_path, "move_file", "Alias for move_path.")

    @ctx.register_tool()
    def rename_file(
        project_id: str = "",
        project_name: str = "",
        source_path: str = "",
        target_path: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Rename a file or directory within a project.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            source_path: Current relative path.
            target_path: New relative path.
            overwrite: Whether to overwrite existing file (default: False).
        """
        return cast(
            dict[str, Any],
            move_path(
                source_project_id=project_id,
                source_project=project_name,
                source_path=source_path,
                target_project_id=project_id or project_name,
                target_path=target_path,
                overwrite=overwrite,
            ),
        )

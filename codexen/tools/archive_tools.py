"""ZIP archive creation and extraction tools."""
from __future__ import annotations

from typing import Any

from ..core import create_project_zip, extract_project_zip, normalize_relative_path
from .decorators import ToolContext


def register_archive_tools(ctx: ToolContext) -> None:
    """Register ZIP archive tools."""

    @ctx.register_tool()
    def create_zip_archive(
        project_id: str = "",
        project_name: str = "",
        source_subpath: str = "",
        output_zip_name: str = "",
    ) -> dict[str, Any]:
        """Package a project or subfolder into a compressed ZIP archive.

        Automatically ignores heavy cache and dependency directories (.git, node_modules, __pycache__, .venv).

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            source_subpath: Subfolder or file to archive (defaults to entire project root).
            output_zip_name: Name of the generated zip file (default: {project_id}_{timestamp}.zip).
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_subpath = normalize_relative_path(source_subpath)
        return create_project_zip(
            project=proj,
            source_subpath=target_subpath,
            output_zip_name=output_zip_name,
            ignored_patterns=ctx.config_mgr.settings.ignored_patterns,
            allow_zip=ctx.config_mgr.settings.allow_zip_operations,
        )

    @ctx.register_tool()
    def extract_zip_archive(
        project_id: str = "",
        project_name: str = "",
        zip_relative_path: str = "",
        target_subpath: str = "",
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Extract a ZIP archive into a destination subfolder with strict Zip-Slip defenses.

        Rejects any archive containing path traversal (../), absolute paths, or zip bombs.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            zip_relative_path: Relative path to the .zip archive inside the project.
            target_subpath: Relative path of destination directory to extract into.
            overwrite: Whether to overwrite existing files (default: True).
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        clean_zip = normalize_relative_path(zip_relative_path)
        clean_dest = normalize_relative_path(target_subpath)
        return extract_project_zip(
            project=proj,
            zip_relative_path=clean_zip,
            target_subpath=clean_dest,
            overwrite=overwrite,
            max_size_bytes=ctx.config_mgr.settings.max_zip_size_bytes,
            allow_zip=ctx.config_mgr.settings.allow_zip_operations,
        )

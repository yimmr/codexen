"""Git repository status, diff inspection, and log tools."""
from __future__ import annotations

from typing import Any

from ..core import (
    get_project_git_diff,
    get_project_git_log,
    get_project_git_status,
    normalize_relative_path,
)
from .decorators import ToolContext


def register_git_tools(ctx: ToolContext) -> None:
    """Register Git integration tools."""

    @ctx.register_tool()
    def git_status(project_id: str = "", project_name: str = "") -> dict[str, Any]:
        """Inspect git repository status (working tree modifications, staged files, branch) in a project.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        return get_project_git_status(proj)

    @ctx.register_tool()
    def git_diff(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        staged: bool = False,
    ) -> dict[str, Any]:
        """View git diff for working tree or staged changes in a project, enabling Agent self-review.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            relative_path: Specific relative file path to view diff for (default: "" for all files).
            path: Alias for relative_path.
            staged: Whether to inspect staged changes (--cached) (default: False).
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return get_project_git_diff(project=proj, relative_path=target_path, staged=staged)

    @ctx.register_tool()
    def git_log(
        project_id: str = "",
        project_name: str = "",
        max_commits: int = 20,
        relative_path: str = "",
        path: str = "",
    ) -> dict[str, Any]:
        """Inspect recent git commit history for a project or specific file.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            max_commits: Number of commits to retrieve (default: 20, max: 100).
            relative_path: File path to filter commits for (default: "" for entire repository).
            path: Alias for relative_path.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return get_project_git_log(project=proj, max_commits=max_commits, relative_path=target_path)

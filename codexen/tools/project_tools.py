"""Project mapping, inspection, and lifecycle tools."""
from __future__ import annotations

from typing import Any, cast

from mcp_server_core import ToolResultListDict

from .decorators import ToolContext


def register_project_tools(ctx: ToolContext) -> None:
    """Register project-level MCP tools."""

    @ctx.register_tool()
    def list_projects() -> dict[str, Any]:
        """List all configured and registered projects in the workspace.

        Returns metadata for each mapped project in an atomic dictionary envelope
        to prevent gateway-level list truncation:
        - status: "success"
        - total: Number of registered projects
        - count: Alias for total
        - projects: List of project metadata dictionaries
        - entries: Alias for projects
        """
        proj_list = [proj.to_dict() for proj in ctx.config_mgr.projects.values()]
        return cast(dict[str, Any], ToolResultListDict(proj_list, key="projects"))

    @ctx.register_tool()
    def get_project_info(
        project_id: str = "",
        project_name: str = "",
    ) -> dict[str, Any]:
        """Get configuration and metadata for a specific mapped project.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        res = proj.to_dict()
        res["status"] = "success"
        return res

    ctx.register_alias(get_project_info, "get_project", "Alias for get_project_info.")

    @ctx.register_tool()
    def reload_projects_config() -> dict[str, Any]:
        """Reload the projects configuration file (config.yaml) and rescan the workspace.

        Call this tool if you modified config.yaml or added new projects in workspace
        while the server is running to immediately apply changes without restarting.
        """
        return ctx.config_mgr.reload()

    @ctx.register_tool()
    def create_project(
        project_id: str = "",
        project_name: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Create a new project directory under the configured workspace root and register it immediately.

        Args:
            project_id: Unique identifier for the project (alphanumeric, -, _).
            project_name: Alias for project_id.
            description: Brief description of the new project.
        """
        pid = (project_id or project_name).strip()
        if not pid:
            raise ValueError("project_id or project_name is required.")

        proj = ctx.config_mgr.create_and_register_project(
            project_id=pid,
            description=description,
            persist=True,
        )
        return {
            "status": "success",
            "project_id": proj.name,
            "root": str(proj.root),
            "description": proj.description,
            "message": f"Project '{pid}' successfully created and registered under {proj.root}.",
        }

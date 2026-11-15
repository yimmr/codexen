"""Terminal command execution tools."""
from __future__ import annotations

from typing import Any

from ..core import normalize_relative_path, run_project_command
from .decorators import ToolContext


def register_command_tools(ctx: ToolContext) -> None:
    """Register terminal command execution tools."""

    @ctx.register_tool()
    def run_terminal_command(
        project_id: str = "",
        project_name: str = "",
        command: str = "",
        subpath: str = "",
        path: str = "",
        timeout_seconds: int = 60,
        confirmed: bool = False,
        require_confirmation: bool = False,
    ) -> dict[str, Any]:
        """Execute a controlled terminal command inside a project directory (e.g. pytest, git, npm, tsc).

        Security & Human-in-the-Loop Protocol:
        - Safe dev commands (git, pytest, python, npm, yarn, cargo, go, make, ruff, tsc) run directly.
        - Destructive or environment-changing commands (rmdir, del, git push, git reset --hard, npm publish,
          or unknown binaries) return status='needs_confirmation'. You MUST ask the user in chat for
          confirmation. Once the user approves, re-call this tool with confirmed=True.
        - System-level destructive commands (format, diskpart, reg, shutdown, C:\\ wiping) are strictly blocked.
        - Execution is anchored strictly to the project root directory.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            command: The command line to execute (e.g. 'pytest -v', 'git status', 'python -m unittest').
            subpath: Relative directory within the project to run in (default: project root).
            path: Alias for subpath.
            timeout_seconds: Maximum run duration in seconds (default: 60, max: 300).
            confirmed: Pass True after the user has explicitly confirmed execution in chat.
            require_confirmation: Force a confirmation request prompt regardless of command type.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_subpath = normalize_relative_path(subpath or path)
        return run_project_command(
            project=proj,
            command=command,
            settings=ctx.config_mgr.settings,
            subpath=target_subpath,
            timeout_seconds=timeout_seconds,
            confirmed=confirmed,
            require_confirmation=require_confirmation,
        )

    ctx.register_alias(
        run_terminal_command,
        "run_command",
        "Alias for run_terminal_command. Execute a controlled terminal command inside a project directory.",
    )

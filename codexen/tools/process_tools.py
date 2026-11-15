"""Long-running background process and interactive terminal session tools."""
from __future__ import annotations

from typing import Any, cast

from mcp_server_core import ToolResultListDict

from ..core import global_process_manager, normalize_relative_path
from .decorators import ToolContext


def register_process_tools(ctx: ToolContext) -> None:
    """Register background process management tools."""

    @ctx.register_tool()
    def start_background_process(
        project_id: str = "",
        project_name: str = "",
        command: str = "",
        subpath: str = "",
        path: str = "",
        confirmed: bool = False,
        require_confirmation: bool = False,
    ) -> dict[str, Any]:
        """Start a long-running or interactive background process (dev server, test watcher, CLI session).

        Returns a unique process_id immediately. You can subsequently read output via
        read_process_output, send stdin inputs via send_process_input, or terminate via stop_background_process.

        Args:
            project_id: Project identifier.
            project_name: Alias for project_id.
            command: Terminal command to launch in background.
            subpath: Relative directory within the project to run in.
            path: Alias for subpath.
            confirmed: Pass True if confirmed by the user.
            require_confirmation: Force a confirmation request.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_subpath = normalize_relative_path(subpath or path)
        return global_process_manager.start_process(
            project=proj,
            command=command,
            settings=ctx.config_mgr.settings,
            subpath=target_subpath,
            confirmed=confirmed,
            require_confirmation=require_confirmation,
        )

    @ctx.register_tool()
    def read_process_output(
        process_id: str = "",
        max_chars: int = 50_000,
        clear_buffer: bool = True,
    ) -> dict[str, Any]:
        """Read pending or accumulated terminal output from a background process.

        Args:
            process_id: Unique process identifier returned by start_background_process.
            max_chars: Maximum characters to retrieve (default: 50000).
            clear_buffer: Whether to clear retrieved lines from buffer (default: True).
        """
        return global_process_manager.read_output(
            process_id=process_id,
            max_chars=max_chars,
            clear_buffer=clear_buffer,
        )

    @ctx.register_tool()
    def send_process_input(
        process_id: str = "",
        input_text: str = "",
        endline: bool = True,
    ) -> dict[str, Any]:
        """Send interactive input or response to a running background process standard input.

        Args:
            process_id: Process identifier.
            input_text: Text/command to write to stdin.
            endline: Whether to automatically append newline (default: True).
        """
        return global_process_manager.send_input(
            process_id=process_id,
            input_text=input_text,
            endline=endline,
        )

    @ctx.register_tool()
    def stop_background_process(
        process_id: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        """Terminate a background process and its entire subprocess tree cleanly.

        Args:
            process_id: Process identifier.
            force: Force kill immediately if True.
        """
        return global_process_manager.stop_process(
            process_id=process_id,
            force=force,
        )

    @ctx.register_tool()
    def list_background_processes(
        project_id: str = "",
        project_name: str = "",
    ) -> dict[str, Any]:
        """List all background processes, their execution status, PID, and runtime info.

        Args:
            project_id: Optional project to filter by.
            project_name: Alias for project_id.
        """
        target = (project_id or "").strip() or (project_name or "").strip()
        items = global_process_manager.list_processes(project_name=target)
        return cast(dict[str, Any], ToolResultListDict(items, key="processes"))

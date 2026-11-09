"""Unified MCP Tools registration for Project File Service.

Compatible with literary_workflow_v1 naming conventions (project_id, read_project_file)
while also providing generic file tool aliases (read_file, write_file, run_command, copy_directory, list_project_files).
"""
from __future__ import annotations

import os
from typing import Any

from .config import ConfigManager
from .core import (
    copy_project_path,
    create_project_directory,
    create_project_zip,
    delete_project_file,
    edit_project_file_snippet,
    extract_project_zip,
    get_project_file_info,
    get_project_git_diff,
    get_project_git_log,
    get_project_git_status,
    global_process_manager,
    list_project_files as _list_project_files_core,
    move_project_path,
    normalize_relative_path,
    read_multiple_project_files,
    read_project_binary_file as _read_binary_core,
    read_project_file as _read_core,
    run_project_command,
    safe_resolve_path,
    search_project_code,
    write_project_binary_file as _write_binary_core,
    write_project_file as _write_core,
)

try:
    from mcp.server.fastmcp import Image
except (ImportError, ModuleNotFoundError):
    Image = None


class ToolResultListDict(dict):
    """Structured container that ensures atomic, single-block MCP response serialization while preserving list semantics.

    FastMCP converts any returned list into multiple separate TextContent blocks in the MCP
    protocol response. When intermediate MCP gateways or LLM clients parse the response,
    many gateways only inspect the last TextContent block, resulting in truncation (e.g.
    only returning the last file).

    ToolResultListDict inherits from dict so FastMCP serializes the ENTIRE structure
    into a single TextContent JSON payload:
    {"status": "success", "count": N, "total": N, "<key>": items, "entries": items}

    Simultaneously, for Python unit tests and backward compatibility, it implements
    list-like iteration (__iter__), indexing (__getitem__ for int), length (__len__),
    and membership (__contains__).
    """

    def __init__(
        self,
        items: list[Any],
        key: str = "items",
        status: str = "success",
        **extra: Any,
    ) -> None:
        super().__init__()
        self["status"] = status
        self["total"] = len(items)
        self["count"] = len(items)
        self[key] = items
        if key != "entries":
            self["entries"] = items
        for k, v in extra.items():
            self[k] = v
        self._items = items

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, int):
            return self._items[item]
        return super().__getitem__(item)

    def __contains__(self, item: Any) -> bool:
        try:
            if super().__contains__(item):
                return True
        except TypeError:
            pass
        return item in self._items


def register_file_tools(mcp: Any, config_mgr: ConfigManager) -> dict[str, Any]:
    """Register all file operations, project management, and developer tools onto FastMCP."""

    def _resolve_project(project_id: str | None = None, project_name: str | None = None):
        target = (project_id or "").strip() or (project_name or "").strip()
        if not target:
            available = list(config_mgr.projects.keys())
            raise ValueError(f"Missing required 'project_id' or 'project_name'. Available projects: {available}")
        return config_mgr.get_project(target)

    @mcp.tool()
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
        proj_list = [proj.to_dict() for proj in config_mgr.projects.values()]
        return ToolResultListDict(proj_list, key="projects")

    @mcp.tool()
    def get_project_info(
        project_id: str = "",
        project_name: str = "",
    ) -> dict[str, Any]:
        """Get configuration and metadata for a specific mapped project.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
        """
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        res = proj.to_dict()
        res["status"] = "success"
        return res

    @mcp.tool()
    def get_project(
        project_id: str = "",
        project_name: str = "",
    ) -> dict[str, Any]:
        """Alias for get_project_info."""
        return get_project_info(project_id=project_id, project_name=project_name)

    @mcp.tool()
    def reload_projects_config() -> dict[str, Any]:
        """Reload the projects configuration file (config.yaml) and rescan the workspace.

        Call this tool if you modified config.yaml or added new projects in workspace
        while the server is running to immediately apply changes without restarting.
        """
        return config_mgr.reload()

    @mcp.tool()
    def create_project(
        project_id: str = "",
        project_name: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Create a new project directory under the workspace root (D:\\works) and register it immediately.

        Args:
            project_id: Unique identifier for the project (alphanumeric, -, _).
            project_name: Alias for project_id.
            description: Brief description of the new project.
        """
        pid = (project_id or project_name).strip()
        if not pid:
            raise ValueError("project_id or project_name is required.")

        proj = config_mgr.create_and_register_project(
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

    @mcp.tool()
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
        """
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_subpath = subpath or relative_path or path
        items = _list_project_files_core(
            project=proj,
            subpath=target_subpath,
            recursive=recursive,
            max_depth=max_depth,
            ignored_patterns=config_mgr.settings.ignored_patterns,
            max_entries=config_mgr.settings.max_list_entries,
            pattern=pattern,
            extension=extension,
        )
        return ToolResultListDict(
            items,
            key="files",
            project_id=proj.name,
            subpath=target_subpath,
            recursive=recursive,
        )

    @mcp.tool()
    def list_project_files(
        project_id: str = "",
        project_name: str = "",
        subpath: str = "",
        relative_path: str = "",
        path: str = "",
        recursive: bool = False,
        max_depth: int = 5,
        pattern: str = "",
        extension: str = "",
    ) -> dict[str, Any]:
        """Alias for list_files (conforms with read_project_file / write_project_file naming)."""
        return list_files(
            project_id=project_id,
            project_name=project_name,
            subpath=subpath,
            relative_path=relative_path,
            path=path,
            recursive=recursive,
            max_depth=max_depth,
            pattern=pattern,
            extension=extension,
        )

    @mcp.tool()
    def list_directory(
        project_id: str = "",
        project_name: str = "",
        subpath: str = "",
        relative_path: str = "",
        path: str = "",
        recursive: bool = False,
        max_depth: int = 5,
        pattern: str = "",
        extension: str = "",
    ) -> dict[str, Any]:
        """Alias for list_files."""
        return list_files(
            project_id=project_id,
            project_name=project_name,
            subpath=subpath,
            relative_path=relative_path,
            path=path,
            recursive=recursive,
            max_depth=max_depth,
            pattern=pattern,
            extension=extension,
        )

    @mcp.tool()
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
    ) -> str:
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return _read_core(
            project=proj,
            relative_path=target_path,
            encoding=encoding or config_mgr.settings.default_encoding,
            max_chars=config_mgr.settings.max_read_chars,
            offset=offset,
            limit=limit,
            start_line=start_line,
            end_line=end_line,
        )

    @mcp.tool()
    def read_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        encoding: str = "utf-8",
        offset: int = 0,
        limit: int = 0,
        start_line: int = 0,
        end_line: int = 0,
    ) -> str:
        """Alias for read_project_file. Read full or partial text content of a project file."""
        return read_project_file(
            project_id=project_id,
            project_name=project_name,
            relative_path=relative_path,
            path=path,
            encoding=encoding,
            offset=offset,
            limit=limit,
            start_line=start_line,
            end_line=end_line,
        )

    @mcp.tool()
    def read_multiple_files(
        project_id: str = "",
        project_name: str = "",
        relative_paths: list[str] | None = None,
        paths: list[str] | None = None,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """Read contents of multiple files within a project in a single batch call.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            relative_paths: List of relative file paths to read.
            paths: Alias for relative_paths.
            encoding: Text encoding (default: 'utf-8').
        """
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_paths = relative_paths or paths or []
        return read_multiple_project_files(
            project=proj,
            relative_paths=target_paths,
            encoding=encoding or config_mgr.settings.default_encoding,
            max_chars_per_file=config_mgr.settings.max_read_chars,
        )

    @mcp.tool()
    def batch_read_files(
        project_id: str = "",
        project_name: str = "",
        relative_paths: list[str] | None = None,
        paths: list[str] | None = None,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """Alias for read_multiple_files."""
        return read_multiple_files(
            project_id=project_id,
            project_name=project_name,
            relative_paths=relative_paths,
            paths=paths,
            encoding=encoding,
        )

    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return _write_core(
            project=proj,
            relative_path=target_path,
            content=content,
            overwrite=overwrite,
            append=append,
            encoding=encoding or config_mgr.settings.default_encoding,
        )

    @mcp.tool()
    def write_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        content: str = "",
        overwrite: bool = True,
        append: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """Alias for write_project_file. Write text content to a project file."""
        return write_project_file(
            project_id=project_id,
            project_name=project_name,
            relative_path=relative_path,
            path=path,
            content=content,
            overwrite=overwrite,
            append=append,
            encoding=encoding,
        )

    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return edit_project_file_snippet(
            project=proj,
            relative_path=target_path,
            search_text=search_text,
            replace_text=replace_text,
            replace_all=replace_all,
            encoding=config_mgr.settings.default_encoding,
        )

    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return get_project_file_info(project=proj, relative_path=target_path)

    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return create_project_directory(project=proj, relative_path=target_path)

    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return delete_project_file(
            project=proj,
            relative_path=target_path,
            allow_delete=config_mgr.settings.allow_delete,
            recursive=recursive,
        )

    @mcp.tool()
    def delete_directory(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        recursive: bool = False,
    ) -> dict[str, Any]:
        """Alias for delete_file for directories."""
        return delete_file(
            project_id=project_id,
            project_name=project_name,
            relative_path=relative_path,
            path=path,
            recursive=recursive,
        )

    @mcp.tool()
    def copy_path(
        source_project_id: str = "",
        source_project: str = "",
        source_path: str = "",
        target_project_id: str | None = None,
        target_project: str | None = None,
        target_path: str = "",
        overwrite: bool = False,
        recursive: bool = True,
    ) -> dict[str, Any]:
        """Copy a file or directory within a project or across two mapped projects.

        Enables copying templates, libraries, or scaffolding from one project (e.g. literary_workflow_v1)
        into another project safely without escaping project root boundaries.

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
        src_proj = _resolve_project(project_id=src_id)
        tgt_proj = _resolve_project(project_id=tgt_id)

        return copy_project_path(
            source_project=src_proj,
            source_relative_path=normalize_relative_path(source_path),
            target_project=tgt_proj,
            target_relative_path=normalize_relative_path(target_path),
            overwrite=overwrite,
            recursive=recursive,
            ignored_patterns=config_mgr.settings.ignored_patterns,
        )

    @mcp.tool()
    def copy_directory(
        source_project_id: str = "",
        source_project: str = "",
        source_path: str = "",
        target_project_id: str | None = None,
        target_project: str | None = None,
        target_path: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Alias for copy_path with recursive=True. Recursively copies directory tree."""
        return copy_path(
            source_project_id=source_project_id,
            source_project=source_project,
            source_path=source_path,
            target_project_id=target_project_id,
            target_project=target_project,
            target_path=target_path,
            overwrite=overwrite,
            recursive=True,
        )

    @mcp.tool()
    def copy_file(
        source_project_id: str = "",
        source_project: str = "",
        source_path: str = "",
        target_project_id: str | None = None,
        target_project: str | None = None,
        target_path: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Alias for copy_path for single files."""
        return copy_path(
            source_project_id=source_project_id,
            source_project=source_project,
            source_path=source_path,
            target_project_id=target_project_id,
            target_project=target_project,
            target_path=target_path,
            overwrite=overwrite,
            recursive=False,
        )

    @mcp.tool()
    def move_path(
        source_project_id: str = "",
        source_project: str = "",
        source_path: str = "",
        target_project_id: str | None = None,
        target_project: str | None = None,
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
        src_proj = _resolve_project(project_id=src_id)
        tgt_proj = _resolve_project(project_id=tgt_id)

        return move_project_path(
            source_project=src_proj,
            source_relative_path=source_path,
            target_project=tgt_proj,
            target_relative_path=target_path,
            overwrite=overwrite,
        )

    @mcp.tool()
    def move_file(
        source_project_id: str = "",
        source_project: str = "",
        source_path: str = "",
        target_project_id: str | None = None,
        target_project: str | None = None,
        target_path: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Alias for move_path."""
        return move_path(
            source_project_id=source_project_id,
            source_project=source_project,
            source_path=source_path,
            target_project_id=target_project_id,
            target_project=target_project,
            target_path=target_path,
            overwrite=overwrite,
        )

    @mcp.tool()
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
        return move_path(
            source_project_id=project_id,
            source_project=project_name,
            source_path=source_path,
            target_project_id=project_id or project_name,
            target_path=target_path,
            overwrite=overwrite,
        )

    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_subpath = normalize_relative_path(source_subpath)
        return create_project_zip(
            project=proj,
            source_subpath=target_subpath,
            output_zip_name=output_zip_name,
            ignored_patterns=config_mgr.settings.ignored_patterns,
        )

    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        clean_zip = normalize_relative_path(zip_relative_path)
        clean_dest = normalize_relative_path(target_subpath)
        return extract_project_zip(
            project=proj,
            zip_relative_path=clean_zip,
            target_subpath=clean_dest,
            overwrite=overwrite,
            max_size_bytes=config_mgr.settings.max_zip_size_bytes,
        )

    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_subpath = normalize_relative_path(subpath or path)
        return run_project_command(
            project=proj,
            command=command,
            settings=config_mgr.settings,
            subpath=target_subpath,
            timeout_seconds=timeout_seconds,
            confirmed=confirmed,
            require_confirmation=require_confirmation,
        )

    @mcp.tool()
    def run_command(
        project_id: str = "",
        project_name: str = "",
        command: str = "",
        subpath: str = "",
        path: str = "",
        timeout_seconds: int = 60,
        confirmed: bool = False,
        require_confirmation: bool = False,
    ) -> dict[str, Any]:
        """Alias for run_terminal_command."""
        return run_terminal_command(
            project_id=project_id,
            project_name=project_name,
            command=command,
            subpath=subpath,
            path=path,
            timeout_seconds=timeout_seconds,
            confirmed=confirmed,
            require_confirmation=require_confirmation,
        )

    @mcp.tool()
    def search_code(
        project_id: str = "",
        project_name: str = "",
        query: str = "",
        subpath: str = "",
        path: str = "",
        is_regex: bool = False,
        case_sensitive: bool = False,
        file_extensions: list[str] | None = None,
        max_results: int = 200,
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
        """
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_subpath = normalize_relative_path(subpath or path)
        return search_project_code(
            project=proj,
            query=query,
            subpath=target_subpath,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            file_extensions=file_extensions,
            ignored_patterns=config_mgr.settings.ignored_patterns,
            max_results=min(max_results, config_mgr.settings.max_search_results),
        )

    @mcp.tool()
    def search_files(
        project_id: str = "",
        project_name: str = "",
        query: str = "",
        subpath: str = "",
        path: str = "",
        is_regex: bool = False,
        case_sensitive: bool = False,
        file_extensions: list[str] | None = None,
        max_results: int = 200,
    ) -> dict[str, Any]:
        """Alias for search_code. Search text or regex within project files."""
        return search_code(
            project_id=project_id,
            project_name=project_name,
            query=query,
            subpath=subpath,
            path=path,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            file_extensions=file_extensions,
            max_results=max_results,
        )

    @mcp.tool()
    def find_in_files(
        project_id: str = "",
        project_name: str = "",
        query: str = "",
        subpath: str = "",
        path: str = "",
        is_regex: bool = False,
        case_sensitive: bool = False,
        file_extensions: list[str] | None = None,
        max_results: int = 200,
    ) -> dict[str, Any]:
        """Alias for search_code."""
        return search_code(
            project_id=project_id,
            project_name=project_name,
            query=query,
            subpath=subpath,
            path=path,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            file_extensions=file_extensions,
            max_results=max_results,
        )

    @mcp.tool()
    def git_status(project_id: str = "", project_name: str = "") -> dict[str, Any]:
        """Inspect git repository status (working tree modifications, staged files, branch) in a project.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
        """
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        return get_project_git_status(proj)

    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return get_project_git_diff(project=proj, relative_path=target_path, staged=staged)

    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return get_project_git_log(project=proj, max_commits=max_commits, relative_path=target_path)

    # --------------------------------------------------------------------------
    # Background Process & Interactive Terminal Tools
    # --------------------------------------------------------------------------
    @mcp.tool()
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
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_subpath = normalize_relative_path(subpath or path)
        return global_process_manager.start_process(
            project=proj,
            command=command,
            settings=config_mgr.settings,
            subpath=target_subpath,
            confirmed=confirmed,
            require_confirmation=require_confirmation,
        )

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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
        return ToolResultListDict(items, key="processes")

    # --------------------------------------------------------------------------
    # Multimodal Media & Binary File Tools
    # --------------------------------------------------------------------------
    @mcp.tool()
    def read_binary_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        max_bytes: int = 10_000_000,
    ) -> dict[str, Any]:
        """Read any binary file (image, binary, document, compiled artifact) encoded in Base64 with SHA256 and MIME metadata.

        Args:
            project_id: Project identifier.
            project_name: Alias for project_id.
            relative_path: Path relative to project root.
            path: Alias for relative_path.
            max_bytes: Safety limit in bytes (default: 10MB).
        """
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return _read_binary_core(project=proj, relative_path=target_path, max_bytes=max_bytes)

    @mcp.tool()
    def read_project_binary_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        max_bytes: int = 10_000_000,
    ) -> dict[str, Any]:
        """Alias for read_binary_file."""
        return read_binary_file(
            project_id=project_id,
            project_name=project_name,
            relative_path=relative_path,
            path=path,
            max_bytes=max_bytes,
        )

    @mcp.tool()
    def write_binary_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        content_base64: str = "",
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Write or overwrite a binary file using a Base64-encoded payload.

        Args:
            project_id: Project identifier.
            project_name: Alias for project_id.
            relative_path: Path relative to project root.
            path: Alias for relative_path.
            content_base64: Raw file content encoded in standard Base64.
            overwrite: Whether to overwrite existing file (default: True).
        """
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return _write_binary_core(
            project=proj,
            relative_path=target_path,
            content_base64=content_base64,
            overwrite=overwrite,
        )

    @mcp.tool()
    def write_project_binary_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        content_base64: str = "",
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Alias for write_binary_file."""
        return write_binary_file(
            project_id=project_id,
            project_name=project_name,
            relative_path=relative_path,
            path=path,
            content_base64=content_base64,
            overwrite=overwrite,
        )

    @mcp.tool()
    def read_media_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
    ) -> Any:
        """Read an image file (PNG, JPG, JPEG, GIF, WEBP) and return it natively for multimodal LLM vision.

        If the MCP client / LLM supports image modalities, this directly renders the image
        in the chat context without requiring manual Base64 decoding.

        Args:
            project_id: Project identifier.
            project_name: Alias for project_id.
            relative_path: Image path relative to project root.
            path: Alias for relative_path.
        """
        proj = _resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        from .core.security import assert_not_sensitive_path, ensure_project_accessible, safe_resolve_path
        assert_not_sensitive_path(target_path, "Reading media")
        ensure_project_accessible(proj)
        file_path = safe_resolve_path(proj.root, target_path)

        if not file_path.exists():
            raise FileNotFoundError(f"Media file not found: '{target_path}' in project '{proj.name}'")
        if file_path.is_dir():
            raise ValueError(f"Target '{target_path}' is a directory, not a media file.")

        suffix = file_path.suffix.lower()
        if Image is not None and suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            return Image(path=file_path)

        # Fallback to binary metadata dictionary
        return _read_binary_core(project=proj, relative_path=target_path)

    @mcp.tool()
    def read_image_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
    ) -> Any:
        """Alias for read_media_file."""
        return read_media_file(
            project_id=project_id,
            project_name=project_name,
            relative_path=relative_path,
            path=path,
        )

    return {
        "list_projects": list_projects,
        "get_project_info": get_project_info,
        "get_project": get_project,
        "reload_projects_config": reload_projects_config,
        "create_project": create_project,
        "read_project_file": read_project_file,
        "write_project_file": write_project_file,
        "read_file": read_file,
        "write_file": write_file,
        "read_multiple_files": read_multiple_files,
        "batch_read_files": batch_read_files,
        "list_files": list_files,
        "list_project_files": list_project_files,
        "list_directory": list_directory,
        "edit_file_snippet": edit_file_snippet,
        "get_file_info": get_file_info,
        "create_directory": create_directory,
        "delete_file": delete_file,
        "delete_directory": delete_directory,
        "copy_path": copy_path,
        "copy_directory": copy_directory,
        "copy_file": copy_file,
        "move_path": move_path,
        "move_file": move_file,
        "rename_file": rename_file,
        "create_zip_archive": create_zip_archive,
        "extract_zip_archive": extract_zip_archive,
        "run_terminal_command": run_terminal_command,
        "run_command": run_command,
        "search_code": search_code,
        "search_files": search_files,
        "find_in_files": find_in_files,
        "git_status": git_status,
        "git_diff": git_diff,
        "git_log": git_log,
        "start_background_process": start_background_process,
        "read_process_output": read_process_output,
        "send_process_input": send_process_input,
        "stop_background_process": stop_background_process,
        "list_background_processes": list_background_processes,
        "read_binary_file": read_binary_file,
        "read_project_binary_file": read_project_binary_file,
        "write_binary_file": write_binary_file,
        "write_project_binary_file": write_project_binary_file,
        "read_media_file": read_media_file,
        "read_image_file": read_image_file,
    }

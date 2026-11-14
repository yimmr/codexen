"""Project File MCP Service package."""
from __future__ import annotations

from .config import ConfigManager, ProjectConfig, ServiceSettings
from .core import (
    copy_project_path,
    create_project_directory,
    create_project_zip,
    delete_project_file,
    edit_project_file_snippet,
    extract_project_zip,
    get_project_file_info,
    list_project_files,
    read_project_file,
    run_project_command,
    safe_resolve_path,
    search_project_code,
    truncate_command_output,
    write_project_file,
)
from .exceptions import (
    CommandExecutionError,
    CommandSecurityError,
    ConfigurationError,
    FileServiceError,
    FileSizeLimitError,
    PathSecurityError,
    ProjectNotFoundError,
    ProjectPathInvalidError,
    ReadOnlyProjectError,
    ZipSlipSecurityError,
)
from .tools import register_file_tools
from .server import mcp

__all__ = [
    "ConfigManager",
    "ProjectConfig",
    "ServiceSettings",
    "FileServiceError",
    "ProjectNotFoundError",
    "ProjectPathInvalidError",
    "PathSecurityError",
    "ZipSlipSecurityError",
    "ReadOnlyProjectError",
    "FileSizeLimitError",
    "ConfigurationError",
    "CommandSecurityError",
    "CommandExecutionError",
    "safe_resolve_path",
    "list_project_files",
    "read_project_file",
    "write_project_file",
    "edit_project_file_snippet",
    "get_project_file_info",
    "create_project_directory",
    "delete_project_file",
    "copy_project_path",
    "create_project_zip",
    "extract_project_zip",
    "run_project_command",
    "search_project_code",
    "truncate_command_output",
    "register_file_tools",
    "mcp",
]

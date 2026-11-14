"""Core file operations, command execution, and security boundaries.

Re-exports fine-grained domain submodules for 100% backward compatibility.
"""
from __future__ import annotations

from .archive import (
    create_project_zip,
    extract_project_zip,
)
from .command import (
    check_command_security,
    kill_process_tree,
    run_project_command,
    truncate_command_output,
)
from .fs import (
    copy_project_path,
    create_project_directory,
    delete_project_file,
    edit_project_file_snippet,
    get_project_file_info,
    is_binary_file,
    list_project_files,
    matches_any_pattern,
    move_project_path,
    read_multiple_project_files,
    read_project_binary_file,
    read_project_file,
    utc_iso_now,
    write_project_binary_file,
    write_project_file,
)
from .process_session import (
    ProcessSession,
    ProcessSessionManager,
    global_process_manager,
)
from .git_ops import (
    get_project_git_diff,
    get_project_git_log,
    get_project_git_status,
)
from .search import (
    search_project_code,
)
from .security import (
    CRITICAL_SENSITIVE_PATTERNS,
    WINDOWS_RESERVED_NAMES,
    assert_not_sensitive_path,
    ensure_project_accessible,
    is_sensitive_path,
    is_windows_reserved,
    normalize_relative_path,
    safe_resolve_path,
)

__all__ = [
    # Security
    "WINDOWS_RESERVED_NAMES",
    "CRITICAL_SENSITIVE_PATTERNS",
    "is_windows_reserved",
    "safe_resolve_path",
    "normalize_relative_path",
    "is_sensitive_path",
    "assert_not_sensitive_path",
    "ensure_project_accessible",
    # Filesystem
    "utc_iso_now",
    "matches_any_pattern",
    "is_binary_file",
    "list_project_files",
    "read_project_file",
    "read_multiple_project_files",
    "read_project_binary_file",
    "write_project_file",
    "write_project_binary_file",
    "edit_project_file_snippet",
    "get_project_file_info",
    "create_project_directory",
    "delete_project_file",
    "copy_project_path",
    "move_project_path",
    # Archive
    "create_project_zip",
    "extract_project_zip",
    # Command & Background Processes
    "truncate_command_output",
    "kill_process_tree",
    "check_command_security",
    "run_project_command",
    "ProcessSession",
    "ProcessSessionManager",
    "global_process_manager",
    # Search
    "search_project_code",
    # Git operations
    "get_project_git_status",
    "get_project_git_diff",
    "get_project_git_log",
]

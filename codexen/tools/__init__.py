"""Unified MCP Tools registration hub for Codexen Project File Service.

Modular architecture delegating to specialized domain modules:
- decorators: Worker thread aspect, telemetry observation, alias registration
- project_tools: Workspace mapping and project metadata
- file_tools: Core text CRUD, snippets, and file metadata
- batch_tools: Multi-file batch reading
- path_tools: File listing, directories, copy, move, delete
- search_tools: Full-text and regex search with dynamic ignore and audit telemetry
- command_tools: Controlled terminal command execution
- process_tools: Long-running background sessions and interactive CLI
- git_tools: Git status, diff, and log review
- archive_tools: Safe ZIP archive packaging and extraction
- binary_tools: Multimodal images and Base64 binary transfers
"""
from __future__ import annotations

from typing import Any

from mcp_server_core import ToolResultListDict

from ..config import ConfigManager
from .archive_tools import register_archive_tools
from .batch_tools import register_batch_tools
from .binary_tools import register_binary_tools
from .command_tools import register_command_tools
from .decorators import ToolContext
from .document_tools import register_document_tools
from .file_tools import register_file_crud_tools
from .git_tools import register_git_tools
from .path_tools import register_path_tools
from .process_tools import register_process_tools
from .project_tools import register_project_tools
from .search_tools import register_search_tools


def register_file_tools(mcp: Any, config_mgr: ConfigManager) -> dict[str, Any]:
    """Register all file operations, project management, and developer tools onto FastMCP."""
    ctx = ToolContext(mcp=mcp, config_mgr=config_mgr)

    # 1. Project mapping & lifecycle
    register_project_tools(ctx)

    # 2. File CRUD operations
    register_file_crud_tools(ctx)

    # 3. Batch reading
    register_batch_tools(ctx)

    # 4. Directory & path operations
    register_path_tools(ctx)

    # 5. Search operations
    register_search_tools(ctx)

    # 6. Terminal commands
    register_command_tools(ctx)

    # 7. Long-running processes
    register_process_tools(ctx)

    # 8. Git review
    register_git_tools(ctx)

    # 9. ZIP archives
    register_archive_tools(ctx)

    # 10. Binary & media tools
    register_binary_tools(ctx)

    # 11. Rich document tools (PDF, DOCX, XLSX, PPTX)
    register_document_tools(ctx)

    return ctx.tools


__all__ = [
    "register_file_tools",
    "ToolResultListDict",
    "ToolContext",
]

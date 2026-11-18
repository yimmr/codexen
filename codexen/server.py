"""Codexen MCP Server definition and tool bindings.

Declares the business FastMCP instance and maps all file, project, and CLI tools.
Lifecycle execution is delegated to the generic mcp_server_core.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

try:
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]
from mcp.server.transport_security import TransportSecuritySettings
from mcp_server_core.env import get_runtime_dir
from mcp_server_core.logging import set_global_payload_hook
from mcp_server_core.oauth import LocalOAuthServer
from mcp_server_core.server import (
    build_mcp_server_app,
)
from mcp_server_core.server import (
    serve_http as framework_serve_http,
)
from mcp_server_core.server import (
    serve_stdio as framework_serve_stdio,
)
from starlette.applications import Starlette

from .config import ConfigManager
from .exceptions import FileServiceError
from .logging_filters import ExpectedFailureFilter
from .tools import register_file_tools

ROOT = Path(__file__).resolve().parents[1]
_error_logger = logging.getLogger("mcp.error")
if not any(isinstance(item, ExpectedFailureFilter) for item in _error_logger.filters):
    _error_logger.addFilter(ExpectedFailureFilter())
load_dotenv(ROOT / ".env")


def codexen_payload_hook(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Payload summary hook for large files and binary content to prevent log bloat."""
    if not isinstance(arguments, dict):
        return None

    modified = dict(arguments)
    changed = False

    # 1. Truncate long content in write_file, write_project_file, edit_file_snippet
    if tool_name in ("write_file", "write_project_file") and "content" in modified:
        content = modified["content"]
        if isinstance(content, str) and len(content) > 300:
            preview = content[:100].replace("\n", " ").replace("\r", "")
            modified["content"] = f"[file_content: {len(content)} chars, preview='{preview}...']"
            changed = True

    if tool_name == "edit_file_snippet":
        for k in ("search_text", "replace_text", "search_block", "replacement_block"):
            if k in modified and isinstance(modified[k], str) and len(modified[k]) > 300:
                snippet = modified[k]
                preview = snippet[:80].replace("\n", " ").replace("\r", "")
                modified[k] = f"[snippet: {len(snippet)} chars, preview='{preview}...']"
                changed = True

    # 2. Binary tools: write_binary_file, write_project_binary_file
    if tool_name in ("write_binary_file", "write_project_binary_file") and "content_base64" in modified:
        b64 = modified["content_base64"]
        if isinstance(b64, str) and len(b64) > 100:
            modified["content_base64"] = f"[base64_data: {len(b64)} chars]"
            changed = True

    # 3. Process session input: send_process_input
    if tool_name == "send_process_input" and "input_text" in modified:
        inp = modified["input_text"]
        if isinstance(inp, str) and len(inp) > 300:
            preview = inp[:100].replace("\n", " ").replace("\r", "")
            modified["input_text"] = f"[input_text: {len(inp)} chars, preview='{preview}...']"
            changed = True

    return modified if changed else None


set_global_payload_hook(codexen_payload_hook)


def create_http_server(
    root: Path | None = None,
    config_mgr: ConfigManager | None = None,
) -> tuple[FastMCP, ConfigManager, LocalOAuthServer]:
    """Create and configure the HTTP FastMCP Server instance with OAuth 2.1 support."""
    base_dir = (root or ROOT).resolve()
    cfg = config_mgr or ConfigManager(base_dir=base_dir)
    http_cfg = cfg.http_settings

    host = http_cfg.host
    if not http_cfg.allow_public_bind and host not in ("127.0.0.1", "localhost"):
        raise FileServiceError(
            "Refusing to bind HTTP MCP server to public interface without allow_public_bind: true"
        )

    db_path = http_cfg.db_path or (get_runtime_dir(base_dir) / "oauth_store.db")
    oauth_server = LocalOAuthServer(admin_password=http_cfg.api_key, db_path=db_path)

    server = FastMCP(
        "Project File Service HTTP",
        host=host,
        port=http_cfg.port,
        streamable_http_path=http_cfg.path,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    # Register all file & project tools (including Agent CLI command execution)
    register_file_tools(server, cfg)

    return server, cfg, oauth_server


def build_starlette_app(
    server: FastMCP,
    config_mgr: ConfigManager,
    oauth_server: LocalOAuthServer,
    require_auth: bool | None = None,
) -> Starlette:
    """Build the final Starlette application with CORS and authentication middlewares."""
    def root_info_builder(request: Any) -> dict[str, Any]:
        projects_summary = [p.to_dict() for p in config_mgr.projects.values()]
        return {
            "status": "online",
            "service": f"{config_mgr.http_settings.display_title} File MCP Service",
            "transport": "Streamable HTTP",
            "mcp_url": f"http://{request.url.netloc}{config_mgr.http_settings.path}",
            "endpoints": {
                "mcp": config_mgr.http_settings.path,
                "health": "/health",
                "oauth_protected_resource": "/.well-known/oauth-protected-resource",
                "oauth_authorization_server": "/.well-known/oauth-authorization-server",
                "oauth_register": "/oauth/register",
                "oauth_authorize": "/oauth/authorize",
                "oauth_token": "/oauth/token",
            },
            "configured_projects": projects_summary,
            "auth_required": config_mgr.http_settings.require_auth,
        }

    return cast(
        Starlette,
        build_mcp_server_app(
            server=server,
            env_cfg=config_mgr.http_settings,
            oauth_server=oauth_server,
            service_name=config_mgr.http_settings.service_name,
            service_title=config_mgr.http_settings.service_title,
            root_info_builder=root_info_builder,
            require_auth=require_auth,
            payload_hook=codexen_payload_hook,
        ),
    )


async def serve(
    config_path: str | Path | None = None,
    host: str | None = None,
    port: int | None = None,
    enable_tunnel: bool | None = None,
) -> None:
    """Run the Codexen HTTP MCP Server using the generic framework runner."""
    cfg = ConfigManager(config_path=config_path, base_dir=ROOT)
    if host:
        cfg.http_settings.host = host
    if port:
        cfg.http_settings.port = port
    if enable_tunnel is not None:
        cfg.http_settings.tunnel = enable_tunnel

    server, cfg, oauth_server = create_http_server(root=ROOT, config_mgr=cfg)

    banner_items = [
        (p.name, f"{'[RO]' if p.read_only else '[RW]'} -> {p.root}")
        for p in cfg.projects.values()
    ]

    await framework_serve_http(
        server=server,
        env_cfg=cfg.http_settings,
        base_dir=ROOT,
        oauth_server=oauth_server,
        service_name=cfg.http_settings.service_name,
        service_title=cfg.http_settings.service_title,
        banner_title=f"{cfg.http_settings.display_title} (Remote Agent Ready)",
        banner_items=banner_items,
        payload_hook=codexen_payload_hook,
    )


def serve_stdio(config_path: str | Path | None = None) -> None:
    """Run Codexen server via Stdio transport."""
    cfg = ConfigManager(config_path=config_path, base_dir=ROOT)
    stdio_server = FastMCP("Codexen File Service Stdio")
    register_file_tools(stdio_server, cfg)
    framework_serve_stdio(
        stdio_server,
        base_dir=ROOT,
        service_name=cfg.http_settings.service_name,
        payload_hook=codexen_payload_hook,
    )


def run() -> None:
    """Backward-compatible alias for serve_stdio."""
    serve_stdio()


# ==============================================================================
# Module-level instances and tool exposures (for tests and stdio introspection)
# ==============================================================================
config_manager = ConfigManager(base_dir=ROOT)
mcp = FastMCP(
    "Project File Service HTTP",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
_tools = register_file_tools(mcp, config_manager)

list_projects = _tools["list_projects"]
get_project_info = _tools["get_project_info"]
get_project = _tools["get_project"]
reload_projects_config = _tools["reload_projects_config"]
create_project = _tools["create_project"]
list_files = _tools["list_files"]
list_project_files = _tools["list_project_files"]
list_directory = _tools["list_directory"]
read_file = _tools["read_file"]
read_project_file = _tools["read_project_file"]
read_multiple_files = _tools["read_multiple_files"]
batch_read_files = _tools["batch_read_files"]
write_file = _tools["write_file"]
write_project_file = _tools["write_project_file"]
edit_file_snippet = _tools["edit_file_snippet"]
get_file_info = _tools["get_file_info"]
create_directory = _tools["create_directory"]
delete_file = _tools["delete_file"]
delete_directory = _tools["delete_directory"]
copy_path = _tools["copy_path"]
copy_directory = _tools["copy_directory"]
copy_file = _tools["copy_file"]
move_path = _tools["move_path"]
move_file = _tools["move_file"]
rename_file = _tools["rename_file"]
create_zip_archive = _tools["create_zip_archive"]
extract_zip_archive = _tools["extract_zip_archive"]
run_terminal_command = _tools["run_terminal_command"]
run_command = _tools["run_command"]
search_code = _tools["search_code"]
search_files = _tools["search_files"]
find_in_files = _tools["find_in_files"]
git_status = _tools["git_status"]
git_diff = _tools["git_diff"]
git_log = _tools["git_log"]
start_background_process = _tools["start_background_process"]
read_process_output = _tools["read_process_output"]
send_process_input = _tools["send_process_input"]
stop_background_process = _tools["stop_background_process"]
list_background_processes = _tools["list_background_processes"]
read_binary_file = _tools["read_binary_file"]
read_project_binary_file = _tools["read_project_binary_file"]
write_binary_file = _tools["write_binary_file"]
write_project_binary_file = _tools["write_project_binary_file"]
read_media_file = _tools["read_media_file"]
read_image_file = _tools["read_image_file"]

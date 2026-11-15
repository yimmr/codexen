"""Codexen MCP Service entrypoint.

Executable directly via:
    python -m codexen
    codexen (after pip install -e .)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mcp_server_core.server import run_mcp_server

from .server import codexen_payload_hook, config_manager, mcp


def build_root_info(request: Any) -> dict[str, Any]:
    """Provide structured service status and available endpoints at GET /."""
    projects_summary = [p.to_dict() for p in config_manager.projects.values()]
    return {
        "status": "online",
        "service": f"{config_manager.http_settings.display_title} File MCP Service",
        "transport": "Streamable HTTP",
        "mcp_url": f"http://{request.url.netloc}{config_manager.http_settings.path}",
        "endpoints": {
            "mcp": config_manager.http_settings.path,
            "health": "/health",
            "oauth_protected_resource": "/.well-known/oauth-protected-resource",
            "oauth_authorization_server": "/.well-known/oauth-authorization-server",
            "oauth_register": "/oauth/register",
            "oauth_authorize": "/oauth/authorize",
            "oauth_token": "/oauth/token",
        },
        "configured_projects": projects_summary,
        "auth_required": config_manager.http_settings.require_auth,
    }


def main() -> None:
    """Run Codexen MCP service through the generic framework runner."""
    def banner_items_builder() -> list[tuple[str, str]]:
        return [
            (p.name, f"{'[RO]' if p.read_only else '[RW]'} -> {p.root}")
            for p in config_manager.projects.values()
        ]

    try:
        run_mcp_server(
            server=mcp,
            base_dir=BASE_DIR,
            banner_items_builder=banner_items_builder,
            root_info_builder=build_root_info,
            env_cfg=config_manager.http_settings,
            payload_hook=codexen_payload_hook,
            logging_config=config_manager.logging_config,
        )
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()

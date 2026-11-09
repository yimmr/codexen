"""Generic MCP Server Environment & Deployment Configuration Module.

Independent of any domain-specific business logic, this module handles:
1. System runtime directory management (.runtime / .mcp_runtime).
2. Dynamic environment variable updates (.env persistence).
3. Generic deployment configuration loading (ports, hosts, Cloudflare tunnels, auth keys, SQLite paths).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import secrets
from typing import Any


def get_runtime_dir(base_dir: Path | None = None) -> Path:
    """Get or create the centralized runtime directory for temporary/state files."""
    root = (base_dir or Path.cwd()).resolve()
    dir_name = os.getenv("MCP_RUNTIME_DIR", ".runtime")
    runtime_dir = root / dir_name
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def update_env_variable(key: str, value: str, env_path: Path | None = None) -> None:
    """Update or append an environment variable in the target .env file."""
    target = env_path or (Path.cwd() / ".env")
    lines = []
    found = False
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


@dataclass
class DeploymentEnvConfig:
    """Generic MCP server deployment and runtime environment configuration (loaded from .env).
    
    This class manages purely infrastructure parameters (ports, hosts, Cloudflare tunnel tokens,
    API keys, and OAuth database paths) completely independent of any project business logic.
    """
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8788
    path: str = "/mcp"
    require_auth: bool = True
    api_key: str = ""
    allow_public_bind: bool = True
    tunnel: bool = True
    tunnel_token: str = ""
    portal_url: str = ""
    upstream_url: str = ""
    allow_query_auth: bool = False
    enable_public_oauth: bool = False
    db_path: str = ""

    @classmethod
    def load(
        cls,
        base_dir: Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> DeploymentEnvConfig:
        """Load deployment and runtime configuration from .env and environment variables."""
        root = (base_dir or Path.cwd()).resolve()
        env_file = root / ".env"
        if env_file.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(env_file)
            except ImportError:
                pass

        overrides = overrides or {}
        runtime_dir = get_runtime_dir(root)

        is_auth_req = bool(
            overrides.get(
                "require_auth",
                os.getenv("MCP_REQUIRE_AUTH", "true").lower() == "true",
            )
        )
        resolved_api_key = str(
            overrides.get("api_key", os.getenv("MCP_API_KEY", ""))
        ).strip()

        # If authentication is required but no key configured anywhere, auto-generate and store in .env
        if is_auth_req and not resolved_api_key:
            resolved_api_key = f"sec_{secrets.token_urlsafe(16)}"
            try:
                update_env_variable("MCP_API_KEY", resolved_api_key, env_file)
            except Exception:
                pass

        resolved_tunnel_token = str(
            overrides.get(
                "tunnel_token",
                os.getenv("CLOUDFLARE_TUNNEL_TOKEN", os.getenv("MCP_TUNNEL_TOKEN", "")),
            )
        ).strip()

        return cls(
            enabled=bool(overrides.get("enabled", True)),
            host=str(overrides.get("host", os.getenv("MCP_HOST", "127.0.0.1"))),
            port=int(overrides.get("port", os.getenv("MCP_PORT", 8788))),
            path=str(overrides.get("path", "/mcp")),
            require_auth=is_auth_req,
            api_key=resolved_api_key,
            allow_public_bind=bool(overrides.get("allow_public_bind", True)),
            tunnel=bool(overrides.get("tunnel", os.getenv("MCP_TUNNEL", "true").lower() == "true")),
            tunnel_token=resolved_tunnel_token,
            portal_url=str(overrides.get("portal_url", os.getenv("MCP_PORTAL_URL", ""))),
            upstream_url=str(overrides.get("upstream_url", os.getenv("MCP_UPSTREAM_URL", ""))),
            allow_query_auth=bool(
                overrides.get(
                    "allow_query_auth",
                    os.getenv("MCP_ALLOW_QUERY_AUTH", "false").lower() == "true",
                )
            ),
            enable_public_oauth=bool(
                overrides.get(
                    "enable_public_oauth",
                    os.getenv("MCP_ENABLE_PUBLIC_OAUTH", "false").lower() == "true",
                )
            ),
            db_path=str(
                overrides.get("db_path", os.getenv("MCP_DB_PATH", str(runtime_dir / "oauth_store.db")))
            ),
        )

"""Generic MCP Server Lifecycle & Runner Engine.

Completely decouples server process lifecycle management from business tools:
1. Universal CLI argument parsing (--port, --host, --tunnel, --no-tunnel, --key, --transport, --config)
2. Cloudflare Tunnel orchestration and graceful shutdown
3. Uvicorn HTTP / SSE ASGI server lifecycle
4. Stdio transport support for local IDEs
5. Process PID registration and clean exit
"""
from __future__ import annotations

import argparse
import atexit
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Callable

# Protect against Windows GBK UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uvicorn
from starlette.applications import Starlette

try:
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer as FastMCP

from .env import DeploymentEnvConfig, get_runtime_dir
from .http_server import create_mcp_asgi_app, get_local_ip, mount_mcp_oauth_routes
from .oauth import LocalOAuthServer
from .tunnel import CloudflareTunnel, is_cloudflared_available

_PID_FILE: Path | None = None


def record_pid(runtime_dir: Path) -> Path:
    """Record current process PID in runtime directory."""
    global _PID_FILE
    _PID_FILE = runtime_dir / ".server.pid"
    _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return _PID_FILE


def cleanup_pid() -> None:
    """Remove PID file on exit."""
    global _PID_FILE
    if _PID_FILE and _PID_FILE.exists():
        try:
            _PID_FILE.unlink()
        except Exception:
            pass


atexit.register(cleanup_pid)


def build_mcp_server_app(
    server: FastMCP,
    env_cfg: DeploymentEnvConfig,
    oauth_server: LocalOAuthServer,
    service_name: str = "mcp_service",
    root_info_builder: Callable[[Any], dict[str, Any]] | None = None,
    require_auth: bool | None = None,
) -> Starlette:
    """Mount standard OAuth endpoints and build the final ASGI application."""
    mount_mcp_oauth_routes(
        server=server,
        oauth_server=oauth_server,
        service_name=service_name,
        root_info_builder=root_info_builder,
        enable_public_oauth=env_cfg.enable_public_oauth,
    )
    return create_mcp_asgi_app(
        server=server,
        env_cfg=env_cfg,
        oauth_server=oauth_server,
        require_auth=require_auth,
    )


async def serve_http(
    server: FastMCP,
    env_cfg: DeploymentEnvConfig,
    base_dir: Path | None = None,
    oauth_server: LocalOAuthServer | None = None,
    service_name: str = "mcp_service",
    root_info_builder: Callable[[Any], dict[str, Any]] | None = None,
    banner_title: str | None = None,
    banner_items: list[tuple[str, str]] | None = None,
) -> None:
    """Run FastMCP via Streamable HTTP with optional Cloudflare Tunnel and OAuth."""
    root = (base_dir or Path.cwd()).resolve()

    if env_cfg.require_auth and not env_cfg.api_key:
        env_cfg.api_key = f"sec_{secrets.token_urlsafe(20)}"

    if oauth_server is None:
        db_path = env_cfg.db_path or (get_runtime_dir(root) / "oauth_store.db")
        oauth_server = LocalOAuthServer(admin_password=env_cfg.api_key, db_path=db_path)

    app = build_mcp_server_app(
        server=server,
        env_cfg=env_cfg,
        oauth_server=oauth_server,
        service_name=service_name,
        root_info_builder=root_info_builder,
    )

    tunnel: CloudflareTunnel | None = None
    public_url = os.getenv("PUBLIC_BASE_URL", "")
    portal_url = env_cfg.portal_url

    if env_cfg.tunnel:
        if is_cloudflared_available():
            if env_cfg.tunnel_token:
                print("  [*] Starting Cloudflare Named Tunnel...")
            else:
                print("  [*] Starting Cloudflare Quick Tunnel for remote agent access...")
            try:
                tunnel = CloudflareTunnel(
                    local_port=env_cfg.port,
                    local_host="127.0.0.1",
                    tunnel_token=env_cfg.tunnel_token or None,
                    public_url=env_cfg.upstream_url or None,
                )
                detected_url = tunnel.start(timeout=20.0)
                if detected_url:
                    public_url = detected_url
                    os.environ["PUBLIC_BASE_URL"] = detected_url
            except Exception as e:
                print(f"  [!] Could not start Cloudflare tunnel: {e}")
        else:
            print("  [!] cloudflared binary not found; skipping tunnel.")

    mcp_path = env_cfg.path
    local_mcp_url = f"http://127.0.0.1:{env_cfg.port}{mcp_path}"
    upstream_mcp_url = env_cfg.upstream_url if env_cfg.upstream_url else (f"{public_url}{mcp_path}" if public_url else None)
    active_mcp_url = portal_url or upstream_mcp_url or local_mcp_url

    title = banner_title or "MCP Server Engine (Remote Agent Ready)"
    print("\n" + "=" * 76)
    print(f"  [>] {title}")
    print("=" * 76)
    print(f"  * 本地监听地址:    {local_mcp_url}")
    if portal_url:
        print(f"  * 官方 MCP 门户:   {portal_url}  (ChatGPT 统一端点)")
    if upstream_mcp_url:
        print(f"  * 内部隧道回源:    {upstream_mcp_url}")
    if env_cfg.require_auth:
        print(f"  * 安全访问密钥:    {env_cfg.api_key}")
    print("-" * 76)

    print("  【远程 Agent 配置指引 (Remote Agent Connection Guide)】")
    if portal_url or upstream_mcp_url:
        primary_endpoint = portal_url or upstream_mcp_url
        print(f"  [ChatGPT Developer Mode / Custom Actions 连接指南]:")
        print(f"    * 服务器 URL:       {primary_endpoint}")
        print(f"    * 身份验证类型:     Bearer Token 或 OAuth")
        print(f"    * 访问密钥:         {env_cfg.api_key}")
        print(f"    * 备用直连回源地址: {upstream_mcp_url}\n")

        if env_cfg.require_auth:
            print(f"  [通用远程 Agent (网页端/Cursor/Claude Desktop/API)]: ")
            print(f"    * 单 URL 快捷接入:  {primary_endpoint}?token={env_cfg.api_key}")
            print(f"    * Header 接入:      Authorization: Bearer {env_cfg.api_key}")
        else:
            print(f"  URL: {primary_endpoint}")
    else:
        if env_cfg.require_auth:
            print(f"  URL:    {local_mcp_url}")
            print(f"  Header: Authorization: Bearer {env_cfg.api_key}")
        else:
            print(f"  URL:    {local_mcp_url}")

    if banner_items:
        print("-" * 76)
        print("  【配置信息 (Configuration Summary)】:")
        for name, desc in banner_items:
            print(f"    - {name:<22} -> {desc}")

    print("=" * 76 + "\n")

    uvicorn_config = uvicorn.Config(
        app,
        host=env_cfg.host,
        port=env_cfg.port,
        log_level="info",
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)

    try:
        await uvicorn_server.serve()
    finally:
        if tunnel:
            print("\n  [*] Stopping Cloudflare Tunnel...")
            tunnel.stop()


def serve_stdio(server: FastMCP) -> None:
    """Run FastMCP via Stdio transport."""
    server.run(transport="stdio")


def run_mcp_server(
    server: FastMCP,
    base_dir: Path | None = None,
    service_name: str = "mcp_service",
    banner_title: str | None = None,
    banner_items_builder: Callable[[], list[tuple[str, str]]] | None = None,
    root_info_builder: Callable[[Any], dict[str, Any]] | None = None,
    cli_args: list[str] | None = None,
) -> None:
    """Universal MCP Server Runner entrypoint with CLI argument parsing."""
    root = (base_dir or Path.cwd()).resolve()
    parser = argparse.ArgumentParser(description=f"{service_name} Runner")
    parser.add_argument("-c", "--config", dest="config_path", default=None, help="Path to configuration file")
    parser.add_argument("--tunnel", dest="tunnel", action="store_true", default=None, help="Enable Cloudflare Tunnel")
    parser.add_argument("--no-tunnel", dest="tunnel", action="store_false", help="Disable Cloudflare Tunnel")
    parser.add_argument("--key", dest="api_key", default=None, help="Custom security API Key")
    parser.add_argument("--host", default=None, help="Host to bind (default: from .env)")
    parser.add_argument("--port", type=int, default=None, help="Port to bind (default: from .env)")
    parser.add_argument("--transport", choices=["http", "stdio"], default="http", help="Transport mode (default: http)")

    parsed = parser.parse_args(cli_args)

    # 1. Record PID
    runtime_dir = get_runtime_dir(root)
    record_pid(runtime_dir)

    # 2. Stdio mode
    if parsed.transport == "stdio":
        serve_stdio(server)
        return

    # 3. HTTP mode: Load deployment config and apply CLI overrides
    env_cfg = DeploymentEnvConfig.load(root)
    if parsed.host:
        env_cfg.host = parsed.host
    if parsed.port:
        env_cfg.port = parsed.port
    if parsed.api_key:
        env_cfg.api_key = parsed.api_key
        env_cfg.require_auth = True
    if parsed.tunnel is not None:
        env_cfg.tunnel = parsed.tunnel

    banner_items = banner_items_builder() if banner_items_builder else None

    import anyio
    anyio.run(
        serve_http,
        server,
        env_cfg,
        root,
        None,
        service_name,
        root_info_builder,
        banner_title,
        banner_items,
    )

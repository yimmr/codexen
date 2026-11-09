"""Generic MCP HTTP Server & ASGI Transport Layer.

Provides reusable ASGI application assembly, CORS setup, authentication middleware,
and standard RFC 9728/8414/7591 OAuth discovery routes for any FastMCP server.
"""
from __future__ import annotations

import socket
import sys
from typing import Any, Callable

# Protect against Windows GBK UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

try:
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer as FastMCP

from .env import DeploymentEnvConfig
from .oauth import (
    LocalOAuthServer,
    get_server_public_url,
    oauth_authorization_server_metadata_endpoint,
    oauth_authorize_endpoint,
    oauth_jwks_endpoint,
    oauth_protected_resource_endpoint,
    oauth_register_endpoint,
    oauth_token_endpoint,
)


def get_local_ip() -> str:
    """Attempt to detect the primary local IPv4 address for network access."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


class HTTPAuthenticationMiddleware(BaseHTTPMiddleware):
    """Simple API Key / Bearer Token & OAuth 2.1 authentication middleware for HTTP MCP."""

    def __init__(
        self,
        app: Any,
        oauth_server: LocalOAuthServer,
        require_auth: bool = False,
        allow_query_auth: bool = False,
        enable_public_oauth: bool = True,
    ) -> None:
        super().__init__(app)
        self.oauth_server = oauth_server
        self.require_auth = require_auth
        self.allow_query_auth = allow_query_auth
        self.enable_public_oauth = enable_public_oauth

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        # 1. Allow CORS pre-flight
        if request.method == "OPTIONS":
            return await call_next(request)

        # 2. Public health check, OAuth and metadata endpoints require no auth
        path = request.url.path
        if (
            path in ("/", "/health", "/docs")
            or path.startswith("/.well-known/")
            or path.startswith("/oauth/")
            or path.endswith("/.well-known/oauth-protected-resource")
        ):
            return await call_next(request)

        # 3. If authentication is not enforced, proceed
        if not self.require_auth:
            return await call_next(request)

        # 4. Extract token from Authorization Header (or URL query only if explicitly allowed)
        token = ""
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif self.allow_query_auth:
            if "token" in request.query_params:
                token = request.query_params.get("token", "").strip()
            elif "key" in request.query_params:
                token = request.query_params.get("key", "").strip()

        public_url = get_server_public_url(request)

        if not token:
            if self.enable_public_oauth:
                challenge = f'Bearer resource_metadata="{public_url}/.well-known/oauth-protected-resource"'
                msg = "Authentication required. Please authenticate via OAuth or provide Bearer token in Authorization header."
            else:
                challenge = 'Bearer error="invalid_token"'
                msg = "Direct public access to this backend is disabled. Please connect through your configured MCP portal or provide a valid Bearer token."
            return JSONResponse(
                {
                    "error": "unauthorized",
                    "message": msg,
                },
                status_code=401,
                headers={"WWW-Authenticate": challenge, "Access-Control-Allow-Origin": "*"},
            )

        if not self.oauth_server.is_token_valid(token):
            if self.enable_public_oauth:
                challenge = f'Bearer error="invalid_token", resource_metadata="{public_url}/.well-known/oauth-protected-resource"'
            else:
                challenge = 'Bearer error="invalid_token"'
            return JSONResponse(
                {"error": "forbidden", "message": "Invalid API Key or OAuth Token."},
                status_code=403,
                headers={"WWW-Authenticate": challenge, "Access-Control-Allow-Origin": "*"},
            )

        return await call_next(request)


def mount_mcp_oauth_routes(
    server: FastMCP,
    oauth_server: LocalOAuthServer,
    service_name: str = "mcp_service",
    root_info_builder: Callable[[Request], dict[str, Any]] | None = None,
    enable_public_oauth: bool = True,
) -> None:
    """Mount standard OAuth 2.1 metadata, authorization, token, and health routes on FastMCP."""
    # 1. Custom informative root endpoint
    @server.custom_route("/", methods=["GET", "OPTIONS"])
    async def root_info(request: Request) -> Response:
        if root_info_builder:
            payload = root_info_builder(request)
        else:
            payload = {
                "status": "online",
                "service": service_name,
                "endpoints": {
                    "health": "/health",
                    "oauth_protected_resource": "/.well-known/oauth-protected-resource",
                    "oauth_authorization_server": "/.well-known/oauth-authorization-server",
                    "oauth_register": "/oauth/register",
                    "oauth_authorize": "/oauth/authorize",
                    "oauth_token": "/oauth/token",
                },
            }
        return JSONResponse(payload, headers={"Access-Control-Allow-Origin": "*"})

    # 2. Health check endpoint
    @server.custom_route("/health", methods=["GET", "OPTIONS"])
    async def health_check(request: Request) -> Response:
        return JSONResponse(
            {"status": "healthy", "service": service_name},
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # 3. RFC 9728 Protected Resource Metadata (ChatGPT 探测入口)
    @server.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
    @server.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET", "OPTIONS"])
    @server.custom_route("/mcp/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
    async def protected_resource(request: Request) -> Response:
        if not enable_public_oauth:
            return JSONResponse(
                {
                    "error": "oauth_disabled",
                    "message": "Public OAuth registration is disabled on this backend.",
                },
                status_code=404,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        return await oauth_protected_resource_endpoint(request)

    # 4. RFC 8414 Authorization Server Metadata (OAuth 2.1 发现入口)
    @server.custom_route("/.well-known/oauth-authorization-server", methods=["GET", "OPTIONS"])
    @server.custom_route("/.well-known/oauth-authorization-server/mcp", methods=["GET", "OPTIONS"])
    @server.custom_route("/.well-known/openid-configuration", methods=["GET", "OPTIONS"])
    @server.custom_route("/.well-known/openid-configuration/mcp", methods=["GET", "OPTIONS"])
    async def authorization_server_metadata(request: Request) -> Response:
        if not enable_public_oauth:
            return JSONResponse(
                {"error": "oauth_disabled", "message": "Public OAuth discovery is disabled on this backend."},
                status_code=404,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        return await oauth_authorization_server_metadata_endpoint(request)

    # 5. RFC 7591 Dynamic Client Registration
    @server.custom_route("/oauth/register", methods=["POST", "OPTIONS"])
    async def oauth_register(request: Request) -> Response:
        if not enable_public_oauth:
            return JSONResponse(
                {
                    "error": "registration_disabled",
                    "error_description": "Public OAuth registration on this endpoint is disabled.",
                },
                status_code=403,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        return await oauth_register_endpoint(request, oauth_server)

    # 6. OAuth 2.1 Authorization Code Consent Page
    @server.custom_route("/oauth/authorize", methods=["GET", "POST", "OPTIONS"])
    async def oauth_authorize(request: Request) -> Response:
        if not enable_public_oauth:
            return JSONResponse(
                {"error": "authorization_disabled", "message": "OAuth consent is disabled on this backend."},
                status_code=403,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        return await oauth_authorize_endpoint(request, oauth_server)

    # 7. OAuth 2.1 Token Exchange
    @server.custom_route("/oauth/token", methods=["POST", "OPTIONS"])
    async def oauth_token(request: Request) -> Response:
        if not enable_public_oauth:
            return JSONResponse(
                {"error": "token_endpoint_disabled", "message": "OAuth token exchange is disabled on this backend."},
                status_code=403,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        return await oauth_token_endpoint(request, oauth_server)

    # 8. JWKS Public Keys Endpoint
    @server.custom_route("/oauth/jwks.json", methods=["GET", "OPTIONS"])
    @server.custom_route("/.well-known/jwks.json", methods=["GET", "OPTIONS"])
    async def oauth_jwks(request: Request) -> Response:
        return await oauth_jwks_endpoint(request)


def create_mcp_asgi_app(
    server: FastMCP,
    env_cfg: DeploymentEnvConfig,
    oauth_server: LocalOAuthServer,
    require_auth: bool | None = None,
) -> Starlette:
    """Build the final Starlette application with CORS and authentication middlewares."""
    is_auth_required = env_cfg.require_auth if require_auth is None else require_auth

    app = server.streamable_http_app()
    app.add_middleware(
        HTTPAuthenticationMiddleware,
        oauth_server=oauth_server,
        require_auth=is_auth_required,
        allow_query_auth=env_cfg.allow_query_auth,
        enable_public_oauth=env_cfg.enable_public_oauth,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*", "WWW-Authenticate", "Mcp-Session-Id"],
    )
    return app

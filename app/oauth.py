"""OAuth 2.1 & RFC 9728 adapter for project_file_mcp.

Re-exports generic OAuth and Protected Resource implementation from mcp_framework.oauth.
"""
from __future__ import annotations

from mcp_framework.oauth import (
    AuthRateLimiter,
    LocalOAuthServer,
    base64url_encode,
    get_client_ip,
    get_server_public_url,
    oauth_authorization_server_metadata_endpoint,
    oauth_authorize_endpoint,
    oauth_jwks_endpoint,
    oauth_protected_resource_endpoint,
    oauth_register_endpoint,
    oauth_token_endpoint,
    verify_pkce,
)

__all__ = [
    "AuthRateLimiter",
    "LocalOAuthServer",
    "base64url_encode",
    "get_client_ip",
    "get_server_public_url",
    "oauth_authorization_server_metadata_endpoint",
    "oauth_authorize_endpoint",
    "oauth_jwks_endpoint",
    "oauth_protected_resource_endpoint",
    "oauth_register_endpoint",
    "oauth_token_endpoint",
    "verify_pkce",
]

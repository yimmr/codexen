"""Cloudflare Tunnel manager adapter for project_file_mcp.

Re-exports generic tunnel tools and CloudflareTunnel from mcp_framework.tunnel.
"""
from __future__ import annotations

from mcp_framework.tunnel import (
    CloudflareTunnel,
    ensure_tunnel_running,
    find_cloudflared,
    get_cloudflare_ingress_ports,
    get_tunnel_token,
    is_cloudflared_available,
    is_port_listening,
    is_tunnel_running,
    stop_tunnel_if_no_mcp,
)

__all__ = [
    "CloudflareTunnel",
    "ensure_tunnel_running",
    "find_cloudflared",
    "get_cloudflare_ingress_ports",
    "get_tunnel_token",
    "is_cloudflared_available",
    "is_port_listening",
    "is_tunnel_running",
    "stop_tunnel_if_no_mcp",
]

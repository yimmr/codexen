"""Integration tests for HTTP URL-based MCP Server and OAuth 2.1 / RFC 9728 for ChatGPT."""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
import yaml

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.config import ConfigManager
from app.server import (
    build_starlette_app,
    create_http_server,
)
from app.oauth import base64url_encode


class HTTPServerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()

        # Create sample project directory
        self.proj_dir = self.base_dir / "my_project"
        self.proj_dir.mkdir(parents=True)
        (self.proj_dir / "sample.txt").write_text("HTTP MCP Content", encoding="utf-8")

        # Create custom settings.yaml
        os.environ["MCP_ENABLE_PUBLIC_OAUTH"] = "true"
        self.config_path = self.base_dir / "settings.yaml"
        config_data = {
            "workspace": {
                "root": str(self.base_dir / "works"),
                "allow_create_project": True,
            },
            "projects": {
                "web_proj": {
                    "root": str(self.proj_dir),
                    "description": "HTTP test project",
                    "read_only": False,
                }
            },
            "mcp": {
                "http": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 8788,
                    "path": "/mcp",
                },
                "security": {
                    "require_auth": False,
                    "api_key": "test-secret-key",
                    "allow_public_bind": True,
                },
                "permissions": {
                    "max_read_chars": 50000,
                    "allow_file_delete": True,
                    "allow_command_execution": True,
                    "command_timeout_seconds": 10,
                },
            },
        }
        self.config_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")
        self.cfg_mgr = ConfigManager(config_path=self.config_path, base_dir=self.base_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_http_info_and_health_endpoints(self):
        server, cfg, oauth_server = create_http_server(root=self.base_dir, config_mgr=self.cfg_mgr)
        app = build_starlette_app(server, cfg, oauth_server, require_auth=False)

        async def run_checks():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8788") as client:
                # 1. Root info endpoint
                r_root = await client.get("/")
                self.assertEqual(r_root.status_code, 200)
                data = r_root.json()
                self.assertEqual(data["status"], "online")
                self.assertEqual(data["endpoints"]["mcp"], "/mcp")
                self.assertTrue(any(p["project_id"] == "web_proj" for p in data["configured_projects"]))

                # 2. Health endpoint
                r_health = await client.get("/health")
                self.assertEqual(r_health.status_code, 200)
                self.assertEqual(r_health.json()["status"], "healthy")

                # 3. RFC 9728 OAuth Protected Resource Metadata (all path variants)
                for res_path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp", "/mcp/.well-known/oauth-protected-resource"):
                    r_oauth_res = await client.get(res_path)
                    self.assertEqual(r_oauth_res.status_code, 200, f"Failed on {res_path}")
                    self.assertIn("authorization_servers", r_oauth_res.json())

                # 4. RFC 8414 OAuth Authorization Server Metadata (all path variants)
                for srv_path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp", "/.well-known/openid-configuration"):
                    r_oauth_srv = await client.get(srv_path)
                    self.assertEqual(r_oauth_srv.status_code, 200, f"Failed on {srv_path}")
                    srv_data = r_oauth_srv.json()
                    self.assertIn("authorization_endpoint", srv_data)
                    self.assertIn("token_endpoint", srv_data)
                    self.assertIn("registration_endpoint", srv_data)

        asyncio.run(run_checks())

    def test_chatgpt_full_oauth_flow_with_pkce(self):
        """Test the complete OAuth 2.1 authorization code flow with PKCE used by ChatGPT."""
        self.cfg_mgr.http_settings.api_key = "my-local-secret"
        self.cfg_mgr.http_settings.require_auth = True

        server, cfg, oauth_server = create_http_server(root=self.base_dir, config_mgr=self.cfg_mgr)
        app = build_starlette_app(server, cfg, oauth_server, require_auth=True)

        async def run_oauth_flow():
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8788") as client:
                    # 1. ChatGPT attempts connection without token -> 401 with resource_metadata challenge
                    r_initial = await client.post("/mcp", json={})
                    self.assertEqual(r_initial.status_code, 401)
                    www_auth = r_initial.headers.get("www-authenticate", "")
                    self.assertIn("resource_metadata=", www_auth)

                    # 2. ChatGPT registers dynamically (RFC 7591 DCR)
                    r_reg = await client.post(
                        "/oauth/register",
                        json={
                            "client_name": "ChatGPT Test Client",
                            "redirect_uris": ["https://chatgpt.com/api/mcp/oauth/callback"],
                        },
                    )
                    self.assertEqual(r_reg.status_code, 201)
                    client_id = r_reg.json()["client_id"]

                    # 3. ChatGPT directs user to /oauth/authorize with PKCE challenge
                    code_verifier = "a-very-secret-code-verifier-string-123456789012345"
                    code_challenge = base64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())

                    # User loads authorization page (GET)
                    r_page = await client.get(
                        f"/oauth/authorize?response_type=code&client_id={client_id}&redirect_uri=https://chatgpt.com/api/mcp/oauth/callback&state=test_state&code_challenge={code_challenge}&code_challenge_method=S256"
                    )
                    self.assertEqual(r_page.status_code, 200)
                    self.assertIn("ChatGPT", r_page.text)

                    # User enters password and submits consent (POST)
                    r_consent = await client.post(
                        "/oauth/authorize",
                        data={
                            "client_id": client_id,
                            "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
                            "state": "test_state",
                            "code_challenge": code_challenge,
                            "code_challenge_method": "S256",
                            "password": "my-local-secret",
                        },
                        follow_redirects=False,
                    )
                    self.assertEqual(r_consent.status_code, 302)
                    redirect_location = r_consent.headers.get("location", "")
                    self.assertIn("code=", redirect_location)
                    self.assertIn("state=test_state", redirect_location)

                    # Extract auth code from redirect URL
                    auth_code = redirect_location.split("code=")[1].split("&")[0]

                    # 4. ChatGPT exchanges code and code_verifier at /oauth/token
                    r_token = await client.post(
                        "/oauth/token",
                        data={
                            "grant_type": "authorization_code",
                            "code": auth_code,
                            "client_id": client_id,
                            "redirect_uri": "https://chatgpt.com/api/mcp/oauth/callback",
                            "code_verifier": code_verifier,
                        },
                    )
                    self.assertEqual(r_token.status_code, 200)
                    token_data = r_token.json()
                    self.assertIn("access_token", token_data)
                    access_token = token_data["access_token"]

                    # 5. Connect to Streamable HTTP MCP with the acquired OAuth token!
                    auth_http_client = httpx.AsyncClient(
                        transport=transport,
                        base_url="http://127.0.0.1:8788",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                    async with auth_http_client:
                        async with streamable_http_client(
                            "http://127.0.0.1:8788/mcp",
                            http_client=auth_http_client,
                        ) as (read_stream, write_stream, _):
                            async with ClientSession(read_stream, write_stream) as session:
                                init_result = await session.initialize()
                                self.assertEqual(init_result.serverInfo.name, "Project File Service HTTP")

                                # 6. Verify list_tools returns all tools including new ones
                                tools_list = await session.list_tools()
                                tool_names = [t.name for t in tools_list.tools]
                                expected_tools = [
                                    "list_projects", "create_project", "read_file", "write_file",
                                    "copy_path", "create_zip_archive", "extract_zip_archive",
                                    "run_terminal_command", "search_code",
                                ]
                                for et in expected_tools:
                                    self.assertIn(et, tool_names, f"Tool '{et}' missing from HTTP MCP server!")

                                # 7. Call list_projects
                                proj_res = await session.call_tool("list_projects", {})
                                self.assertIn("web_proj", proj_res.content[0].text)

                                # 8. Call search_code over HTTP
                                search_res = await session.call_tool("search_code", {
                                    "project_id": "web_proj",
                                    "query": "HTTP",
                                })
                                self.assertIn("sample.txt", search_res.content[0].text)

                                # 9. Call run_terminal_command over HTTP
                                cmd_res = await session.call_tool("run_terminal_command", {
                                    "project_id": "web_proj",
                                    "command": 'python -c "print(\'Streamable HTTP Command Executed\')"',
                                })
                                self.assertIn("Streamable HTTP Command Executed", cmd_res.content[0].text)

        asyncio.run(run_oauth_flow())


if __name__ == "__main__":
    unittest.main()

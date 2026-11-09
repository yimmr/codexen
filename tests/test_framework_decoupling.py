"""Tests ensuring strict architectural decoupling between generic mcp_framework and app."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
import tempfile
import shutil

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import mcp_framework
from mcp_framework.env import DeploymentEnvConfig, get_runtime_dir, update_env_variable
from mcp_framework.oauth import LocalOAuthServer, AuthRateLimiter, base64url_encode, verify_pkce
from mcp_framework.tunnel import CloudflareTunnel, is_cloudflared_available, is_port_listening
from mcp_framework.http_server import create_mcp_asgi_app, mount_mcp_oauth_routes, get_local_ip
from mcp_framework.process import is_pid_alive, is_port_in_use, check_service_status, find_pid_by_port

try:
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer as FastMCP


class FrameworkDecouplingTests(unittest.TestCase):
    """Test suite verifying generic MCP framework independence and contract fulfillment."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_fw_test_"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_framework_exports(self):
        """Verify all primary framework symbols are properly exposed at top level."""
        expected = [
            "DeploymentEnvConfig",
            "get_runtime_dir",
            "update_env_variable",
            "create_mcp_asgi_app",
            "LocalOAuthServer",
            "AuthRateLimiter",
            "base64url_encode",
            "get_server_public_url",
            "ensure_tunnel_running",
            "find_cloudflared",
            "get_cloudflare_ingress_ports",
            "is_tunnel_running",
            "stop_tunnel_if_no_mcp",
        ]
        for symbol in expected:
            self.assertTrue(hasattr(mcp_framework, symbol), f"mcp_framework missing symbol {symbol}")

    def test_framework_pure_instantiation(self):
        """Verify mcp_framework can power an arbitrary dummy MCP service without app."""
        dummy_mcp = FastMCP("DummyService")

        @dummy_mcp.tool()
        def dummy_ping() -> str:
            return "pong"

        db_path = self.temp_dir / "dummy_oauth.db"
        oauth_server = LocalOAuthServer(admin_password="test_secret_pass", db_path=db_path)
        mount_mcp_oauth_routes(dummy_mcp, oauth_server, service_name="dummy_service")

        env_cfg = DeploymentEnvConfig(
            port=9999,
            require_auth=True,
            api_key="test_secret_pass",
        )

        app = create_mcp_asgi_app(dummy_mcp, env_cfg, oauth_server)
        self.assertIsNotNone(app)

    def test_env_rotation(self):
        """Verify update_env_variable updates on disk safely."""
        fake_env = self.temp_dir / ".env"
        fake_env.write_text("FOO=bar\nMCP_PORT=8788\n", encoding="utf-8")

        update_env_variable("MCP_PORT", "8888", env_path=fake_env)
        content = fake_env.read_text(encoding="utf-8")
        self.assertIn("MCP_PORT=8888", content)
        self.assertIn("FOO=bar", content)

    def test_oauth_pkce_validation(self):
        """Verify RFC 7636 PKCE validation algorithm."""
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        import hashlib
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64url_encode(digest)

        self.assertTrue(verify_pkce(verifier, challenge, "S256"))
        self.assertFalse(verify_pkce("wrong_verifier", challenge, "S256"))

    def test_process_manager_utilities(self):
        """Verify process management and PID checks."""
        import os
        my_pid = os.getpid()
        self.assertTrue(is_pid_alive(my_pid))
        self.assertFalse(is_pid_alive(99999999))
        self.assertFalse(is_port_in_use(59999))

    def test_yaml_roundtrip_comment_preservation(self):
        """Verify _persist_project_to_yaml preserves existing comments with ruamel.yaml."""
        from app.config import ConfigManager, ProjectConfig

        config_file = self.temp_dir / "config.yaml"
        config_file.write_text(
            "# Primary Workspace Header\n"
            "workspace:\n"
            "  root: 'D:/works'\n"
            "# Configured target projects list\n"
            "projects:\n"
            "  existing_proj:\n"
            "    root: 'D:/works/existing_proj'\n"
            "    description: 'Existing project'\n"
            "    read_only: false\n",
            encoding="utf-8",
        )

        cfg_mgr = ConfigManager(config_path=config_file, base_dir=self.temp_dir)
        new_proj = ProjectConfig(
            name="new_proj",
            root=Path("D:/works/new_proj"),
            description="Newly created project",
            read_only=False,
        )
        cfg_mgr._persist_project_to_yaml(new_proj)

        content = config_file.read_text(encoding="utf-8")
        self.assertIn("# Primary Workspace Header", content)
        self.assertIn("# Configured target projects list", content)
        self.assertIn("new_proj", content)
        self.assertIn("existing_proj", content)


if __name__ == "__main__":
    unittest.main()

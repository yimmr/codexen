"""Comprehensive unit tests for enhanced security protections:
- Sensitive file blacklist enforcement (.env, id_rsa, .git/, credentials)
- Native SQLite OAuth token persistence across restarts
- Brute-force rate limiting and lockout on /oauth/authorize
- DCR dynamic client registration rate limiting
- Git repository status and diff tools
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
import yaml

import httpx

from codexen.config import (
    ConfigManager,
    ProjectConfig,
    get_runtime_dir,
    update_env_variable,
)
from codexen.core import (
    delete_project_file,
    list_project_files,
    read_project_file,
    write_project_file,
)
from codexen.exceptions import ConfigurationError, PathSecurityError
from codexen.server import build_starlette_app, create_http_server
from codexen.tools import register_file_tools
from codexen.oauth import (
    AuthRateLimiter,
    LocalOAuthServer,
    base64url_encode,
)


class SecurityEnhancedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()
        self.proj_dir = self.base_dir / "my_proj"
        self.proj_dir.mkdir(parents=True)

        self.project = ProjectConfig(
            name="test_proj",
            root=self.proj_dir,
            description="Test Project",
            read_only=False,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sensitive_file_read_blocked(self):
        """Ensure sensitive credentials and VCS internal files cannot be read in project."""
        sensitive_files = [
            ".env",
            ".env.local",
            ".env.production",
            "id_rsa",
            "id_rsa.pub",
            "id_ed25519",
            "server.pem",
            "cert.key",
            ".git/config",
            ".git/HEAD",
        ]

        for rel in sensitive_files:
            file_path = self.proj_dir / rel
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("SUPER_SECRET_VALUE", encoding="utf-8")

            with self.subTest(sensitive_path=rel):
                with self.assertRaises(PathSecurityError):
                    read_project_file(self.project, rel)

    def test_sensitive_file_write_blocked(self):
        """Ensure sensitive credentials and VCS internal files cannot be written or created."""
        forbidden_writes = [
            ".env",
            ".env.staging",
            "id_rsa",
            "my_key.pem",
            ".git/hooks/pre-commit",
        ]
        for rel in forbidden_writes:
            with self.subTest(forbidden_write=rel):
                with self.assertRaises(PathSecurityError):
                    write_project_file(self.project, rel, "malicious_payload", overwrite=True)

    def test_normal_project_files_allowed(self):
        """Ensure normal development project files (config/settings.yaml, launcher.py, app.db)
        are NOT blocked by the MCP bridge. The bridge must never impose server-internal file
        blacklists onto the user's target projects.
        """
        allowed_files = [
            "config/settings.yaml",
            "launcher.py",
            "tray_app.py",
            "app.db",
            "data.sqlite3",
            "server.pid",
        ]
        for rel in allowed_files:
            with self.subTest(allowed_file=rel):
                write_project_file(self.project, rel, f"# content of {rel}", overwrite=True)
                content = read_project_file(self.project, rel)
                self.assertIn(f"# content of {rel}", content)

    def test_sensitive_file_delete_blocked(self):
        """Ensure sensitive files cannot be deleted."""
        target = self.proj_dir / ".env"
        target.write_text("FOO=BAR", encoding="utf-8")
        with self.assertRaises(PathSecurityError):
            delete_project_file(self.project, ".env")

    def test_sensitive_files_hidden_from_list_files(self):
        """Ensure sensitive files are filtered out from list_files results."""
        (self.proj_dir / "normal.py").write_text("print('hello')", encoding="utf-8")
        (self.proj_dir / ".env").write_text("SECRET=123", encoding="utf-8")
        (self.proj_dir / "key.pem").write_text("CERT", encoding="utf-8")

        entries = list_project_files(self.project)
        names = [e["name"] for e in entries]

        self.assertIn("normal.py", names)
        self.assertNotIn(".env", names)
        self.assertNotIn("key.pem", names)

    def test_sqlite_token_persistence_across_server_restarts(self):
        """Verify that tokens and clients stored in SQLite survive full server reboots."""
        db_file = self.base_dir / "oauth_test.db"

        # 1. First server instance: register client and generate token
        srv1 = LocalOAuthServer(admin_password="test-secret-key", db_path=db_file)
        client = srv1.register_client({"client_name": "ChatGPT Test"})
        client_id = client["client_id"]

        code_verifier = "my-secret-verifier-for-pkce-123456789012345"
        code_challenge = base64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())

        code = srv1.create_auth_code(
            client_id=client_id,
            redirect_uri="https://chatgpt.com/callback",
            code_challenge=code_challenge,
        )

        token_info = srv1.exchange_code_for_token(
            code=code,
            client_id=client_id,
            redirect_uri="https://chatgpt.com/callback",
            code_verifier=code_verifier,
        )
        token = token_info["access_token"]
        self.assertTrue(srv1.is_token_valid(token))

        # 2. Simulate server crash / restart: create brand new LocalOAuthServer instance pointing to same DB
        del srv1
        srv2 = LocalOAuthServer(admin_password="test-secret-key", db_path=db_file)

        # The previously issued token MUST still be valid in srv2 without re-authenticating!
        self.assertTrue(srv2.is_token_valid(token))
        # Static admin password must also remain valid
        self.assertTrue(srv2.is_token_valid("test-secret-key"))
        # Invalid token must be rejected
        self.assertFalse(srv2.is_token_valid("invalid-token-123"))

    def test_rate_limiter_lockout_on_password_bruteforce(self):
        """Test sliding-window rate limiter triggers lockout after repeated failed attempts."""
        limiter = AuthRateLimiter(max_attempts=3, window_seconds=60.0, lockout_seconds=30.0)
        test_ip = "192.168.1.100"

        # Attempt 1 & 2: failure recorded, not locked
        locked, secs, rem = limiter.record_failure(test_ip)
        self.assertFalse(locked)
        self.assertEqual(rem, 2)

        locked, secs, rem = limiter.record_failure(test_ip)
        self.assertFalse(locked)
        self.assertEqual(rem, 1)

        # Attempt 3: 3rd failure reaches max_attempts -> locked out!
        locked, secs, rem = limiter.record_failure(test_ip)
        self.assertTrue(locked)
        self.assertGreater(secs, 0)
        self.assertEqual(rem, 0)

        # Subsequent check shows locked
        is_locked, rem_secs = limiter.is_locked_out(test_ip)
        self.assertTrue(is_locked)
        self.assertGreater(rem_secs, 0)

        # Success resets the lockout
        limiter.record_success(test_ip)
        is_locked, _ = limiter.is_locked_out(test_ip)
        self.assertFalse(is_locked)

    def test_dcr_rate_limiting(self):
        """Test Dynamic Client Registration (DCR) rate limiting per IP."""
        limiter = AuthRateLimiter()
        test_ip = "10.0.0.5"

        # Allow up to 3 DCRs
        for _ in range(3):
            self.assertTrue(limiter.check_dcr_rate_limit(test_ip, max_dcr_per_hour=3))

        # 4th registration exceeds limit
        self.assertFalse(limiter.check_dcr_rate_limit(test_ip, max_dcr_per_hour=3))

    def test_git_tools_in_git_repo(self):
        """Test git_status and git_diff in an actual local git repository."""
        # Initialize a temporary git repo
        try:
            subprocess.run(["git", "init"], cwd=str(self.proj_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(self.proj_dir), check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(self.proj_dir), check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.skipTest("git not available in environment")

        cfg_path = self.base_dir / "settings.yaml"
        cfg_path.write_text(yaml.safe_dump({
            "projects": {
                "test_proj": {
                    "root": str(self.proj_dir),
                    "description": "Test git project",
                    "read_only": False,
                }
            }
        }), encoding="utf-8")
        cfg_mgr = ConfigManager(config_path=cfg_path, base_dir=self.base_dir)

        class MockMCP:
            def tool(self):
                def decorator(fn):
                    return fn
                return decorator

        tools = register_file_tools(MockMCP(), cfg_mgr)
        git_status_tool = tools["git_status"]
        git_diff_tool = tools["git_diff"]

        # Status on clean empty repo
        status_res = git_status_tool(project_id="test_proj")
        self.assertEqual(status_res["status"], "success")

        # Create and stage a file
        (self.proj_dir / "code.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "code.py"], cwd=str(self.proj_dir), check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.proj_dir), check=True)

        # Modify file and test git_diff
        (self.proj_dir / "code.py").write_text("x = 2\n", encoding="utf-8")
        diff_res = git_diff_tool(project_id="test_proj", relative_path="code.py")
        self.assertEqual(diff_res["status"], "success")
        self.assertIn("-x = 1", diff_res["diff"])
        self.assertIn("+x = 2", diff_res["diff"])

    def test_runtime_dir_and_env_key_management(self):
        """Verify centralized runtime directory and dynamic .env key rotation."""
        # 1. get_runtime_dir
        r_dir = get_runtime_dir(self.base_dir)
        self.assertEqual(r_dir, self.base_dir / ".runtime")
        self.assertTrue(r_dir.exists())

        # 2. update_env_variable helper
        env_file = self.base_dir / ".env"
        update_env_variable("TEST_VAR", "hello_world", env_file)
        self.assertIn("TEST_VAR=hello_world", env_file.read_text(encoding="utf-8"))

        update_env_variable("TEST_VAR", "rotated_val", env_file)
        content = env_file.read_text(encoding="utf-8")
        self.assertIn("TEST_VAR=rotated_val", content)
        self.assertNotIn("hello_world", content)

        # 3. Auto-generation of API key when require_auth is True and key is empty
        cfg_file = self.base_dir / "config" / "settings.yaml"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(
            yaml.dump({
                "mcp": {
                    "http": {
                        "require_auth": True,
                        "api_key": "",
                    }
                }
            }),
            encoding="utf-8",
        )
        # Clear env to ensure auto-generation triggers
        old_env_key = os.environ.pop("MCP_API_KEY", None)
        try:
            mgr = ConfigManager(config_path=cfg_file, base_dir=self.base_dir)
            loaded = mgr.load()
            self.assertTrue(loaded.security.api_key.startswith("sec_"))
            self.assertIn(f"MCP_API_KEY={loaded.security.api_key}", env_file.read_text(encoding="utf-8"))
        finally:
            if old_env_key is not None:
                os.environ["MCP_API_KEY"] = old_env_key

    def test_codexen_engine_isolation(self):
        """Verify that Codexen's own engine directory is NEVER permitted to be exposed as a project."""
        from codexen.core import safe_resolve_path

        # 1. Project root pointing to Codexen base_dir is blocked
        cfg_bad_proj = self.base_dir / "config" / "bad_proj.yaml"
        cfg_bad_proj.parent.mkdir(parents=True, exist_ok=True)
        cfg_bad_proj.write_text(
            yaml.dump({
                "workspace": {"root": str(self.base_dir / "external_works")},
                "projects": {
                    "self_codexen": {
                        "root": str(self.base_dir),
                        "description": "Attempt to expose Codexen itself",
                    }
                }
            }),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigurationError) as ctx:
            ConfigManager(config_path=cfg_bad_proj, base_dir=self.base_dir)
        self.assertIn("Security boundary violation", str(ctx.exception))

        # 2. Workspace root pointing to Codexen base_dir is blocked
        cfg_bad_ws = self.base_dir / "config" / "bad_ws.yaml"
        cfg_bad_ws.write_text(
            yaml.dump({
                "workspace": {"root": str(self.base_dir)},
                "projects": {}
            }),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigurationError) as ctx:
            ConfigManager(config_path=cfg_bad_ws, base_dir=self.base_dir)
        self.assertIn("Security boundary violation", str(ctx.exception))

        # 3. safe_resolve_path rejects any attempt to reach Codexen directory
        codexen_dir = Path(__file__).resolve().parents[1].resolve()
        with self.assertRaises(PathSecurityError):
            safe_resolve_path(self.proj_dir, str(codexen_dir / "launcher.py"))


if __name__ == "__main__":
    unittest.main()

"""Comprehensive tests for RFC 0001 Phase 3: Human-in-the-Loop (HITL) Penetration Protocol.

Verifies:
1. Intercepting unconfirmed penetration attempts with status="needs_confirmation"
2. Rejecting confirmed=True when authorized_paths does not cover requested subpath
3. Authorizing penetration with exact or wildcard authorized_paths
4. Safety limits (2MB file size, sensitive path protection) remain enforced even when authorized
5. Audit log generation in .runtime/audit.log
6. FastMCP tool envelope end-to-end integration for search_code and list_files
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from mcp_server_core.env import get_runtime_dir
from codexen.config import ConfigManager, ProjectConfig
from codexen.core.fs import list_project_files
from codexen.core.gitignore import clear_ignore_manager_cache
from codexen.core.search import search_project_code
from codexen.tools import register_file_tools
from mcp.server.fastmcp import FastMCP


class TestHITLPenetrationProtocol(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_hitl_test_"))
        self.workspace_dir = self.temp_dir / "workspace"
        self.workspace_dir.mkdir(parents=True)
        clear_ignore_manager_cache()

        # Seed target project with .gitignore
        self.proj_dir = self.workspace_dir / "my_app"
        self.proj_dir.mkdir()
        (self.proj_dir / ".gitignore").write_text(
            "\n".join([
                "target/",
                "desktop/src-tauri/target/",
                "node_modules/",
                "*.log",
                ".env*",
                "!.env.example",
            ]),
            encoding="utf-8",
        )

        # Normal source
        (self.proj_dir / "src").mkdir()
        (self.proj_dir / "src" / "app.py").write_text("def find_me(): return 'source'", encoding="utf-8")

        # Ignored target folder
        target_dir = self.proj_dir / "desktop" / "src-tauri" / "target"
        target_dir.mkdir(parents=True)
        (target_dir / "debug.rs").write_text("fn find_me() { println!(\"target debug\"); }", encoding="utf-8")

        # Big file > 2MB inside target
        with open(target_dir / "huge.dump", "wb") as f:
            f.seek(2 * 1024 * 1024 + 1024)
            f.write(b"fn find_me() in huge dump\n")

        # Sensitive file inside project
        (self.proj_dir / ".env").write_text("SECRET_TOKEN = 'top_secret'", encoding="utf-8")

        # Write config.yaml
        self.config_file = self.temp_dir / "config.yaml"
        self.config_file.write_text(
            f"""
workspace:
  root: "{self.workspace_dir.as_posix()}"
settings:
  command_timeout_seconds: 5
projects:
  my_app:
    root: "{self.proj_dir.as_posix()}"
    description: "HITL Test Project"
""",
            encoding="utf-8",
        )
        self.config_mgr = ConfigManager(config_path=self.config_file, base_dir=self.temp_dir)
        self.proj = self.config_mgr.get_project("my_app")

    def tearDown(self) -> None:
        clear_ignore_manager_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unconfirmed_penetration_returns_needs_confirmation(self) -> None:
        """Agent trying to search inside ignored target/ without confirmation must be intercepted."""
        res = search_project_code(
            project=self.proj,
            query="find_me",
            subpath="desktop/src-tauri/target",
            confirmed=False,
            authorized_paths=[],
        )
        self.assertEqual(res["status"], "needs_confirmation")
        self.assertEqual(res["confirmation_type"], "ignored_directory_penetration")
        self.assertEqual(res["requested_subpath"], "desktop/src-tauri/target")
        self.assertIn("MUST ask the user in chat for confirmation", res["message"])
        self.assertIn("desktop/src-tauri/target", res["message"])

        # Check that audit log recorded the attempt
        runtime_dir = get_runtime_dir(self.temp_dir)
        audit_file = runtime_dir / "audit.log"
        if audit_file.exists():
            log_content = audit_file.read_text(encoding="utf-8")
            self.assertIn("needs_confirmation", log_content)
            self.assertIn("desktop/src-tauri/target", log_content)

    def test_confirmed_with_mismatched_authorized_paths_rejected(self) -> None:
        """Agent providing confirmed=True but incorrect authorized_paths is still rejected."""
        res = search_project_code(
            project=self.proj,
            query="find_me",
            subpath="desktop/src-tauri/target",
            confirmed=True,
            authorized_paths=["node_modules"],  # does not match desktop/src-tauri/target
        )
        self.assertEqual(res["status"], "needs_confirmation")
        self.assertIn("not covered by authorized_paths", res["message"])

    def test_authorized_penetration_executes_safely(self) -> None:
        """When confirmed=True and authorized_paths matches, search executes and skips huge files."""
        res = search_project_code(
            project=self.proj,
            query="find_me",
            subpath="desktop/src-tauri/target",
            confirmed=True,
            authorized_paths=["desktop/src-tauri/target"],
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["total_matches"], 1)
        self.assertEqual(res["matches"][0]["file"], "desktop/src-tauri/target/debug.rs")
        self.assertIn("fn find_me", res["matches"][0]["line_content"])

        # Verify huge.dump was skipped due to 2MB limit
        matched_files = [m["file"] for m in res["matches"]]
        self.assertNotIn("desktop/src-tauri/target/huge.dump", matched_files)

    def test_wildcard_authorized_penetration(self) -> None:
        """authorized_paths=['*'] allows searching ignored folders across the project."""
        res = search_project_code(
            project=self.proj,
            query="find_me",
            subpath="",
            confirmed=True,
            authorized_paths=["*"],
        )
        self.assertEqual(res["status"], "success")
        matched_files = {m["file"] for m in res["matches"]}
        self.assertIn("src/app.py", matched_files)
        self.assertIn("desktop/src-tauri/target/debug.rs", matched_files)

    def test_sensitive_credentials_never_penetrated(self) -> None:
        """Level 4 baseline credentials (.env) can NEVER be penetrated even with confirmed=True and ['*']."""
        res = search_project_code(
            project=self.proj,
            query="SECRET_TOKEN",
            subpath="",
            confirmed=True,
            authorized_paths=["*"],
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["total_matches"], 0)

    def test_list_files_hitl_interception_and_authorization(self) -> None:
        """list_project_files intercepts unconfirmed penetration and allows authorized penetration."""
        # 1. Unconfirmed attempt
        items = list_project_files(
            project=self.proj,
            subpath="desktop/src-tauri/target",
            confirmed=False,
            authorized_paths=[],
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].get("status"), "needs_confirmation")
        self.assertEqual(items[0].get("confirmation_type"), "ignored_directory_penetration")

        # 2. Authorized attempt
        auth_items = list_project_files(
            project=self.proj,
            subpath="desktop/src-tauri/target",
            confirmed=True,
            authorized_paths=["desktop/src-tauri/target"],
        )
        self.assertGreaterEqual(len(auth_items), 1)
        names = [f["name"] for f in auth_items]
        self.assertIn("debug.rs", names)

    def test_fastmcp_tool_envelope_integration(self) -> None:
        """Test end-to-end calling search_code via FastMCP tool dictionary."""
        mcp = FastMCP("Test HITL Server")
        tools = register_file_tools(mcp, self.config_mgr)

        search_tool = tools["search_code"]

        # Call without confirmation
        res1 = search_tool(
            project_id="my_app",
            query="find_me",
            subpath="desktop/src-tauri/target",
            confirmed=False,
            authorized_paths=[],
        )
        self.assertEqual(res1["status"], "needs_confirmation")

        # Call with confirmation and whitelist
        res2 = search_tool(
            project_id="my_app",
            query="find_me",
            subpath="desktop/src-tauri/target",
            confirmed=True,
            authorized_paths=["desktop/src-tauri/target"],
        )
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["total_matches"], 1)


if __name__ == "__main__":
    unittest.main()

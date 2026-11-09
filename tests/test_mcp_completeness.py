"""Comprehensive tests for MCP tool inventory completeness, parameter compatibility, and edge cases."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
import yaml

from app.config import ConfigManager, ProjectConfig
from app.tools import register_file_tools
from app.core import (
    is_sensitive_path,
    normalize_relative_path,
    list_project_files,
    move_project_path,
    read_multiple_project_files,
    get_project_git_log,
)
from app.exceptions import PathSecurityError, ConfigurationError

try:
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer as FastMCP


class MCPCompletenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()
        self.workspace_dir = self.base_dir / "works"
        self.workspace_dir.mkdir(parents=True)

        # Create multiple workspace project folders
        self.proj_alpha = self.workspace_dir / "alpha_proj"
        self.proj_beta = self.workspace_dir / "beta_proj"
        self.proj_gamma = self.workspace_dir / "gamma_unmapped"

        for p in (self.proj_alpha, self.proj_beta, self.proj_gamma):
            p.mkdir(parents=True)

        # Seed files in alpha_proj
        (self.proj_alpha / "README.md").write_text("# Alpha Readme", encoding="utf-8")
        (self.proj_alpha / ".env.example").write_text("API_KEY=your_key_here\n", encoding="utf-8")
        (self.proj_alpha / ".env").write_text("API_KEY=real_secret_123\n", encoding="utf-8")
        (self.proj_alpha / "src").mkdir()
        (self.proj_alpha / "src" / "index.js").write_text("console.log('hi');\n", encoding="utf-8")

        # Seed files in gamma_unmapped
        (self.proj_gamma / "gamma.txt").write_text("Hello from gamma!", encoding="utf-8")

        # Setup config.yaml with only alpha_proj and beta_proj explicitly declared
        self.config_path = self.base_dir / "config.yaml"
        config_data = {
            "workspace": {
                "root": str(self.workspace_dir),
                "allow_create_project": True,
            },
            "projects": {
                "alpha_proj": {
                    "root": str(self.proj_alpha),
                    "description": "Alpha Project Explicit",
                    "read_only": False,
                },
                "beta_proj": {
                    "root": str(self.proj_beta),
                    "description": "Beta Project Explicit",
                    "read_only": True,
                },
            },
            "permissions": {
                "allow_file_delete": True,
                "allow_command_execution": True,
            },
        }
        self.config_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")

        self.cfg_mgr = ConfigManager(config_path=self.config_path, base_dir=self.base_dir)
        self.mcp = FastMCP("Test Completeness MCP")
        self.tools = register_file_tools(self.mcp, self.cfg_mgr)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_only_registered_projects_listed(self):
        """Verify that list_projects only lists explicitly registered projects, not unmapped workspace folders."""
        projects = self.tools["list_projects"]()
        project_ids = [p["project_id"] for p in projects]

        # Explicitly configured projects must be listed
        self.assertIn("alpha_proj", project_ids)
        self.assertIn("beta_proj", project_ids)

        # Unregistered folders in workspace root must NOT be listed
        self.assertNotIn("gamma_unmapped", project_ids)
        self.assertEqual(len(projects), 2)

        # Unregistered project access must be rejected with ConfigurationError
        with self.assertRaises(ConfigurationError):
            self.tools["read_file"](project_id="gamma_unmapped", relative_path="gamma.txt")

    def test_create_project_auto_registers_in_workspace(self):
        """Verify that create_project creates directory under workspace_root and registers it immediately."""
        res = self.tools["create_project"](project_id="gamma_created", description="Created Project")
        self.assertEqual(res["status"], "success")

        # Now it must be listed in registered projects
        projects = self.tools["list_projects"]()
        project_ids = [p["project_id"] for p in projects]
        self.assertIn("gamma_created", project_ids)

        # And immediately accessible
        self.tools["write_file"](project_id="gamma_created", relative_path="welcome.txt", content="Welcome!")
        self.assertEqual(self.tools["read_file"](project_id="gamma_created", relative_path="welcome.txt"), "Welcome!")

    def test_project_name_parameter_compatibility(self):
        """Verify that passing project_name instead of project_id works across all tools."""
        content = self.tools["read_file"](project_name="alpha_proj", relative_path="README.md")
        self.assertEqual(content, "# Alpha Readme")

        files = self.tools["list_files"](project_name="alpha_proj")
        self.assertTrue(any(f["name"] == "README.md" for f in files))

    def test_path_parameter_compatibility(self):
        """Verify that passing 'path' instead of 'relative_path' or 'subpath' works seamlessly."""
        # read_file with path
        content = self.tools["read_file"](project_id="alpha_proj", path="README.md")
        self.assertEqual(content, "# Alpha Readme")

        # write_file with path
        self.tools["write_file"](project_id="alpha_proj", path="test_path.txt", content="created via path")
        self.assertEqual(self.tools["read_file"](project_id="alpha_proj", path="test_path.txt"), "created via path")

        # list_files with path
        sub_files = self.tools["list_files"](project_id="alpha_proj", path="src")
        self.assertTrue(any(f["name"] == "index.js" for f in sub_files))

    def test_leading_slash_graceful_normalization(self):
        """Verify that leading slashes (/README.md, subpath='/') are normalized rather than raising PathSecurityError."""
        content = self.tools["read_file"](project_id="alpha_proj", relative_path="/README.md")
        self.assertEqual(content, "# Alpha Readme")

        root_files = self.tools["list_files"](project_id="alpha_proj", subpath="/")
        self.assertTrue(any(f["name"] == "README.md" for f in root_files))

    def test_env_example_readable_and_listed(self):
        """Verify .env.example template files are permitted while real .env secrets remain strictly protected."""
        # 1. .env.example should be readable
        example_content = self.tools["read_file"](project_id="alpha_proj", relative_path=".env.example")
        self.assertIn("API_KEY=your_key_here", example_content)

        # 2. .env.example should be visible in list_files
        files = self.tools["list_files"](project_id="alpha_proj")
        names = [f["name"] for f in files]
        self.assertIn(".env.example", names)

        # 3. Real .env secret file must still be blocked from reading and hidden from listing
        self.assertNotIn(".env", names)
        with self.assertRaises(PathSecurityError):
            self.tools["read_file"](project_id="alpha_proj", relative_path=".env")

    def test_list_project_files_alias(self):
        """Verify list_project_files and list_directory aliases exist and function correctly."""
        res1 = self.tools["list_project_files"](project_id="alpha_proj")
        res2 = self.tools["list_directory"](project_id="alpha_proj")
        self.assertEqual(len(res1), len(res2))
        self.assertTrue(any(f["name"] == "README.md" for f in res1))

    def test_read_multiple_files_tool(self):
        """Verify batch reading of multiple files in a single tool call."""
        res = self.tools["read_multiple_files"](
            project_id="alpha_proj",
            paths=["README.md", ".env.example"],
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["files_read"], 2)
        self.assertEqual(res["contents"]["README.md"], "# Alpha Readme")
        self.assertIn("your_key_here", res["contents"][".env.example"])

        # Also verify batch_read_files alias
        alias_res = self.tools["batch_read_files"](
            project_id="alpha_proj",
            paths=["README.md"],
        )
        self.assertEqual(alias_res["status"], "success")

    def test_move_and_rename_file_tools(self):
        """Verify move_path, move_file, and rename_file operations."""
        self.tools["write_file"](project_id="alpha_proj", relative_path="old_name.txt", content="move me")

        # 1. Rename inside project
        rename_res = self.tools["rename_file"](
            project_id="alpha_proj",
            source_path="old_name.txt",
            target_path="renamed.txt",
        )
        self.assertEqual(rename_res["status"], "success")
        self.assertEqual(self.tools["read_file"](project_id="alpha_proj", relative_path="renamed.txt"), "move me")
        with self.assertRaises(FileNotFoundError):
            self.tools["read_file"](project_id="alpha_proj", relative_path="old_name.txt")

        # 2. Move cross project (create target registered project first)
        self.tools["create_project"](project_id="gamma_target")
        move_res = self.tools["move_path"](
            source_project_id="alpha_proj",
            source_path="renamed.txt",
            target_project_id="gamma_target",
            target_path="imported.txt",
        )
        self.assertEqual(move_res["status"], "success")
        self.assertEqual(self.tools["read_file"](project_id="gamma_target", relative_path="imported.txt"), "move me")

    def test_delete_directory_recursive(self):
        """Verify delete_file with recursive=True deletes non-empty directories safely."""
        self.tools["create_directory"](project_id="alpha_proj", relative_path="temp_dir/sub")
        self.tools["write_file"](project_id="alpha_proj", relative_path="temp_dir/sub/file.txt", content="temp")

        # Delete non-empty without recursive=True should fail cleanly
        with self.assertRaises(Exception):
            self.tools["delete_file"](project_id="alpha_proj", relative_path="temp_dir", recursive=False)

        # Delete with recursive=True or delete_directory should succeed
        del_res = self.tools["delete_directory"](project_id="alpha_proj", relative_path="temp_dir", recursive=True)
        self.assertEqual(del_res["status"], "success")
        self.assertFalse((self.proj_alpha / "temp_dir").exists())

    def test_git_log_tool(self):
        """Verify git_log tool handles non-git and git repositories gracefully."""
        # Non-git repository test
        res = self.tools["git_log"](project_id="alpha_proj")
        self.assertEqual(res["status"], "not_a_git_repository")

    def test_mcp_gateway_single_content_block_serialization(self):
        """Verify FastMCP serializes list_files and list_projects as a SINGLE TextContent block.
        
        This definitively resolves the MCP serialization truncation bug where intermediate
        gateways only returned the last TextContent element (e.g. tunnel_shared.py).
        """
        async def run_test():
            # 1. Test list_files single block serialization
            raw_res = await self.mcp.call_tool("list_files", {
                "project_id": "alpha_proj",
                "recursive": True,
            })
            content = raw_res[0] if isinstance(raw_res, tuple) else raw_res
            # FastMCP must return exactly 1 TextContent block containing the full JSON payload
            self.assertEqual(len(content), 1, "list_files must return exactly 1 content block to prevent gateway truncation")
            payload = json.loads(content[0].text)
            self.assertEqual(payload["status"], "success")
            self.assertGreater(payload["total"], 1)
            self.assertEqual(len(payload["files"]), payload["total"])
            
            # Verify ALL files are present in payload without any being dropped
            file_names = [f["name"] for f in payload["files"]]
            self.assertIn("README.md", file_names)
            self.assertIn("index.js", file_names)

            # 2. Test list_projects single block serialization
            proj_res = await self.mcp.call_tool("list_projects", {})
            proj_content = proj_res[0] if isinstance(proj_res, tuple) else proj_res
            self.assertEqual(len(proj_content), 1, "list_projects must return exactly 1 content block")
            proj_payload = json.loads(proj_content[0].text)
            self.assertEqual(proj_payload["status"], "success")
            self.assertGreaterEqual(proj_payload["total"], 2)
            proj_ids = [p["project_id"] for p in proj_payload["projects"]]
            self.assertIn("alpha_proj", proj_ids)
            self.assertIn("beta_proj", proj_ids)

        asyncio.run(run_test())

    def test_list_files_pattern_and_extension_filters(self):
        """Verify pattern (glob) and extension filtering in list_files."""
        # Test pattern filter
        js_files = self.tools["list_files"](project_id="alpha_proj", pattern="*.js", recursive=True)
        self.assertEqual(len(js_files), 1)
        self.assertEqual(js_files[0]["name"], "index.js")

        # Test extension filter
        md_files = self.tools["list_files"](project_id="alpha_proj", extension=".md", recursive=True)
        self.assertEqual(len(md_files), 1)
        self.assertEqual(md_files[0]["name"], "README.md")

    def test_get_project_info_and_search_aliases(self):
        """Verify get_project_info tool and search_files / find_in_files aliases."""
        info = self.tools["get_project_info"](project_id="alpha_proj")
        self.assertEqual(info["status"], "success")
        self.assertEqual(info["project_id"], "alpha_proj")
        self.assertFalse(info["read_only"])

        alias_info = self.tools["get_project"](project_id="alpha_proj")
        self.assertEqual(alias_info["project_id"], "alpha_proj")

        # search_files alias
        s1 = self.tools["search_files"](project_id="alpha_proj", query="Alpha Readme")
        self.assertEqual(s1["status"], "success")
        self.assertEqual(s1["total_matches"], 1)

        # find_in_files alias
        s2 = self.tools["find_in_files"](project_id="alpha_proj", query="console.log")
        self.assertEqual(s2["status"], "success")
        self.assertEqual(s2["total_matches"], 1)


if __name__ == "__main__":
    unittest.main()

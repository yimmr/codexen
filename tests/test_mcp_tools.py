"""Integration tests for MCP file server tools."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import yaml

from app.config import ConfigManager
from app.tools import register_file_tools
from app.exceptions import PathSecurityError, CommandSecurityError

try:
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer as FastMCP


class MCPToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()
        self.workspace_dir = self.base_dir / "works"
        self.workspace_dir.mkdir(parents=True)

        # Create two target project folders
        self.proj_a = self.workspace_dir / "project_alpha"
        self.proj_b = self.workspace_dir / "project_beta"
        self.proj_a.mkdir(parents=True)
        self.proj_b.mkdir(parents=True)

        # Write sample file in proj_a
        (self.proj_a / "hello.txt").write_text("Hello from Alpha!", encoding="utf-8")
        (self.proj_a / "sample.py").write_text("def sample_func():\n    return 42\n", encoding="utf-8")

        # Create config file (settings.yaml)
        self.config_path = self.base_dir / "settings.yaml"
        config_data = {
            "workspace": {
                "root": str(self.workspace_dir),
                "allow_create_project": True,
            },
            "projects": {
                "alpha": {
                    "root": str(self.proj_a),
                    "description": "Alpha project",
                    "read_only": False,
                },
                "beta": {
                    "root": str(self.proj_b),
                    "description": "Beta project readonly",
                    "read_only": True,
                },
            },
            "mcp": {
                "http": {"port": 8788},
                "permissions": {
                    "max_read_chars": 50000,
                    "allow_file_delete": True,
                    "allow_command_execution": True,
                    "command_timeout_seconds": 10,
                },
            },
        }
        self.config_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")

        # Setup ConfigManager and FastMCP
        self.cfg_mgr = ConfigManager(config_path=self.config_path, base_dir=self.base_dir)
        self.mcp = FastMCP("Test File MCP")
        self.tools = register_file_tools(self.mcp, self.cfg_mgr)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_projects(self):
        projects = self.tools["list_projects"]()
        names = [p["project_id"] for p in projects]
        self.assertIn("alpha", names)
        self.assertIn("beta", names)

        beta_info = next(p for p in projects if p["project_id"] == "beta")
        self.assertTrue(beta_info["read_only"])
        self.assertTrue(beta_info["exists"])

    def test_read_project_file_and_alias(self):
        # 1. Using read_project_file (literary_workflow_v1 signature)
        content1 = self.tools["read_project_file"](project_id="alpha", relative_path="hello.txt")
        self.assertEqual(content1, "Hello from Alpha!")

        # 2. Using read_file alias
        content2 = self.tools["read_file"](project_id="alpha", relative_path="hello.txt")
        self.assertEqual(content2, "Hello from Alpha!")

    def test_write_project_file_and_alias(self):
        res = self.tools["write_project_file"](
            project_id="alpha",
            relative_path="sub/new.txt",
            content="New content from MCP tool",
        )
        self.assertEqual(res["status"], "success")

        content = self.tools["read_project_file"](project_id="alpha", relative_path="sub/new.txt")
        self.assertEqual(content, "New content from MCP tool")

    def test_list_files_tool(self):
        self.tools["write_project_file"](project_id="alpha", relative_path="test1.txt", content="1")
        self.tools["write_project_file"](project_id="alpha", relative_path="sub/test2.txt", content="2")

        files = self.tools["list_files"](project_id="alpha", recursive=True)
        rel_paths = [f["relative_path"] for f in files]
        self.assertIn("hello.txt", rel_paths)
        self.assertIn("test1.txt", rel_paths)
        self.assertIn("sub/test2.txt", rel_paths)

    def test_edit_snippet_tool(self):
        self.tools["write_project_file"](project_id="alpha", relative_path="config.json", content='{"version": "1.0.0"}')
        self.tools["edit_file_snippet"](
            project_id="alpha",
            relative_path="config.json",
            search_text='"1.0.0"',
            replace_text='"1.0.1"',
        )
        content = self.tools["read_project_file"](project_id="alpha", relative_path="config.json")
        self.assertIn('"version": "1.0.1"', content)

    def test_security_violation_via_tool(self):
        with self.assertRaises(PathSecurityError):
            self.tools["read_project_file"](project_id="alpha", relative_path="../outside.txt")

    def test_create_project_tool(self):
        res = self.tools["create_project"](project_id="new_proj_tool", description="Tool created project")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["project_id"], "new_proj_tool")

        # Verify it can be listed and written to immediately
        self.tools["write_file"](project_id="new_proj_tool", relative_path="init.txt", content="init")
        self.assertEqual(self.tools["read_file"](project_id="new_proj_tool", relative_path="init.txt"), "init")

    def test_copy_path_and_directory_tools(self):
        # 1. Intra-project copy
        res1 = self.tools["copy_path"](
            source_project_id="alpha",
            source_path="hello.txt",
            target_project_id="alpha",
            target_path="hello_copied.txt",
        )
        self.assertEqual(res1["status"], "success")
        self.assertEqual(self.tools["read_file"](project_id="alpha", relative_path="hello_copied.txt"), "Hello from Alpha!")

        # 2. copy_directory alias
        self.tools["create_project"](project_id="target_copy_proj")
        res2 = self.tools["copy_directory"](
            source_project_id="alpha",
            source_path=".",
            target_project_id="target_copy_proj",
            target_path="",
        )
        self.assertEqual(res2["status"], "success")
        self.assertTrue(res2["files_copied"] >= 2)

    def test_zip_tools(self):
        # Pack
        pack = self.tools["create_zip_archive"](project_id="alpha", output_zip_name="alpha.zip")
        self.assertEqual(pack["status"], "success")

        # Extract
        ext = self.tools["extract_zip_archive"](
            project_id="alpha",
            zip_relative_path="alpha.zip",
            target_subpath="extracted",
        )
        self.assertEqual(ext["status"], "success")
        self.assertTrue((self.proj_a / "extracted" / "hello.txt").exists())

    def test_command_tools(self):
        # 1. run_terminal_command
        res1 = self.tools["run_terminal_command"](
            project_id="alpha",
            command='python -c "print(\'Tool Command Test\')"',
        )
        self.assertEqual(res1["status"], "success")
        self.assertIn("Tool Command Test", res1["stdout"])

        # 2. run_command alias
        res2 = self.tools["run_command"](
            project_id="alpha",
            command='python -c "print(\'Alias Test\')"',
        )
        self.assertEqual(res2["status"], "success")
        self.assertIn("Alias Test", res2["stdout"])

        # 3. Confirmation required command
        res3 = self.tools["run_command"](
            project_id="alpha",
            command="del non_existent.txt",
            confirmed=False,
        )
        self.assertEqual(res3["status"], "needs_confirmation")

        # 4. Prohibited destructive command raises CommandSecurityError
        with self.assertRaises(CommandSecurityError):
            self.tools["run_command"](
                project_id="alpha",
                command="format C: /fs:NTFS",
            )

    def test_search_code_tool(self):
        res = self.tools["search_code"](
            project_id="alpha",
            query="sample_func",
            file_extensions=[".py"],
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["total_matches"], 1)
        self.assertEqual(res["matches"][0]["file"], "sample.py")

    def test_reload_config_tool(self):
        proj_c = self.base_dir / "project_gamma"
        proj_c.mkdir()

        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["projects"]["gamma"] = {
            "root": str(proj_c),
            "description": "Gamma project added dynamically",
            "read_only": False,
        }
        self.config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

        res = self.tools["reload_projects_config"]()
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["projects_count"], 3)

        projects = self.tools["list_projects"]()
        names = [p["project_id"] for p in projects]
        self.assertIn("gamma", names)


if __name__ == "__main__":
    unittest.main()

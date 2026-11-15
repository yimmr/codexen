"""Regression and robustness tests for async tool execution, DoS protection, and schema normalization."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from mcp.server.fastmcp import FastMCP
from codexen.config import ConfigManager, ProjectConfig
from codexen.core.search import search_project_code
from codexen.tools import register_file_tools


class AsyncSearchConcurrencyAndSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_dir = Path(self.temp_dir) / "works"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        self.proj_root = self.workspace_dir / "test_app"
        self.proj_root.mkdir(parents=True, exist_ok=True)

        # 1. Normal source code
        (self.proj_root / "src").mkdir()
        (self.proj_root / "src" / "main.py").write_text("def start_engine():\n    return 'Engine online'\n", encoding="utf-8")
        (self.proj_root / "src" / "dispatcher.py").write_text("class EventDispatcher:\n    def dispatch(self):\n        pass\n", encoding="utf-8")

        # 2. Deep compilation target directory (like Tauri / Cargo)
        target_dir = self.proj_root / "desktop" / "src-tauri" / "target" / "debug" / "deps"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "huge_dep.d").write_text("EventDispatcher dependency line\n" * 1000, encoding="utf-8")

        # 3. Frontend dist and node_modules
        dist_dir = self.proj_root / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "bundle.min.js").write_text("var EventDispatcher=function(){};" * 500, encoding="utf-8")

        # 4. Large file (> 2MB)
        large_file = self.proj_root / "src" / "big_dump.txt"
        large_file.write_text("A" * (3 * 1024 * 1024), encoding="utf-8")

        # Config file setup
        self.config_file = Path(self.temp_dir) / "config.yaml"
        self.config_file.write_text(f"""
workspace:
  root: "{self.workspace_dir.as_posix()}"
projects:
  test_app:
    root: "{self.proj_root.as_posix()}"
    read_only: false
permissions:
  allow_file_read: true
  allow_command_execution: true
""", encoding="utf-8")

        self.config_mgr = ConfigManager(config_path=self.config_file, base_dir=Path(self.temp_dir))
        self.mcp = FastMCP("Test MCP Concurrency Server")
        self.tools = register_file_tools(self.mcp, self.config_mgr)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_search_prunes_target_dist_and_large_files(self) -> None:
        """Verify search ignores target/, dist/, and files > 2MB, finishing in milliseconds."""
        proj = self.config_mgr.get_project("test_app")
        start = time.monotonic()
        res = search_project_code(proj, query="EventDispatcher")
        elapsed_ms = (time.monotonic() - start) * 1000

        self.assertLess(elapsed_ms, 500, f"Search took too long ({elapsed_ms}ms)")
        self.assertEqual(res["total_matches"], 1)
        self.assertEqual(res["matches"][0]["file"], "src/dispatcher.py")

        # Verify nothing from target, dist or big dump matched
        for m in res["matches"]:
            self.assertFalse(m["file"].startswith("desktop/src-tauri/target"))
            self.assertFalse(m["file"].startswith("dist"))

    def test_all_mcp_tools_are_async_and_nonblocking(self) -> None:
        """Verify all registered MCP tools are marked is_async=True in FastMCP."""
        tool_names = ["search_code", "read_file", "write_file", "git_status", "run_command"]
        for name in tool_names:
            tool_obj = self.mcp._tool_manager.get_tool(name)
            self.assertIsNotNone(tool_obj, f"Tool {name} not found")
            self.assertTrue(tool_obj.is_async, f"Tool {name} should be async non-blocking")

    def test_schema_strictness_no_anyof(self) -> None:
        """Verify none of the 46 tools produce anyOf union types in their schema properties."""
        tools = asyncio.run(self.mcp.list_tools())
        self.assertGreaterEqual(len(tools), 40)
        for t in tools:
            props = t.inputSchema.get("properties", {})
            for p_name, p_val in props.items():
                self.assertNotIn("anyOf", p_val, f"Tool {t.name} prop {p_name} contains anyOf")
                self.assertNotIn("oneOf", p_val, f"Tool {t.name} prop {p_name} contains oneOf")
                self.assertNotIn("allOf", p_val, f"Tool {t.name} prop {p_name} contains allOf")

    def test_async_call_tool_concurrency(self) -> None:
        """Verify calling search_code via FastMCP does not block concurrent tool calls."""
        async def run_concurrent() -> None:
            t1 = self.mcp.call_tool("search_code", {"project_id": "test_app", "query": "start_engine"})
            t2 = self.mcp.call_tool("read_file", {"project_id": "test_app", "relative_path": "src/main.py"})
            r1, r2 = await asyncio.gather(t1, t2)
            self.assertIn("def start_engine", str(r1))
            self.assertIn("Engine online", str(r2))

        asyncio.run(run_concurrent())


if __name__ == "__main__":
    unittest.main()

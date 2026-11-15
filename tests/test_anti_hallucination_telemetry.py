"""Comprehensive tests for RFC 0001 Phase 4: Anti-Hallucination Telemetry & Adaptive Pagination.

Verifies:
1. read_file self-healing pagination on 50,000-line large files with accurate next_start_line and hint
2. Following the hint with start_line/end_line accurately reads consecutive segments
3. search_code returns rich search_audit telemetry (large_files_skipped, truncated_lines_count, directories_pruned)
4. Dynamic max_file_size_mb tuning allows inspecting files that were previously skipped
5. ReDoS defense: lines exceeding MAX_SEARCH_LINE_CHARS are safely truncated with telemetry tracking
6. Zero-hallucination guarantees across FastMCP tool wrappers
"""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from codexen.config import ConfigManager
from codexen.core.fs import read_project_file
from codexen.core.gitignore import clear_ignore_manager_cache
from codexen.core.search import search_project_code
from codexen.tools import register_file_tools
from mcp.server.fastmcp import FastMCP


class TestAntiHallucinationTelemetry(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_anti_hallucination_"))
        self.workspace_dir = self.temp_dir / "workspace"
        self.workspace_dir.mkdir(parents=True)
        clear_ignore_manager_cache()

        self.proj_dir = self.workspace_dir / "telemetry_app"
        self.proj_dir.mkdir()

        # Seed .gitignore with build directory
        (self.proj_dir / ".gitignore").write_text("target/\nnode_modules/\n", encoding="utf-8")

        # 1. Normal source file
        (self.proj_dir / "src").mkdir()
        (self.proj_dir / "src" / "main.py").write_text("def find_token(): return 'active'", encoding="utf-8")

        # 2. Ignored directory to verify directories_pruned in search_audit
        (self.proj_dir / "target").mkdir()
        (self.proj_dir / "target" / "junk.txt").write_text("junk", encoding="utf-8")

        # 3. Create a 3MB text file to test large_files_skipped in search_audit
        with open(self.proj_dir / "src" / "big_dump.txt", "wb") as f:
            f.write(b" " * (3 * 1024 * 1024))
            f.write(b"\ndef find_token(): pass\n")

        # 4. Create a 50,000-line file to test read_file self-healing pagination
        large_file_path = self.proj_dir / "src" / "large_service.py"
        with open(large_file_path, "w", encoding="utf-8") as f:
            for i in range(1, 50001):
                f.write(f"Line {i}: def func_{i}(): return {i}\n")

        # 5. Create a file with a single line of 10,000 characters to test ReDoS line truncation
        long_line = "TOKEN " + ("x" * 9000) + " END\n"
        (self.proj_dir / "src" / "minified.js").write_text(long_line, encoding="utf-8")

        # Config setup
        self.config_file = self.temp_dir / "config.yaml"
        self.config_file.write_text(
            f"""
workspace:
  root: "{self.workspace_dir.as_posix()}"
settings:
  max_read_chars: 10000
projects:
  telemetry_app:
    root: "{self.proj_dir.as_posix()}"
""",
            encoding="utf-8",
        )
        self.config_mgr = ConfigManager(config_path=self.config_file, base_dir=self.temp_dir)
        self.proj = self.config_mgr.get_project("telemetry_app")

        self.mcp = FastMCP("Telemetry Server")
        self.tools = register_file_tools(self.mcp, self.config_mgr)

    def tearDown(self) -> None:
        clear_ignore_manager_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_read_file_self_healing_on_50k_line_file(self) -> None:
        """Calling read_file on 50,000-line file returns self-healing envelope with exact next_start_line and hint."""
        read_tool = self.tools["read_file"]
        res = read_tool(
            project_id="telemetry_app",
            relative_path="src/large_service.py",
        )
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["truncated"])
        self.assertEqual(res["truncation_reason"], "max_read_chars_limit_reached")
        self.assertEqual(res["total_file_lines"], 50000)
        self.assertEqual(res["returned_start_line"], 1)
        self.assertGreater(res["returned_end_line"], 10)
        self.assertEqual(res["next_start_line"], res["returned_end_line"] + 1)
        self.assertIn("File truncated due to size limit", res["hint"])
        self.assertIn(f"start_line={res['next_start_line']}", res["hint"])

        # Follow the hint to read page 2
        next_s = res["next_start_line"]
        next_e = next_s + 100
        page2 = read_tool(
            project_id="telemetry_app",
            relative_path="src/large_service.py",
            start_line=next_s,
            end_line=next_e,
        )
        self.assertIsInstance(page2, str)
        self.assertIn(f"Line {next_s}:", page2)
        self.assertIn(f"Line {next_e}:", page2)

    def test_search_code_audit_records_skipped_large_files(self) -> None:
        """search_code returns search_audit with accurate telemetry for 3MB skipped file."""
        search_tool = self.tools["search_code"]
        res = search_tool(
            project_id="telemetry_app",
            query="find_token",
            max_file_size_mb=2,  # 3MB file must be skipped
        )
        self.assertEqual(res["status"], "success")
        self.assertIn("search_audit", res)

        audit = res["search_audit"]
        self.assertIn("large_files_skipped", audit)
        self.assertIn("directories_pruned", audit)

        # 1. 3MB file was skipped and recorded
        skipped_names = [f["file"] for f in audit["large_files_skipped"]]
        self.assertIn("src/big_dump.txt", skipped_names)
        dump_info = next(f for f in audit["large_files_skipped"] if f["file"] == "src/big_dump.txt")
        self.assertGreaterEqual(dump_info["size_mb"], 3.0)
        self.assertIn("Exceeded max_file_size_mb", dump_info["reason"])

        # 2. Ignored target directory was recorded in directories_pruned
        self.assertTrue(any("target" in d for d in audit["directories_pruned"]))

    def test_dynamic_max_file_size_mb_allows_tuning(self) -> None:
        """Tuning max_file_size_mb=5 allows searching the 3MB file without skipping."""
        search_tool = self.tools["search_code"]
        res = search_tool(
            project_id="telemetry_app",
            query="find_token",
            max_file_size_mb=5,  # 5MB threshold allows the 3MB file
        )
        self.assertEqual(res["status"], "success")
        matched_files = {m["file"] for m in res["matches"]}
        self.assertIn("src/main.py", matched_files)
        self.assertIn("src/big_dump.txt", matched_files)

        # In this run, no large files were skipped
        audit = res["search_audit"]
        self.assertEqual(len(audit["large_files_skipped"]), 0)

    def test_redos_protection_line_truncation_telemetry(self) -> None:
        """Lines exceeding 5,000 chars are safely truncated and counted in truncated_lines_count."""
        search_tool = self.tools["search_code"]
        res = search_tool(
            project_id="telemetry_app",
            query="TOKEN",
        )
        self.assertEqual(res["status"], "success")
        audit = res["search_audit"]
        self.assertGreaterEqual(audit["truncated_lines_count"], 1)

    def test_core_read_project_file_backward_compatibility(self) -> None:
        """read_project_file raises FileSizeLimitError when auto_paginate=False (backward compatibility)."""
        from codexen.exceptions import FileSizeLimitError

        with self.assertRaises(FileSizeLimitError):
            read_project_file(
                self.proj,
                "src/large_service.py",
                max_chars=100,
                auto_paginate=False,
            )


if __name__ == "__main__":
    unittest.main()

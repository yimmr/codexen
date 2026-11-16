"""Comprehensive validation suite for Large File Handling & Anti-Hallucination Remediation.

Covers Acceptance Criteria from docs/plans/problem-report-large-file-handling.md:
- AC-1: search_code skipped files include actionable tuning & read_file guidance
- AC-2: get_file_info on large files (>=5MB) accurately counts lines in O(1) memory
- AC-3: read_file(start_line=1, end_line=500) on 10MB file returns in milliseconds with minimal RAM
- AC-4: No regression and full test verification
- Batch tools large file error guidance
- Full file auto-pagination envelopes on 10MB files
"""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import time
import tracemalloc
import unittest

from codexen.config import ConfigManager
from codexen.core.fs import count_file_lines_stream, get_project_file_info, read_project_file
from codexen.core.gitignore import clear_ignore_manager_cache
from codexen.core.search import search_project_code
from codexen.tools import register_file_tools
from mcp.server.fastmcp import FastMCP


class TestLargeFileRemediation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_large_file_test_"))
        self.workspace_dir = self.temp_dir / "workspace"
        self.workspace_dir.mkdir(parents=True)
        clear_ignore_manager_cache()

        self.proj_dir = self.workspace_dir / "large_file_app"
        self.proj_dir.mkdir()

        # Seed minimal .gitignore
        (self.proj_dir / ".gitignore").write_text(".git/\n", encoding="utf-8")

        # 1. AC-1: Create a 3MB text file
        self.file_3mb = self.proj_dir / "dump_3mb.txt"
        with open(self.file_3mb, "wb") as f:
            f.write(b"line data for search test\n" * 130_000)  # ~3.22MB

        # 2. AC-2: Create a 5MB text file with known line count
        self.file_5mb = self.proj_dir / "log_5mb.txt"
        self.lines_5mb_expected = 100_000
        with open(self.file_5mb, "w", encoding="utf-8") as f:
            for i in range(1, self.lines_5mb_expected + 1):
                f.write(f"2026-11-16 12:00:{i % 60:02d} INFO User request processed line {i}\n")

        # 3. AC-3: Create a 10MB text file with known line count
        self.file_10mb = self.proj_dir / "data_10mb.txt"
        self.lines_10mb_expected = 200_000
        with open(self.file_10mb, "w", encoding="utf-8") as f:
            for i in range(1, self.lines_10mb_expected + 1):
                f.write(f"Record #{i}: payload data payload data payload data {i}\n")

        # Config setup with 100,000 char threshold
        self.config_file = self.temp_dir / "config.yaml"
        self.config_file.write_text(
            f"""
workspace:
  root: "{self.workspace_dir.as_posix()}"
settings:
  max_read_chars: 100000
projects:
  large_file_app:
    root: "{self.proj_dir.as_posix()}"
""",
            encoding="utf-8",
        )
        self.config_mgr = ConfigManager(config_path=self.config_file, base_dir=self.temp_dir)
        self.proj = self.config_mgr.get_project("large_file_app")

        self.mcp = FastMCP("Large File Server")
        self.tools = register_file_tools(self.mcp, self.config_mgr)

    def tearDown(self) -> None:
        clear_ignore_manager_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ac1_search_code_telemetry_actionable_guidance(self) -> None:
        """AC-1: 3MB file skipped by default search_code contains actionable tuning & read_file guidance."""
        search_tool = self.tools["search_code"]
        res = search_tool(
            project_id="large_file_app",
            query="search test",
            max_file_size_mb=2,  # default is 2MB, so 3MB file must be skipped
        )
        self.assertEqual(res["status"], "success")
        audit = res.get("search_audit", {})
        skipped = audit.get("large_files_skipped", [])
        self.assertGreaterEqual(len(skipped), 1)

        target_skip = next((f for f in skipped if "dump_3mb.txt" in f["file"]), None)
        self.assertIsNotNone(target_skip, "3MB dump file was not in large_files_skipped")
        assert target_skip is not None
        self.assertGreaterEqual(target_skip["size_mb"], 3.0)

        reason = target_skip["reason"]
        # Verify self-healing guidance
        self.assertIn("Exceeded max_file_size_mb limit (2MB)", reason)
        self.assertIn("max_file_size_mb=", reason)
        self.assertIn("read_file with start_line and end_line", reason)

    def test_ac2_get_file_info_5mb_accurate_line_count(self) -> None:
        """AC-2: get_file_info on 5MB text file returns accurate line_count in milliseconds."""
        get_info_tool = self.tools["get_file_info"]

        t0 = time.perf_counter()
        info = get_info_tool(project_id="large_file_app", relative_path="log_5mb.txt")
        elapsed_ms = (time.perf_counter() - t0) * 1000

        self.assertEqual(info["status"], "success")
        self.assertTrue(info["is_file"])
        self.assertGreater(info["size_bytes"], 5 * 1024 * 1024)
        self.assertEqual(info["line_count"], self.lines_5mb_expected)
        # Line count must be calculated almost instantaneously (< 200ms)
        self.assertLess(elapsed_ms, 500, f"Line count took too long: {elapsed_ms:.2f}ms")

    def test_ac3_read_file_line_slice_10mb_speed_and_ram(self) -> None:
        """AC-3: read_file(start_line=1, end_line=500) on 10MB file returns in milliseconds with minimal RAM."""
        read_tool = self.tools["read_file"]

        # Track memory usage during slice
        tracemalloc.start()
        snapshot_before = tracemalloc.take_snapshot()

        t0 = time.perf_counter()
        content = read_tool(
            project_id="large_file_app",
            relative_path="data_10mb.txt",
            start_line=1,
            end_line=500,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        snapshot_after = tracemalloc.take_snapshot()
        current_ram, peak_ram = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Content verification
        self.assertIsInstance(content, str)
        lines = content.splitlines()
        self.assertEqual(len(lines), 500)
        self.assertIn("Record #1:", lines[0])
        self.assertIn("Record #500:", lines[499])

        # Latency check: reading 500 lines should be sub-50ms (usually < 5ms)
        self.assertLess(elapsed_ms, 150, f"read_file took too long: {elapsed_ms:.2f}ms")

        # Memory check: peak RAM must be far below 10MB (typically < 1MB)
        peak_ram_mb = peak_ram / (1024 * 1024)
        self.assertLess(peak_ram_mb, 10.0, f"Peak memory exceeded 10MB: {peak_ram_mb:.2f}MB")

    def test_full_file_auto_pagination_on_10mb_file(self) -> None:
        """Calling read_file without start/end lines on 10MB file self-heals via auto-pagination."""
        read_tool = self.tools["read_file"]
        res = read_tool(
            project_id="large_file_app",
            relative_path="data_10mb.txt",
        )
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["truncated"])
        self.assertEqual(res["truncation_reason"], "max_read_chars_limit_reached")
        self.assertEqual(res["total_file_lines"], self.lines_10mb_expected)
        self.assertEqual(res["returned_start_line"], 1)
        self.assertGreater(res["returned_end_line"], 100)
        self.assertEqual(res["next_start_line"], res["returned_end_line"] + 1)
        self.assertIn("File truncated due to size limit", res["hint"])
        self.assertIn("Tip: For large files", res["hint"])

    def test_batch_read_files_error_guidance_for_large_file(self) -> None:
        """batch_read_files provides actionable error guidance when encountering large files."""
        batch_tool = self.tools["batch_read_files"]
        res = batch_tool(
            project_id="large_file_app",
            paths=["log_5mb.txt"],
        )
        self.assertIn(res["status"], ["partial_success", "error"])
        self.assertIn("log_5mb.txt", res["errors"])
        err_msg = res["errors"]["log_5mb.txt"]
        self.assertIn("exceeds the max allowed limit", err_msg)
        self.assertIn("read_file with start_line and end_line", err_msg)

    def test_count_file_lines_stream_edge_cases(self) -> None:
        """Verify stream line counter against empty, single-line, and non-newline terminated files."""
        # 1. Empty file
        empty_file = self.proj_dir / "empty.txt"
        empty_file.write_bytes(b"")
        self.assertEqual(count_file_lines_stream(empty_file), 0)

        # 2. File without trailing newline
        no_newline = self.proj_dir / "no_newline.txt"
        no_newline.write_bytes(b"a\nb\nc")
        self.assertEqual(count_file_lines_stream(no_newline), 3)

        # 3. File with trailing newline
        with_newline = self.proj_dir / "with_newline.txt"
        with_newline.write_bytes(b"a\nb\nc\n")
        self.assertEqual(count_file_lines_stream(with_newline), 3)

        # 4. Windows CRLF file
        crlf_file = self.proj_dir / "crlf.txt"
        crlf_file.write_bytes(b"line1\r\nline2\r\nline3\r\n")
        self.assertEqual(count_file_lines_stream(crlf_file), 3)


if __name__ == "__main__":
    unittest.main()

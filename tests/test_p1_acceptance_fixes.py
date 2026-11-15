"""Regression tests verifying P1/P2 fixes from acceptance report (2026-11-15).

Verifies:
- D4: create_zip_archive and copy_project_path respect project .gitignore over global fallback
- D5: Subdirectory-only .gitignore functions properly even when project root has no .gitignore
- D6: search_code provides line_truncated and line_full_length for lines exceeding 300 chars
- D7: list_files returns truncated=True, limit_reached=True, and hint when reaching max_entries
- D8: oauth_store.db is strictly protected as a sensitive credential file
- D9: read_project_file line-sliced next_offset is calculated from absolute file start
- D16: list_files returns top-level status="needs_confirmation" on penetration attempts
"""
from __future__ import annotations

import io
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from codexen.config import ConfigManager, ProjectConfig
from codexen.core.archive import create_project_zip
from codexen.core.fs import copy_project_path, read_project_file
from codexen.core.gitignore import clear_ignore_manager_cache, get_project_ignore_manager, set_audit_base_dir
from codexen.core.search import search_project_code
from codexen.core.security import is_sensitive_path
from codexen.tools import register_file_tools
from mcp.server.fastmcp import FastMCP


class TestP1AcceptanceFixes(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_p1_test_"))
        self.workspace_dir = self.temp_dir / "workspace"
        self.workspace_dir.mkdir(parents=True)
        set_audit_base_dir(self.temp_dir)
        clear_ignore_manager_cache()

    def tearDown(self) -> None:
        set_audit_base_dir(None)
        clear_ignore_manager_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_d8_oauth_store_db_is_sensitive(self) -> None:
        """D8: oauth_store.db is strictly blocked across any relative path."""
        self.assertTrue(is_sensitive_path("oauth_store.db"))
        self.assertTrue(is_sensitive_path("data/oauth_store.db"))
        self.assertTrue(is_sensitive_path(".runtime/oauth_store.db"))
        # Normal sqlite databases must remain allowed
        self.assertFalse(is_sensitive_path("app.db"))
        self.assertFalse(is_sensitive_path("tasks.sqlite3"))

    def test_d5_subdirectory_only_gitignore_without_root_gitignore(self) -> None:
        """D5: When root has NO .gitignore, subfolder .gitignore (e.g. packages/fe/.gitignore) still works."""
        proj_dir = self.workspace_dir / "monorepo"
        proj_dir.mkdir(parents=True)
        # Note: NO root .gitignore created!

        sub_fe = proj_dir / "packages" / "fe"
        sub_fe.mkdir(parents=True)
        (sub_fe / ".gitignore").write_text("zzbuild/\n*.log\n", encoding="utf-8")

        (sub_fe / "src").mkdir()
        (sub_fe / "src" / "index.js").write_text("console.log('src');", encoding="utf-8")

        (sub_fe / "zzbuild").mkdir()
        (sub_fe / "zzbuild" / "app.js").write_text("console.log('build');", encoding="utf-8")

        proj = ProjectConfig(name="monorepo", root=proj_dir)
        mgr = get_project_ignore_manager(proj, fallback_ignored_patterns=["node_modules", "out"])

        # Subdirectory .gitignore must ignore zzbuild/ inside packages/fe
        self.assertTrue(mgr.is_ignored("packages/fe/zzbuild", is_dir=True))
        self.assertTrue(mgr.is_ignored("packages/fe/zzbuild/app.js", is_dir=False))
        # Normal source must not be ignored
        self.assertFalse(mgr.is_ignored("packages/fe/src/index.js", is_dir=False))

    def test_d4_zip_and_copy_respects_project_gitignore_over_global_fallback(self) -> None:
        """D4: create_project_zip and copy_project_path do not drop out/ when only in global fallback."""
        proj_dir = self.workspace_dir / "tracked_app"
        proj_dir.mkdir(parents=True)
        # Project .gitignore only ignores target/, NOT out/
        (proj_dir / ".gitignore").write_text("target/\n", encoding="utf-8")

        (proj_dir / "out").mkdir()
        (proj_dir / "out" / "tracked.txt").write_text("important output", encoding="utf-8")
        (proj_dir / "target").mkdir()
        (proj_dir / "target" / "ignored.txt").write_text("junk", encoding="utf-8")

        proj = ProjectConfig(name="tracked_app", root=proj_dir)

        # 1. Test ZIP: Global settings has "out", but project .gitignore does NOT ignore out/
        zip_res = create_project_zip(
            project=proj,
            source_subpath="",
            output_zip_name="archive.zip",
            ignored_patterns=["out", "node_modules"],  # global fallback
        )
        self.assertEqual(zip_res["status"], "success")

        zip_path = proj_dir / zip_res["archive_relative_path"]
        with zipfile.ZipFile(zip_path, "r") as zf:
            namelist = zf.namelist()
            self.assertIn("out/tracked.txt", namelist)
            self.assertNotIn("target/ignored.txt", namelist)

        # 2. Test Copy
        dest_dir = self.workspace_dir / "copied_app"
        dest_dir.mkdir(parents=True)
        tgt_proj = ProjectConfig(name="copied_app", root=dest_dir)

        copy_res = copy_project_path(
            source_project=proj,
            source_relative_path="",
            target_project=tgt_proj,
            target_relative_path="",
            overwrite=True,
            recursive=True,
            ignored_patterns=["out", "node_modules"],  # global fallback
        )
        self.assertEqual(copy_res["status"], "success")
        self.assertTrue((dest_dir / "out" / "tracked.txt").exists())
        self.assertFalse((dest_dir / "target" / "ignored.txt").exists())

    def test_d6_search_code_line_truncation_metadata(self) -> None:
        """D6: Matches on lines longer than 300 chars include line_truncated=True and line_full_length."""
        proj_dir = self.workspace_dir / "long_line_app"
        proj_dir.mkdir(parents=True)

        # 417 characters line with TOKEN near the end
        long_line = ("a" * 350) + "_TOKEN_" + ("b" * 60) + "\n"
        (proj_dir / "long.txt").write_text(long_line, encoding="utf-8")

        proj = ProjectConfig(name="long_line_app", root=proj_dir)
        res = search_project_code(project=proj, query="TOKEN")
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["matches"]), 1)

        m = res["matches"][0]
        self.assertTrue(m["line_truncated"])
        self.assertGreater(m["line_full_length"], 400)
        self.assertEqual(len(m["line_content"]), 300)

    def test_d9_read_file_line_slice_next_offset(self) -> None:
        """D9: Line-sliced pagination next_offset starts at absolute file offset."""
        proj_dir = self.workspace_dir / "slice_app"
        proj_dir.mkdir(parents=True)

        # Create file with 100 lines, each 50 chars
        file_path = proj_dir / "data.txt"
        with open(file_path, "w", encoding="utf-8") as f:
            for i in range(1, 101):
                f.write(f"Line {i:04d}: " + ("x" * 38) + "\n")

        proj = ProjectConfig(name="slice_app", root=proj_dir)

        # Read lines 10 to 100 with max_chars = 300 (roughly 6 lines)
        res = read_project_file(
            project=proj,
            relative_path="data.txt",
            start_line=10,
            end_line=100,
            max_chars=300,
            auto_paginate=True,
        )
        self.assertIsInstance(res, dict)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["returned_start_line"], 10)
        # Line 10 starts at character offset 9 * 50 = 450
        # If returned 6 lines, next_offset should be roughly 450 + 300 = 750
        self.assertGreater(res["next_offset"], 450)

    def test_d7_and_d16_list_files_truncation_and_hitl(self) -> None:
        """D7 & D16: list_files returns top-level needs_confirmation, and truncated=True when max_entries hit."""
        proj_dir = self.workspace_dir / "list_app"
        proj_dir.mkdir(parents=True)
        (proj_dir / ".gitignore").write_text("target/\n", encoding="utf-8")
        (proj_dir / "target").mkdir()
        (proj_dir / "target" / "item.txt").write_text("item", encoding="utf-8")

        for i in range(10):
            (proj_dir / f"file_{i}.txt").write_text(f"content {i}", encoding="utf-8")

        cfg_file = self.temp_dir / "config.yaml"
        cfg_file.write_text(
            f"""
workspace:
  root: "{self.workspace_dir.as_posix()}"
settings:
  max_list_entries: 5
projects:
  list_app:
    root: "{proj_dir.as_posix()}"
""",
            encoding="utf-8",
        )
        cfg_mgr = ConfigManager(config_path=cfg_file, base_dir=self.temp_dir)
        mcp = FastMCP("test")
        tools = register_file_tools(mcp, cfg_mgr)
        list_tool = tools["list_files"]

        # D16: list_files on target/ returns top-level status="needs_confirmation"
        hitl_res = list_tool(project_id="list_app", subpath="target")
        self.assertEqual(hitl_res["status"], "needs_confirmation")
        self.assertEqual(hitl_res["confirmation_type"], "ignored_directory_penetration")
        self.assertEqual(hitl_res["matched_rule_source"], ".gitignore")
        self.assertEqual(hitl_res["matched_pattern"], "target/")

        # D7: list_files reaching max_list_entries=5 has truncated=True and hint
        normal_res = list_tool(project_id="list_app")
        self.assertEqual(normal_res["status"], "success")
        self.assertEqual(len(normal_res["files"]), 5)
        self.assertTrue(normal_res["truncated"])
        self.assertTrue(normal_res["limit_reached"])
        self.assertIn("hint", normal_res)

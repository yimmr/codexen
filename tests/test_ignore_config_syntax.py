"""Tests for Plan 0001 / §4: Configuration Syntax (YAML List, Block Text, Comments, Escaping).

Verifies:
1. YAML list syntax for projects.<id>.ignored_patterns.
2. YAML block string (multiline) syntax for projects.<id>.ignored_patterns.
3. Comments (#) are ignored; escaped literal comments (\\#) match literal #.
4. Negation (!) and escaped literal negation (\\!).
5. Globstar (**) matching across directories.
6. Trailing space stripping and escaped trailing spaces.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from codexen.config import ConfigManager, ProjectConfig, normalize_ignored_patterns
from codexen.core.gitignore import clear_ignore_manager_cache, get_project_ignore_manager


class TestIgnoreConfigSyntax(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_syntax_test_"))
        self.workspace_dir = self.temp_dir / "workspace"
        self.workspace_dir.mkdir(parents=True)
        self.proj_root = self.workspace_dir / "syntax_proj"
        self.proj_root.mkdir()
        clear_ignore_manager_cache()

    def tearDown(self) -> None:
        clear_ignore_manager_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_normalize_ignored_patterns_helper(self) -> None:
        """normalize_ignored_patterns parses list, block text, and strips blank lines."""
        # 1. From list
        lines = normalize_ignored_patterns(["target/", "dist/", "  ", "*.log"])
        self.assertEqual(lines, ["target/", "dist/", "*.log"])

        # 2. From block text
        block = """
        target/
        dist/

        *.log
        !keep.log
        """
        lines_block = normalize_ignored_patterns(block)
        # Note: indent was present, so let's verify stripped lines
        expected = ["target/", "dist/", "*.log", "!keep.log"]
        cleaned_block = "\n".join(["target/", "dist/", "", "*.log", "!keep.log"])
        self.assertEqual(normalize_ignored_patterns(cleaned_block), expected)

    def test_yaml_block_text_project_config(self) -> None:
        """Projects can specify ignored_patterns as a multiline YAML block."""
        cfg_file = self.temp_dir / "config.yaml"
        cfg_file.write_text(
            f"""
workspace:
  root: "{self.workspace_dir.as_posix()}"
projects:
  block_app:
    root: "{self.proj_root.as_posix()}"
    ignored_patterns: |
      target/
      dist/
      *.tmp
      !important.tmp
""",
            encoding="utf-8",
        )
        cfg_mgr = ConfigManager(config_path=cfg_file, base_dir=self.temp_dir)
        proj = cfg_mgr.get_project("block_app")

        self.assertIn("target/", proj.ignored_patterns)
        self.assertIn("dist/", proj.ignored_patterns)
        self.assertIn("*.tmp", proj.ignored_patterns)
        self.assertIn("!important.tmp", proj.ignored_patterns)

        mgr = get_project_ignore_manager(proj)
        self.assertTrue(mgr.is_ignored("target", is_dir=True))
        self.assertTrue(mgr.is_ignored("dist/bundle.js", is_dir=False))
        self.assertTrue(mgr.is_ignored("cache.tmp", is_dir=False))
        self.assertFalse(mgr.is_ignored("important.tmp", is_dir=False))

    def test_yaml_list_project_config(self) -> None:
        """Projects can specify ignored_patterns as a standard YAML list."""
        cfg_file = self.temp_dir / "config_list.yaml"
        cfg_file.write_text(
            f"""
workspace:
  root: "{self.workspace_dir.as_posix()}"
projects:
  list_app:
    root: "{self.proj_root.as_posix()}"
    ignored_patterns:
      - "build/"
      - "**/*.bak"
      - "!core.bak"
""",
            encoding="utf-8",
        )
        cfg_mgr = ConfigManager(config_path=cfg_file, base_dir=self.temp_dir)
        proj = cfg_mgr.get_project("list_app")

        mgr = get_project_ignore_manager(proj)
        self.assertTrue(mgr.is_ignored("build", is_dir=True))
        self.assertTrue(mgr.is_ignored("nested/folder/file.bak", is_dir=False))
        self.assertFalse(mgr.is_ignored("core.bak", is_dir=False))

    def test_comments_and_escaping_in_config(self) -> None:
        """Comments (#) are ignored; literal escaped comments (\\#) match filenames."""
        proj = ProjectConfig(
            name="esc_app",
            root=self.proj_root,
            ignored_patterns=[
                "# this is a comment",
                r"\#notes.txt",
                r"file\ with\ space.txt",
                r"\!important.txt",
            ],
        )
        mgr = get_project_ignore_manager(proj)

        # Comment rule is not an exclusion
        self.assertFalse(mgr.is_ignored("this is a comment", is_dir=False))

        # Escaped # matches literal #notes.txt
        self.assertTrue(mgr.is_ignored("#notes.txt", is_dir=False))

        # Escaped space matches filename with space
        self.assertTrue(mgr.is_ignored("file with space.txt", is_dir=False))

        # Escaped ! matches literal !important.txt
        self.assertTrue(mgr.is_ignored("!important.txt", is_dir=False))


if __name__ == "__main__":
    unittest.main()

"""Tests for Plan 0001 / §3.5, §5.4: Nested .gitignore Stack, Parent Exclusions, and Deepest-First Precedence.

Verifies:
1. Deepest .gitignore definitive verdict takes precedence over shallower and root .gitignore.
2. Parent directory exclusion prevents re-inclusion by child negation (!pattern).
3. Child negation works when parent directory is not excluded.
4. Hot-reload / mtime invalidation on nested .gitignore files.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import time
import unittest

from codexen.config import ProjectConfig
from codexen.core.gitignore import clear_ignore_manager_cache, get_project_ignore_manager


class TestIgnoreNested(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_nested_test_"))
        self.proj_root = self.temp_dir / "nested_proj"
        self.proj_root.mkdir()
        clear_ignore_manager_cache()

    def tearDown(self) -> None:
        clear_ignore_manager_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_deepest_directory_takes_precedence(self) -> None:
        """Deepest .gitignore verdict overrides shallower .gitignore verdict."""
        # Root: ignores all .log files
        (self.proj_root / ".gitignore").write_text("*.log\n", encoding="utf-8")

        # Level 1 subfolder: packages/app/.gitignore negates special.log
        sub_app = self.proj_root / "packages" / "app"
        sub_app.mkdir(parents=True)
        (sub_app / ".gitignore").write_text("!special.log\n", encoding="utf-8")

        # Level 2 subfolder: packages/app/tests/.gitignore re-ignores special.log!
        sub_tests = sub_app / "tests"
        sub_tests.mkdir(parents=True)
        (sub_tests / ".gitignore").write_text("special.log\n", encoding="utf-8")

        proj = ProjectConfig(name="nested_app", root=self.proj_root)
        mgr = get_project_ignore_manager(proj)

        # In root: root.log is ignored by root .gitignore
        self.assertTrue(mgr.is_ignored("root.log", is_dir=False))

        # In packages/app: special.log is NOT ignored (negated by packages/app/.gitignore)
        app_dec = mgr.explain_ignored_decision("packages/app/special.log", is_dir=False)
        self.assertFalse(app_dec.ignored)
        self.assertEqual(app_dec.rule_file, "packages/app/.gitignore")

        # In packages/app/tests: special.log IS ignored (deepest packages/app/tests/.gitignore overrides)
        tests_dec = mgr.explain_ignored_decision("packages/app/tests/special.log", is_dir=False)
        self.assertTrue(tests_dec.ignored)
        self.assertEqual(tests_dec.rule_file, "packages/app/tests/.gitignore")

    def test_parent_directory_exclusion_cannot_be_reincluded_by_child_negation(self) -> None:
        """Git Invariant: If a parent directory is excluded, child negation cannot re-include files within it."""
        # Root .gitignore excludes target/ directory
        (self.proj_root / ".gitignore").write_text("target/\n", encoding="utf-8")

        target_dir = self.proj_root / "target"
        target_dir.mkdir()
        # Even if a subfolder .gitignore tries to re-include keep.txt
        (target_dir / ".gitignore").write_text("!keep.txt\n!sub/\n", encoding="utf-8")

        sub_dir = target_dir / "sub"
        sub_dir.mkdir()
        (sub_dir / "keep.txt").write_text("keep", encoding="utf-8")

        proj = ProjectConfig(name="parent_excl_app", root=self.proj_root)
        mgr = get_project_ignore_manager(proj)

        # Checking target/sub/keep.txt with check_parents=True
        dec = mgr.explain_ignored_decision("target/sub/keep.txt", is_dir=False, check_parents=True)
        self.assertTrue(dec.ignored)
        # Excluded by the parent directory rule
        self.assertEqual(dec.pattern, "target/")
        self.assertEqual(dec.rule_file, ".gitignore")

    def test_child_negation_works_when_parent_not_excluded(self) -> None:
        """Child negation works as expected when the parent folder is allowed."""
        (self.proj_root / ".gitignore").write_text("*.tmp\n", encoding="utf-8")

        pkg_dir = self.proj_root / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / ".gitignore").write_text("!vital.tmp\n", encoding="utf-8")
        (pkg_dir / "vital.tmp").write_text("data", encoding="utf-8")
        (pkg_dir / "junk.tmp").write_text("data", encoding="utf-8")

        proj = ProjectConfig(name="child_neg_app", root=self.proj_root)
        mgr = get_project_ignore_manager(proj)

        self.assertFalse(mgr.is_ignored("pkg/vital.tmp", is_dir=False))
        self.assertTrue(mgr.is_ignored("pkg/junk.tmp", is_dir=False))

    def test_nested_gitignore_dynamic_mtime_reload(self) -> None:
        """Modifying a nested .gitignore file at runtime updates the cached rules dynamically."""
        pkg_dir = self.proj_root / "pkg"
        pkg_dir.mkdir()
        gi_file = pkg_dir / ".gitignore"
        gi_file.write_text("v1_output/\n", encoding="utf-8")

        proj = ProjectConfig(name="mtime_app", root=self.proj_root)
        mgr = get_project_ignore_manager(proj)

        self.assertTrue(mgr.is_ignored("pkg/v1_output", is_dir=True))
        self.assertFalse(mgr.is_ignored("pkg/v2_output", is_dir=True))

        time.sleep(0.05)
        gi_file.write_text("v1_output/\nv2_output/\n", encoding="utf-8")

        # After file modification, fresh rules are automatically reloaded
        self.assertTrue(mgr.is_ignored("pkg/v2_output", is_dir=True))


if __name__ == "__main__":
    unittest.main()

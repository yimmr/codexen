"""Tests for Plan 0001 / D-002: Project Rule Bundle Precedence.

Verifies:
1. .gitignore vetoes project configuration (.gitignore takes precedence).
2. .gitignore explicit negation (!pattern) cannot be overridden by project config.
3. Project config rules apply when .gitignore does not make a definitive verdict.
4. Project config negation (!pattern) applies to paths unadjudicated by .gitignore.
5. Multi-level rule bundle (root .gitignore + sub .gitignore + project config) merges cleanly.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from codexen.config import ProjectConfig
from codexen.core.gitignore import clear_ignore_manager_cache, get_project_ignore_manager


class TestIgnorePrecedenceBundle(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_bundle_test_"))
        self.proj_root = self.temp_dir / "bundle_proj"
        self.proj_root.mkdir()
        clear_ignore_manager_cache()

    def tearDown(self) -> None:
        clear_ignore_manager_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_gitignore_vetoes_project_config_negation(self) -> None:
        """If .gitignore ignores a path, project config negation cannot un-ignore it."""
        (self.proj_root / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
        (self.proj_root / "build").mkdir()
        (self.proj_root / "build" / "app.js").write_text("code", encoding="utf-8")
        (self.proj_root / "test.log").write_text("log", encoding="utf-8")

        # Project config attempts to re-include build/ and test.log via negation
        proj = ProjectConfig(
            name="bundle_app",
            root=self.proj_root,
            ignored_patterns=["!build/", "!test.log"],
        )
        mgr = get_project_ignore_manager(proj)

        # .gitignore must win: build/ and test.log remain ignored
        self.assertTrue(mgr.is_ignored("build", is_dir=True))
        self.assertTrue(mgr.is_ignored("build/app.js", is_dir=False))
        self.assertTrue(mgr.is_ignored("test.log", is_dir=False))

        decision = mgr.explain_ignored_decision("build", is_dir=True)
        self.assertEqual(decision.source, "project_gitignore")
        self.assertEqual(decision.pattern, "build/")

    def test_gitignore_negation_prevents_project_config_ignore(self) -> None:
        """If .gitignore explicitly un-ignores (!pattern), project config cannot re-ignore it."""
        (self.proj_root / ".gitignore").write_text("*.log\n!important.log\n", encoding="utf-8")
        (self.proj_root / "important.log").write_text("data", encoding="utf-8")
        (self.proj_root / "other.log").write_text("data", encoding="utf-8")

        # Project config attempts to ignore all logs
        proj = ProjectConfig(
            name="bundle_app",
            root=self.proj_root,
            ignored_patterns=["important.log"],
        )
        mgr = get_project_ignore_manager(proj)

        # important.log is explicitly negated by .gitignore -> NOT ignored!
        self.assertFalse(mgr.is_ignored("important.log", is_dir=False))
        # other.log is ignored by .gitignore
        self.assertTrue(mgr.is_ignored("other.log", is_dir=False))

        decision = mgr.explain_ignored_decision("important.log", is_dir=False)
        self.assertFalse(decision.ignored)
        self.assertEqual(decision.source, "project_gitignore")
        self.assertEqual(decision.pattern, "!important.log")

    def test_project_config_supplements_unmatched_paths(self) -> None:
        """Project config applies when .gitignore does not match."""
        (self.proj_root / ".gitignore").write_text("target/\n", encoding="utf-8")
        (self.proj_root / "target").mkdir()
        (self.proj_root / "custom_data").mkdir()
        (self.proj_root / "custom_data" / "file.txt").write_text("data", encoding="utf-8")

        proj = ProjectConfig(
            name="bundle_app",
            root=self.proj_root,
            ignored_patterns=["custom_data/"],
        )
        mgr = get_project_ignore_manager(proj)

        # target/ is ignored by .gitignore
        t_decision = mgr.explain_ignored_decision("target", is_dir=True)
        self.assertTrue(t_decision.ignored)
        self.assertEqual(t_decision.source, "project_gitignore")

        # custom_data/ is ignored by project_config
        c_decision = mgr.explain_ignored_decision("custom_data", is_dir=True)
        self.assertTrue(c_decision.ignored)
        self.assertEqual(c_decision.source, "project_config")
        self.assertEqual(c_decision.pattern, "custom_data/")

    def test_project_bundle_full_hierarchy(self) -> None:
        """Root .gitignore, subfolder .gitignore, and project config combine cleanly."""
        (self.proj_root / ".gitignore").write_text("*.tmp\n", encoding="utf-8")

        sub = self.proj_root / "packages" / "core"
        sub.mkdir(parents=True)
        (sub / ".gitignore").write_text("local_cache/\n!keep.tmp\n", encoding="utf-8")

        proj = ProjectConfig(
            name="bundle_app",
            root=self.proj_root,
            ignored_patterns=["reports/"],
        )
        mgr = get_project_ignore_manager(proj)

        # 1. Root .gitignore matches root.tmp
        self.assertTrue(mgr.is_ignored("root.tmp", is_dir=False))

        # 2. Sub .gitignore matches local_cache/ in packages/core
        self.assertTrue(mgr.is_ignored("packages/core/local_cache", is_dir=True))

        # 3. Sub .gitignore negates keep.tmp in packages/core
        self.assertFalse(mgr.is_ignored("packages/core/keep.tmp", is_dir=False))

        # 4. Project config matches reports/
        r_decision = mgr.explain_ignored_decision("reports", is_dir=True)
        self.assertTrue(r_decision.ignored)
        self.assertEqual(r_decision.source, "project_config")


if __name__ == "__main__":
    unittest.main()

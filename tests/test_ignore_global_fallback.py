"""Tests for Plan 0001 / D-003, D-007, D-008: Global Fallback Trigger Conditions & Model C.

Verifies:
1. When root has NO .gitignore and NO project config, global fallback is active.
2. An empty 0-byte root .gitignore acts as an explicit switch to disable global fallback (D-008).
3. Non-empty project config (projects.<id>.ignored_patterns) disables global fallback even with no root .gitignore (D-003).
4. Subdirectory .gitignore works strictly within its subtree and does NOT disable global fallback for the rest of the project (D-007 / Model C).
5. has_project_level_rules fast-path avoids scanning the entire project tree.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from codexen.config import ProjectConfig
from codexen.core.gitignore import (
    BASELINE_FALLBACK_IGNORED,
    clear_ignore_manager_cache,
    get_project_ignore_manager,
)


class TestIgnoreGlobalFallbackModelC(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_fallback_test_"))
        self.proj_root = self.temp_dir / "fallback_proj"
        self.proj_root.mkdir()
        clear_ignore_manager_cache()

    def tearDown(self) -> None:
        clear_ignore_manager_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_global_fallback_active_when_no_project_rules(self) -> None:
        """When project has no root .gitignore and no config, global fallback protects standard paths."""
        proj = ProjectConfig(name="uninit", root=self.proj_root)
        mgr = get_project_ignore_manager(proj, fallback_ignored_patterns=["node_modules", "target", "dist"])

        self.assertFalse(mgr.has_project_root_gitignore())
        self.assertFalse(mgr.has_project_config_rules())
        self.assertFalse(mgr.has_project_level_rules())

        # Standard build dirs are caught by global fallback
        decision = mgr.explain_ignored_decision("node_modules", is_dir=True)
        self.assertTrue(decision.ignored)
        self.assertEqual(decision.source, "global_fallback")

        self.assertTrue(mgr.is_ignored("target", is_dir=True))
        self.assertTrue(mgr.is_ignored("dist/bundle.js", is_dir=False))

        # Normal source is NOT ignored
        self.assertFalse(mgr.is_ignored("src/app.py", is_dir=False))

    def test_empty_root_gitignore_disables_global_fallback(self) -> None:
        """D-008: An empty 0-byte root .gitignore explicitly disables global fallback."""
        (self.proj_root / ".gitignore").write_text("", encoding="utf-8")

        proj = ProjectConfig(name="empty_gi", root=self.proj_root)
        mgr = get_project_ignore_manager(proj, fallback_ignored_patterns=["node_modules", "target", "dist"])

        self.assertTrue(mgr.has_project_root_gitignore())
        self.assertTrue(mgr.has_project_level_rules())

        # Because empty .gitignore disables global fallback, unlisted build dirs are NOT ignored!
        self.assertFalse(mgr.is_ignored("node_modules", is_dir=True))
        self.assertFalse(mgr.is_ignored("target", is_dir=True))
        self.assertFalse(mgr.is_ignored("dist/bundle.js", is_dir=False))

    def test_project_config_disables_global_fallback_without_root_gitignore(self) -> None:
        """D-003: Project config without root .gitignore disables global fallback."""
        # Ensure NO root .gitignore exists
        self.assertFalse((self.proj_root / ".gitignore").exists())

        proj = ProjectConfig(
            name="config_only",
            root=self.proj_root,
            ignored_patterns=["custom_build/"],
        )
        mgr = get_project_ignore_manager(proj, fallback_ignored_patterns=["node_modules", "target", "dist"])

        self.assertFalse(mgr.has_project_root_gitignore())
        self.assertTrue(mgr.has_project_config_rules())
        self.assertTrue(mgr.has_project_level_rules())

        # custom_build/ is ignored by project config
        c_decision = mgr.explain_ignored_decision("custom_build", is_dir=True)
        self.assertTrue(c_decision.ignored)
        self.assertEqual(c_decision.source, "project_config")

        # node_modules and target are NOT ignored because global fallback is closed by project config!
        self.assertFalse(mgr.is_ignored("node_modules", is_dir=True))
        self.assertFalse(mgr.is_ignored("target", is_dir=True))

    def test_subdirectory_only_gitignore_model_c(self) -> None:
        """D-007 / Model C: Subdirectory .gitignore acts on subtree only; global fallback remains active for rest."""
        # Project structure:
        # my-project/
        #   (NO root .gitignore, NO project config)
        #   packages/fe/.gitignore (ignores fe_dist/ and !fe_keep.tmp)
        #   node_modules/ (root dependency)
        sub_fe = self.proj_root / "packages" / "fe"
        sub_fe.mkdir(parents=True)
        (sub_fe / ".gitignore").write_text("fe_dist/\n!fe_keep.tmp\n", encoding="utf-8")

        proj = ProjectConfig(name="monorepo_model_c", root=self.proj_root)
        mgr = get_project_ignore_manager(proj, fallback_ignored_patterns=["node_modules", "*.tmp"])

        # Model C switch: Root lacks .gitignore and project config is empty -> has_project_level_rules is FALSE
        self.assertFalse(mgr.has_project_level_rules())

        # 1. Inside packages/fe: fe_dist is ignored by packages/fe/.gitignore
        fe_dist_decision = mgr.explain_ignored_decision("packages/fe/fe_dist", is_dir=True)
        self.assertTrue(fe_dist_decision.ignored)
        self.assertEqual(fe_dist_decision.source, "project_gitignore")
        self.assertEqual(fe_dist_decision.rule_file, "packages/fe/.gitignore")

        # 2. Inside packages/fe: fe_keep.tmp is un-ignored by packages/fe/.gitignore negation
        # Even though global fallback has *.tmp!
        fe_keep_decision = mgr.explain_ignored_decision("packages/fe/fe_keep.tmp", is_dir=False)
        self.assertFalse(fe_keep_decision.ignored)
        self.assertEqual(fe_keep_decision.source, "project_gitignore")
        self.assertEqual(fe_keep_decision.pattern, "!fe_keep.tmp")

        # 3. Inside packages/fe: other.tmp is not matched by packages/fe/.gitignore, so falls through to global fallback!
        fe_other_decision = mgr.explain_ignored_decision("packages/fe/other.tmp", is_dir=False)
        self.assertTrue(fe_other_decision.ignored)
        self.assertEqual(fe_other_decision.source, "global_fallback")

        # 4. Outside packages/fe: root node_modules is STILL protected by global fallback!
        nm_decision = mgr.explain_ignored_decision("node_modules", is_dir=True)
        self.assertTrue(nm_decision.ignored)
        self.assertEqual(nm_decision.source, "global_fallback")


if __name__ == "__main__":
    unittest.main()

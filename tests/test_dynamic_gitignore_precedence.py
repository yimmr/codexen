"""Comprehensive tests for RFC 0001 Phase 2: Dynamic .gitignore Engine & Precedence Pipeline.

Verifies:
1. Level 1: Project-level .gitignore (highest authority)
2. Project self-determination (unlisted directories like out/ aren't suppressed by global list)
3. Pattern syntax: wildcards, dir-only, nested paths, negations (!)
4. Nested .gitignore in subdirectories
5. Dynamic mtime auto-invalidation & hot-reload
6. Level 2: Project-specific config in projects.<id>.ignored_patterns
7. Level 3: Global fallback when no .gitignore is present
8. Level 4: Baseline system exclusions (.git, credentials)
9. Targeted read_file decoupling
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from codexen.config import ConfigManager, ProjectConfig
from codexen.core.archive import create_project_zip
from codexen.core.fs import list_project_files, read_project_file
from codexen.core.gitignore import (
    clear_ignore_manager_cache,
    get_project_ignore_manager,
)
from codexen.core.search import search_project_code


class TestDynamicGitIgnorePrecedence(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_gitignore_test_"))
        clear_ignore_manager_cache()

    def tearDown(self) -> None:
        clear_ignore_manager_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_level_1_gitignore_rules_and_project_self_determination(self) -> None:
        """Verify project .gitignore takes precedence, and unlisted folders are not blocked by global fallback."""
        proj_root = self.temp_dir / "app"
        proj_root.mkdir()

        # Seed .gitignore
        (proj_root / ".gitignore").write_text(
            "\n".join([
                "target/",
                "desktop/src-tauri/gen/",
                "*.log",
                "!special.log",
                "custom_cache",
            ]),
            encoding="utf-8",
        )

        # Seed file structure
        (proj_root / "src").mkdir()
        (proj_root / "src" / "main.py").write_text("def run(): return 'online'", encoding="utf-8")

        # target/ is ignored
        (proj_root / "target").mkdir()
        (proj_root / "target" / "compiled.py").write_text("def run(): return 'target'", encoding="utf-8")

        # desktop/src-tauri/gen is ignored
        (proj_root / "desktop" / "src-tauri" / "gen").mkdir(parents=True)
        (proj_root / "desktop" / "src-tauri" / "gen" / "generated.py").write_text("def run(): return 'gen'", encoding="utf-8")

        # logs
        (proj_root / "debug.log").write_text("def run(): return 'debug log'", encoding="utf-8")
        (proj_root / "special.log").write_text("def run(): return 'special log'", encoding="utf-8")

        # out/ is in global fallback list, BUT NOT in this project's .gitignore!
        # Project self-determination: out/ MUST be searchable and listable!
        (proj_root / "out").mkdir()
        (proj_root / "out" / "release.py").write_text("def run(): return 'out release'", encoding="utf-8")

        proj = ProjectConfig(name="app", root=proj_root)

        # 1. Search code
        res = search_project_code(proj, query="def run")
        self.assertEqual(res["status"], "success")

        matched_files = {m["file"] for m in res["matches"]}
        self.assertIn("src/main.py", matched_files)
        self.assertIn("out/release.py", matched_files, "out/ must NOT be blocked when project has .gitignore")
        self.assertIn("special.log", matched_files, "!special.log must be re-included by negation")

        self.assertNotIn("target/compiled.py", matched_files, "target/ must be pruned")
        self.assertNotIn("desktop/src-tauri/gen/generated.py", matched_files, "nested gen/ must be pruned")
        self.assertNotIn("debug.log", matched_files, "debug.log must be ignored by *.log")

        # 2. List files
        files = list_project_files(proj, recursive=True)
        rel_paths = [f["relative_path"] for f in files]

        self.assertTrue(any("src/main.py" in p for p in rel_paths))
        self.assertTrue(any("out/release.py" in p for p in rel_paths))
        self.assertTrue(any("special.log" in p for p in rel_paths))
        self.assertFalse(any("target" in p for p in rel_paths))
        self.assertFalse(any("desktop/src-tauri/gen" in p for p in rel_paths))
        self.assertFalse(any("debug.log" in p for p in rel_paths))

    def test_level_2_project_specific_ignored_patterns(self) -> None:
        """Verify projects.<id>.ignored_patterns applies in addition to .gitignore."""
        proj_root = self.temp_dir / "proj_level2"
        proj_root.mkdir()

        (proj_root / ".gitignore").write_text("target/\n", encoding="utf-8")
        (proj_root / "src").mkdir()
        (proj_root / "src" / "index.ts").write_text("const msg = 'hello';", encoding="utf-8")
        (proj_root / "local_mock").mkdir()
        (proj_root / "local_mock" / "mock.ts").write_text("const msg = 'mock';", encoding="utf-8")

        # Configure Level 2 project-specific ignore
        proj = ProjectConfig(
            name="proj_level2",
            root=proj_root,
            ignored_patterns=["local_mock"],
        )

        res = search_project_code(proj, query="const msg")
        matched_files = {m["file"] for m in res["matches"]}
        self.assertIn("src/index.ts", matched_files)
        self.assertNotIn("local_mock/mock.ts", matched_files)

    def test_level_3_global_fallback_when_no_gitignore(self) -> None:
        """Verify global fallback safely protects build and cache folders when project lacks .gitignore."""
        proj_root = self.temp_dir / "uninitialized_proj"
        proj_root.mkdir()

        (proj_root / "src").mkdir()
        (proj_root / "src" / "app.py").write_text("VAL = 'active'", encoding="utf-8")
        (proj_root / "node_modules").mkdir()
        (proj_root / "node_modules" / "dep.js").write_text("VAL = 'dep'", encoding="utf-8")
        (proj_root / "target").mkdir()
        (proj_root / "target" / "app.exe").write_text("VAL = 'target'", encoding="utf-8")
        (proj_root / "dist").mkdir()
        (proj_root / "dist" / "bundle.js").write_text("VAL = 'bundle'", encoding="utf-8")

        proj = ProjectConfig(name="uninit", root=proj_root)
        mgr = get_project_ignore_manager(proj)
        self.assertFalse(mgr.has_project_gitignore())

        res = search_project_code(proj, query="VAL =")
        matched_files = {m["file"] for m in res["matches"]}
        self.assertIn("src/app.py", matched_files)
        self.assertNotIn("node_modules/dep.js", matched_files)
        self.assertNotIn("dist/bundle.js", matched_files)

    def test_level_4_baseline_system_security(self) -> None:
        """Verify Level 4 security invariants (.git, .env) are blocked regardless of .gitignore."""
        proj_root = self.temp_dir / "sec_proj"
        proj_root.mkdir()

        # Malicious or misconfigured .gitignore trying to re-include .git or .env
        (proj_root / ".gitignore").write_text("!.git\n!.env\n", encoding="utf-8")

        git_dir = proj_root / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("SECRET_GIT_KEY = 12345", encoding="utf-8")
        (proj_root / ".env").write_text("SECRET_ENV_KEY = 67890", encoding="utf-8")
        (proj_root / "src").mkdir()
        (proj_root / "src" / "ok.py").write_text("SECRET_KEY = 999", encoding="utf-8")

        proj = ProjectConfig(name="sec_proj", root=proj_root)

        # Search code must never match .git or .env
        res = search_project_code(proj, query="SECRET_")
        matched_files = {m["file"] for m in res["matches"]}
        self.assertIn("src/ok.py", matched_files)
        self.assertNotIn(".git/config", matched_files)
        self.assertNotIn(".env", matched_files)

        # File listing must never return .git or .env
        files = list_project_files(proj, recursive=True)
        rel_paths = [f["relative_path"] for f in files]
        self.assertFalse(any(p == ".git" or p.startswith(".git/") or "/.git/" in p for p in rel_paths))
        self.assertFalse(any(p == ".env" or p.startswith(".env.") or "/.env" in p for p in rel_paths))

    def test_nested_gitignore_in_subdirectories(self) -> None:
        """Verify subfolder .gitignore rules override/augment root rules for subtrees."""
        proj_root = self.temp_dir / "monorepo"
        proj_root.mkdir()

        (proj_root / ".gitignore").write_text("*.tmp\n", encoding="utf-8")

        frontend = proj_root / "packages" / "frontend"
        frontend.mkdir(parents=True)
        (frontend / ".gitignore").write_text("build/\n!keep.tmp\n", encoding="utf-8")

        (proj_root / "root.tmp").write_text("root tmp", encoding="utf-8")
        (frontend / "keep.tmp").write_text("keep tmp", encoding="utf-8")
        (frontend / "build").mkdir()
        (frontend / "build" / "app.js").write_text("build js", encoding="utf-8")

        proj = ProjectConfig(name="monorepo", root=proj_root)
        mgr = get_project_ignore_manager(proj)

        # Root .tmp is ignored
        self.assertTrue(mgr.is_ignored("root.tmp", is_dir=False))
        # frontend build/ is ignored by frontend .gitignore
        self.assertTrue(mgr.is_ignored("packages/frontend/build", is_dir=True))
        # frontend keep.tmp is re-included by frontend .gitignore negation
        self.assertFalse(mgr.is_ignored("packages/frontend/keep.tmp", is_dir=False))

    def test_dynamic_mtime_invalidation_and_hot_reload(self) -> None:
        """Verify editing .gitignore at runtime takes effect without server restart."""
        proj_root = self.temp_dir / "hot_reload_proj"
        proj_root.mkdir()

        gi_file = proj_root / ".gitignore"
        gi_file.write_text("cache_v1/\n", encoding="utf-8")

        proj = ProjectConfig(name="hot_reload_proj", root=proj_root)
        mgr = get_project_ignore_manager(proj)

        self.assertTrue(mgr.is_ignored("cache_v1", is_dir=True))
        self.assertFalse(mgr.is_ignored("cache_v2", is_dir=True))

        # Simulate user updating .gitignore
        time.sleep(0.05)
        gi_file.write_text("cache_v1/\ncache_v2/\n", encoding="utf-8")

        # Next check automatically picks up new rules
        self.assertTrue(mgr.is_ignored("cache_v2", is_dir=True))

    def test_targeted_read_file_decoupling(self) -> None:
        """Verify read_project_file allows targeted read of ignored files (Principle 2)."""
        proj_root = self.temp_dir / "read_proj"
        proj_root.mkdir()

        (proj_root / ".gitignore").write_text("dist/\n", encoding="utf-8")
        (proj_root / "dist").mkdir()
        (proj_root / "dist" / "build.log").write_text("Build succeeded at step 42", encoding="utf-8")

        proj = ProjectConfig(name="read_proj", root=proj_root)

        # read_file should allow targeted reading of dist/build.log
        content = read_project_file(proj, "dist/build.log")
        self.assertIn("Build succeeded at step 42", content)

    def test_archive_zip_respects_gitignore(self) -> None:
        """Verify create_project_zip omits files excluded by .gitignore."""
        proj_root = self.temp_dir / "zip_proj"
        proj_root.mkdir()

        (proj_root / ".gitignore").write_text("node_modules/\n*.dump\n", encoding="utf-8")
        (proj_root / "src").mkdir()
        (proj_root / "src" / "index.js").write_text("console.log(1)", encoding="utf-8")
        (proj_root / "node_modules").mkdir()
        (proj_root / "node_modules" / "dep.js").write_text("dep", encoding="utf-8")
        (proj_root / "core.dump").write_text("dump data", encoding="utf-8")

        proj = ProjectConfig(name="zip_proj", root=proj_root)
        zip_res = create_project_zip(proj, output_zip_name="archive.zip")

        self.assertEqual(zip_res["status"], "success")
        self.assertEqual(zip_res["files_packed"], 2)  # .gitignore + src/index.js


if __name__ == "__main__":
    unittest.main()

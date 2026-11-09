"""Unit tests for unregister_project in ConfigManager."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from app.config import ConfigManager
from app.exceptions import ConfigurationError


class TestConfigUnregisterProject(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_unreg_proj_"))
        self.workspace_dir = self.temp_dir / "works"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.temp_dir / "config.yaml"
        self.config_path.write_text("""# Header comment
workspace:
  root: "{}"

# Projects comment
projects:
  alpha:
    root: "{}"
    description: "Alpha project"
    read_only: false
  beta:
    root: "{}"
    description: "Beta project"
    read_only: false

# Footer comment
permissions:
  allow_file_read: true
""".format(
            str(self.workspace_dir).replace("\\", "/"),
            str(self.workspace_dir / "alpha").replace("\\", "/"),
            str(self.workspace_dir / "beta").replace("\\", "/"),
        ), encoding="utf-8")
        (self.workspace_dir / "alpha").mkdir()
        (self.workspace_dir / "beta").mkdir()

        self.cfg_mgr = ConfigManager(config_path=self.config_path, base_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unregister_project_removes_from_memory_and_persists(self):
        self.assertIn("alpha", self.cfg_mgr.projects)
        self.assertIn("beta", self.cfg_mgr.projects)

        removed = self.cfg_mgr.unregister_project("alpha")
        self.assertEqual(removed.name, "alpha")
        self.assertNotIn("alpha", self.cfg_mgr.projects)
        self.assertIn("beta", self.cfg_mgr.projects)

        # Ensure physical directory is strictly NOT deleted
        self.assertTrue((self.workspace_dir / "alpha").exists())

        # Verify persisted change in config.yaml
        cfg_reloaded = ConfigManager(config_path=self.config_path, base_dir=self.temp_dir)
        self.assertNotIn("alpha", cfg_reloaded.projects)
        self.assertIn("beta", cfg_reloaded.projects)

        # Verify comments were preserved in config.yaml
        content = self.config_path.read_text(encoding="utf-8")
        self.assertIn("# Header comment", content)
        self.assertIn("# Projects comment", content)
        self.assertIn("# Footer comment", content)
        self.assertIn("permissions:", content)

    def test_unregister_nonexistent_project_raises_error(self):
        with self.assertRaises(ConfigurationError) as ctx:
            self.cfg_mgr.unregister_project("nonexistent_project")
        self.assertIn("not configured", str(ctx.exception))

    def test_unregister_last_project_persists(self):
        self.cfg_mgr.unregister_project("alpha")
        self.cfg_mgr.unregister_project("beta")
        self.assertEqual(len(self.cfg_mgr.projects), 0)

        cfg_reloaded = ConfigManager(config_path=self.config_path, base_dir=self.temp_dir)
        self.assertEqual(len(cfg_reloaded.projects), 0)

        content = self.config_path.read_text(encoding="utf-8")
        self.assertIn("# Footer comment", content)
        self.assertIn("permissions:", content)

    def test_unregister_without_persist(self):
        removed = self.cfg_mgr.unregister_project("alpha", persist=False)
        self.assertEqual(removed.name, "alpha")
        self.assertNotIn("alpha", self.cfg_mgr.projects)

        # Config file should still have alpha
        cfg_reloaded = ConfigManager(config_path=self.config_path, base_dir=self.temp_dir)
        self.assertIn("alpha", cfg_reloaded.projects)


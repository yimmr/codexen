"""Unit test for register_existing_project in ConfigManager."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from app.config import ConfigManager
from app.exceptions import ConfigurationError


class TestConfigAddProject(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_add_proj_"))
        self.workspace_dir = self.temp_dir / "works"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.temp_dir / "config.yaml"
        self.config_path.write_text("""
workspace:
  root: "{}"
projects:
  alpha:
    root: "{}"
    description: "Alpha"
""".format(
            str(self.workspace_dir).replace("\\", "/"),
            str(self.workspace_dir / "alpha").replace("\\", "/"),
        ), encoding="utf-8")
        (self.workspace_dir / "alpha").mkdir()

        self.cfg_mgr = ConfigManager(config_path=self.config_path, base_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_register_new_folder(self):
        new_folder = self.temp_dir / "beta_project"
        new_folder.mkdir()

        proj = self.cfg_mgr.register_existing_project(new_folder)
        self.assertEqual(proj.name, "beta_project")
        self.assertEqual(proj.root, new_folder.resolve())
        self.assertIn("beta_project", self.cfg_mgr.projects)

        # Verify persisted to YAML
        cfg_reloaded = ConfigManager(config_path=self.config_path, base_dir=self.temp_dir)
        self.assertIn("beta_project", cfg_reloaded.projects)

    def test_duplicate_name_rejected(self):
        dup_folder = self.temp_dir / "other_dir" / "alpha"
        dup_folder.mkdir(parents=True)

        with self.assertRaises(ValueError) as ctx:
            self.cfg_mgr.register_existing_project(dup_folder)
        self.assertIn("already registered", str(ctx.exception))

    def test_duplicate_path_rejected(self):
        # Register same folder with different name
        existing_folder = self.workspace_dir / "alpha"
        with self.assertRaises(ValueError) as ctx:
            self.cfg_mgr.register_existing_project(existing_folder, name="custom_alias")
        self.assertIn("already registered under project name", str(ctx.exception))

    def test_engine_dir_overlap_rejected(self):
        codexen_root = Path(__file__).resolve().parents[1].resolve()
        with self.assertRaises(ConfigurationError):
            self.cfg_mgr.register_existing_project(codexen_root)


if __name__ == "__main__":
    unittest.main()

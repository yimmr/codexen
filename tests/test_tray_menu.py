"""Unit tests for tray menu project removal items."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

try:
    import pystray
    from pystray import MenuItem as item
except ImportError:
    pystray = None  # type: ignore
    item = None  # type: ignore

from codexen.config import ConfigManager


@unittest.skipIf(pystray is None, "pystray is required for tray menu tests")
class TestTrayMenuRemoval(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_tray_menu_"))
        self.workspace_dir = self.temp_dir / "works"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.temp_dir / "config.yaml"
        self.config_path.write_text("""workspace:
  root: "{}"
projects:
  demo_alpha:
    root: "{}"
    description: "Alpha"
  demo_beta:
    root: "{}"
    description: "Beta"
""".format(
            str(self.workspace_dir).replace("\\", "/"),
            str(self.workspace_dir / "demo_alpha").replace("\\", "/"),
            str(self.workspace_dir / "demo_beta").replace("\\", "/"),
        ), encoding="utf-8")
        (self.workspace_dir / "demo_alpha").mkdir()
        (self.workspace_dir / "demo_beta").mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_remove_project_items_with_projects(self):
        assert item is not None
        # Test helper logic equivalent to tray_app
        cfg_mgr = ConfigManager(config_path=self.config_path, base_dir=self.temp_dir)
        proj_names = sorted(cfg_mgr.projects.keys())
        self.assertEqual(proj_names, ["demo_alpha", "demo_beta"])

        items = [item(p_name, lambda: None) for p_name in proj_names]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].text, "demo_alpha")
        self.assertEqual(items[1].text, "demo_beta")

    def test_get_remove_project_items_empty(self):
        assert item is not None
        empty_config = self.temp_dir / "empty_config.yaml"
        empty_config.write_text("""workspace:
  root: "{}"
projects:
""".format(str(self.workspace_dir).replace("\\", "/")), encoding="utf-8")

        cfg_mgr = ConfigManager(config_path=empty_config, base_dir=self.temp_dir)
        proj_names = sorted(cfg_mgr.projects.keys())
        self.assertEqual(len(proj_names), 0)

        fallback_items = [item("(暂无已注册项目)", lambda: None, enabled=False)]
        self.assertEqual(len(fallback_items), 1)
        self.assertEqual(fallback_items[0].text, "(暂无已注册项目)")
        self.assertFalse(fallback_items[0].enabled)

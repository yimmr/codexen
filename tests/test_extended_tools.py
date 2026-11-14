"""Unit and integration tests for extended MCP tools: copy, zip, commands, search, project creation."""
from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from codexen.config import ConfigManager, ProjectConfig
from codexen.core import (
    copy_project_path,
    create_project_zip,
    extract_project_zip,
    run_project_command,
    search_project_code,
    truncate_command_output,
)
from codexen.exceptions import (
    CommandSecurityError,
    PathSecurityError,
    ZipSlipSecurityError,
)


class TestExtendedTools(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_dir = Path(self.temp_dir) / "works"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Setup two temporary project roots
        self.proj1_root = self.workspace_dir / "proj_alpha"
        self.proj1_root.mkdir(parents=True, exist_ok=True)
        (self.proj1_root / "src").mkdir()
        (self.proj1_root / "src" / "main.py").write_text("def hello():\n    print('Hello World')\n", encoding="utf-8")
        (self.proj1_root / "README.md").write_text("# Alpha Project\nDocumentation here.\n", encoding="utf-8")

        self.proj2_root = self.workspace_dir / "proj_beta"
        self.proj2_root.mkdir(parents=True, exist_ok=True)

        self.proj1 = ProjectConfig(name="proj_alpha", root=self.proj1_root)
        self.proj2 = ProjectConfig(name="proj_beta", root=self.proj2_root)

        # Setup config file
        self.config_file = Path(self.temp_dir) / "settings.yaml"
        self.config_file.write_text(f"""
workspace:
  root: "{str(self.workspace_dir).replace('\\', '/')}"
  allow_create_project: true
projects:
  proj_alpha:
    root: "{str(self.proj1_root).replace('\\', '/')}"
    description: "Alpha"
    read_only: false
  proj_beta:
    root: "{str(self.proj2_root).replace('\\', '/')}"
    description: "Beta"
    read_only: false
mcp:
  permissions:
    allow_command_execution: true
    command_timeout_seconds: 5
    max_command_output_chars: 1000
""", encoding="utf-8")

        self.config_mgr = ConfigManager(config_path=self.config_file, base_dir=Path(self.temp_dir))
        self.settings = self.config_mgr.settings

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # --------------------------------------------------------------------------
    # 1. Project Creation
    # --------------------------------------------------------------------------
    def test_create_project(self) -> None:
        new_proj = self.config_mgr.create_and_register_project(
            project_id="proj_gamma",
            description="Gamma project",
            persist=True,
        )
        self.assertTrue(new_proj.root.exists())
        self.assertIn("proj_gamma", self.config_mgr.projects)
        self.assertEqual(new_proj.name, "proj_gamma")

        # Test invalid name
        with self.assertRaises(ValueError):
            self.config_mgr.create_and_register_project("invalid/name/with/slashes")

    # --------------------------------------------------------------------------
    # 2. File and Directory Copying (Intra & Cross-Project)
    # --------------------------------------------------------------------------
    def test_copy_single_file_intra_project(self) -> None:
        res = copy_project_path(
            source_project=self.proj1,
            source_relative_path="README.md",
            target_project=self.proj1,
            target_relative_path="README_COPY.md",
        )
        self.assertEqual(res["status"], "success")
        self.assertTrue((self.proj1_root / "README_COPY.md").exists())
        self.assertIn("Alpha Project", (self.proj1_root / "README_COPY.md").read_text(encoding="utf-8"))

    def test_copy_cross_project(self) -> None:
        res = copy_project_path(
            source_project=self.proj1,
            source_relative_path="src",
            target_project=self.proj2,
            target_relative_path="imported_src",
            recursive=True,
        )
        self.assertEqual(res["status"], "success")
        self.assertTrue((self.proj2_root / "imported_src" / "main.py").exists())

    def test_copy_boundary_violation(self) -> None:
        with self.assertRaises(PathSecurityError):
            copy_project_path(
                source_project=self.proj1,
                source_relative_path="../outside.txt",
                target_project=self.proj2,
                target_relative_path="file.txt",
            )

    # --------------------------------------------------------------------------
    # 3. ZIP Archive and Zip-Slip Defense
    # --------------------------------------------------------------------------
    def test_zip_archive_and_extract(self) -> None:
        # Create archive
        pack_res = create_project_zip(
            project=self.proj1,
            source_subpath="src",
            output_zip_name="src_backup.zip",
        )
        self.assertEqual(pack_res["status"], "success")
        zip_path = self.proj1_root / "src_backup.zip"
        self.assertTrue(zip_path.exists())

        # Extract to subfolder
        ext_res = extract_project_zip(
            project=self.proj1,
            zip_relative_path="src_backup.zip",
            target_subpath="extracted_src",
        )
        self.assertEqual(ext_res["status"], "success")
        self.assertTrue((self.proj1_root / "extracted_src" / "main.py").exists())

    def test_zip_slip_exploit_intercepted(self) -> None:
        # Handcraft a malicious zip containing traversal '../evil.txt'
        malicious_zip = self.proj1_root / "malicious.zip"
        with zipfile.ZipFile(malicious_zip, "w") as zf:
            zf.writestr("../../evil_outside.txt", "Malicious content")

        with self.assertRaises(ZipSlipSecurityError):
            extract_project_zip(
                project=self.proj1,
                zip_relative_path="malicious.zip",
                target_subpath="safe_target",
            )

        # Ensure no file escaped to workspace or temp
        self.assertFalse((self.temp_dir / Path("evil_outside.txt")).exists())

    # --------------------------------------------------------------------------
    # 4. Command Execution and Modern Output Formatting
    # --------------------------------------------------------------------------
    def test_run_command_allowed(self) -> None:
        res = run_project_command(
            project=self.proj1,
            command='python -c "print(\'Agent Hello World\')"',
            settings=self.settings,
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("Agent Hello World", res["stdout"])
        self.assertFalse(res["timed_out"])

    def test_run_command_blacklist_blocked(self) -> None:
        with self.assertRaises(CommandSecurityError):
            run_project_command(
                project=self.proj1,
                command="format C: /fs:NTFS",
                settings=self.settings,
            )

    def test_run_command_needs_confirmation(self) -> None:
        # rmdir is in command_require_confirmation
        res = run_project_command(
            project=self.proj1,
            command="rmdir /s /q temp_folder",
            settings=self.settings,
            confirmed=False,
        )
        self.assertEqual(res["status"], "needs_confirmation")
        self.assertIn("confirmation", res["message"].lower())

        # When caller passes confirmed=True, it proceeds to execute
        res_confirmed = run_project_command(
            project=self.proj1,
            command="rmdir /s /q non_existent_folder_xyz",
            settings=self.settings,
            confirmed=True,
        )
        # Not blocked by security check, though cmd may return error for non-existent path
        self.assertNotEqual(res_confirmed["status"], "needs_confirmation")

    def test_output_smart_truncation(self) -> None:
        # Generate 10,000 characters of logs
        large_text = "\n".join(f"Line {i}: startup log sample text" for i in range(500))
        truncated, is_trunc, total_lines, omitted_lines = truncate_command_output(large_text, max_chars=500)
        self.assertTrue(is_trunc)
        self.assertIn("[Output truncated:", truncated)
        self.assertIn("Line 0:", truncated)
        self.assertIn("Line 499:", truncated)

    # --------------------------------------------------------------------------
    # 5. Fast Code Search
    # --------------------------------------------------------------------------
    def test_search_code(self) -> None:
        res = search_project_code(
            project=self.proj1,
            query="hello",
            is_regex=False,
            case_sensitive=False,
        )
        self.assertEqual(res["status"], "success")
        self.assertGreaterEqual(res["total_matches"], 1)
        self.assertEqual(res["matches"][0]["file"], "src/main.py")
        self.assertIn("def hello", res["matches"][0]["line_content"])

    def test_sensitive_files_excluded_from_zip_and_search(self) -> None:
        # Seed sensitive .env file
        (self.proj1_root / ".env").write_text("SUPER_SECRET_KEY=12345\n", encoding="utf-8")

        # 1. search_code must not match in .env
        search_res = search_project_code(project=self.proj1, query="SUPER_SECRET_KEY")
        self.assertEqual(search_res["total_matches"], 0)

        # 2. create_project_zip must not pack .env
        zip_res = create_project_zip(self.proj1, output_zip_name="safe_pack.zip")
        self.assertEqual(zip_res["status"], "success")
        with zipfile.ZipFile(self.proj1_root / "safe_pack.zip", "r") as zf:
            names = zf.namelist()
            self.assertNotIn(".env", names)

        # 3. copy_project_path must not copy .env
        copy_res = copy_project_path(
            source_project=self.proj1,
            source_relative_path=".",
            target_project=self.proj2,
            target_relative_path="",
            recursive=True,
        )
        self.assertEqual(copy_res["status"], "success")
        self.assertFalse((self.proj2_root / ".env").exists())


if __name__ == "__main__":
    unittest.main()

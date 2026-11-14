"""Unit tests for core file operations and security boundaries."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexen.config import ProjectConfig
from codexen.core import (
    create_project_directory,
    delete_project_file,
    edit_project_file_snippet,
    get_project_file_info,
    list_project_files,
    read_project_file,
    safe_resolve_path,
    write_project_file,
)
from codexen.exceptions import (
    FileSizeLimitError,
    PathSecurityError,
    ReadOnlyProjectError,
)


class CoreSecurityAndFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name).resolve()

        self.writable_project = ProjectConfig(
            name="test_proj",
            root=self.root_path,
            description="Test project",
            read_only=False,
        )

        self.readonly_project = ProjectConfig(
            name="readonly_proj",
            root=self.root_path,
            description="Readonly project",
            read_only=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_safe_resolve_path_valid(self):
        resolved = safe_resolve_path(self.root_path, "sub/dir/file.txt")
        self.assertEqual(resolved, (self.root_path / "sub" / "dir" / "file.txt").resolve())

    def test_safe_resolve_path_traversal_rejected(self):
        dangerous_paths = [
            "../outside.txt",
            "dir/../../escaped.txt",
            "/absolute_escape.txt",
            "C:/Windows/win.ini",
            "D:\\secret.txt",
            "\\\\network\\share",
            "CON.txt",
            "aux",
            "com1/foo.txt",
        ]
        for p in dangerous_paths:
            with self.subTest(dangerous_path=p):
                with self.assertRaises(PathSecurityError):
                    safe_resolve_path(self.root_path, p)

    def test_write_and_read_file(self):
        res = write_project_file(
            self.writable_project,
            "docs/readme.md",
            "# Hello World\nTesting MCP file server.",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["relative_path"], "docs/readme.md")

        content = read_project_file(self.writable_project, "docs/readme.md")
        self.assertIn("Hello World", content)

    def test_overwrite_behavior(self):
        write_project_file(self.writable_project, "test.txt", "v1")
        # Overwrite=False should raise FileExistsError
        with self.assertRaises(FileExistsError):
            write_project_file(self.writable_project, "test.txt", "v2", overwrite=False)

        # Overwrite=True should succeed
        write_project_file(self.writable_project, "test.txt", "v2", overwrite=True)
        content = read_project_file(self.writable_project, "test.txt")
        self.assertEqual(content, "v2")

    def test_readonly_project_protection(self):
        # Readonly project rejects write
        with self.assertRaises(ReadOnlyProjectError):
            write_project_file(self.readonly_project, "forbidden.txt", "data")

        # Readonly project rejects edit
        with self.assertRaises(ReadOnlyProjectError):
            edit_project_file_snippet(self.readonly_project, "any.txt", "a", "b")

        # Readonly project rejects delete
        with self.assertRaises(ReadOnlyProjectError):
            delete_project_file(self.readonly_project, "any.txt")

    def test_edit_file_snippet(self):
        write_project_file(
            self.writable_project,
            "code.py",
            "def foo():\n    return 'old_value'\n",
        )
        edit_project_file_snippet(
            self.writable_project,
            "code.py",
            search_text="'old_value'",
            replace_text="'new_value'",
        )
        content = read_project_file(self.writable_project, "code.py")
        self.assertIn("'new_value'", content)
        self.assertNotIn("'old_value'", content)

    def test_list_files_and_ignore_patterns(self):
        write_project_file(self.writable_project, "src/main.py", "print(1)")
        git_dir = self.root_path / ".git"
        git_dir.mkdir(parents=True, exist_ok=True)
        (git_dir / "config").write_text("secret", encoding="utf-8")
        nm_dir = self.root_path / "node_modules" / "pkg"
        nm_dir.mkdir(parents=True, exist_ok=True)
        (nm_dir / "index.js").write_text("1", encoding="utf-8")

        files = list_project_files(
            self.writable_project,
            recursive=True,
            ignored_patterns=[".git", "node_modules"],
        )
        paths = [f["relative_path"] for f in files]
        self.assertTrue(any("src/main.py" in p for p in paths))
        self.assertFalse(any(".git" in p for p in paths))
        self.assertFalse(any("node_modules" in p for p in paths))

    def test_sensitive_file_write_blocked(self):
        with self.assertRaises(PathSecurityError):
            write_project_file(self.writable_project, ".git/config", "hacked")
        with self.assertRaises(PathSecurityError):
            write_project_file(self.writable_project, ".env", "SECRET=123")

    def test_get_file_info(self):
        write_project_file(self.writable_project, "info.txt", "line 1\nline 2\nline 3")
        info = get_project_file_info(self.writable_project, "info.txt")
        self.assertEqual(info["is_file"], True)
        self.assertEqual(info["is_directory"], False)
        self.assertEqual(info["line_count"], 3)

    def test_create_directory(self):
        res = create_project_directory(self.writable_project, "new_folder/sub")
        self.assertEqual(res["status"], "success")
        self.assertTrue((self.root_path / "new_folder" / "sub").is_dir())

    def test_file_size_limit_error(self):
        write_project_file(self.writable_project, "big.txt", "a" * 100)
        with self.assertRaises(FileSizeLimitError):
            read_project_file(self.writable_project, "big.txt", max_chars=10)

    def test_delete_file(self):
        write_project_file(self.writable_project, "to_delete.txt", "bye")
        del_res = delete_project_file(self.writable_project, "to_delete.txt")
        self.assertEqual(del_res["status"], "success")
        self.assertEqual(del_res["is_directory"], False)
        with self.assertRaises(FileNotFoundError):
            read_project_file(self.writable_project, "to_delete.txt")

    def test_append_write_and_line_slicing(self):
        # 1. Test append write
        write_project_file(self.writable_project, "multi.txt", "line1\n", overwrite=True)
        write_project_file(self.writable_project, "multi.txt", "line2\n", append=True)
        write_project_file(self.writable_project, "multi.txt", "line3\n", append=True)
        full_content = read_project_file(self.writable_project, "multi.txt")
        self.assertEqual(full_content, "line1\nline2\nline3\n")

        # 2. Test line-based slicing
        lines_slice = read_project_file(self.writable_project, "multi.txt", start_line=2, end_line=2)
        self.assertEqual(lines_slice, "line2\n")

        lines_from_2 = read_project_file(self.writable_project, "multi.txt", start_line=2)
        self.assertEqual(lines_from_2, "line2\nline3\n")

    def test_delete_directory_is_directory_flag(self):
        create_project_directory(self.writable_project, "del_dir")
        del_res = delete_project_file(self.writable_project, "del_dir", recursive=True)
        self.assertEqual(del_res["status"], "success")
        self.assertEqual(del_res["is_directory"], True)

    def test_get_file_info_status(self):
        write_project_file(self.writable_project, "info_test.txt", "content")
        info = get_project_file_info(self.writable_project, "info_test.txt")
        self.assertEqual(info["status"], "success")


if __name__ == "__main__":
    unittest.main()

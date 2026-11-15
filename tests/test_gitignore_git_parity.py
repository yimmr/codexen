"""Git Parity differential tests for Codexen ignore engine.

Compares GitIgnoreSpec evaluation against git check-ignore in a temporary git repository.
Skipped automatically if git executable is not available.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from pathspec import GitIgnoreSpec

from codexen.config import ProjectConfig
from codexen.core.gitignore import clear_ignore_manager_cache, get_project_ignore_manager


def is_git_available() -> bool:
    try:
        res = subprocess.run(["git", "--version"], capture_output=True, text=True)
        return res.returncode == 0
    except Exception:
        return False


@unittest.skipUnless(is_git_available(), "Git executable not available on system")
class TestGitIgnoreParity(unittest.TestCase):
    def setUp(self) -> None:
        if "GIT_CONFIG_GLOBAL" not in os.environ:
            os.environ["GIT_CONFIG_GLOBAL"] = "NUL" if os.name == "nt" else "/dev/null"
        if "GIT_CONFIG_SYSTEM" not in os.environ:
            os.environ["GIT_CONFIG_SYSTEM"] = "NUL" if os.name == "nt" else "/dev/null"
        self.temp_dir = Path(tempfile.mkdtemp(prefix="codexen_ignore_parity_"))
        subprocess.run(["git", "init"], cwd=self.temp_dir, capture_output=True, check=True)
        clear_ignore_manager_cache()

    def tearDown(self) -> None:
        clear_ignore_manager_cache()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_git_check_ignore(self, paths: list[str]) -> dict[str, tuple[bool | None, str]]:
        """Run `git check-ignore -v -n --stdin` and parse (is_ignored, pattern).

        Returns:
            dict mapping path -> (True=ignored, False=negated, None=unmatched, matched_pattern)
        """
        # Ensure path format is posix for git check-ignore
        input_data = "\n".join(p.replace("\\", "/") for p in paths) + "\n"
        proc = subprocess.run(
            ["git", "check-ignore", "-v", "-n", "--stdin"],
            input=input_data.encode("utf-8"),
            cwd=self.temp_dir,
            capture_output=True,
            check=True,
        )
        lines = proc.stdout.decode("utf-8").splitlines()
        results: dict[str, tuple[bool | None, str]] = {}
        for line in lines:
            if not line:
                continue
            # Output format: <source>:<lineno>:<pattern>\t<path> OR ::\t<path>
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            meta, path = parts[0], parts[1].rstrip("\r\n")
            # Handle quoted paths from git (e.g. spaces or special chars)
            if path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
                # Git quotes escape backslashes and quotes
                path = path.encode("utf-8").decode("unicode_escape")
            if meta == "::":
                results[path] = (None, "")
            else:
                meta_parts = meta.split(":")
                pattern = ":".join(meta_parts[2:]) if len(meta_parts) >= 3 else ""
                if pattern.startswith("!"):
                    results[path] = (False, pattern[1:])
                else:
                    results[path] = (True, pattern)
        return results

    def test_basic_patterns(self) -> None:
        patterns = ["*.log", "target/", "!keep.log"]
        (self.temp_dir / ".gitignore").write_text("\n".join(patterns) + "\n", encoding="utf-8")
        
        spec = GitIgnoreSpec.from_lines(patterns)
        test_paths = [
            ("app.log", False),
            ("keep.log", False),
            ("target/out.bin", False),
            ("src/main.rs", False),
            ("target", True),  # directory
        ]
        
        # Git check-ignore requires trailing slash for directories if they don't exist physically,
        # or we create the directory physically
        (self.temp_dir / "target").mkdir()
        (self.temp_dir / "src").mkdir()

        git_paths = ["app.log", "keep.log", "target/out.bin", "src/main.rs", "target/"]
        git_results = self.run_git_check_ignore(git_paths)

        # Check pathspec
        for path, is_dir in test_paths:
            check_p = (path + "/") if is_dir else path
            git_key = (path + "/") if is_dir else path
            spec_res = spec.check_file(check_p)
            git_verdict, git_pat = git_results[git_key]
            self.assertEqual(
                spec_res.include,
                git_verdict,
                f"Mismatch for '{path}': pathspec={spec_res.include}, git={git_verdict}",
            )

    def test_anchoring_patterns(self) -> None:
        patterns = ["/root.txt", "dir/file.txt", "dir/"]
        (self.temp_dir / ".gitignore").write_text("\n".join(patterns) + "\n", encoding="utf-8")
        (self.temp_dir / "dir").mkdir()
        (self.temp_dir / "sub" / "dir").mkdir(parents=True)

        spec = GitIgnoreSpec.from_lines(patterns)
        test_paths = [
            ("root.txt", False),
            ("sub/root.txt", False),
            ("dir/file.txt", False),
            ("sub/dir/file.txt", False),
            ("dir/", True),
            ("sub/dir/", True),
        ]

        git_paths = [p if not is_dir else p for p, is_dir in test_paths]
        git_results = self.run_git_check_ignore(git_paths)

        for path, _is_dir in test_paths:
            spec_res = spec.check_file(path)
            git_verdict, _ = git_results[path]
            self.assertEqual(
                spec_res.include,
                git_verdict,
                f"Mismatch for '{path}': pathspec={spec_res.include}, git={git_verdict}",
            )

    def test_globstar_patterns(self) -> None:
        patterns = ["**/foo", "bar/**", "a/**/b"]
        (self.temp_dir / ".gitignore").write_text("\n".join(patterns) + "\n", encoding="utf-8")

        spec = GitIgnoreSpec.from_lines(patterns)
        test_paths = [
            "foo",
            "x/foo",
            "x/y/foo",
            "bar/baz.txt",
            "bar/sub/baz.txt",
            "a/b",
            "a/x/b",
            "a/x/y/b",
            "other.txt",
        ]

        git_results = self.run_git_check_ignore(test_paths)
        for path in test_paths:
            spec_res = spec.check_file(path)
            git_verdict, _ = git_results[path]
            self.assertEqual(
                spec_res.include,
                git_verdict,
                f"Mismatch for '{path}': pathspec={spec_res.include}, git={git_verdict}",
            )

    def test_character_classes_and_escaping(self) -> None:
        patterns = [r"[ab].txt", r"file\ with\ space", r"\#literal", r"\!literal"]
        (self.temp_dir / ".gitignore").write_text("\n".join(patterns) + "\n", encoding="utf-8")

        spec = GitIgnoreSpec.from_lines(patterns)
        test_paths = [
            "a.txt",
            "b.txt",
            "c.txt",
            "file with space",
            "#literal",
            "!literal",
        ]

        git_results = self.run_git_check_ignore(test_paths)
        for path in test_paths:
            spec_res = spec.check_file(path)
            git_verdict, _ = git_results[path]
            self.assertEqual(
                spec_res.include,
                git_verdict,
                f"Mismatch for '{path}': pathspec={spec_res.include}, git={git_verdict}",
            )

    def test_trailing_space_empty_lines_and_comments(self) -> None:
        patterns = [
            "foo   ",
            "",
            "   ",
            "# this is a comment",
            r"bar\ ",
        ]
        (self.temp_dir / ".gitignore").write_text("\n".join(patterns) + "\n", encoding="utf-8")

        spec = GitIgnoreSpec.from_lines(patterns)
        test_paths = [
            "foo",        # trailing unescaped space stripped
            "foo   ",
            "bar ",       # escaped trailing space preserved
            "# this is a comment",
        ]

        git_results = self.run_git_check_ignore(test_paths)
        for path in test_paths:
            spec_res = spec.check_file(path)
            git_verdict, _ = git_results[path]
            self.assertEqual(
                spec_res.include,
                git_verdict,
                f"Mismatch for '{path}': pathspec={spec_res.include}, git={git_verdict}",
            )

    def test_nested_gitignores_parity(self) -> None:
        root_patterns = ["*.tmp", "dist/"]
        (self.temp_dir / ".gitignore").write_text("\n".join(root_patterns) + "\n", encoding="utf-8")

        fe_dir = self.temp_dir / "packages" / "fe"
        fe_dir.mkdir(parents=True)
        sub_patterns = ["build/", "!keep.tmp"]
        (fe_dir / ".gitignore").write_text("\n".join(sub_patterns) + "\n", encoding="utf-8")

        test_paths = [
            "root.tmp",
            "packages/fe/keep.tmp",
            "packages/fe/other.tmp",
            "packages/fe/build/app.js",
            "packages/fe/src/app.js",
        ]

        git_results = self.run_git_check_ignore(test_paths)

        # In git:
        # root.tmp -> ignored by .gitignore
        # packages/fe/keep.tmp -> negated by packages/fe/.gitignore (!keep.tmp)
        # packages/fe/other.tmp -> ignored by root .gitignore (*.tmp)
        # packages/fe/build/app.js -> ignored by packages/fe/.gitignore (build/)
        # packages/fe/src/app.js -> unmatched (None)
        self.assertEqual(git_results["root.tmp"][0], True)
        self.assertEqual(git_results["packages/fe/keep.tmp"][0], False)
        self.assertEqual(git_results["packages/fe/other.tmp"][0], True)
        self.assertEqual(git_results["packages/fe/build/app.js"][0], True)
        self.assertEqual(git_results["packages/fe/src/app.js"][0], None)

        # Engine-level parity: ProjectIgnoreManager's nested stack must agree
        # with Git for the same fixture (not just pathspec's flat spec).
        proj = ProjectConfig(name="parity", root=self.temp_dir)
        mgr = get_project_ignore_manager(proj, fallback_ignored_patterns=[])
        for path in test_paths:
            git_ignored = git_results[path][0] is True
            engine_ignored = mgr.is_ignored(path, is_dir=False, check_parents=True)
            self.assertEqual(
                engine_ignored,
                git_ignored,
                f"Engine mismatch for '{path}': engine={engine_ignored}, git={git_ignored}",
            )



if __name__ == "__main__":
    unittest.main()

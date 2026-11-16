"""Receipt-level tests for edit_file_snippet: changed_lines, optional hunk, retries."""
from __future__ import annotations

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexen.config import ProjectConfig
from codexen.core import edit_project_file_snippet
from codexen.core.fs import EDIT_REPLACE_RETRY_LIMIT
from codexen.exceptions import FileServiceError


class TestEditReceipt(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.project = ProjectConfig(
            name="receipt_proj",
            root=self.root,
            description="Receipt semantics",
            read_only=False,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, name: str, text: str) -> None:
        (self.root / name).write_bytes(text.encode("utf-8"))

    def _read(self, name: str) -> str:
        return (self.root / name).read_bytes().decode("utf-8")

    def _edit(self, name: str = "doc.txt", **kwargs):
        return edit_project_file_snippet(self.project, name, **kwargs)

    def _assert_no_temp_leftovers(self) -> None:
        self.assertEqual(list(self.root.glob(".codexen_edit_*")), [])

    # --- changed_lines -------------------------------------------------------
    def test_changed_lines_single_line(self) -> None:
        self._write("doc.txt", "a\nb\nNEEDLE\nc\n")
        res = self._edit(search_text="NEEDLE", replace_text="X")
        self.assertEqual(res["changed_lines"], [{"start": 3, "end": 3}])
        self.assertIn("line 3", res["hint"])

    def test_changed_lines_multiline_replacement(self) -> None:
        self._write("doc.txt", "a\nOLD1\nOLD2\nc\n")
        res = self._edit(search_text="OLD1\nOLD2\n", replace_text="N1\nN2\nN3\n")
        self.assertEqual(res["changed_lines"], [{"start": 2, "end": 4}])
        self._assert_no_temp_leftovers()

    def test_changed_lines_deletion(self) -> None:
        self._write("doc.txt", "a\nDEL\nc\n")
        res = self._edit(search_text="DEL\n", replace_text="")
        self.assertEqual(res["changed_lines"], [{"start": 2, "end": 2}])
        self.assertEqual(self._read("doc.txt"), "a\nc\n")

    def test_changed_lines_absent_for_multiple_replacements(self) -> None:
        self._write("doc.txt", "a\nX\nb\nX\n")
        res = self._edit(search_text="X", replace_text="Y", replace_all=True)
        self.assertNotIn("changed_lines", res)
        self.assertNotIn("hunk", res)
        self.assertEqual(res["occurrences_replaced"], 2)

    def test_changed_lines_present_when_replace_all_hits_once(self) -> None:
        self._write("doc.txt", "a\nX\nb\n")
        res = self._edit(search_text="X", replace_text="Y", replace_all=True)
        self.assertEqual(res["changed_lines"], [{"start": 2, "end": 2}])
        self.assertEqual(res["occurrences_replaced"], 1)

    # --- hunk ----------------------------------------------------------------
    def test_hunk_is_off_by_default(self) -> None:
        self._write("doc.txt", "a\nNEEDLE\nc\n")
        res = self._edit(search_text="NEEDLE", replace_text="X")
        self.assertNotIn("hunk", res)

    def test_hunk_included_on_request(self) -> None:
        self._write("doc.txt", "l1\nl2\nOLD_A\nOLD_B\nl5\n")
        res = self._edit(
            search_text="OLD_A\nOLD_B\n",
            replace_text="NEW_A\nNEW_B\nNEW_C\n",
            include_hunk=True,
        )
        hunk = res["hunk"]
        self.assertIn("@@ -3,2 +3,3 @@", hunk)
        self.assertIn("- OLD_A", hunk)
        self.assertIn("+ NEW_A", hunk)
        self.assertIn(" l1", hunk)
        self.assertLessEqual(len(hunk), 320)
        self.assertNotIn("hunk_omitted", res)

    def test_hunk_omitted_for_unfaithful_preview(self) -> None:
        """A long line cannot be shown faithfully: omit the hunk instead of clipping it."""
        long_line = "A" * 150 + "NEEDLE" + "B" * 150
        self._write("doc.txt", f"START\n{long_line}\nEND\n")
        res = self._edit(search_text="NEEDLE", replace_text="X", include_hunk=True)
        self.assertNotIn("hunk", res)
        self.assertEqual(res["hunk_omitted"], "exceeds_budget")
        self.assertIn("git_diff", res["hint"])
        self.assertIn("read_file(start_line=", res["hint"])

    def test_hunk_omitted_for_long_replacement(self) -> None:
        self._write("doc.txt", "head\nTAIL\n")
        replacement = "\n".join(f"line{i:02d} " + "x" * 40 for i in range(30))
        res = self._edit(search_text="TAIL", replace_text=replacement, include_hunk=True)
        self.assertNotIn("hunk", res)
        self.assertEqual(res["hunk_omitted"], "exceeds_budget")
        self.assertIn("git_diff", res["hint"])
        self.assertIn("read_file(start_line=", res["hint"])
        self.assertLessEqual(len(json.dumps(res, ensure_ascii=False)), 700)

    def test_hunk_absent_when_multiple_replacements(self) -> None:
        self._write("doc.txt", "a\nX\nb\nX\n")
        res = self._edit(search_text="X", replace_text="Y", replace_all=True, include_hunk=True)
        self.assertNotIn("hunk", res)

    # --- token budget --------------------------------------------------------
    def test_receipt_size_budget(self) -> None:
        self._write("doc.txt", "a\nNEEDLE\nc\n")
        res = self._edit(search_text="NEEDLE", replace_text="X")
        self.assertLessEqual(len(json.dumps(res, ensure_ascii=False)), 400)

        res = self._edit(search_text="X", replace_text="X", include_hunk=True)
        self.assertLessEqual(len(json.dumps(res, ensure_ascii=False)), 700)
        self.assertLessEqual(len(res["hunk"]), 320)

    # --- Windows lock retries ------------------------------------------------
    def test_transient_replace_failure_is_retried(self) -> None:
        self._write("doc.txt", "hello\n")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(errno.EACCES, "file is locked")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=flaky), mock.patch("time.sleep"):
            res = self._edit(search_text="hello", replace_text="world")

        self.assertEqual(res["status"], "success")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(self._read("doc.txt"), "world\n")
        self._assert_no_temp_leftovers()

    def test_retry_limit_exhausted_keeps_original(self) -> None:
        self._write("doc.txt", "hello\n")
        calls = {"n": 0}

        def always_locked(src, dst):
            calls["n"] += 1
            raise PermissionError(errno.EACCES, "file is locked")

        with mock.patch("os.replace", side_effect=always_locked), mock.patch("time.sleep"):
            with self.assertRaises(FileServiceError):
                self._edit(search_text="hello", replace_text="world")

        self.assertEqual(calls["n"], EDIT_REPLACE_RETRY_LIMIT + 1)
        self.assertEqual(self._read("doc.txt"), "hello\n")
        self._assert_no_temp_leftovers()

    def test_non_transient_replace_error_fails_fast(self) -> None:
        self._write("doc.txt", "hello\n")
        calls = {"n": 0}

        def disk_full(src, dst):
            calls["n"] += 1
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch("os.replace", side_effect=disk_full):
            with self.assertRaises(FileServiceError):
                self._edit(search_text="hello", replace_text="world")

        self.assertEqual(calls["n"], 1)
        self.assertEqual(self._read("doc.txt"), "hello\n")
        self._assert_no_temp_leftovers()


if __name__ == "__main__":
    unittest.main()

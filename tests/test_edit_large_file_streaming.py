"""Validation suite for large-file snippet editing (streamed, size-limit free).

Regression target: `edit_file_snippet` used to read the whole file through
`read_project_file(auto_paginate=False)`, so any file above `settings.max_read_chars`
could not be edited at all and there was no parameter to lift the limit.
"""
from __future__ import annotations

import os
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

from codexen.config import ConfigManager, ProjectConfig
from codexen.core import edit_project_file_snippet, read_project_file, write_project_file
from codexen.exceptions import FileServiceError
from codexen.tools import register_file_tools
from mcp.server.fastmcp import FastMCP


MARKER = "UNIQUE_NEEDLE_'old'"


class TestEditLargeFileStreaming(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.project = ProjectConfig(
            name="edit_proj",
            root=self.root,
            description="Large-file edit project",
            read_only=False,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_bytes(self, name: str, text: str) -> None:
        """Write exact UTF-8 bytes, bypassing platform newline translation."""
        (self.root / name).write_bytes(text.encode("utf-8"))

    def _read_bytes(self, name: str) -> bytes:
        return (self.root / name).read_bytes()

    def _assert_no_temp_leftovers(self) -> None:
        leftovers = list(self.root.rglob(".codexen_edit_*"))
        self.assertEqual(leftovers, [], f"Temporary edit files were not cleaned up: {leftovers}")

    def test_edit_file_above_read_limit_without_extra_parameter(self) -> None:
        """A 2 MB file (far beyond max_read_chars) is editable with the default signature."""
        filler_line = "0123456789abcdef" * 4 + "\n"  # 65 chars
        content = filler_line * 32_000 + MARKER + "trailer\n" + filler_line * 1_000
        self.assertGreater(len(content), 2_000_000)
        self._write_bytes("big.log", content)

        res = edit_project_file_snippet(
            self.project, "big.log", search_text=MARKER, replace_text="REPLACED_OK"
        )

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["relative_path"], "big.log")
        self.assertEqual(res["occurrences_replaced"], 1)
        expected = content.replace(MARKER, "REPLACED_OK", 1)
        self.assertEqual(self._read_bytes("big.log").decode("utf-8"), expected)
        self.assertEqual(len(expected), len(content) - len(MARKER) + len("REPLACED_OK"))
        self._assert_no_temp_leftovers()

    def test_replace_all_and_occurrence_count(self) -> None:
        content = ("head MARKER middle MARKER\n" * 500) + "MARKER tail\n"
        self._write_bytes("many.txt", content)

        res = edit_project_file_snippet(
            self.project, "many.txt", "MARKER", "TOKEN", replace_all=True
        )

        self.assertEqual(res["occurrences_replaced"], 1001)
        self.assertEqual(
            self._read_bytes("many.txt").decode("utf-8"), content.replace("MARKER", "TOKEN")
        )

    def test_ambiguous_edit_rejected_without_touching_file(self) -> None:
        content = ("x" * 40 + " MARKER y\n") * 2_500  # > 100k chars, 2 500 occurrences
        self.assertGreater(len(content), 100_000)
        self._write_bytes("ambiguous.txt", content)

        with self.assertRaises(FileServiceError) as ctx:
            edit_project_file_snippet(self.project, "ambiguous.txt", "MARKER", "TOKEN")

        self.assertIn("occurs 2500 times", str(ctx.exception))
        self.assertIn("replace_all=True", str(ctx.exception))
        self.assertEqual(self._read_bytes("ambiguous.txt").decode("utf-8"), content)
        self._assert_no_temp_leftovers()

    def test_missing_snippet_keeps_original_bytes(self) -> None:
        content = "nothing to see here\n" * 100
        self._write_bytes("missing.txt", content)

        with self.assertRaises(FileServiceError):
            edit_project_file_snippet(self.project, "missing.txt", "ABSENT", "X")

        self.assertEqual(self._read_bytes("missing.txt").decode("utf-8"), content)
        self._assert_no_temp_leftovers()

    def test_chunk_boundary_matches_are_not_lost_or_duplicated(self) -> None:
        """Matches straddling chunk edges must be found exactly once, for tiny chunks too."""
        cases = [
            ("0123456789" * 7 + MARKER + "tail" * 5, MARKER, "<NEW>"),
            ("prefix " + MARKER + MARKER + " suffix", MARKER, "<NEW>"),
            (MARKER, MARKER, "<NEW>"),
            ("aaaa" + "aa" * 40, "aa", "b"),  # overlapping pattern stress
            ("a\nb\nc\nb\nc\n" * 5, "b\nc\n", "B\nC\n"),  # multi-line snippet + newline fidelity
        ]
        for body, search_text, replace_text in cases:
            for chunk_chars in (1, 2, 3, 7, 16, 64, 4096):
                with self.subTest(chunk_chars=chunk_chars, body_len=len(body)):
                    self._write_bytes("boundary.txt", body)
                    edit_project_file_snippet(
                        self.project,
                        "boundary.txt",
                        search_text=search_text,
                        replace_text=replace_text,
                        replace_all=True,
                        chunk_chars=chunk_chars,
                    )
                    self.assertEqual(
                        self._read_bytes("boundary.txt").decode("utf-8"),
                        body.replace(search_text, replace_text),
                    )
                    self._assert_no_temp_leftovers()

    def test_multibyte_text_and_crlf_preserved(self) -> None:
        content = f"第一行\r\n第二行 {MARKER} 数据\r\n第三行 {MARKER}\r\n"
        self._write_bytes("crlf.txt", content)

        edit_project_file_snippet(
            self.project, "crlf.txt", MARKER, "替换值", replace_all=True, chunk_chars=4
        )

        raw = self._read_bytes("crlf.txt")
        expected = "第一行\r\n第二行 替换值 数据\r\n第三行 替换值\r\n".encode()
        self.assertEqual(raw, expected)

    def test_binary_file_rejected(self) -> None:
        binary = b"\x00\x01\x02raw\x00bytes"
        (self.root / "blob.bin").write_bytes(binary)

        with self.assertRaises(FileServiceError) as ctx:
            edit_project_file_snippet(self.project, "blob.bin", "raw", "x")

        self.assertIn("binary", str(ctx.exception))
        self.assertEqual(self._read_bytes("blob.bin"), binary)
        self._assert_no_temp_leftovers()

    def test_invalid_encoding_leaves_file_untouched(self) -> None:
        original = b"valid start \xff invalid tail"
        (self.root / "latin.txt").write_bytes(original)

        with self.assertRaises(FileServiceError):
            edit_project_file_snippet(self.project, "latin.txt", "valid", "changed")

        self.assertEqual(self._read_bytes("latin.txt"), original)
        self._assert_no_temp_leftovers()

    def test_orphaned_temp_files_cleaned_but_active_ones_preserved(self) -> None:
        """Crash leftovers older than an hour are swept; a live temp file is untouched."""
        self._write_bytes("cleanup.txt", "hello\n")
        stale = self.root / ".codexen_edit_stale123.tmp"
        stale.write_bytes(b"orphaned partial content")
        old_ts = time.time() - 7_200
        os.utime(stale, (old_ts, old_ts))
        active = self.root / ".codexen_edit_active123.tmp"
        active.write_bytes(b"concurrent edit in flight")

        edit_project_file_snippet(self.project, "cleanup.txt", "hello", "world")

        self.assertFalse(stale.exists(), "Orphaned temp file was not cleaned up")
        self.assertTrue(active.exists(), "A recent temp file must never be swept")
        active.unlink()

    def test_streaming_edit_uses_bounded_memory(self) -> None:
        """Editing a ~11 MB file must not load it whole (old path held two full copies)."""
        line = "record payload " + "x" * 60 + "\n"
        content = line * 150_000 + "NEEDLE_MARKER_" + "y" * 40 + "\n" + line * 1_000
        self._write_bytes("memory.dat", content)
        self.assertGreater((self.root / "memory.dat").stat().st_size, 10_000_000)

        tracemalloc.start()
        try:
            res = edit_project_file_snippet(
                self.project, "memory.dat", "NEEDLE_MARKER_", "DONE_"
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(res["occurrences_replaced"], 1)
        peak_mb = peak / (1024 * 1024)
        self.assertLess(peak_mb, 10.0, f"Edit peak memory too high: {peak_mb:.2f} MB")
        self._assert_no_temp_leftovers()

    def test_tool_layer_edit_ignores_max_read_chars_setting(self) -> None:
        """End-to-end: MCP tool edits a file larger than settings.max_read_chars=1000."""
        workspace = self.root / "workspace"
        project_dir = workspace / "limited_proj"
        project_dir.mkdir(parents=True)
        config_file = self.root / "config.yaml"
        config_file.write_text(
            f"""
workspace:
  root: "{workspace.as_posix()}"
settings:
  max_read_chars: 1000
projects:
  limited_proj:
    root: "{project_dir.as_posix()}"
""",
            encoding="utf-8",
        )
        config_mgr = ConfigManager(config_path=config_file, base_dir=self.root)
        tools = register_file_tools(FastMCP("Edit Large File Server"), config_mgr)

        large_text = ("padding line of text\n" * 20_000) + "TARGET_OLD" + ("\n" * 10)
        tools["write_file"](project_id="limited_proj", relative_path="huge.txt", content=large_text)

        # Reading the full file hits the old wall and self-heals via pagination...
        read_res = tools["read_file"](project_id="limited_proj", relative_path="huge.txt")
        self.assertIsInstance(read_res, dict)
        self.assertTrue(read_res["truncated"])

        # ...but editing must now succeed in one call, with no force/limit parameter.
        edit_res = tools["edit_file_snippet"](
            project_id="limited_proj",
            relative_path="huge.txt",
            search_text="TARGET_OLD",
            replace_text="TARGET_NEW",
            include_hunk=True,
        )
        self.assertEqual(edit_res["status"], "success")
        self.assertEqual(edit_res["occurrences_replaced"], 1)
        self.assertIn("hunk", edit_res)

        proj = config_mgr.get_project("limited_proj")
        tail = read_project_file(
            proj, "huge.txt", start_line=20_001, end_line=20_011, max_chars=1000
        )
        self.assertIn("TARGET_NEW", tail)
        self.assertNotIn("TARGET_OLD", tail)


if __name__ == "__main__":
    unittest.main()

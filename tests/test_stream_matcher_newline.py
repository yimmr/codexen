import io
from pathlib import Path

from codexen.core.fs import _replace_snippet_streaming
from codexen.core.stream_matcher import replace_streaming_auto
from codexen.exceptions import UnsupportedNewlineEncodingError

def test_crlf_file_matches_lf_pattern_at_all_chunk_sizes(tmp_path: Path):
    source = tmp_path / "x.txt"
    original = b"a\r\nb\r\nc\r\n"
    for size in (1, 2, 3, 7, 16):
        source.write_bytes(original)
        out = io.StringIO()
        result = replace_streaming_auto(source, out, "a\nb\n", "x\ny\n", False, "utf-8", size)
        assert result["occurrences"] == 1
        assert out.getvalue() == "x\r\ny\r\nc\r\n"

def test_lone_cr_is_not_a_newline_token(tmp_path: Path):
    source = tmp_path / "x.txt"
    source.write_bytes(b"a\rb\n")
    out = io.StringIO()
    result = replace_streaming_auto(source, out, "a\rb", "z", False, "utf-8", 1)
    assert result["occurrences"] == 1
    assert out.getvalue() == "z\n"

def test_auto_rejects_utf16(tmp_path: Path):
    source = tmp_path / "x.txt"
    source.write_bytes("x".encode("utf-16"))
    try:
        replace_streaming_auto(source, io.StringIO(), "x", "y", False, "utf-16", 1)
    except UnsupportedNewlineEncodingError:
        return
    raise AssertionError("utf-16 must be rejected in auto mode")


def test_raw_count_is_chunk_invariant_for_overlapping_prefix(tmp_path: Path):
    source = tmp_path / "raw.txt"
    source.write_bytes(b"aaaaaaaaaa")
    for chunk in (1, 2, 3, 7, 16, 64, 4096):
        out = io.StringIO()
        count, *_ = _replace_snippet_streaming(source, out, "aaa", "X", False, "utf-8", chunk, False)
        assert count == 3
        assert out.getvalue() == "Xaaaaaaa"

import io
from pathlib import Path

import pytest

from codexen.core.stream_matcher import replace_streaming_auto

@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 16, 64, 4096])
def test_replay_fixture_compares_complete_auto_result(tmp_path: Path, chunk: int):
    source = tmp_path / "fixture.txt"
    original = b"header\r\nTARGET\r\nfooter\n"
    source.write_bytes(original)
    out = io.StringIO()
    result = replace_streaming_auto(source, out, "TARGET\n", "REPLACED\n", False, "utf-8", chunk)
    assert result["occurrences"] == 1
    assert out.getvalue().encode() == b"header\r\nREPLACED\r\nfooter\n"

def test_replay_fixture_failure_leaves_source_unchanged(tmp_path: Path):
    source = tmp_path / "fixture.txt"
    original = b"header\r\nTARGET\r\n"
    source.write_bytes(original)
    out = io.StringIO()
    result = replace_streaming_auto(source, out, "MISSING\n", "X\n", False, "utf-8", 3)
    assert result["occurrences"] == 0
    assert source.read_bytes() == original

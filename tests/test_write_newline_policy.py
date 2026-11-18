from unittest.mock import patch

import pytest

from codexen.config import ProjectConfig
from codexen.core.fs import write_project_file
from codexen.exceptions import FileServiceError, UnsupportedNewlineEncodingError

def test_raw_write_preserves_literal_crlf(tmp_path):
    project = ProjectConfig(name="p", root=tmp_path, read_only=False)
    write_project_file(project, "x.txt", "a\r\nb", newline_mode="raw")
    assert (tmp_path / "x.txt").read_bytes() == b"a\r\nb"

def test_auto_write_does_not_double_crlf(tmp_path):
    project = ProjectConfig(name="p", root=tmp_path, read_only=False)
    (tmp_path / "x.txt").write_bytes(b"old\r\n")
    write_project_file(project, "x.txt", "new\r\n", newline_mode="auto")
    assert b"\r\r\n" not in (tmp_path / "x.txt").read_bytes()


def test_failed_append_preserves_original_and_cleans_temp(tmp_path):
    project = ProjectConfig(name="p", root=tmp_path, read_only=False)
    target = tmp_path / "x.txt"
    target.write_bytes(b"original\r\n")
    with patch("codexen.core.fs._atomic_replace_with_retry", side_effect=OSError("disk full")):
        with pytest.raises(FileServiceError):
            write_project_file(project, "x.txt", "new\n", append=True)
    assert target.read_bytes() == b"original\r\n"
    assert not list(tmp_path.glob(".codexen_write_*.tmp"))


def test_append_uses_last_newline_beyond_sample_limit(tmp_path):
    project = ProjectConfig(name="p", root=tmp_path, read_only=False)
    target = tmp_path / "x.txt"
    original = b"prefix\n" + b"x" * 1_048_576 + b"tail\r\n"
    target.write_bytes(original)
    result = write_project_file(project, "x.txt", "new\n", append=True)
    assert target.read_bytes() == original + b"new\r\n"
    assert result["newline_style_used"] == "crlf"


def test_utf8_sig_append_does_not_insert_second_bom(tmp_path):
    project = ProjectConfig(name="p", root=tmp_path, read_only=False)
    target = tmp_path / "x.txt"
    original = "old\r\n".encode("utf-8-sig")
    target.write_bytes(original)
    write_project_file(project, "x.txt", "new\n", encoding="utf-8-sig", append=True)
    assert target.read_bytes() == original + b"new\r\n"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32", "no-such-codec"])
def test_unsupported_write_encoding_does_not_change_target(tmp_path, encoding):
    project = ProjectConfig(name="p", root=tmp_path, read_only=False)
    target = tmp_path / "x.txt"
    target.write_bytes(b"old")
    with pytest.raises(UnsupportedNewlineEncodingError):
        write_project_file(project, "x.txt", "new", encoding=encoding)
    assert target.read_bytes() == b"old"

from unittest.mock import patch

from codexen.core.edit_diagnostics import diagnose_edit, diagnostic_summary, safe_diagnose_edit


def test_diagnostic_reports_prefix_without_content(tmp_path):
    path = tmp_path / "source.txt"
    path.write_bytes(b"secret\n" * 10)
    result = diagnose_edit(path, "secret", max_bytes=14)
    assert result["scope"] == "prefix"
    assert result["diagnostic_truncated"]
    assert result["scanned_bytes"] == 14
    assert result["occurrences_observed"] == 2
    assert "secret" not in str(result)


def test_diagnostic_exception_is_isolated(tmp_path):
    with patch("codexen.core.edit_diagnostics.diagnose_edit", side_effect=RuntimeError("broken")):
        assert safe_diagnose_edit(tmp_path / "source", "x")["scope"] == "unavailable"


def test_short_common_line_is_not_an_anchor(tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b"try:\n" * 20)
    assert diagnose_edit(path, "try:")["anchors"] == []


def test_rare_long_line_produces_scoped_anchor(tmp_path):
    path = tmp_path / "source"
    line = "def distinctive_function_for_editing():"
    path.write_text("header\n" + line + "\n")
    result = diagnose_edit(path, line + "\nold body")
    assert result["anchors"][0]["lines"] == [2]
    assert result["scope"] == "full"


def test_summary_exposes_prefix_scope_without_full_file_claim(tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b"line\n" * 20)
    result = diagnose_edit(path, "missing", max_bytes=8)
    summary = diagnostic_summary(result)
    assert "scope=prefix" in summary
    assert "scanned_bytes=8" in summary
    assert "no matches" not in summary

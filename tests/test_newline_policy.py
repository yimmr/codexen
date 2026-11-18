from codexen.core.newline_policy import convert_newline_text, normalize_newline_text, profile_newlines
from codexen.core.newline_policy import validate_newline_encoding

def test_profile_tie_uses_first_style():
    profile = profile_newlines("a\r\nb\nc\r\n")
    assert profile.mixed
    assert profile.dominant_style == "crlf"

def test_conversion_preserves_lone_cr():
    text = "a\r\nb\rc\n"
    assert normalize_newline_text(text) == "a\nb\rc\n"
    assert convert_newline_text(text, "crlf") == "a\r\nb\rc\r\n"


def test_safe_codec_aliases_are_accepted():
    for codec in ("utf8", "utf-8-sig", "latin1", "cp1252", "gbk", "gb18030", "gb2312", "big5"):
        validate_newline_encoding(codec)

"""Explicit, lossless newline primitives used by text editing."""
from __future__ import annotations

import codecs
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

NewlineStyle = Literal["crlf", "lf"]
SAFE_ENCODINGS = {"utf-8", "utf-8-sig", "ascii", "latin-1", "cp1252", "gbk", "gb18030", "gb2312", "big5"}


def validate_newline_encoding(encoding: str) -> None:
    """Reject codecs where ASCII newline byte boundaries are not isolated."""
    from ..exceptions import UnsupportedNewlineEncodingError

    try:
        name = codecs.lookup(encoding).name
        supported = {codecs.lookup(item).name for item in SAFE_ENCODINGS}
    except LookupError as exc:
        raise UnsupportedNewlineEncodingError(f"auto newline mode does not support encoding '{encoding}'") from exc
    if name not in supported:
        raise UnsupportedNewlineEncodingError(f"auto newline mode does not support encoding '{encoding}'")


@dataclass(frozen=True)
class NewlineProfile:
    crlf_count: int
    lf_count: int
    first_style: NewlineStyle | None
    last_style: NewlineStyle | None

    @property
    def has_newline(self) -> bool:
        return bool(self.crlf_count or self.lf_count)

    @property
    def mixed(self) -> bool:
        return self.crlf_count > 0 and self.lf_count > 0

    @property
    def dominant_style(self) -> NewlineStyle | None:
        if not self.has_newline:
            return None
        if self.crlf_count == self.lf_count and self.first_style is not None:
            return self.first_style
        return "crlf" if self.crlf_count > self.lf_count else "lf"


def iter_newline_tokens(text: str) -> Iterator[tuple[str, str]]:
    i = 0
    while i < len(text):
        if text[i:i + 2] == "\r\n":
            yield "\n", "\r\n"
            i += 2
        elif text[i] == "\n":
            yield "\n", "\n"
            i += 1
        else:
            yield text[i], text[i]
            i += 1


def normalize_newline_text(text: str) -> str:
    return "".join(logical for logical, _ in iter_newline_tokens(text))


def convert_newline_text(text: str, style: NewlineStyle | None) -> str:
    if style is None:
        return text
    out: list[str] = []
    for logical, raw in iter_newline_tokens(text):
        if logical == "\n":
            out.append("\r\n" if style == "crlf" else "\n")
        else:
            out.append(raw)
    return "".join(out)


def profile_newlines(text: str) -> NewlineProfile:
    styles = [raw for logical, raw in iter_newline_tokens(text) if logical == "\n"]
    return NewlineProfile(styles.count("\r\n"), styles.count("\n"),
                          ("crlf" if styles[0] == "\r\n" else "lf") if styles else None,
                          ("crlf" if styles[-1] == "\r\n" else "lf") if styles else None)

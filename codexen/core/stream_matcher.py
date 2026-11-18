"""Streaming logical-newline matcher with bounded KMP look-behind."""
from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from .newline_policy import (
    NewlineProfile,
    NewlineStyle,
    convert_newline_text,
    iter_newline_tokens,
    validate_newline_encoding,
)

SAFE_ENCODINGS = {"utf-8", "utf-8-sig", "ascii", "latin-1", "cp1252", "gbk", "gb18030", "gb2312", "big5"}


def _tokens(path: Path, encoding: str, chunk_chars: int) -> Iterator[tuple[str, str]]:
    carry = ""
    with open(path, encoding=encoding, errors="strict", newline="") as src:
        while chunk := src.read(max(1, chunk_chars)):
            text = carry + chunk
            if text.endswith("\r"):
                carry, text = "\r", text[:-1]
            else:
                carry = ""
            yield from iter_newline_tokens(text)
    if carry:
        yield "\r", "\r"


def _failure_table(pattern: str) -> list[int]:
    table = [0] * len(pattern)
    q = 0
    for i in range(1, len(pattern)):
        while q and pattern[i] != pattern[q]:
            q = table[q - 1]
        if pattern[i] == pattern[q]:
            q += 1
        table[i] = q
    return table


def replace_streaming_auto(src_path: Path, dst: Any, search_text: str, replace_text: str,
                          replace_all: bool, encoding: str, chunk_chars: int = 1_048_576,
                          include_hunk: bool = False) -> dict[str, Any]:
    """Replace non-overlapping logical matches while preserving unmatched raw text."""
    validate_newline_encoding(encoding)
    pattern = "".join(logical for logical, _ in iter_newline_tokens(search_text))
    if not pattern:
        return {"occurrences": 0, "changed_lines": None, "hunk": None,
                "hunk_omitted_reason": None, "profile": NewlineProfile(0, 0, None, None)}
    failure = _failure_table(pattern)
    pending: deque[str] = deque()
    q = 0
    count = crlf = lf = 0
    first: str | None = None
    last: str | None = None
    recent: str | None = None
    output_lines = 0
    start_line = end_line = 0
    before = after = ""
    captured_before = ""

    def copy_raw(raw: str) -> None:
        nonlocal output_lines, before, after
        dst.write(raw)
        output_lines += raw.count("\n")
        if include_hunk:
            if count:
                if len(after) < 200 and after.count("\n") < 2:
                    after = (after + raw)[:200]
            else:
                before = (before + raw)[-200:]

    def write_old(n: int = 1) -> None:
        for _ in range(n):
            copy_raw(pending.popleft())

    for token, raw in _tokens(src_path, encoding, chunk_chars):
        if token == "\n":
            token_style = "crlf" if raw == "\r\n" else "lf"
            crlf += token_style == "crlf"
            lf += token_style == "lf"
            first = first or token_style
            last = recent = token_style
        pending.append(raw)
        while q and token != pattern[q]:
            next_q = failure[q - 1]
            write_old(q - next_q)
            q = next_q
        if token == pattern[q]:
            q += 1
            if q == len(pattern):
                count += 1
                region = list(pending)
                style: NewlineStyle | None = cast(NewlineStyle | None, next(("crlf" if part == "\r\n" else "lf" for part in region if part in ("\r\n", "\n")), None))
                if style is None:
                    style = cast(NewlineStyle, recent or ("crlf" if crlf >= lf and crlf else "lf" if lf else first) or "lf")
                if replace_all or count == 1:
                    body = convert_newline_text(replace_text, style)
                    if count == 1:
                        start_line = output_lines + 1
                        end_line = max(start_line, start_line + body.count("\n") - int(body.endswith("\n")))
                        captured_before = before
                    dst.write(body)
                    output_lines += body.count("\n")
                else:
                    dst.write("".join(region))
                pending.clear()
                q = 0
        elif q == 0:
            write_old()
    while pending:
        write_old()
    profile = NewlineProfile(crlf, lf, cast(NewlineStyle | None, first), cast(NewlineStyle | None, last))
    hunk = omitted = None
    if count == 1 and include_hunk:
        from .fs import _build_snippet_hunk
        from .newline_policy import normalize_newline_text
        hunk, omitted = _build_snippet_hunk(normalize_newline_text(captured_before),
                                          normalize_newline_text(search_text), normalize_newline_text(replace_text),
                                          normalize_newline_text(after), start_line)
    return {"occurrences": count, "changed_lines": [{"start": start_line, "end": end_line}] if count == 1 else None, "hunk": hunk,
            "hunk_omitted_reason": omitted,
            "profile": profile}

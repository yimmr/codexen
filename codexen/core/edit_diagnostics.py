"""Bounded, evidence-based diagnostics for failed edits."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_server_core.logging import sanitize_string_value

from .newline_policy import normalize_newline_text


def escaped_preview(text: str) -> str:
    cleaned = sanitize_string_value(text)
    return cleaned.encode("unicode_escape").decode("ascii")[:120]


def diagnose_edit(path: Path, search_text: str, *, max_bytes: int = 8 * 1024 * 1024) -> dict[str, Any]:
    with path.open("rb") as source:
        data = source.read(max_bytes)
    text = data.decode("utf-8", errors="replace")
    logical = normalize_newline_text(text)
    lines = logical.split("\n")
    wanted = normalize_newline_text(search_text).split("\n")
    anchors: list[dict[str, Any]] = []
    full_scope = path.stat().st_size <= max_bytes
    # Single-line anchors require a full scan to prove rarity. Prefix scans
    # only use consecutive context pairs and label their evidence accordingly.
    for candidate in wanted[:32]:
        if full_scope and len(candidate) >= 25:
            positions = [i + 1 for i, line in enumerate(lines) if line == candidate]
            if 0 < len(positions) <= 2:
                anchors.append({"lines": positions, "preview": escaped_preview(candidate)})
    if not anchors:
        for a, b in zip(wanted[:32], wanted[1:33], strict=False):
            if not a.strip() or not b.strip():
                continue
            positions = [i + 1 for i in range(len(lines) - 1) if lines[i:i + 2] == [a, b]]
            if positions:
                anchors.append({"lines": positions[:5], "total_observed": len(positions),
                                "preview": escaped_preview(a + "\n" + b)})
            if len(anchors) >= 5:
                break
    return {"diagnostic_truncated": path.stat().st_size > max_bytes, "scanned_bytes": len(data),
            "scanned_lines": text.count("\n") + (1 if text else 0), "scope": "prefix" if path.stat().st_size > max_bytes else "full",
            "occurrences_observed": logical.count(normalize_newline_text(search_text)) if search_text else 0,
            "anchors": anchors[:5]}


def safe_diagnose_edit(path: Path, search_text: str) -> dict[str, Any]:
    """Diagnostics must never replace the original tool failure."""
    try:
        return diagnose_edit(path, search_text)
    except Exception:
        return {"scope": "unavailable", "occurrences_observed": "unknown"}


def diagnostic_summary(diagnostic: dict[str, Any]) -> str:
    """Render bounded evidence without turning observations into conclusions."""
    scope = diagnostic.get("scope", "unavailable")
    observed = diagnostic.get("occurrences_observed", "unknown")
    scanned = diagnostic.get("scanned_bytes", "unknown")
    anchors = diagnostic.get("anchors") or []
    suffix = f"; anchors={len(anchors)}" if anchors else ""
    return f"Diagnostic scope={scope}, scanned_bytes={scanned}, occurrences_observed={observed}{suffix}."

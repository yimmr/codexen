"""Document operations: read and inspect rich office and technical documents (DOCX, PDF, XLSX, PPTX)."""
from __future__ import annotations

from typing import Any

from ..core import (
    normalize_relative_path,
    read_project_document,
)
from .decorators import ToolContext


def register_document_tools(ctx: ToolContext) -> None:
    """Register rich document parsing, pagination, and outline inspection tools."""

    @ctx.register_tool()
    def read_document_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        # --- Universal pagination ---
        page: int = 1,
        page_count: int = 10,
        max_chars: int = 50_000,
        # --- Format-specific fine controls ---
        start_page: int = 0,
        end_page: int = 0,
        sheet_name: str = "",
        start_row: int = 0,
        max_rows: int = 100,
        start_slide: int = 0,
        end_slide: int = 0,
        start_paragraph: int = 0,
        max_paragraphs: int = 0,
        # --- Output modes and safeguards ---
        format: str = "markdown",
        outline_only: bool = False,
        auto_paginate: bool = True,
    ) -> dict[str, Any]:
        """Read non-plaintext rich documents (DOCX, PDF, XLSX, PPTX) with LLM-friendly Markdown and chunked pagination.

        Converts tables to Markdown tables, headers to markdown headings (#, ##), and slide/sheet structures
        into structured Markdown or raw text. Built-in anti-overflow self-healing protocol prevents context blowout.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            relative_path: Path to the document file within the project root boundary.
            path: Alias for relative_path.
            page: Universal 1-based page / chunk number (default: 1).
            page_count: Number of pages / slides per segment (default: 10).
            max_chars: Maximum characters to return before triggering auto-pagination (default: 50,000).
            start_page: Fine-grained PDF 1-based start page (overrides page).
            end_page: Fine-grained PDF 1-based end page (inclusive).
            sheet_name: Target XLSX sheet name (defaults to first active sheet).
            start_row: XLSX 1-based start row index (default: 1).
            max_rows: Maximum rows to read per XLSX sheet chunk (default: 100).
            start_slide: Fine-grained PPTX 1-based start slide (overrides page).
            end_slide: Fine-grained PPTX 1-based end slide (inclusive).
            start_paragraph: DOCX 1-based start paragraph/element index.
            max_paragraphs: Maximum paragraphs/elements per DOCX slice (default: 50).
            format: Output format ('markdown' or 'raw', default: 'markdown').
            outline_only: If True, fast-probes TOC/sheet outline/metadata without full content.
            auto_paginate: If True, automatically paginates and returns self-healing telemetry instead of error.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return read_project_document(
            project=proj,
            relative_path=target_path,
            page=page,
            page_count=page_count,
            max_chars=max_chars,
            start_page=start_page,
            end_page=end_page,
            sheet_name=sheet_name,
            start_row=start_row,
            max_rows=max_rows,
            start_slide=start_slide,
            end_slide=end_slide,
            start_paragraph=start_paragraph,
            max_paragraphs=max_paragraphs,
            format=format,
            outline_only=outline_only,
            auto_paginate=auto_paginate,
        )

    ctx.register_alias(
        read_document_file,
        "read_document",
        "Alias for read_document_file. Read rich documents (DOCX, PDF, XLSX, PPTX) with LLM-friendly Markdown and chunked pagination.",
    )

    @ctx.register_tool()
    def inspect_document(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
    ) -> dict[str, Any]:
        """Inspect structural outline, TOC, sheet list, and metadata of a rich document (PDF, DOCX, XLSX, PPTX).

        Fast millisecond-level probe without extracting full text/body. Returns sheet lists with row/col
        dimensions, PDF bookmarks / table of contents, presentation slide lists, and document metadata.

        Args:
            project_id: The ID/name of the configured project.
            project_name: Alias for project_id.
            relative_path: Path to the document file within the project.
            path: Alias for relative_path.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return read_project_document(
            project=proj,
            relative_path=target_path,
            outline_only=True,
        )

    ctx.register_alias(
        inspect_document,
        "inspect_document_file",
        "Alias for inspect_document. Fast outline, TOC, and metadata probe for rich document files.",
    )

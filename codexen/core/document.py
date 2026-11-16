"""Universal Document Extraction Engine and Anti-Overflow Chunked Healing Protocol.

Supports DOCX, PDF, XLSX, and PPTX with pure-Python drivers, outline extraction,
rich Markdown formatting, streaming pagination, and anti-hallucination telemetry.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from ..config import ProjectConfig
from ..exceptions import FileServiceError, FileSizeLimitError
from .security import (
    assert_not_sensitive_path,
    ensure_project_accessible,
    safe_resolve_path,
)

DOCUMENT_EXTENSIONS: set[str] = {".pdf", ".docx", ".xlsx", ".pptx"}


def _format_cell_value(val: Any) -> str:
    """Format an Excel or table cell value safely into Markdown/text."""
    if val is None:
        return ""
    if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
        return val.isoformat()
    s = str(val).strip()
    return s.replace("\r\n", " ").replace("\n", " ").replace("|", "\\|")


def _render_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render rows into a GitHub Flavored Markdown table."""
    col_count = max(len(headers), max((len(r) for r in rows), default=0))
    if col_count == 0:
        return ""

    norm_headers = [headers[i] if i < len(headers) else f"Col {i + 1}" for i in range(col_count)]
    header_line = "| " + " | ".join(norm_headers) + " |"
    separator_line = "| " + " | ".join(["---"] * col_count) + " |"

    data_lines = []
    for r in rows:
        norm_row = [r[i] if i < len(r) else "" for i in range(col_count)]
        data_lines.append("| " + " | ".join(norm_row) + " |")

    return "\n".join([header_line, separator_line] + data_lines)


# ============================================================================
# PDF Driver (pypdf)
# ============================================================================

def _parse_pdf_outline(reader: Any, outline_items: list[Any]) -> list[dict[str, Any]]:
    """Recursively parse pypdf outline bookmarks into structured JSON."""
    results: list[dict[str, Any]] = []
    current_entry: dict[str, Any] | None = None

    for item in outline_items:
        if isinstance(item, list):
            children = _parse_pdf_outline(reader, item)
            if current_entry is not None:
                current_entry.setdefault("children", []).extend(children)
            else:
                results.extend(children)
        else:
            title = getattr(item, "title", str(item))
            page_num = 1
            try:
                page_num = reader.get_destination_page_number(item) + 1
            except Exception:
                pass
            current_entry = {
                "title": title,
                "page": page_num,
                "children": [],
            }
            results.append(current_entry)

    return results


def _extract_pdf(
    file_path: Path,
    page: int = 1,
    page_count: int = 10,
    start_page: int = 0,
    end_page: int = 0,
    output_format: str = "markdown",
    outline_only: bool = False,
) -> dict[str, Any]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(file_path))
    except Exception as e:
        raise FileServiceError(f"Failed to parse PDF document '{file_path.name}': {e}") from e

    total_pages = len(reader.pages)

    # Metadata extraction
    meta = reader.metadata
    title = getattr(meta, "title", "") or ""
    author = getattr(meta, "author", "") or ""
    creator = getattr(meta, "creator", "") or ""
    producer = getattr(meta, "producer", "") or ""
    c_date = getattr(meta, "creation_date", "") or ""
    m_date = getattr(meta, "modification_date", "") or ""
    metadata: dict[str, Any] = {
        "title": title,
        "author": author,
        "creator": creator,
        "producer": producer,
        "creation_date": str(c_date) if c_date else "",
        "modification_date": str(m_date) if m_date else "",
    }

    # Outline extraction
    outline: list[dict[str, Any]] = []
    try:
        if reader.outline:
            outline = _parse_pdf_outline(reader, reader.outline)
    except Exception:
        outline = []

    if outline_only:
        return {
            "document_type": "pdf",
            "total_pages": total_pages,
            "metadata": metadata,
            "outline": outline,
            "available_sheets": [],
            "content": "",
        }

    if total_pages == 0:
        return {
            "document_type": "pdf",
            "total_pages": 0,
            "returned_start_page": 0,
            "returned_end_page": 0,
            "content": "",
            "available_sheets": [],
            "metadata": metadata,
            "warning": "Document contains 0 pages.",
        }

    # Determine 1-based page window
    if start_page > 0 or end_page > 0:
        s_page = max(1, start_page)
        e_page = min(total_pages, end_page) if end_page > 0 else min(total_pages, s_page + page_count - 1)
    else:
        s_page = max(1, (page - 1) * page_count + 1)
        e_page = min(total_pages, s_page + page_count - 1)

    s_page = min(s_page, total_pages)
    e_page = max(s_page, min(e_page, total_pages))

    page_blocks: list[str] = []
    total_extracted_chars = 0

    for p_no in range(s_page, e_page + 1):
        try:
            page_obj = reader.pages[p_no - 1]
            txt = (page_obj.extract_text() or "").strip()
        except Exception as p_err:
            txt = f"[Error extracting page {p_no}: {p_err}]"

        img_count = 0
        try:
            if hasattr(page_obj, "images"):
                img_count = len(page_obj.images)
        except Exception:
            img_count = 0

        img_annotation = f"\n\n*[Image: {img_count} embedded image(s) on Page {p_no}]*" if img_count > 0 else ""

        total_extracted_chars += len(txt)
        if output_format == "raw":
            page_blocks.append(f"[Page {p_no}/{total_pages}]\n{txt}{img_annotation}")
        else:
            page_blocks.append(f"--- Page {p_no} / {total_pages} ---\n\n{txt}{img_annotation}")

    content = "\n\n".join(page_blocks)

    warning = ""
    if total_extracted_chars < 10:
        warning = "Document appears to be a scanned image with no text layer. Text extraction cannot extract scanned pixels."

    res: dict[str, Any] = {
        "document_type": "pdf",
        "total_pages": total_pages,
        "returned_start_page": s_page,
        "returned_end_page": e_page,
        "content": content,
        "available_sheets": [],
        "metadata": metadata,
        "outline": outline,
    }
    if warning:
        res["warning"] = warning
    return res


# ============================================================================
# DOCX Driver (python-docx)
# ============================================================================

def _extract_docx(
    file_path: Path,
    page: int = 1,
    page_count: int = 10,
    start_paragraph: int = 0,
    max_paragraphs: int = 0,
    output_format: str = "markdown",
    outline_only: bool = False,
) -> dict[str, Any]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = docx.Document(str(file_path))
    except Exception as e:
        raise FileServiceError(f"Failed to parse DOCX document '{file_path.name}': {e}") from e

    # Extract metadata
    metadata: dict[str, Any] = {}
    try:
        cp = doc.core_properties
        metadata = {
            "title": cp.title or "",
            "author": cp.author or "",
            "created": cp.created.isoformat() if cp.created else "",
            "modified": cp.modified.isoformat() if cp.modified else "",
            "comments": cp.comments or "",
            "category": cp.category or "",
        }
    except Exception:
        metadata = {}

    # Extract outline (headings)
    outline: list[dict[str, Any]] = []
    heading_prefixes = {
        "heading 1": 1,
        "heading 2": 2,
        "heading 3": 3,
        "heading 4": 4,
        "heading 5": 5,
        "heading 6": 6,
        "title": 1,
    }

    try:
        for p_idx, p in enumerate(doc.paragraphs):
            style_name = (p.style.name if p.style and p.style.name else "").lower()
            text = p.text.strip()
            if not text:
                continue
            for h_prefix, level in heading_prefixes.items():
                if style_name.startswith(h_prefix) or style_name == h_prefix:
                    outline.append({
                        "level": level,
                        "text": text,
                        "style": getattr(p.style, "name", "") or "",
                        "paragraph_index": p_idx + 1,
                    })
                    break
    except Exception:
        outline = []

    if outline_only:
        total_p_and_tbl = 0
        try:
            total_p_and_tbl = sum(1 for child in doc.element.body if child.tag.endswith(("p", "tbl")))
        except Exception:
            total_p_and_tbl = len(doc.paragraphs)
        return {
            "document_type": "docx",
            "total_pages": max(1, (total_p_and_tbl + 49) // 50),
            "total_paragraphs": total_p_and_tbl,
            "metadata": metadata,
            "outline": outline,
            "available_sheets": [],
            "content": "",
        }

    # Extract body elements in document order
    elements: list[tuple[str, Any]] = []
    try:
        for child in doc.element.body:
            if child.tag.endswith("p"):
                elements.append(("p", Paragraph(child, doc)))
            elif child.tag.endswith("tbl"):
                elements.append(("tbl", Table(child, doc)))
    except Exception:
        # Graceful fallback to paragraphs
        elements = [("p", p) for p in doc.paragraphs]

    total_elements = len(elements)
    per_page = 50
    total_pages = max(1, (total_elements + per_page - 1) // per_page)

    if start_paragraph > 0:
        s_idx = start_paragraph - 1
        chunk_len = max_paragraphs if max_paragraphs > 0 else (per_page * max(1, page_count))
        e_idx = s_idx + chunk_len
    else:
        p_num = max(1, page)
        p_cnt = max(1, page_count)
        s_idx = (p_num - 1) * per_page
        chunk_len = max_paragraphs if max_paragraphs > 0 else (per_page * p_cnt)
        e_idx = s_idx + chunk_len

    s_idx = max(0, min(s_idx, max(0, total_elements - 1))) if total_elements > 0 else 0
    e_idx = min(total_elements, max(s_idx + 1, e_idx))

    slice_elements = elements[s_idx:e_idx]
    rendered_blocks: list[str] = []

    for elem_type, elem in slice_elements:
        try:
            if elem_type == "p":
                text = elem.text.strip()
                has_drawing = False
                try:
                    if hasattr(elem, "_p") and elem._p is not None and "drawing" in elem._p.xml:
                        has_drawing = True
                except Exception:
                    has_drawing = False

                if not text and not has_drawing:
                    continue

                if not text and has_drawing:
                    rendered_blocks.append("*[Image: Embedded graphic]*")
                    continue

                drawing_suffix = " *[Image: Embedded graphic]*" if has_drawing else ""
                if output_format == "raw":
                    rendered_blocks.append(f"{text}{drawing_suffix}")
                else:
                    style_name = (elem.style.name if elem.style and elem.style.name else "").lower()
                    if style_name.startswith("heading 1"):
                        rendered_blocks.append(f"# {text}{drawing_suffix}")
                    elif style_name.startswith("heading 2"):
                        rendered_blocks.append(f"## {text}{drawing_suffix}")
                    elif style_name.startswith("heading 3"):
                        rendered_blocks.append(f"### {text}{drawing_suffix}")
                    elif style_name.startswith("heading 4"):
                        rendered_blocks.append(f"#### {text}{drawing_suffix}")
                    elif style_name == "title":
                        rendered_blocks.append(f"# {text}{drawing_suffix}")
                    elif style_name == "subtitle":
                        rendered_blocks.append(f"*{text}*{drawing_suffix}")
                    elif "bullet" in style_name:
                        rendered_blocks.append(f"- {text}{drawing_suffix}")
                    else:
                        rendered_blocks.append(f"{text}{drawing_suffix}")
            elif elem_type == "tbl":
                # Convert Table to Markdown or TSV
                t_rows: list[list[str]] = []
                for row in elem.rows:
                    row_vals = [_format_cell_value(cell.text) for cell in row.cells]
                    if any(row_vals):
                        t_rows.append(row_vals)
                if not t_rows:
                    continue
                if output_format == "raw":
                    rendered_blocks.append("\n".join(["\t".join(r) for r in t_rows]))
                else:
                    headers = t_rows[0]
                    body_rows = t_rows[1:] if len(t_rows) > 1 else []
                    rendered_blocks.append(_render_markdown_table(headers, body_rows))
        except Exception:
            # Fallback for individual complex XML element
            fallback_txt = getattr(elem, "text", "")
            if fallback_txt.strip():
                rendered_blocks.append(fallback_txt.strip())

    content = "\n\n".join(rendered_blocks)

    ret_start_page = (s_idx // per_page) + 1
    ret_end_page = max(ret_start_page, min(total_pages, (e_idx + per_page - 1) // per_page))

    return {
        "document_type": "docx",
        "total_pages": total_pages,
        "total_paragraphs": total_elements,
        "returned_start_page": ret_start_page,
        "returned_end_page": ret_end_page,
        "returned_start_paragraph": s_idx + 1,
        "returned_end_paragraph": e_idx,
        "content": content,
        "available_sheets": [],
        "metadata": metadata,
        "outline": outline,
    }


# ============================================================================
# XLSX Driver (openpyxl)
# ============================================================================

def _extract_xlsx(
    file_path: Path,
    sheet_name: str = "",
    page: int = 1,
    page_count: int = 10,
    start_row: int = 0,
    max_rows: int = 100,
    output_format: str = "markdown",
    outline_only: bool = False,
) -> dict[str, Any]:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(filename=str(file_path), read_only=True, data_only=True)
    except Exception as e:
        raise FileServiceError(f"Failed to parse XLSX document '{file_path.name}': {e}") from e

    try:
        sheets = wb.sheetnames
        metadata: dict[str, Any] = {}
        try:
            props = wb.properties
            metadata = {
                "title": props.title or "",
                "creator": props.creator or "",
                "created": props.created.isoformat() if props.created else "",
                "modified": props.modified.isoformat() if props.modified else "",
            }
        except Exception:
            metadata = {}

        outline: list[dict[str, Any]] = []
        for sname in sheets:
            ws_probe = wb[sname]
            outline.append({
                "sheet_name": sname,
                "max_row": ws_probe.max_row,
                "max_column": ws_probe.max_column,
            })

        if outline_only:
            return {
                "document_type": "xlsx",
                "total_pages": len(sheets),
                "total_sheets": len(sheets),
                "metadata": metadata,
                "outline": outline,
                "available_sheets": sheets,
                "content": "",
            }

        target_sheet = sheet_name.strip() if sheet_name.strip() else (sheets[0] if sheets else "")
        if not target_sheet or target_sheet not in sheets:
            raise FileServiceError(
                f"Sheet '{sheet_name}' not found in workbook '{file_path.name}'. Available sheets: {sheets}"
            )

        ws = wb[target_sheet]
        limit_rows = max(1, max_rows)
        if start_row > 0:
            s_row = start_row
            cur_page = ((s_row - 1) // limit_rows) + 1
        else:
            cur_page = max(1, page)
            s_row = (cur_page - 1) * limit_rows + 1

        e_row = s_row + limit_rows - 1

        # Fetch header row if s_row > 1 to preserve Markdown table structure
        header_row_vals: list[str] = []
        if s_row > 1:
            try:
                for r in ws.iter_rows(min_row=1, max_row=1, values_only=True):
                    header_row_vals = [_format_cell_value(c) for c in r]
                    break
            except Exception:
                header_row_vals = []

        data_rows: list[list[str]] = []
        actual_end_row = s_row
        row_cursor = s_row

        for row_tuple in ws.iter_rows(min_row=s_row, max_row=e_row, values_only=True):
            actual_end_row = row_cursor
            row_cursor += 1
            formatted = [_format_cell_value(c) for c in row_tuple]
            # Smart empty row skipping
            if any(formatted):
                data_rows.append(formatted)

        # Check if more rows exist beyond e_row
        has_more_rows = False
        try:
            for _ in ws.iter_rows(min_row=e_row + 1, max_row=e_row + 1, values_only=True):
                has_more_rows = True
                break
        except Exception:
            if ws.max_row and ws.max_row > e_row:
                has_more_rows = True

        total_sheet_pages = 1
        if ws.max_row and ws.max_row > 0:
            total_sheet_pages = max(1, (ws.max_row + limit_rows - 1) // limit_rows)
        else:
            total_sheet_pages = cur_page if not has_more_rows else (cur_page + 1)
        total_sheet_pages = max(total_sheet_pages, cur_page)

        if output_format == "raw":
            lines = [f"[Sheet: {target_sheet} (Rows {s_row}-{actual_end_row})]"]
            for r in data_rows:
                lines.append("\t".join(r))
            content = "\n".join(lines)
        else:
            header_title = f"### Sheet: {target_sheet} (Rows {s_row}-{actual_end_row})\n\n"
            if s_row > 1 and header_row_vals:
                table_md = _render_markdown_table(header_row_vals, data_rows)
            elif data_rows:
                table_md = _render_markdown_table(data_rows[0], data_rows[1:])
            else:
                table_md = "*[Sheet slice is empty]*"
            content = header_title + table_md

        return {
            "document_type": "xlsx",
            "total_pages": total_sheet_pages,
            "total_sheets": len(sheets),
            "sheet_name": target_sheet,
            "returned_start_page": cur_page,
            "returned_end_page": cur_page,
            "returned_start_row": s_row,
            "returned_end_row": actual_end_row,
            "has_more_rows": has_more_rows,
            "content": content,
            "available_sheets": sheets,
            "metadata": metadata,
            "outline": outline,
        }
    finally:
        wb.close()


# ============================================================================
# PPTX Driver (python-pptx)
# ============================================================================

def _extract_pptx(
    file_path: Path,
    page: int = 1,
    page_count: int = 10,
    start_slide: int = 0,
    end_slide: int = 0,
    output_format: str = "markdown",
    outline_only: bool = False,
) -> dict[str, Any]:
    from pptx import Presentation

    try:
        prs = Presentation(str(file_path))
    except Exception as e:
        raise FileServiceError(f"Failed to parse PPTX document '{file_path.name}': {e}") from e

    total_slides = len(prs.slides)

    # Metadata extraction
    metadata: dict[str, Any] = {}
    try:
        cp = prs.core_properties
        metadata = {
            "title": cp.title or "",
            "author": cp.author or "",
            "created": cp.created.isoformat() if cp.created else "",
            "modified": cp.modified.isoformat() if cp.modified else "",
            "subject": cp.subject or "",
        }
    except Exception:
        metadata = {}

    # Outline extraction (slide titles)
    outline: list[dict[str, Any]] = []
    for s_idx, slide in enumerate(prs.slides):
        title = ""
        try:
            if slide.shapes.title and slide.shapes.title.text:
                title = slide.shapes.title.text.strip()
        except Exception:
            pass

        if not title:
            for shp in slide.shapes:
                if shp.has_text_frame and shp.text_frame.text.strip():
                    title = shp.text_frame.text.strip().split("\n")[0]
                    break

        outline.append({
            "slide": s_idx + 1,
            "title": title or f"Slide {s_idx + 1}",
        })

    if outline_only:
        return {
            "document_type": "pptx",
            "total_pages": total_slides,
            "metadata": metadata,
            "outline": outline,
            "available_sheets": [],
            "content": "",
        }

    if total_slides == 0:
        return {
            "document_type": "pptx",
            "total_pages": 0,
            "returned_start_page": 0,
            "returned_end_page": 0,
            "content": "",
            "available_sheets": [],
            "metadata": metadata,
            "outline": outline,
        }

    # Slide slicing
    if start_slide > 0 or end_slide > 0:
        s_slide = max(1, start_slide)
        e_slide = min(total_slides, end_slide) if end_slide > 0 else min(total_slides, s_slide + page_count - 1)
    else:
        s_slide = max(1, (page - 1) * page_count + 1)
        e_slide = min(total_slides, s_slide + page_count - 1)

    s_slide = min(s_slide, total_slides)
    e_slide = max(s_slide, min(e_slide, total_slides))

    slide_blocks: list[str] = []

    for s_no in range(s_slide, e_slide + 1):
        slide = prs.slides[s_no - 1]
        slide_title = outline[s_no - 1]["title"] if s_no - 1 < len(outline) else f"Slide {s_no}"

        body_elements: list[str] = []
        for shape in slide.shapes:
            try:
                # Tables in slide
                if shape.has_table:
                    tbl_rows: list[list[str]] = []
                    for row in shape.table.rows:
                        row_vals = [_format_cell_value(cell.text) for cell in row.cells]
                        if any(row_vals):
                            tbl_rows.append(row_vals)
                    if tbl_rows:
                        if output_format == "raw":
                            body_elements.append("\n".join(["\t".join(r) for r in tbl_rows]))
                        else:
                            body_elements.append(_render_markdown_table(tbl_rows[0], tbl_rows[1:]))
                elif shape.has_text_frame:
                    # Text box / bullet points
                    tf_text = shape.text_frame.text.strip()
                    if not tf_text:
                        continue
                    # Skip if exact duplicate of slide title
                    if tf_text == slide_title and shape == slide.shapes.title:
                        continue

                    para_lines: list[str] = []
                    for p in shape.text_frame.paragraphs:
                        p_txt = p.text.strip()
                        if not p_txt:
                            continue
                        if output_format == "raw":
                            para_lines.append(p_txt)
                        else:
                            indent = "  " * max(0, p.level or 0)
                            para_lines.append(f"{indent}- {p_txt}")
                    if para_lines:
                        body_elements.append("\n".join(para_lines))
                elif getattr(shape, "shape_type", None) == 13 or hasattr(shape, "image"):
                    pic_name = getattr(shape, "name", "Picture")
                    body_elements.append(f"*[Image: {pic_name} on Slide {s_no}]*")
            except Exception:
                continue

        # Speaker notes
        notes_text = ""
        try:
            if slide.has_notes_slide:
                notes_frame = slide.notes_slide.notes_text_frame
                if notes_frame and notes_frame.text.strip():
                    notes_text = notes_frame.text.strip()
        except Exception:
            pass

        if output_format == "raw":
            block = f"[Slide {s_no}: {slide_title}]\n" + "\n".join(body_elements)
            if notes_text:
                block += f"\nNotes: {notes_text}"
        else:
            block = f"### Slide {s_no}: {slide_title}\n\n" + "\n\n".join(body_elements)
            if notes_text:
                block += f"\n\n> 📝 Speaker Notes: {notes_text}"

        slide_blocks.append(block)

    content = "\n\n---\n\n".join(slide_blocks)

    return {
        "document_type": "pptx",
        "total_pages": total_slides,
        "returned_start_page": s_slide,
        "returned_end_page": e_slide,
        "content": content,
        "available_sheets": [],
        "metadata": metadata,
        "outline": outline,
    }


# ============================================================================
# High-Level Core Dispatcher & Self-Healing Telemetry
# ============================================================================

def read_project_document(
    project: ProjectConfig,
    relative_path: str,
    page: int = 1,
    page_count: int = 10,
    max_chars: int = 50_000,
    start_page: int = 0,
    end_page: int = 0,
    sheet_name: str = "",
    start_row: int = 0,
    max_rows: int = 100,
    start_slide: int = 0,
    end_slide: int = 0,
    start_paragraph: int = 0,
    max_paragraphs: int = 0,
    format: str = "markdown",
    outline_only: bool = False,
    auto_paginate: bool = True,
) -> dict[str, Any]:
    """Read a rich document (PDF, DOCX, XLSX, PPTX) within project boundaries with anti-overflow telemetry."""
    assert_not_sensitive_path(relative_path, "Reading document")
    ensure_project_accessible(project)
    file_path = safe_resolve_path(project.root, relative_path)

    suffix = file_path.suffix.lower()
    if suffix not in DOCUMENT_EXTENSIONS:
        raise FileServiceError(
            f"Unsupported document format '{suffix}' for '{relative_path}'. Supported formats: {sorted(DOCUMENT_EXTENSIONS)}"
        )

    if not file_path.exists():
        raise FileNotFoundError(
            f"Document file not found: '{relative_path}' in project '{project.name}' (Root: {project.root})"
        )
    if file_path.is_dir():
        raise FileServiceError(f"Cannot read document '{relative_path}': Path is a directory, not a regular file.")

    # Empty file check
    try:
        if file_path.stat().st_size == 0:
            doc_type = suffix.lstrip(".")
            return {
                "status": "success",
                "project": project.name,
                "relative_path": relative_path,
                "format": format,
                "document_type": doc_type,
                "total_pages": 0,
                "returned_start_page": 0,
                "returned_end_page": 0,
                "returned_chars": 0,
                "truncated": False,
                "truncation_reason": "",
                "content": "",
                "available_sheets": [],
                "has_more": False,
                "next_page": 0,
                "hint": f"Document '{relative_path}' is empty (0 bytes).",
            }
    except OSError as e:
        raise FileServiceError(f"Failed to inspect document '{relative_path}': {e}") from e

    # Format dispatch
    if suffix == ".pdf":
        raw_res = _extract_pdf(
            file_path=file_path,
            page=page,
            page_count=page_count,
            start_page=start_page,
            end_page=end_page,
            output_format=format,
            outline_only=outline_only,
        )
    elif suffix == ".docx":
        raw_res = _extract_docx(
            file_path=file_path,
            page=page,
            page_count=page_count,
            start_paragraph=start_paragraph,
            max_paragraphs=max_paragraphs,
            output_format=format,
            outline_only=outline_only,
        )
    elif suffix == ".xlsx":
        raw_res = _extract_xlsx(
            file_path=file_path,
            sheet_name=sheet_name,
            page=page,
            page_count=page_count,
            start_row=start_row,
            max_rows=max_rows,
            output_format=format,
            outline_only=outline_only,
        )
    elif suffix == ".pptx":
        raw_res = _extract_pptx(
            file_path=file_path,
            page=page,
            page_count=page_count,
            start_slide=start_slide,
            end_slide=end_slide,
            output_format=format,
            outline_only=outline_only,
        )
    else:
        raise FileServiceError(f"Unsupported format: {suffix}")

    doc_type = raw_res["document_type"]

    if outline_only:
        out_res = {
            "status": "success",
            "project": project.name,
            "relative_path": relative_path,
            "format": format,
            "document_type": doc_type,
            "total_pages": raw_res.get("total_pages", 0),
            "outline": raw_res.get("outline", []),
            "metadata": raw_res.get("metadata", {}),
            "available_sheets": raw_res.get("available_sheets", []),
            "truncated": False,
            "hint": f"Outline successfully generated for '{relative_path}'. Call read_document_file to read full content.",
        }
        if "total_sheets" in raw_res:
            out_res["total_sheets"] = raw_res["total_sheets"]
        return out_res

    content = raw_res.get("content", "")
    total_pages = raw_res.get("total_pages", 0)
    returned_start = raw_res.get("returned_start_page", 1)
    returned_end = raw_res.get("returned_end_page", 1)

    truncated = False
    truncation_reason = ""
    has_more = False
    next_page = 0

    if doc_type == "xlsx":
        has_more = raw_res.get("has_more_rows", False)
        target_sheet = raw_res.get("sheet_name", "")
        ret_s_row = raw_res.get("returned_start_row", 1)
        ret_e_row = raw_res.get("returned_end_row", 1)
        if has_more:
            truncated = True
            truncation_reason = "max_rows_limit_reached"
            next_start_row = ret_e_row + 1
            next_page = returned_end + 1
            hint = (
                f"Document truncated: Showing rows {ret_s_row}-{ret_e_row} of sheet '{target_sheet}'. "
                f"Call read_document_file(project_id='{project.name}', relative_path='{relative_path}', "
                f"sheet_name='{target_sheet}', page={next_page}) or with start_row={next_start_row}, max_rows={max_rows} to read the next segment."
            )
        else:
            hint = f"Full content of sheet '{target_sheet}' successfully retrieved."
    elif doc_type in ("pdf", "pptx"):
        has_more = returned_end < total_pages
        if has_more:
            truncated = True
            truncation_reason = "default_page_limit_reached"
            next_page = returned_end + 1
            unit = "pages" if doc_type == "pdf" else "slides"
            arg_start = "start_page" if doc_type == "pdf" else "start_slide"
            arg_end = "end_page" if doc_type == "pdf" else "end_slide"
            next_end = min(total_pages, next_page + page_count - 1)
            hint = (
                f"Document truncated: Showing {unit} {returned_start}-{returned_end} of {total_pages}. "
                f"Call read_document_file(project_id='{project.name}', relative_path='{relative_path}', "
                f"{arg_start}={next_page}, {arg_end}={next_end}) to read the next segment."
            )
        else:
            hint = f"All {total_pages} {doc_type.upper()} pages/slides successfully retrieved."
    elif doc_type == "docx":
        total_p = raw_res.get("total_paragraphs", 0)
        end_p = raw_res.get("returned_end_paragraph", 0)
        has_more = end_p < total_p
        if has_more:
            truncated = True
            truncation_reason = "default_page_limit_reached"
            next_page = returned_end + 1
            next_start_p = end_p + 1
            hint = (
                f"Document truncated: Showing paragraphs {raw_res.get('returned_start_paragraph', 1)}-{end_p} of {total_p}. "
                f"Call read_document_file(project_id='{project.name}', relative_path='{relative_path}', "
                f"start_paragraph={next_start_p}, max_paragraphs={max_paragraphs or 50}) to read the next segment."
            )
        else:
            hint = "All DOCX paragraphs successfully retrieved."
    else:
        hint = "Document retrieved successfully."

    # Max characters overflow protection
    if len(content) > max_chars:
        if not auto_paginate:
            raise FileSizeLimitError(
                f"Extracted document content ({len(content)} chars) exceeds max allowed limit of {max_chars} characters. "
                f"Tip: Specify a narrower page/row window or enable auto_paginate."
            )
        content = content[:max_chars]
        truncated = True
        truncation_reason = "max_chars_limit_reached"
        hint = (
            f"Content truncated due to max_chars limit ({max_chars} chars). "
            f"Specify narrower pagination parameters (start_page, sheet_name, start_row, etc.) to read smaller segments."
        )

    payload: dict[str, Any] = {
        "status": "success",
        "project": project.name,
        "relative_path": relative_path,
        "format": format,
        "document_type": doc_type,
        "total_pages": total_pages,
        "returned_start_page": returned_start,
        "returned_end_page": returned_end,
        "returned_chars": len(content),
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "content": content,
        "available_sheets": raw_res.get("available_sheets", []),
        "has_more": has_more,
        "next_page": next_page,
        "hint": hint,
    }

    if "total_sheets" in raw_res:
        payload["total_sheets"] = raw_res["total_sheets"]
    if "sheet_name" in raw_res:
        payload["sheet_name"] = raw_res["sheet_name"]
    if "returned_start_row" in raw_res:
        payload["returned_start_row"] = raw_res["returned_start_row"]
    if "returned_end_row" in raw_res:
        payload["returned_end_row"] = raw_res["returned_end_row"]
    if "returned_start_paragraph" in raw_res:
        payload["returned_start_paragraph"] = raw_res["returned_start_paragraph"]
    if "returned_end_paragraph" in raw_res:
        payload["returned_end_paragraph"] = raw_res["returned_end_paragraph"]

    if "warning" in raw_res:
        payload["warning"] = raw_res["warning"]
    if "metadata" in raw_res and raw_res["metadata"]:
        payload["metadata"] = raw_res["metadata"]

    return payload


def extract_document_content(
    project: ProjectConfig,
    relative_path: str,
    page: int = 1,
    page_count: int = 10,
    max_chars: int = 50_000,
    start_page: int = 0,
    end_page: int = 0,
    sheet_name: str = "",
    start_row: int = 0,
    max_rows: int = 100,
    start_slide: int = 0,
    end_slide: int = 0,
    start_paragraph: int = 0,
    max_paragraphs: int = 0,
    format: str = "markdown",
    auto_paginate: bool = True,
) -> dict[str, Any]:
    """Extract content from a document file."""
    return read_project_document(
        project=project,
        relative_path=relative_path,
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
        outline_only=False,
        auto_paginate=auto_paginate,
    )


def inspect_document_outline(
    project: ProjectConfig,
    relative_path: str,
) -> dict[str, Any]:
    """Inspect structural outline, TOC, sheets, and metadata of a document file."""
    return read_project_document(
        project=project,
        relative_path=relative_path,
        outline_only=True,
    )

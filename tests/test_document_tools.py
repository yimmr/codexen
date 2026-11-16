"""Comprehensive unit and integration tests for RFC 0002 Universal Document Extraction Engine.

Covers:
- PDF (pypdf): Multi-page streaming, outline TOC, bookmarks, scanned warning, fine-grained pagination
- DOCX (python-docx): Heading styles (#, ##), tables to Markdown tables, bullet points, outline, raw format
- XLSX (openpyxl): Multi-sheet support, row windowing, header preservation, markdown table conversion, outline
- PPTX (python-pptx): Slide slicing, shapes, bullet indentation, tables, speaker notes, outline
- MCP Tool Layer: read_document_file, read_document, inspect_document, inspect_document_file
- Transparent Routing: read_file routing to document engine with guidance hint
- Anti-Hallucination Telemetry: total_pages, returned_start_page, returned_end_page, next_page, truncated, hint
- Security & Boundary Errors: sensitive paths, missing files, corrupt files, max_chars overflow
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import docx
import openpyxl
from pptx import Presentation
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from codexen.config import ConfigManager, ProjectConfig
from codexen.core import (
    DOCUMENT_EXTENSIONS,
    extract_document_content,
    inspect_document_outline,
    read_project_document,
)
from codexen.exceptions import FileServiceError, FileSizeLimitError, PathSecurityError
from codexen.tools import register_file_tools

try:
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]


def _create_sample_pdf(file_path: Path, num_pages: int = 15, empty_pages: bool = False) -> None:
    """Create a test PDF with text and bookmarks."""
    writer = PdfWriter()
    font_dict = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })

    for i in range(num_pages):
        page = writer.add_blank_page(width=300, height=300)
        if not empty_pages:
            stream = DecodedStreamObject()
            text = f"Content of PDF Page {i + 1}. System specification and architecture design."
            stream.set_data(f"BT /F1 12 Tf 50 150 Td ({text}) Tj ET".encode("latin-1"))
            page[NameObject("/Contents")] = stream
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_dict})
            })

    if not empty_pages and num_pages >= 2:
        writer.add_outline_item(title="Chapter 1: Overview", page_number=0)
        writer.add_outline_item(title="Chapter 2: Architecture", page_number=1)

    with open(file_path, "wb") as f:
        writer.write(f)


def _create_sample_docx(file_path: Path) -> None:
    """Create a test DOCX with headings, bullet points, and tables."""
    doc = docx.Document()
    doc.core_properties.title = "Architecture Spec"
    doc.core_properties.author = "System Architect"

    doc.add_heading("Architecture Specification", level=1)
    doc.add_paragraph("This document describes the core system architecture and module boundaries.")

    doc.add_heading("Subsystems", level=2)
    doc.add_paragraph("Microservice Cluster", style="List Bullet")
    doc.add_paragraph("Gateway Proxy", style="List Bullet")

    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "Service"
    table.cell(0, 1).text = "Port"
    table.cell(1, 0).text = "auth-service"
    table.cell(1, 1).text = "8081"
    table.cell(2, 0).text = "data-service"
    table.cell(2, 1).text = "8082"

    doc.save(str(file_path))


def _create_sample_xlsx(file_path: Path, num_rows: int = 150) -> None:
    """Create a test XLSX with multiple sheets and a large row set."""
    wb = openpyxl.Workbook()
    ws_metrics = wb.active
    ws_metrics.title = "Metrics"
    ws_metrics.append(["MetricID", "MetricName", "TargetValue", "Status"])

    for i in range(1, num_rows + 1):
        ws_metrics.append([f"M{i:03d}", f"ResponseTime_{i}", i * 5, "OK" if i % 2 == 0 else "WARN"])

    ws_config = wb.create_sheet("Config")
    ws_config.append(["ConfigKey", "ConfigValue"])
    ws_config.append(["max_connections", 500])
    ws_config.append(["timeout_seconds", 30])

    wb.save(str(file_path))


def _create_sample_pptx(file_path: Path, num_slides: int = 12) -> None:
    """Create a test PPTX presentation with slide titles, bullet points, and speaker notes."""
    prs = Presentation()
    prs.core_properties.title = "Codexen Technical Briefing"
    prs.core_properties.author = "Core Architecture Team"

    for i in range(num_slides):
        slide_layout = prs.slide_layouts[1]  # title and content
        slide = prs.slides.add_slide(slide_layout)
        slide.shapes.title.text = f"Slide {i + 1}: Topic {i + 1}"

        tf = slide.placeholders[1].text_frame
        tf.text = f"Key highlight for topic {i + 1}"
        p2 = tf.add_paragraph()
        p2.text = f"Detailed engineering consideration {i + 1}"
        p2.level = 1

        if i == 1:
            notes = slide.notes_slide
            notes.notes_text_frame.text = "Speaker notes: Emphasize memory safety and zero C-dependencies."

    prs.save(str(file_path))


class TestDocumentTools(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()
        self.workspace_dir = self.base_dir / "workspace"
        self.workspace_dir.mkdir(parents=True)

        self.proj_dir = self.workspace_dir / "core_proj"
        self.proj_dir.mkdir(parents=True)
        self.docs_dir = self.proj_dir / "docs"
        self.docs_dir.mkdir(parents=True)

        # Generate sample documents
        self.pdf_path = self.docs_dir / "spec.pdf"
        _create_sample_pdf(self.pdf_path, num_pages=15)

        self.scanned_pdf_path = self.docs_dir / "scanned.pdf"
        _create_sample_pdf(self.scanned_pdf_path, num_pages=2, empty_pages=True)

        self.docx_path = self.docs_dir / "architecture.docx"
        _create_sample_docx(self.docx_path)

        self.xlsx_path = self.docs_dir / "metrics.xlsx"
        _create_sample_xlsx(self.xlsx_path, num_rows=150)

        self.pptx_path = self.docs_dir / "slides.pptx"
        _create_sample_pptx(self.pptx_path, num_slides=12)

        # Config setup
        self.config_path = self.base_dir / "config.yaml"
        self.config_path.write_text(f"""
workspace:
  root: "{self.workspace_dir.as_posix()}"
  allow_create_project: true
projects:
  core_proj:
    root: "{self.proj_dir.as_posix()}"
    description: "Core Project with Documents"
    read_only: false
permissions:
  allow_file_delete: true
  allow_command_execution: true
""", encoding="utf-8")

        self.cfg_mgr = ConfigManager(config_path=self.config_path, base_dir=self.base_dir)
        self.mcp = FastMCP("Test Document Tools MCP")
        self.tools = register_file_tools(self.mcp, self.cfg_mgr)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # ========================================================================
    # 1. PDF Tests
    # ========================================================================

    def test_pdf_auto_pagination_and_telemetry(self) -> None:
        """Test reading 15-page PDF with default auto-pagination (pages 1-10)."""
        res = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/spec.pdf",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["document_type"], "pdf")
        self.assertEqual(res["total_pages"], 15)
        self.assertEqual(res["returned_start_page"], 1)
        self.assertEqual(res["returned_end_page"], 10)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["truncation_reason"], "default_page_limit_reached")
        self.assertTrue(res["has_more"])
        self.assertEqual(res["next_page"], 11)
        self.assertIn("--- Page 1 / 15 ---", res["content"])
        self.assertIn("--- Page 10 / 15 ---", res["content"])
        self.assertNotIn("--- Page 11 / 15 ---", res["content"])
        self.assertIn("start_page=11", res["hint"])

    def test_pdf_fine_grained_page_slice(self) -> None:
        """Test reading specific PDF slice (pages 11 to 15)."""
        res = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/spec.pdf",
            start_page=11,
            end_page=15,
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["returned_start_page"], 11)
        self.assertEqual(res["returned_end_page"], 15)
        self.assertFalse(res["truncated"])
        self.assertFalse(res["has_more"])
        self.assertIn("--- Page 11 / 15 ---", res["content"])
        self.assertIn("--- Page 15 / 15 ---", res["content"])
        self.assertNotIn("--- Page 1 / 15 ---", res["content"])

    def test_pdf_scanned_document_warning(self) -> None:
        """Test detection and warning for scanned image PDFs with no text layer."""
        res = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/scanned.pdf",
        )
        self.assertEqual(res["status"], "success")
        self.assertIn("warning", res)
        self.assertIn("scanned image with no text layer", res["warning"])

    def test_pdf_outline_inspection(self) -> None:
        """Test outline and TOC extraction on PDF."""
        res = self.tools["inspect_document"](
            project_id="core_proj",
            relative_path="docs/spec.pdf",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["document_type"], "pdf")
        self.assertEqual(res["total_pages"], 15)
        self.assertIn("outline", res)
        self.assertGreaterEqual(len(res["outline"]), 2)
        titles = [item["title"] for item in res["outline"]]
        self.assertIn("Chapter 1: Overview", titles)
        self.assertIn("Chapter 2: Architecture", titles)

    # ========================================================================
    # 2. DOCX Tests
    # ========================================================================

    def test_docx_headings_and_tables_markdown(self) -> None:
        """Test DOCX conversion to Markdown headings and GitHub Flavored Markdown table."""
        res = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/architecture.docx",
            format="markdown",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["document_type"], "docx")
        content = res["content"]
        # Headings converted to # and ##
        self.assertIn("# Architecture Specification", content)
        self.assertIn("## Subsystems", content)
        # Bullet list converted to -
        self.assertIn("- Microservice Cluster", content)
        # Table converted to Markdown table
        self.assertIn("| Service | Port |", content)
        self.assertIn("| auth-service | 8081 |", content)
        self.assertIn("| data-service | 8082 |", content)

    def test_docx_raw_format(self) -> None:
        """Test DOCX raw format output without markdown markers."""
        res = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/architecture.docx",
            format="raw",
        )
        self.assertEqual(res["status"], "success")
        self.assertNotIn("# Architecture", res["content"])
        self.assertIn("Architecture Specification", res["content"])
        self.assertIn("auth-service\t8081", res["content"])

    def test_docx_outline_inspection(self) -> None:
        """Test DOCX outline inspection extracts heading hierarchy and document properties."""
        res = self.tools["inspect_document"](
            project_id="core_proj",
            relative_path="docs/architecture.docx",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["document_type"], "docx")
        self.assertIn("outline", res)
        outline = res["outline"]
        self.assertTrue(any(h["text"] == "Architecture Specification" and h["level"] == 1 for h in outline))
        self.assertTrue(any(h["text"] == "Subsystems" and h["level"] == 2 for h in outline))
        self.assertEqual(res["metadata"]["title"], "Architecture Spec")
        self.assertEqual(res["metadata"]["author"], "System Architect")

    # ========================================================================
    # 3. XLSX Tests
    # ========================================================================

    def test_xlsx_sheet_pagination_and_headers(self) -> None:
        """Test XLSX row slicing, max_rows truncation, and preserved table headers."""
        # 1. Default first chunk (rows 1-100)
        res1 = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/metrics.xlsx",
            sheet_name="Metrics",
            start_row=1,
            max_rows=100,
        )
        self.assertEqual(res1["status"], "success")
        self.assertEqual(res1["document_type"], "xlsx")
        self.assertTrue(res1["truncated"])
        self.assertEqual(res1["truncation_reason"], "max_rows_limit_reached")
        self.assertTrue(res1["has_more"])
        self.assertIn("### Sheet: Metrics", res1["content"])
        self.assertIn("| MetricID | MetricName | TargetValue | Status |", res1["content"])
        self.assertIn("| M001 | ResponseTime_1 | 5 | WARN |", res1["content"])
        self.assertIn("| M099 | ResponseTime_99 | 495 | WARN |", res1["content"])
        self.assertNotIn("| M100 |", res1["content"])
        self.assertIn("start_row=101", res1["hint"])

        # 2. Second chunk (rows 101-150) preserves header
        res2 = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/metrics.xlsx",
            sheet_name="Metrics",
            start_row=101,
            max_rows=100,
        )
        self.assertEqual(res2["status"], "success")
        self.assertFalse(res2["has_more"])
        self.assertIn("| MetricID | MetricName | TargetValue | Status |", res2["content"])
        self.assertIn("| M100 | ResponseTime_100 |", res2["content"])
        self.assertIn("| M150 | ResponseTime_150 |", res2["content"])

    def test_xlsx_multiple_sheets_and_outline(self) -> None:
        """Test XLSX available sheets listing and secondary sheet reading."""
        outline_res = self.tools["inspect_document"](
            project_id="core_proj",
            relative_path="docs/metrics.xlsx",
        )
        self.assertEqual(outline_res["status"], "success")
        self.assertIn("Metrics", outline_res["available_sheets"])
        self.assertIn("Config", outline_res["available_sheets"])

        # Read the second sheet directly
        cfg_res = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/metrics.xlsx",
            sheet_name="Config",
        )
        self.assertEqual(cfg_res["status"], "success")
        self.assertIn("max_connections", cfg_res["content"])
        self.assertIn("timeout_seconds", cfg_res["content"])

    # ========================================================================
    # 4. PPTX Tests
    # ========================================================================

    def test_pptx_slides_and_speaker_notes(self) -> None:
        """Test PPTX slide chunking and speaker notes extraction."""
        res = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/slides.pptx",
            start_slide=1,
            end_slide=2,
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["document_type"], "pptx")
        self.assertEqual(res["total_pages"], 12)
        self.assertEqual(res["returned_start_page"], 1)
        self.assertEqual(res["returned_end_page"], 2)
        self.assertTrue(res["has_more"])
        self.assertIn("### Slide 1: Slide 1: Topic 1", res["content"])
        self.assertIn("### Slide 2: Slide 2: Topic 2", res["content"])
        self.assertIn("> 📝 Speaker Notes: Speaker notes: Emphasize memory safety and zero C-dependencies.", res["content"])

    def test_pptx_outline_inspection(self) -> None:
        """Test PPTX outline inspection lists all slides and core metadata."""
        res = self.tools["inspect_document"](
            project_id="core_proj",
            relative_path="docs/slides.pptx",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["document_type"], "pptx")
        self.assertEqual(res["total_pages"], 12)
        self.assertEqual(len(res["outline"]), 12)
        self.assertIn("Topic 1", res["outline"][0]["title"])
        self.assertEqual(res["metadata"]["title"], "Codexen Technical Briefing")
        self.assertEqual(res["metadata"]["author"], "Core Architecture Team")

    # ========================================================================
    # 5. MCP Aliases and Transparent Routing
    # ========================================================================

    def test_mcp_aliases(self) -> None:
        """Verify read_document and inspect_document_file aliases function identically."""
        # read_document alias
        res1 = self.tools["read_document"](
            project_id="core_proj",
            relative_path="docs/architecture.docx",
        )
        self.assertEqual(res1["status"], "success")
        self.assertEqual(res1["document_type"], "docx")

        # inspect_document_file alias
        res2 = self.tools["inspect_document_file"](
            project_id="core_proj",
            relative_path="docs/architecture.docx",
        )
        self.assertEqual(res2["status"], "success")
        self.assertIn("outline", res2)

    def test_read_file_transparent_fallback_routing(self) -> None:
        """Verify read_file silently routes rich documents to document engine with guidance hint."""
        # PDF transparent routing
        pdf_res = self.tools["read_file"](
            project_id="core_proj",
            relative_path="docs/spec.pdf",
        )
        self.assertIsInstance(pdf_res, dict)
        self.assertEqual(pdf_res["status"], "success")
        self.assertEqual(pdf_res["document_type"], "pdf")
        self.assertIn("--- Page 1 / 15 ---", pdf_res["content"])
        self.assertIn("Notice: 'docs/spec.pdf' is a rich document file", pdf_res["hint"])
        self.assertIn("read_document_file", pdf_res["hint"])

        # DOCX transparent routing
        docx_res = self.tools["read_file"](
            project_id="core_proj",
            relative_path="docs/architecture.docx",
        )
        self.assertIsInstance(docx_res, dict)
        self.assertEqual(docx_res["document_type"], "docx")
        self.assertIn("# Architecture Specification", docx_res["content"])
        self.assertIn("Notice: 'docs/architecture.docx' is a rich document file", docx_res["hint"])

        # XLSX transparent routing
        xlsx_res = self.tools["read_file"](
            project_id="core_proj",
            relative_path="docs/metrics.xlsx",
        )
        self.assertIsInstance(xlsx_res, dict)
        self.assertEqual(xlsx_res["document_type"], "xlsx")
        self.assertIn("### Sheet: Metrics", xlsx_res["content"])
        self.assertIn("Notice: 'docs/metrics.xlsx' is a rich document file", xlsx_res["hint"])

        # PPTX transparent routing
        pptx_res = self.tools["read_file"](
            project_id="core_proj",
            relative_path="docs/slides.pptx",
        )
        self.assertIsInstance(pptx_res, dict)
        self.assertEqual(pptx_res["document_type"], "pptx")
        self.assertIn("### Slide 1", pptx_res["content"])
        self.assertIn("Notice: 'docs/slides.pptx' is a rich document file", pptx_res["hint"])

    def test_xlsx_universal_page_pagination(self) -> None:
        """Test XLSX reading with universal page parameter (page=1, page=2) without specifying start_row."""
        # Page 1 (rows 1-100)
        res1 = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/metrics.xlsx",
            page=1,
            max_rows=100,
        )
        self.assertEqual(res1["status"], "success")
        self.assertEqual(res1["returned_start_page"], 1)
        self.assertEqual(res1["returned_end_page"], 1)
        self.assertEqual(res1["returned_start_row"], 1)
        self.assertEqual(res1["returned_end_row"], 100)
        self.assertEqual(res1["total_pages"], 2)
        self.assertTrue(res1["truncated"])
        self.assertEqual(res1["next_page"], 2)

        # Page 2 (rows 101-151)
        res2 = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/metrics.xlsx",
            page=2,
            max_rows=100,
        )
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["returned_start_page"], 2)
        self.assertEqual(res2["returned_end_page"], 2)
        self.assertEqual(res2["returned_start_row"], 101)
        self.assertEqual(res2["returned_end_row"], 151)
        self.assertFalse(res2["has_more"])
        self.assertIn("| M100 | ResponseTime_100 |", res2["content"])
        self.assertIn("| M150 | ResponseTime_150 |", res2["content"])

    def test_read_file_line_range_routing(self) -> None:
        """Test read_file with start_line and end_line properly routed to document slice."""
        # PDF range routing
        pdf_res = self.tools["read_file"](
            project_id="core_proj",
            relative_path="docs/spec.pdf",
            start_line=11,
            end_line=15,
        )
        self.assertEqual(pdf_res["status"], "success")
        self.assertEqual(pdf_res["returned_start_page"], 11)
        self.assertEqual(pdf_res["returned_end_page"], 15)
        self.assertIn("--- Page 11 / 15 ---", pdf_res["content"])
        self.assertIn("--- Page 15 / 15 ---", pdf_res["content"])
        self.assertNotIn("--- Page 1 / 15 ---", pdf_res["content"])
        self.assertIn("lines 11-15", pdf_res["hint"])

        # XLSX range routing
        xlsx_res = self.tools["read_file"](
            project_id="core_proj",
            relative_path="docs/metrics.xlsx",
            start_line=101,
            end_line=150,
        )
        self.assertEqual(xlsx_res["status"], "success")
        self.assertEqual(xlsx_res["returned_start_row"], 101)
        self.assertIn("| M100 | ResponseTime_100 |", xlsx_res["content"])

    def test_docx_page_count_pagination(self) -> None:
        """Test DOCX page_count reads multiple logical pages."""
        res = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/architecture.docx",
            page=1,
            page_count=2,
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["returned_start_page"], 1)
        self.assertIn("# Architecture Specification", res["content"])

    # ========================================================================
    # 6. Security & Edge Case Handling
    # ========================================================================

    def test_sensitive_path_and_security(self) -> None:
        """Verify sensitive paths are blocked by PathSecurityError."""
        with self.assertRaises(PathSecurityError):
            self.tools["read_document_file"](
                project_id="core_proj",
                relative_path=".env.docx",
            )

    def test_missing_and_invalid_paths(self) -> None:
        """Verify missing documents raise FileNotFoundError and unsupported extensions raise FileServiceError."""
        with self.assertRaises(FileNotFoundError):
            self.tools["read_document_file"](
                project_id="core_proj",
                relative_path="docs/non_existent.pdf",
            )

        with self.assertRaises(FileServiceError):
            self.tools["read_document_file"](
                project_id="core_proj",
                relative_path="docs/spec.txt",
            )

    def test_empty_zero_byte_document(self) -> None:
        """Verify empty (0-byte) document returns clean status without unhandled exceptions."""
        empty_doc = self.docs_dir / "empty.pdf"
        empty_doc.write_bytes(b"")

        res = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/empty.pdf",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["total_pages"], 0)
        self.assertEqual(res["content"], "")
        self.assertIn("is empty", res["hint"])

    def test_max_chars_overflow_truncation(self) -> None:
        """Verify max_chars overflow truncates gracefully when auto_paginate=True and raises when False."""
        # 1. auto_paginate=True truncates
        res = self.tools["read_document_file"](
            project_id="core_proj",
            relative_path="docs/spec.pdf",
            max_chars=200,
            auto_paginate=True,
        )
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["truncated"])
        self.assertEqual(res["truncation_reason"], "max_chars_limit_reached")
        self.assertLessEqual(len(res["content"]), 200)

        # 2. auto_paginate=False raises FileSizeLimitError
        with self.assertRaises(FileSizeLimitError):
            self.tools["read_document_file"](
                project_id="core_proj",
                relative_path="docs/spec.pdf",
                max_chars=200,
                auto_paginate=False,
            )


if __name__ == "__main__":
    unittest.main()

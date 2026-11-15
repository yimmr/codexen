"""Binary files, Base64 transfer, and multimodal image inspection tools."""
from __future__ import annotations

from typing import Any

try:
    from mcp.server.fastmcp import Image
except (ImportError, ModuleNotFoundError):
    Image = None  # type: ignore[assignment,misc]

from ..core import (
    normalize_relative_path,
)
from ..core import (
    read_project_binary_file as _read_binary_core,
)
from ..core import (
    write_project_binary_file as _write_binary_core,
)
from ..core.security import assert_not_sensitive_path, ensure_project_accessible, safe_resolve_path
from .decorators import ToolContext


def register_binary_tools(ctx: ToolContext) -> None:
    """Register binary and media inspection tools."""

    @ctx.register_tool()
    def read_binary_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        max_bytes: int = 10_000_000,
    ) -> dict[str, Any]:
        """Read any binary file (image, binary, document, compiled artifact) encoded in Base64 with SHA256 and MIME metadata.

        Args:
            project_id: Project identifier.
            project_name: Alias for project_id.
            relative_path: Path relative to project root.
            path: Alias for relative_path.
            max_bytes: Safety limit in bytes (default: 10MB).
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return _read_binary_core(project=proj, relative_path=target_path, max_bytes=max_bytes)

    ctx.register_alias(
        read_binary_file,
        "read_project_binary_file",
        "Alias for read_binary_file.",
    )

    @ctx.register_tool()
    def write_binary_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
        content_base64: str = "",
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Write or overwrite a binary file using a Base64-encoded payload.

        Args:
            project_id: Project identifier.
            project_name: Alias for project_id.
            relative_path: Path relative to project root.
            path: Alias for relative_path.
            content_base64: Raw file content encoded in standard Base64.
            overwrite: Whether to overwrite existing file (default: True).
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        return _write_binary_core(
            project=proj,
            relative_path=target_path,
            content_base64=content_base64,
            overwrite=overwrite,
        )

    ctx.register_alias(
        write_binary_file,
        "write_project_binary_file",
        "Alias for write_binary_file.",
    )

    @ctx.register_tool()
    def read_media_file(
        project_id: str = "",
        project_name: str = "",
        relative_path: str = "",
        path: str = "",
    ) -> Any:
        """Read an image file (PNG, JPG, JPEG, GIF, WEBP) and return it natively for multimodal LLM vision.

        If the MCP client / LLM supports image modalities, this directly renders the image
        in the chat context without requiring manual Base64 decoding.

        Args:
            project_id: Project identifier.
            project_name: Alias for project_id.
            relative_path: Image path relative to project root.
            path: Alias for relative_path.
        """
        proj = ctx.resolve_project(project_id=project_id, project_name=project_name)
        target_path = normalize_relative_path(relative_path or path)
        assert_not_sensitive_path(target_path, "Reading media")
        ensure_project_accessible(proj)
        file_path = safe_resolve_path(proj.root, target_path)

        if not file_path.exists():
            raise FileNotFoundError(f"Media file not found: '{target_path}' in project '{proj.name}'")
        if file_path.is_dir():
            raise ValueError(f"Target '{target_path}' is a directory, not a media file.")

        suffix = file_path.suffix.lower()
        if Image is not None and suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            return Image(path=file_path)

        return _read_binary_core(project=proj, relative_path=target_path)

    ctx.register_alias(
        read_media_file,
        "read_image_file",
        "Alias for read_media_file.",
    )

"""ZIP archive creation and safe extraction with Zip-Slip / Zip-Bomb defenses."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import zipfile
from typing import Any

from ..config import ProjectConfig
from ..exceptions import (
    FileServiceError,
    FileSizeLimitError,
    ReadOnlyProjectError,
    ZipSlipSecurityError,
)
from .fs import matches_any_pattern, utc_iso_now
from .security import (
    assert_not_sensitive_path,
    ensure_project_accessible,
    is_sensitive_path,
    safe_resolve_path,
)


def create_project_zip(
    project: ProjectConfig,
    source_subpath: str = "",
    output_zip_name: str = "",
    ignored_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """Create a ZIP archive of a project or subfolder, excluding ignored patterns."""
    ensure_project_accessible(project)
    src_target = safe_resolve_path(project.root, source_subpath)

    if not src_target.exists():
        raise FileNotFoundError(f"Path '{source_subpath}' not found in project '{project.name}'.")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = output_zip_name.strip() if output_zip_name else f"{project.name}_{timestamp}.zip"
    if not zip_filename.lower().endswith(".zip"):
        zip_filename += ".zip"

    output_zip_path = safe_resolve_path(project.root, zip_filename)
    if output_zip_path == project.root:
        output_zip_path = project.root / zip_filename

    ignored = ignored_patterns or []
    files_packed = 0

    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if src_target.is_file():
            zf.write(src_target, arcname=src_target.name)
            files_packed = 1
        else:
            for root, dirs, files in os.walk(src_target):
                dirs[:] = [d for d in dirs if not matches_any_pattern(d, ignored)]
                cur_p = Path(root)
                for f in files:
                    arcname = str((cur_p / f).relative_to(src_target)).replace("\\", "/")
                    if matches_any_pattern(f, ignored) or is_sensitive_path(f) or is_sensitive_path(arcname):
                        continue
                    file_full = cur_p / f
                    # Avoid archiving the archive itself
                    if file_full.resolve() == output_zip_path.resolve():
                        continue
                    zf.write(file_full, arcname=arcname)
                    files_packed += 1

    stat = output_zip_path.stat()
    rel = str(output_zip_path.relative_to(project.root)).replace("\\", "/")

    return {
        "status": "success",
        "project": project.name,
        "archive_relative_path": rel,
        "size_bytes": stat.st_size,
        "files_packed": files_packed,
        "created_at": utc_iso_now(),
    }


def extract_project_zip(
    project: ProjectConfig,
    zip_relative_path: str,
    target_subpath: str = "",
    overwrite: bool = True,
    max_size_bytes: int = 100 * 1024 * 1024,
) -> dict[str, Any]:
    """Extract a ZIP archive with strict Zip-Slip and Zip-Bomb defenses."""
    if project.read_only:
        raise ReadOnlyProjectError(
            f"Project '{project.name}' is read-only. Cannot extract archive."
        )

    ensure_project_accessible(project)
    zip_file_path = safe_resolve_path(project.root, zip_relative_path)
    dest_dir = safe_resolve_path(project.root, target_subpath)

    if not zip_file_path.exists():
        raise FileNotFoundError(f"Archive '{zip_relative_path}' not found in project '{project.name}'.")
    if not zip_file_path.is_file():
        raise FileServiceError(f"'{zip_relative_path}' is not a regular file.")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_dir_resolved = dest_dir.resolve()

    extracted_count = 0
    total_uncompressed = 0

    with zipfile.ZipFile(zip_file_path, "r") as zf:
        infolist = zf.infolist()

        # Phase 1: Pre-validation of size and Zip-Slip vulnerability check
        for member in infolist:
            total_uncompressed += member.file_size
            if total_uncompressed > max_size_bytes:
                raise FileSizeLimitError(
                    f"Archive exceeds maximum uncompressed size limit ({max_size_bytes} bytes). Potential zip bomb aborted."
                )

            name = member.filename.replace("\\", "/")
            assert_not_sensitive_path(name, "Extracting from archive")
            # Reject drive letters or absolute paths
            if re.match(r"^[a-zA-Z]:", name) or name.startswith("/"):
                raise ZipSlipSecurityError(
                    f"Malicious zip entry detected: '{member.filename}' specifies an absolute path."
                )

            target_entry_path = (dest_dir_resolved / Path(name)).resolve()
            if dest_dir_resolved != target_entry_path and dest_dir_resolved not in target_entry_path.parents:
                raise ZipSlipSecurityError(
                    f"Zip-Slip exploit attempt detected: '{member.filename}' escapes target destination directory."
                )

        # Phase 2: Safe extraction
        for member in infolist:
            name = member.filename.replace("\\", "/")
            target_entry_path = (dest_dir_resolved / Path(name)).resolve()

            if member.is_dir():
                target_entry_path.mkdir(parents=True, exist_ok=True)
                continue

            target_entry_path.parent.mkdir(parents=True, exist_ok=True)
            if target_entry_path.exists() and not overwrite:
                continue

            with zf.open(member) as source_f, open(target_entry_path, "wb") as target_f:
                shutil.copyfileobj(source_f, target_f)
            extracted_count += 1

    return {
        "status": "success",
        "project": project.name,
        "archive": zip_relative_path,
        "destination": str(dest_dir_resolved.relative_to(project.root)).replace("\\", "/") or ".",
        "extracted_files": extracted_count,
        "total_uncompressed_bytes": total_uncompressed,
    }

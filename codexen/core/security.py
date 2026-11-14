"""Security boundaries, path resolution, and sensitive file isolation."""
from __future__ import annotations

import fnmatch
from pathlib import Path
import re

from ..config import ProjectConfig
from ..exceptions import (
    PathSecurityError,
    ProjectPathInvalidError,
)

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

CRITICAL_SENSITIVE_PATTERNS = [
    # Sensitive credentials & private keys in target projects
    r"^\.env($|\..*)",
    r".*\.pem$",
    r".*\.key$",
    r".*\.pfx$",
    r".*\.p12$",
    r"^id_(rsa|dsa|ecdsa|ed25519)($|\..*)",
    # Version control internal metadata (prevents corruption of .git database via raw writes)
    r"^\.git($|/.*|\\.*)",
    r"^\.svn($|/.*|\\.*)",
    r"^\.hg($|/.*|\\.*)",
]

SAFE_ENV_TEMPLATE_SUFFIXES = (".example", ".template", ".sample", ".dist", ".schema")


def normalize_relative_path(path_str: str) -> str:
    """Normalize relative path input from MCP tools, stripping accidental leading slashes."""
    if not path_str or path_str.strip() in ("", ".", "/", "\\"):
        return ""
    cleaned = path_str.strip().replace("\\", "/")
    # Strip leading slash if not a drive letter (C:) or network share (//)
    if not (re.match(r"^[a-zA-Z]:", cleaned) or cleaned.startswith("//")):
        cleaned = cleaned.lstrip("/")
    return cleaned


def is_windows_reserved(name: str) -> bool:
    """Check whether a filename or path segment matches Windows reserved device names."""
    base = name.split(".")[0].upper()
    return base in WINDOWS_RESERVED_NAMES


def safe_resolve_path(root: Path, relative_path: str) -> Path:
    """Resolve a relative path against the project root with strict security guarantees.

    Guarantees:
    - Rejects path traversal attempts (e.g. '..', 'dir/../../etc')
    - Rejects absolute paths, UNC network paths, Windows drive prefixes, and leading slashes (e.g. 'C:', '\\server', '/etc')
    - Rejects Windows reserved device names (CON, NUL, PRN, etc.)
    - Ensures the final resolved path is either the root directory itself or strictly inside it.
    """
    root_resolved = root.resolve()

    if not relative_path or relative_path.strip() in ("", "."):
        return root_resolved

    cleaned = relative_path.strip().replace("\\", "/")

    # Check for drive letter or UNC prefix
    if re.match(r"^[a-zA-Z]:", cleaned) or cleaned.startswith("//") or cleaned.startswith("\\\\"):
        raise PathSecurityError(
            f"Absolute paths, drive letters, and UNC shares are forbidden: '{relative_path}'"
        )

    # Check for leading slashes (absolute paths on root)
    if cleaned.startswith("/"):
        raise PathSecurityError(
            f"Leading slashes / root-level paths are forbidden: '{relative_path}'. Please provide a relative path."
        )

    # Inspect each segment before combining
    segments = [s for s in cleaned.split("/") if s and s != "."]
    for seg in segments:
        if is_windows_reserved(seg):
            raise PathSecurityError(f"Path segment '{seg}' is a reserved Windows system name.")

    # Resolve combined path
    candidate = (root_resolved / Path(*segments)).resolve()

    # Enforce boundary check
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PathSecurityError(
            f"Security violation: path '{relative_path}' escapes project root boundary."
        )

    # Engine self-isolation: reject any access touching the Codexen tool directory
    codexen_root = Path(__file__).resolve().parents[2].resolve()
    if candidate == codexen_root or codexen_root in candidate.parents:
        raise PathSecurityError(
            "Security violation: access to Codexen engine directory is strictly prohibited."
        )

    return candidate


def is_sensitive_path(relative_path: str) -> bool:
    """Check if relative path matches critical sensitive/secret files."""
    if not relative_path:
        return False
    cleaned = relative_path.strip().replace("\\", "/").lstrip("/")
    name = Path(cleaned).name
    # Explicitly permit developer template/example files (e.g. .env.example, .env.template)
    name_lower = name.lower()
    if name_lower.startswith(".env") and any(name_lower.endswith(s) for s in SAFE_ENV_TEMPLATE_SUFFIXES):
        return False

    for pattern in CRITICAL_SENSITIVE_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE) or re.search(pattern, name, re.IGNORECASE):
            return True
    return False


def assert_not_sensitive_path(relative_path: str, action: str = "access") -> None:
    """Enforce strict blockage of sensitive/secret files across all file operations."""
    if is_sensitive_path(relative_path):
        raise PathSecurityError(
            f"Security violation: {action} is forbidden on protected sensitive file/directory '{relative_path}'."
        )


def ensure_project_accessible(project: ProjectConfig) -> None:
    """Validate that the mapped project root exists and is a directory."""
    if not project.root.exists():
        raise ProjectPathInvalidError(
            f"Project '{project.name}' root directory does not exist on host: {project.root}"
        )
    if not project.root.is_dir():
        raise ProjectPathInvalidError(
            f"Project '{project.name}' root path is not a directory: {project.root}"
        )

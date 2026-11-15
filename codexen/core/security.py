"""Security boundaries, path resolution, and sensitive file isolation."""
from __future__ import annotations

import os
import re
from pathlib import Path

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
    r"(^|.*/)oauth_store\.db$",
    # Cloud credentials & developer deployment tokens
    r"(^|.*/)\.aws/(credentials|config)$",
    r"(^|.*/)gcloud/.*\.json$",
    r"(^|.*/)wrangler\.toml$",
    # Package manager tokens and registry configs
    r"(^|.*/)\.npmrc$",
    r"(^|.*/)\.pypirc$",
    r"(^|.*/)\.docker/config\.json$",
    r"(^|.*/)\.cargo/credentials(\.toml)?$",
    # Local secrets and master keys
    r"(^|.*/)master\.key$",
    r".*\.kdbx$",
    r"(^|.*/)secrets\.(ya?ml|json)$",
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
    - Canonicalizes paths (os.path.realpath) to neutralize Windows 8.3 short filename alias bypasses
    - Ensures the final resolved path is either the root directory itself or strictly inside it.
    """
    root_resolved = Path(os.path.realpath(root)).resolve()

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
    # Resolve realpath if file/dir already exists on filesystem to fold 8.3 aliases and symlinks
    if candidate.exists():
        candidate_real = Path(os.path.realpath(candidate)).resolve()
    else:
        # For non-existent candidate, resolve its nearest existing parent with realpath
        curr = candidate
        tail_parts: list[str] = []
        while not curr.exists() and curr != curr.parent:
            tail_parts.append(curr.name)
            curr = curr.parent
        if curr.exists():
            curr_real = Path(os.path.realpath(curr)).resolve()
            candidate_real = (curr_real / Path(*reversed(tail_parts))).resolve()
        else:
            candidate_real = candidate

    # Enforce boundary check
    if candidate_real != root_resolved and root_resolved not in candidate_real.parents:
        raise PathSecurityError(
            f"Security violation: path '{relative_path}' escapes project root boundary."
        )

    # Engine self-isolation: reject any access touching the Codexen tool directory
    codexen_root = Path(os.path.realpath(Path(__file__).resolve().parents[2])).resolve()
    if candidate_real == codexen_root or codexen_root in candidate_real.parents:
        raise PathSecurityError(
            "Security violation: access to Codexen engine directory is strictly prohibited."
        )

    return candidate_real


def is_sensitive_path(relative_path: str, custom_patterns: list[str] | None = None) -> bool:
    """Check if relative path matches critical sensitive/secret files."""
    if not relative_path:
        return False
    cleaned = relative_path.strip().replace("\\", "/").lstrip("/")
    name = Path(cleaned).name
    name_lower = name.lower()
    # Explicitly permit developer template/example files (e.g. .env.example, .env.template),
    # but only when they are the final filename and no sensitive ancestor exists.
    name_is_template = name_lower.startswith(".env") and any(
        name_lower.endswith(s) for s in SAFE_ENV_TEMPLATE_SUFFIXES
    )

    # Protect .env files/directories in ANY path segment. Checking only the basename
    # would allow e.g. ".env/secret.txt" to bypass the credential guard when .env is
    # a directory; the template whitelist above only applies to the final filename.
    parts = [part for part in cleaned.split("/") if part]
    for idx, part in enumerate(parts):
        part_lower = part.lower()
        if part_lower == ".env" or part_lower.startswith(".env."):
            if idx == len(parts) - 1 and name_is_template:
                continue
            return True

    if name_is_template:
        return False

    all_patterns = list(CRITICAL_SENSITIVE_PATTERNS)
    if custom_patterns:
        all_patterns.extend(custom_patterns)

    for pattern in all_patterns:
        if re.search(pattern, cleaned, re.IGNORECASE) or re.search(pattern, name, re.IGNORECASE):
            return True
    return False


def assert_not_sensitive_path(
    relative_path: str, action: str = "access", custom_patterns: list[str] | None = None
) -> None:
    """Enforce strict blockage of sensitive/secret files across all file operations."""
    if is_sensitive_path(relative_path, custom_patterns=custom_patterns):
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

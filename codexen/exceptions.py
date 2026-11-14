"""Custom exceptions for the Project File MCP Server."""
from __future__ import annotations


class FileServiceError(Exception):
    """Base exception for all file service errors."""
    pass


class ProjectNotFoundError(FileServiceError):
    """Raised when a specified project cannot be found in configuration."""
    pass


class ProjectPathInvalidError(FileServiceError):
    """Raised when a project's configured root directory does not exist or is invalid."""
    pass


class PathSecurityError(FileServiceError):
    """Raised when a requested path attempts to escape the project's root boundary."""
    pass


class ZipSlipSecurityError(PathSecurityError):
    """Raised when a zip entry attempts to escape the target directory (Zip-Slip attack)."""
    pass


class ReadOnlyProjectError(FileServiceError):
    """Raised when a write/edit/delete operation is attempted on a read-only project."""
    pass


class FileSizeLimitError(FileServiceError):
    """Raised when a file exceeds the maximum allowed characters/size for reading."""
    pass


class ConfigurationError(FileServiceError):
    """Raised when the configuration file is missing, unreadable, or invalid."""
    pass


class CommandSecurityError(FileServiceError):
    """Raised when a terminal command violates security blacklist or cwd constraints."""
    pass


class CommandExecutionError(FileServiceError):
    """Raised when a terminal command fails or times out."""
    pass

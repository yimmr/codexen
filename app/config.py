"""Configuration manager for Project File MCP Server."""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml

from mcp_framework.env import (
    DeploymentEnvConfig,
    get_runtime_dir,
    update_env_variable,
)
from .exceptions import ConfigurationError


# Backward-compatibility alias
HttpSettings = DeploymentEnvConfig


@dataclass
class ProjectConfig:
    """Configuration definition for a single mapped project."""
    name: str
    root: Path
    description: str = ""
    read_only: bool = False

    def exists(self) -> bool:
        """Check if the mapped root directory actually exists on the filesystem."""
        return self.root.exists() and self.root.is_dir()

    @property
    def id(self) -> str:
        return self.name

    def to_dict(self, expose_root: bool = True) -> dict[str, Any]:
        """Convert project config to dictionary representation."""
        data = {
            "project_id": self.name,
            "name": self.name,
            "description": self.description,
            "read_only": self.read_only,
            "exists": self.exists(),
        }
        if expose_root:
            data["root"] = str(self.root)
        return data


@dataclass
class ServiceSettings:
    """Global service settings and permissions."""
    max_read_chars: int = 1_000_000
    max_list_entries: int = 1_000
    max_search_results: int = 200
    max_zip_size_bytes: int = 100 * 1024 * 1024  # 100MB
    default_encoding: str = "utf-8"
    allow_delete: bool = True
    allow_copy_path: bool = True
    allow_zip_operations: bool = True
    allow_command_execution: bool = True
    allow_create_project: bool = True
    command_timeout_seconds: int = 60
    max_command_timeout_seconds: int = 300
    max_command_output_chars: int = 50_000
    command_whitelist: list[str] = field(
        default_factory=lambda: [
            "git", "pytest", "python", "py", "node", "npm", "pnpm", "yarn",
            "cargo", "rustc", "go", "make", "ruff", "flake8", "tsc",
            "pip", "poetry", "uv", "dir", "echo", "type", "cat", "findstr", "grep",
        ]
    )
    command_blacklist: list[str] = field(
        default_factory=lambda: [
            "format", "diskpart", "mklink", "reg", "regedit",
            "shutdown", "bcdedit", "vssadmin", "cipher", "takeown",
        ]
    )
    command_require_confirmation: list[str] = field(
        default_factory=lambda: [
            "rmdir", "del", "rm", "git push", "git reset --hard",
            "git clean", "npm publish", "pip install --upgrade",
        ]
    )
    ignored_patterns: list[str] = field(
        default_factory=lambda: [
            ".git",
            ".svn",
            ".hg",
            "__pycache__",
            ".venv",
            "node_modules",
            ".idea",
            ".vscode",
            "*.pyc",
        ]
    )


class ConfigManager:
    """Loads, validates, and manages project mappings and service settings."""

    def __init__(self, config_path: str | Path | None = None, base_dir: Path | None = None) -> None:
        self.base_dir = (base_dir or Path(__file__).resolve().parents[1]).resolve()
        self.config_path = self._resolve_config_path(config_path)
        self.projects: dict[str, ProjectConfig] = {}
        self.settings = ServiceSettings()
        self.http_settings = HttpSettings()
        self.workspace_root = Path("D:/works")
        self.load()

    def _resolve_config_path(self, config_path: str | Path | None) -> Path:
        """Resolve the absolute path to the configuration file (config.yaml)."""
        if config_path:
            p = Path(config_path)
            if not p.is_absolute():
                p = (self.base_dir / p).resolve()
            return p

        env_path = os.getenv("PROJECTS_CONFIG_PATH") or os.getenv("SETTINGS_PATH")
        if env_path:
            p = Path(env_path)
            if not p.is_absolute():
                p = (self.base_dir / p).resolve()
            return p

        # Check config.yaml (root primary default)
        root_config = (self.base_dir / "config.yaml").resolve()
        if root_config.exists():
            return root_config

        # Check config/settings.yaml or settings.yaml (legacy fallbacks)
        legacy_config = (self.base_dir / "config" / "settings.yaml").resolve()
        if legacy_config.exists():
            return legacy_config

        fallback_settings = (self.base_dir / "settings.yaml").resolve()
        if fallback_settings.exists():
            return fallback_settings

        return root_config

    def load(self) -> ConfigManager:
        """Load and parse the YAML configuration file."""
        if not self.config_path.exists():
            raise ConfigurationError(f"Configuration file not found at: {self.config_path}")

        try:
            content = self.config_path.read_text(encoding="utf-8")
            raw_data = yaml.safe_load(content)
        except Exception as e:
            raise ConfigurationError(f"Failed to read/parse YAML configuration at {self.config_path}: {e}") from e

        if not isinstance(raw_data, dict):
            raise ConfigurationError(f"Configuration root must be a YAML mapping (dict), got {type(raw_data).__name__}")

        # Parse workspace settings
        raw_ws = raw_data.get("workspace", {}) if isinstance(raw_data.get("workspace"), dict) else {}
        ws_root_str = raw_ws.get("root", os.getenv("WORKSPACE_ROOT", "D:/works"))
        self.workspace_root = Path(ws_root_str).resolve()

        codexen_real_root = Path(__file__).resolve().parents[1].resolve()
        if (
            self.workspace_root == self.base_dir
            or self.workspace_root == codexen_real_root
            or codexen_real_root in self.workspace_root.parents
            or self.workspace_root in codexen_real_root.parents
        ):
            raise ConfigurationError(
                f"Security boundary violation: Workspace root '{self.workspace_root}' cannot overlap with "
                f"Codexen engine directory."
            )

        raw_mcp = raw_data.get("mcp", {}) if isinstance(raw_data.get("mcp"), dict) else {}
        raw_mcp_http = raw_mcp.get("http", {}) if isinstance(raw_mcp.get("http"), dict) else {}
        raw_mcp_sec = raw_mcp.get("security", {}) if isinstance(raw_mcp.get("security"), dict) else {}
        raw_mcp_perm = raw_mcp.get("permissions", {}) if isinstance(raw_mcp.get("permissions"), dict) else {}

        raw_settings = raw_data.get("settings", {}) if isinstance(raw_data.get("settings"), dict) else {}
        raw_perm = raw_data.get("permissions", {}) if isinstance(raw_data.get("permissions"), dict) else {}
        merged_settings = {**raw_settings, **raw_mcp_perm, **raw_perm}

        self.settings = ServiceSettings(
            max_read_chars=int(merged_settings.get("max_read_chars", 1_000_000)),
            max_list_entries=int(merged_settings.get("max_list_entries", 1_000)),
            max_search_results=int(merged_settings.get("max_search_results", 200)),
            max_zip_size_bytes=int(merged_settings.get("max_zip_size_bytes", 100 * 1024 * 1024)),
            default_encoding=str(merged_settings.get("default_encoding", "utf-8")),
            allow_delete=bool(merged_settings.get("allow_delete", merged_settings.get("allow_file_delete", True))),
            allow_copy_path=bool(merged_settings.get("allow_copy_path", True)),
            allow_zip_operations=bool(merged_settings.get("allow_zip_operations", True)),
            allow_command_execution=bool(merged_settings.get("allow_command_execution", True)),
            allow_create_project=bool(merged_settings.get("allow_create_project", raw_ws.get("allow_create_project", True))),
            command_timeout_seconds=int(merged_settings.get("command_timeout_seconds", 60)),
            max_command_timeout_seconds=int(merged_settings.get("max_command_timeout_seconds", 300)),
            max_command_output_chars=int(merged_settings.get("max_command_output_chars", 50_000)),
            command_whitelist=list(merged_settings.get("command_whitelist", self.settings.command_whitelist)),
            command_blacklist=list(merged_settings.get("command_blacklist", self.settings.command_blacklist)),
            command_require_confirmation=list(
                merged_settings.get("command_require_confirmation", self.settings.command_require_confirmation)
            ),
            ignored_patterns=list(merged_settings.get("ignored_patterns", self.settings.ignored_patterns)),
        )

        # Parse infrastructure & deployment environment settings (from .env with optional overrides)
        raw_http = raw_data.get("http", {}) if isinstance(raw_data.get("http"), dict) else {}
        merged_http = {**raw_http, **raw_mcp_http}
        merged_sec = {**raw_data.get("security", {}), **raw_mcp_sec}
        overrides = {**merged_http, **merged_sec}

        self.http_settings = DeploymentEnvConfig.load(self.base_dir, overrides=overrides)

        # Parse projects
        raw_projects = raw_data.get("projects", {})
        if raw_projects is None:
            raw_projects = {}
        elif not isinstance(raw_projects, dict):
            raise ConfigurationError(f"'projects' in configuration must be a mapping of project_name -> details")

        new_projects: dict[str, ProjectConfig] = {}
        for name, item in raw_projects.items():
            if not isinstance(item, dict):
                continue
            raw_root = item.get("root")
            if not raw_root:
                continue

            root_path = Path(raw_root)
            if not root_path.is_absolute():
                root_path = (self.base_dir / root_path).resolve()
            else:
                root_path = root_path.resolve()

            # Security boundary: Codexen's own engine directory can never be exposed as a target project
            if (
                root_path == self.base_dir
                or root_path == codexen_real_root
                or codexen_real_root in root_path.parents
                or root_path in codexen_real_root.parents
            ):
                raise ConfigurationError(
                    f"Security boundary violation: Project '{name}' root '{root_path}' cannot overlap with "
                    f"Codexen engine directory. Codexen is the bridge service tool and "
                    f"must never be exposed as a target project."
                )

            desc = str(item.get("description", ""))
            read_only = bool(item.get("read_only", False))

            new_projects[str(name)] = ProjectConfig(
                name=str(name),
                root=root_path,
                description=desc,
                read_only=read_only,
            )

        self.projects = new_projects
        return self

    @property
    def server(self) -> HttpSettings:
        return self.http_settings

    @property
    def security(self) -> HttpSettings:
        return self.http_settings

    @property
    def tunnel(self) -> HttpSettings:
        return self.http_settings

    def reload(self) -> dict[str, Any]:
        """Reload configuration from disk and return summary."""
        self.load()
        return {
            "status": "success",
            "config_file": str(self.config_path),
            "workspace_root": str(self.workspace_root),
            "projects_count": len(self.projects),
            "projects": [p.to_dict() for p in self.projects.values()],
        }

    def get_project(self, name: str) -> ProjectConfig:
        """Get project configuration by name. Only registered projects can be accessed."""
        if name in self.projects:
            return self.projects[name]

        available = list(self.projects.keys())
        raise ConfigurationError(
            f"Project '{name}' is not configured. Available projects: {available}"
        )

    def create_and_register_project(
        self,
        project_id: str,
        description: str = "",
        read_only: bool = False,
        persist: bool = True,
    ) -> ProjectConfig:
        """Create a new project directory under workspace_root and register it immediately.

        Args:
            project_id: Unique identifier for the project (alphanumeric, -, _).
            description: Human-readable project description.
            read_only: Whether the project is read-only.
            persist: Whether to write the new project back to the YAML config file.
        """
        if not self.settings.allow_create_project:
            raise ConfigurationError("Project creation is disabled by configuration.")

        cleaned_id = project_id.strip()
        if not re.match(r"^[a-zA-Z0-9_-]+$", cleaned_id):
            raise ValueError(
                f"Invalid project_id '{project_id}'. Must contain only alphanumeric characters, underscores, and dashes."
            )

        if cleaned_id in self.projects:
            existing = self.projects[cleaned_id]
            return existing

        target_dir = (self.workspace_root / cleaned_id).resolve()
        codexen_real_root = Path(__file__).resolve().parents[1].resolve()
        if (
            target_dir == self.base_dir
            or target_dir == codexen_real_root
            or codexen_real_root in target_dir.parents
            or target_dir in codexen_real_root.parents
        ):
            raise ConfigurationError(
                f"Security boundary violation: Cannot create project overlapping with Codexen engine directory."
            )
        target_dir.mkdir(parents=True, exist_ok=True)

        new_proj = ProjectConfig(
            name=cleaned_id,
            root=target_dir,
            description=description or f"Project {cleaned_id} under workspace {self.workspace_root.name}",
            read_only=read_only,
        )
        self.projects[cleaned_id] = new_proj

        if persist and self.config_path.exists():
            try:
                self._persist_project_to_yaml(new_proj)
            except Exception as e:
                # Log or tolerate persistence error if in-memory registration succeeded
                pass

        return new_proj

    def register_existing_project(
        self,
        folder_path: str | Path,
        name: str | None = None,
        description: str = "",
        read_only: bool = False,
        persist: bool = True,
    ) -> ProjectConfig:
        """Register an existing directory as an active project with duplicate checks."""
        folder = Path(folder_path).resolve()
        if not folder.exists() or not folder.is_dir():
            raise FileNotFoundError(f"Target directory does not exist or is not a directory: {folder}")

        proj_name = (name or folder.name).strip()
        if not proj_name:
            raise ValueError("Project name cannot be empty.")

        codexen_real_root = Path(__file__).resolve().parents[1].resolve()
        if (
            folder == self.base_dir
            or folder == codexen_real_root
            or codexen_real_root in folder.parents
            or folder in codexen_real_root.parents
        ):
            raise ConfigurationError(
                f"Security boundary violation: Folder '{folder}' cannot overlap with Codexen engine directory."
            )

        # Check duplicate name
        if proj_name in self.projects:
            existing = self.projects[proj_name]
            raise ValueError(
                f"Project name '{proj_name}' is already registered (points to '{existing.root}')."
            )

        # Check duplicate path
        for p in self.projects.values():
            if p.root.resolve() == folder:
                raise ValueError(
                    f"Directory '{folder}' is already registered under project name '{p.name}'."
                )

        new_proj = ProjectConfig(
            name=proj_name,
            root=folder,
            description=description or f"Project {proj_name}",
            read_only=read_only,
        )
        self.projects[proj_name] = new_proj

        if persist and self.config_path.exists():
            self._persist_project_to_yaml(new_proj)

        return new_proj

    def unregister_project(self, name: str, persist: bool = True) -> ProjectConfig:
        """Unregister an existing project from active projects without deleting physical files.

        Args:
            name: Project name to unregister.
            persist: Whether to remove the entry from config.yaml.

        Returns:
            ProjectConfig of the unregistered project.

        Raises:
            ConfigurationError: If the project is not registered.
        """
        proj_name = name.strip()
        if proj_name not in self.projects:
            available = list(self.projects.keys())
            raise ConfigurationError(
                f"Project '{proj_name}' is not configured. Available projects: {available}"
            )

        removed_proj = self.projects.pop(proj_name)

        if persist and self.config_path.exists():
            self._remove_project_from_yaml(proj_name)

        return removed_proj

    def _persist_project_to_yaml(self, project: ProjectConfig) -> None:
        """Persist a single project entry into config.yaml using text-based insertion.

        Avoids ruamel.yaml comment-reordering bugs by locating the end of the
        `projects:` block as raw text and inserting before the first top-level
        line (a line starting at column 0 that is not blank and not indented).
        All existing comments are preserved verbatim.
        """
        import re

        raw = self.config_path.read_bytes()
        # Detect line ending (CRLF / LF)
        eol = b"\r\n" if b"\r\n" in raw else b"\n"
        content = raw.decode("utf-8")
        sep = eol.decode("utf-8")

        # Build the new YAML entry (2-space indent matching projects: block)
        root_str = str(project.root).replace("\\", "\\\\")
        desc_str = project.description.replace('"', '\\"')
        read_only_str = "true" if project.read_only else "false"
        new_entry = (
            f"  {project.name}:{sep}"
            f'    root: "{root_str}"{sep}'
            f'    description: "{desc_str}"{sep}'
            f"    read_only: {read_only_str}{sep}"
        )

        # Find the projects: key
        projects_match = re.search(r"^projects\s*:", content, re.MULTILINE)
        if not projects_match:
            # No projects: section — append at end
            content = content.rstrip() + sep + "projects:" + sep + new_entry
            self.config_path.write_bytes(content.encode("utf-8"))
            return

        after_projects = content[projects_match.end():]

        # Find the first line that starts at column 0 and is non-blank
        # (i.e. a top-level comment "# ..." or a top-level key "word:")
        top_level_match = re.search(r"^(?![ \t])(\S)", after_projects, re.MULTILINE)

        if top_level_match:
            insert_abs = projects_match.end() + top_level_match.start()
            # Strip trailing whitespace/blank-lines from the prefix so that
            # the new entry sits flush after the last project, then add one
            # blank line before the section separator comment.
            prefix = content[:insert_abs].rstrip()
            suffix = content[insert_abs:]
            content = prefix + sep + new_entry + sep + suffix
        else:
            # projects: block goes to EOF -- just append
            content = content.rstrip() + sep + new_entry

        self.config_path.write_bytes(content.encode("utf-8"))

    def _remove_project_from_yaml(self, project_name: str) -> None:
        """Remove a single project entry from config.yaml while preserving all comments."""
        import re

        raw = self.config_path.read_bytes()
        eol = b"\x0d\x0a" if b"\x0d\x0a" in raw else b"\x0a"
        content = raw.decode("utf-8")
        sep = eol.decode("utf-8")

        lines = content.splitlines(keepends=True)
        out_lines: list[str] = []
        in_projects = False
        skip_current = False

        for line in lines:
            if re.match(r"^projects\s*:", line):
                in_projects = True
                out_lines.append(line)
                continue

            if in_projects:
                # Check if line is at column 0 and non-blank (top-level key or comment)
                if re.match(r"^(?!\s)", line) and line.strip():
                    in_projects = False
                    skip_current = False
                    if out_lines and out_lines[-1].strip():
                        out_lines.append(sep)
                    out_lines.append(line)
                    continue

                # Check if project starts (2 spaces indent)
                m = re.match(r"^  (\S[^:]*)\s*:", line)
                if m:
                    p_name = m.group(1).strip()
                    if p_name == project_name:
                        skip_current = True
                        continue
                    else:
                        skip_current = False
                        out_lines.append(line)
                        continue

                if skip_current:
                    if re.match(r"^\s{3,}", line):
                        continue
                    elif not line.strip():
                        continue
                    else:
                        skip_current = False
                        out_lines.append(line)
                        continue

            out_lines.append(line)

        self.config_path.write_bytes("".join(out_lines).encode("utf-8"))


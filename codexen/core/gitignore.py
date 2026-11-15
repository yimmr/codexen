"""Dynamic .gitignore engine supporting RFC 0001 multi-level rule precedence and caching."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import re
from typing import Any

from ..config import ProjectConfig
from .security import is_sensitive_path

# Baseline system-level exclusions that must never be traversed or exposed (Level 4)
BASELINE_SYSTEM_IGNORED: list[str] = [
    ".git",
    ".svn",
    ".hg",
    ".bzr",
]

# Baseline patterns for projects that do NOT have a .gitignore (Level 3 Fallback)
BASELINE_FALLBACK_IGNORED: list[str] = [
    ".git", ".svn", ".hg", ".bzr",
    "target", "dist", "build", "out", "bin", "obj", ".next", ".nuxt", ".output",
    "node_modules", ".pnpm", ".pnpm-store", "bower_components", "vendor",
    ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".cache",
    ".tmp", "tmp", "temp",
    ".idea", ".vscode",
    "*.pyc", "*.pyo", "*.pyd", "*.exe", "*.dll", "*.so", "*.dylib", "*.pdb", "*.bin",
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "Cargo.lock",
    "*.map", "*.min.js", "*.min.css",
]


class GitIgnoreRule:
    """A single parsed rule from a .gitignore file."""

    def __init__(
        self,
        raw_pattern: str,
        base_dir: str = "",
        is_negation: bool = False,
        dir_only: bool = False,
        anchored: bool = False,
        regex: re.Pattern | None = None,
    ) -> None:
        self.raw_pattern = raw_pattern
        self.base_dir = base_dir.replace("\\", "/").strip("/")
        self.is_negation = is_negation
        self.dir_only = dir_only
        self.anchored = anchored
        self.regex = regex or self._compile()

    def _compile(self) -> re.Pattern:
        pattern = self.raw_pattern

        if pattern.startswith("**/"):
            pattern = pattern[3:]
            anchored = False
        else:
            anchored = self.anchored

        res: list[str] = []
        i = 0
        n = len(pattern)
        while i < n:
            if pattern[i : i + 4] == "/**/":
                res.append("(?:/|/.+/)")
                i += 4
            elif pattern[i : i + 3] == "/**":
                res.append("/.*")
                i += 3
            elif pattern[i : i + 2] == "**":
                res.append(".*")
                i += 2
            elif pattern[i] == "*":
                res.append("[^/]*")
                i += 1
            elif pattern[i] == "?":
                res.append("[^/]")
                i += 1
            elif pattern[i] == "[":
                j = i + 1
                if j < n and pattern[j] == "!":
                    j += 1
                if j < n and pattern[j] == "]":
                    j += 1
                while j < n and pattern[j] != "]":
                    j += 1
                if j >= n:
                    res.append(r"\[")
                    i += 1
                else:
                    sub = pattern[i + 1 : j]
                    i = j + 1
                    if sub.startswith("!"):
                        sub = "^" + sub[1:]
                    elif sub.startswith("^"):
                        sub = r"\^" + sub[1:]
                    res.append("(?:(?!/)" + "[" + sub + "])")
            elif pattern[i] in r".^$+{}()|\\":
                res.append("\\" + pattern[i])
                i += 1
            else:
                res.append(pattern[i])
                i += 1

        pattern_regex = "".join(res)

        prefix = ""
        if self.base_dir:
            prefix = re.escape(self.base_dir) + "/"

        if anchored:
            full_regex = f"^{prefix}{pattern_regex}(?:/.*)?$"
        else:
            if prefix:
                full_regex = f"^{prefix}(?:.+/)?{pattern_regex}(?:/.*)?$"
            else:
                full_regex = f"(?:^|/){pattern_regex}(?:/.*)?$"

        return re.compile(full_regex)

    def matches(self, rel_path: str, is_dir: bool = False) -> bool:
        """Check if relative path matches this rule."""
        norm_path = rel_path.replace("\\", "/").strip("/")
        if self.dir_only and not is_dir:
            return False
        return bool(self.regex.search(norm_path))


class GitIgnoreFile:
    """Represents a loaded .gitignore file with automatic mtime invalidation."""

    def __init__(self, file_path: Path, base_dir: str = "") -> None:
        self.file_path = file_path
        self.base_dir = base_dir.replace("\\", "/").strip("/")
        self.mtime: float = -1.0
        self.rules: list[GitIgnoreRule] = []
        self.load()

    def is_stale(self) -> bool:
        """Check whether the file on disk was modified or deleted."""
        try:
            current_mtime = self.file_path.stat().st_mtime
            return current_mtime != self.mtime
        except OSError:
            return True

    def load(self) -> None:
        """Load and parse the .gitignore rules."""
        try:
            stat = self.file_path.stat()
            self.mtime = stat.st_mtime
            content = self.file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self.mtime = -1.0
            self.rules = []
            return

        parsed_rules: list[GitIgnoreRule] = []
        for raw_line in content.splitlines():
            rule = self._parse_line(raw_line)
            if rule:
                parsed_rules.append(rule)
        self.rules = parsed_rules

    def _parse_line(self, raw_line: str) -> GitIgnoreRule | None:
        line = raw_line.rstrip("\r\n")

        # Strip unescaped trailing whitespace
        m = re.search(r"(?<!\\)\s+$", line)
        if m:
            line = line[: m.start()]
        line = line.replace(r"\ ", " ")

        if not line:
            return None

        # Comments
        if line.startswith(r"\#"):
            line = line[1:]
        elif line.startswith("#"):
            return None

        # Negation
        is_negation = False
        if line.startswith(r"\!"):
            line = line[1:]
        elif line.startswith("!"):
            is_negation = True
            line = line[1:]

        if not line:
            return None

        # Directory-only
        dir_only = False
        if line.endswith("/"):
            dir_only = True
            line = line[:-1]

        # Anchoring
        if line.startswith("/"):
            line = line[1:]
            anchored = True
        elif "/" in line:
            anchored = True
        else:
            anchored = False

        if not line:
            return None

        return GitIgnoreRule(
            raw_pattern=line,
            base_dir=self.base_dir,
            is_negation=is_negation,
            dir_only=dir_only,
            anchored=anchored,
        )

    def evaluate(self, rel_path: str, is_dir: bool = False) -> bool | None:
        """Evaluate path against all rules in order. Return True=ignored, False=negated, None=no match."""
        norm_path = rel_path.replace("\\", "/").strip("/")
        if self.base_dir:
            if not (norm_path == self.base_dir or norm_path.startswith(self.base_dir + "/")):
                return None

        last_match: bool | None = None
        for rule in self.rules:
            if rule.matches(norm_path, is_dir=is_dir):
                last_match = not rule.is_negation
        return last_match


class ProjectIgnoreManager:
    """Manages ignore rules for a project following RFC 0001 four-level precedence:

    Level 1: Project-level .gitignore (root & subdirectories, highest authority)
    Level 2: config.yaml project-specific ignored_patterns (projects.<id>.ignored_patterns)
    Level 3: Global permissions.ignored_patterns (ONLY active as fallback when Level 1 and Level 2 are absent)
    Level 4: Baseline system & security exclusions (.git, credentials, etc.)
    """

    def __init__(
        self,
        project: ProjectConfig,
        fallback_ignored_patterns: list[str] | None = None,
    ) -> None:
        self.project = project
        self.fallback_ignored_patterns = list(fallback_ignored_patterns or BASELINE_FALLBACK_IGNORED)
        self._gitignore_cache: dict[str, GitIgnoreFile] = {}

    def has_project_gitignore(self) -> bool:
        """Check if project root has a valid .gitignore file."""
        root_gi = self.project.root / ".gitignore"
        return root_gi.is_file()

    def get_or_load_gitignore(self, base_dir: str = "") -> GitIgnoreFile | None:
        """Get cached GitIgnoreFile for base_dir or load it if fresh/modified."""
        norm_base = base_dir.replace("\\", "/").strip("/")
        gi_path = (self.project.root / norm_base / ".gitignore") if norm_base else (self.project.root / ".gitignore")

        cached = self._gitignore_cache.get(norm_base)
        if cached is not None:
            if cached.is_stale():
                if gi_path.is_file():
                    cached.load()
                else:
                    del self._gitignore_cache[norm_base]
                    return None
            return cached

        if gi_path.is_file():
            loaded = GitIgnoreFile(gi_path, base_dir=norm_base)
            self._gitignore_cache[norm_base] = loaded
            return loaded

        return None

    def evaluate_gitignore(self, rel_path: str, is_dir: bool = False) -> bool | None:
        """Evaluate path against applicable .gitignore files from deepest subfolder up to root."""
        norm_path = rel_path.replace("\\", "/").strip("/")
        parts = norm_path.split("/") if norm_path else []

        candidate_dirs: list[str] = []
        if is_dir:
            for idx in range(len(parts), 0, -1):
                candidate_dirs.append("/".join(parts[:idx]))
        else:
            for idx in range(len(parts) - 1, 0, -1):
                candidate_dirs.append("/".join(parts[:idx]))
        candidate_dirs.append("")  # project root

        for bdir in candidate_dirs:
            gi_file = self.get_or_load_gitignore(bdir)
            if gi_file is not None:
                res = gi_file.evaluate(norm_path, is_dir=is_dir)
                if res is not None:
                    return res

        return None

    def is_ignored(
        self,
        rel_path: str,
        is_dir: bool = False,
        caller_ignored: list[str] | None = None,
        check_parents: bool = True,
    ) -> bool:
        """Determine whether rel_path should be ignored according to RFC 0001 rules."""
        norm_path = rel_path.replace("\\", "/").strip("/")
        if not norm_path or norm_path == ".":
            return False

        basename = norm_path.rsplit("/", 1)[-1]

        # Level 4: Baseline system exclusions & sensitive credentials
        if any(part in BASELINE_SYSTEM_IGNORED for part in norm_path.split("/")):
            return True
        if is_sensitive_path(norm_path) or is_sensitive_path(basename):
            return True

        # Caller-specified patterns (explicitly passed by user/agent)
        if caller_ignored:
            if any(fnmatch.fnmatch(basename, p) or fnmatch.fnmatch(norm_path, p) for p in caller_ignored):
                return True

        # Level 1: Project .gitignore
        if self.has_project_gitignore():
            # Check ancestor directories if check_parents=True
            if check_parents and "/" in norm_path:
                parts = norm_path.split("/")
                for i in range(1, len(parts)):
                    ancestor = "/".join(parts[:i])
                    ans_res = self.evaluate_gitignore(ancestor, is_dir=True)
                    if ans_res is True:
                        return True

            gi_res = self.evaluate_gitignore(norm_path, is_dir=is_dir)
            if gi_res is not None:
                return gi_res

            # Level 2: Project-specific ignored_patterns in config.yaml
            proj_patterns = getattr(self.project, "ignored_patterns", None) or []
            if proj_patterns:
                if any(
                    fnmatch.fnmatch(basename, p) or fnmatch.fnmatch(norm_path, p)
                    for p in proj_patterns
                ):
                    return True

            # If project has .gitignore, Level 3 (Global fallback) is strictly bypassed
            return False

        # Fallback (Project has NO .gitignore):
        # Level 2: Project-specific ignored_patterns
        proj_patterns = getattr(self.project, "ignored_patterns", None) or []
        if proj_patterns:
            if any(
                fnmatch.fnmatch(basename, p) or fnmatch.fnmatch(norm_path, p)
                for p in proj_patterns
            ):
                return True

        # Level 3: Global fallback
        effective_fallback = self.fallback_ignored_patterns or BASELINE_FALLBACK_IGNORED
        if any(
            fnmatch.fnmatch(basename, p) or fnmatch.fnmatch(norm_path, p)
            for p in effective_fallback
        ):
            return True

        return False


_MANAGER_CACHE: dict[str, ProjectIgnoreManager] = {}


def get_project_ignore_manager(
    project: ProjectConfig,
    fallback_ignored_patterns: list[str] | None = None,
) -> ProjectIgnoreManager:
    """Get or create a ProjectIgnoreManager for the given project."""
    key = str(project.root.resolve())
    mgr = _MANAGER_CACHE.get(key)
    if mgr is None:
        mgr = ProjectIgnoreManager(project=project, fallback_ignored_patterns=fallback_ignored_patterns)
        _MANAGER_CACHE[key] = mgr
    else:
        mgr.project = project
        if fallback_ignored_patterns is not None:
            mgr.fallback_ignored_patterns = list(fallback_ignored_patterns)
    return mgr


def clear_ignore_manager_cache() -> None:
    """Clear cached ignore managers (for testing or full reload)."""
    _MANAGER_CACHE.clear()

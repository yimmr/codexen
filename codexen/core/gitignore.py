"""Unified GitIgnore engine supporting Git syntax across Level 1-3 rules, Model C switch, and HITL."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import re
from typing import Any

from pathspec import GitIgnoreSpec

from ..config import ProjectConfig, normalize_ignored_patterns
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

DEFAULT_IGNORE_ENGINE = "pathspec"


def get_current_engine() -> str:
    """Return the active ignore engine name ('pathspec' or 'legacy')."""
    return os.getenv("CODEXEN_IGNORE_ENGINE", DEFAULT_IGNORE_ENGINE).lower().strip()


@dataclass
class IgnoreDecision:
    """Detailed decision verdict from the ignore engine."""

    ignored: bool
    source: str
    pattern: str
    rule_file: str = ""

    def as_tuple(self) -> tuple[bool, str, str]:
        """Convert to classic 3-tuple (ignored, source, pattern)."""
        return (self.ignored, self.source, self.pattern)


class IgnorePatternSource:
    """A compiled GitIgnore pattern source (file, project config, or global fallback)."""

    def __init__(self, name: str, lines: list[str], base_dir: str = "", rule_file: str = "") -> None:
        self.name = name
        self.base_dir = base_dir.replace("\\", "/").strip("/")
        self.rule_file = rule_file.replace("\\", "/").strip("/")
        self.lines = [line.rstrip("\r\n") for line in lines if line.rstrip("\r\n").strip()]
        self.spec: GitIgnoreSpec | None = GitIgnoreSpec.from_lines(self.lines) if self.lines else None

    def check(self, norm_path: str, is_dir: bool = False) -> tuple[bool | None, str]:
        """Check relative path (from project root) against this pattern source.

        Returns:
            (verdict, matched_pattern) where verdict is:
            True: ignored
            False: negated (!)
            None: unmatched
        """
        if self.spec is None or not self.lines:
            return None, ""

        if self.base_dir:
            if norm_path == self.base_dir:
                # A directory cannot be ignored by rules inside itself
                return None, ""
            if not norm_path.startswith(self.base_dir + "/"):
                return None, ""
            rel = norm_path[len(self.base_dir) + 1 :]
        else:
            rel = norm_path

        if not rel:
            return None, ""

        check_path = (rel + "/") if (is_dir and not rel.endswith("/")) else rel
        res = self.spec.check_file(check_path)
        if res.include is None or res.index is None:
            return None, ""

        patterns_list = list(self.spec.patterns)
        if 0 <= res.index < len(patterns_list):
            pat_obj = patterns_list[res.index]
            raw_pat = getattr(pat_obj, "pattern", "")
        else:
            raw_pat = ""

        return res.include, raw_pat


class CompiledGitIgnoreFile:
    """Represents a .gitignore file compiled with GitIgnoreSpec and cached with mtime."""

    def __init__(self, file_path: Path, base_dir: str = "", rule_file: str = "") -> None:
        self.file_path = file_path
        self.base_dir = base_dir.replace("\\", "/").strip("/")
        self.rule_file = rule_file.replace("\\", "/").strip("/")
        self.mtime: float = -1.0
        self.source: IgnorePatternSource | None = None
        self.load()

    def is_stale(self) -> bool:
        """Check whether the file on disk was modified or deleted."""
        try:
            current_mtime = self.file_path.stat().st_mtime
            return current_mtime != self.mtime
        except OSError:
            return True

    def load(self) -> None:
        """Load and compile the .gitignore file with GitIgnoreSpec."""
        try:
            stat = self.file_path.stat()
            self.mtime = stat.st_mtime
            content = self.file_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
        except OSError:
            self.mtime = -1.0
            self.source = None
            return

        self.source = IgnorePatternSource(
            name="project_gitignore",
            lines=lines,
            base_dir=self.base_dir,
            rule_file=self.rule_file,
        )

    def check(self, norm_path: str, is_dir: bool = False) -> tuple[bool | None, str]:
        if self.source is None:
            return None, ""
        return self.source.check(norm_path, is_dir=is_dir)


class GitIgnoreRule:
    """Legacy parsed rule from a .gitignore file (retained for rollback / legacy engine)."""

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
    """Legacy loaded .gitignore file with automatic mtime invalidation."""

    def __init__(self, file_path: Path, base_dir: str = "") -> None:
        self.file_path = file_path
        self.base_dir = base_dir.replace("\\", "/").strip("/")
        self.mtime: float = -1.0
        self.rules: list[GitIgnoreRule] = []
        self.load()

    def is_stale(self) -> bool:
        try:
            current_mtime = self.file_path.stat().st_mtime
            return current_mtime != self.mtime
        except OSError:
            return True

    def load(self) -> None:
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

        m = re.search(r"(?<!\\)\s+$", line)
        if m:
            line = line[: m.start()]
        line = line.replace(r"\ ", " ")

        if not line:
            return None

        if line.startswith(r"\#"):
            line = line[1:]
        elif line.startswith("#"):
            return None

        is_negation = False
        if line.startswith(r"\!"):
            line = line[1:]
        elif line.startswith("!"):
            is_negation = True
            line = line[1:]

        if not line:
            return None

        dir_only = False
        if line.endswith("/"):
            dir_only = True
            line = line[:-1]

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

    def evaluate_rule(self, rel_path: str, is_dir: bool = False) -> tuple[bool | None, GitIgnoreRule | None]:
        norm_path = rel_path.replace("\\", "/").strip("/")
        if self.base_dir:
            if not (norm_path == self.base_dir or norm_path.startswith(self.base_dir + "/")):
                return None, None

        last_match: bool | None = None
        last_rule: GitIgnoreRule | None = None
        for rule in self.rules:
            if rule.matches(norm_path, is_dir=is_dir):
                last_match = not rule.is_negation
                last_rule = rule
        return last_match, last_rule

    def evaluate(self, rel_path: str, is_dir: bool = False) -> bool | None:
        res, _ = self.evaluate_rule(rel_path, is_dir=is_dir)
        return res


def is_path_authorized(rel_path: str, authorized_paths: list[str] | None) -> bool:
    """Check whether a relative path is covered by any authorized path in the HITL whitelist."""
    if not authorized_paths:
        return False

    norm = rel_path.replace("\\", "/").strip("/")
    basename = norm.rsplit("/", 1)[-1]

    for raw_auth in authorized_paths:
        auth = raw_auth.replace("\\", "/").strip("/")
        if not auth:
            continue
        if auth == "*":
            return True
        # Exact match
        if norm == auth:
            return True
        # Child of authorized directory (e.g. auth='target', norm='target/debug/app.exe')
        if norm.startswith(auth + "/"):
            return True
        # Ancestor of authorized path (to allow traversing into an authorized subfolder)
        if auth.startswith(norm + "/"):
            return True
        # Glob match on full path or basename
        if fnmatch.fnmatch(norm, auth) or fnmatch.fnmatch(basename, auth):
            return True
        if norm.endswith("/" + auth):
            return True

    return False


_CUSTOM_AUDIT_BASE_DIR: Path | None = None


def set_audit_base_dir(base_dir: Path | None) -> None:
    """Override base directory for penetration audit logging."""
    global _CUSTOM_AUDIT_BASE_DIR
    _CUSTOM_AUDIT_BASE_DIR = base_dir


def get_audit_base_dir() -> Path | None:
    """Get the current audit base directory override."""
    return _CUSTOM_AUDIT_BASE_DIR


def log_penetration_audit(
    project_name: str,
    requested_path: str,
    status: str,
    authorized_paths: list[str] | None = None,
    confirmed: bool = False,
    duration_ms: float = 0.0,
    files_scanned: int = 0,
    matches_count: int = 0,
    base_dir: Path | None = None,
) -> None:
    """Record an audit trail event in .runtime/audit.log for human-in-the-loop penetration."""
    try:
        from mcp_server_core.env import get_runtime_dir

        target_base = base_dir or _CUSTOM_AUDIT_BASE_DIR
        runtime_dir = get_runtime_dir(target_base)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        audit_file = runtime_dir / "audit.log"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "penetration_audit",
            "project": project_name,
            "requested_path": requested_path,
            "status": status,
            "confirmed": confirmed,
            "authorized_paths": list(authorized_paths or []),
            "duration_ms": duration_ms,
            "files_scanned": files_scanned,
            "matches_count": matches_count,
        }
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


class ProjectIgnoreManager:
    """Unified GitIgnore Ignore Engine managing RFC 0001 / PLAN 0001 rules:

    Level 1: Project-level .gitignore (root and subdirectories, Git syntax, highest authority)
    Level 2: config.yaml project-specific ignored_patterns (projects.<id>.ignored_patterns)
    Level 3: Global permissions.ignored_patterns (fallback when no project-level rules exist)
    Level 4: Baseline system & security exclusions (.git, credentials, etc.)
    """

    def __init__(
        self,
        project: ProjectConfig,
        fallback_ignored_patterns: list[str] | None = None,
        engine: str | None = None,
    ) -> None:
        self.project = project
        self.fallback_ignored_patterns = normalize_ignored_patterns(
            fallback_ignored_patterns if fallback_ignored_patterns is not None else BASELINE_FALLBACK_IGNORED
        )
        self.engine = (engine or get_current_engine()).lower().strip()
        self._gitignore_cache: dict[str, CompiledGitIgnoreFile] = {}
        self._legacy_gitignore_cache: dict[str, GitIgnoreFile] = {}
        self._cached_proj_config_key: tuple[str, ...] | None = None
        self._cached_proj_config_source: IgnorePatternSource | None = None
        self._cached_global_key: tuple[str, ...] | None = None
        self._cached_global_source: IgnorePatternSource | None = None

    def has_project_root_gitignore(self) -> bool:
        """Check if project root has a .gitignore file (including empty 0-byte file)."""
        root_gi = self.project.root / ".gitignore"
        return root_gi.is_file()

    def has_project_gitignore(self) -> bool:
        """Backward-compatible alias for has_project_root_gitignore."""
        return self.has_project_root_gitignore()

    def has_project_config_rules(self) -> bool:
        """Check if project-specific configuration (projects.<id>.ignored_patterns) is non-empty."""
        patterns = getattr(self.project, "ignored_patterns", None) or []
        return bool(patterns)

    def has_project_level_rules(self) -> bool:
        """Model C switch (D-003, D-007, D-008):

        Returns True if project root has a .gitignore (even empty) OR project config is non-empty.
        Subdirectory .gitignore files do NOT trigger this switch.
        """
        return self.has_project_root_gitignore() or self.has_project_config_rules()

    def get_or_load_compiled_gitignore(self, base_dir: str = "") -> CompiledGitIgnoreFile | None:
        """Get cached CompiledGitIgnoreFile for base_dir or reload if fresh/modified."""
        norm_base = base_dir.replace("\\", "/").strip("/")
        gi_path = (self.project.root / norm_base / ".gitignore") if norm_base else (self.project.root / ".gitignore")
        rule_rel = (norm_base + "/.gitignore") if norm_base else ".gitignore"

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
            loaded = CompiledGitIgnoreFile(gi_path, base_dir=norm_base, rule_file=rule_rel)
            self._gitignore_cache[norm_base] = loaded
            return loaded

        return None

    def get_or_load_legacy_gitignore(self, base_dir: str = "") -> GitIgnoreFile | None:
        """Get cached GitIgnoreFile for legacy engine."""
        norm_base = base_dir.replace("\\", "/").strip("/")
        gi_path = (self.project.root / norm_base / ".gitignore") if norm_base else (self.project.root / ".gitignore")

        cached = self._legacy_gitignore_cache.get(norm_base)
        if cached is not None:
            if cached.is_stale():
                if gi_path.is_file():
                    cached.load()
                else:
                    del self._legacy_gitignore_cache[norm_base]
                    return None
            return cached

        if gi_path.is_file():
            loaded = GitIgnoreFile(gi_path, base_dir=norm_base)
            self._legacy_gitignore_cache[norm_base] = loaded
            return loaded

        return None

    def get_or_load_gitignore(self, base_dir: str = "") -> CompiledGitIgnoreFile | GitIgnoreFile | None:
        """Get cached ignore file for base_dir depending on active engine."""
        if self.engine == "legacy":
            return self.get_or_load_legacy_gitignore(base_dir)
        return self.get_or_load_compiled_gitignore(base_dir)

    def _get_project_config_source(self) -> IgnorePatternSource | None:
        patterns = getattr(self.project, "ignored_patterns", None) or []
        key = tuple(patterns)
        if self._cached_proj_config_source is not None and self._cached_proj_config_key == key:
            return self._cached_proj_config_source

        self._cached_proj_config_key = key
        if not patterns:
            self._cached_proj_config_source = None
            return None

        self._cached_proj_config_source = IgnorePatternSource(
            name="project_config",
            lines=list(patterns),
            base_dir="",
            rule_file="config.yaml",
        )
        return self._cached_proj_config_source

    def _get_global_fallback_source(self) -> IgnorePatternSource:
        patterns = self.fallback_ignored_patterns or BASELINE_FALLBACK_IGNORED
        key = tuple(patterns)
        if self._cached_global_source is not None and self._cached_global_key == key:
            return self._cached_global_source

        self._cached_global_key = key
        self._cached_global_source = IgnorePatternSource(
            name="global_fallback",
            lines=list(patterns),
            base_dir="",
            rule_file="global_fallback",
        )
        return self._cached_global_source

    def _evaluate_path_candidate_rules(
        self, norm_path: str, is_dir: bool
    ) -> tuple[bool | None, str, str, str]:
        """Evaluate candidate rules (gitignore stack, project config, global fallback) on norm_path.

        Returns:
            (verdict, source, pattern, rule_file)
            where verdict is True (ignored), False (negated), or None (unmatched).
        """
        # 1. Project .gitignore stack (deepest candidate folder up to root)
        parts = norm_path.split("/") if norm_path else []
        candidate_dirs: list[str] = []
        limit = len(parts) if is_dir else len(parts) - 1
        for idx in range(limit, 0, -1):
            candidate_dirs.append("/".join(parts[:idx]))
        candidate_dirs.append("")  # project root

        for bdir in candidate_dirs:
            gi = self.get_or_load_compiled_gitignore(bdir)
            if gi is not None:
                verdict, raw_pat = gi.check(norm_path, is_dir=is_dir)
                if verdict is not None:
                    pat_display = f"!{raw_pat}" if verdict is False and not raw_pat.startswith("!") else raw_pat
                    return verdict, "project_gitignore", pat_display, gi.rule_file

        # 2. Project config (C) - evaluated only if .gitignore stack returned None
        if self.has_project_config_rules():
            cfg_src = self._get_project_config_source()
            if cfg_src is not None:
                verdict, raw_pat = cfg_src.check(norm_path, is_dir=is_dir)
                if verdict is not None:
                    pat_display = f"!{raw_pat}" if verdict is False and not raw_pat.startswith("!") else raw_pat
                    return verdict, "project_config", pat_display, "config.yaml"
            # Project config exists and was evaluated without matching -> global fallback is CLOSED!
            return None, "", "", ""

        # 3. Global fallback switch (Model C - D-003, D-007, D-008)
        # If root .gitignore exists (including empty file), global fallback is CLOSED!
        if self.has_project_root_gitignore():
            return None, "", "", ""

        # 4. Neither root .gitignore nor project config exists -> Evaluate global fallback (G)
        g_src = self._get_global_fallback_source()
        verdict, raw_pat = g_src.check(norm_path, is_dir=is_dir)
        if verdict is not None:
            pat_display = f"!{raw_pat}" if verdict is False and not raw_pat.startswith("!") else raw_pat
            return verdict, "global_fallback", pat_display, ""

        return None, "", "", ""

    def evaluate_gitignore_with_rule(
        self, rel_path: str, is_dir: bool = False
    ) -> tuple[bool | None, GitIgnoreRule | None]:
        """Legacy helper evaluating candidate .gitignore files."""
        if self.engine == "legacy":
            norm_path = rel_path.replace("\\", "/").strip("/")
            parts = norm_path.split("/") if norm_path else []

            candidate_dirs: list[str] = []
            if is_dir:
                for idx in range(len(parts), 0, -1):
                    candidate_dirs.append("/".join(parts[:idx]))
            else:
                for idx in range(len(parts) - 1, 0, -1):
                    candidate_dirs.append("/".join(parts[:idx]))
            candidate_dirs.append("")

            for bdir in candidate_dirs:
                gi_file = self.get_or_load_legacy_gitignore(bdir)
                if gi_file is not None:
                    res, rule = gi_file.evaluate_rule(norm_path, is_dir=is_dir)
                    if res is not None:
                        return res, rule

            return None, None

        # Pathspec mode
        verdict, source, pattern, rule_file = self._evaluate_path_candidate_rules(rel_path, is_dir=is_dir)
        if source == "project_gitignore" and verdict is not None:
            dummy_rule = GitIgnoreRule(
                raw_pattern=pattern.lstrip("!"),
                is_negation=verdict is False,
                dir_only=pattern.endswith("/"),
            )
            return verdict, dummy_rule
        return None, None

    def evaluate_gitignore(self, rel_path: str, is_dir: bool = False) -> bool | None:
        """Evaluate path against applicable .gitignore files."""
        if self.engine == "legacy":
            res, _ = self.evaluate_gitignore_with_rule(rel_path, is_dir=is_dir)
            return res

        norm_path = rel_path.replace("\\", "/").strip("/")
        parts = norm_path.split("/") if norm_path else []
        candidate_dirs: list[str] = []
        limit = len(parts) if is_dir else len(parts) - 1
        for idx in range(limit, 0, -1):
            candidate_dirs.append("/".join(parts[:idx]))
        candidate_dirs.append("")

        for bdir in candidate_dirs:
            gi = self.get_or_load_compiled_gitignore(bdir)
            if gi is not None:
                verdict, _ = gi.check(norm_path, is_dir=is_dir)
                if verdict is not None:
                    return verdict
        return None

    def explain_ignored_decision(
        self,
        rel_path: str,
        is_dir: bool = False,
        caller_ignored: list[str] | None = None,
        check_parents: bool = True,
        authorized_paths: list[str] | None = None,
    ) -> IgnoreDecision:
        """Determine whether rel_path should be ignored and return an IgnoreDecision object."""
        if self.engine == "legacy":
            ignored, src, pat = self._legacy_explain_ignored(
                rel_path=rel_path,
                is_dir=is_dir,
                caller_ignored=caller_ignored,
                check_parents=check_parents,
                authorized_paths=authorized_paths,
            )
            rule_file = ".gitignore" if src == "project_gitignore" else ""
            return IgnoreDecision(ignored=ignored, source=src, pattern=pat, rule_file=rule_file)

        norm_path = rel_path.replace("\\", "/").strip("/")
        if not norm_path or norm_path == ".":
            return IgnoreDecision(ignored=False, source="", pattern="", rule_file="")

        basename = norm_path.rsplit("/", 1)[-1]

        # Step 2: Baseline system exclusions & sensitive credentials (NEVER bypassed)
        for part in norm_path.split("/"):
            if part in BASELINE_SYSTEM_IGNORED:
                return IgnoreDecision(ignored=True, source="baseline_system", pattern=part, rule_file="")
        if is_sensitive_path(norm_path) or is_sensitive_path(basename):
            return IgnoreDecision(ignored=True, source="sensitive_rule", pattern=basename, rule_file="")

        # Step 3: Caller-specified patterns
        if caller_ignored:
            caller_src = IgnorePatternSource("caller_rule", lines=list(caller_ignored))
            c_verdict, c_pat = caller_src.check(norm_path, is_dir=is_dir)
            if c_verdict is True:
                return IgnoreDecision(ignored=True, source="caller_rule", pattern=c_pat, rule_file="")
            elif c_verdict is False:
                return IgnoreDecision(ignored=False, source="caller_rule", pattern=f"!{c_pat}", rule_file="")

        # Step 4: Human-in-the-loop (HITL) Penetration Authorization
        if authorized_paths and is_path_authorized(norm_path, authorized_paths):
            return IgnoreDecision(ignored=False, source="authorized_whitelist", pattern="", rule_file="")

        # Step 5: Check ancestors (check_parents=True)
        if check_parents and "/" in norm_path:
            parts = norm_path.split("/")
            for i in range(1, len(parts)):
                ancestor = "/".join(parts[:i])
                ans_base = ancestor.rsplit("/", 1)[-1]
                if ans_base in BASELINE_SYSTEM_IGNORED:
                    return IgnoreDecision(ignored=True, source="baseline_system", pattern=ans_base, rule_file="")
                if is_sensitive_path(ancestor) or is_sensitive_path(ans_base):
                    return IgnoreDecision(ignored=True, source="sensitive_rule", pattern=ans_base, rule_file="")

                ans_verdict, ans_source, ans_pat, ans_rule_file = self._evaluate_path_candidate_rules(
                    ancestor, is_dir=True
                )
                if ans_verdict is True:
                    return IgnoreDecision(
                        ignored=True,
                        source=ans_source,
                        pattern=ans_pat,
                        rule_file=ans_rule_file,
                    )

        # Step 6: Evaluate target path itself
        verdict, source, pattern, rule_file = self._evaluate_path_candidate_rules(norm_path, is_dir=is_dir)
        if verdict is True:
            return IgnoreDecision(ignored=True, source=source, pattern=pattern, rule_file=rule_file)
        elif verdict is False:
            return IgnoreDecision(ignored=False, source=source, pattern=pattern, rule_file=rule_file)

        return IgnoreDecision(ignored=False, source="", pattern="", rule_file="")

    def explain_ignored(
        self,
        rel_path: str,
        is_dir: bool = False,
        caller_ignored: list[str] | None = None,
        check_parents: bool = True,
        authorized_paths: list[str] | None = None,
    ) -> tuple[bool, str, str]:
        """Determine whether rel_path should be ignored and return (is_ignored, rule_source, matched_pattern)."""
        decision = self.explain_ignored_decision(
            rel_path=rel_path,
            is_dir=is_dir,
            caller_ignored=caller_ignored,
            check_parents=check_parents,
            authorized_paths=authorized_paths,
        )
        return decision.as_tuple()

    def is_ignored(
        self,
        rel_path: str,
        is_dir: bool = False,
        caller_ignored: list[str] | None = None,
        check_parents: bool = True,
        authorized_paths: list[str] | None = None,
    ) -> bool:
        """Determine whether rel_path should be ignored according to RFC 0001 / PLAN 0001 rules."""
        return self.explain_ignored_decision(
            rel_path=rel_path,
            is_dir=is_dir,
            caller_ignored=caller_ignored,
            check_parents=check_parents,
            authorized_paths=authorized_paths,
        ).ignored

    def _legacy_explain_ignored(
        self,
        rel_path: str,
        is_dir: bool = False,
        caller_ignored: list[str] | None = None,
        check_parents: bool = True,
        authorized_paths: list[str] | None = None,
    ) -> tuple[bool, str, str]:
        """Legacy implementation retained for rollback."""
        norm_path = rel_path.replace("\\", "/").strip("/")
        if not norm_path or norm_path == ".":
            return False, "", ""

        basename = norm_path.rsplit("/", 1)[-1]

        # Level 4: Baseline system exclusions & sensitive credentials
        for part in norm_path.split("/"):
            if part in BASELINE_SYSTEM_IGNORED:
                return True, "baseline_system", part
        if is_sensitive_path(norm_path) or is_sensitive_path(basename):
            return True, "sensitive_rule", basename

        # Caller-specified patterns
        if caller_ignored:
            for p in caller_ignored:
                if fnmatch.fnmatch(basename, p) or fnmatch.fnmatch(norm_path, p):
                    return True, "caller_rule", p

        # Human-in-the-loop (HITL) Penetration Authorization
        if authorized_paths and is_path_authorized(norm_path, authorized_paths):
            return False, "authorized_whitelist", ""

        # Level 1: Project .gitignore
        if check_parents and "/" in norm_path:
            parts = norm_path.split("/")
            for i in range(1, len(parts)):
                ancestor = "/".join(parts[:i])
                ans_res, ans_rule = self.evaluate_gitignore_with_rule(ancestor, is_dir=True)
                if ans_res is True:
                    pattern_str = (
                        (ans_rule.raw_pattern + "/")
                        if ans_rule and ans_rule.dir_only
                        else (ans_rule.raw_pattern if ans_rule else ancestor)
                    )
                    return True, "project_gitignore", pattern_str

        gi_res, gi_rule = self.evaluate_gitignore_with_rule(norm_path, is_dir=is_dir)
        if gi_res is True:
            pattern_str = (
                (gi_rule.raw_pattern + "/")
                if gi_rule and gi_rule.dir_only
                else (gi_rule.raw_pattern if gi_rule else norm_path)
            )
            return True, "project_gitignore", pattern_str
        elif gi_res is False:
            pattern_str = gi_rule.raw_pattern if gi_rule else norm_path
            return False, "project_gitignore", f"!{pattern_str}"

        # Level 2 & 3
        if self.has_project_root_gitignore():
            proj_patterns = getattr(self.project, "ignored_patterns", None) or []
            for p in proj_patterns:
                if fnmatch.fnmatch(basename, p) or fnmatch.fnmatch(norm_path, p):
                    return True, "project_config", p
            return False, "", ""

        proj_patterns = getattr(self.project, "ignored_patterns", None) or []
        for p in proj_patterns:
            if fnmatch.fnmatch(basename, p) or fnmatch.fnmatch(norm_path, p):
                return True, "project_config", p

        effective_fallback = self.fallback_ignored_patterns or BASELINE_FALLBACK_IGNORED
        for p in effective_fallback:
            if fnmatch.fnmatch(basename, p) or fnmatch.fnmatch(norm_path, p):
                return True, "global_fallback", p

        return False, "", ""


_MANAGER_CACHE: dict[str, ProjectIgnoreManager] = {}


def get_project_ignore_manager(
    project: ProjectConfig,
    fallback_ignored_patterns: list[str] | None = None,
    engine: str | None = None,
) -> ProjectIgnoreManager:
    """Get or create a ProjectIgnoreManager for the given project."""
    key = str(project.root.resolve())
    mgr = _MANAGER_CACHE.get(key)
    if mgr is None:
        mgr = ProjectIgnoreManager(
            project=project,
            fallback_ignored_patterns=fallback_ignored_patterns,
            engine=engine,
        )
        _MANAGER_CACHE[key] = mgr
    else:
        mgr.project = project
        if fallback_ignored_patterns is not None:
            mgr.fallback_ignored_patterns = normalize_ignored_patterns(fallback_ignored_patterns)
        if engine is not None:
            mgr.engine = engine.lower().strip()
    return mgr


def clear_ignore_manager_cache() -> None:
    """Clear cached ignore managers (for testing or full reload)."""
    _MANAGER_CACHE.clear()

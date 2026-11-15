"""Git operations: status inspection and diff review for target projects."""
from __future__ import annotations

import os
import subprocess
from typing import Any

from ..config import ProjectConfig
from .security import ensure_project_accessible, safe_resolve_path


def get_project_git_status(project: ProjectConfig) -> dict[str, Any]:
    """Inspect git repository status (working tree modifications, staged files, branch) in a project."""
    ensure_project_accessible(project)
    if not (project.root / ".git").exists():
        return {
            "status": "not_a_git_repository",
            "project": project.name,
            "message": f"Project '{project.name}' does not contain a .git directory.",
        }

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        res = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=str(project.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        if res.returncode != 0:
            return {
                "status": "error",
                "project": project.name,
                "exit_code": res.returncode,
                "message": res.stderr.strip() or f"git status failed with exit code {res.returncode}",
            }
        return {
            "status": "success",
            "project": project.name,
            "exit_code": res.returncode,
            "output": res.stdout,
        }
    except FileNotFoundError:
        return {"status": "error", "message": "git executable not found in host PATH."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to execute git status: {e}"}


def get_project_git_diff(
    project: ProjectConfig,
    relative_path: str = "",
    staged: bool = False,
) -> dict[str, Any]:
    """View git diff for working tree or staged changes in a project, enabling Agent self-review."""
    ensure_project_accessible(project)
    if not (project.root / ".git").exists():
        return {
            "status": "not_a_git_repository",
            "project": project.name,
            "message": f"Project '{project.name}' does not contain a .git directory.",
        }

    cmd = ["git", "diff"]
    if staged:
        cmd.append("--cached")
    if relative_path:
        safe_resolve_path(project.root, relative_path)
        cmd.extend(["--", relative_path.replace("\\", "/")])

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        res = subprocess.run(
            cmd,
            cwd=str(project.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=creationflags,
        )
        if res.returncode != 0:
            return {
                "status": "error",
                "project": project.name,
                "exit_code": res.returncode,
                "message": res.stderr.strip() or f"git diff failed with exit code {res.returncode}",
            }
        diff_text = res.stdout
        is_truncated = len(diff_text) > 50000
        payload: dict[str, Any] = {
            "status": "success",
            "project": project.name,
            "staged": staged,
            "file": relative_path or "all",
            "diff": diff_text[:50000] if diff_text else "[No differences]",
            "truncated": is_truncated,
        }
        if is_truncated:
            payload["total_lines"] = len(diff_text.splitlines())
            payload["hint"] = (
                "Diff output exceeded 50,000 characters and was truncated. "
                "Specify relative_path to view changes per file."
            )
        return payload
    except FileNotFoundError:
        return {"status": "error", "message": "git executable not found in host PATH."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to execute git diff: {e}"}


def get_project_git_log(
    project: ProjectConfig,
    max_commits: int = 20,
    relative_path: str = "",
) -> dict[str, Any]:
    """Inspect recent git commit history in a project."""
    ensure_project_accessible(project)
    if not (project.root / ".git").exists():
        return {
            "status": "not_a_git_repository",
            "project": project.name,
            "message": f"Project '{project.name}' does not contain a .git directory.",
        }

    count = min(max(1, max_commits), 100)
    cmd = ["git", "log", f"-n{count}", "--oneline", "--decorate"]
    if relative_path:
        safe_resolve_path(project.root, relative_path)
        cmd.extend(["--", relative_path.replace("\\", "/")])

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        res = subprocess.run(
            cmd,
            cwd=str(project.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        if res.returncode != 0:
            return {
                "status": "error",
                "project": project.name,
                "exit_code": res.returncode,
                "message": res.stderr.strip() or f"git log failed with exit code {res.returncode}",
            }
        return {
            "status": "success",
            "project": project.name,
            "exit_code": res.returncode,
            "output": res.stdout,
        }
    except FileNotFoundError:
        return {"status": "error", "message": "git executable not found in host PATH."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to execute git log: {e}"}

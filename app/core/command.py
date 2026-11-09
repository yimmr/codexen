"""Controlled terminal execution, security inspections, and output truncation."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from ..config import ProjectConfig, ServiceSettings
from ..exceptions import (
    CommandSecurityError,
    FileServiceError,
)
from .security import ensure_project_accessible, safe_resolve_path


def truncate_command_output(
    text: str,
    max_chars: int = 50_000,
    head_ratio: float = 0.2,
) -> tuple[str, bool, int, int]:
    """Truncate output using modern Agent head+tail retention strategy.

    Retains early context (startup logs, command banners) and the critical tail
    (test failures, stack traces, exit summaries).

    Returns:
        (truncated_text, is_truncated, total_lines, omitted_lines)
    """
    if not text:
        return "", False, 0, 0

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)

    if len(text) <= max_chars:
        return text, False, total_lines, 0

    head_limit = max(100, int(max_chars * head_ratio))
    tail_limit = max(500, max_chars - head_limit)

    head_part = text[:head_limit]
    tail_part = text[-tail_limit:]

    # Clean up cut lines
    if "\n" in head_part:
        head_part = head_part.rsplit("\n", 1)[0] + "\n"
    if "\n" in tail_part:
        tail_part = "\n" + tail_part.split("\n", 1)[1]

    omitted_chars = len(text) - len(head_part) - len(tail_part)
    head_lines_count = head_part.count("\n")
    tail_lines_count = tail_part.count("\n")
    omitted_lines = max(0, total_lines - head_lines_count - tail_lines_count)

    notice = (
        f"\n... [Output truncated: omitted {omitted_chars} characters "
        f"({omitted_lines} lines). Showing initial startup context and final execution tail] ...\n"
    )
    return head_part + notice + tail_part, True, total_lines, omitted_lines


def kill_process_tree(pid: int) -> None:
    """Terminate a process and all its child subprocesses cleanly."""
    if pid <= 0:
        return
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            creationflags=creationflags,
        )
    else:
        try:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass


def check_command_security(
    command: str,
    settings: ServiceSettings,
    confirmed: bool = False,
) -> tuple[str, str]:
    """Inspect terminal command against blacklists, confirmation rules, and whitelists.

    Returns:
        (verdict, message) where verdict is "allow", "deny", or "needs_confirmation".
    """
    cmd_lower = command.lower().strip()

    # 1. Hard system destruction blacklists (forbidden even if confirmed)
    for black_item in settings.command_blacklist:
        item = black_item.lower().strip()
        pattern = r"(^|[\s;|&])" + re.escape(item) + r"([\s;|&]|$)"
        if re.search(pattern, cmd_lower):
            raise CommandSecurityError(
                f"Prohibited destructive instruction detected: '{black_item}'. This command is hard-blocked for safety."
            )

    # 2. Disk wipe patterns (e.g. del /f /s /q C:\ or rmdir /s /q C:\)
    if re.search(r"\b(del|rmdir|rm)\b.*[a-zA-Z]:\\", cmd_lower):
        raise CommandSecurityError(
            "Command attempts deletion of system drive or root-level absolute paths."
        )

    # 3. Confirmation required actions (e.g. git push, npm publish, del, rmdir)
    for req_item in settings.command_require_confirmation:
        item = req_item.lower().strip()
        if item in cmd_lower:
            if not confirmed:
                return (
                    "needs_confirmation",
                    f"Command contains sensitive operation '{req_item}'. "
                    f"Please prompt the user for explicit confirmation before proceeding.",
                )

    # 4. Whitelist check for primary executable
    first_token = re.split(r"[\s;|&]+", cmd_lower)[0].replace(".exe", "")
    whitelist_clean = [w.lower().replace(".exe", "") for w in settings.command_whitelist]

    if first_token not in whitelist_clean:
        if not confirmed:
            return (
                "needs_confirmation",
                f"Executable '{first_token}' is not in the default development whitelist. "
                f"Please ask user for authorization to execute this command.",
            )

    return "allow", ""


def run_project_command(
    project: ProjectConfig,
    command: str,
    settings: ServiceSettings,
    subpath: str = "",
    timeout_seconds: int | None = None,
    confirmed: bool = False,
    require_confirmation: bool = False,
) -> dict[str, Any]:
    """Execute a controlled terminal command anchored inside the project root."""
    if not settings.allow_command_execution:
        raise CommandSecurityError("Terminal command execution is disabled by server configuration.")

    ensure_project_accessible(project)
    cwd = safe_resolve_path(project.root, subpath)
    if not cwd.is_dir():
        raise FileServiceError(f"Command working directory '{subpath}' is not a directory.")

    rel_cwd = str(cwd.relative_to(project.root)).replace("\\", "/") or "."

    # 1. Check if caller explicitly requested human confirmation or if confirmed is False
    if require_confirmation and not confirmed:
        return {
            "status": "needs_confirmation",
            "command": command,
            "project": project.name,
            "cwd": rel_cwd,
            "message": (
                "⚠️ This command requires explicit user confirmation before running. "
                "Please present the command and directory to the user in chat. "
                "When the user confirms, re-call run_terminal_command with confirmed=True."
            ),
            "confirmation_prompt": (
                f"Agent requests authorization to execute command:\n"
                f"  Project: {project.name}\n"
                f"  Directory: {rel_cwd}\n"
                f"  Command: {command}\n"
                f"Authorize this execution?"
            ),
        }

    # 2. Automated security and confirmation inspection
    verdict, reason = check_command_security(command, settings, confirmed=confirmed)
    if verdict == "needs_confirmation":
        return {
            "status": "needs_confirmation",
            "command": command,
            "project": project.name,
            "cwd": rel_cwd,
            "message": (
                f"⚠️ {reason} "
                "Please ask the user in chat for confirmation. "
                "When confirmed, re-invoke this tool with confirmed=True."
            ),
            "confirmation_prompt": (
                f"Agent requests permission to execute:\n"
                f"  Command: {command}\n"
                f"  Target: [{project.name}] {rel_cwd}\n"
                f"Do you authorize this execution?"
            ),
        }

    # 3. Compute timeout
    timeout = min(
        timeout_seconds or settings.command_timeout_seconds,
        settings.max_command_timeout_seconds,
    )

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    # Prepend active Python and Scripts directories to PATH
    import sys
    py_bin_dir = Path(sys.executable).parent
    scripts_dir = py_bin_dir / "Scripts"
    lib_bin_dir = py_bin_dir / "Library" / "bin"
    prepend_parts = [str(p) for p in (py_bin_dir, scripts_dir, lib_bin_dir) if p.exists()]
    if prepend_parts:
        env["PATH"] = os.pathsep.join(prepend_parts) + os.pathsep + env.get("PATH", "")

    start_time = time.perf_counter()
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=creationflags,
        )

        stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
        elapsed = time.perf_counter() - start_time
        exit_code = proc.returncode

    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start_time
        kill_process_tree(proc.pid)
        return {
            "status": "timeout",
            "command": command,
            "project": project.name,
            "cwd": rel_cwd,
            "exit_code": -1,
            "timed_out": True,
            "elapsed_seconds": round(elapsed, 2),
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds and was terminated.",
            "output": f"[ERROR: Execution timed out after {timeout}s]",
            "truncated": False,
            "total_lines": 0,
        }
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        return {
            "status": "error",
            "command": command,
            "project": project.name,
            "cwd": rel_cwd,
            "exit_code": -1,
            "timed_out": False,
            "elapsed_seconds": round(elapsed, 2),
            "stdout": "",
            "stderr": f"Failed to spawn process: {e}",
            "output": f"Execution error: {e}",
            "truncated": False,
            "total_lines": 0,
        }

    # 4. Smart Head+Tail output truncation
    max_output = settings.max_command_output_chars
    stdout_clean, out_trunc, out_lines, _ = truncate_command_output(stdout_raw, max_chars=max_output)
    stderr_clean, err_trunc, err_lines, _ = truncate_command_output(stderr_raw, max_chars=max_output)

    # Build combined output stream
    combined_raw = stdout_raw
    if stderr_raw:
        if combined_raw:
            combined_raw += "\n--- STDERR ---\n" + stderr_raw
        else:
            combined_raw = stderr_raw

    combined_clean, comb_trunc, comb_lines, omitted_lines = truncate_command_output(
        combined_raw, max_chars=max_output
    )

    return {
        "status": "success" if exit_code == 0 else "error",
        "command": command,
        "project": project.name,
        "cwd": rel_cwd,
        "exit_code": exit_code,
        "timed_out": False,
        "elapsed_seconds": round(elapsed, 2),
        "stdout": stdout_clean,
        "stderr": stderr_clean,
        "output": combined_clean,
        "truncated": comb_trunc,
        "total_lines": comb_lines,
        "omitted_lines": omitted_lines,
    }

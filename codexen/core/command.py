"""Controlled terminal execution, security inspections, and output truncation."""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
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
        # 1. Attempt fast Win32 API termination for the process and its child tree
        try:
            import ctypes
            import ctypes.wintypes
            kernel32 = ctypes.windll.kernel32
            TH32CS_SNAPPROCESS = 0x00000002

            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", ctypes.wintypes.DWORD),
                    ("cntUsage", ctypes.wintypes.DWORD),
                    ("th32ProcessID", ctypes.wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", ctypes.wintypes.DWORD),
                    ("cntThreads", ctypes.wintypes.DWORD),
                    ("th32ParentProcessID", ctypes.wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260),
                ]

            pids = [pid]
            h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if h_snap and h_snap != -1:
                pe = PROCESSENTRY32()
                pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
                parent_map: dict[int, list[int]] = {}
                if kernel32.Process32First(h_snap, ctypes.byref(pe)):
                    while True:
                        parent_map.setdefault(pe.th32ParentProcessID, []).append(pe.th32ProcessID)
                        if not kernel32.Process32Next(h_snap, ctypes.byref(pe)):
                            break
                kernel32.CloseHandle(h_snap)
                queue = [pid]
                while queue:
                    curr = queue.pop(0)
                    for child in parent_map.get(curr, []):
                        pids.append(child)
                        queue.append(child)

            import win32api
            import win32con
            for p in reversed(pids):
                try:
                    h = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, p)
                    if h:
                        win32api.TerminateProcess(h, 1)
                        win32api.CloseHandle(h)
                except Exception:
                    pass
        except Exception:
            pass

        # 2. taskkill fallback with timeout
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                creationflags=creationflags,
                timeout=2.0,
            )
        except (subprocess.TimeoutExpired, Exception):
            pass
    else:
        try:
            import signal
            killpg = getattr(os, "killpg", None)
            getpgid = getattr(os, "getpgid", None)
            sigkill = getattr(signal, "SIGKILL", getattr(signal, "SIGTERM", 15))
            if killpg and getpgid:
                killpg(getpgid(pid), sigkill)
            else:
                os.kill(pid, sigkill)
        except Exception:
            pass


def split_chained_commands(command_str: str) -> list[str]:
    """Split a shell command line by chaining operators (&, &&, |, ||, ;) respecting quotes
    and stream redirections (e.g. 2>&1).
    """
    cmd = command_str.strip()
    if not cmd:
        return []

    tokens: list[str] = []
    current: list[str] = []
    in_single_quote = False
    in_double_quote = False
    i = 0
    n = len(cmd)

    while i < n:
        char = cmd[i]

        # Handle quotes
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            current.append(char)
            i += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            current.append(char)
            i += 1
            continue

        # If inside quotes, nothing is a command separator
        if in_single_quote or in_double_quote:
            current.append(char)
            i += 1
            continue

        # Check for stream redirection like 2>&1, >&1, 1>&2
        if char == "&":
            prev_chars = "".join(current).rstrip()
            if prev_chars.endswith(">"):
                # Part of redirection operator (e.g. >&1 or 2>&1)
                current.append(char)
                i += 1
                continue

            # Check if this '&' is the PowerShell invocation operator at the start of a command
            if not "".join(current).strip():
                if i + 1 < n and cmd[i + 1] == "&":
                    pass  # '&&' at start, treat as separator
                else:
                    # Single '&' with empty prefix -> invocation operator like '& "python.exe"'
                    current.append(char)
                    i += 1
                    continue

            # Check for '&&'
            if i + 1 < n and cmd[i + 1] == "&":
                chunk = "".join(current).strip()
                if chunk:
                    tokens.append(chunk)
                current = []
                i += 2
                continue
            else:
                # Single '&'
                chunk = "".join(current).strip()
                if chunk:
                    tokens.append(chunk)
                current = []
                i += 1
                continue

        # Check for '||' or '|'
        if char == "|":
            if i + 1 < n and cmd[i + 1] == "|":
                chunk = "".join(current).strip()
                if chunk:
                    tokens.append(chunk)
                current = []
                i += 2
                continue
            else:
                chunk = "".join(current).strip()
                if chunk:
                    tokens.append(chunk)
                current = []
                i += 1
                continue

        # Check for ';'
        if char == ";":
            chunk = "".join(current).strip()
            if chunk:
                tokens.append(chunk)
            current = []
            i += 1
            continue

        current.append(char)
        i += 1

    last_chunk = "".join(current).strip()
    if last_chunk:
        tokens.append(last_chunk)

    return tokens


def extract_all_command_executables(command_str: str) -> list[tuple[str, str]]:
    """Extract (clean_token, base_name) for EVERY chained sub-command in command_str."""
    sub_commands = split_chained_commands(command_str)
    results: list[tuple[str, str]] = []
    for sub in sub_commands:
        token, base = extract_command_executable(sub)
        if token or base:
            results.append((token, base))
    return results


def extract_command_executable(command_str: str) -> tuple[str, str]:
    """Extract full initial executable path and base binary name from a command line.

    Handles quotes, relative paths, Windows and POSIX path separators.

    Returns:
        (clean_token, base_name)
        e.g. (".venv/scripts/python", "python")
             ("python", "python")
             ("git", "git")
    """
    cmd = command_str.strip()
    if not cmd:
        return "", ""

    # Strip optional shell invocation prefix (e.g. PowerShell '& ')
    cmd = re.sub(r"^&\s+", "", cmd)

    # Extract the first token respecting quotes
    if cmd.startswith(('"', "'")):
        quote_char = cmd[0]
        end_idx = cmd.find(quote_char, 1)
        token = cmd[1:end_idx] if end_idx != -1 else cmd[1:]
    else:
        # Split by first whitespace or shell separator
        match = re.split(r"[\s;|&]+", cmd, maxsplit=1)
        token = match[0] if match else ""

    token = token.strip().replace("\\", "/")
    token_lower = token.lower()

    # Remove .exe extension if present
    clean_token = token_lower.removesuffix(".exe")
    # Base binary name without leading path and extension
    base_name = Path(clean_token).name
    return clean_token, base_name


def is_safe_test_command(command: str) -> bool:
    """Identify standard offline test runner commands that can safely run without extra prompts."""
    cmd_lower = command.lower().strip()
    normalized = cmd_lower.replace("\\", "/")
    # Python unittest / pytest module invocation (e.g. python -m unittest, .venv/scripts/python -m unittest)
    if re.search(r"(?:^|[\s;|&/])(?:python\d*|py)(?:\.exe)?[\"']?\s+-m\s+(?:unittest|pytest|test)\b", normalized):
        return True
    # Direct pytest invocation
    if re.search(r"(?:^|[\s;|&/])pytest(?:\.exe)?\b", normalized):
        return True
    # Node / Cargo / Go test suites
    if re.search(r"(?:^|[\s;|&])(?:npm|pnpm|yarn|cargo|go)\s+test\b", normalized):
        return True
    return False


def check_command_security(
    command: str,
    settings: ServiceSettings,
    confirmed: bool = False,
) -> tuple[str, str]:
    """Inspect terminal command against blacklists, confirmation rules, and whitelists.

    Every sub-command in chained expressions (&, &&, |, ||, ;) is independently verified.

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

    # Split compound chained commands (&, &&, |, ||, ;)
    sub_commands = split_chained_commands(command)
    if not sub_commands:
        sub_commands = [command]

    # 3. Confirmation required actions (e.g. git push, npm publish, del, rmdir)
    for sub in sub_commands:
        sub_lower = sub.lower().strip()
        safe_test = is_safe_test_command(sub)

        for req_item in settings.command_require_confirmation:
            item = req_item.lower().strip()
            if not item:
                continue

            # If this is an offline test runner, bypass generic interpreter confirmation prompts
            if item in ("python", "py", "node") and safe_test:
                continue

            pattern = r"(?:^|[\s;|&])" + re.escape(item) + r"(?:[\s;|&]|$)"
            if re.search(pattern, sub_lower):
                if not confirmed:
                    return (
                        "needs_confirmation",
                        f"Command contains sensitive operation '{req_item}'. "
                        f"Please prompt the user for explicit confirmation before proceeding.",
                    )

    # 4. Whitelist check for EVERY sub-command in the chain
    whitelist_clean = {w.lower().removesuffix(".exe") for w in settings.command_whitelist}

    for sub in sub_commands:
        clean_token, base_name = extract_command_executable(sub)
        if not clean_token and not base_name:
            continue

        safe_test = is_safe_test_command(sub)
        is_whitelisted = (
            base_name in whitelist_clean
            or clean_token in whitelist_clean
            or (safe_test and base_name in ("python", "py", "pytest", "node", "npm", "pnpm", "yarn", "cargo", "go"))
        )

        if not is_whitelisted:
            if not confirmed:
                return (
                    "needs_confirmation",
                    f"Executable '{base_name or clean_token}' in chained command is not in the default development whitelist. "
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

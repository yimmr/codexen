"""Universal Process & Lifecycle Manager for MCP Services.

Provides unified process management, port inspection, single-instance verification,
and clean termination for both CLI and GUI desktop runners.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .env import get_runtime_dir
from .tunnel import stop_tunnel_if_no_mcp

if sys.platform == "win32":
    import win32api
    import win32con
    import win32event
    import win32process
    import winerror


def is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is currently active."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                code = win32process.GetExitCodeProcess(handle)
                win32api.CloseHandle(handle)
                return code == 259  # STILL_ACTIVE
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    return False


def find_pid_by_port(port: int) -> int | None:
    """Inspect local port and extract listening process PID."""
    if sys.platform == "win32":
        try:
            cmd = f'netstat -ano | findstr ":{port}"'
            out = subprocess.check_output(cmd, shell=True, text=True, errors="replace")
            for line in out.strip().splitlines():
                if "LISTENING" in line:
                    parts = line.split()
                    if len(parts) >= 5 and parts[-1] != "0":
                        return int(parts[-1])
        except Exception:
            pass
    return None


def is_port_in_use(port: int) -> bool:
    """Fast socket check whether local port is bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_pid(pid: int, timeout: float = 3.0) -> bool:
    """Terminate a process and all its subprocesses by PID cleanly."""
    if pid <= 0 or pid == os.getpid():
        return False
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
        try:
            res = subprocess.run(
                ["taskkill", "/f", "/t", "/pid", str(pid)],
                capture_output=True,
                timeout=timeout,
                creationflags=creationflags,
            )
            return res.returncode == 0
        except Exception:
            return False
    else:
        try:
            import signal
            os.kill(pid, signal.SIGKILL)
            return True
        except Exception:
            return False


def check_service_status(port: int, root_dir: Path, mutex_name: str | None = None) -> tuple[bool, str, int | None]:
    """Check whether MCP service is already active, starting, or listening.

    Returns (is_active, mode_description, pid).
    """
    runtime_dir = get_runtime_dir(root_dir)
    in_use = is_port_in_use(port)
    pid = find_pid_by_port(port) if in_use else None

    # 1. Check if port is in use
    if in_use:
        for t_file in [runtime_dir / ".tray.pid", root_dir / ".tray.pid"]:
            if t_file.exists():
                try:
                    t_pid = int(t_file.read_text().strip())
                    if is_pid_alive(t_pid):
                        return True, "系统托盘常驻模式 (Tray App)", t_pid
                except Exception:
                    pass
        return True, "控制台 / 后台服务模式", pid

    # 2. Check tray process PID (even during initial startup before port opens)
    for t_file in [runtime_dir / ".tray.pid", root_dir / ".tray.pid"]:
        if t_file.exists():
            try:
                t_pid = int(t_file.read_text().strip())
                if is_pid_alive(t_pid):
                    return True, "系统托盘常驻模式 (启动就绪中)", t_pid
            except Exception:
                pass

    # 3. Check server process PID
    for s_file in [runtime_dir / ".server.pid", root_dir / ".server.pid"]:
        if s_file.exists():
            try:
                s_pid = int(s_file.read_text().strip())
                if is_pid_alive(s_pid):
                    return True, "核心服务进程 (启动就绪中)", s_pid
            except Exception:
                pass

    # 4. Check Named Mutex on Windows if provided
    if mutex_name and sys.platform == "win32":
        try:
            h_mutex = win32event.OpenMutex(win32event.SYNCHRONIZE, False, mutex_name)
            if h_mutex:
                win32api.CloseHandle(h_mutex)
                return True, "系统托盘常驻模式 (互斥锁已占用)", None
        except Exception:
            pass

    return False, "", None


def kill_stale_server_process(port: int, root_dir: Path) -> list[int]:
    """Terminate any stale local server processes listening on port without stopping the tunnel.

    Returns list of killed PIDs.
    """
    runtime_dir = get_runtime_dir(root_dir)
    killed = []

    for pid_file in [runtime_dir / ".server.pid", root_dir / ".server.pid"]:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if pid > 0 and pid != os.getpid():
                    if kill_pid(pid):
                        killed.append(pid)
            except Exception:
                pass
            try:
                pid_file.unlink()
            except Exception:
                pass

    port_pid = find_pid_by_port(port)
    if port_pid and port_pid not in killed:
        if kill_pid(port_pid):
            killed.append(port_pid)

    return killed


def stop_mcp_service(port: int, root_dir: Path) -> dict[str, Any]:
    """Complete shutdown of server, tray app, port listeners, and tunnel.

    Returns summary of stopped components and killed PIDs.
    """
    runtime_dir = get_runtime_dir(root_dir)
    killed_pids: list[int] = []

    # 1. Stop tray app if running
    for tray_pid_file in [runtime_dir / ".tray.pid", root_dir / ".tray.pid"]:
        if tray_pid_file.exists():
            try:
                pid = int(tray_pid_file.read_text().strip())
                if kill_pid(pid):
                    killed_pids.append(pid)
            except Exception:
                pass
            try:
                tray_pid_file.unlink()
            except Exception:
                pass

    # 2. Stop server PID
    for pid_file in [runtime_dir / ".server.pid", root_dir / ".server.pid"]:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if kill_pid(pid):
                    killed_pids.append(pid)
            except Exception:
                pass
            try:
                pid_file.unlink()
            except Exception:
                pass

    # 3. Kill any process listening on the target port
    port_pid = find_pid_by_port(port)
    if port_pid and port_pid not in killed_pids:
        if kill_pid(port_pid):
            killed_pids.append(port_pid)

    # 4. Stop tunnel if no other service is active
    tunnel_stopped = False
    try:
        tunnel_stopped = stop_tunnel_if_no_mcp()
    except Exception:
        pass

    return {
        "status": "success",
        "port": port,
        "killed_pids": killed_pids,
        "tunnel_stopped": tunnel_stopped,
    }

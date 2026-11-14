"""Asynchronous & interactive process session management.

Allows remote AI Agents to spawn long-running background tasks (e.g. test suites,
dev servers, build watchers, interactive CLIs), poll outputs progressively,
send interactive input, and manage process lifecycles without timing out HTTP requests.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
import uuid

from ..config import ProjectConfig, ServiceSettings
from ..exceptions import (
    CommandSecurityError,
    FileServiceError,
)
from .command import check_command_security, kill_process_tree, truncate_command_output
from .security import ensure_project_accessible, safe_resolve_path


class ProcessSession:
    """Manages an active or historical background process session."""

    def __init__(
        self,
        session_id: str,
        project_name: str,
        command: str,
        cwd: Path,
        rel_cwd: str,
        process: subprocess.Popen,
    ) -> None:
        self.session_id = session_id
        self.project_name = project_name
        self.command = command
        self.cwd = cwd
        self.rel_cwd = rel_cwd
        self.process = process
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.ended_at: str | None = None
        self._lock = threading.Lock()
        self._buffer: deque[str] = deque(maxlen=20_000)
        self._all_output_chars = 0
        self._stop_requested = False

        # Launch background stream reader thread
        self._reader_thread = threading.Thread(
            target=self._stream_reader,
            name=f"reader-{session_id}",
            daemon=True,
        )
        self._reader_thread.start()

    def _stream_reader(self) -> None:
        """Continuously read stdout/stderr from process into the thread-safe circular buffer."""
        try:
            if self.process.stdout:
                for line in iter(self.process.stdout.readline, ""):
                    if not line:
                        break
                    with self._lock:
                        self._buffer.append(line)
                        self._all_output_chars += len(line)
        except Exception:
            pass
        finally:
            try:
                if self.process.stdout:
                    self.process.stdout.close()
            except Exception:
                pass
            with self._lock:
                if not self.ended_at:
                    self.ended_at = datetime.now(timezone.utc).isoformat()

    def is_alive(self) -> bool:
        """Check if process is currently executing."""
        if self.process.poll() is None:
            return True
        if not self.ended_at:
            with self._lock:
                self.ended_at = datetime.now(timezone.utc).isoformat()
        return False

    @property
    def exit_code(self) -> int | None:
        return self.process.poll()

    @property
    def status(self) -> str:
        if self.is_alive():
            return "running"
        if self._stop_requested:
            return "stopped"
        code = self.exit_code
        return "completed" if code == 0 else "failed"

    def read_output(
        self,
        max_chars: int = 50_000,
        clear_buffer: bool = True,
    ) -> tuple[str, bool, int]:
        """Read pending or accumulated output from the process.

        Returns:
            (output_text, is_truncated, total_lines)
        """
        with self._lock:
            if clear_buffer:
                lines = list(self._buffer)
                self._buffer.clear()
            else:
                lines = list(self._buffer)

        raw_text = "".join(lines)
        if not raw_text:
            return "", False, 0

        clean_text, truncated, total_lines, _ = truncate_command_output(raw_text, max_chars=max_chars)
        return clean_text, truncated, total_lines

    def send_input(self, input_text: str, endline: bool = True) -> int:
        """Deliver text to process standard input."""
        if not self.is_alive():
            raise RuntimeError(f"Cannot send input: Process '{self.session_id}' has already terminated.")

        if not self.process.stdin:
            raise RuntimeError(f"Process '{self.session_id}' does not have standard input pipe opened.")

        payload = input_text
        if endline and not payload.endswith("\n"):
            payload += "\n"

        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
            return len(payload)
        except Exception as e:
            raise RuntimeError(f"Failed to write to stdin of process '{self.session_id}': {e}") from e

    def stop(self, force: bool = False) -> dict[str, Any]:
        """Stop or terminate the process tree cleanly."""
        self._stop_requested = True
        pid = self.process.pid
        if not self.is_alive():
            return {
                "process_id": self.session_id,
                "status": self.status,
                "exit_code": self.exit_code,
                "message": "Process was already terminated.",
            }

        kill_process_tree(pid)
        try:
            self.process.wait(timeout=2.0)
        except Exception:
            if force:
                try:
                    self.process.kill()
                except Exception:
                    pass

        with self._lock:
            if not self.ended_at:
                self.ended_at = datetime.now(timezone.utc).isoformat()

        return {
            "process_id": self.session_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "pid": pid,
            "message": "Process successfully stopped.",
        }

    def to_dict(self) -> dict[str, Any]:
        """Convert session state to human & agent readable dictionary."""
        alive = self.is_alive()
        code = self.exit_code
        return {
            "process_id": self.session_id,
            "project": self.project_name,
            "command": self.command,
            "cwd": self.rel_cwd,
            "pid": self.process.pid,
            "status": self.status,
            "is_alive": alive,
            "exit_code": code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class ProcessSessionManager:
    """Registry and controller for all active and recently completed background processes."""

    def __init__(self, max_history: int = 50) -> None:
        self._sessions: dict[str, ProcessSession] = {}
        self._lock = threading.Lock()
        self.max_history = max_history

    def _prune_history_locked(self) -> None:
        """Prune oldest dead sessions if history exceeds max_history limit."""
        if len(self._sessions) <= self.max_history:
            return

        dead_keys = [k for k, s in self._sessions.items() if not s.is_alive()]
        excess = len(self._sessions) - self.max_history
        for k in dead_keys[:excess]:
            self._sessions.pop(k, None)

    def start_process(
        self,
        project: ProjectConfig,
        command: str,
        settings: ServiceSettings,
        subpath: str = "",
        confirmed: bool = False,
        require_confirmation: bool = False,
    ) -> dict[str, Any]:
        """Start a new background process inside the project workspace."""
        if not settings.allow_command_execution:
            raise CommandSecurityError("Terminal command execution is disabled by server configuration.")

        ensure_project_accessible(project)
        cwd = safe_resolve_path(project.root, subpath)
        if not cwd.is_dir():
            raise FileServiceError(f"Working directory '{subpath}' is not a directory.")

        rel_cwd = str(cwd.relative_to(project.root)).replace("\\", "/") or "."

        # Check explicit confirmation
        if require_confirmation and not confirmed:
            return {
                "status": "needs_confirmation",
                "command": command,
                "project": project.name,
                "cwd": rel_cwd,
                "message": (
                    "⚠️ This background command requires explicit user confirmation before running. "
                    "Please confirm with the user and call start_background_process with confirmed=True."
                ),
            }

        # Automated security rules check
        verdict, reason = check_command_security(command, settings, confirmed=confirmed)
        if verdict == "needs_confirmation":
            return {
                "status": "needs_confirmation",
                "command": command,
                "project": project.name,
                "cwd": rel_cwd,
                "message": f"⚠️ {reason} Please ask user for authorization and re-call with confirmed=True.",
            }

        # Setup execution environment
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        import sys
        py_bin_dir = Path(sys.executable).parent
        scripts_dir = py_bin_dir / "Scripts"
        lib_bin_dir = py_bin_dir / "Library" / "bin"
        prepend_parts = [str(p) for p in (py_bin_dir, scripts_dir, lib_bin_dir) if p.exists()]
        if prepend_parts:
            env["PATH"] = os.pathsep.join(prepend_parts) + os.pathsep + env.get("PATH", "")

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=creationflags,
                bufsize=1,
            )
        except Exception as e:
            return {
                "status": "error",
                "command": command,
                "project": project.name,
                "cwd": rel_cwd,
                "message": f"Failed to spawn background process: {e}",
            }

        session_id = f"proc_{uuid.uuid4().hex[:8]}"
        session = ProcessSession(
            session_id=session_id,
            project_name=project.name,
            command=command,
            cwd=cwd,
            rel_cwd=rel_cwd,
            process=proc,
        )

        with self._lock:
            self._prune_history_locked()
            self._sessions[session_id] = session

        return {
            "status": "success",
            "process_id": session_id,
            "project": project.name,
            "command": command,
            "cwd": rel_cwd,
            "pid": proc.pid,
            "started_at": session.started_at,
            "message": (
                f"Background process '{session_id}' started. "
                f"Use read_process_output(process_id='{session_id}') to inspect output, "
                f"send_process_input(process_id='{session_id}') to interact, or "
                f"stop_background_process(process_id='{session_id}') to terminate."
            ),
        }

    def get_session(self, process_id: str) -> ProcessSession:
        """Lookup session by ID."""
        with self._lock:
            session = self._sessions.get(process_id)
        if not session:
            available = list(self._sessions.keys())
            raise ValueError(f"Process session '{process_id}' not found. Available sessions: {available}")
        return session

    def read_output(
        self,
        process_id: str,
        max_chars: int = 50_000,
        clear_buffer: bool = True,
    ) -> dict[str, Any]:
        """Read output from a background process."""
        session = self.get_session(process_id)
        output, truncated, total_lines = session.read_output(max_chars=max_chars, clear_buffer=clear_buffer)
        return {
            "status": "success",
            "process_id": process_id,
            "project": session.project_name,
            "command": session.command,
            "is_alive": session.is_alive(),
            "process_status": session.status,
            "exit_code": session.exit_code,
            "output": output,
            "truncated": truncated,
            "lines": total_lines,
        }

    def send_input(
        self,
        process_id: str,
        input_text: str,
        endline: bool = True,
    ) -> dict[str, Any]:
        """Send input to a background process standard input."""
        session = self.get_session(process_id)
        chars_written = session.send_input(input_text=input_text, endline=endline)
        return {
            "status": "success",
            "process_id": process_id,
            "chars_written": chars_written,
            "is_alive": session.is_alive(),
        }

    def stop_process(
        self,
        process_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Stop a background process."""
        session = self.get_session(process_id)
        res = session.stop(force=force)
        res["status"] = "success"
        return res

    def list_processes(
        self,
        project_name: str = "",
    ) -> list[dict[str, Any]]:
        """List all tracked processes, optionally filtered by project name."""
        with self._lock:
            sessions = list(self._sessions.values())

        target = project_name.strip().lower()
        results: list[dict[str, Any]] = []
        for s in sessions:
            if target and s.project_name.lower() != target:
                continue
            results.append(s.to_dict())

        return results


# Global singleton instance for process management across tools
global_process_manager = ProcessSessionManager()

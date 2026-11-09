"""Cloudflare Tunnel Manager for MCP services.

Provides both:
1. Singleton Named Tunnel Daemon management (ingress port discovery via :20245/config,
   auto start, and smart shutdown when no MCP services remain).
2. Dedicated CloudflareTunnel subprocess wrapper (Quick Tunnel or Named Tunnel).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

# Local metrics / management endpoint for cloudflared
TUNNEL_METRICS_HOST = "127.0.0.1"
TUNNEL_METRICS_PORT = 20245
FALLBACK_PORTS = {8787, 8788}


def get_tunnel_token() -> str:
    """Resolve Cloudflare Named Tunnel token from environment or configuration."""
    token = os.getenv("CLOUDFLARE_TUNNEL_TOKEN") or os.getenv("MCP_TUNNEL_TOKEN")
    if token:
        return token.strip()

    # Fallback to local .env in current or parent directories
    for candidate in [Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"]:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("CLOUDFLARE_TUNNEL_TOKEN="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
                elif line.startswith("MCP_TUNNEL_TOKEN="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val

    # Fallback to config.yaml or config/settings.yaml
    for candidate in [
        Path.cwd() / "config.yaml",
        Path(__file__).resolve().parents[1] / "config.yaml",
        Path.cwd() / "config" / "settings.yaml",
        Path(__file__).resolve().parents[1] / "config" / "settings.yaml",
    ]:
        if candidate.exists():
            try:
                import yaml
                data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    t = data.get("mcp", {}).get("http", {}).get("tunnel_token", "")
                    if t:
                        return str(t).strip()
            except Exception:
                pass

    return ""


def find_cloudflared(custom_path: str | Path | None = None) -> Path:
    """Locate cloudflared executable."""
    if custom_path and Path(custom_path).exists():
        return Path(custom_path).resolve()

    candidates = [
        Path.cwd() / "bin" / "cloudflared.exe",
        Path(__file__).resolve().parents[1] / "bin" / "cloudflared.exe",
        Path("D:/Codexen/bin/cloudflared.exe"),
        Path("D:/works/literary_workflow_v1/bin/cloudflared.exe"),
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()

    which = shutil.which("cloudflared")
    if which:
        return Path(which).resolve()

    raise FileNotFoundError(
        "cloudflared.exe not found! Please place cloudflared.exe in 'bin/' or install it in PATH."
    )


def is_cloudflared_available() -> bool:
    """Check if cloudflared binary is accessible."""
    try:
        bin_path = find_cloudflared()
        return bin_path.exists()
    except Exception:
        return False


def is_port_listening(port: int) -> bool:
    """Fast socket check whether a local port is currently listening (IPv4/IPv6)."""
    for target in [("127.0.0.1", port), ("::1", port)]:
        try:
            family = socket.AF_INET6 if ":" in target[0] else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                if s.connect_ex(target) == 0:
                    return True
        except Exception:
            pass
    return False


def get_cloudflare_ingress_ports() -> set[int]:
    """Dynamically fetch all local service ports configured in Cloudflare Zero Trust Dashboard
    for this tunnel directly via the cloudflared local management endpoint.
    """
    ports: set[int] = set()
    url = f"http://{TUNNEL_METRICS_HOST}:{TUNNEL_METRICS_PORT}/config"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("config", {}).get("ingress", []):
                srv = item.get("service", "")
                if srv.startswith(("http://", "https://", "tcp://")):
                    p = urlparse(srv).port
                    if p:
                        ports.add(p)
    except Exception:
        return set(FALLBACK_PORTS)

    return ports or set(FALLBACK_PORTS)


def is_tunnel_running() -> bool:
    """Check if cloudflared.exe is currently running on the system."""
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        res = subprocess.run(
            ["tasklist", "/fi", "imagename eq cloudflared.exe"],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=creationflags,
        )
        return "cloudflared.exe" in res.stdout
    except Exception:
        return False


def ensure_tunnel_running(token: str | None = None) -> bool:
    """Ensure the Cloudflare Named Tunnel daemon is running. Starts it if not already active."""
    if is_tunnel_running():
        return True

    resolved_token = (token or get_tunnel_token()).strip()
    if not resolved_token:
        return False

    exe = find_cloudflared()
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    cmd = [
        str(exe),
        "tunnel",
        "--metrics",
        f"{TUNNEL_METRICS_HOST}:{TUNNEL_METRICS_PORT}",
        "run",
        "--token",
        resolved_token,
        "--protocol",
        "http2",
    ]
    subprocess.Popen(
        cmd,
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    return True


def stop_tunnel_if_no_mcp() -> bool:
    """Stop cloudflared.exe if no local MCP services configured in Cloudflare are listening.

    Returns True if tunnel was stopped, False if other services are still active.
    """
    ingress_ports = get_cloudflare_ingress_ports()

    for port in ingress_ports:
        if is_port_listening(port):
            return False

    try:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.run(
            ["taskkill", "/f", "/im", "cloudflared.exe"],
            capture_output=True,
            creationflags=creationflags,
        )
    except Exception:
        pass
    return True


class CloudflareTunnel:
    """Manages cloudflared subprocess to create a secure public HTTPS endpoint."""

    def __init__(
        self,
        local_port: int = 8788,
        local_host: str = "127.0.0.1",
        cloudflared_path: str | Path | None = None,
        tunnel_token: str | None = None,
        public_url: str | None = None,
    ) -> None:
        self.local_port = local_port
        self.local_host = local_host
        self.tunnel_token = tunnel_token.strip() if tunnel_token else None
        self.cloudflared_bin = find_cloudflared(cloudflared_path)
        self.process: subprocess.Popen | None = None
        self.public_url: str | None = public_url
        self._stop_event = threading.Event()
        self._listener_thread: threading.Thread | None = None

    def start(self, on_url_ready: Callable[[str], None] | None = None, timeout: float = 30.0) -> str:
        """Start cloudflared tunnel and wait until public HTTPS URL or connection is detected."""
        target_url = f"http://{self.local_host}:{self.local_port}"

        if self.tunnel_token:
            cmd = [
                str(self.cloudflared_bin),
                "tunnel",
                "run",
                "--token",
                self.tunnel_token,
                "--protocol",
                "http2",
            ]
        else:
            cmd = [
                str(self.cloudflared_bin),
                "tunnel",
                "--url",
                target_url,
                "--no-autoupdate",
                "--protocol",
                "http2",
            ]

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )

        ready_event = threading.Event()

        def _reader():
            quick_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
            while not self._stop_event.is_set() and self.process and self.process.poll() is None:
                line = self.process.stdout.readline()
                if not line:
                    break

                if self.tunnel_token:
                    if "Registered tunnel connection" in line or "Updated to new configuration" in line:
                        if not self.public_url:
                            self.public_url = os.getenv("TUNNEL_PUBLIC_URL", "") or os.getenv("MCP_PORTAL_URL", "") or "https://your-domain.com"
                        ready_event.set()
                        if on_url_ready:
                            on_url_ready(self.public_url)
                else:
                    match = quick_pattern.search(line)
                    if match and not self.public_url:
                        self.public_url = match.group(0).strip()
                        ready_event.set()
                        if on_url_ready:
                            on_url_ready(self.public_url)

        self._listener_thread = threading.Thread(target=_reader, daemon=True)
        self._listener_thread.start()

        if not ready_event.wait(timeout=timeout):
            if self.process.poll() is not None:
                raise RuntimeError(f"cloudflared process exited unexpectedly with code {self.process.returncode}")
            raise TimeoutError(f"Timed out after {timeout}s waiting for Cloudflare Tunnel connection.")

        return self.public_url or ""

    def stop(self) -> None:
        """Stop the tunnel process safely."""
        self._stop_event.set()
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None


def get_public_tunnel_url() -> str:
    """Resolve public tunnel URL from environment or configuration."""
    url = os.getenv("PUBLIC_BASE_URL") or os.getenv("TUNNEL_PUBLIC_URL")
    if url:
        return url.strip()
    try:
        from .env import DeploymentEnvConfig
        cfg = DeploymentEnvConfig.load()
        if cfg.upstream_url:
            return cfg.upstream_url.strip()
        if cfg.portal_url:
            return cfg.portal_url.strip()
    except Exception:
        pass
    return os.getenv("TUNNEL_PUBLIC_URL", "https://your-domain.com")


def check_tunnel_health(public_url: str = "", timeout: float = 5.0) -> bool:
    """Perform an HTTP GET check against the public tunnel health endpoint."""
    url = (public_url or get_public_tunnel_url()).strip().rstrip("/")
    if not url:
        return False
    health_url = url if url.endswith("/health") else f"{url}/health"
    try:
        req = urllib.request.Request(
            health_url,
            headers={"User-Agent": "Codexen-Tunnel-Heartbeat/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or getattr(resp, "code", None)
            if code is None and hasattr(resp, "getcode"):
                code = resp.getcode()
            if code is None:
                code = 200
            return 200 <= int(code) < 400
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 400
    except Exception:
        return False


def restart_tunnel() -> bool:
    """Force restart the Cloudflare Named Tunnel daemon to recover broken connections."""
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.run(
            ["taskkill", "/f", "/im", "cloudflared.exe"],
            capture_output=True,
            creationflags=creationflags,
        )
    except Exception:
        pass
    time.sleep(1.0)
    return ensure_tunnel_running()


class TunnelHeartbeatMonitor:
    """Periodic background heartbeat probe that verifies public tunnel health and automatically heals if broken."""

    def __init__(
        self,
        public_url: str = "",
        check_interval: float = 30.0,
        max_consecutive_failures: int = 3,
        on_heal: Callable[[], None] | None = None,
    ) -> None:
        self.public_url = (public_url or get_public_tunnel_url()).strip().rstrip("/")
        self.check_interval = max(5.0, check_interval)
        self.max_consecutive_failures = max(1, max_consecutive_failures)
        self.on_heal = on_heal
        self.consecutive_failures = 0
        self.last_check_time: float = 0.0
        self.last_success_time: float = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def probe_now(self, timeout: float = 5.0) -> bool:
        """Run a synchronous health probe right now."""
        self.last_check_time = time.time()
        is_healthy = check_tunnel_health(self.public_url, timeout=timeout)
        if is_healthy:
            self.consecutive_failures = 0
            self.last_success_time = time.time()
        else:
            self.consecutive_failures += 1
        return is_healthy

    def trigger_self_heal(self) -> bool:
        """Self-heal by terminating and relaunching the Cloudflare tunnel daemon."""
        restarted = restart_tunnel()
        if self.on_heal:
            try:
                self.on_heal()
            except Exception:
                pass
        self.consecutive_failures = 0
        return restarted

    def _run_loop(self) -> None:
        self._stop_event.wait(5.0)
        while not self._stop_event.is_set():
            healthy = self.probe_now()
            if not healthy and self.consecutive_failures >= self.max_consecutive_failures:
                self.trigger_self_heal()
            self._stop_event.wait(self.check_interval)

    def start(self) -> None:
        """Start the background heartbeat monitor thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="tunnel-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the heartbeat monitor thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None


def start_tunnel_heartbeat_monitor(
    public_url: str = "",
    check_interval: float = 30.0,
    max_consecutive_failures: int = 3,
) -> TunnelHeartbeatMonitor:
    """Start and return a global or dedicated tunnel heartbeat monitor in daemon mode."""
    monitor = TunnelHeartbeatMonitor(
        public_url=public_url,
        check_interval=check_interval,
        max_consecutive_failures=max_consecutive_failures,
    )
    monitor.start()
    return monitor


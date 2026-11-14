"""Tests for async process sessions, multimodal images, binary files, and tunnel health monitor."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

try:
    from mcp.server.fastmcp import FastMCP, Image
except ImportError:
    from mcp.server.mcpserver import MCPServer as FastMCP
    Image = None

from app.config import ConfigManager, ProjectConfig
from app.core import (
    ProcessSessionManager,
    global_process_manager,
    read_project_binary_file,
    write_project_binary_file,
)
from app.exceptions import (
    CommandSecurityError,
    PathSecurityError,
    ReadOnlyProjectError,
)
from app.tools import ToolResultListDict, register_file_tools
from mcp_server_core.tunnel import (
    TunnelHeartbeatMonitor,
    check_tunnel_health,
    restart_tunnel,
)


class AsyncProcessAndMultimodalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_dir = Path(self.temp_dir) / "works"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        self.proj_root = self.workspace_dir / "test_proj"
        self.proj_root.mkdir(parents=True, exist_ok=True)

        self.ro_proj_root = self.workspace_dir / "ro_proj"
        self.ro_proj_root.mkdir(parents=True, exist_ok=True)

        self.config_file = Path(self.temp_dir) / "settings.yaml"
        self.config_file.write_text(f"""
workspace:
  root: "{str(self.workspace_dir).replace('\\', '/')}"
  allow_create_project: true
projects:
  test_proj:
    root: "{str(self.proj_root).replace('\\', '/')}"
    description: "Test Project"
    read_only: false
  ro_proj:
    root: "{str(self.ro_proj_root).replace('\\', '/')}"
    description: "Read Only Project"
    read_only: true
mcp:
  permissions:
    allow_command_execution: true
    command_timeout_seconds: 10
    max_command_output_chars: 10000
""", encoding="utf-8")

        self.config_mgr = ConfigManager(config_path=self.config_file, base_dir=Path(self.temp_dir))
        self.mcp = FastMCP("Test MCP Server")
        self.tools = register_file_tools(self.mcp, self.config_mgr)

    def tearDown(self) -> None:
        # Clean up any lingering background processes
        for p in global_process_manager.list_processes():
            try:
                global_process_manager.stop_process(p["process_id"], force=True)
            except Exception:
                pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # --------------------------------------------------------------------------
    # 1. Asynchronous & Interactive Process Sessions
    # --------------------------------------------------------------------------
    def test_background_process_lifecycle(self) -> None:
        # 1. Start a simple background python script that prints lines and sleeps
        py_cmd = (
            'python -c "import time, sys; '
            'print(\'alpha_start\'); sys.stdout.flush(); '
            'time.sleep(0.3); '
            'print(\'alpha_mid\'); sys.stdout.flush(); '
            'time.sleep(0.3); '
            'print(\'alpha_end\'); sys.stdout.flush()"'
        )
        start_res = self.tools["start_background_process"](
            project_id="test_proj",
            command=py_cmd,
            confirmed=True,
        )
        self.assertEqual(start_res["status"], "success")
        proc_id = start_res["process_id"]
        self.assertTrue(proc_id.startswith("proc_"))
        self.assertEqual(start_res["project"], "test_proj")

        # 2. Poll output progressively with brief retry for Windows process startup
        out1 = {"output": ""}
        for _ in range(10):
            time.sleep(0.1)
            out1 = self.tools["read_process_output"](process_id=proc_id, clear_buffer=False)
            if "alpha_start" in out1.get("output", ""):
                break
        self.assertEqual(out1["status"], "success")
        self.assertIn("alpha_start", out1["output"])

        # 3. List processes
        list_res = self.tools["list_background_processes"](project_id="test_proj")
        self.assertIsInstance(list_res, ToolResultListDict)
        self.assertGreaterEqual(len(list_res), 1)
        matching = [p for p in list_res["processes"] if p["process_id"] == proc_id]
        self.assertEqual(len(matching), 1)

        # 4. Wait for completion and read cleared output
        time.sleep(0.6)
        out2 = self.tools["read_process_output"](process_id=proc_id, clear_buffer=True)
        self.assertIn("alpha_end", out2["output"])

        # Subsequent read with cleared buffer should be empty
        out3 = self.tools["read_process_output"](process_id=proc_id, clear_buffer=True)
        self.assertEqual(out3["output"], "")

    def test_background_process_interactive_stdin(self) -> None:
        # Script that reads a name from stdin and prints greeting
        py_cmd = (
            'python -c "import sys; '
            'line = sys.stdin.readline().strip(); '
            'print(f\'GREETINGS:{line}\'); '
            'sys.stdout.flush()"'
        )
        start_res = self.tools["start_background_process"](
            project_id="test_proj",
            command=py_cmd,
            confirmed=True,
        )
        proc_id = start_res["process_id"]
        time.sleep(0.1)

        # Send input
        send_res = self.tools["send_process_input"](
            process_id=proc_id,
            input_text="AgentCodexen",
            endline=True,
        )
        self.assertEqual(send_res["status"], "success")
        self.assertGreater(send_res["chars_written"], 0)

        # Wait and read greeting
        time.sleep(0.3)
        out = self.tools["read_process_output"](process_id=proc_id)
        self.assertIn("GREETINGS:AgentCodexen", out["output"])

    def test_background_process_stop(self) -> None:
        # Long sleeping process
        py_cmd = 'python -c "import time; time.sleep(60)"'
        start_res = self.tools["start_background_process"](
            project_id="test_proj",
            command=py_cmd,
            confirmed=True,
        )
        proc_id = start_res["process_id"]

        stop_res = self.tools["stop_background_process"](process_id=proc_id, force=True)
        self.assertEqual(stop_res["status"], "success")

        # Verify it is no longer alive
        proc_info = self.tools["read_process_output"](process_id=proc_id)
        self.assertFalse(proc_info["is_alive"])

    def test_background_process_security_block(self) -> None:
        # Destructive command should be hard blocked
        with self.assertRaises(CommandSecurityError):
            self.tools["start_background_process"](
                project_id="test_proj",
                command="format C:",
                confirmed=True,
            )

        # Unconfirmed dangerous or non-whitelisted command should request confirmation
        needs_conf = self.tools["start_background_process"](
            project_id="test_proj",
            command="custom_tool --flag",
            confirmed=False,
        )
        self.assertEqual(needs_conf["status"], "needs_confirmation")

    # --------------------------------------------------------------------------
    # 2. Binary File & Multimodal Image Tools
    # --------------------------------------------------------------------------
    def test_binary_file_read_and_write(self) -> None:
        # Raw binary content with null bytes and high-byte values
        sample_bytes = bytes([0x00, 0xFF, 0xFE, 0x01, 0x42, 0x43, 0x00, 0x10, 0xAA])
        b64_content = base64.b64encode(sample_bytes).decode("ascii")
        expected_sha = hashlib.sha256(sample_bytes).hexdigest()

        # 1. Write binary file via tool
        write_res = self.tools["write_binary_file"](
            project_id="test_proj",
            relative_path="data/blob.bin",
            content_base64=b64_content,
            overwrite=True,
        )
        self.assertEqual(write_res["status"], "success")
        self.assertEqual(write_res["size_bytes"], len(sample_bytes))
        self.assertEqual(write_res["sha256"], expected_sha)

        # 2. Read back via read_binary_file
        read_res = self.tools["read_binary_file"](
            project_id="test_proj",
            relative_path="data/blob.bin",
        )
        self.assertEqual(read_res["status"], "success")
        self.assertEqual(read_res["size_bytes"], len(sample_bytes))
        self.assertEqual(read_res["sha256"], expected_sha)
        self.assertEqual(read_res["content_base64"], b64_content)
        self.assertEqual(read_res["mime_type"], "application/octet-stream")

        # Verify underlying disk content
        written_bytes = (self.proj_root / "data" / "blob.bin").read_bytes()
        self.assertEqual(written_bytes, sample_bytes)

    def test_binary_file_readonly_and_sensitive_guard(self) -> None:
        sample_b64 = base64.b64encode(b"hello").decode("ascii")

        # 1. Reject writing to read-only project
        with self.assertRaises(ReadOnlyProjectError):
            self.tools["write_binary_file"](
                project_id="ro_proj",
                relative_path="file.bin",
                content_base64=sample_b64,
            )

        # 2. Reject sensitive paths like .env or id_rsa
        with self.assertRaises(PathSecurityError):
            self.tools["write_binary_file"](
                project_id="test_proj",
                relative_path=".env",
                content_base64=sample_b64,
            )
        with self.assertRaises(PathSecurityError):
            self.tools["read_binary_file"](
                project_id="test_proj",
                relative_path=".git/config",
            )

    def test_multimodal_image_tool(self) -> None:
        # Create a valid 1x1 transparent PNG file
        # PNG 1x1 pixel base64:
        png_1x1_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        png_bytes = base64.b64decode(png_1x1_b64)
        img_path = self.proj_root / "assets" / "sample.png"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(png_bytes)

        # Call read_media_file
        media_res = self.tools["read_media_file"](
            project_id="test_proj",
            relative_path="assets/sample.png",
        )

        if Image is not None:
            # When FastMCP Image is available, it should return an Image instance
            self.assertIsInstance(media_res, Image)
            # Verify internal mime type
            content = media_res.to_image_content()
            self.assertEqual(content.type, "image")
            self.assertEqual(content.mimeType, "image/png")
            self.assertEqual(content.data, png_1x1_b64)
        else:
            # Fallback to dictionary
            self.assertEqual(media_res["status"], "success")
            self.assertEqual(media_res["mime_type"], "image/png")
            self.assertEqual(media_res["content_base64"], png_1x1_b64)

        # Alias read_image_file should behave identically
        img_res = self.tools["read_image_file"](
            project_id="test_proj",
            relative_path="assets/sample.png",
        )
        if Image is not None:
            self.assertIsInstance(img_res, Image)
        else:
            self.assertEqual(img_res["status"], "success")

    # --------------------------------------------------------------------------
    # 3. Cloudflare Tunnel Health & Self-Healing
    # --------------------------------------------------------------------------
    def test_check_tunnel_health_mocked(self) -> None:
        # Mock successful 200 OK health response
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.status = 200
        with patch("urllib.request.urlopen", return_value=mock_resp):
            healthy = check_tunnel_health("https://example.com")
            self.assertTrue(healthy)

        # Mock network failure
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            unhealthy = check_tunnel_health("https://unreachable.invalid")
            self.assertFalse(unhealthy)

    def test_tunnel_heartbeat_monitor_self_healing(self) -> None:
        healed_flag = []

        def mock_heal_callback():
            healed_flag.append(True)

        monitor = TunnelHeartbeatMonitor(
            public_url="https://mock.invalid",
            check_interval=10.0,
            max_consecutive_failures=2,
            on_heal=mock_heal_callback,
        )

        # Simulate probe returning failure
        with patch("mcp_server_core.tunnel.check_tunnel_health", return_value=False):
            with patch("mcp_server_core.tunnel.restart_tunnel", return_value=True):
                # 1st failure
                res1 = monitor.probe_now()
                self.assertFalse(res1)
                self.assertEqual(monitor.consecutive_failures, 1)
                self.assertEqual(len(healed_flag), 0)

                # 2nd failure triggers self heal
                res2 = monitor.probe_now()
                self.assertFalse(res2)
                self.assertEqual(monitor.consecutive_failures, 2)
                if monitor.consecutive_failures >= monitor.max_consecutive_failures:
                    monitor.trigger_self_heal()
                self.assertEqual(len(healed_flag), 1)
                self.assertEqual(monitor.consecutive_failures, 0)


if __name__ == "__main__":
    unittest.main()

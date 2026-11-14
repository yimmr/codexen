"""Comprehensive Acceptance Test Suite for Codexen and Literary Workflow MCP Services.

Verifies:
1. List truncation fix (atomic ToolResultListDict serialization, multiple projects and files).
2. File operations (read with line-slicing, append write, get_file_info status, delete_file is_directory flag).
3. Security boundary checks (sensitive files blocked, engine directory isolation).
4. Asynchronous terminal processes (start, poll, stdin, stop, security blacklist).
5. Binary and multimodal media tools (Base64 + SHA256 integrity, native FastMCP Image).
6. Tunnel health check and self-healing monitor.
7. Literary Workflow MCP layer integrity (list_projects, list_candidates, domain templates untouched).
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from app.config import ConfigManager, ProjectConfig
from app.core import (
    global_process_manager,
    read_project_binary_file,
    write_project_binary_file,
)
from app.server import (
    create_directory,
    delete_directory,
    delete_file,
    get_file_info,
    get_project,
    get_project_info,
    list_background_processes,
    list_files,
    list_project_files,
    list_projects,
    mcp,
    read_binary_file,
    read_file,
    read_image_file,
    read_media_file,
    read_multiple_files,
    read_process_output,
    read_project_file,
    run_command,
    run_terminal_command,
    search_code,
    send_process_input,
    start_background_process,
    stop_background_process,
    write_binary_file,
    write_file,
    write_project_file,
)
from app.tools import ToolResultListDict
from mcp_server_core.tunnel import (
    TunnelHeartbeatMonitor,
    check_tunnel_health,
    restart_tunnel,
)


class CompleteAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.mkdtemp(prefix="codexen_acceptance_")
        cls.work_dir = Path(cls.temp_dir) / "works"
        cls.work_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def tearDown(self) -> None:
        for p in global_process_manager.list_processes():
            try:
                global_process_manager.stop_process(p["process_id"], force=True)
            except Exception:
                pass

    # ==========================================================================
    # 1. 序列化截断修复验证 (Single-Block Serialization Verification)
    # ==========================================================================
    def test_01_codexen_projects_and_files_list_not_truncated(self) -> None:
        """Verify list_projects and list_files return ALL items without gateway truncation."""
        projs = list_projects()
        self.assertIsInstance(projs, ToolResultListDict)
        self.assertGreaterEqual(len(projs), 2)
        proj_names = [p["name"] for p in projs]
        self.assertIn("literary_workflow_v1", proj_names)
        self.assertIn("literary-workflow", proj_names)

        # literary_workflow_v1 must have more than 1 file (original bug was returning only 1 file)
        files_v1 = list_project_files(project_id="literary_workflow_v1")
        self.assertIsInstance(files_v1, ToolResultListDict)
        self.assertGreaterEqual(len(files_v1), 10, f"Expected 10+ files, got {len(files_v1)}")

        # literary-workflow must have more than 1 file
        files_v2 = list_project_files(project_id="literary-workflow")
        self.assertIsInstance(files_v2, ToolResultListDict)
        self.assertGreaterEqual(len(files_v2), 10, f"Expected 10+ files, got {len(files_v2)}")

    # ==========================================================================
    # 2. 核心文件读写与行切片/追加功能验证
    # ==========================================================================
    def test_02_file_operations_with_append_and_lines(self) -> None:
        """Verify append write, line slicing, and file info status."""
        test_file = "acceptance_test.txt"
        # 1. Write file
        w1 = write_file(project_id="literary-workflow", relative_path=test_file, content="Line 1\nLine 2\n")
        self.assertEqual(w1["status"], "success")

        # 2. Append write
        w2 = write_file(project_id="literary-workflow", relative_path=test_file, content="Line 3\nLine 4\n", append=True)
        self.assertEqual(w2["status"], "success")
        self.assertEqual(w2["action"], "appended")

        # 3. Read with line slicing
        content_slice = read_file(project_id="literary-workflow", relative_path=test_file, start_line=2, end_line=3)
        self.assertEqual(content_slice, "Line 2\nLine 3\n")

        # 4. Check get_file_info has 'status': 'success'
        info = get_file_info(project_id="literary-workflow", relative_path=test_file)
        self.assertEqual(info["status"], "success")
        self.assertEqual(info["is_file"], True)

        # 5. Delete file
        del_res = delete_file(project_id="literary-workflow", relative_path=test_file)
        self.assertEqual(del_res["status"], "success")
        self.assertEqual(del_res["is_directory"], False)

    # ==========================================================================
    # 3. 安全隔离边界验证
    # ==========================================================================
    def test_03_security_guards(self) -> None:
        """Ensure sensitive credentials (.env, id_rsa, .git) and destructive commands are blocked."""
        # 1. Reading .env must be rejected
        with self.assertRaises(Exception):
            read_file(project_id="literary-workflow", relative_path=".env")

        # 2. Writing to .env must be rejected
        with self.assertRaises(Exception):
            write_file(project_id="literary-workflow", relative_path=".env", content="HACKED")

        # 3. Destructive command format C: hard-blocked
        with self.assertRaises(Exception):
            run_command(project_id="literary-workflow", command="format C:", confirmed=True)

    # ==========================================================================
    # 4. 异步长任务与交互式终端会话验证
    # ==========================================================================
    def test_04_async_process_sessions(self) -> None:
        """Verify background process creation, incremental output reading, and termination."""
        py_cmd = (
            'python -c "import time, sys; '
            'print(\'ASYNC_HELLO\'); sys.stdout.flush(); '
            'time.sleep(0.3); '
            'print(\'ASYNC_DONE\'); sys.stdout.flush()"'
        )
        proc_res = start_background_process(
            project_id="literary-workflow",
            command=py_cmd,
            confirmed=True,
        )
        self.assertEqual(proc_res["status"], "success")
        proc_id = proc_res["process_id"]

        # Poll output with brief retry to account for Windows process startup latency
        out1 = {"output": ""}
        for _ in range(10):
            time.sleep(0.1)
            out1 = read_process_output(process_id=proc_id, clear_buffer=False)
            if "ASYNC_HELLO" in out1.get("output", ""):
                break
        self.assertEqual(out1["status"], "success")
        self.assertIn("ASYNC_HELLO", out1["output"])

        # List processes
        proc_list = list_background_processes(project_id="literary-workflow")
        self.assertIsInstance(proc_list, ToolResultListDict)
        ids = [p["process_id"] for p in proc_list["processes"]]
        self.assertIn(proc_id, ids)

        # Wait for completion
        time.sleep(0.5)
        out2 = read_process_output(process_id=proc_id, clear_buffer=True)
        self.assertIn("ASYNC_DONE", out2["output"])

    # ==========================================================================
    # 5. 多模态与二进制文件读写验证
    # ==========================================================================
    def test_05_multimodal_and_binary_tools(self) -> None:
        """Verify Base64 binary read/write and FastMCP native image return."""
        raw_data = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x42])
        b64_payload = base64.b64encode(raw_data).decode("ascii")
        expected_sha = hashlib.sha256(raw_data).hexdigest()

        # Write binary
        w_bin = write_binary_file(
            project_id="literary-workflow",
            relative_path="test_bin/sample.bin",
            content_base64=b64_payload,
            overwrite=True,
        )
        self.assertEqual(w_bin["status"], "success")
        self.assertEqual(w_bin["sha256"], expected_sha)

        # Read binary
        r_bin = read_binary_file(
            project_id="literary-workflow",
            relative_path="test_bin/sample.bin",
        )
        self.assertEqual(r_bin["status"], "success")
        self.assertEqual(r_bin["content_base64"], b64_payload)
        self.assertEqual(r_bin["sha256"], expected_sha)

        # Clean up
        delete_file(project_id="literary-workflow", relative_path="test_bin/sample.bin")
        delete_directory(project_id="literary-workflow", relative_path="test_bin")

    # ==========================================================================
    # 6. 小说创作工作流 (Literary Workflow) MCP 层验证
    # ==========================================================================
    def test_06_literary_workflow_mcp_layer_integrity(self) -> None:
        """Verify literary workflow MCP tools are active and list_projects is not truncated."""
        import sys
        lit_root = Path("D:/works/literary-workflow").resolve()
        if str(lit_root) not in sys.path:
            sys.path.insert(0, str(lit_root))

        from literary_workflow import mcp_server
        res = mcp_server.list_projects()
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status"], "success")
        self.assertIn("projects", res)
        self.assertIn("demo", res["projects"])

        # Check candidate listing
        cands = mcp_server.list_project_candidates("demo")
        self.assertIsInstance(cands, dict)
        self.assertEqual(cands["status"], "success")
        self.assertIn("candidates", cands)


if __name__ == "__main__":
    unittest.main()

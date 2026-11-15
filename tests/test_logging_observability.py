"""Tests for Codexen logging, observability, payload hooks, and end-to-end tracing."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

from mcp_server_core.logging import (
    LoggingConfig,
    extract_or_generate_trace_id,
    get_log_manager,
    setup_logging,
)
from codexen.config import ConfigManager
from codexen.core.gitignore import log_penetration_audit
from codexen.server import (
    build_starlette_app,
    codexen_payload_hook,
    create_http_server,
)


class TestCodexenLoggingConfig(unittest.TestCase):
    def test_config_yaml_custom_logging_parsing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_file = base / "config.yaml"
            cfg_data = {
                "workspace": {"root": "D:/works_temp_test" if os.name == "nt" else "/tmp/works_test"},
                "logging": {
                    "level": "DEBUG",
                    "dir": ".runtime/custom_logs",
                    "filenames": {
                        "app": "codexen_engine.log",
                        "access": "cf_ingress.log",
                        "error": "crash.log",
                    },
                    "max_bytes": 20 * 1024 * 1024,
                    "backup_count": 10,
                    "enable_console": False,
                },
                "projects": {},
            }
            cfg_file.write_text(yaml.safe_dump(cfg_data), encoding="utf-8")

            cm = ConfigManager(config_path=cfg_file, base_dir=base)
            self.assertIsNotNone(cm.logging_config)
            self.assertEqual(cm.logging_config.log_dir, ".runtime/custom_logs")
            self.assertEqual(cm.logging_config.app_filename, "codexen_engine.log")
            self.assertEqual(cm.logging_config.access_filename, "cf_ingress.log")
            self.assertEqual(cm.logging_config.error_filename, "crash.log")
            self.assertEqual(cm.logging_config.level, "DEBUG")
            self.assertEqual(cm.logging_config.max_bytes, 20 * 1024 * 1024)
            self.assertEqual(cm.logging_config.backup_count, 10)
            self.assertFalse(cm.logging_config.enable_console)

    def test_env_override_takes_precedence_over_yaml(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_file = base / "config.yaml"
            cfg_data = {
                "workspace": {"root": "D:/works_temp_test" if os.name == "nt" else "/tmp/works_test"},
                "logging": {
                    "level": "INFO",
                    "dir": ".runtime/yaml_logs",
                    "filenames": {"app": "yaml_app.log"},
                },
                "projects": {},
            }
            cfg_file.write_text(yaml.safe_dump(cfg_data), encoding="utf-8")

            with patch.dict(os.environ, {
                "MCP_LOG_LEVEL": "WARNING",
                "MCP_LOG_DIR": ".runtime/env_override_logs",
                "MCP_LOG_APP_FILE": "env_override.log",
            }):
                cm = ConfigManager(config_path=cfg_file, base_dir=base)
                self.assertEqual(cm.logging_config.log_dir, ".runtime/env_override_logs")
                self.assertEqual(cm.logging_config.app_filename, "env_override.log")
                self.assertEqual(cm.logging_config.level, "WARNING")


class TestCodexenPayloadHook(unittest.TestCase):
    def test_write_file_large_content_truncation(self):
        large_code = "def complex_function():\n    print('hello')\n" * 50
        args = {"project_id": "demo", "file_path": "script.py", "content": large_code}
        hooked = codexen_payload_hook("write_file", args)
        self.assertIsNotNone(hooked)
        self.assertIn("[file_content:", hooked["content"])
        self.assertIn(f"{len(large_code)} chars", hooked["content"])
        self.assertEqual(hooked["file_path"], "script.py")

    def test_write_file_small_content_intact(self):
        small_code = "print('hello')"
        args = {"project_id": "demo", "file_path": "small.py", "content": small_code}
        hooked = codexen_payload_hook("write_file", args)
        # Unchanged when under 300 characters
        self.assertIsNone(hooked)

    def test_write_binary_file_payload_hook(self):
        base64_str = "A" * 500
        args = {"project_id": "demo", "file_path": "image.png", "content_base64": base64_str}
        hooked = codexen_payload_hook("write_binary_file", args)
        self.assertIsNotNone(hooked)
        self.assertIn("[base64_data: 500 chars]", hooked["content_base64"])

    def test_edit_file_snippet_hook(self):
        big_block = "line\n" * 80
        args = {
            "project_id": "demo",
            "file_path": "test.txt",
            "search_block": big_block,
            "replacement_block": "short_replacement",
        }
        hooked = codexen_payload_hook("edit_file_snippet", args)
        self.assertIsNotNone(hooked)
        self.assertIn("[snippet:", hooked["search_block"])


class TestEndToEndObservabilityAndTracing(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_http_cf_ray_tracing_and_log_separation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_file = base / "config.yaml"
            proj_dir = base / "test_project"
            proj_dir.mkdir(parents=True, exist_ok=True)
            (proj_dir / "sample.txt").write_text("Hello Observability!", encoding="utf-8")

            cfg_data = {
                "workspace": {"root": str(base / "workspace")},
                "http": {"require_auth": False},
                "logging": {
                    "level": "INFO",
                    "dir": ".runtime/logs",
                    "filenames": {
                        "app": "codexen.log",
                        "access": "access.log",
                        "error": "error.log",
                    },
                    "enable_console": False,
                },
                "projects": {
                    "demo_proj": {
                        "root": str(proj_dir),
                        "description": "Tracing test project",
                    }
                },
            }
            cfg_file.write_text(yaml.safe_dump(cfg_data), encoding="utf-8")

            # Initialize ConfigManager and create HTTP server
            cm = ConfigManager(config_path=cfg_file, base_dir=base)
            server, cm, oauth_server = create_http_server(root=base, config_mgr=cm)

            # Setup logging for this base directory
            log_mgr = setup_logging(
                base_dir=base,
                config=cm.logging_config,
                service_name="codexen",
            )

            # Build Starlette ASGI app
            app = build_starlette_app(
                server=server,
                config_mgr=cm,
                oauth_server=oauth_server,
                require_auth=False,
            )

            test_ray_id = "8e1a2b3c4d-HKG"
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as client:
                    # 1. Normal HTTP health check with cf-ray
                    resp = await client.get("/health", headers={"cf-ray": test_ray_id})
                    self.assertEqual(resp.status_code, 200)
                    self.assertEqual(resp.headers.get("x-trace-id"), test_ray_id)
                    self.assertEqual(resp.headers.get("cf-ray"), test_ray_id)

                    # 2. Invoke a tool via FastMCP server call_tool
                    call_res = await server.call_tool("read_file", {
                        "project_id": "demo_proj",
                        "relative_path": "sample.txt",
                    })
                    self.assertIsNotNone(call_res)

                    # 3. Simulate an error: reading a non-existent file
                    with self.assertRaises(Exception) as err_ctx:
                        await server.call_tool("read_file", {
                            "project_id": "demo_proj",
                            "relative_path": "does_not_exist_file.txt",
                        })

                    err_msg = str(err_ctx.exception)
                    # Verify that error returned to client contains trace_id
                    self.assertTrue("trace_" in err_msg or "trace_id" in err_msg)

                # Flush logs
                for h in log_mgr._handlers:
                    h.flush()

                # Verify files exist in .runtime/logs/
                logs_dir = base / ".runtime" / "logs"
                access_log = logs_dir / "access.log"
                app_log = logs_dir / "codexen.log"
                error_log = logs_dir / "error.log"

                self.assertTrue(access_log.exists(), "access.log must exist")
                self.assertTrue(app_log.exists(), "codexen.log must exist")
                self.assertTrue(error_log.exists(), "error.log must exist")

                access_content = access_log.read_text(encoding="utf-8")
                app_content = app_log.read_text(encoding="utf-8")
                error_content = error_log.read_text(encoding="utf-8")

                # Verify Access Stream has CF-Ray
                self.assertIn(f"[trace_id: {test_ray_id}]", access_content)
                self.assertIn('"GET /health HTTP/1.1" 200', access_content)

                # Verify App Stream has tool call
                self.assertIn("Tool execution started: read_file", app_content)
                self.assertIn('"sample.txt"', app_content)
                self.assertIn("Tool execution completed: read_file", app_content)

                # Verify Error Stream has traceback and error record
                self.assertIn("Tool execution failed: read_file", error_content)
                self.assertIn("does_not_exist_file.txt", error_content)

            finally:
                log_mgr.close()

    async def test_end_to_end_real_mcp_transport_cf_ray_and_error_trace(self):
        """P1-1 / P1-2 / R-1 / R-2 / R-9: Full MCP Streamable HTTP transport test with distinct CF-Rays."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_file = base / "config.yaml"
            proj_dir = base / "test_workspace"
            proj_dir.mkdir(parents=True, exist_ok=True)
            (proj_dir / "sample.txt").write_text("Hello from Codexen live transport!", encoding="utf-8")

            cfg_data = {
                "workspace": {"root": str(base / "workspace")},
                "http": {"require_auth": False, "port": 8788},
                "logging": {
                    "dir": ".runtime/logs",
                    "filenames": {"app": "codexen_service.log"},
                    "enable_console": False,
                },
                "projects": {
                    "demo_proj": {
                        "root": str(proj_dir),
                        "description": "Live transport project",
                    }
                },
            }
            cfg_file.write_text(yaml.safe_dump(cfg_data), encoding="utf-8")

            cm = ConfigManager(config_path=cfg_file, base_dir=base)
            server, cm, oauth_server = create_http_server(root=base, config_mgr=cm)

            log_mgr = setup_logging(
                base_dir=base,
                config=cm.logging_config,
                service_name="codexen_service",
            )

            app = build_starlette_app(
                server=server,
                config_mgr=cm,
                oauth_server=oauth_server,
                require_auth=False,
            )

            headers_common = {"accept": "application/json, text/event-stream"}

            try:
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8788"
                    ) as client:
                        # 1. MCP Initialize with ray-init-CODEXEN
                        res_init = await client.post(
                            "/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": "init-1",
                                "method": "initialize",
                                "params": {
                                    "protocolVersion": "2024-11-05",
                                    "capabilities": {},
                                    "clientInfo": {"name": "test-client", "version": "1.0"},
                                },
                            },
                            headers={**headers_common, "cf-ray": "ray-init-CODEXEN"},
                        )
                        self.assertEqual(res_init.status_code, 200)
                        session_id = res_init.headers.get("mcp-session-id")
                        self.assertIsNotNone(session_id)

                        # 2. Initialized notification with ray-notif-CODEXEN
                        res_notif = await client.post(
                            "/mcp",
                            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                            headers={**headers_common, "mcp-session-id": session_id, "cf-ray": "ray-notif-CODEXEN"},
                        )
                        self.assertEqual(res_notif.status_code, 202)

                        # 3. Successful tool call with ray-read-SUCCESS
                        res_call1 = await client.post(
                            "/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": "call-1",
                                "method": "tools/call",
                                "params": {
                                    "name": "read_file",
                                    "arguments": {"project_id": "demo_proj", "relative_path": "sample.txt"},
                                },
                            },
                            headers={**headers_common, "mcp-session-id": session_id, "cf-ray": "ray-read-SUCCESS"},
                        )
                        self.assertEqual(res_call1.status_code, 200)
                        self.assertIn("Hello from Codexen live transport!", res_call1.text)

                        # 4. Error tool call with ray-err-FAILURE
                        res_call2 = await client.post(
                            "/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": "call-2",
                                "method": "tools/call",
                                "params": {
                                    "name": "read_file",
                                    "arguments": {"project_id": "demo_proj", "relative_path": "missing_file.txt"},
                                },
                            },
                            headers={**headers_common, "mcp-session-id": session_id, "cf-ray": "ray-err-FAILURE"},
                        )
                        self.assertEqual(res_call2.status_code, 200)

                        call2_body = res_call2.text
                        self.assertIn("isError", call2_body)
                        self.assertIn("trace_id: ray-err-FAILURE", call2_body)
                        self.assertNotIn("ray-init-CODEXEN", call2_body)

                for h in log_mgr._handlers:
                    h.flush()

                logs_dir = base / ".runtime" / "logs"
                access_txt = (logs_dir / "access.log").read_text(encoding="utf-8")
                app_txt = (logs_dir / "codexen_service.log").read_text(encoding="utf-8")
                error_txt = (logs_dir / "error.log").read_text(encoding="utf-8")

                # Verify each request logged its own ray ID in access.log
                self.assertIn("[trace_id: ray-init-CODEXEN]", access_txt)
                self.assertIn("[trace_id: ray-notif-CODEXEN]", access_txt)
                self.assertIn("[trace_id: ray-read-SUCCESS]", access_txt)
                self.assertIn("[trace_id: ray-err-FAILURE]", access_txt)

                # Verify tool execution logs tied to respective request ray IDs
                self.assertIn("Tool execution started: read_file", app_txt)
                self.assertIn("[trace_id: ray-read-SUCCESS]", app_txt)
                self.assertIn("Tool execution completed: read_file", app_txt)

                # Verify error log tied to ray-err-FAILURE and contains stacktrace
                self.assertIn("Tool execution failed: read_file", error_txt)
                self.assertIn("[trace_id: ray-err-FAILURE]", error_txt)
                self.assertIn("missing_file.txt", error_txt)
                self.assertNotIn("[trace_id: ray-init-CODEXEN] Tool execution failed", error_txt)

            finally:
                log_mgr.close()

    def test_production_entrypoint_config_yaml_logging_wiring(self):
        """P1-3 / R-3: Verify production runner receives config.yaml logging."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            cfg_file = base / "config.yaml"
            cfg_data = {
                "workspace": {"root": str(base / "workspace")},
                "http": {"require_auth": False, "port": 8788},
                "logging": {
                    "dir": ".runtime/custom_entry_logs",
                    "filenames": {"app": "custom_entry.log"},
                    "enable_console": False,
                },
                "projects": {},
            }
            cfg_file.write_text(yaml.safe_dump(cfg_data), encoding="utf-8")

            cm = ConfigManager(config_path=cfg_file, base_dir=base)
            self.assertEqual(cm.http_settings.logging_config.log_dir, ".runtime/custom_entry_logs")
            self.assertEqual(cm.http_settings.logging_config.app_filename, "custom_entry.log")

            # Setup logging directly from http_settings.logging_config (as production runner does)
            mgr = setup_logging(base_dir=base, config=cm.http_settings.logging_config, service_name="test_prod")
            try:
                app_lg = mgr.get_app_logger()
                app_lg.info("Production test log message")
                for h in mgr._handlers:
                    h.flush()

                custom_log = base / ".runtime" / "custom_entry_logs" / "custom_entry.log"
                self.assertTrue(custom_log.exists(), f"{custom_log} must exist")
                self.assertIn("Production test log message", custom_log.read_text(encoding="utf-8"))
            finally:
                mgr.close()

    def test_audit_log_backward_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            # Log penetration audit
            log_penetration_audit(
                project_name="demo",
                requested_path="secret/.env",
                status="blocked",
                base_dir=base,
            )
            audit_file = base / ".runtime" / "audit.log"
            self.assertTrue(audit_file.exists())
            content = audit_file.read_text(encoding="utf-8")
            self.assertIn("penetration_audit", content)
            self.assertIn("secret/.env", content)
            self.assertIn("blocked", content)


class TestZeroPollution(unittest.TestCase):
    def test_zero_os_global_pollution(self):
        """Confirm that during standard lifecycle, zero files are created in OS global directories."""
        local_appdata = os.getenv("LOCALAPPDATA")
        if local_appdata:
            codexen_appdata = Path(local_appdata) / "Codexen"
            # Codexen should never create a log directory in LOCALAPPDATA
            if codexen_appdata.exists():
                logs = list(codexen_appdata.glob("**/*.log"))
                self.assertEqual(len(logs), 0, f"Found unintended logs in LOCALAPPDATA: {logs}")


if __name__ == "__main__":
    unittest.main()

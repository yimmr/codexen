"""Codexen MCP Engine Launcher.

Handles single-instance check, tunnel initialization, server startup, and clean shutdown.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Force UTF-8 encoding for console output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from mcp_framework.env import DeploymentEnvConfig, get_runtime_dir
from mcp_framework.process import check_service_status, stop_mcp_service

ROOT_DIR = Path(__file__).resolve().parent
os.chdir(ROOT_DIR)

RUNTIME_DIR = get_runtime_dir(ROOT_DIR)

try:
    _env_cfg = DeploymentEnvConfig.load(ROOT_DIR)
    PORT = _env_cfg.port
    SECRET_KEY = _env_cfg.api_key or "未配置"
    PUBLIC_ENDPOINT = _env_cfg.upstream_url or f"http://127.0.0.1:{PORT}/mcp"
except Exception:
    PORT = 8788
    SECRET_KEY = os.getenv("MCP_API_KEY", "")
    PUBLIC_ENDPOINT = os.getenv("TUNNEL_PUBLIC_URL", f"http://127.0.0.1:{PORT}/mcp")

LOCAL_ENDPOINT = f"http://127.0.0.1:{PORT}/mcp"
MUTEX_NAME = f"Local\\Codexen_MCP_Tray_App_{PORT}"


def stop_server() -> None:
    print(f"[*] 正在停止 Codexen MCP 服务 (端口 {PORT})...")
    res = stop_mcp_service(PORT, ROOT_DIR)
    if res.get("killed_pids"):
        print(f"[SUCCESS] Codexen MCP 服务已成功停止 (已终结进程: {res['killed_pids']})！")
    else:
        print(f"[INFO] 未检测到端口 {PORT} 上正在运行的 Codexen 服务。")


def main() -> None:
    if "--stop" in sys.argv:
        stop_server()
        sys.exit(0)

    is_running, mode_desc, running_pid = check_service_status(PORT, ROOT_DIR, MUTEX_NAME)
    if is_running:
        print("\n" + "=" * 70)
        print("  【提示】Codexen MCP 服务已在运行中（或正在启动），请勿重复启动！")
        print("=" * 70)
        print(f"  * 运行模式:       {mode_desc}")
        if running_pid:
            print(f"  * 进程 PID:       {running_pid}")
        print(f"  * 专属公网端点:   {PUBLIC_ENDPOINT}")
        print(f"  * 安全访问密钥:   {SECRET_KEY}")
        print(f"  * 本地监听地址:   {LOCAL_ENDPOINT}")
        print("-" * 70)
        print("  如需停止服务，请直接运行 stop.bat 或右键右下角托盘图标退出")
        print("=" * 70 + "\n")
        try:
            input("按 Enter 键退出...")
        except Exception:
            pass
        sys.exit(0)

    # 1. Ensure shared tunnel is active & start self-healing heartbeat monitor
    print("[*] 检查并确保共享穿透隧道 (mcp-tunnel) 在线...")
    try:
        from mcp_framework.tunnel import ensure_tunnel_running, start_tunnel_heartbeat_monitor
        ensure_tunnel_running()
        if PUBLIC_ENDPOINT:
            start_tunnel_heartbeat_monitor(public_url=PUBLIC_ENDPOINT, check_interval=30.0)
    except Exception as e:
        print(f"[!] 隧道自检提示: {e}")

    if "--test" in sys.argv:
        print("[OK] Codexen MCP launcher pre-flight check passed!")
        sys.exit(0)

    # 2. Run Codexen server
    print(f"[*] 正在启动 Codexen MCP 核心引擎 (端口 {PORT})...\n")
    from app.__main__ import main as run_app_main
    sys.argv = [sys.argv[0], "--no-tunnel"] + sys.argv[1:]
    run_app_main()


if __name__ == "__main__":
    main()

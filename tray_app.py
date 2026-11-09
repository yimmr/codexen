"""System Tray Manager for Codexen MCP Engine."""
from __future__ import annotations

import atexit
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

# Windows GBK safety & High-DPI Awareness
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        import ctypes
        try:
            # Per-Monitor High DPI Awareness (Windows 8.1+)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            # System DPI Awareness (Windows Vista+)
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as item
import win32api
import win32clipboard
import win32con
import win32event
import win32job
import winerror

from mcp_framework.tunnel import ensure_tunnel_running, stop_tunnel_if_no_mcp
from mcp_framework.env import DeploymentEnvConfig, get_runtime_dir, update_env_variable
from mcp_framework.process import is_port_in_use, kill_stale_server_process

SERVICE_NAME = "Codexen 项目开发引擎"
BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = get_runtime_dir(BASE_DIR)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def get_current_env():
    try:
        return DeploymentEnvConfig.load(BASE_DIR)
    except Exception:
        return None


def get_port() -> int:
    cfg = get_current_env()
    if cfg and cfg.port:
        return cfg.port
    return 8788


def get_api_key() -> str:
    cfg = get_current_env()
    if cfg and cfg.api_key:
        return cfg.api_key
    return os.getenv("MCP_API_KEY", "")


def get_public_url() -> str:
    cfg = get_current_env()
    if cfg and cfg.upstream_url:
        return cfg.upstream_url
    return os.getenv("TUNNEL_PUBLIC_URL", f"http://127.0.0.1:{get_port()}/mcp")


PORT = get_port()
MUTEX_NAME = f"Local\\Codexen_MCP_Tray_App_{PORT}"
TRAY_PID_FILE = RUNTIME_DIR / ".tray.pid"


def get_server_python_exe() -> str:
    """Ensure the child server runs with python.exe (with CREATE_NO_WINDOW) rather than pythonw.exe,
    so that uvicorn/anyio/logging streams remain valid and never abort.
    """
    py_exe = sys.executable
    if py_exe.lower().endswith("pythonw.exe"):
        cand = py_exe[:-5] + ".exe"
        if os.path.exists(cand):
            return cand
    return py_exe



def record_tray_pid() -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        TRAY_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def cleanup_tray_pid() -> None:
    for t_file in [TRAY_PID_FILE, BASE_DIR / ".tray.pid"]:
        try:
            if t_file.exists():
                t_file.unlink()
        except Exception:
            pass


def ensure_single_instance():
    """Ensure only one tray app instance runs at a time using a Windows Named Mutex."""
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        try:
            win32api.MessageBox(
                0,
                f"【提示】{SERVICE_NAME} 已在系统托盘运行中！\n\n请在任务栏右下角通知区域查看图标。\n如需停止，可右键托盘图标退出或运行 stop.bat。",
                SERVICE_NAME,
                win32con.MB_ICONINFORMATION | win32con.MB_OK | win32con.MB_SYSTEMMODAL,
            )
        except Exception:
            pass
        sys.exit(0)
    return mutex


def copy_to_clipboard(text: str) -> None:
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
    except Exception as e:
        print("Clipboard error:", e)


def choose_folder() -> str | None:
    """Open native high-DPI folder browser dialog to choose a directory."""
    if sys.platform == "win32":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        # Set custom application icon on parent window to replace Tkinter feather
        ico_path = RUNTIME_DIR / "app.ico"
        if not ico_path.exists():
            try:
                create_tray_icon().save(ico_path, format="ICO")
            except Exception:
                pass
        if ico_path.exists():
            try:
                root.iconbitmap(str(ico_path))
            except Exception:
                pass

        folder = filedialog.askdirectory(title="选择要添加的项目文件夹", parent=root)
        root.destroy()
        if folder:
            return folder
    except Exception:
        pass

    # Fallback to PowerShell FolderBrowserDialog
    try:
        ps_cmd = (
            'Add-Type -AssemblyName System.Windows.Forms; '
            '$fb = New-Object System.Windows.Forms.FolderBrowserDialog; '
            '$fb.Description = "选择要添加的项目文件夹"; '
            '$fb.ShowNewFolderButton = $true; '
            'if ($fb.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { '
            '    Write-Output $fb.SelectedPath '
            '}'
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            creationflags=creationflags,
        )
        out = res.stdout.strip()
        return out if out else None
    except Exception:
        return None


def create_tray_icon() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Teal/Emerald circle
    draw.ellipse((4, 4, 60, 60), fill="#00b894")
    # Draw letter 'C' in the center
    try:
        font = ImageFont.truetype("arial.ttf", 36)
        draw.text((18, 12), "C", fill="white", font=font)
    except Exception:
        draw.text((22, 16), "C", fill="white")
    return img


class ServerManager:
    def __init__(self, port: int, cwd: Path):
        self.port = port
        self.cwd = cwd
        self.runtime_dir = get_runtime_dir(cwd)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen | None = None
        self.job = None
        self.status_text = f"● 【运行中】127.0.0.1:{self.port}"
        self._restarting = False
        self._init_job_object()

    def _init_job_object(self) -> None:
        try:
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            self.job = job
        except Exception:
            self.job = None

    def kill_stale_server_only(self) -> None:
        """Kill only the local MCP server on this port without stopping the tunnel."""
        kill_stale_server_process(self.port, self.cwd)
        time.sleep(0.3)

    def start(self) -> None:
        self.kill_stale_server_only()
        ensure_tunnel_running()

        py_exe = get_server_python_exe()
        cmd = [py_exe, "-m", "app", "--no-tunnel"]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(self.runtime_dir / "server_error.log", "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.cwd),
            creationflags=creationflags,
            env=env,
            stdout=log_file,
            stderr=log_file,
        )

        if self.job and self.proc and self.proc._handle:
            try:
                win32job.AssignProcessToJobObject(self.job, self.proc._handle)
            except Exception:
                pass

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self.kill_stale_server_only()
        stop_tunnel_if_no_mcp()

    def restart_async(self, icon: pystray.Icon | None = None) -> None:
        """Restart the local MCP server in a background thread without freezing the tray UI."""
        if self._restarting:
            return
        threading.Thread(target=self._do_restart, args=(icon,), daemon=True).start()

    def _do_restart(self, icon: pystray.Icon | None = None) -> None:
        self._restarting = True
        self.status_text = f"● 【重启中...】127.0.0.1:{self.port}"
        if icon:
            try:
                icon.update_menu()
                icon.notify("正在重启 MCP 服务，请稍候...", SERVICE_NAME)
            except Exception:
                pass

        try:
            # 1. Stop only the child server process (keep Cloudflare Tunnel active!)
            if self.proc:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=2)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                self.proc = None

            self.kill_stale_server_only()
            ensure_tunnel_running()

            # 2. Spawn new child server
            py_exe = get_server_python_exe()
            cmd = [py_exe, "-m", "app", "--no-tunnel"]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            log_file = open(self.runtime_dir / "server_error.log", "w", encoding="utf-8")
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(self.cwd),
                creationflags=creationflags,
                env=env,
                stdout=log_file,
                stderr=log_file,
            )

            if self.job and self.proc and self.proc._handle:
                try:
                    win32job.AssignProcessToJobObject(self.job, self.proc._handle)
                except Exception:
                    pass

            # 3. Health verification: poll /health up to 10 seconds
            healthy = False
            for _ in range(20):
                time.sleep(0.5)
                try:
                    req = urllib.request.Request(f"http://127.0.0.1:{self.port}/health")
                    with urllib.request.urlopen(req, timeout=1) as resp:
                        if resp.status == 200:
                            healthy = True
                            break
                except Exception:
                    pass

            if healthy:
                self.status_text = f"● 【运行中】127.0.0.1:{self.port}"
                if icon:
                    try:
                        icon.update_menu()
                        icon.notify(f"服务已重启完成并就绪 (端口 {self.port})", SERVICE_NAME)
                    except Exception:
                        pass
            else:
                self.status_text = f"● 【异常】端口 {self.port} 未响应"
                if icon:
                    try:
                        icon.update_menu()
                        icon.notify(f"警告：服务重启超时，健康检查未通过！", SERVICE_NAME)
                    except Exception:
                        pass
        finally:
            self._restarting = False


def main() -> None:
    mutex = ensure_single_instance()

    # If already running in console mode on port 8788, alert and exit cleanly
    if is_port_in_use(PORT):
        try:
            win32api.MessageBox(
                0,
                f"{SERVICE_NAME} 已在运行中 (端口 {PORT} 已被占用)。\n\n如需切换为无黑框托盘常驻模式，请先运行 stop.bat 停止现有服务后再启动。",
                SERVICE_NAME,
                win32con.MB_ICONINFORMATION | win32con.MB_OK,
            )
        except Exception:
            pass
        sys.exit(0)

    record_tray_pid()
    atexit.register(cleanup_tray_pid)

    srv = ServerManager(PORT, BASE_DIR)
    srv.start()

    def on_add_project(icon, _):
        def _worker():
            selected = choose_folder()
            if not selected:
                return

            target_path = Path(selected).resolve()
            if not target_path.exists() or not target_path.is_dir():
                win32api.MessageBox(
                    0,
                    f"选择的路径不存在或不是文件夹：\n{target_path}",
                    SERVICE_NAME,
                    win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL,
                )
                return

            folder_name = target_path.name.strip()
            if not folder_name:
                win32api.MessageBox(
                    0,
                    "无法获取该文件夹名称，请重新选择！",
                    SERVICE_NAME,
                    win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL,
                )
                return

            try:
                from app.config import ConfigManager
                cfg_mgr = ConfigManager(base_dir=BASE_DIR)
                cfg_mgr.register_existing_project(
                    folder_path=target_path,
                    name=folder_name,
                    description=f"本地开发项目 {folder_name}",
                    read_only=False,
                    persist=True,
                )
            except Exception as err:
                win32api.MessageBox(
                    0,
                    f"添加项目未成功 (重复检查或安全拦截)：\n\n{err}",
                    SERVICE_NAME,
                    win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL,
                )
                return

            # Restart child MCP server asynchronously so it loads the new project
            srv.restart_async(icon)

            win32api.MessageBox(
                0,
                f"已成功添加并启用新项目！\n\n"
                f"项目名称: {folder_name}\n"
                f"项目路径: {target_path}\n\n"
                f"服务已自动重启生效，AI Agent 现已可访问并开发此项目。",
                SERVICE_NAME,
                win32con.MB_ICONINFORMATION | win32con.MB_SYSTEMMODAL,
            )

        threading.Thread(target=_worker, daemon=True).start()

    def make_on_remove_project(proj_name: str):
        def on_remove(icon, _):
            def _worker():
                try:
                    from app.config import ConfigManager
                    cfg_mgr = ConfigManager(base_dir=BASE_DIR)
                    if proj_name not in cfg_mgr.projects:
                        win32api.MessageBox(
                            0,
                            f"项目【{proj_name}】当前未在配置列表中，无需移除。",
                            SERVICE_NAME,
                            win32con.MB_ICONINFORMATION | win32con.MB_SYSTEMMODAL,
                        )
                        return
                    proj = cfg_mgr.projects[proj_name]
                    proj_root = str(proj.root)
                except Exception as err:
                    win32api.MessageBox(
                        0,
                        f"读取项目配置失败：\n\n{err}",
                        SERVICE_NAME,
                        win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL,
                    )
                    return

                # Native confirmation dialog to prevent accidental removal
                ret = win32api.MessageBox(
                    0,
                    f"确定要从 Codexen 移除项目【{proj_name}】吗？\n\n"
                    f"项目路径: {proj_root}\n\n"
                    f"安全说明：这仅会解除 MCP 服务的项目映射与访问权限，"
                    f"绝不会删除或修改磁盘上的任何物理代码文件。",
                    f"{SERVICE_NAME} - 移除项目确认",
                    win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL,
                )
                if ret != win32con.IDYES:
                    return

                try:
                    cfg_mgr.unregister_project(proj_name, persist=True)
                except Exception as err:
                    win32api.MessageBox(
                        0,
                        f"移除项目失败：\n\n{err}",
                        SERVICE_NAME,
                        win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL,
                    )
                    return

                # Restart child MCP server asynchronously so it drops the project
                srv.restart_async(icon)

                try:
                    icon.notify(
                        f"已成功移除项目【{proj_name}】\n服务已自动重启生效。",
                        SERVICE_NAME,
                    )
                except Exception:
                    pass

            threading.Thread(target=_worker, daemon=True).start()

        return on_remove

    def get_remove_project_items():
        try:
            from app.config import ConfigManager
            cfg_mgr = ConfigManager(base_dir=BASE_DIR)
            proj_names = sorted(cfg_mgr.projects.keys())
        except Exception:
            proj_names = []

        if not proj_names:
            return [item("(暂无已注册项目)", lambda: None, enabled=False)]

        return [
            item(p_name, make_on_remove_project(p_name))
            for p_name in proj_names
        ]

    def on_copy_url(icon, _):
        url = get_public_url()
        copy_to_clipboard(url)
        try:
            icon.notify("已复制 MCP 服务器网址:\n" + url, SERVICE_NAME)
        except Exception:
            pass

    def on_copy_key(icon, _):
        key = get_api_key()
        copy_to_clipboard(key)
        try:
            icon.notify("已复制访问密匙:\n" + key, SERVICE_NAME)
        except Exception:
            pass

    def on_rotate_key(icon, _):
        new_key = f"sec_{secrets.token_urlsafe(16)}"
        env_file = BASE_DIR / ".env"
        update_env_variable("MCP_API_KEY", new_key, env_file)
        os.environ["MCP_API_KEY"] = new_key
        copy_to_clipboard(new_key)
        try:
            icon.notify(
                f"已生成新访问密匙并复制到剪贴板！\n{new_key}\n正在平滑重启服务以生效...",
                SERVICE_NAME,
            )
        except Exception:
            pass
        srv.restart_async(icon)

    def on_open_health(icon, _):
        webbrowser.open(f"http://127.0.0.1:{PORT}/health")

    def on_restart(icon, _):
        srv.restart_async(icon)

    def on_exit(icon, _):
        def do_exit():
            try:
                icon.title = f"{SERVICE_NAME} (正在退出...)"
                icon.notify(f"{SERVICE_NAME} 正在停止服务，请稍候...", SERVICE_NAME)
            except Exception:
                pass

            try:
                srv.stop()
            except Exception:
                pass

            cleanup_tray_pid()

            try:
                icon.visible = False
                icon.stop()
            except Exception:
                pass

            os._exit(0)

        threading.Thread(target=do_exit, daemon=True).start()

    def get_status_text(item_obj) -> str:
        return srv.status_text

    menu = pystray.Menu(
        item(get_status_text, lambda: None, enabled=False),
        pystray.Menu.SEPARATOR,
        item("添加项目", on_add_project),
        item("移除项目", pystray.Menu(get_remove_project_items)),
        item("复制 MCP 服务器网址", on_copy_url),
        item("复制访问密匙", on_copy_key),
        item("重设访问密匙", on_rotate_key),
        item("健康检查", on_open_health),
        pystray.Menu.SEPARATOR,
        item("重启", on_restart),
        item("退出", on_exit),
    )

    icon = pystray.Icon(
        name="codexen_tray",
        icon=create_tray_icon(),
        title=f"{SERVICE_NAME} (端口 {PORT})",
        menu=menu,
    )

    try:
        icon.run()
    except Exception as e:
        import traceback
        try:
            (RUNTIME_DIR / "tray_debug.log").write_text(f"Exception: {e}\n{traceback.format_exc()}", encoding="utf-8")
        except Exception:
            pass
    finally:
        try:
            (RUNTIME_DIR / "tray_debug.log").write_text("main() finally reached\n", encoding="utf-8")
        except Exception:
            pass
        try:
            srv.stop()
        except Exception:
            pass
        cleanup_tray_pid()
        os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        import traceback
        err_msg = f"{SERVICE_NAME} 托盘启动失败：\n\n{err}\n\n{traceback.format_exc()}"
        try:
            (RUNTIME_DIR / "tray_error.log").write_text(err_msg, encoding="utf-8")
        except Exception:
            pass
        try:
            win32api.MessageBox(
                0,
                err_msg,
                SERVICE_NAME,
                win32con.MB_ICONERROR | win32con.MB_SYSTEMMODAL,
            )
        except Exception:
            pass
        sys.exit(1)


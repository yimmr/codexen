"""System Tray Manager for Codexen MCP Engine."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from pystray import MenuItem as item
import pystray
import win32con

from mcp_server_core.tray import BaseTrayApp
from .config import ConfigManager

BASE_DIR = Path(__file__).resolve().parents[1]


class CodexenTrayApp(BaseTrayApp):
    """Customized System Tray App for Codexen, featuring Project Management."""

    def __init__(self):
        super().__init__(
            service_name="Codexen 项目开发引擎",
            base_dir=BASE_DIR,
            default_port=8788,
            icon_char="C",
            icon_color="#00b894",
            server_module="codexen",
        )

    def build_custom_menu(self) -> list[item]:
        """Inject Codexen-specific project management menu items into the tray."""
        return [
            item("添加项目", self.on_add_project),
            item("移除项目", self.build_remove_project_menu()),
        ]

    def on_add_project(self, icon, _):
        def _worker():
            selected = self.choose_folder(title="选择要添加的项目文件夹")
            if not selected:
                return

            target_path = Path(selected).resolve()
            if not target_path.exists() or not target_path.is_dir():
                self.message_box(
                    f"选择的路径不存在或不是文件夹：\n{target_path}",
                    flags=win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL,
                )
                return

            folder_name = target_path.name.strip()
            if not folder_name:
                self.message_box(
                    "无法获取该文件夹名称，请重新选择！",
                    flags=win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL,
                )
                return

            try:
                cfg_mgr = ConfigManager(base_dir=self.base_dir)
                cfg_mgr.register_existing_project(
                    folder_path=target_path,
                    name=folder_name,
                    description=f"本地开发项目 {folder_name}",
                    read_only=False,
                    persist=True,
                )
            except Exception as err:
                self.message_box(
                    f"添加项目未成功 (重复检查或安全拦截)：\n\n{err}",
                    flags=win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL,
                )
                return

            self.supervisor.restart_async(icon)

            self.message_box(
                f"已成功添加并启用新项目！\n\n"
                f"项目名称: {folder_name}\n"
                f"项目路径: {target_path}\n\n"
                f"服务已自动重启生效，AI Agent 现已可访问并开发此项目。",
                flags=win32con.MB_ICONINFORMATION | win32con.MB_SYSTEMMODAL,
            )

        threading.Thread(target=_worker, daemon=True).start()

    def build_remove_project_menu(self):
        def get_items():
            try:
                cfg_mgr = ConfigManager(base_dir=self.base_dir)
                proj_names = sorted(cfg_mgr.projects.keys())
            except Exception:
                proj_names = []

            if not proj_names:
                return [item("(暂无已注册项目)", lambda: None, enabled=False)]

            return [
                item(p_name, self._make_remove_handler(p_name))
                for p_name in proj_names
            ]

        return pystray.Menu(get_items)

    def _make_remove_handler(self, proj_name: str):
        def on_remove(icon, _):
            def _worker():
                try:
                    cfg_mgr = ConfigManager(base_dir=self.base_dir)
                    if proj_name not in cfg_mgr.projects:
                        self.message_box(
                            f"项目【{proj_name}】当前未在配置列表中，无需移除。",
                            flags=win32con.MB_ICONINFORMATION | win32con.MB_SYSTEMMODAL,
                        )
                        return
                    proj = cfg_mgr.projects[proj_name]
                    proj_root = str(proj.root)
                except Exception as err:
                    self.message_box(
                        f"读取项目配置失败：\n\n{err}",
                        flags=win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL,
                    )
                    return

                ret = self.message_box(
                    f"确定要从 Codexen 移除项目【{proj_name}】吗？\n\n"
                    f"项目路径: {proj_root}\n\n"
                    f"安全说明：这仅会解除 MCP 服务的项目映射与访问权限，"
                    f"绝不会删除或修改磁盘上的任何物理代码文件。",
                    title=f"{self.service_name} - 移除项目确认",
                    flags=win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_SYSTEMMODAL,
                )
                if ret != win32con.IDYES:
                    return

                try:
                    cfg_mgr.unregister_project(proj_name, persist=True)
                except Exception as err:
                    self.message_box(
                        f"移除项目失败：\n\n{err}",
                        flags=win32con.MB_ICONWARNING | win32con.MB_SYSTEMMODAL,
                    )
                    return

                self.supervisor.restart_async(icon)

                try:
                    icon.notify(
                        f"已成功移除项目【{proj_name}】\n服务已自动重启生效。",
                        self.service_name,
                    )
                except Exception:
                    pass

            threading.Thread(target=_worker, daemon=True).start()

        return on_remove


def main() -> None:
    CodexenTrayApp().run()


if __name__ == "__main__":
    main()

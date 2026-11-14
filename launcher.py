"""Codexen MCP Engine Launcher."""
from __future__ import annotations

import sys
from pathlib import Path

from mcp_server_core.launcher import launch_service
from app.__main__ import main as run_app_main

ROOT_DIR = Path(__file__).resolve().parent


def main() -> None:
    launch_service(
        service_name="Codexen 项目开发引擎",
        server_runner=run_app_main,
        default_port=8788,
        root_dir=ROOT_DIR,
    )


if __name__ == "__main__":
    main()

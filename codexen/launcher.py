"""Codexen MCP Engine Launcher."""
from __future__ import annotations

import sys
from pathlib import Path

from mcp_server_core.launcher import launch_service
from .__main__ import main as run_app_main

ROOT_DIR = Path(__file__).resolve().parents[1]


def main() -> None:
    try:
        launch_service(
            server_runner=run_app_main,
            default_port=8788,
            root_dir=ROOT_DIR,
        )
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()

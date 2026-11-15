"""Decorator cut-points and project resolution helpers for MCP tools."""
from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

import anyio
from mcp_server_core.logging import observe_tool

from ..config import ConfigManager, ProjectConfig

F = TypeVar("F", bound=Callable[..., Any])


class ToolContext:
    """Encapsulates MCP server instance and configuration manager for tool modules."""

    def __init__(self, mcp: Any, config_mgr: ConfigManager) -> None:
        self.mcp = mcp
        self.config_mgr = config_mgr
        self.tools: dict[str, Any] = {}

    def register_tool(self, *dargs: Any, **dkwargs: Any) -> Any:
        """Register a tool onto FastMCP with worker thread execution to avoid event loop starvation."""
        def decorator(fn: F) -> F:
            observed_fn = observe_tool()(fn)
            tool_name = dkwargs.get("name") or fn.__name__

            @functools.wraps(observed_fn)
            async def async_worker(*args: Any, **kwargs: Any) -> Any:
                return await anyio.to_thread.run_sync(functools.partial(observed_fn, *args, **kwargs))

            self.mcp.tool(*dargs, **dkwargs)(async_worker)
            self.tools[tool_name] = observed_fn
            return observed_fn  # type: ignore[no-any-return,return-value]

        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return decorator(dargs[0])
        return decorator

    def register_alias(
        self,
        source_fn: Any,
        alias_name: str,
        alias_doc: str | None = None,
    ) -> Any:
        """Register an identical tool alias sharing exact signature, schema, and worker runner."""
        @functools.wraps(source_fn)
        def alias_fn(*args: Any, **kwargs: Any) -> Any:
            return source_fn(*args, **kwargs)

        alias_fn.__name__ = alias_name
        if alias_doc:
            alias_fn.__doc__ = alias_doc
        return self.register_tool()(alias_fn)

    def resolve_project(self, project_id: str | None = None, project_name: str | None = None) -> ProjectConfig:
        """Resolve ProjectConfig from project_id or project_name."""
        target = (project_id or "").strip() or (project_name or "").strip()
        if not target:
            available = list(self.config_mgr.projects.keys())
            raise ValueError(f"Missing required 'project_id' or 'project_name'. Available projects: {available}")
        return self.config_mgr.get_project(target)

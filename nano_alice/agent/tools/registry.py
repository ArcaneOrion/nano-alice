"""Tool registry for dynamic tool management."""

import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

from nano_alice.agent.tools.base import Tool
from nano_alice.logging_utils import summarize_tool_result

_FILE_TOOLS = frozenset({"read_file", "write_file", "edit_file", "list_dir", "append_file"})
_ABS_PATH_RE = re.compile(r"(?:^|[\s|>;])(/[^\s\"'>]+)")
_SAFE_ABS_PATHS = frozenset({
    "/dev/null", "/dev/zero", "/dev/urandom", "/dev/random",
    "/dev/stdin", "/dev/stdout", "/dev/stderr",
})


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._workspace = workspace.resolve() if workspace else None
        self._allowed_dir = allowed_dir.resolve() if allowed_dir else None

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    # ------------------------------------------------------------------
    # Boundary guard
    # ------------------------------------------------------------------

    def _boundary_guard(self, name: str, params: dict[str, Any]) -> str | None:
        """Return an error string if the tool call would escape *allowed_dir*."""
        if self._allowed_dir is None:
            return None

        if name in _FILE_TOOLS:
            return self._check_file_path(name, params)
        if name == "exec":
            return self._check_exec(params)
        return None

    def _blocked(self, reason: str) -> str:
        return f"[系统拦截] {reason}，仅允许访问工作目录 {self._allowed_dir}"

    def _check_file_path(self, name: str, params: dict[str, Any]) -> str | None:
        raw = params.get("path")
        if not raw:
            return None
        try:
            p = Path(raw).expanduser()
            if not p.is_absolute() and self._workspace:
                p = self._workspace / p
            resolved = p.resolve()
        except Exception:
            reason = f"工具 {name} 的路径 '{raw}' 无法解析"
            logger.warning("Boundary guard blocked tool '{}': {}", name, reason)
            return self._blocked(reason)

        if not resolved.is_relative_to(self._allowed_dir):
            reason = f"工具 {name} 的路径 '{raw}' 超出工作目录"
            logger.warning("Boundary guard blocked tool '{}': {}", name, reason)
            return self._blocked(reason)
        return None

    def _check_exec(self, params: dict[str, Any]) -> str | None:
        # Check working_dir
        wd = params.get("working_dir")
        if wd:
            try:
                wd_resolved = Path(wd).expanduser().resolve()
            except Exception:
                reason = f"exec 的 working_dir '{wd}' 无法解析"
                logger.warning("Boundary guard blocked tool 'exec': {}", reason)
                return self._blocked(reason)
            if not wd_resolved.is_relative_to(self._allowed_dir):
                reason = f"exec 的 working_dir '{wd}' 超出工作目录"
                logger.warning("Boundary guard blocked tool 'exec': {}", reason)
                return self._blocked(reason)

        cmd = params.get("command", "")

        # Traversal patterns
        if "../" in cmd or "..\\" in cmd:
            reason = "exec 命令包含路径穿越（../）"
            logger.warning("Boundary guard blocked tool 'exec': {}", reason)
            return self._blocked(reason)

        # ~ expansion
        for token in cmd.split():
            if token.startswith("~"):
                try:
                    expanded = Path(token).expanduser().resolve()
                except Exception:
                    continue
                if expanded.is_absolute() and not expanded.is_relative_to(self._allowed_dir):
                    reason = f"exec 命令引用了工作目录外的路径 '{token}'"
                    logger.warning("Boundary guard blocked tool 'exec': {}", reason)
                    return self._blocked(reason)

        # Absolute paths
        for match in _ABS_PATH_RE.finditer(cmd):
            raw = match.group(1)
            try:
                resolved = Path(raw).resolve()
            except Exception:
                continue
            if raw in _SAFE_ABS_PATHS:
                continue
            if not resolved.is_relative_to(self._allowed_dir):
                reason = f"exec 命令引用了工作目录外的路径 '{raw}'"
                logger.warning("Boundary guard blocked tool 'exec': {}", reason)
                return self._blocked(reason)

        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, name: str, params: dict[str, Any]) -> str | list:
        """
        Execute a tool by name with given parameters.

        Args:
            name: Tool name.
            params: Tool parameters.

        Returns:
            Tool execution result as string.

        Raises:
            KeyError: If tool not found.
        """
        tool = self._tools.get(name)
        if not tool:
            logger.warning("Tool not found: {}", name)
            return f"Error: Tool '{name}' not found"

        try:
            errors = tool.validate_params(params)
            if errors:
                logger.warning("Tool '{}' param validation failed: {}", name, "; ".join(errors))
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)

            violation = self._boundary_guard(name, params)
            if violation:
                return violation

            t0 = time.perf_counter()
            result = await tool.execute(**params)
            elapsed = time.perf_counter() - t0
            summary = summarize_tool_result(name, result)
            logger.debug(
                "Tool '{}' executed in {:.2f}s, result_bytes={} result_kind={} preview={}",
                name,
                elapsed,
                summary["result_bytes"],
                summary["result_kind"],
                summary["preview"],
            )
            return result
        except Exception as e:
            logger.error("Tool '{}' raised exception: {}", name, e)
            return f"Error executing {name}: {str(e)}"

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

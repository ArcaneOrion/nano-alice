"""Tests for ToolRegistry boundary guard."""

from pathlib import Path
from typing import Any

import pytest

from nano_alice.agent.tools.base import Tool
from nano_alice.agent.tools.registry import ToolRegistry


class DummyTool(Tool):
    """Minimal tool for testing — always succeeds."""

    def __init__(self, tool_name: str, required: list[str] | None = None):
        self._name = tool_name
        self._required = required or []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "dummy"

    @property
    def parameters(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "command": {"type": "string"},
            "working_dir": {"type": "string"},
        }
        return {
            "type": "object",
            "properties": props,
            "required": self._required,
        }

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def _make_registry(workspace: str, allowed: bool = True) -> ToolRegistry:
    ws = Path(workspace)
    allowed_dir = ws if allowed else None
    reg = ToolRegistry(workspace=ws, allowed_dir=allowed_dir)
    for tool_name in ("read_file", "write_file", "edit_file", "list_dir", "append_file"):
        reg.register(DummyTool(tool_name, required=["path"]))
    reg.register(DummyTool("exec", required=["command"]))
    reg.register(DummyTool("web_search"))
    return reg


# ── File tools ────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool_name", ["read_file", "write_file", "edit_file", "list_dir", "append_file"])
async def test_file_tool_inside_workspace(tmp_path: Path, tool_name: str) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute(tool_name, {"path": str(tmp_path / "file.txt")})
    assert result == "ok"


@pytest.mark.parametrize("tool_name", ["read_file", "write_file", "edit_file", "list_dir"])
async def test_file_tool_relative_path(tmp_path: Path, tool_name: str) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute(tool_name, {"path": "memory/MEMORY.md"})
    assert result == "ok"


@pytest.mark.parametrize("tool_name", ["read_file", "write_file", "edit_file", "list_dir"])
async def test_file_tool_outside_workspace(tmp_path: Path, tool_name: str) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute(tool_name, {"path": "/etc/passwd"})
    assert "[系统拦截]" in result
    assert "仅允许访问工作目录" in result


async def test_file_tool_tilde_outside(tmp_path: Path) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute("read_file", {"path": "~/.bashrc"})
    assert "[系统拦截]" in result


# ── exec tool ─────────────────────────────────────────────────────────


async def test_exec_simple_command(tmp_path: Path) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute("exec", {"command": "ls"})
    assert result == "ok"


async def test_exec_traversal_blocked(tmp_path: Path) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute("exec", {"command": "cat ../../etc/passwd"})
    assert "[系统拦截]" in result
    assert "路径穿越" in result


async def test_exec_absolute_path_blocked(tmp_path: Path) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute("exec", {"command": "ls /etc"})
    assert "[系统拦截]" in result


async def test_exec_absolute_path_inside_workspace(tmp_path: Path) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute("exec", {"command": f"ls {tmp_path}/subdir"})
    assert result == "ok"


async def test_exec_tilde_path_blocked(tmp_path: Path) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute("exec", {"command": "cat ~/../../etc/passwd"})
    assert "[系统拦截]" in result


async def test_exec_working_dir_outside(tmp_path: Path) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute("exec", {"command": "ls", "working_dir": "/tmp"})
    assert "[系统拦截]" in result


async def test_exec_working_dir_inside(tmp_path: Path) -> None:
    reg = _make_registry(str(tmp_path))
    sub = tmp_path / "sub"
    sub.mkdir()
    result = await reg.execute("exec", {"command": "ls", "working_dir": str(sub)})
    assert result == "ok"


# ── Guard disabled ────────────────────────────────────────────────────


async def test_no_guard_when_allowed_dir_none(tmp_path: Path) -> None:
    """When allowed_dir is None, nothing is blocked."""
    reg = _make_registry(str(tmp_path), allowed=False)
    result = await reg.execute("read_file", {"path": "/etc/passwd"})
    assert result == "ok"

    result = await reg.execute("exec", {"command": "ls /etc"})
    assert result == "ok"


# ── Other tools skip guard ────────────────────────────────────────────


async def test_other_tools_skip_guard(tmp_path: Path) -> None:
    reg = _make_registry(str(tmp_path))
    result = await reg.execute("web_search", {"query": "hello"})
    assert result == "ok"

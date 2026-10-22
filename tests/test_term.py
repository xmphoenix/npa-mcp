"""独立控制台窗口:URL 构造、配置开关与进程启动逻辑."""

from __future__ import annotations

import pytest

from npa_mcp import server, term
from npa_mcp.config import ConfigManager
from npa_mcp.server import Runtime


class _FakeSession:
    session_id = "serial-9"
    label = "测试串口"
    kind = "serial"


class _FakeProc:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        return None


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://127.0.0.1:8765", "ws://127.0.0.1:8765/ws/session/serial-9"),
        ("https://box:1/", "wss://box:1/ws/session/serial-9"),
        ("ws://box:1", "ws://box:1/ws/session/serial-9"),
        ("127.0.0.1:8765", "ws://127.0.0.1:8765/ws/session/serial-9"),
    ],
)
def test_ws_url_variants(base: str, expected: str) -> None:
    assert term._ws_url(base, "serial-9") == expected


def test_native_terminal_default_on(manager: ConfigManager) -> None:
    assert manager.load().web.native_terminal is True


def test_terminal_window_spawn_arguments(
    manager: ConfigManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = Runtime(str(manager.path))
    rt.web_url = "http://127.0.0.1:8765"
    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.delenv("NPA_MCP_WINDOW", raising=False)
    monkeypatch.delenv("NPA_MCP_NO_WINDOW", raising=False)
    calls: list[list[str]] = []

    def fake_popen(args, **kwargs):  # noqa: ANN001, ANN003, ARG001
        calls.append(list(args))
        return _FakeProc()

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    session = _FakeSession()

    assert rt.ensure_terminal_window(session) is True
    assert calls and calls[0][1:4] == ["-m", "npa_mcp.term", "--url"]
    assert "--session" in calls[0] and "serial-9" in calls[0]
    # 已在运行时不重复开窗口
    assert rt.ensure_terminal_window(session) is True
    assert len(calls) == 1
    rt._close_terminal_windows()


def test_terminal_window_respects_env_and_config(
    manager: ConfigManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = Runtime(str(manager.path))
    rt.web_url = "http://127.0.0.1:8765"
    monkeypatch.setattr(server.sys, "platform", "win32")
    spawned: list[object] = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: spawned.append(a) or _FakeProc())
    session = _FakeSession()

    monkeypatch.setenv("NPA_MCP_NO_WINDOW", "1")
    assert rt.ensure_terminal_window(session) is False
    monkeypatch.delenv("NPA_MCP_NO_WINDOW")
    monkeypatch.setenv("NPA_MCP_WINDOW", "nightly")
    assert rt.ensure_terminal_window(session) is False
    monkeypatch.delenv("NPA_MCP_WINDOW")
    manager.update_from_dict({"web": {"native_terminal": False}})
    assert rt.ensure_terminal_window(session) is False
    assert spawned == []

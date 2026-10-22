"""启动时自动打开配置页:配置开关、无头跳过与跨实例节流."""

from __future__ import annotations

import time

import pytest

from npa_mcp import server
from npa_mcp.config import ConfigManager


class _FakeRuntime:
    def __init__(self, manager: ConfigManager, web_url: str = "") -> None:
        self.manager = manager
        self.web_url = web_url


def test_web_auto_open_defaults_and_patch(manager: ConfigManager) -> None:
    cfg = manager.load()
    assert cfg.web.auto_open is True
    assert cfg.web.auto_open_interval_sec == 30
    manager.update_from_dict({"web": {"auto_open": False}})
    assert manager.load().web.auto_open is False


def test_claim_first_succeeds_then_blocked(manager: ConfigManager) -> None:
    rt = _FakeRuntime(manager)
    assert server._claim_browser_open(rt, 30) is True
    assert server._claim_browser_open(rt, 30) is False


def test_claim_allowed_after_interval(manager: ConfigManager) -> None:
    rt = _FakeRuntime(manager)
    assert server._claim_browser_open(rt, 30) is True
    stamp = manager.path.parent / "web_opened.stamp"
    stamp.write_text(f"{time.time() - 3600:.3f}\n", encoding="utf-8")
    assert server._claim_browser_open(rt, 30) is True


def test_claim_zero_interval_never_blocks(manager: ConfigManager) -> None:
    rt = _FakeRuntime(manager)
    assert server._claim_browser_open(rt, 0) is True
    assert server._claim_browser_open(rt, 0) is True


async def test_auto_open_skips_headless_and_disabled(
    manager: ConfigManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _FakeRuntime(manager, "http://127.0.0.1:8765")
    opened: list[str] = []
    monkeypatch.setattr(server, "open_url", lambda url: opened.append(url) or True)

    monkeypatch.setenv("NPA_MCP_WINDOW", "nightly")
    await server._auto_open_config_page(rt, manager.load())
    assert opened == []

    monkeypatch.delenv("NPA_MCP_WINDOW")
    manager.update_from_dict({"web": {"auto_open": False}})
    await server._auto_open_config_page(rt, manager.load())
    assert opened == []


async def test_auto_open_opens_config_page(
    manager: ConfigManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _FakeRuntime(manager, "http://127.0.0.1:8765")
    opened: list[str] = []
    monkeypatch.setattr(server, "open_url", lambda url: opened.append(url) or True)
    monkeypatch.delenv("NPA_MCP_WINDOW", raising=False)
    monkeypatch.delenv("NPA_MCP_NO_BROWSER", raising=False)

    await server._auto_open_config_page(rt, manager.load())

    assert opened == ["http://127.0.0.1:8765/config"]

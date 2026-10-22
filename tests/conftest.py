"""pytest 公共夹具:每个测试用独立的 NPA_MCP_HOME,互不污染真实配置."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "npa-home"
    monkeypatch.setenv("NPA_MCP_HOME", str(home))
    return home


@pytest.fixture
def manager(isolated_home: Path):
    from npa_mcp.config import ConfigManager

    cfg_manager = ConfigManager(isolated_home / "config.json")
    cfg_manager.ensure_default()
    return cfg_manager

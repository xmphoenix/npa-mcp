"""传输层纯函数测试:路径形态判断、cd 前缀、POSIX 引号."""

from __future__ import annotations

import pytest

from npa_mcp.server import _resolve_remote
from npa_mcp.transports import cd_prefix, is_windows_path, shell_quote


@pytest.mark.parametrize(
    "value",
    ["C:\\work", "C:/work", "d:\\x", "\\\\server\\share"],
)
def test_is_windows_path_true(value: str) -> None:
    assert is_windows_path(value) is True


@pytest.mark.parametrize(
    "value",
    ["/home/dev", "~/proj", "relative/path", "", "project", "//server/share"],
)
def test_is_windows_path_false(value: str) -> None:
    assert is_windows_path(value) is False


def test_cd_prefix_posix_uses_single_quotes() -> None:
    assert cd_prefix("/home/dev/my project") == "cd '/home/dev/my project' && "


def test_cd_prefix_posix_escapes_quote() -> None:
    assert cd_prefix("/tmp/it's") == "cd '/tmp/it'\"'\"'s' && "


def test_cd_prefix_windows_drive_uses_cd_d() -> None:
    assert cd_prefix("C:\\Users\\dev\\项目") == 'cd /d "C:\\Users\\dev\\项目" && '


def test_cd_prefix_windows_unc_uses_pushd() -> None:
    assert cd_prefix("\\\\nas\\share\\proj") == 'pushd "\\\\nas\\share\\proj" && '


def test_shell_quote() -> None:
    assert shell_quote("plain") == "'plain'"
    assert shell_quote("a b") == "'a b'"
    assert shell_quote("it's") == "'it'\"'\"'s'"


@pytest.mark.asyncio
async def test_resolve_remote_keeps_windows_absolute() -> None:
    # Windows 绝对路径不拼 workdir,也不再做 ~ 展开(无需真实目标)
    assert await _resolve_remote(None, "C:/work/a.txt", "D:/base") == "C:/work/a.txt"  # type: ignore[arg-type]
    assert await _resolve_remote(None, "\\\\nas\\share\\a", "D:/base") == "\\\\nas\\share\\a"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_resolve_remote_joins_relative() -> None:
    assert await _resolve_remote(None, "a.txt", "D:/base") == "D:/base/a.txt"  # type: ignore[arg-type]
    assert await _resolve_remote(None, "", "/home/dev") == "/home/dev"

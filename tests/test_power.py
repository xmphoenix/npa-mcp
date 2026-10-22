"""电源管理设备(PduClient)单元测试:判定逻辑、模板与输出清理."""

from __future__ import annotations

import pytest

from npa_mcp.config import PowerConfig
from npa_mcp.power import PduClient, PowerError, _find_first, _strip_command_echo


def test_find_first_returns_earliest_keyword() -> None:
    text = "ERROR: denied\r\nOK: done"
    assert _find_first(text, ["OK", "ERROR"]) == ("ERROR", 0)
    assert _find_first(text, ["OK", "denied"]) == ("denied", 7)


def test_find_first_none_when_absent() -> None:
    assert _find_first("nothing here", ["OK", "ON"]) is None


def test_find_first_ignores_case() -> None:
    hit = _find_first("ok: done", ["OK"])
    assert hit is not None
    assert hit[0] == "OK"


def test_strip_command_echo() -> None:
    output = "outlet 1 on\r\nOK: outlet 1 is ON\r\nPDU> "
    assert _strip_command_echo(output, "outlet 1 on") == "OK: outlet 1 is ON\r\nPDU> "


def test_strip_command_echo_keeps_unrelated_first_line() -> None:
    output = "OK: outlet 1 is ON\r\nPDU> "
    assert _strip_command_echo(output, "outlet 1 on") == output


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    *,
    action: str = "on",
    cfg: PowerConfig | None = None,
    outlet: int | None = None,
) -> tuple[object, dict[str, str]]:
    cfg = cfg or PowerConfig(host="10.0.0.9")
    client = PduClient(cfg)
    captured: dict[str, str] = {}

    async def fake_telnet(self: PduClient, command: str) -> str:
        captured["command"] = command
        return output

    monkeypatch.setattr(PduClient, "_run_telnet", fake_telnet)
    result = await client.run(action, outlet=outlet)
    return result, captured


async def test_run_ok_output(monkeypatch: pytest.MonkeyPatch) -> None:
    result, captured = await _run(monkeypatch, "OK: outlet 1 is ON\r\nPDU> ")
    assert captured["command"] == "outlet 1 on"
    assert result.ok is True
    assert result.determined is True
    assert result.action == "on"
    assert result.outlet == 1


async def test_run_fail_output(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = await _run(monkeypatch, "ERROR: permission denied\r\nPDU> ")
    assert result.ok is False
    assert result.determined is True


async def test_run_ok_before_fail_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = await _run(monkeypatch, "OK: accepted\r\nERROR: later warning\r\n")
    assert result.ok is True
    assert result.determined is True


async def test_run_fail_before_ok_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = await _run(monkeypatch, "ERROR: rejected\r\nOK: never happened\r\n")
    assert result.ok is False
    assert result.determined is True


async def test_run_unknown_output_is_not_silent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = await _run(monkeypatch, "working...\r\n")
    assert result.ok is False
    assert result.determined is False
    assert result.output == "working..."


async def test_status_inferred_from_on_off(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = PowerConfig(host="10.0.0.9", expect={"ok": [], "fail": []})
    result, captured = await _run(monkeypatch, "outlet 1: OFF\r\nPDU> ", action="status", cfg=cfg)
    assert captured["command"] == "outlet 1 status"
    assert result.determined is True
    assert result.ok is True


async def test_outlet_override(monkeypatch: pytest.MonkeyPatch) -> None:
    result, captured = await _run(monkeypatch, "OK: outlet 4 is OFF\r\n", action="off", outlet=4)
    assert captured["command"] == "outlet 4 off"
    assert result.outlet == 4


async def test_unknown_action_rejected() -> None:
    client = PduClient(PowerConfig())
    with pytest.raises(PowerError):
        await client.run("reboot")


async def test_unknown_placeholder_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = PowerConfig(host="10.0.0.9")
    cfg.commands.on = "outlet {n} on"
    client = PduClient(cfg)
    with pytest.raises(PowerError):
        await client.run("on")

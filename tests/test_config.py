"""配置模型、密钥掩码与热加载测试."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from npa_mcp.config import (
    Config,
    ConfigManager,
    DeviceSerialConfig,
    WorkWindow,
    normalize_com_port,
)


def _at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute)


def test_default_config_created_with_dirs(manager: ConfigManager) -> None:
    assert manager.path.is_file()
    cfg = manager.load()
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 8765
    cfg.ensure_dirs()
    assert cfg.paths.image_path().is_dir()
    assert cfg.paths.report_path().is_dir()
    assert cfg.paths.log_path().is_dir()


def test_secret_masking_and_sentinel_preserved(manager: ConfigManager) -> None:
    cfg = manager.load()
    cfg.device.telnet.username = "root"
    cfg.device.telnet.password = "s3cret"
    cfg.power.password = "pdu-pass"
    cfg.firmware_server.http.token = "http-token"
    manager.save(cfg)

    masked = manager.masked_dict()
    assert masked["device"]["telnet"]["username"] == "root"
    assert masked["device"]["telnet"]["password"] == "***"
    assert masked["power"]["password"] == "***"
    assert masked["firmware_server"]["http"]["token"] == "***"

    manager.update_from_dict(
        {
            "device": {"telnet": {"host": "192.168.1.50", "password": "***"}},
            "power": {"password": "***"},
        }
    )
    saved = manager.load()
    assert saved.device.telnet.password == "s3cret"
    assert saved.device.telnet.host == "192.168.1.50"
    assert saved.power.password == "pdu-pass"

    manager.update_from_dict({"power": {"password": "new-pass"}})
    assert manager.load().power.password == "new-pass"


def test_partial_patch_keeps_unrelated_values(manager: ConfigManager) -> None:
    cfg = manager.load()
    cfg.code_server.host = "build.example.com"
    cfg.code_server.workdir = "/home/dev/project"
    manager.save(cfg)

    manager.update_from_dict({"device": {"serial": {"port": "COM5"}}})
    saved = manager.load()
    assert saved.code_server.host == "build.example.com"
    assert saved.code_server.workdir == "/home/dev/project"
    assert saved.device.serial.port == "COM5"


def test_serial_login_credentials_round_trip(manager: ConfigManager) -> None:
    """串口登录账号必须能落盘:模型缺字段时会被 extra=ignore 静默丢弃."""
    manager.update_from_dict(
        {
            "device": {
                "serial": {"port": "COM5", "username": "root", "password": "root"}
            }
        }
    )
    saved = manager.load()
    assert saved.device.serial.username == "root"
    assert saved.device.serial.password == "root"


def test_load_hot_reloads_changed_file(manager: ConfigManager) -> None:
    assert manager.load().web.port == 8765
    data = json.loads(manager.path.read_text(encoding="utf-8"))
    data["web"]["port"] = 9911
    data["web"]["keep_alive_sec"] = 3
    manager.path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = manager.load()
    assert reloaded.web.port == 9911
    assert reloaded.web.keep_alive_sec == 3


def test_corrupt_config_is_backed_up(manager: ConfigManager) -> None:
    manager.path.write_text("{not json", encoding="utf-8")
    cfg = manager.load()
    assert cfg.version == 1
    backups = list(manager.path.parent.glob("config.corrupt-*.json"))
    assert backups, "损坏的配置应被备份而不是直接丢弃"


def test_work_window_same_day() -> None:
    window = WorkWindow(name="day", days=[3], start="09:00", end="17:00")
    # 2026-10-22 是周四(weekday=3)
    assert window.is_active(_at(2026, 10, 22, 10, 0))
    assert not window.is_active(_at(2026, 10, 22, 18, 0))
    assert not window.is_active(_at(2026, 10, 23, 10, 0))
    assert window.active_end(_at(2026, 10, 22, 10, 0)) == _at(2026, 10, 22, 17, 0)


def test_work_window_cross_midnight() -> None:
    window = WorkWindow(name="night", days=[3], start="23:00", end="06:00")
    assert window.is_active(_at(2026, 10, 22, 23, 30))
    # 周五凌晨仍属于周四 23:00 开始的窗口
    assert window.is_active(_at(2026, 10, 23, 5, 0))
    assert not window.is_active(_at(2026, 10, 23, 6, 30))
    assert not window.is_active(_at(2026, 10, 21, 23, 30))
    assert window.active_end(_at(2026, 10, 22, 23, 30)) == _at(2026, 10, 23, 6, 0)
    assert window.active_end(_at(2026, 10, 23, 5, 0)) == _at(2026, 10, 23, 6, 0)


def test_work_window_next_start() -> None:
    window = WorkWindow(name="night", days=[3], start="23:00", end="06:00")
    assert window.next_start(_at(2026, 10, 22, 12, 0)) == _at(2026, 10, 22, 23, 0)
    assert window.next_start(_at(2026, 10, 23, 12, 0)) == _at(2026, 10, 29, 23, 0)


@pytest.mark.parametrize("days", [[], [-1], [7], [1, 8]])
def test_invalid_days_rejected(days: list[int]) -> None:
    with pytest.raises(ValidationError):
        WorkWindow(days=days)


@pytest.mark.parametrize("bad", ["9:00", "24:00", "23:60", "abc"])
def test_invalid_time_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        WorkWindow(start=bad)


def test_duplicate_window_names_rejected() -> None:
    with pytest.raises(ValidationError):
        Config(work_windows=[WorkWindow(name="a"), WorkWindow(name="a")])


def test_config_window_lookup() -> None:
    cfg = Config(work_windows=[WorkWindow(name="nightly"), WorkWindow(name="weekend")])
    assert cfg.window("weekend") is not None
    assert cfg.window("missing") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("COM3", "COM3"),
        ("com9", "com9"),
        ("COM10", "\\\\.\\COM10"),
        ("\\\\.\\COM10", "\\\\.\\COM10"),
        ("", ""),
    ],
)
def test_normalize_com_port(raw: str, expected: str) -> None:
    assert normalize_com_port(raw) == expected


def test_serial_config_normalizes_port() -> None:
    assert DeviceSerialConfig(port="COM12").port == "\\\\.\\COM12"


def test_lan_peer_prefers_enabled_device_host(manager: ConfigManager) -> None:
    cfg = manager.load()
    cfg.device.telnet.enabled = True
    cfg.device.telnet.host = "192.168.71.120"
    cfg.power.host = "192.168.71.130"
    assert cfg.lan_peer() == "192.168.71.120"


def test_lan_peer_ignores_hostnames_and_loopback(manager: ConfigManager) -> None:
    cfg = manager.load()
    cfg.device.ssh.host = "board.local"
    cfg.device.telnet.host = "127.0.0.1"
    cfg.power.host = "192.168.71.130"
    assert cfg.lan_peer() == "192.168.71.130"


def test_firmware_host_explicit_override(manager: ConfigManager) -> None:
    cfg = manager.load()
    cfg.device.ssh.host = "192.168.71.120"
    cfg.firmware_server.host = "192.168.71.55"
    assert cfg.firmware_host() == "192.168.71.55"


def test_detect_lan_ip_uses_peer_route(monkeypatch: pytest.MonkeyPatch) -> None:
    from npa_mcp import config as config_module

    calls: list[str] = []

    def fake_local_ip(target: str) -> str:
        calls.append(target)
        return "192.168.71.55" if target == "192.168.71.120" else "172.19.0.1"

    monkeypatch.setattr(config_module, "_udp_local_ip", fake_local_ip)
    assert config_module.detect_lan_ip("192.168.71.120") == "192.168.71.55"
    assert calls == ["192.168.71.120"]


def test_detect_lan_ip_skips_virtual_dot_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from npa_mcp import config as config_module

    monkeypatch.setattr(
        config_module,
        "_local_ipv4_candidates",
        lambda: ["192.168.71.55"],
    )
    monkeypatch.setattr(config_module, "_udp_local_ip", lambda target: "172.19.0.1")
    assert config_module.detect_lan_ip() == "192.168.71.55"


def test_local_ipv4_candidates_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    from npa_mcp import config as config_module

    monkeypatch.setattr(
        config_module.socket,
        "gethostbyname_ex",
        lambda host: ("pc", [], ["192.168.56.1", "192.168.71.55", "172.19.0.1", "169.254.1.1"]),
    )
    assert config_module._local_ipv4_candidates() == [
        "192.168.71.55",
        "192.168.56.1",
        "172.19.0.1",
    ]

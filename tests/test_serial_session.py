"""串口会话测试:用假 ``serial.Serial`` 模拟一台需要登录的开发板.

本机没有 COM0COM 回环对,真实串口端到端无法自动化;这里在同一进程里
用线程安全的假串口验证登录握手、命令回显剥离、提示符判定与错误转中文。
"""

from __future__ import annotations

import threading
import time

import pytest
import serial as serial_module
from serial import SerialException

from npa_mcp.config import ConfigManager, DeviceSerialConfig
from npa_mcp.sessions import SerialSession, SessionError, SessionHub


class FakeSerial:
    """假串口:收到一行命令就按"登录 → 密码 → shell"状态机回包.

    ``read`` 超时返回 ``b""``(与真实串口 timeout 行为一致),会话的
    reader 线程因此会调用 ``flush_partial``,把没有换行结尾的提示符
    ``login: `` / ``Password: `` / ``# `` 发布出来。
    """

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.port = kwargs.get("port", "")
        self.baudrate = kwargs.get("baudrate")
        self.closed = False
        self.fail_write = False
        self.writes: list[bytes] = []
        self._lock = threading.Lock()
        self._rx = bytearray()
        self._pending = ""
        self._stage = "login"
        self._emit("login: ")

    # -- 供 SerialSession 调用 ---------------------------------------
    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + 0.12
        while True:
            with self._lock:
                if self._rx:
                    data = bytes(self._rx[:size])
                    del self._rx[:size]
                    return data
                if self.closed:
                    return b""
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.01)

    def write(self, data: bytes) -> int:
        if self.closed:
            raise SerialException("ClearCommError failed (port closed)")
        if self.fail_write:
            raise SerialException("WriteFile failed (device disconnected)")
        self.writes.append(data)
        text = data.decode("utf-8", "replace")
        lines: list[str] = []
        with self._lock:
            self._rx.extend(data)  # 设备回显
            self._pending += text
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                lines.append(line.rstrip("\r"))
        for line in lines:
            self._respond(line)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    # -- 内部 ---------------------------------------------------------
    def _emit(self, text: str) -> None:
        with self._lock:
            self._rx.extend(text.encode("utf-8"))

    def _respond(self, line: str) -> None:
        command = line.strip()
        if self._stage == "login":
            self._stage = "password"
            self._emit("Password: ")
        elif self._stage == "password":
            self._stage = "shell"
            self._emit("\r\nWelcome to fake-router (busybox)\r\n# ")
        elif command == "uname -a":
            self._emit("Linux fake-router 5.10.0 #1 SMP armv7l GNU/Linux\r\n# ")
        elif command in ("exit", "logout"):
            self._emit("logout\r\n")
        elif command:
            self._emit(f"sh: {command.split()[0]}: not found\r\n# ")
        else:
            self._emit("# ")


@pytest.fixture
def fake_serial(monkeypatch: pytest.MonkeyPatch) -> type[FakeSerial]:
    monkeypatch.setattr(serial_module, "Serial", FakeSerial)
    return FakeSerial


@pytest.fixture
async def hub(manager: ConfigManager):
    session_hub = SessionHub(manager)
    try:
        yield session_hub
    finally:
        await session_hub.close_all()


def _cfg(**overrides: object) -> DeviceSerialConfig:
    data = {
        "enabled": True,
        "port": "COM_FAKE",
        "username": "root",
        "password": "root",
        "login_required": True,
        "shell_prompt": "# ",
        "echo_strip": True,
    }
    data.update(overrides)
    return DeviceSerialConfig(**data)  # type: ignore[arg-type]


async def test_serial_login_and_exec(hub: SessionHub, fake_serial: type[FakeSerial]) -> None:
    session = SerialSession(hub, "serial-1", "设备串口", _cfg())
    try:
        await session.open()
        assert isinstance(session.ser, FakeSerial)
        assert session.ser.writes[0] == b"root\r\n"
        assert session.ser.writes[1] == b"root\r\n"

        result = await hub.exec_on_console(session, "uname -a", timeout=3.0)
        assert result["matched_prompt"] is True
        assert result["timed_out"] is False
        assert "Linux fake-router" in result["output"]
        # 设备回显的那一行应被剥离,不混进命令输出
        assert "uname -a" not in result["output"]
        assert session.ser.writes[-1] == b"uname -a\r\n"
    finally:
        await session.close_transport()


async def test_serial_open_failure_maps_to_chinese_error(
    hub: SessionHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _NoPortSerial:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise SerialException(
                "could not open port 'COM_FAKE': Access is denied."
            )

    monkeypatch.setattr(serial_module, "Serial", _NoPortSerial)
    session = SerialSession(hub, "serial-2", "设备串口", _cfg())
    with pytest.raises(SessionError) as excinfo:
        await session.open()
    assert "无法打开串口" in excinfo.value.message
    assert "驱动" in excinfo.value.hint or "占用" in excinfo.value.hint


async def test_serial_write_failure_maps_to_chinese_error(
    hub: SessionHub, fake_serial: type[FakeSerial]
) -> None:
    session = SerialSession(hub, "serial-3", "设备串口", _cfg())
    try:
        await session.open()
        assert session.ser is not None
        session.ser.fail_write = True
        with pytest.raises(SessionError) as excinfo:
            await hub.exec_on_console(session, "ls", timeout=1.0)
        assert "串口写入失败" in excinfo.value.message
    finally:
        await session.close_transport()


async def test_serial_write_after_close_rejected(
    hub: SessionHub, fake_serial: type[FakeSerial]
) -> None:
    session = SerialSession(hub, "serial-4", "设备串口", _cfg())
    await session.open()
    await session.close_transport()
    with pytest.raises(SessionError) as excinfo:
        await session.write_raw("ls\r\n")
    assert "串口会话已关闭" in excinfo.value.message

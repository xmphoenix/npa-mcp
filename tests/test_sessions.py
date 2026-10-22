"""会话层测试:RingBuffer、回显剥离、行组装、SessionHub 与只读镜像."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from npa_mcp.config import ConfigManager
from npa_mcp.sessions import (
    Console,
    EchoStripper,
    LineAssembler,
    RingBuffer,
    SessionError,
    SessionHub,
    SessionLog,
    _strip_echo_prefix,
    compile_pattern,
)


def test_ring_buffer_returns_incrementing_seq() -> None:
    ring = RingBuffer()
    first = ring.append("one\n")
    second = ring.append("two\n")
    assert first == 1
    assert second == 2
    start, text = ring.since(1)
    assert start == 1
    assert text == "two\n"


def test_ring_buffer_trims_old_lines() -> None:
    ring = RingBuffer(max_lines=3)
    for index in range(5):
        ring.append(f"line{index}\n")
    start, text = ring.since(0)
    assert start == 3
    assert "line0" not in text
    assert "line4" in text
    assert ring.tail(2) == "line3\nline4"


def test_ring_buffer_ignores_empty_text() -> None:
    ring = RingBuffer()
    seq = ring.append("")
    assert seq == 1
    assert ring.next_seq == 1


def test_echo_stripper_removes_echo_but_keeps_prompt() -> None:
    stripper = EchoStripper()
    stripper.expect("ls -l")
    assert stripper.process_line("root@dev:~# ls -l\r\n") == "root@dev:~# \r\n"
    # 同一条命令只剥一次
    assert stripper.process_line("total 0\r\n") == "total 0\r\n"


def test_echo_stripper_exact_echo_removed() -> None:
    stripper = EchoStripper()
    stripper.expect("uname -a")
    assert stripper.process_line("uname -a\r\n") == ""


def test_echo_stripper_passes_other_lines() -> None:
    stripper = EchoStripper()
    stripper.expect("ls")
    assert stripper.process_line("hello world\r\n") == "hello world\r\n"


def test_echo_stripper_disabled() -> None:
    stripper = EchoStripper(enabled=False)
    stripper.expect("ls")
    assert stripper.process_line("ls\r\n") == "ls\r\n"


def test_line_assembler_handles_partial_lines() -> None:
    assembler = LineAssembler()
    assert assembler.feed("abc") == []
    assert assembler.feed("def\nghi") == ["abcdef\n"]
    assert assembler.flush() == "ghi"
    assert assembler.feed("one\rtwo") == ["one\r"]
    assert assembler.flush() == "two"


def test_compile_pattern_falls_back_to_literal() -> None:
    pattern = compile_pattern("[")
    assert pattern.search("[") is not None
    assert pattern.search("no bracket") is None


def test_strip_echo_prefix() -> None:
    assert _strip_echo_prefix("ls -l\r\nfile\r\n", "ls -l") == "file\r\n"
    assert _strip_echo_prefix("file\r\n", "ls -l") == "file\r\n"


def test_session_log_appends(tmp_path: Path) -> None:
    path = tmp_path / "session.log"
    session_log = SessionLog(path)
    session_log.write("hello\n")
    session_log.write("世界\n")
    session_log.close()
    assert path.read_text(encoding="utf-8") == "hello\n世界\n"


@pytest.fixture
async def hub(manager: ConfigManager):
    session_hub = SessionHub(manager)
    try:
        yield session_hub
    finally:
        await session_hub.close_all()


async def test_mirror_session_is_read_only(hub: SessionHub) -> None:
    session = hub.open_mirror("代码服务器", "dev@build")
    assert session.read_only
    await hub.note(session, "[npa] hello\r\n")

    data = hub.since(session.session_id, 0)
    assert "hello" in data["text"]
    assert data["next_seq"] > 0
    assert "hello" in hub.tail(session.session_id)

    with pytest.raises(SessionError):
        await hub.send(session, "ls\r\n")


async def test_session_list_contains_terminal_url(hub: SessionHub) -> None:
    session = hub.open_mirror("设备 Telnet")
    items = hub.list()
    assert items[0]["session_id"] == session.session_id
    assert items[0]["terminal_url"].startswith("/terminals?session=")


async def test_close_view_then_force_close(hub: SessionHub) -> None:
    session = hub.open_mirror("m")
    result = await hub.close_session(session.session_id)
    assert result["closed"] is False
    assert session.hidden is True
    assert hub.list()[0]["hidden"] is True

    result = await hub.close_session(session.session_id, force=True)
    assert result["transport_closed"] is True
    assert session.closed
    # 已关闭会话保留在列表中(供事后读最后输出),但默认列表不展示
    assert session.session_id in hub.sessions
    assert hub.list() == []
    closed_items = hub.list(include_closed=True)
    assert closed_items[0]["session_id"] == session.session_id
    assert closed_items[0]["closed"] is True


async def test_wait_for_matches_prompt_at_tail(hub: SessionHub) -> None:
    session = hub.open_mirror("m")
    session.publish("banner\r\n# ")
    result = await hub.wait_for(session, compile_pattern("# "), 0.5, 0)
    assert result["matched"]
    assert result["match"] == "# "


async def test_wait_for_times_out(hub: SessionHub) -> None:
    session = hub.open_mirror("m")
    session.publish("still running\r\n")
    result = await hub.wait_for(session, compile_pattern("# "), 0.1, 0)
    assert result["matched"] is False
    assert result["timed_out"] is True
    assert "still running" in result["text"]


async def test_wait_for_mid_buffer_literals(hub: SessionHub) -> None:
    """等待业务输出时出现过即命中;等提示符时仍要求位于末尾."""
    session = hub.open_mirror("m")
    session.publish("Build complete\r\nstill compiling extra files\r\n")

    as_prompt = await hub.wait_for(session, compile_pattern("Build complete"), 0.1, 0)
    assert as_prompt["matched"] is False and as_prompt["timed_out"] is True

    as_literal = await hub.wait_for(
        session, compile_pattern("Build complete"), 0.1, 0, require_tail=False
    )
    assert as_literal["matched"] is True
    assert as_literal["match"] == "Build complete"


async def test_subscribe_fanout(hub: SessionHub) -> None:
    session = hub.open_mirror("m")
    received: list[str] = []
    unsubscribe = hub.subscribe(session.session_id, received.append)
    assert hub.subscriber_count(session.session_id) == 1

    hub._fanout(session.session_id, "直投\n")
    assert received == ["直投\n"]

    await hub.note(session, "线程安全\n")
    await asyncio.sleep(0.01)
    assert "线程安全\n" in received

    unsubscribe()
    assert hub.subscriber_count(session.session_id) == 0


class _EchoConsole(Console):
    """测试替身:写入时把命令回显 + 输出 + 提示符写回 ring."""

    kind = "test"

    def __init__(self, session_hub: SessionHub, session_id: str = "test-1"):
        super().__init__(
            session_hub,
            session_id,
            "test",
            "test",
            prompt="# ",
            echo_strip=True,
        )
        self.writes: list[str] = []

    async def write_raw(self, text: str) -> None:
        self.writes.append(text)
        command = text.rstrip("\r\n")
        self.publish(f"{command}\r\nOK\r\n# \r\n")

    async def close_transport(self) -> None:
        self.log.close()
        self.closed = True


async def test_exec_on_console_waits_for_prompt(hub: SessionHub) -> None:
    session = _EchoConsole(hub)
    hub.sessions[session.session_id] = session

    result = await hub.exec_on_console(session, "uname -a", timeout=1.0)
    assert result["matched_prompt"] is True
    assert result["timed_out"] is False
    assert "OK" in result["output"]
    assert session.writes == ["uname -a\r\n"]

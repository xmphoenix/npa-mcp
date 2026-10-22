"""远程交互会话:SSH shell / Telnet / 串口.

并发约定:
- reader(线程或协程)只做生产:写 RingBuffer + 会话日志,再通过
  ``loop.call_soon_threadsafe`` 把增量扇出给 WebSocket。
- 所有状态变更与写入在同一事件循环线程里,每个会话一把 asyncio 锁。
- 回显剥离是"尽力而为":失配就原样输出,绝不吞掉真实内容。
"""

from __future__ import annotations

import asyncio
import codecs
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import serial
from serial import SerialException

from .config import (
    AuthConfig,
    ConfigManager,
    DeviceSerialConfig,
    DeviceSshConfig,
    DeviceTelnetConfig,
)
from .transports import POOL, SshTarget, TransportError, shell_quote
from .util import ensure_dir, get_logger

log = get_logger(__name__)

IDLE_RECYCLE_SEC = 1800.0
SESSION_LOG_DIRNAME = "sessions"


class SessionError(RuntimeError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------
# RingBuffer / 日志
# --------------------------------------------------------------------------
class RingBuffer:
    """按块保存输出,按字节数与行数双重限长,seq 单调递增."""

    def __init__(self, max_lines: int = 10000, max_bytes: int = 1024 * 1024):
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self._items: deque[tuple[int, str]] = deque()
        self._bytes = 0
        self._next_seq = 1
        self._start_seq = 1
        self._lock = threading.Lock()

    @property
    def next_seq(self) -> int:
        with self._lock:
            return self._next_seq

    @property
    def start_seq(self) -> int:
        with self._lock:
            return self._start_seq

    def append(self, text: str) -> int:
        if not text:
            return self.next_seq
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            self._items.append((seq, text))
            self._bytes += len(text.encode("utf-8", "replace"))
            while self._items and (
                len(self._items) > self.max_lines or self._bytes > self.max_bytes
            ):
                _, old = self._items.popleft()
                self._bytes -= len(old.encode("utf-8", "replace"))
                self._start_seq = self._items[0][0] if self._items else self._next_seq
            return seq

    def since(self, seq: int) -> tuple[int, str]:
        with self._lock:
            parts = [text for item_seq, text in self._items if item_seq > seq]
            start = self._start_seq
        return start, "".join(parts)

    def tail(self, lines: int = 200) -> str:
        with self._lock:
            items = list(self._items)
        text = "".join(text for _, text in items)
        if lines <= 0:
            return text
        return "\n".join(text.splitlines()[-lines:])

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0
            self._start_seq = self._next_seq


class SessionLog:
    """会话原始输出追加日志,线程安全."""

    def __init__(self, path: Path):
        self.path = path
        ensure_dir(path.parent)
        self._lock = threading.Lock()
        self._fh = open(path, "ab", buffering=0)

    def write(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        with self._lock:
            try:
                self._fh.write(data)
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 回显剥离 / 行组装
# --------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_WS_RE = re.compile(r"\s+")


class EchoStripper:
    """对"我们刚发出去的命令被设备回显"做逐行剥离."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.expected: deque[tuple[str, float]] = deque()

    @staticmethod
    def _norm(text: str) -> str:
        return _WS_RE.sub("", _ANSI_RE.sub("", text)).strip()

    def expect(self, command: str) -> None:
        if not self.enabled:
            return
        for line in command.splitlines() or [command]:
            normalized = self._norm(line)
            if normalized:
                self.expected.append((normalized, time.monotonic()))

    def process_line(self, line: str) -> str:
        if not self.enabled or not self.expected:
            return line
        now = time.monotonic()
        while self.expected and now - self.expected[0][1] > 3.0:
            self.expected.popleft()
        normalized = self._norm(line)
        if not normalized:
            return line
        for index, (echo, _ts) in enumerate(self.expected):
            if normalized == echo or normalized.endswith(echo):
                # 尽量只删掉命令文本本身,保留可能存在的提示符前缀
                stripped_raw = self._norm(line)
                if echo and stripped_raw.endswith(echo):
                    cut = len(line)
                    # 从行尾反向匹配删掉 echo 对应的字符(忽略空白)
                    matched = 0
                    while cut > 0 and matched < len(echo):
                        cut -= 1
                        ch = _ANSI_RE.sub("", line[cut])
                        if ch.isspace():
                            continue
                        matched += 1
                    prefix = line[:cut]
                    if _WS_RE.sub("", _ANSI_RE.sub("", prefix)):
                        del self.expected[index]
                        return prefix + "\r\n"
                del self.expected[index]
                return ""
        return line


class LineAssembler:
    """把任意分块输出切成完整行,残留部分等待空闲时冲刷."""

    _LINE_RE = re.compile(r"(.*?)(\r\n|\n|\r)", re.S)

    def __init__(self, max_partial: int = 256 * 1024):
        self.buffer = ""
        self.max_partial = max_partial

    def feed(self, text: str) -> list[str]:
        self.buffer += text
        lines: list[str] = []
        while True:
            match = self._LINE_RE.match(self.buffer)
            if not match:
                break
            lines.append(match.group(1) + match.group(2))
            self.buffer = self.buffer[match.end() :]
        if len(self.buffer) > self.max_partial:
            # 长时间没有换行的二进制/进度输出,强制吐出,避免吃内存
            lines.append(self.buffer)
            self.buffer = ""
        return lines

    def flush(self) -> str:
        if not self.buffer:
            return ""
        text, self.buffer = self.buffer, ""
        return text


def compile_pattern(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error:
        return re.compile(re.escape(pattern))


# --------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------
@dataclass
class SessionInfo:
    session_id: str
    kind: str
    label: str
    target: str
    created_at: str
    last_active: str
    closed: bool = False
    hidden: bool = False
    subscribers: int = 0
    note: str = ""
    read_only: bool = False

    def to_dict(self, terminal_base: str = "/terminals") -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "kind": self.kind,
            "label": self.label,
            "target": self.target,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "closed": self.closed,
            "hidden": self.hidden,
            "subscribers": self.subscribers,
            "note": self.note,
            "read_only": self.read_only,
            "terminal_url": f"{terminal_base}?session={self.session_id}",
        }


class Console:
    kind = "console"
    read_only = False

    def __init__(
        self,
        hub: "SessionHub",
        session_id: str,
        label: str,
        target: str,
        *,
        prompt: str = "# ",
        encoding: str = "utf-8",
        line_ending: str = "crlf",
        echo_strip: bool = False,
        close_cmd: str = "exit",
    ):
        self.hub = hub
        self.session_id = session_id
        self.label = label
        self.target = target
        self.prompt_re = compile_pattern(prompt)
        self.encoding = encoding or "utf-8"
        self.eol = {"crlf": "\r\n", "lf": "\n", "cr": "\r"}.get(line_ending, "\r\n")
        self.close_cmd = close_cmd
        self.ring = RingBuffer()
        self.stripper = EchoStripper(echo_strip)
        self.assembler = LineAssembler()
        self.created = datetime.now()
        self.last_active = time.monotonic()
        self.closed = False
        self.hidden = False
        self.lock = asyncio.Lock()
        self._decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        self.log = SessionLog(hub.session_log_dir / f"{session_id}.log")
        self.note = ""

    # -- 输出侧 -----------------------------------------------------------
    def feed_bytes(self, data: bytes) -> None:
        if not data:
            return
        self.feed_text(self._decoder.decode(data))

    def feed_text(self, text: str, *, strip: bool = True) -> None:
        if not text:
            return
        for line in self.assembler.feed(text):
            processed = self.stripper.process_line(line) if strip else line
            if processed:
                self.publish(processed)

    def flush_partial(self) -> None:
        partial = self.assembler.flush()
        if partial:
            self.publish(self.stripper.process_line(partial))

    def publish(self, text: str) -> None:
        if not text:
            return
        self.last_active = time.monotonic()
        self.ring.append(text)
        self.log.write(text)
        self.hub.fanout_threadsafe(self.session_id, text)

    # -- 输入侧 -----------------------------------------------------------
    async def write_raw(self, text: str) -> None:
        raise NotImplementedError

    async def close_transport(self) -> None:
        raise NotImplementedError

    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            kind=self.kind,
            label=self.label,
            target=self.target,
            created_at=self.created.isoformat(timespec="seconds"),
            last_active=datetime.fromtimestamp(
                time.time() - (time.monotonic() - self.last_active)
            ).isoformat(timespec="seconds"),
            closed=self.closed,
            hidden=self.hidden,
            subscribers=self.hub.subscriber_count(self.session_id),
            note=self.note,
            read_only=self.read_only,
        )

    def pid(self) -> int | None:
        return None


# --- SSH shell -------------------------------------------------------------
class SshShellSession(Console):
    kind = "ssh"

    def __init__(self, hub: "SessionHub", session_id: str, label: str, cfg: DeviceSshConfig):
        super().__init__(
            hub,
            session_id,
            label,
            f"{cfg.username}@{cfg.host}:{cfg.port}",
            prompt=cfg.shell_prompt,
            echo_strip=False,
            close_cmd=cfg.close_cmd,
        )
        self.target = cfg.host
        self.cfg = cfg
        self.client = None
        self.channel = None
        self._stop = threading.Event()
        self._reader_thread: threading.Thread | None = None

    async def open(self) -> None:
        target = SshTarget.from_device(self.cfg)
        client = await asyncio.to_thread(POOL.get, target)
        transport = client.get_transport()
        if transport is None:
            raise SessionError("SSH 连接不可用", "请检查目标主机与凭据。")
        try:
            channel = await asyncio.to_thread(transport.open_session, 15)
            channel.settimeout(0.2)
            # paramiko 5.x: 终端参数改由 get_pty 传入,invoke_shell 不再收关键字参数
            await asyncio.to_thread(channel.get_pty, term="xterm", width=160, height=48)
            await asyncio.to_thread(channel.invoke_shell)
        except Exception as exc:  # noqa: BLE001
            raise SessionError(f"无法打开 SSH 交互终端: {exc}") from exc
        self.client = client
        self.channel = channel
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"ssh-reader-{self.session_id}", daemon=True
        )
        self._reader_thread.start()
        if self.cfg.shell_prompt:
            await self.hub.delay(0.4)
            result = await self.hub.wait_for(self, self.prompt_re, 6.0, 0, settle=True)
            if not result["matched"]:
                self.note = "未在 6 秒内看到 shell 提示符,命令完成判定可能超时(可在配置页调整)"
        await self.hub.note(self, f"[npa] 已连接 SSH 交互终端 {self.target}\r\n")

    def _reader_loop(self) -> None:
        assert self.channel is not None
        while not self._stop.is_set() and not self.closed:
            try:
                data = self.channel.recv(4096)
            except Exception as exc:  # noqa: BLE001 - socket.timeout 属于正常空闲
                if isinstance(exc, TimeoutError) or exc.__class__.__name__ == "timeout":
                    self.flush_partial()
                    continue
                if not self.closed:
                    self.publish(f"\r\n[npa] SSH 会话读取异常: {exc}\r\n")
                break
            if not data:
                self.publish("\r\n[npa] SSH 会话已结束\r\n")
                self.closed = True
                break
            self.feed_bytes(data)

    async def write_raw(self, text: str) -> None:
        if self.channel is None or self.closed:
            raise SessionError("SSH 会话已关闭", "请重新打开会话。")
        await asyncio.to_thread(self.channel.sendall, text.encode(self.encoding, "replace"))

    async def close_transport(self) -> None:
        self._stop.set()
        try:
            if self.close_cmd and self.channel is not None:
                await asyncio.to_thread(self.channel.sendall, (self.close_cmd + self.eol).encode())
        except Exception:
            pass
        await self.hub.delay(0.2)
        try:
            if self.channel is not None:
                self.channel.close()
        except Exception:
            pass
        try:
            self.log.close()
        except Exception:
            pass
        self.closed = True


# --- Telnet ----------------------------------------------------------------
class TelnetSession(Console):
    kind = "telnet"

    def __init__(
        self, hub: "SessionHub", session_id: str, label: str, cfg: DeviceTelnetConfig
    ):
        super().__init__(
            hub,
            session_id,
            label,
            f"{cfg.username}@{cfg.host}:{cfg.port}",
            prompt=cfg.shell_prompt,
            encoding=cfg.encoding,
            line_ending=cfg.line_ending,
            echo_strip=cfg.echo_strip,
            close_cmd=cfg.close_cmd,
        )
        self.cfg = cfg
        self.target = f"{cfg.host}:{cfg.port}"
        self.reader = None
        self.writer = None
        self._reader_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def open(self) -> None:
        import telnetlib3

        try:
            self.reader, self.writer = await asyncio.wait_for(
                telnetlib3.open_connection(
                    host=self.cfg.host,
                    port=self.cfg.port,
                    encoding=self.encoding,
                    encoding_errors="replace",
                    connect_minwait=0.2,
                    connect_maxwait=5.0,
                ),
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            raise SessionError(
                f"Telnet 连接失败: {self.cfg.host}:{self.cfg.port} ({exc})",
                "确认设备 IP/端口可达,且 telnet 服务已开启。",
            ) from exc
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self.hub.note(self, f"[npa] 已连接 Telnet {self.target}\r\n")
        await self._login()

    async def _login(self) -> None:
        cfg = self.cfg
        if cfg.login_required:
            await self.hub.delay(0.2)
            res = await self.hub.wait_for(
                self, compile_pattern(cfg.login_prompt), 10.0, 0, settle=True
            )
            if not res["matched"]:
                raise SessionError(
                    f"未检测到登录提示 {cfg.login_prompt!r}",
                    "请核对配置页中 telnet 的 login_prompt,或关闭 login_required。",
                )
            await self.write_raw(cfg.username + self.eol)
            res = await self.hub.wait_for(
                self, compile_pattern(cfg.password_prompt), 10.0, 0, settle=True
            )
            if not res["matched"]:
                raise SessionError(
                    f"未检测到密码提示 {cfg.password_prompt!r}",
                    "请核对 password_prompt 配置。",
                )
            await self.write_raw(cfg.password + self.eol)
        else:
            await self.write_raw(self.eol)
        res = await self.hub.wait_for(self, self.prompt_re, 20.0, 0, settle=True)
        if not res["matched"]:
            raise SessionError(
                "登录后未检测到 shell 提示符",
                f"请把配置页中的 shell_prompt 改成设备实际提示符(当前 {self.cfg.shell_prompt!r})。",
            )

    async def _reader_loop(self) -> None:
        assert self.reader is not None
        while not self._stop.is_set() and not self.closed:
            try:
                chunk = await asyncio.wait_for(self.reader.read(4096), timeout=0.12)
            except asyncio.TimeoutError:
                self.flush_partial()
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not self.closed:
                    self.publish(f"\r\n[npa] Telnet 读取异常: {exc}\r\n")
                break
            if chunk == "":
                self.publish("\r\n[npa] Telnet 会话已断开\r\n")
                self.closed = True
                break
            self.feed_text(chunk)

    async def write_raw(self, text: str) -> None:
        if self.writer is None or self.closed:
            raise SessionError("Telnet 会话已关闭", "请重新打开会话。")
        self.writer.write(text)
        await self.writer.drain()

    async def close_transport(self) -> None:
        self._stop.set()
        try:
            if self.close_cmd:
                await self.write_raw(self.close_cmd + self.eol)
        except Exception:
            pass
        await self.hub.delay(0.2)
        if self._reader_task is not None:
            self._reader_task.cancel()
        try:
            if self.writer is not None:
                self.writer.close()
                waiter = getattr(self.writer, "wait_closed", None)
                if waiter is not None:
                    await asyncio.wait_for(waiter(), timeout=3)
        except Exception:
            pass
        self.log.close()
        self.closed = True


# --- Serial ----------------------------------------------------------------
class SerialSession(Console):
    kind = "serial"

    def __init__(
        self, hub: "SessionHub", session_id: str, label: str, cfg: DeviceSerialConfig
    ):
        super().__init__(
            hub,
            session_id,
            label,
            cfg.port,
            prompt=cfg.shell_prompt,
            encoding=cfg.encoding,
            line_ending=cfg.line_ending,
            echo_strip=cfg.echo_strip,
            close_cmd="",
        )
        self.cfg = cfg
        self.target = cfg.port
        self.ser: serial.Serial | None = None
        self._stop = threading.Event()
        self._reader_thread: threading.Thread | None = None

    async def open(self) -> None:
        cfg = self.cfg
        parity = {
            "N": serial.PARITY_NONE,
            "E": serial.PARITY_EVEN,
            "O": serial.PARITY_ODD,
            "M": serial.PARITY_MARK,
            "S": serial.PARITY_SPACE,
        }.get(str(cfg.parity).upper(), serial.PARITY_NONE)
        try:
            self.ser = await asyncio.to_thread(
                serial.Serial,
                port=cfg.port,
                baudrate=cfg.baudrate,
                bytesize=cfg.bytesize,
                parity=parity,
                stopbits=cfg.stopbits,
                timeout=0.12,
                write_timeout=3,
                exclusive=True,
            )
        except (SerialException, OSError, ValueError) as exc:
            raise SessionError(
                f"无法打开串口 {cfg.port}: {exc}",
                "确认串口号正确、未被其他程序占用(如串口助手),且驱动已安装。",
            ) from exc
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"serial-{self.session_id}", daemon=True
        )
        self._reader_thread.start()
        await self.hub.note(self, f"[npa] 已打开串口 {cfg.port} @ {cfg.baudrate}\r\n")
        await self._login()

    async def _login(self) -> None:
        cfg = self.cfg
        if cfg.login_required:
            await self.hub.delay(0.2)
            res = await self.hub.wait_for(
                self, compile_pattern(cfg.login_prompt), 12.0, 0, settle=True
            )
            if not res["matched"]:
                raise SessionError(
                    f"串口未检测到登录提示 {cfg.login_prompt!r}",
                    "确认设备已启动、波特率正确,或调整 login_prompt/关闭 login_required。",
                )
            await self.write_raw(cfg.username + self.eol)
            res = await self.hub.wait_for(
                self, compile_pattern(cfg.password_prompt), 12.0, 0, settle=True
            )
            if not res["matched"]:
                raise SessionError("串口未检测到密码提示", "请核对 password_prompt 配置。")
            await self.write_raw(cfg.password + self.eol)
        else:
            await self.write_raw(self.eol)
        res = await self.hub.wait_for(self, self.prompt_re, 20.0, 0, settle=True)
        if not res["matched"]:
            raise SessionError(
                "串口登录后未检测到 shell 提示符",
                f"请把 shell_prompt 改成设备实际提示符(当前 {cfg.shell_prompt!r})。",
            )

    def _reader_loop(self) -> None:
        assert self.ser is not None
        while not self._stop.is_set() and not self.closed:
            try:
                data = self.ser.read(4096)
            except Exception as exc:  # noqa: BLE001
                if not self.closed:
                    self.publish(f"\r\n[npa] 串口读取异常: {exc}\r\n")
                break
            if data:
                self.feed_bytes(data)
            else:
                self.flush_partial()

    async def write_raw(self, text: str) -> None:
        if self.ser is None or self.closed:
            raise SessionError("串口会话已关闭", "请重新打开会话。")
        data = text.encode(self.encoding, "replace")

        def _write() -> None:
            assert self.ser is not None
            self.ser.write(data)
            self.ser.flush()

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:  # noqa: BLE001
            raise SessionError(f"串口写入失败: {exc}", "检查 USB 串口是否被拔出。") from exc

    async def close_transport(self) -> None:
        self._stop.set()
        try:
            if self.ser is not None:
                await asyncio.to_thread(self.ser.close)
        except Exception:
            pass
        self.log.close()
        self.closed = True


# --- 只读镜像(代码服务器命令 / 电源设备) ---------------------------------
class MirrorSession(Console):
    kind = "mirror"
    read_only = True

    def __init__(self, hub: "SessionHub", session_id: str, label: str, target: str = ""):
        super().__init__(hub, session_id, label, target or label, prompt="", echo_strip=False)

    async def open(self) -> None:
        return

    async def write_raw(self, text: str) -> None:
        raise SessionError("这是只读镜像终端", "请直接在终端页使用设备交互会话输入命令。")

    async def close_transport(self) -> None:
        self.log.close()
        self.closed = True


# --------------------------------------------------------------------------
# SessionHub
# --------------------------------------------------------------------------
class SessionHub:
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.loop = asyncio.get_event_loop()
        cfg = config_manager.load()
        self.session_log_dir = cfg.paths.log_path() / SESSION_LOG_DIRNAME
        ensure_dir(self.session_log_dir)
        self.sessions: dict[str, Console] = {}
        self._subscribers: dict[str, set[Callable[[str], None]]] = {}
        self._counter = 0
        self._maintenance: asyncio.Task | None = None
        # 新会话创建后的回调(Runtime 用来打开独立控制台窗口)
        self.on_session_open: Callable[[Console], None] | None = None

    def _notify_open(self, session: Console) -> None:
        if self.on_session_open is None:
            return
        try:
            self.on_session_open(session)
        except Exception:  # noqa: BLE001
            log.debug("会话打开回调异常", exc_info=True)

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        if self._maintenance is None:
            self._maintenance = asyncio.create_task(self._maintenance_loop())

    async def close_all(self) -> None:
        if self._maintenance is not None:
            self._maintenance.cancel()
            self._maintenance = None
        for session in list(self.sessions.values()):
            try:
                await session.close_transport()
            except Exception:
                pass
        self.sessions.clear()
        self._subscribers.clear()

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for session in list(self.sessions.values()):
                if session.closed:
                    self.sessions.pop(session.session_id, None)
                    continue
                if self.subscriber_count(session.session_id):
                    continue
                if now - session.last_active > IDLE_RECYCLE_SEC:
                    await self.note(session, "\r\n[npa] 会话空闲 30 分钟,自动回收\r\n")
                    try:
                        await session.close_transport()
                    finally:
                        self.sessions.pop(session.session_id, None)

    # -- 订阅 -------------------------------------------------------------
    def subscribe(self, session_id: str, callback: Callable[[str], None]) -> Callable[[], None]:
        self._subscribers.setdefault(session_id, set()).add(callback)

        def unsubscribe() -> None:
            subscribers = self._subscribers.get(session_id)
            if subscribers is not None:
                subscribers.discard(callback)
                if not subscribers:
                    self._subscribers.pop(session_id, None)

        return unsubscribe

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subscribers.get(session_id, ()))

    def _fanout(self, session_id: str, text: str) -> None:
        callbacks = list(self._subscribers.get(session_id, ()))
        for callback in callbacks:
            try:
                callback(text)
            except Exception:
                log.debug("终端订阅回调异常", exc_info=True)

    def fanout_threadsafe(self, session_id: str, text: str) -> None:
        try:
            self.loop.call_soon_threadsafe(self._fanout, session_id, text)
        except RuntimeError:
            pass

    # -- 创建 -------------------------------------------------------------
    def _new_id(self, kind: str) -> str:
        self._counter += 1
        return f"{kind}-{self._counter}"

    async def open_device(self, kind: str | None = None, label: str = "") -> Console:
        cfg = self.config_manager.load().device
        kind = kind or cfg.prefer
        for existing in self.sessions.values():
            if existing.kind == kind and not existing.closed:
                # 之前“关闭视图”的会话底层仍在运行,直接复用并重新显示
                existing.hidden = False
                return existing
        session_id = self._new_id(kind)
        if kind == "serial":
            if not cfg.serial.enabled:
                raise SessionError("串口未启用", "请在配置页 device.serial 勾选启用并填写串口号。")
            session = SerialSession(self, session_id, label or "设备串口", cfg.serial)
        elif kind == "telnet":
            if not cfg.telnet.enabled:
                raise SessionError("Telnet 未启用", "请在配置页 device.telnet 勾选启用。")
            session = TelnetSession(self, session_id, label or "设备 Telnet", cfg.telnet)
        elif kind == "ssh":
            if not cfg.ssh.enabled:
                raise SessionError("设备 SSH 未启用", "请在配置页 device.ssh 勾选启用。")
            if not SshTarget.from_device(cfg.ssh).ready():
                raise SessionError(
                    "设备 SSH 凭据未配置", "请在配置页填写设备 SSH 账号与密码/私钥。"
                )
            session = SshShellSession(self, session_id, label or "设备 SSH", cfg.ssh)
        else:
            raise SessionError(f"未知会话类型: {kind}", "kind 只能是 ssh/telnet/serial。")
        self.sessions[session_id] = session
        try:
            await session.open()
        except Exception:
            self.sessions.pop(session_id, None)
            try:
                await session.close_transport()
            except Exception:
                pass
            raise
        self._notify_open(session)
        return session

    async def open_or_get(self, kind: str) -> Console:
        return await self.open_device(kind)

    def open_mirror(self, label: str, target: str = "") -> Console:
        """创建/复用一个只读镜像终端,用于展示 ssh_exec / 电源命令."""
        for session in self.sessions.values():
            if session.kind == "mirror" and session.label == label and not session.closed:
                return session
        session = MirrorSession(self, self._new_id("mirror"), label, target)
        self.sessions[session.session_id] = session
        self._notify_open(session)
        return session

    def get(self, session_id: str) -> Console:
        session = self.sessions.get(session_id)
        if session is None:
            raise SessionError(f"会话不存在: {session_id}", "先调用 session_open 或 session_list。")
        return session

    def list(self, include_closed: bool = False) -> list[dict[str, Any]]:
        result = []
        for session in self.sessions.values():
            if session.closed and not include_closed:
                continue
            result.append(session.info().to_dict())
        result.sort(key=lambda item: item["created_at"])
        return result

    # -- 收发 -------------------------------------------------------------
    async def note(self, session: Console, text: str) -> None:
        session.publish(text)

    async def send(self, session: Console, data: str, *, echo_expect: str | None = None) -> None:
        if session.closed:
            raise SessionError("会话已关闭", "请重新打开会话。")
        if session.read_only:
            raise SessionError("这是只读镜像终端", "请使用设备交互会话输入命令。")
        async with session.lock:
            if echo_expect:
                session.stripper.expect(echo_expect)
            await session.write_raw(data)
            session.last_active = time.monotonic()

    async def exec_on_console(
        self, session: Console, command: str, timeout: float = 120.0, display: bool = True
    ) -> dict[str, Any]:
        """在交互控制台执行一条命令,以 shell_prompt 判定结束."""
        if session.closed:
            raise SessionError("会话已关闭", "请重新打开会话。")
        async with session.lock:
            if not _tail_at_prompt(session):
                # 上一条命令(例如网页终端里手动敲的)的提示符可能还在路上,
                # 迟到后会被误判成这次命令的结束;先等它落到缓冲末尾再记序号。
                await self.wait_for(session, session.prompt_re, 1.0, session.ring.next_seq)
            seq = session.ring.next_seq
            if display:
                await self.note(session, f"\r\n[npa] $ {command}\r\n")
            session.stripper.expect(command)
            await session.write_raw(command + session.eol)
            session.last_active = time.monotonic()
        result = await self.wait_for(session, session.prompt_re, timeout, seq)
        output = result["text"]
        output = _strip_echo_prefix(output, command)
        return {
            "output": output,
            "matched_prompt": result["matched"],
            "timed_out": result["timed_out"],
            "match": result["match"],
            "duration_sec": round(result["duration_sec"], 3),
        }

    async def wait_for(
        self,
        session: Console,
        pattern: re.Pattern[str],
        timeout: float,
        since_seq: int,
        *,
        settle: bool = False,
        require_tail: bool = True,
    ) -> dict[str, Any]:
        """增量扫描 ring,匹配模式.

        require_tail=True 用于等 shell 提示符:匹配必须出现在当前输出末尾
        附近,避免把之前出现过的提示符误判为本次命令结束;等待用户指定的
        业务输出(如编译结果)时传 False,出现过即算命中。
        """
        started = time.monotonic()
        deadline = started + max(0.1, timeout)
        last_text = ""
        while True:
            _, text = session.ring.since(since_seq)
            last_text = text
            match = None
            for candidate in pattern.finditer(text):
                match = candidate
            if match is not None:
                tail = text[match.end() :]
                if not require_tail or len(tail.rstrip("\r\n \t")) <= 2:
                    return {
                        "matched": True,
                        "timed_out": False,
                        "text": text,
                        "match": match.group(0),
                        "duration_sec": time.monotonic() - started,
                    }
            if session.closed:
                return {
                    "matched": False,
                    "timed_out": False,
                    "text": last_text,
                    "match": "",
                    "duration_sec": time.monotonic() - started,
                    "closed": True,
                }
            if time.monotonic() >= deadline:
                if settle:
                    return {
                        "matched": False,
                        "timed_out": True,
                        "text": last_text,
                        "match": "",
                        "duration_sec": time.monotonic() - started,
                    }
                return {
                    "matched": False,
                    "timed_out": True,
                    "text": last_text,
                    "match": "",
                    "duration_sec": time.monotonic() - started,
                }
            await asyncio.sleep(0.05)

    async def delay(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def close_session(self, session_id: str, force: bool = False) -> dict[str, Any]:
        session = self.get(session_id)
        if force:
            await session.close_transport()
            # 保留已关闭的会话记录,便于事后 session_read/session_list 查看
            # 最后一段输出;维护协程会在下一轮(≤60s)回收。
            return {"session_id": session_id, "closed": True, "transport_closed": True}
        session.hidden = True
        for callback in list(self._subscribers.get(session_id, ())):
            try:
                callback("\r\n[npa] 终端视图已关闭,底层会话仍在运行;可再次 session_open 查看。\r\n")
            except Exception:
                pass
        return {
            "session_id": session_id,
            "closed": False,
            "hidden": True,
            "hint": "视图已关闭,底层会话保留。需要彻底断开请传 force=true。",
        }

    def tail(self, session_id: str, lines: int = 200) -> str:
        return self.get(session_id).ring.tail(lines)

    def since(self, session_id: str, seq: int) -> dict[str, Any]:
        session = self.get(session_id)
        start, text = session.ring.since(seq)
        return {
            "session_id": session_id,
            "start_seq": start,
            "next_seq": session.ring.next_seq,
            "text": text,
            "closed": session.closed,
        }


def _strip_echo_prefix(output: str, command: str) -> str:
    """兜底:去掉输出开头与命令相同的一行(设备回显)."""
    if not output:
        return output
    lines = output.splitlines(keepends=True)
    stripped_command = _WS_RE.sub("", command)
    for index in range(min(3, len(lines))):
        if _WS_RE.sub("", _ANSI_RE.sub("", lines[index])).strip() == stripped_command:
            return "".join(lines[:index] + lines[index + 1 :])
    return output


def _tail_at_prompt(session: Console) -> bool:
    """当前缓冲末尾是否已经停在 shell 提示符上(说明上一轮命令已收尾)."""
    tail = session.ring.tail(8)
    if not tail:
        # 还没有任何输出:新会话由 _login 保证已就绪,无需等待。
        return True
    matches = list(session.prompt_re.finditer(tail))
    if not matches:
        return False
    return not tail[matches[-1].end() :].strip(" \t\r\n")


def quote_command(command: str) -> str:
    return shell_quote(command)


__all__ = [
    "Console",
    "SessionError",
    "SessionHub",
    "SshTarget",
    "TransportError",
    "AuthConfig",
]

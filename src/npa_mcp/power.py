"""电源管理设备(PDU)客户端.

支持 telnet(常用)与 ssh 两种传输。每条命令都按配置的 ok/fail 关键字判定,
判定不了就返回原始输出,绝不静默当作成功。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import PowerConfig
from .transports import SshTarget, TransportError, ssh_exec
from .util import get_logger

log = get_logger(__name__)


class PowerError(RuntimeError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass
class PowerResult:
    action: str
    outlet: int
    command: str
    ok: bool
    determined: bool
    output: str
    duration_sec: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "outlet": self.outlet,
            "command": self.command,
            "ok": self.ok,
            "determined": self.determined,
            "output": self.output,
            "duration_sec": round(self.duration_sec, 3),
        }


def _find_first(text: str, patterns: list[str]) -> tuple[str, int] | None:
    """返回最早出现的 (关键字, 位置);同一位置取列表顺序靠前者."""
    best: tuple[str, int] | None = None
    for pattern in patterns:
        match = re.search(re.escape(pattern), text, re.IGNORECASE)
        if match is None:
            continue
        if best is None or match.start() < best[1]:
            best = (pattern, match.start())
    return best


class PduClient:
    def __init__(
        self,
        cfg: PowerConfig,
        on_output: Callable[[str], None] | None = None,
    ):
        self.cfg = cfg
        self.on_output = on_output
        self._reader = None
        self._writer = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0

    # -- telnet 连接 ------------------------------------------------------
    async def _connect(self) -> None:
        import telnetlib3

        cfg = self.cfg
        try:
            self._reader, self._writer = await asyncio.wait_for(
                telnetlib3.open_connection(
                    host=cfg.host,
                    port=cfg.port,
                    encoding=cfg.encoding,
                    encoding_errors="replace",
                    connect_minwait=0.2,
                    connect_maxwait=5.0,
                ),
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            raise PowerError(
                f"无法连接电源管理设备 {cfg.host}:{cfg.port}: {exc}",
                "确认设备 IP/端口可达,telnet 服务已开启。",
            ) from exc
        if cfg.login_required:
            await self._expect(cfg.login_prompt, 10.0, "登录提示")
            await self._send(cfg.username)
            await self._expect(cfg.password_prompt, 10.0, "密码提示")
            await self._send(cfg.password)
        else:
            await self._send("")
        await self._expect(cfg.prompt, 10.0, "命令提示符")
        self._last_used = time.monotonic()

    async def _send(self, line: str) -> None:
        if self._writer is None:
            raise PowerError("电源设备连接已断开", "请重试。")
        self._writer.write(line + "\r\n")
        await self._writer.drain()

    async def _expect(self, pattern: str, timeout: float, what: str) -> str:
        assert self._reader is not None
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        buffer = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=0.3)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                raise PowerError(f"读取电源设备输出失败: {exc}") from exc
            if chunk == "":
                raise PowerError("电源设备已断开连接", "检查设备电源与网络。")
            buffer += chunk
            if self.on_output:
                try:
                    self.on_output(chunk)
                except Exception:
                    pass
            if regex.search(buffer):
                return buffer
        raise PowerError(
            f"等待{what} {pattern!r} 超时({timeout:.0f}s)",
            "核对配置页中电源设备的提示符/登录提示。",
        )

    async def _close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                waiter = getattr(self._writer, "wait_closed", None)
                if waiter is not None:
                    await asyncio.wait_for(waiter(), timeout=3)
            except Exception:
                pass
        self._reader = None
        self._writer = None

    # -- 对外 -------------------------------------------------------------
    async def run(self, action: str, outlet: int | None = None) -> PowerResult:
        cfg = self.cfg
        outlet = int(outlet if outlet is not None else cfg.outlet)
        template = {
            "on": cfg.commands.on,
            "off": cfg.commands.off,
            "status": cfg.commands.status,
        }.get(action)
        if template is None:
            raise PowerError(f"未知电源动作: {action}", "只能是 on/off/status。")
        try:
            command = template.format(outlet=outlet)
        except KeyError as exc:
            raise PowerError(
                f"电源命令模板含未知占位符: {exc}", "模板只支持 {outlet} 占位符。"
            ) from exc

        started = time.monotonic()
        if cfg.transport == "ssh":
            output = await self._run_ssh(command)
        else:
            output = await self._run_telnet(command)
        ok_hit = _find_first(output, cfg.expect.ok)
        fail_hit = _find_first(output, cfg.expect.fail)
        determined = False
        ok = False
        if ok_hit and (fail_hit is None or ok_hit[1] <= fail_hit[1]):
            ok, determined = True, True
        elif fail_hit:
            ok, determined = False, True
        # status 的额外推断:输出里出现 ON/OFF 也算判定成功
        if action == "status" and not determined:
            upper = output.upper()
            if re.search(r"\bON\b", upper):
                ok, determined = True, True
            elif re.search(r"\bOFF\b", upper):
                ok, determined = True, True
        result = PowerResult(
            action=action,
            outlet=outlet,
            command=command,
            ok=ok,
            determined=determined,
            output=output.strip(),
            duration_sec=time.monotonic() - started,
        )
        return result

    async def _run_telnet(self, command: str) -> str:
        async with self._lock:
            if self.cfg.session_reuse and self._writer is not None:
                if time.monotonic() - self._last_used > 120:
                    await self._close()
            if self._writer is None:
                await self._connect()
            try:
                await self._send(command)
                output = await self._expect(self.cfg.prompt, self.cfg.timeout_sec, "命令结果")
                self._last_used = time.monotonic()
                return _strip_command_echo(output, command)
            except Exception:
                await self._close()
                raise

    async def _run_ssh(self, command: str) -> str:
        target = SshTarget(
            host=self.cfg.host,
            port=self.cfg.port,
            username=self.cfg.username,
            auth_type="password",
            password=self.cfg.password,
        )
        try:
            result = await asyncio.to_thread(
                ssh_exec, target, command, timeout=self.cfg.timeout_sec
            )
        except TransportError as exc:
            raise PowerError(exc.message, exc.hint) from exc
        if self.on_output:
            try:
                self.on_output(result.stdout + result.stderr)
            except Exception:
                pass
        if result.timed_out:
            raise PowerError("电源设备命令执行超时", "调大 timeout_sec 或检查设备状态。")
        return result.stdout + result.stderr

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    async def status_snapshot(self) -> PowerResult:
        return await self.run("status")


def _strip_command_echo(output: str, command: str) -> str:
    lines = output.splitlines(keepends=True)
    for index in range(min(2, len(lines))):
        if lines[index].strip().endswith(command.strip()):
            return "".join(lines[:index] + lines[index + 1 :])
    return output

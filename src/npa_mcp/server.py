"""npa-mcp 服务器:FastMCP 工具定义与进程生命周期.

一个进程里同时运行:

- FastMCP stdio 服务器(Claude Code 通过它调用工具);
- uvicorn 本地网页服务(凭据配置页、xterm 终端页、镜像下载、报告浏览);
- 若干文件服务线程(TFTP/FTP)与会话读取线程。

所有工具统一返回 ``{"ok": bool, ...}``;失败时带中文 ``error`` 与
``hint``。工具内部从不抛出裸 traceback,异常由 :func:`_guard` 收敛。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import inspect
import os
import posixpath
import re
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastmcp import FastMCP

from . import __version__
from .config import (
    Config,
    ConfigError,
    ConfigManager,
    auth_summary,
    detect_lan_ip,
    npa_home,
)
from .fileservers import FileserverManager
from .power import PduClient, PowerError
from .report import generate_report as _generate_report
from .scheduler import (
    ScheduleError,
    read_last_run,
    read_last_session,
    register_all,
    register_window,
    status_report,
    task_name_for,
    unregister_task,
)
from .sessions import Console, SessionError, SessionHub, compile_pattern
from .transports import (
    SshTarget,
    TransportError,
    close_all,
    is_windows_path,
    sftp_download,
    sftp_list,
    sftp_read,
    sftp_upload,
    sftp_write,
    shell_quote,
)
from .transports import (
    ssh_exec as ssh_exec_impl,
)
from .util import (
    atomic_write_text,
    get_logger,
    md5_file,
    open_url,
    safe_join,
    setup_logging,
    truncate_text,
)
from .webapp import RuntimeState, create_app

log = get_logger(__name__)

mcp = FastMCP(
    name="npa-mcp",
    instructions=(
        "嵌入式开发远程操作服务器。可远程编辑/编译代码服务器上的工程、"
        "通过 SSH/Telnet/串口操作开发板、让开发板用 tftp/wget/ftpget/scp "
        "拉取固件、telnet 控制电源管理设备、按时间窗口自主工作并生成中文 "
        "HTML 总结报告。凭据只能由用户在本地配置页填写;调用 list_targets "
        "查看当前环境、open_config_page 打开配置页。串口/telnet/SSH 等会话"
        "创建时会自动弹出独立终端窗口(类似 cmd,可实时观看和输入),"
        "工具返回里的 terminal_url 只是浏览器备用视图。"
    ),
)

RUNTIME: Runtime | None = None
_REMOTE_HOMES: dict[str, str] = {}
_DEFAULT_TIMEOUT = 120.0
_MAX_TIMEOUT = 3600.0
_MAX_OUTPUT = 300_000


# ---------------------------------------------------------------------------
# 统一错误包装
# ---------------------------------------------------------------------------
class ToolFailure(RuntimeError):
    """工具层主动失败,直接变成 {"ok": false, "error", "hint"}."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


def _guard(fn: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[dict[str, Any]]]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except ToolFailure as exc:
            return {"ok": False, "error": exc.message, "hint": exc.hint}
        except (SessionError, TransportError, PowerError, ScheduleError) as exc:
            return {"ok": False, "error": exc.message, "hint": exc.hint}
        except ConfigError as exc:
            return {
                "ok": False,
                "error": f"配置错误: {exc}",
                "hint": "打开配置页检查并保存;必要时备份后删除 ~/.npa-mcp/config.json。",
            }
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": "操作超时",
                "hint": "调大 timeout,或检查网络、设备电源与串口连接。",
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("工具 %s 执行失败", fn.__name__)
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "查看 ~/.npa-mcp/logs 下的日志了解详情。",
            }

    return wrapper


def _runtime() -> Runtime:
    if RUNTIME is None:
        raise ToolFailure("服务器尚未完成初始化", "请稍后重试;若持续失败请查看 MCP 日志。")
    return RUNTIME


def _limit(text: str, max_bytes: int = _MAX_OUTPUT) -> tuple[str, bool]:
    return truncate_text(text or "", max_bytes)


def _clamp_timeout(value: float, default: float, maximum: float = _MAX_TIMEOUT) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if number <= 0:
        number = default
    return max(0.5, min(number, maximum))


def _terminal_url(session_id: str) -> str:
    rt = RUNTIME
    if rt is None or not rt.web_url or not session_id:
        return ""
    return f"{rt.web_url}/terminals?session={session_id}"


def _terminals_url() -> str:
    rt = RUNTIME
    return f"{rt.web_url}/terminals" if rt is not None and rt.web_url else ""


def _session_payload(session: Console) -> dict[str, Any]:
    payload = session.info().to_dict()
    payload["terminal_url"] = _terminal_url(session.session_id)
    return payload


# ---------------------------------------------------------------------------
# 运行时
# ---------------------------------------------------------------------------
class _QuietServer(uvicorn.Server):
    """不安装 SIGINT/SIGTERM 处理器,信号交给 MCP 主循环."""

    def install_signal_handlers(self) -> None:  # pragma: no cover - 平台相关
        return None


def _bind_socket(host: str, port: int) -> socket.socket | None:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    last_error: OSError | None = None
    for family, socktype, proto, _name, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                try:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass
            sock.bind(sockaddr)
            sock.listen(128)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            last_error = exc
            try:
                sock.close()
            except OSError:
                pass
    if last_error is not None:
        log.debug("绑定 %s:%s 失败: %s", host, port, last_error)
    return None


class Runtime:
    def __init__(self, config_path: str | None = None):
        self.manager = ConfigManager(config_path)
        self.state = RuntimeState()
        self.state.version = __version__
        self.hub: SessionHub | None = None
        self.fileservers: FileserverManager | None = None
        self.web_server: _QuietServer | None = None
        self.web_task: asyncio.Task[Any] | None = None
        self.web_port = 0
        self.web_url = ""
        self.web_error = ""
        self._socket: socket.socket | None = None
        self.pdu: PduClient | None = None
        self.pdu_key = ""
        self.pdu_mirror: Console | None = None
        self._shutting_down = False
        self._term_procs: dict[str, subprocess.Popen[bytes]] = {}

    # -- 启动 -------------------------------------------------------------
    async def start(self, no_web: bool = False) -> None:
        cfg = self.manager.load(force=True)
        cfg.ensure_dirs()
        self.hub = SessionHub(self.manager)
        self.hub.on_session_open = self._handle_session_open
        self.hub.start()
        self.fileservers = FileserverManager(cfg)
        await asyncio.to_thread(self.fileservers.start)

        if no_web:
            self.fileservers.update_http_port(cfg.web.port, running=False)
            self.web_error = "启动参数 --no-web"
            return
        try:
            port = await self._start_web(cfg)
        except Exception as exc:  # noqa: BLE001
            self.web_error = f"{type(exc).__name__}: {exc}"
            log.exception("本地网页服务启动失败")
            self.fileservers.update_http_port(cfg.web.port, running=False)
            return
        self.web_port = port
        self.state.web_port = port
        self.fileservers.http_port = port
        self.fileservers.update_http_port(port, running=cfg.firmware_server.http.enabled)
        try:
            atomic_write_text(self.manager.path.parent / "web_port.txt", f"{port}\n")
        except OSError as exc:
            log.warning("写入 web_port.txt 失败: %s", exc)

    # -- 独立控制台窗口 ---------------------------------------------------
    def _handle_session_open(self, session: Console) -> None:
        try:
            self.ensure_terminal_window(session)
        except Exception:  # noqa: BLE001
            log.debug("打开独立终端窗口异常", exc_info=True)

    def ensure_terminal_window(self, session: Console) -> bool:
        """为会话打开一个原生控制台窗口(类似 cmd),已在运行时直接复用."""
        if not self.web_url or sys.platform != "win32":
            return False
        if os.environ.get("NPA_MCP_WINDOW") or os.environ.get("NPA_MCP_NO_WINDOW"):
            return False
        try:
            if not self.manager.load().web.native_terminal:
                return False
        except Exception:  # noqa: BLE001
            return False
        existing = self._term_procs.get(session.session_id)
        if existing is not None and existing.poll() is None:
            return True
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        title = f"npa-mcp · {session.label} · {session.session_id}"
        args = [
            sys.executable,
            "-m",
            "npa_mcp.term",
            "--url",
            self.web_url,
            "--session",
            session.session_id,
            "--title",
            title,
        ]
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(self.manager.path.parent),
                env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        except OSError as exc:
            log.warning("打开独立终端窗口失败: %s", exc)
            return False
        self._term_procs[session.session_id] = proc
        log.info("已打开独立终端窗口: %s (pid %s)", title, proc.pid)
        return True

    def _close_terminal_windows(self) -> None:
        for proc in self._term_procs.values():
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        self._term_procs.clear()

    async def _start_web(self, cfg: Config) -> int:
        if self.hub is None or self.fileservers is None:
            raise RuntimeError("会话或文件服务尚未初始化")
        app = create_app(self.manager, self.hub, self.fileservers, self.state)
        host = (cfg.web.host or "127.0.0.1").strip()
        base_port = int(cfg.web.port or 8765)
        log.info("启动本地网页服务: http://%s:%s", host, base_port)
        last_error = ""
        for offset in range(11):
            port = base_port + offset
            sock = _bind_socket(host, port)
            if sock is None:
                last_error = f"端口 {port} 无法绑定(可能已被占用)"
                continue
            try:
                actual_port = int(sock.getsockname()[1])
            except OSError:
                actual_port = port
            config = uvicorn.Config(
                app,
                host=host,
                port=actual_port,
                log_level="warning",
                access_log=False,
                lifespan="on",
                log_config=None,
                server_header=False,
                proxy_headers=False,
            )
            server = _QuietServer(config)
            task = asyncio.create_task(
                server.serve(sockets=[sock]), name=f"npa-web-{actual_port}"
            )
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if server.started or task.done():
                    break
                await asyncio.sleep(0.05)
            if server.started:
                self.web_server = server
                self.web_task = task
                self._socket = sock
                display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
                self.web_url = f"http://{display_host}:{actual_port}"
                log.info("网页服务就绪: %s", self.web_url)
                return actual_port
            # 启动失败,清理后换下一个端口
            server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except Exception:  # noqa: BLE001
                pass
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
            try:
                sock.close()
            except OSError:
                pass
            last_error = f"端口 {port} 启动失败"
        raise RuntimeError(last_error or "无法启动本地网页服务")

    # -- 关闭 -------------------------------------------------------------
    async def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("开始关闭 npa-mcp 运行时")
        self._close_terminal_windows()
        if self.pdu is not None:
            try:
                await asyncio.wait_for(self.pdu.close(), timeout=5.0)
            except Exception:  # noqa: BLE001
                pass
            self.pdu = None
            self.pdu_mirror = None
        if self.web_server is not None:
            self.web_server.should_exit = True
        if self.web_task is not None:
            task = self.web_task
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
            except Exception:  # noqa: BLE001
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass
            self.web_task = None
            self.web_server = None
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        if self.hub is not None:
            try:
                await self.hub.close_all()
            except Exception:  # noqa: BLE001
                log.exception("关闭会话失败")
        if self.fileservers is not None:
            try:
                await asyncio.to_thread(self.fileservers.stop)
            except Exception:  # noqa: BLE001
                log.exception("关闭文件服务失败")
        try:
            await asyncio.to_thread(close_all)
        except Exception:  # noqa: BLE001
            log.exception("关闭 SSH 连接池失败")
        log.info("npa-mcp 运行时已关闭")

    def keep_alive_sec(self) -> float:
        try:
            value = float(self.manager.load().web.keep_alive_sec)
        except Exception:  # noqa: BLE001
            return 0.0
        return max(0.0, min(value, 86400.0))


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------
def _key_file_exists(auth: Any) -> bool:
    if auth.type != "key":
        return True
    text = (auth.key_file or "").strip()
    if not text:
        return False
    return Path(text).expanduser().exists()


def _ssh_ready(host: str, username: str, auth: Any) -> bool:
    if not host or not username:
        return False
    if auth.type == "key":
        return _key_file_exists(auth)
    return bool(auth.password)


def _require_code_server(cfg: Config) -> tuple[SshTarget, Any]:
    cs = cfg.code_server
    if not cs.host or not cs.username:
        raise ToolFailure(
            "代码服务器未配置",
            "打开配置页填写 code_server.host / username / 密码或私钥后保存。",
        )
    if not _key_file_exists(cs.auth):
        raise ToolFailure(
            "代码服务器私钥文件不存在",
            f"检查配置页 code_server.auth.key_file,当前路径: {cs.auth.key_file or '(空)'}",
        )
    target = SshTarget.from_code_server(cs)
    if not target.ready():
        raise ToolFailure(
            "代码服务器凭据不完整",
            "打开配置页填写密码,或选择私钥认证并指定私钥文件。",
        )
    return target, cs


async def _resolve_remote(target: SshTarget, remote_path: str, default_dir: str = "") -> str:
    """把 ``~`` / 相对路径解析成远端绝对路径(SFTP 不展开 ~)."""
    text = (remote_path or "").strip()
    if not text:
        if not default_dir:
            raise ToolFailure("远端路径为空", "请提供 remote_path。")
        text = default_dir
    if not text.startswith(("/", "~")) and not is_windows_path(text):
        base = default_dir or "~"
        text = posixpath.join(base, text)
    if text == "~" or text.startswith("~/"):
        home = await _remote_home(target)
        text = home if text == "~" else posixpath.join(home, text[2:])
    return text


async def _remote_home(target: SshTarget) -> str:
    key = target.key()
    cached = _REMOTE_HOMES.get(key)
    if cached:
        return cached
    result = await asyncio.to_thread(
        ssh_exec_impl, target, 'printf %s "$HOME"', timeout=20.0
    )
    home = (result.stdout or "").strip()
    if not home:
        raise ToolFailure(
            "无法确定远端用户主目录",
            "请把配置中的 workdir 改成绝对路径,例如 /home/dev/project。",
        )
    _REMOTE_HOMES[key] = home
    return home


def _mask_command(command: str) -> str:
    masked = re.sub(r"(\s-p\s+)(\S+)", r"\1***", command)
    masked = re.sub(r"(--password[= ])(\S+)", r"\1***", masked, flags=re.IGNORECASE)
    return masked


def _extract_md5(text: str) -> str:
    match = re.search(r"\b([0-9a-fA-F]{32})\b", text or "")
    return match.group(1).lower() if match else ""


def _parse_mode(text: str) -> int:
    raw = (text or "644").strip().lower()
    if raw.startswith("0o"):
        raw = raw[2:]
    try:
        value = int(raw, 8)
    except ValueError as exc:
        raise ToolFailure(f"非法文件权限: {text}", "mode 使用八进制,例如 644 / 755。") from exc
    if value < 0 or value > 0o7777:
        raise ToolFailure(f"文件权限超出范围: {text}", "mode 取值范围 0..7777。")
    return value


def _newest_firmware(cfg: Config, filename: str) -> tuple[Path, str]:
    # 先 resolve:配置里可能写成 8.3 短路径(如 C:\Users\深海的~1\...),
    # safe_join 返回的是解析后的长路径,两者混用会让 relative_to 抛 ValueError。
    image_dir = cfg.paths.image_path().resolve()
    if not image_dir.exists():
        raise ToolFailure("镜像目录不存在", f"检查配置页 paths.image_dir,当前: {image_dir}")
    if filename.strip():
        relative = filename.strip().replace("\\", "/").lstrip("/")
        path = safe_join(image_dir, relative)
        if path is None:
            raise ToolFailure(
                "文件名超出镜像目录",
                "只能拉取 paths.image_dir 下的文件;不要使用 .. 或绝对路径。",
            )
        if not path.is_file():
            raise ToolFailure(
                f"镜像文件不存在: {relative}",
                f"把固件放到 {image_dir},或先用 ssh_download 拉回本机。",
            )
        return path, path.relative_to(image_dir).as_posix()
    candidates = [
        item
        for item in image_dir.rglob("*")
        if item.is_file()
        and not item.name.startswith(".")
        and item.suffix.lower() not in {".part", ".tmp", ".bak"}
    ]
    if not candidates:
        raise ToolFailure(
            "镜像目录中没有可拉取的固件",
            f"把固件放到 {image_dir} 后重试,或传入 filename 指定文件。",
        )
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    return latest, latest.relative_to(image_dir).as_posix()


def _remote_target(cfg: Config, remote_path: str, filename: str) -> str:
    raw = (remote_path or "").strip()
    if raw:
        if raw.endswith(("/", "\\")):
            return posixpath.join(raw.replace("\\", "/").rstrip("/"), filename)
        return raw
    base = (cfg.firmware.device_pull_dir or "/tmp").strip()
    return posixpath.join(base.rstrip("/") or "/", filename)


def _pull_hints(cfg: Config, rt: Runtime, filename: str, remote_path: str, token: str) -> dict[str, str]:
    host = cfg.firmware_host()
    http_port = rt.web_port or cfg.web.port
    values = {
        "file": filename,
        "host": host,
        "http_port": http_port,
        "port": cfg.firmware_server.ftp.port,
        "user": cfg.firmware_server.ftp.username,
        "pass": cfg.firmware_server.ftp.password,
        "remote_path": remote_path,
        "token": token if token else cfg.firmware_server.http.token,
    }
    hints: dict[str, str] = {}
    for name, template in cfg.firmware.pull_templates.model_dump().items():
        try:
            hints[name] = str(template).format(**values)
        except (KeyError, IndexError, ValueError) as exc:
            hints[name] = f"<模板不可用: {exc}>"
    return hints


def _infer_power_state(output: str) -> str:
    text = (output or "").upper()
    on = bool(re.search(r"\bON\b", text))
    off = bool(re.search(r"\bOFF\b", text))
    if on and not off:
        return "on"
    if off and not on:
        return "off"
    return "unknown"


async def _remote_md5(target: SshTarget, remote_path: str) -> tuple[str, Any]:
    quoted = shell_quote(remote_path)
    last: Any = None
    for template in ("md5sum -- {path}", "md5 -- {path}", "openssl md5 -- {path}"):
        command = template.format(path=quoted)
        result = await asyncio.to_thread(ssh_exec_impl, target, command, timeout=30.0)
        last = result
        if result.exit_code == 0 and not result.timed_out:
            digest = _extract_md5(result.stdout + result.stderr)
            if digest:
                return digest, result
    return "", last


async def _get_pdu(rt: Runtime, cfg: Config) -> tuple[PduClient, Console]:
    if rt.hub is None:
        raise ToolFailure("会话系统未初始化", "重启 npa-mcp 后重试。")
    key = cfg.power.model_dump_json()
    stale = (
        rt.pdu is not None
        and (rt.pdu_key != key or rt.pdu_mirror is None or rt.pdu_mirror.closed)
    )
    if stale and rt.pdu is not None:
        try:
            await asyncio.wait_for(rt.pdu.close(), timeout=5.0)
        except Exception:  # noqa: BLE001
            pass
        rt.pdu = None
        rt.pdu_mirror = None
    if rt.pdu is None:
        mirror = rt.hub.open_mirror("电源管理", f"{cfg.power.host}:{cfg.power.port}")
        rt.pdu = PduClient(cfg.power, on_output=mirror.publish)
        rt.pdu_key = key
        rt.pdu_mirror = mirror
    if rt.pdu_mirror is None:
        rt.pdu_mirror = rt.hub.open_mirror("电源管理", f"{cfg.power.host}:{cfg.power.port}")
    return rt.pdu, rt.pdu_mirror


async def _device_command(
    rt: Runtime,
    session: Console,
    command: str,
    timeout: float,
    *,
    display: bool = True,
    log_command: str = "",
) -> dict[str, Any]:
    clamped = _clamp_timeout(timeout, _DEFAULT_TIMEOUT)
    if not display:
        await rt.hub.note(  # type: ignore[union-attr]
            session, f"\r\n[npa] $ {log_command or _mask_command(command)}\r\n"
        )
    result = await rt.hub.exec_on_console(  # type: ignore[union-attr]
        session, command, timeout=clamped, display=display
    )
    output, truncated = _limit(result["output"])
    closed = session.closed
    ok = bool(result["matched_prompt"]) and not result["timed_out"] and not closed
    payload: dict[str, Any] = {
        "ok": ok,
        "kind": session.kind,
        "command": _mask_command(command),
        "output": output,
        "truncated": truncated,
        "matched_prompt": result["matched_prompt"],
        "timed_out": result["timed_out"],
        "match": result["match"],
        "duration_sec": result["duration_sec"],
        "closed": closed,
        "session_id": session.session_id,
        "terminal_url": _terminal_url(session.session_id),
    }
    if closed:
        payload["error"] = "会话在命令结束前关闭"
        payload["hint"] = "重新调用 device_exec 或 session_open 建立会话。"
    elif result["timed_out"]:
        payload["error"] = f"等待设备提示符超时({clamped:g}s)"
        payload["hint"] = (
            "命令可能仍在执行;调大 timeout,或检查 shell_prompt 配置是否与设备提示符一致。"
        )
    elif not result["matched_prompt"]:
        payload["error"] = "没有识别到设备提示符"
        payload["hint"] = "检查配置页 device.*.shell_prompt;输出已原样返回。"
    return payload


async def _open_device_session(rt: Runtime, kind: str) -> Console:
    if rt.hub is None:
        raise ToolFailure("会话系统未初始化", "重启 npa-mcp 后重试。")
    cfg = rt.manager.load()
    device = cfg.device
    requested = (kind or "auto").strip().lower()
    if requested == "auto":
        prefer = device.prefer if device.prefer in ("ssh", "telnet", "serial") else "serial"
        order = [prefer] + [name for name in ("ssh", "telnet", "serial") if name != prefer]
    elif requested in ("ssh", "telnet", "serial"):
        order = [requested]
    else:
        raise ToolFailure(f"未知设备连接方式: {kind}", "kind 只能是 auto/ssh/telnet/serial。")

    failures: list[str] = []
    for name in order:
        try:
            return await rt.hub.open_device(name)
        except SessionError as exc:
            failures.append(f"{name}: {exc.message}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    detail = ";".join(failures) if failures else "没有可用的连接方式"
    raise ToolFailure(
        "无法连接开发板",
        f"已尝试 {' → '.join(order)}。{detail}。请检查配置页的启用开关、地址和凭据。",
    )


async def _power_action(action: str, outlet: int, settle_sec: float = 0.0) -> dict[str, Any]:
    rt = _runtime()
    cfg = rt.manager.load()
    power = cfg.power
    if not power.host:
        raise ToolFailure(
            "电源管理设备未配置",
            "打开配置页填写 power.host、账号、密码与插座命令模板后保存。",
        )
    if power.login_required and not power.password:
        raise ToolFailure(
            "电源管理设备密码未配置",
            "打开配置页填写 power.password(或关闭 login_required)。",
        )
    if outlet and (outlet < 1 or outlet > 256):
        raise ToolFailure(f"非法插座号: {outlet}", "outlet 通常是 1..8;传 0 使用配置中的默认插座。")
    pdu, mirror = await _get_pdu(rt, cfg)
    await rt.hub.note(mirror, f"\r\n[npa] {action} outlet={outlet or power.outlet}\r\n")  # type: ignore[union-attr]
    result = await pdu.run(action, outlet or None)
    payload = result.to_dict()
    payload.update(
        {
            "ok": bool(result.ok),
            "session_id": mirror.session_id,
            "terminal_url": _terminal_url(mirror.session_id),
        }
    )
    if action == "status":
        payload["state"] = _infer_power_state(result.output)
    if not result.determined:
        payload["ok"] = False
        payload["error"] = "无法判定电源设备执行结果"
        payload["hint"] = (
            "检查配置页 power.expect.ok / fail 关键字;设备原始输出已原样返回。"
        )
    elif not result.ok:
        payload["error"] = "电源管理设备返回失败"
        payload["hint"] = "核对 outlet 编号、账号权限与设备命令模板;查看 output。"
    else:
        settle = max(0.0, min(float(settle_sec or 0.0), 600.0))
        if action in ("on", "off") and settle > 0:
            payload["settle_sec"] = settle
            await asyncio.sleep(settle)
    return payload


# ---------------------------------------------------------------------------
# 工具:状态
# ---------------------------------------------------------------------------
@mcp.tool()
@_guard
async def list_targets() -> dict[str, Any]:
    """列出代码服务器、开发板、电源、文件服务、会话与定时任务的当前状态.

    凭据一律掩码显示(未配置/***),不会泄露密码或私钥内容。凭据只能由用户
    在 open_config_page 打开的本地网页里填写。开机或修改配置后先调用本工具,
    确认哪些目标已经可用。
    """
    rt = _runtime()
    cfg = rt.manager.load()
    cs = cfg.code_server
    cs_ready = bool(cs.host and cs.username and _ssh_ready(cs.host, cs.username, cs.auth))
    dev = cfg.device
    device_info: dict[str, Any] = {"prefer": dev.prefer}
    for name in ("ssh", "telnet", "serial"):
        item = getattr(dev, name)
        info: dict[str, Any] = {"enabled": bool(item.enabled)}
        if name == "ssh":
            info.update(
                {
                    "host": item.host,
                    "port": item.port,
                    "username": item.username,
                    "auth": auth_summary(item.auth),
                    "configured": bool(item.enabled and _ssh_ready(item.host, item.username, item.auth)),
                }
            )
        elif name == "telnet":
            info.update(
                {
                    "host": item.host,
                    "port": item.port,
                    "username": item.username,
                    "configured": bool(
                        item.enabled
                        and item.host
                        and (not item.login_required or item.password)
                    ),
                }
            )
        else:
            info.update(
                {
                    "port": item.port,
                    "baudrate": item.baudrate,
                    "configured": bool(item.enabled and item.port),
                }
            )
        device_info[name] = info
    power = cfg.power
    power_ready = bool(
        power.host and (not power.login_required or power.password)
    )
    windows: list[dict[str, Any]] = []
    now = datetime.now()
    active_windows: list[str] = []
    for index, window in enumerate(cfg.work_windows):
        end = window.active_end(now)
        if end is not None:
            active_windows.append(window.name)
        windows.append(
            {
                "name": window.name,
                "days": window.days,
                "start": window.start,
                "end": window.end,
                "active_now": end is not None,
                "active_end": end.isoformat(timespec="seconds") if end else "",
                "next_start": window.next_start(now).isoformat(timespec="seconds"),
                "task_name": task_name_for(window, index),
                "task_file": window.job.task_file,
                "project_dir": window.job.project_dir,
            }
        )

    sessions: list[dict[str, Any]] = []
    if rt.hub is not None:
        for item in rt.hub.list():
            entry = dict(item)
            entry["terminal_url"] = _terminal_url(entry["session_id"])
            sessions.append(entry)

    warnings: list[str] = []
    if not cs_ready:
        warnings.append("代码服务器未配置完整,ssh_exec / 文件操作不可用。")
    if not any(getattr(dev, name).enabled for name in ("ssh", "telnet", "serial")):
        warnings.append("开发板连接(ssh/telnet/serial)全部未启用。")
    if not power_ready:
        warnings.append("电源管理设备未配置,power_on/off/status 不可用。")
    if rt.web_port == 0:
        warnings.append(f"本地网页服务未启动: {rt.web_error or '未知原因'}。")
    if cfg.firmware_server.http.enabled and not cfg.firmware_server.http.token:
        warnings.append(
            "HTTP 镜像服务未设置 token;若 http 服务监听在局域网地址,请只在可信网络使用。"
        )
    warnings.append("凭据以明文保存在 config.json,请限制该文件的访问权限。")

    servers = rt.fileservers.status() if rt.fileservers is not None else {}
    return {
        "ok": True,
        "version": __version__,
        "config_path": str(rt.manager.path),
        "web": {
            "running": rt.web_server is not None,
            "url": f"{rt.web_url}/config" if rt.web_url else "",
            "terminals_url": _terminals_url(),
            "port": rt.web_port,
            "host": cfg.web.host,
            "error": rt.web_error,
        },
        "paths": cfg.paths.model_dump(),
        "lan_ip": detect_lan_ip(cfg.lan_peer()),
        "code_server": {
            "host": cs.host,
            "port": cs.port,
            "username": cs.username,
            "auth": auth_summary(cs.auth),
            "workdir": cs.workdir,
            "configured": cs_ready,
        },
        "device": device_info,
        "power": {
            "transport": power.transport,
            "host": power.host,
            "port": power.port,
            "outlet": power.outlet,
            "configured": power_ready,
        },
        "fileservers": servers,
        "work_windows": windows,
        "active_windows": active_windows,
        "sessions": sessions,
        "warnings": warnings,
    }


@mcp.tool()
@_guard
async def open_config_page() -> dict[str, Any]:
    """在默认浏览器打开本地配置页(凭据只能在这里填写).

    返回配置页与终端页地址。若自动打开失败,把 url 复制到浏览器即可。
    """
    rt = _runtime()
    if not rt.web_url or rt.web_server is None:
        raise ToolFailure(
            "本地网页服务未运行",
            f"启动 MCP 时不要加 --no-web;当前错误: {rt.web_error or '未知'}。",
        )
    url = f"{rt.web_url}/config"
    opened = await asyncio.to_thread(open_url, url)
    return {
        "ok": True,
        "url": url,
        "terminals_url": _terminals_url(),
        "opened": opened,
        "hint": "" if opened else f"浏览器未自动打开,请手动访问 {url}",
    }


# ---------------------------------------------------------------------------
# 工具:代码服务器
# ---------------------------------------------------------------------------
@mcp.tool()
@_guard
async def ssh_exec(command: str, timeout: float = 120.0, cwd: str = "") -> dict[str, Any]:
    """在远程代码服务器上执行一条 shell 命令并返回完整输出.

    默认在配置的 code_server.workdir 下执行;cwd 可覆盖。编译、git、make
    等操作都走这里。命令过程会同步显示在"代码服务器"网页终端中
    (返回 session_id / terminal_url),用户可实时观看。
    """
    if not (command or "").strip():
        raise ToolFailure("命令为空", "请提供要执行的 shell 命令。")
    rt = _runtime()
    cfg = rt.manager.load()
    target, cs = _require_code_server(cfg)
    workdir = (cwd or "").strip() or (cs.workdir or "").strip()
    session = rt.hub.open_mirror("代码服务器", f"{cs.username}@{cs.host}")  # type: ignore[union-attr]
    await rt.hub.note(  # type: ignore[union-attr]
        session,
        f"\r\n[npa] ssh {cs.username}@{cs.host}:{cs.port} {workdir or '~'}\r\n"
        f"[npa] $ {command}\r\n",
    )
    clamped = _clamp_timeout(timeout, _DEFAULT_TIMEOUT)
    try:
        result = await asyncio.to_thread(
            ssh_exec_impl,
            target,
            command,
            timeout=clamped,
            cwd=workdir or None,
        )
    except TransportError as exc:
        await rt.hub.note(session, f"[npa] 失败: {exc.message}\r\n")  # type: ignore[union-attr]
        raise
    stdout, out_truncated = _limit(result.stdout)
    stderr, err_truncated = _limit(result.stderr, 100_000)
    shown = stdout
    if stderr:
        shown = f"{stdout}\n{stderr}" if stdout else stderr
    if shown:
        if len(shown) > 20_000:
            shown = shown[:20_000] + "\r\n...[npa] 输出过长,终端中已截断\r\n"
        await rt.hub.note(session, shown.rstrip("\r\n") + "\r\n")  # type: ignore[union-attr]
    await rt.hub.note(  # type: ignore[union-attr]
        session, f"[npa] exit={result.exit_code} duration={result.duration_sec:.2f}s\r\n"
    )
    payload: dict[str, Any] = {
        "ok": result.exit_code == 0 and not result.timed_out,
        "command": command,
        "cwd": workdir,
        "exit_code": result.exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": out_truncated or err_truncated,
        "timed_out": result.timed_out,
        "duration_sec": round(result.duration_sec, 3),
        "session_id": session.session_id,
        "terminal_url": _terminal_url(session.session_id),
    }
    if result.timed_out:
        payload["error"] = f"命令在 {clamped:g}s 内未结束"
        payload["hint"] = "进程可能仍在远端运行;调大 timeout,或用 session_* 工具持续观察。"
    elif result.exit_code != 0:
        payload["error"] = f"命令退出码 {result.exit_code}"
        payload["hint"] = "查看 stderr;若是编译错误,请按输出修复后重试。"
    return payload


@mcp.tool()
@_guard
async def remote_read_file(remote_path: str, max_bytes: int = 2097152) -> dict[str, Any]:
    """读取代码服务器上的文本文件(SFTP),默认最多 2MB.

    返回 content、size、truncated、mtime。适合读取源码、Makefile、构建日志;
    路径支持 ~ 和相对 workdir 的相对路径。修改文件请用 remote_write_file。
    """
    if not (remote_path or "").strip():
        raise ToolFailure("远端路径为空", "请提供 remote_path。")
    rt = _runtime()
    cfg = rt.manager.load()
    target, cs = _require_code_server(cfg)
    resolved = await _resolve_remote(target, remote_path, cs.workdir or "~")
    limit = max(1024, min(int(max_bytes or 0), 8 * 1024 * 1024))
    session = rt.hub.open_mirror("代码服务器", f"{cs.username}@{cs.host}")  # type: ignore[union-attr]
    await rt.hub.note(session, f"\r\n[npa] 读取 {resolved}\r\n")  # type: ignore[union-attr]
    data = await asyncio.to_thread(sftp_read, target, resolved, limit)
    payload: dict[str, Any] = {"ok": True, **data, "resolved_path": resolved}
    payload["session_id"] = session.session_id
    payload["terminal_url"] = _terminal_url(session.session_id)
    if data.get("truncated"):
        payload["hint"] = f"文件超过 {limit} 字节,只返回前 {limit} 字节;可调大 max_bytes。"
    return payload


@mcp.tool()
@_guard
async def remote_write_file(
    remote_path: str, content: str, mode: str = "644"
) -> dict[str, Any]:
    """原子写入代码服务器上的文本文件(SFTP 临时文件 + rename,保留 .bak 备份).

    写入内容按 UTF-8 原样保存,不会把 LF 改成 CRLF。mode 为八进制权限,默认
    644。返回 bytes / sha256 / 备份路径。二进制文件请用 ssh_upload。
    """
    if not (remote_path or "").strip():
        raise ToolFailure("远端路径为空", "请提供 remote_path。")
    if len((content or "").encode("utf-8")) > 8 * 1024 * 1024:
        raise ToolFailure("文件内容超过 8MB", "二进制或超大文件请先落地再 ssh_upload。")
    mode_value = _parse_mode(mode)
    rt = _runtime()
    cfg = rt.manager.load()
    target, cs = _require_code_server(cfg)
    resolved = await _resolve_remote(target, remote_path, cs.workdir or "~")
    session = rt.hub.open_mirror("代码服务器", f"{cs.username}@{cs.host}")  # type: ignore[union-attr]
    await rt.hub.note(session, f"\r\n[npa] 写入 {resolved} ({len(content.encode('utf-8'))}B)\r\n")  # type: ignore[union-attr]
    result = await asyncio.to_thread(sftp_write, target, resolved, content, mode_value)
    payload: dict[str, Any] = {
        "ok": True,
        **result,
        "resolved_path": resolved,
        "mode": oct(mode_value),
        "session_id": session.session_id,
        "terminal_url": _terminal_url(session.session_id),
    }
    return payload


@mcp.tool()
@_guard
async def remote_list_dir(remote_dir: str = "") -> dict[str, Any]:
    """列出代码服务器上的目录内容(SFTP),默认列出 workdir.

    条目按目录优先、名称排序;超大目录最多返回 500 条并标记 truncated。
    """
    rt = _runtime()
    cfg = rt.manager.load()
    target, cs = _require_code_server(cfg)
    resolved = await _resolve_remote(
        target, remote_dir or cs.workdir or "~", cs.workdir or "~"
    )
    entries = await asyncio.to_thread(sftp_list, target, resolved)
    truncated = len(entries) > 500
    if truncated:
        entries = entries[:500]
    session = rt.hub.open_mirror("代码服务器", f"{cs.username}@{cs.host}")  # type: ignore[union-attr]
    return {
        "ok": True,
        "remote_dir": resolved,
        "count": len(entries),
        "entries": entries,
        "truncated": truncated,
        "session_id": session.session_id,
        "terminal_url": _terminal_url(session.session_id),
    }


@mcp.tool()
@_guard
async def ssh_upload(local_path: str, remote_path: str = "") -> dict[str, Any]:
    """把 Windows 本地文件上传到代码服务器(SFTP).

    remote_path 为空时上传到 workdir 下同名文件;以 / 结尾表示目录。返回
    size 与本地 MD5,便于与远端校验。
    """
    if not (local_path or "").strip():
        raise ToolFailure("本地路径为空", "请提供 local_path。")
    local = Path(local_path).expanduser()
    if not local.is_absolute():
        local = (Path.cwd() / local).resolve()
    if not local.is_file():
        raise ToolFailure(f"本地文件不存在: {local}", "检查路径;相对路径基于 MCP 进程工作目录。")
    rt = _runtime()
    cfg = rt.manager.load()
    target, cs = _require_code_server(cfg)
    raw_remote = (remote_path or "").strip()
    if raw_remote.endswith(("/", "\\")):
        raw_remote = posixpath.join(raw_remote.replace("\\", "/").rstrip("/"), local.name)
    if not raw_remote:
        raw_remote = posixpath.join(cs.workdir.strip() or "~", local.name)
    resolved = await _resolve_remote(target, raw_remote, cs.workdir or "~")
    session = rt.hub.open_mirror("代码服务器", f"{cs.username}@{cs.host}")  # type: ignore[union-attr]
    await rt.hub.note(session, f"\r\n[npa] 上传 {local} -> {resolved}\r\n")  # type: ignore[union-attr]
    result = await asyncio.to_thread(sftp_upload, target, local, resolved)
    return {
        "ok": True,
        **result,
        "resolved_path": resolved,
        "session_id": session.session_id,
        "terminal_url": _terminal_url(session.session_id),
    }


@mcp.tool()
@_guard
async def ssh_download(remote_path: str, local_path: str = "") -> dict[str, Any]:
    """把代码服务器上的文件下载到 Windows(默认存到镜像目录),返回 MD5.

    local_path 为空时存为 paths.image_dir 下的同名文件;相对路径只能落在
    镜像目录内,绝对路径可写到其他位置。下载先写 .part 再原子替换。
    """
    if not (remote_path or "").strip():
        raise ToolFailure("远端路径为空", "请提供 remote_path。")
    rt = _runtime()
    cfg = rt.manager.load()
    target, cs = _require_code_server(cfg)
    resolved = await _resolve_remote(target, remote_path, cs.workdir or "~")
    name = posixpath.basename(resolved.rstrip("/")) or "download.bin"
    image_dir = cfg.paths.image_path()
    raw_local = (local_path or "").strip()
    if not raw_local:
        local = image_dir / name
    else:
        candidate = Path(raw_local).expanduser()
        if candidate.is_absolute():
            local = candidate
        else:
            joined = safe_join(image_dir, raw_local)
            if joined is None:
                raise ToolFailure(
                    "本地路径超出镜像目录",
                    "相对路径必须位于 paths.image_dir 内;要写到其他位置请用绝对路径。",
                )
            local = joined
        if raw_local.endswith(("/", "\\")) or local.is_dir():
            local = local / name
    session = rt.hub.open_mirror("代码服务器", f"{cs.username}@{cs.host}")  # type: ignore[union-attr]
    await rt.hub.note(session, f"\r\n[npa] 下载 {resolved} -> {local}\r\n")  # type: ignore[union-attr]
    result = await asyncio.to_thread(sftp_download, target, resolved, local)
    return {
        "ok": True,
        **result,
        "resolved_path": resolved,
        "session_id": session.session_id,
        "terminal_url": _terminal_url(session.session_id),
    }


# ---------------------------------------------------------------------------
# 工具:开发板
# ---------------------------------------------------------------------------
@mcp.tool()
@_guard
async def device_exec(command: str, timeout: float = 120.0, kind: str = "auto") -> dict[str, Any]:
    """在开发板上执行命令并通过 shell 提示符判断结束.

    kind=auto 时按配置的 device.prefer 优先尝试 ssh/telnet/serial,失败自动
    换下一种。串口与 telnet 是常驻会话,用户可在网页终端看到完整过程。
    启动阶段(uboot/内核日志)请用 session_open + session_read 观察。
    """
    if not (command or "").strip():
        raise ToolFailure("命令为空", "请提供要在开发板上执行的命令。")
    rt = _runtime()
    session = await _open_device_session(rt, kind)
    return await _device_command(rt, session, command, timeout)


@mcp.tool()
@_guard
async def device_pull_firmware(
    filename: str = "",
    remote_path: str = "",
    method: str = "auto",
    token: str = "",
    verify: bool = True,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """让开发板主动从本机文件服务拉取固件(核心更新流程),默认做 MD5 校验.

    - filename 为空时自动选 paths.image_dir 中最新固件;只允许该目录内文件。
    - method=auto 依次选正在运行的 tftp / http(wget) / ftp(ftpget);
      scp 需要显式指定(从代码服务器拉,而非本机)。
    - remote_path 为空时为 firmware.device_pull_dir/文件名。
    - 返回 pull(命令输出)与 verify(校验输出)、local_md5、verified。
    下载命令模板在配置页 firmware.pull_templates 中可改。
    """
    rt = _runtime()
    cfg = rt.manager.load()
    method = (method or "auto").strip().lower()
    if method not in ("auto", "tftp", "wget", "ftpget", "scp"):
        raise ToolFailure(
            f"未知拉取方式: {method}", "method 只能是 auto/tftp/wget/ftpget/scp。"
        )
    local, relative = _newest_firmware(cfg, filename)
    base_name = Path(relative).name
    remote = _remote_target(cfg, remote_path, base_name)

    servers: dict[str, dict[str, Any]] = {}
    if rt.fileservers is not None:
        for item in rt.fileservers.status()["servers"]:
            servers[str(item.get("name"))] = item
    if method == "auto":
        method = ""
        for candidate, service in (("tftp", "tftp"), ("wget", "http"), ("ftpget", "ftp")):
            if servers.get(service, {}).get("running"):
                method = candidate
                break
        if not method:
            raise ToolFailure(
                "没有可用的文件服务",
                "检查配置页 firmware_server 的 tftp/ftp/http 启用状态与端口占用;"
                "也可用 method=scp(需开发板能从代码服务器取文件)。",
            )
    else:
        service = {"tftp": "tftp", "wget": "http", "ftpget": "ftp"}.get(method)
        if service and not servers.get(service, {}).get("running"):
            status = servers.get(service, {})
            raise ToolFailure(
                f"{service.upper()} 文件服务未运行",
                f"原因: {status.get('error') or '未启用'};检查配置页或端口占用。",
            )

    hints = _pull_hints(cfg, rt, relative, remote, token)
    command = hints.get(method, "")
    if not command or command.startswith("<"):
        raise ToolFailure(
            f"拉取命令模板不可用: {method}",
            "检查配置页 firmware.pull_templates。",
        )
    session = await _open_device_session(rt, "auto")
    pull_payload = await _device_command(
        rt,
        session,
        command,
        timeout,
        display=method != "ftpget",
        log_command=_mask_command(command),
    )
    local_md5 = await asyncio.to_thread(md5_file, local)

    payload: dict[str, Any] = {
        "ok": bool(pull_payload.get("ok")),
        "method": method,
        "filename": relative,
        "local_path": str(local),
        "local_md5": local_md5,
        "remote_path": remote,
        "pull_command": _mask_command(command),
        "pull": pull_payload,
        "verified": None,
        "session_id": pull_payload.get("session_id", session.session_id),
        "terminal_url": pull_payload.get("terminal_url", _terminal_url(session.session_id)),
    }
    if not pull_payload.get("ok"):
        payload["error"] = pull_payload.get("error") or "固件拉取失败"
        payload["hint"] = pull_payload.get("hint") or (
            "检查开发板网络、本机防火墙(69/UDP、2121/TCP、网页端口)与文件服务状态。"
        )
        return payload

    if not verify:
        return payload

    template = (cfg.firmware.verify.cmd or "").strip()
    if not template:
        payload["hint"] = "配置页 firmware.verify.cmd 为空,已跳过校验。"
        return payload
    try:
        verify_cmd = template.format(remote_path=remote)
    except (KeyError, IndexError, ValueError) as exc:
        payload["hint"] = f"校验命令模板无法格式化,已跳过校验: {exc}"
        return payload
    verify_payload = await _device_command(
        rt, session, verify_cmd, min(_clamp_timeout(timeout, 300.0), 120.0)
    )
    expect = (cfg.firmware.verify.expect_ok or "").strip()
    if expect:
        verified = expect in (verify_payload.get("output") or "")
        remote_md5 = ""
    else:
        remote_md5 = _extract_md5(verify_payload.get("output") or "")
        verified = bool(remote_md5) and remote_md5 == local_md5
    payload.update(
        {
            "verify_command": verify_cmd,
            "verify": verify_payload,
            "verified": verified,
            "remote_md5": remote_md5,
            "ok": bool(verified),
        }
    )
    if not verified:
        payload["error"] = "固件校验失败"
        payload["hint"] = (
            "设备端文件可能不完整或下载方式有问题;检查 MD5、重试或改用其他 method。"
            "TFTP 偶发超时,建议保留 verify=true。"
        )
        return payload
    payload.pop("error", None)
    return payload


@mcp.tool()
@_guard
async def device_push_firmware(
    filename: str = "", remote_path: str = "", verify: bool = True
) -> dict[str, Any]:
    """开发板有 SSH 服务时,直接通过 SFTP 把固件上传到开发板.

    与 device_pull_firmware(设备主动拉)互补,适合设备网络无法访问本机但
    SSH 可达的情况。filename 为空时选镜像目录中最新的固件。verify=true 时
    在设备上执行 md5sum 与本地 MD5 比对。
    """
    rt = _runtime()
    cfg = rt.manager.load()
    dev = cfg.device.ssh
    if not dev.enabled:
        raise ToolFailure("设备 SSH 未启用", "在配置页启用 device.ssh 并填写地址与凭据。")
    if not _key_file_exists(dev.auth):
        raise ToolFailure(
            "设备 SSH 私钥文件不存在",
            f"检查配置页 device.ssh.auth.key_file,当前: {dev.auth.key_file or '(空)'}",
        )
    target = SshTarget.from_device(dev)
    if not target.ready():
        raise ToolFailure("设备 SSH 凭据不完整", "在配置页填写设备 SSH 密码或私钥。")
    local, relative = _newest_firmware(cfg, filename)
    base_name = Path(relative).name
    resolved = await _resolve_remote(target, remote_path or base_name, "~")
    session = rt.hub.open_mirror("设备 SSH", f"{dev.username}@{dev.host}")  # type: ignore[union-attr]
    await rt.hub.note(session, f"\r\n[npa] SFTP 上传 {local} -> {resolved}\r\n")  # type: ignore[union-attr]
    upload = await asyncio.to_thread(sftp_upload, target, local, resolved)
    payload: dict[str, Any] = {
        "ok": True,
        "filename": relative,
        "local_path": str(local),
        "remote_path": resolved,
        "size": upload.get("size", 0),
        "local_md5": upload.get("local_md5", ""),
        "verified": None,
        "session_id": session.session_id,
        "terminal_url": _terminal_url(session.session_id),
    }
    if not verify:
        return payload
    remote_md5, result = await _remote_md5(target, resolved)
    verified = bool(remote_md5) and remote_md5 == payload["local_md5"]
    payload.update(
        {
            "verified": verified,
            "remote_md5": remote_md5,
            "verify_command": getattr(result, "command", ""),
            "verify_output": _limit(getattr(result, "stdout", ""))[0],
            "ok": verified,
        }
    )
    if not verified:
        payload["error"] = "设备端 MD5 校验失败"
        payload["hint"] = (
            "确认设备支持 md5sum/md5;若命令不存在,可传 verify=false 后自行校验。"
        )
    return payload


# ---------------------------------------------------------------------------
# 工具:交互会话
# ---------------------------------------------------------------------------
@mcp.tool()
@_guard
async def session_open(kind: str = "auto", label: str = "") -> dict[str, Any]:
    """打开(或复用)一个开发板远程会话,并弹出独立的控制台窗口.

    kind=auto 用配置的 device.prefer;也可指定 ssh/telnet/serial。
    telnet/串口同一时间只有一个常驻会话,重复调用会复用。会话创建时会
    自动开一个类似 cmd 的原生终端窗口;若窗口已被关掉,再次调用本工具
    会重新打开。terminal_url 是浏览器备用视图。
    """
    rt = _runtime()
    cfg = rt.manager.load()
    requested = (kind or "auto").strip().lower()
    if requested == "auto":
        requested = cfg.device.prefer if cfg.device.prefer in ("ssh", "telnet", "serial") else "serial"
    if requested not in ("ssh", "telnet", "serial"):
        raise ToolFailure(f"未知会话类型: {kind}", "kind 只能是 auto/ssh/telnet/serial。")
    session = await rt.hub.open_device(requested, label.strip())  # type: ignore[union-attr]
    return {
        "ok": True,
        **_session_payload(session),
        "terminals_url": _terminals_url(),
        "window_opened": rt.ensure_terminal_window(session),
    }


@mcp.tool()
@_guard
async def session_send(session_id: str, data: str, newline: bool = True) -> dict[str, Any]:
    """向会话写入原始字符(命令或按键),newline=true 时自动补行尾.

    适合交互式程序、uboot 菜单、需要逐字符输入的场景。普通命令优先用
    device_exec(会自动等提示符并返回输出)。
    """
    if not session_id:
        raise ToolFailure("session_id 为空", "先调用 session_open 或 session_list。")
    rt = _runtime()
    session = rt.hub.get(session_id)  # type: ignore[union-attr]
    payload = data or ""
    if len(payload.encode("utf-8")) > 64 * 1024:
        raise ToolFailure("单次输入超过 64KB", "请分多次发送。")
    closing = payload.endswith(("\n", "\r"))
    if newline and not closing:
        payload += session.eol
    echo_expect = None
    if newline:
        text = payload.rstrip("\r\n")
        if text and all(ord(char) >= 32 or char == "\t" for char in text):
            echo_expect = text
    await rt.hub.send(session, payload, echo_expect=echo_expect)  # type: ignore[union-attr]
    return {
        "ok": True,
        "session_id": session_id,
        "sent_bytes": len(payload.encode("utf-8")),
        "newline": newline,
        "closed": session.closed,
        "terminal_url": _terminal_url(session_id),
    }


@mcp.tool()
@_guard
async def session_read(
    session_id: str,
    since_seq: int = 0,
    lines: int = 200,
    wait_pattern: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """读取会话输出;可用正则等待某段输出出现(等编译/烧录结果).

    - wait_pattern 非空:从 since_seq(为 0 则从现在)开始等待,匹配成功时
      matched=true;超时返回已收到的全部文本(timed_out=true)。
    - 否则 since_seq>0 返回增量,=0 返回最近 lines 行。
    返回 next_seq,下次可取增量,避免重复读取。
    """
    if not session_id:
        raise ToolFailure("session_id 为空", "先调用 session_open 或 session_list。")
    rt = _runtime()
    session = rt.hub.get(session_id)  # type: ignore[union-attr]
    line_count = max(1, min(int(lines or 200), 5000))
    clamped = _clamp_timeout(timeout, 30.0, 600.0)
    start = int(since_seq or 0)

    if (wait_pattern or "").strip():
        pattern = compile_pattern(wait_pattern.strip())
        if start <= 0:
            start = session.ring.next_seq
        waited = await rt.hub.wait_for(  # type: ignore[union-attr]
            session, pattern, clamped, start, require_tail=False
        )
        text, truncated = _limit(waited.get("text", ""))
        closed = bool(waited.get("closed")) or session.closed
        payload: dict[str, Any] = {
            "ok": bool(waited.get("matched")),
            "session_id": session_id,
            "matched": bool(waited.get("matched")),
            "timed_out": bool(waited.get("timed_out")),
            "match": waited.get("match", ""),
            "text": text,
            "truncated": truncated,
            "start_seq": start,
            "next_seq": session.ring.next_seq,
            "closed": closed,
            "duration_sec": round(float(waited.get("duration_sec", 0.0)), 3),
            "terminal_url": _terminal_url(session_id),
        }
        if not payload["ok"]:
            if payload["timed_out"]:
                payload["error"] = f"{clamped:g}s 内未匹配到 {wait_pattern!r}"
                payload["hint"] = (
                    "调大 timeout,或放宽正则;已返回这段时间收到的全部输出。"
                )
            elif closed:
                payload["error"] = "会话已关闭"
                payload["hint"] = "重新打开会话后再试。"
        return payload

    if start > 0:
        data = rt.hub.since(session_id, start)  # type: ignore[union-attr]
        text = data["text"]
        next_seq = data["next_seq"]
        closed = bool(data.get("closed"))
        used_start = int(data.get("start_seq", start))
    else:
        text = session.ring.tail(line_count)
        next_seq = session.ring.next_seq
        closed = session.closed
        used_start = session.ring.start_seq
    text, truncated = _limit(text)
    return {
        "ok": True,
        "session_id": session_id,
        "text": text,
        "truncated": truncated,
        "start_seq": used_start,
        "next_seq": next_seq,
        "closed": closed,
        "terminal_url": _terminal_url(session_id),
    }


@mcp.tool()
@_guard
async def session_list() -> dict[str, Any]:
    """列出所有会话(含隐藏/镜像/已关闭),给出对应网页终端地址."""
    rt = _runtime()
    sessions: list[dict[str, Any]] = []
    if rt.hub is not None:
        for item in rt.hub.list(include_closed=True):
            entry = dict(item)
            entry["terminal_url"] = _terminal_url(entry["session_id"])
            sessions.append(entry)
    return {
        "ok": True,
        "sessions": sessions,
        "terminals_url": _terminals_url(),
    }


@mcp.tool()
@_guard
async def session_close(session_id: str, force: bool = False) -> dict[str, Any]:
    """关闭会话视图(force=false)或彻底断开底层连接(force=true).

    串口/telnet 会话默认只隐藏网页视图,底层仍保持连接,便于下次直接复用;
    真正释放串口或断开设备请传 force=true。
    """
    if not session_id:
        raise ToolFailure("session_id 为空", "先调用 session_list 获取会话 id。")
    rt = _runtime()
    result = await rt.hub.close_session(session_id, force=force)  # type: ignore[union-attr]
    result["ok"] = True
    if not force:
        result["terminal_url"] = _terminal_url(session_id)
    return result


# ---------------------------------------------------------------------------
# 工具:电源
# ---------------------------------------------------------------------------
@mcp.tool()
@_guard
async def power_on(outlet: int = 0, settle_sec: float = 0.0) -> dict[str, Any]:
    """给开发板通电(telnet/ssh 控制电源管理设备).

    outlet=0 用配置里的默认插座号。settle_sec>0 时命令成功后等待若干秒再
    返回,给设备启动留时间。过程显示在"电源管理"网页终端中。
    """
    return await _power_action("on", int(outlet or 0), settle_sec)


@mcp.tool()
@_guard
async def power_off(outlet: int = 0, settle_sec: float = 0.0) -> dict[str, Any]:
    """给开发板断电(强制断电前请确认设备当前状态).

    outlet=0 用配置里的默认插座号。返回 ok / determined / 原始 output;
    无法识别设备回包时不会假装成功。
    """
    return await _power_action("off", int(outlet or 0), settle_sec)


@mcp.tool()
@_guard
async def power_status(outlet: int = 0) -> dict[str, Any]:
    """查询开发板电源状态,返回 state=on/off/unknown 与原始输出."""
    return await _power_action("status", int(outlet or 0))


# ---------------------------------------------------------------------------
# 工具:定时任务
# ---------------------------------------------------------------------------
@mcp.tool()
@_guard
async def schedule_register(window: str = "") -> dict[str, Any]:
    """把配置中的工作时间段注册为 Windows 任务计划(窗口起点启动无头 Claude).

    window 为空注册全部时间段;指定名称只注册该窗口。任务名 npa-mcp-<slug>,
    注册后可在 Windows 任务计划程序中查看。窗口外补跑会被 sched_run 自行跳过。
    """
    rt = _runtime()
    cfg = rt.manager.load()
    cfg.ensure_dirs()
    name = (window or "").strip()
    if not name:
        result = await asyncio.to_thread(register_all, rt.manager)
        return result
    found: tuple[int, Any] | None = None
    for index, item in enumerate(cfg.work_windows):
        if item.name == name:
            found = (index, item)
            break
    if found is None:
        names = ", ".join(item.name for item in cfg.work_windows) or "(无)"
        raise ToolFailure(
            f"找不到工作时段: {name}",
            f"可用时间段: {names}。先在配置页 work_windows 中添加。",
        )
    index, item = found
    result = await asyncio.to_thread(register_window, rt.manager, item, index)
    payload = {
        "ok": bool(result.get("ok")),
        "tasks": [result],
        "stale_tasks": [],
        "hint": "" if result.get("ok") else result.get("error", "注册任务计划失败"),
    }
    return payload


@mcp.tool()
@_guard
async def schedule_status() -> dict[str, Any]:
    """查看任务计划的注册状态、下次/上次运行时间与最近一次无头运行结果."""
    rt = _runtime()
    report = await asyncio.to_thread(status_report, rt.manager)
    cfg = rt.manager.load()
    report["last_run"] = read_last_run(cfg)
    report["last_session"] = read_last_session(cfg)
    report["now"] = datetime.now().isoformat(timespec="seconds")
    report["ok"] = True
    return report


@mcp.tool()
@_guard
async def schedule_remove(task_name: str = "", window: str = "") -> dict[str, Any]:
    """删除 npa-mcp 注册的任务计划;可用任务名或工作时间段名称指定.

    只允许删除 npa-mcp- 前缀的任务,不会误删系统任务。
    """
    rt = _runtime()
    cfg = rt.manager.load()
    name = (task_name or "").strip()
    if not name:
        target_window = (window or "").strip()
        if not target_window:
            raise ToolFailure("需要指定任务", "传 task_name(如 npa-mcp-nightly)或 window 名称。")
        found = None
        for index, item in enumerate(cfg.work_windows):
            if item.name == target_window:
                found = task_name_for(item, index)
                break
        if not found:
            raise ToolFailure(
                f"找不到工作时段: {target_window}",
                "先调用 schedule_status 查看已注册任务名。",
            )
        name = found
    result = await asyncio.to_thread(unregister_task, name)
    result["ok"] = bool(result.get("ok"))
    if not result["ok"]:
        result["hint"] = "任务可能不存在,或当前用户没有删除该任务的权限。"
    return result


@mcp.tool()
@_guard
async def work_window_check() -> dict[str, Any]:
    """检查当前是否处于允许自主工作的时间段(AI 开工前先自查).

    返回每个窗口的 active_now / active_end / remaining_sec / next_start。
    不在任何窗口内时 active=false,自主任务应立即停止,避免白天消耗 token。
    """
    rt = _runtime()
    cfg = rt.manager.load()
    now = datetime.now()
    items: list[dict[str, Any]] = []
    active: list[str] = []
    for index, window in enumerate(cfg.work_windows):
        end = window.active_end(now)
        if end is not None:
            active.append(window.name)
        items.append(
            {
                "name": window.name,
                "days": window.days,
                "start": window.start,
                "end": window.end,
                "active_now": end is not None,
                "active_end": end.isoformat(timespec="seconds") if end else "",
                "remaining_sec": int((end - now).total_seconds()) if end else 0,
                "next_start": window.next_start(now).isoformat(timespec="seconds"),
                "task_name": task_name_for(window, index),
                "task_file": window.job.task_file,
                "project_dir": window.job.project_dir,
            }
        )
    return {
        "ok": True,
        "now": now.isoformat(timespec="seconds"),
        "active": bool(active),
        "active_windows": active,
        "windows": items,
        "hint": "" if active else "当前不在任何工作时段;自主任务应停止或等待下一个窗口。",
    }


# ---------------------------------------------------------------------------
# 工具:报告
# ---------------------------------------------------------------------------
@mcp.tool()
@_guard
async def generate_report(
    title: str,
    summary: str = "",
    changes: list[Any] | None = None,
    issues: list[Any] | None = None,
    next_steps: list[Any] | None = None,
    session_ids: list[str] | None = None,
    status: str = "success",
    project: str = "",
    open_in_browser: bool = False,
) -> dict[str, Any]:
    """生成中文 HTML 工作总结报告(独立单文件,零外链,可直接归档/分享).

    changes 每项可为字符串,或 {"category","description","file"}。
    issues 每项可为字符串,或 {"severity","description","status"}。
    session_ids 中的会话日志会附在报告里,便于回溯 AI 做过什么。
    返回 path 与 url(网页可打开);open_in_browser=true 时自动打开。
    每次完成一个功能后都应调用本工具生成报告。
    """
    rt = _runtime()
    result = await asyncio.to_thread(
        _generate_report,
        rt.manager,
        title=title,
        summary=summary,
        changes=changes,
        issues=issues,
        next_steps=next_steps,
        session_ids=session_ids,
        status=status,
        project=project,
        hub=rt.hub,
        web_port=rt.web_port,
    )
    if result.get("url"):
        result["reports_url"] = f"{rt.web_url}/reports"
        if open_in_browser:
            result["opened"] = await asyncio.to_thread(open_url, result["url"])
    result["terminal_url"] = ""
    return result


# ---------------------------------------------------------------------------
# 进程入口
# ---------------------------------------------------------------------------
async def _run_mcp_stdio() -> None:
    runner = getattr(mcp, "run_stdio_async", None)
    if callable(runner):
        try:
            params = inspect.signature(runner).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs: dict[str, Any] = {"show_banner": False} if "show_banner" in params else {}
        await runner(**kwargs)
        return
    fallback = getattr(mcp, "run_async", None)
    if not callable(fallback):
        raise RuntimeError("当前 FastMCP 版本缺少 run_stdio_async / run_async")
    try:
        params = inspect.signature(fallback).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs = {"transport": "stdio"}
    if "show_banner" in params:
        kwargs["show_banner"] = False
    await fallback(**kwargs)


def _claim_browser_open(rt: Runtime, interval: float) -> bool:
    """跨实例节流:interval 秒内只允许一个 npa-mcp 进程打开浏览器.

    多个 Claude 会话(插件 + 终端)会各起一个 MCP 进程,若不加节流,
    启动瞬间会连开多个配置页。标记文件放在 npa_home 下。
    """
    if interval <= 0:
        return True
    stamp = rt.manager.path.parent / "web_opened.stamp"
    now = time.time()
    try:
        fd = os.open(stamp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            last = float(stamp.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        if now - last < interval:
            return False
        try:
            atomic_write_text(stamp, f"{now:.3f}\n")
        except OSError:
            return False
        return True
    except OSError:
        return True  # 无法写标记时不影响主功能
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{now:.3f}\n")
    except OSError:
        pass
    return True


async def _auto_open_config_page(rt: Runtime, cfg: Config) -> None:
    """按配置在默认浏览器打开配置页."""
    if not cfg.web.auto_open or not rt.web_url:
        return
    for name in ("NPA_MCP_WINDOW", "NPA_MCP_NO_BROWSER"):
        if os.environ.get(name):
            log.info("检测到 %s,跳过自动打开配置页", name)
            return
    interval = float(cfg.web.auto_open_interval_sec or 0)
    if not await asyncio.to_thread(_claim_browser_open, rt, interval):
        log.info("另一个实例刚刚打开过配置页,本次跳过")
        return
    url = f"{rt.web_url}/config"
    if await asyncio.to_thread(open_url, url):
        log.info("已在浏览器打开配置页: %s", url)
    else:
        log.warning("自动打开配置页失败,请手动访问 %s", url)


async def _serve(args: argparse.Namespace) -> None:
    global RUNTIME
    rt = Runtime(args.config or None)
    RUNTIME = rt
    cfg = rt.manager.load(force=True)
    setup_logging(cfg.paths.log_path())
    log.info("npa-mcp %s 启动,配置: %s", __version__, rt.manager.path)
    open_task: asyncio.Task[None] | None = None
    try:
        await rt.start(no_web=args.no_web)
        if rt.web_url:
            log.info("配置页: %s/config", rt.web_url)
            log.info("终端页: %s/terminals", rt.web_url)
            # 后台打开浏览器,不阻塞 stdio 握手(避免 MCP 客户端显示 connecting)
            open_task = asyncio.create_task(
                _auto_open_config_page(rt, cfg), name="npa-auto-open"
            )
        else:
            log.warning("网页服务未启动: %s", rt.web_error)
        try:
            await _run_mcp_stdio()
        finally:
            keep = rt.keep_alive_sec() if rt.web_server is not None else 0.0
            if keep > 0:
                log.info("stdio 已断开,网页继续运行 %.0f 秒", keep)
                try:
                    await asyncio.sleep(keep)
                except asyncio.CancelledError:
                    pass
    finally:
        if open_task is not None and not open_task.done():
            open_task.cancel()
        await rt.shutdown()
        RUNTIME = None
        log.info("npa-mcp 已退出")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="npa-mcp",
        description="嵌入式开发 MCP 服务器(Claude Code / FastMCP)",
    )
    parser.add_argument("--config", default="", help="配置文件路径,默认 ~/.npa-mcp/config.json")
    parser.add_argument("--no-web", action="store_true", help="不启动本地配置/终端网页")
    parser.add_argument("--version", action="store_true", help="显示版本后退出")
    args = parser.parse_args(argv)
    if args.version:
        print(f"npa-mcp {__version__}")
        return 0
    setup_logging(npa_home() / "logs")
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        log.info("收到中断信号,退出")
        return 130
    except Exception:  # noqa: BLE001
        log.exception("npa-mcp 异常退出")
        return 1
    return 0


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()

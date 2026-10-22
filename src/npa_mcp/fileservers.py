"""TFTP / FTP 文件服务.

两者都只服务 ``paths.image_dir``。端口被占用时降级为"未启动"并记录原因,
不会阻塞 MCP 主流程;HTTP 分发由 webapp 提供。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Config, FirmwareServerConfig
from .util import get_logger

log = get_logger(__name__)


@dataclass
class ServerStatus:
    name: str
    enabled: bool
    running: bool = False
    bind: str = ""
    port: int = 0
    root: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "running": self.running,
            "bind": self.bind,
            "port": self.port,
            "root": self.root,
            "error": self.error,
        }


class TftpService:
    def __init__(self, cfg: FirmwareServerConfig, root: Path):
        self.cfg = cfg
        self.root = root
        self.status = ServerStatus(
            "tftp",
            enabled=cfg.tftp.enabled,
            bind=cfg.tftp.bind,
            port=cfg.tftp.port,
            root=str(root),
        )
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> ServerStatus:
        if not self.status.enabled:
            self.status.error = "配置中未启用"
            return self.status
        if self.status.running:
            return self.status
        try:
            import tftpy

            self._server = tftpy.TftpServer(str(self.root))

            def _run() -> None:
                assert self._server is not None
                try:
                    self._server.listen(
                        self.cfg.tftp.bind or "0.0.0.0",
                        self.cfg.tftp.port,
                        timeout=2,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.status.running = False
                    self.status.error = f"TFTP 服务异常退出: {exc}"
                    log.warning(self.status.error)

            self._thread = threading.Thread(target=_run, name="tftp-server", daemon=True)
            self._thread.start()
            # tftpy.listen 没有就绪回调,短暂等待后以线程存活为准
            self._thread.join(timeout=0.4)
            if not self._thread.is_alive():
                self.status.error = self.status.error or "TFTP 未能启动(端口 69/UDP 可能被占用)"
                return self.status
            self.status.running = True
            self.status.error = ""
            log.info("TFTP 服务已启动: %s:%s -> %s", self.cfg.tftp.bind, self.cfg.tftp.port, self.root)
        except Exception as exc:  # noqa: BLE001
            self.status.running = False
            self.status.error = f"TFTP 启动失败: {exc}"
            log.warning(self.status.error)
        return self.status

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.stop(now=True)
            except Exception:
                pass
        self._server = None
        self.status.running = False


class FtpService:
    def __init__(self, cfg: FirmwareServerConfig, root: Path, lan_ip: str):
        self.cfg = cfg
        self.root = root
        self.lan_ip = lan_ip
        self.status = ServerStatus(
            "ftp",
            enabled=cfg.ftp.enabled,
            bind=cfg.ftp.bind,
            port=cfg.ftp.port,
            root=str(root),
        )
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> ServerStatus:
        if not self.status.enabled:
            self.status.error = "配置中未启用"
            return self.status
        if self.status.running:
            return self.status
        try:
            from pyftpdlib.authorizers import DummyAuthorizer
            from pyftpdlib.handlers import FTPHandler
            from pyftpdlib.servers import FTPServer

            authorizer = DummyAuthorizer()
            authorizer.add_user(
                self.cfg.ftp.username or "npa",
                self.cfg.ftp.password or "npa-pass",
                str(self.root),
                perm="elradfmwMT",
            )
            handler = FTPHandler
            handler.authorizer = authorizer
            handler.banner = "npa-mcp firmware ftp ready"
            handler.masquerade_address = self.lan_ip
            try:
                start, _, end = self.cfg.ftp.passive_ports.partition("-")
                low = int(start)
                high = int(end) if end else low
                handler.passive_ports = range(low, high + 1)
            except (ValueError, TypeError):
                log.warning("FTP passive_ports 配置非法,使用 30000-30100")
                handler.passive_ports = range(30000, 30101)
            self._server = FTPServer((self.cfg.ftp.bind or "0.0.0.0", self.cfg.ftp.port), handler)
            self._server.max_cons = 50
            self._server.max_cons_per_ip = 10

            def _run() -> None:
                assert self._server is not None
                try:
                    self._server.serve_forever(timeout=0.5)
                except Exception as exc:  # noqa: BLE001
                    self.status.running = False
                    self.status.error = f"FTP 服务异常退出: {exc}"
                    log.warning(self.status.error)

            self._thread = threading.Thread(target=_run, name="ftp-server", daemon=True)
            self._thread.start()
            self._thread.join(timeout=0.4)
            if not self._thread.is_alive():
                self.status.error = self.status.error or "FTP 未能启动(端口可能被占用)"
                return self.status
            self.status.running = True
            self.status.error = ""
            log.info(
                "FTP 服务已启动: %s:%s -> %s (passive via %s)",
                self.cfg.ftp.bind,
                self.cfg.ftp.port,
                self.root,
                self.lan_ip,
            )
        except Exception as exc:  # noqa: BLE001
            self.status.running = False
            self.status.error = f"FTP 启动失败: {exc}"
            log.warning(self.status.error)
        return self.status

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close_all()
            except Exception:
                pass
        self._server = None
        self.status.running = False


class FileserverManager:
    def __init__(self, cfg: Config, http_port: int | None = None):
        self.cfg = cfg
        self.http_port = http_port
        root = cfg.paths.image_path()
        root.mkdir(parents=True, exist_ok=True)
        lan_ip = cfg.firmware_host()
        self.tftp = TftpService(cfg.firmware_server, root)
        self.ftp = FtpService(cfg.firmware_server, root, lan_ip)
        self.http_status = ServerStatus(
            "http",
            enabled=cfg.firmware_server.http.enabled,
            bind=cfg.web.host,
            port=http_port or cfg.web.port,
            root=str(root),
        )

    def start(self) -> None:
        self.tftp.start()
        self.ftp.start()
        if self.http_status.enabled:
            self.http_status.running = True
            self.http_status.error = ""
        else:
            self.http_status.error = "配置中未启用"

    def update_http_port(self, port: int, running: bool = True) -> None:
        self.http_status.port = port
        self.http_status.running = running and self.http_status.enabled

    def stop(self) -> None:
        self.tftp.stop()
        self.ftp.stop()

    def status(self) -> dict[str, Any]:
        return {
            "host": self.cfg.firmware_host(),
            "http_port": self.http_port or self.cfg.web.port,
            "servers": [
                self.tftp.status.to_dict(),
                self.ftp.status.to_dict(),
                self.http_status.to_dict(),
            ],
        }

    def pull_hints(self, filename: str, remote_path: str, token: str = "") -> dict[str, str]:
        cfg = self.cfg
        host = cfg.firmware_host()
        http_port = self.http_port or cfg.web.port
        token = token if token else cfg.firmware_server.http.token
        values = {
            "file": filename,
            "host": host,
            "http_port": http_port,
            "port": cfg.firmware_server.ftp.port,
            "user": cfg.firmware_server.ftp.username,
            "pass": cfg.firmware_server.ftp.password,
            "remote_path": remote_path,
            "token": token,
        }
        result: dict[str, str] = {}
        for name, template in cfg.firmware.pull_templates.model_dump().items():
            try:
                result[name] = template.format(**values)
            except KeyError as exc:
                result[name] = f"<模板缺少占位符 {exc}>"
        return result


async def to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)

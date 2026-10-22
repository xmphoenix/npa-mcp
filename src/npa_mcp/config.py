"""配置模型与热加载.

配置保存在 ``~/.npa-mcp/config.json``(可用 NPA_MCP_HOME 覆盖)。
网页保存后无需重启进程:每次工具调用按 (mtime, size) 重新加载。
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import socket
import threading
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .util import atomic_write_text, ensure_dir, get_logger

log = get_logger(__name__)

SECRET_KEYS = {"password", "passphrase", "token", "api_key", "secret"}
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(RuntimeError):
    """配置缺失或非法."""


def npa_home() -> Path:
    return Path(os.environ.get("NPA_MCP_HOME", str(Path.home() / ".npa-mcp"))).expanduser()


def default_config_path() -> Path:
    return npa_home() / "config.json"


def _default_paths() -> dict[str, str]:
    home = npa_home()
    return {
        "image_dir": str(home / "images"),
        "report_dir": str(home / "reports"),
        "log_dir": str(home / "logs"),
    }


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AuthConfig(_Model):
    type: Literal["password", "key"] = "password"
    password: str = ""
    key_file: str = ""
    passphrase: str = ""

    def is_configured(self) -> bool:
        return bool(self.key_file.strip()) if self.type == "key" else bool(self.password)


class CodeServerConfig(_Model):
    host: str = ""
    port: int = 22
    username: str = ""
    auth: AuthConfig = Field(default_factory=AuthConfig)
    workdir: str = "~"
    shell_prompt: str = "$ "


class DeviceSshConfig(_Model):
    enabled: bool = False
    host: str = ""
    port: int = 22
    username: str = ""
    auth: AuthConfig = Field(default_factory=AuthConfig)
    shell_prompt: str = "# "
    close_cmd: str = "exit"


class DeviceTelnetConfig(_Model):
    enabled: bool = False
    host: str = ""
    port: int = 23
    username: str = ""
    password: str = ""
    login_required: bool = True
    login_prompt: str = "login:"
    password_prompt: str = "Password:"
    shell_prompt: str = "# "
    close_cmd: str = "exit"
    echo_strip: bool = True
    line_ending: Literal["crlf", "lf", "cr"] = "crlf"
    encoding: str = "utf-8"


class DeviceSerialConfig(_Model):
    enabled: bool = False
    port: str = "COM3"
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    username: str = "root"
    password: str = ""
    encoding: str = "utf-8"
    line_ending: Literal["crlf", "lf", "cr"] = "crlf"
    login_required: bool = True
    login_prompt: str = "login:"
    password_prompt: str = "Password:"
    shell_prompt: str = "# "
    echo_strip: bool = True

    @field_validator("port")
    @classmethod
    def _normalize_port(cls, value: str) -> str:
        return normalize_com_port(value)


class DeviceConfig(_Model):
    prefer: Literal["ssh", "telnet", "serial"] = "serial"
    ssh: DeviceSshConfig = Field(default_factory=DeviceSshConfig)
    telnet: DeviceTelnetConfig = Field(default_factory=DeviceTelnetConfig)
    serial: DeviceSerialConfig = Field(default_factory=DeviceSerialConfig)


class PowerCommands(_Model):
    on: str = "outlet {outlet} on"
    off: str = "outlet {outlet} off"
    status: str = "outlet {outlet} status"


class PowerExpect(_Model):
    ok: list[str] = Field(default_factory=lambda: ["OK", "success", "ON", "OFF"])
    fail: list[str] = Field(default_factory=lambda: ["ERROR", "invalid", "denied", "fail"])


class PowerConfig(_Model):
    transport: Literal["telnet", "ssh"] = "telnet"
    host: str = ""
    port: int = 23
    username: str = ""
    password: str = ""
    login_required: bool = True
    login_prompt: str = "Login:"
    password_prompt: str = "Password:"
    prompt: str = "PDU>"
    outlet: int = 1
    commands: PowerCommands = Field(default_factory=PowerCommands)
    expect: PowerExpect = Field(default_factory=PowerExpect)
    session_reuse: bool = True
    timeout_sec: int = 15
    encoding: str = "utf-8"


class TftpConfig(_Model):
    enabled: bool = True
    port: int = 69
    bind: str = "0.0.0.0"


class FtpConfig(_Model):
    enabled: bool = True
    port: int = 2121
    bind: str = "0.0.0.0"
    username: str = "npa"
    password: str = "npa-pass"
    passive_ports: str = "30000-30100"


class HttpConfig(_Model):
    enabled: bool = True
    token: str = ""


class FirmwareServerConfig(_Model):
    host: str = ""  # 空 = 自动探测局域网 IP
    tftp: TftpConfig = Field(default_factory=TftpConfig)
    ftp: FtpConfig = Field(default_factory=FtpConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)


class PullTemplates(_Model):
    tftp: str = "tftp -g -r {file} {host}"
    wget: str = "wget http://{host}:{http_port}/images/{file}?token={token} -O {remote_path}"
    ftpget: str = "ftpget -P {port} -u {user} -p {pass} {host} {file} {remote_path}"
    scp: str = "scp {user}@{host}:{file} {remote_path}"


class VerifyConfig(_Model):
    cmd: str = "ls -l {remote_path} && md5sum {remote_path}"
    expect_ok: str = ""


class FirmwareConfig(_Model):
    device_pull_dir: str = "/tmp"
    pull_templates: PullTemplates = Field(default_factory=PullTemplates)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)


class JobConfig(_Model):
    project_dir: str = ""
    task_file: str = str(npa_home() / "task.md")
    resume_session_id: str = ""
    model: str = ""
    max_turns: int = 100
    allowed_tools: str = "mcp__npa_mcp__*,Bash,Read,Edit,Write,Glob,Grep"


class WorkWindow(_Model):
    name: str = "nightly"
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # Monday=0
    start: str = "23:00"
    end: str = "06:00"
    job: JobConfig = Field(default_factory=JobConfig)

    @field_validator("days")
    @classmethod
    def _validate_days(cls, value: list[int]) -> list[int]:
        cleaned = sorted({int(v) for v in value})
        if not cleaned or any(v < 0 or v > 6 for v in cleaned):
            raise ValueError("days 必须是 0-6 的整数(0=周一,6=周日),且不能为空")
        return cleaned

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        if not _HHMM_RE.match(value):
            raise ValueError("时间格式必须是 HH:MM,例如 23:00")
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("时间段名称不能为空")
        return value

    # -- 时间窗口计算(跨午夜受支持) -------------------------------------
    def start_time(self) -> dtime:
        h, m = self.start.split(":")
        return dtime(int(h), int(m))

    def end_time(self) -> dtime:
        h, m = self.end.split(":")
        return dtime(int(h), int(m))

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        t = now.time()
        start, end = self.start_time(), self.end_time()
        today = now.weekday()
        if start <= end:
            return today in self.days and start <= t <= end
        return (today in self.days and t >= start) or (((today - 1) % 7) in self.days and t <= end)

    def active_end(self, now: datetime | None = None) -> datetime | None:
        """若当前在窗口内,返回本次窗口的结束时间."""
        now = now or datetime.now()
        if not self.is_active(now):
            return None
        end = self.end_time()
        result = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if result <= now:
            result = result.replace(day=result.day)  # 同日窗口还没结束的情况在下方修正
        start, finish = self.start_time(), self.end_time()
        if start <= finish:
            return now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        # 跨午夜:若当前时间 <= end,结束在今天;否则结束在明天
        if now.time() <= finish:
            return now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        from datetime import timedelta

        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)

    def next_start(self, now: datetime | None = None) -> datetime:
        from datetime import timedelta

        now = now or datetime.now()
        start = self.start_time()
        for offset in range(0, 8):
            day = now + timedelta(days=offset)
            candidate = day.replace(
                hour=start.hour, minute=start.minute, second=0, microsecond=0
            )
            if day.weekday() in self.days and candidate >= now:
                return candidate
        # 理论不可达(8 天内必有一次)
        return now


class WebConfig(_Model):
    host: str = "127.0.0.1"
    port: int = 8765
    keep_alive_sec: int = 0
    # 启动时自动在浏览器打开配置页;无头/计划任务(NPA_MCP_WINDOW)下自动跳过
    auto_open: bool = True
    auto_open_interval_sec: int = Field(default=30, ge=0)
    # 每个交互会话自动开一个独立控制台窗口(类似 cmd),浏览器终端页保留作备用
    native_terminal: bool = True


class PathsConfig(_Model):
    image_dir: str = Field(default_factory=lambda: _default_paths()["image_dir"])
    report_dir: str = Field(default_factory=lambda: _default_paths()["report_dir"])
    log_dir: str = Field(default_factory=lambda: _default_paths()["log_dir"])

    def image_path(self) -> Path:
        return Path(self.image_dir).expanduser()

    def report_path(self) -> Path:
        return Path(self.report_dir).expanduser()

    def log_path(self) -> Path:
        return Path(self.log_dir).expanduser()

    def ensure_dirs(self) -> None:
        for path in (self.image_path(), self.report_path(), self.log_path()):
            ensure_dir(path)


class Config(_Model):
    version: int = 1
    web: WebConfig = Field(default_factory=WebConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    code_server: CodeServerConfig = Field(default_factory=CodeServerConfig)
    device: DeviceConfig = Field(default_factory=DeviceConfig)
    power: PowerConfig = Field(default_factory=PowerConfig)
    firmware_server: FirmwareServerConfig = Field(default_factory=FirmwareServerConfig)
    firmware: FirmwareConfig = Field(default_factory=FirmwareConfig)
    work_windows: list[WorkWindow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_windows(self) -> "Config":
        names = [w.name for w in self.work_windows]
        if len(names) != len(set(names)):
            raise ValueError("work_windows 中名称不能重复")
        return self

    def window(self, name: str) -> WorkWindow | None:
        for window in self.work_windows:
            if window.name == name:
                return window
        return None

    def firmware_host(self) -> str:
        host = self.firmware_server.host.strip()
        return host or detect_lan_ip(self.lan_peer())

    def lan_peer(self) -> str:
        """开发板/电源等已知设备的地址,用于选出与它们同网段的本机 IP."""
        candidates: list[str] = []
        enabled = {
            "ssh": self.device.ssh.enabled,
            "telnet": self.device.telnet.enabled,
        }
        for name in ("ssh", "telnet"):
            if enabled.get(name):
                candidates.append(getattr(self.device, name).host)
        candidates.extend(
            [
                self.device.ssh.host,
                self.device.telnet.host,
                self.power.host,
                self.code_server.host,
            ]
        )
        for host in candidates:
            value = _ipv4_or_empty(host)
            if value and not value.startswith("127."):
                return value
        return ""

    def ensure_dirs(self) -> None:
        self.paths.ensure_dirs()


def normalize_com_port(port: str) -> str:
    r"""COM10+ 需要 \\.\ 前缀才能被 pyserial 正确打开."""
    if not port:
        return port
    value = port.strip()
    if value.upper().startswith("COM") and not value.startswith("\\\\.\\"):
        number = value[3:]
        if number.isdigit() and int(number) >= 10:
            return f"\\\\.\\{value}"
    return value


def _ipv4_or_empty(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return ""
    return str(ip) if ip.version == 4 else ""


def _udp_local_ip(target: str) -> str:
    """UDP connect 不发包,只按路由表返回本机源地址."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect((target, 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith(("127.", "169.254.", "0.")):
            return ip
    except Exception:
        pass
    finally:
        sock.close()
    return ""


def _local_ipv4_candidates() -> list[str]:
    """枚举本机 IPv4(不含回环/链路本地),虚拟网卡的 .1 地址排在后面."""
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        return []
    unique: list[str] = []
    for addr in addrs:
        value = _ipv4_or_empty(addr)
        if not value or value.startswith(("127.", "169.254.", "0.")):
            continue
        if value not in unique:
            unique.append(value)
    # VirtualBox/VMware/WSL/Docker 等虚拟网卡通常占 .1;物理网卡多是 DHCP 地址
    return sorted(unique, key=lambda ip: ip.endswith(".1"))


def detect_lan_ip(peer: str = "") -> str:
    """探测本机面向局域网(或指定 peer)的 IPv4.

    优先用 peer(开发板地址)反查源地址:本机装有 Sing-box/WSL 等虚拟网卡时,
    默认路由会落在虚拟网卡上,直接连 8.8.8.8 会选出 172.19.0.1 这类地址,
    导致开发板根本连不到本机的 TFTP/FTP/HTTP 服务。
    """
    peer = (peer or "").strip()
    if peer:
        ip = _udp_local_ip(peer)
        if ip:
            return ip
    candidates = _local_ipv4_candidates()
    if candidates:
        return candidates[0]
    ip = _udp_local_ip("8.8.8.8")
    return ip or "127.0.0.1"


def _walk_secrets(data: Any, old: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(data, dict):
        old_dict = old if isinstance(old, dict) else {}
        result: dict[str, Any] = {}
        for key, value in data.items():
            old_value = old_dict.get(key)
            if key in SECRET_KEYS and isinstance(value, str) and value == "***":
                result[key] = old_value if isinstance(old_value, str) else ""
            else:
                result[key] = _walk_secrets(value, old_value, (*path, key))
        return result
    if isinstance(data, list):
        old_list = old if isinstance(old, list) else []
        return [
            _walk_secrets(item, old_list[i] if i < len(old_list) else None, (*path, str(i)))
            for i, item in enumerate(data)
        ]
    return data


def _mask(data: Any) -> Any:
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if key in SECRET_KEYS and isinstance(value, str):
                result[key] = "***" if value else ""
            else:
                result[key] = _mask(value)
        return result
    if isinstance(data, list):
        return [_mask(item) for item in data]
    return data


def _deep_merge(old: Any, new: Any) -> Any:
    """以 old 为底、new 覆盖合并;new 中不存在的键保留 old."""
    if isinstance(old, dict) and isinstance(new, dict):
        result = copy.deepcopy(old)
        for key, value in new.items():
            result[key] = _deep_merge(old.get(key), value)
        return result
    return new


class ConfigManager:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path).expanduser() if path else default_config_path()
        self._cache: Config | None = None
        self._cache_key: tuple[int, int] | None = None
        self._lock = threading.RLock()

    def ensure_default(self) -> None:
        with self._lock:
            if not self.path.exists():
                ensure_dir(self.path.parent)
                cfg = Config()
                self.save(cfg)
                log.info("已生成默认配置: %s", self.path)

    def load(self, force: bool = False) -> Config:
        with self._lock:
            self.ensure_default()
            try:
                stat = self.path.stat()
            except OSError as exc:
                raise ConfigError(f"无法读取配置文件 {self.path}: {exc}") from exc
            key = (stat.st_mtime_ns, stat.st_size)
            if not force and self._cache is not None and key == self._cache_key:
                return self._cache
            try:
                raw = self.path.read_text(encoding="utf-8-sig")
                data = json.loads(raw) if raw.strip() else {}
                cfg = Config.model_validate(data)
            except (json.JSONDecodeError, ValueError) as exc:
                backup = self.path.with_suffix(f".corrupt-{int(datetime.now().timestamp())}.json")
                try:
                    self.path.replace(backup)
                    log.error("配置文件损坏,已备份到 %s: %s", backup, exc)
                except OSError:
                    log.error("配置文件损坏且备份失败: %s", exc)
                cfg = Config()
                self.save(cfg)
            try:
                cfg.ensure_dirs()
            except OSError as exc:
                log.warning("创建数据目录失败: %s", exc)
            self._cache = cfg
            self._cache_key = key
            return cfg

    def save(self, cfg: Config) -> Config:
        with self._lock:
            payload = json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False, indent=2)
            atomic_write_text(self.path, payload + "\n")
            stat = self.path.stat()
            self._cache = cfg
            self._cache_key = (stat.st_mtime_ns, stat.st_size)
            return cfg

    def masked_dict(self) -> dict[str, Any]:
        return _mask(self.load().model_dump(mode="json"))

    def update_from_dict(self, patch: dict[str, Any]) -> Config:
        """网页保存:与现有配置深合并,处理 *** 密码哨兵."""
        current = self.load().model_dump(mode="json")
        merged = _walk_secrets(patch, current)
        merged = _deep_merge(current, merged)
        cfg = Config.model_validate(merged)
        return self.save(cfg)

    def reload(self) -> Config:
        return self.load(force=True)


def auth_summary(auth: AuthConfig) -> str:
    if auth.type == "key":
        return "key" if auth.key_file else "未配置"
    return "password" if auth.password else "未配置"

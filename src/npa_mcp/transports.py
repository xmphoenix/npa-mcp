"""SSH / SFTP 传输层.

- SSH 连接池:按目标复用连接,空闲 10 分钟回收,keepalive 30s。
- exec:自己读 channel,支持超时与超时后关闭远端命令。
- 写远端文件:临时文件 + rename 原子替换,绝不把 ``\\n`` 换成 CRLF。
- 下载:先写 ``*.part`` 再 os.replace。
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import stat as stat_mod
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import paramiko

from .config import AuthConfig, CodeServerConfig, DeviceSshConfig
from .util import ensure_dir, get_logger

log = get_logger(__name__)

CONNECT_TIMEOUT = 15.0
IDLE_RECYCLE_SEC = 600.0


class TransportError(RuntimeError):
    """网络/认证/文件传输失败,message 为中文说明."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass
class ExecResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_sec": round(self.duration_sec, 3),
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int
    username: str
    auth_type: str = "password"
    password: str = field(default="", repr=False)
    key_file: str = ""
    passphrase: str = field(default="", repr=False)

    @classmethod
    def from_code_server(cls, cfg: CodeServerConfig) -> "SshTarget":
        return cls(
            host=cfg.host,
            port=cfg.port,
            username=cfg.username,
            auth_type=cfg.auth.type,
            password=cfg.auth.password,
            key_file=cfg.auth.key_file,
            passphrase=cfg.auth.passphrase,
        )

    @classmethod
    def from_device(cls, cfg: DeviceSshConfig) -> "SshTarget":
        return cls(
            host=cfg.host,
            port=cfg.port,
            username=cfg.username,
            auth_type=cfg.auth.type,
            password=cfg.auth.password,
            key_file=cfg.auth.key_file,
            passphrase=cfg.auth.passphrase,
        )

    def key(self) -> tuple[str, int, str, str]:
        return (self.host, self.port, self.username, self.auth_type)

    def ready(self) -> bool:
        if not self.host or not self.username:
            return False
        if self.auth_type == "key":
            return bool(self.key_file)
        return bool(self.password)


def _load_private_key(auth: AuthConfig) -> paramiko.PKey:
    path = Path(auth.key_file).expanduser()
    if not path.exists():
        raise TransportError(
            f"SSH 私钥文件不存在: {path}", "请检查配置页中的私钥路径,或改用密码登录。"
        )
    last_error: Exception | None = None
    # paramiko 5.x 移除了 DSSKey,因此候选列表按可用性构造
    candidates = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
    dss_cls = getattr(paramiko, "DSSKey", None)
    if dss_cls is not None:
        candidates.append(dss_cls)
    for key_cls in candidates:
        try:
            return key_cls.from_private_key_file(str(path), password=auth.passphrase or None)
        except (paramiko.SSHException, ValueError) as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001 - 不同 key 类型抛的异常不一致
            last_error = exc
    raise TransportError(
        f"无法解析 SSH 私钥 {path}: {last_error}",
        "确认私钥格式为 OpenSSH/PEM,且口令正确。",
    )


class _PooledClient:
    def __init__(self, client: paramiko.SSHClient):
        self.client = client
        self.last_used = time.monotonic()
        self.lock = threading.Lock()

    def touch(self) -> paramiko.SSHClient:
        self.last_used = time.monotonic()
        return self.client


class SshClientPool:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, int, str, str], _PooledClient] = {}
        self._lock = threading.RLock()

    def _new_client(self, target: SshTarget) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = {
            "hostname": target.host,
            "port": target.port,
            "username": target.username,
            "timeout": CONNECT_TIMEOUT,
            "banner_timeout": CONNECT_TIMEOUT,
            "auth_timeout": CONNECT_TIMEOUT,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if target.auth_type == "key":
            kwargs["pkey"] = _load_private_key(
                AuthConfig(
                    type="key",
                    key_file=target.key_file,
                    passphrase=target.passphrase,
                )
            )
        else:
            kwargs["password"] = target.password
        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException as exc:
            client.close()
            raise TransportError(
                f"SSH 认证失败: {target.username}@{target.host}:{target.port}",
                "账号或密码/私钥错误。请在配置页核对后重新保存。",
            ) from exc
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            raise TransportError(
                f"SSH 连接失败: {target.host}:{target.port} ({exc})",
                "确认目标主机可达、端口正确、防火墙放行,且 SSH 服务已启动。",
            ) from exc
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        return client

    def get(self, target: SshTarget) -> paramiko.SSHClient:
        if not target.ready():
            raise TransportError(
                f"SSH 目标未配置完整: {target.host or '(空主机)'}",
                "请先调用 open_config_page 打开配置页填写地址与凭据。",
            )
        key = target.key()
        with self._lock:
            self._reap_locked()
            entry = self._entries.get(key)
            if entry is not None:
                transport = entry.client.get_transport()
                if transport is not None and transport.is_active():
                    return entry.touch()
                try:
                    entry.client.close()
                except Exception:
                    pass
                self._entries.pop(key, None)
            client = self._new_client(target)
            self._entries[key] = _PooledClient(client)
            return client

    def _reap_locked(self) -> None:
        now = time.monotonic()
        for key, entry in list(self._entries.items()):
            transport = entry.client.get_transport()
            stale = now - entry.last_used > IDLE_RECYCLE_SEC
            if stale or transport is None or not transport.is_active():
                try:
                    entry.client.close()
                except Exception:
                    pass
                self._entries.pop(key, None)

    def close_all(self) -> None:
        with self._lock:
            for entry in self._entries.values():
                try:
                    entry.client.close()
                except Exception:
                    pass
            self._entries.clear()


POOL = SshClientPool()


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def is_windows_path(value: str) -> bool:
    """判断是否盘符或 UNC 路径(``C:\\x`` / ``C:/x`` / ``\\\\server\\share``)."""
    return bool(WINDOWS_PATH_RE.match((value or "").strip()))


def cd_prefix(cwd: str) -> str:
    """生成"进入工作目录"的命令前缀.

    Windows OpenSSH 的默认 shell 是 cmd.exe,POSIX 单引号在那里不生效,
    因此按路径形态选择语法;cmd.exe 不支持 UNC 作为当前目录,用 pushd。
    """
    text = (cwd or "").strip()
    if is_windows_path(text):
        verb = "pushd" if text.startswith("\\\\") else "cd /d"
        return f'{verb} "{text}" && '
    return f"cd {shell_quote(text)} && "


def ssh_exec(
    target: SshTarget,
    command: str,
    *,
    timeout: float = 120.0,
    cwd: str | None = None,
) -> ExecResult:
    """在工作线程中调用. 返回 ExecResult,超时不抛异常."""
    client = POOL.get(target)
    if cwd:
        command = cd_prefix(cwd) + command
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise TransportError("SSH 连接已断开", "请重试;若反复出现请检查网络。")
    try:
        chan = transport.open_session(timeout=CONNECT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        raise TransportError(f"无法打开 SSH 会话: {exc}", "连接可能已失效,请重试。") from exc
    chan.settimeout(0.2)
    started = time.monotonic()
    try:
        chan.exec_command(command)
    except Exception as exc:  # noqa: BLE001
        chan.close()
        raise TransportError(f"命令执行失败: {exc}", "请检查命令是否合法。") from exc
    out = bytearray()
    err = bytearray()
    deadline = started + timeout
    timed_out = False
    while True:
        drained = False
        try:
            while chan.recv_ready():
                out += chan.recv(65536)
                drained = True
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
                drained = True
        except Exception:  # noqa: BLE001 - channel closed mid-read
            break
        if chan.exit_status_ready() and not drained and not chan.recv_ready():
            break
        if chan.closed:
            break
        if time.monotonic() > deadline:
            timed_out = True
            try:
                chan.close()
            except Exception:
                pass
            break
        time.sleep(0.05)
    exit_code = -1
    if not timed_out:
        try:
            exit_code = chan.recv_exit_status()
        except Exception:
            exit_code = -1
        finally:
            try:
                chan.close()
            except Exception:
                pass
    duration = time.monotonic() - started
    return ExecResult(
        command=command,
        exit_code=exit_code,
        stdout=_decode(bytes(out)),
        stderr=_decode(bytes(err)),
        duration_sec=duration,
        timed_out=timed_out,
    )


def shell_quote(value: str) -> str:
    """POSIX 单引号转义,避免命令注入与空格问题."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _sftp(target: SshTarget) -> paramiko.SFTPClient:
    client = POOL.get(target)
    try:
        sftp = client.open_sftp()
    except Exception as exc:  # noqa: BLE001
        raise TransportError(f"无法建立 SFTP 通道: {exc}", "目标可能未启用 SFTP。") from exc
    sftp.get_channel().settimeout(30)
    return sftp


def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    if not remote_dir or remote_dir in ("/", "."):
        return
    parts = remote_dir.strip("/").split("/")
    current = "/" if remote_dir.startswith("/") else ""
    for part in parts:
        current = posixpath.join(current, part) if current else part
        try:
            sftp.stat(current)
        except IOError:
            try:
                sftp.mkdir(current)
            except IOError as exc:
                raise TransportError(
                    f"无法创建远端目录 {current}: {exc}", "请检查用户权限。"
                ) from exc


def sftp_read(target: SshTarget, remote_path: str, max_bytes: int = 2 * 1024 * 1024) -> dict:
    sftp = _sftp(target)
    try:
        try:
            st = sftp.stat(remote_path)
        except IOError as exc:
            raise TransportError(f"远端文件不存在或不可读: {remote_path} ({exc})") from exc
        size = int(st.st_size)
        with sftp.open(remote_path, "rb") as fh:
            data = fh.read(min(size, max_bytes) if max_bytes > 0 else size)
        truncated = max_bytes > 0 and size > max_bytes
        return {
            "path": remote_path,
            "size": size,
            "truncated": truncated,
            "content": data.decode("utf-8", errors="replace"),
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        }
    finally:
        sftp.close()


def sftp_write(target: SshTarget, remote_path: str, content: str, mode: int = 0o644) -> dict:
    data = content.encode("utf-8")
    return sftp_write_bytes(target, remote_path, data, mode=mode)


def sftp_write_bytes(
    target: SshTarget, remote_path: str, data: bytes, mode: int = 0o644, backup: bool = True
) -> dict:
    sftp = _sftp(target)
    remote_dir = posixpath.dirname(remote_path) or "."
    tmp_path = posixpath.join(remote_dir, f".{posixpath.basename(remote_path)}.npa-tmp")
    try:
        _ensure_remote_dir(sftp, remote_dir if remote_dir != "." else "")
        if backup:
            try:
                st = sftp.stat(remote_path)
                if stat_mod.S_ISREG(st.st_mode):
                    bak = remote_path + ".bak"
                    try:
                        sftp.remove(bak)
                    except IOError:
                        pass
                    sftp.rename(remote_path, bak)
            except IOError:
                pass
        with sftp.open(tmp_path, "wb") as fh:
            fh.write(data)
        try:
            sftp.chmod(tmp_path, mode)
        except IOError:
            pass
        try:
            sftp.posix_rename(tmp_path, remote_path)
        except (IOError, AttributeError):
            try:
                sftp.remove(remote_path)
            except IOError:
                pass
            sftp.rename(tmp_path, remote_path)
        return {"path": remote_path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    except TransportError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TransportError(f"写远端文件失败: {remote_path} ({exc})", "请检查目录权限。") from exc
    finally:
        try:
            sftp.close()
        except Exception:
            pass


def sftp_list(target: SshTarget, remote_dir: str) -> list[dict]:
    sftp = _sftp(target)
    try:
        try:
            attrs = sftp.listdir_attr(remote_dir)
        except IOError as exc:
            raise TransportError(f"无法列出远端目录 {remote_dir}: {exc}") from exc
        result = []
        for attr in attrs:
            is_dir = stat_mod.S_ISDIR(attr.st_mode or 0)
            result.append(
                {
                    "name": attr.filename,
                    "is_dir": is_dir,
                    "size": 0 if is_dir else int(attr.st_size or 0),
                    "mtime": datetime.fromtimestamp(attr.st_mtime or 0).isoformat(
                        timespec="seconds"
                    ),
                    "mode": oct(attr.st_mode or 0),
                }
            )
        result.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        return result
    finally:
        sftp.close()


def sftp_upload(
    target: SshTarget, local_path: Path, remote_path: str, create_dirs: bool = True
) -> dict:
    local_path = Path(local_path)
    if not local_path.is_file():
        raise TransportError(f"本地文件不存在: {local_path}", "请先确认编译产物路径。")
    sftp = _sftp(target)
    try:
        if create_dirs:
            _ensure_remote_dir(sftp, posixpath.dirname(remote_path))
        sftp.put(str(local_path), remote_path, confirm=True)
        st = sftp.stat(remote_path)
        return {
            "local": str(local_path),
            "remote": remote_path,
            "size": int(st.st_size),
            "local_md5": _md5(local_path),
        }
    except TransportError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TransportError(f"上传失败 {local_path} -> {remote_path}: {exc}") from exc
    finally:
        sftp.close()


def sftp_download(target: SshTarget, remote_path: str, local_path: Path) -> dict:
    local_path = Path(local_path)
    ensure_dir(local_path.parent)
    part = local_path.with_name(local_path.name + ".part")
    sftp = _sftp(target)
    try:
        try:
            st = sftp.stat(remote_path)
        except IOError as exc:
            raise TransportError(f"远端文件不存在: {remote_path} ({exc})") from exc
        sftp.get(remote_path, str(part))
        os.replace(part, local_path)
        return {
            "remote": remote_path,
            "local": str(local_path),
            "size": int(st.st_size),
            "md5": _md5(local_path),
        }
    except TransportError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TransportError(f"下载失败 {remote_path} -> {local_path}: {exc}") from exc
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        sftp.close()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def close_all() -> None:
    POOL.close_all()

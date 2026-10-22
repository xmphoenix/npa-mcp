"""通用工具:原子写、子进程、路径、日志、哈希."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Sequence

_LOG_LOCK = threading.Lock()
_LOGGING_READY = False


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """同目录临时文件 + os.replace,避免读到半截内容."""
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_text(path: Path, encoding: str = "utf-8") -> str:
    """读取文本,UTF-8 时兼容 BOM."""
    return Path(path).read_text(encoding="utf-8-sig" if encoding == "utf-8" else encoding)


def open_url(url: str) -> bool:
    """用默认浏览器打开 URL,失败回退 os.startfile(Windows)."""
    try:
        if webbrowser.open(url, new=2):
            return True
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except Exception:
            pass
    return False


def run_command(
    args: Sequence[str],
    *,
    timeout: float = 30.0,
    cwd: str | Path | None = None,
    encoding: str | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """运行命令并捕获输出. Windows 下隐藏控制台窗口."""
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": encoding or "utf-8",
        "errors": "replace",
        "cwd": str(cwd) if cwd else None,
        "input": input_text,
        "timeout": timeout,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(list(args), **kwargs)


def md5_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def safe_slug(text: str, fallback: str = "report") -> str:
    text = (text or "").strip()
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")
    return slug[:60] or fallback


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def truncate_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """按 UTF-8 字节截断,不切坏多字节字符."""
    if max_bytes <= 0:
        return "", bool(text)
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text, False
    cut = raw[:max_bytes].decode("utf-8", "ignore")
    return cut, True


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """日志只写 stderr + 文件,绝不写 stdout(MCP 协议占用)."""
    global _LOGGING_READY
    with _LOG_LOCK:
        if _LOGGING_READY:
            return
        ensure_dir(log_dir)
        root = logging.getLogger()
        root.setLevel(level)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        file_handler = RotatingFileHandler(
            str(log_dir / "npa-mcp.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(fmt)
        root.addHandler(stream)
        root.addHandler(file_handler)
        _LOGGING_READY = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def safe_join(root: Path, relative: str) -> Path | None:
    """把 URL 相对路径安全地解析到 root 内,越界返回 None."""
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


class TokenBucket:
    """简单的输出限速辅助(保留给日志洪泛场景)."""

    def __init__(self, max_tokens: int, refill_per_sec: float):
        self.max_tokens = max_tokens
        self.tokens = float(max_tokens)
        self.refill_per_sec = refill_per_sec
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def take(self, count: int) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(
                float(self.max_tokens),
                self.tokens + (now - self.updated) * self.refill_per_sec,
            )
            self.updated = now
            if self.tokens >= count:
                self.tokens -= count
                return True
            return False

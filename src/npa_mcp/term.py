"""独立控制台窗口客户端:在原生 cmd 风格窗口中显示一个 npa-mcp 会话.

由 npa-mcp 以 ``CREATE_NEW_CONSOLE`` 启动::

    python -m npa_mcp.term --url http://127.0.0.1:8765 --session serial-1

窗口内实时显示会话输出,键盘输入原样转发(Enter、方向键、Ctrl+C 等),
Ctrl+] 关闭窗口但保留底层会话。浏览器终端页仍可作为备用视图。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading

from websockets import connect

_EXIT = object()

# msvcrt 的扩展键码 -> ANSI 序列(xterm.js 也发这些)
_SPECIAL_KEYS = {
    "H": "\x1b[A",  # ↑
    "P": "\x1b[B",  # ↓
    "M": "\x1b[C",  # →
    "K": "\x1b[D",  # ←
    "G": "\x1b[H",  # Home
    "O": "\x1b[F",  # End
    "S": "\x1b[3~",  # Delete
    "I": "\x1b[5~",  # PgUp
    "Q": "\x1b[6~",  # PgDn
    "R": "\x1b[2~",  # Insert
}

_last_char = ""


def _write(text: str) -> None:
    """按 Windows 控制台习惯补全换行(单独的 \\n 前补 \\r)."""
    global _last_char
    if not text:
        return
    out: list[str] = []
    prev = _last_char
    for ch in text:
        if ch == "\n" and prev != "\r":
            out.append("\r")
        out.append(ch)
        prev = ch
    _last_char = prev
    try:
        sys.stdout.write("".join(out))
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _prepare_console(title: str) -> None:
    """设置窗口标题、开启 VT 转义渲染、关掉 Ctrl+C 中断处理."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if title:
            kernel32.SetConsoleTitleW(title)
        # stdout: ENABLE_VIRTUAL_TERMINAL_PROCESSING,让颜色/进度条正常显示
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        # stdin: 关闭 ENABLE_PROCESSED_INPUT,否则 Ctrl+C 会中断本进程
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value & ~0x0001)
    except Exception:  # noqa: BLE001
        pass


def _key_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[object]) -> None:
    """后台线程逐键读取控制台输入,转成会话需要的字符序列."""
    try:
        import msvcrt
    except ImportError:  # 非 Windows:退化为行输入
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, _EXIT)
        return
    while True:
        try:
            ch = msvcrt.getwch()
        except Exception:  # noqa: BLE001
            return
        text = ""
        if ch in ("\x00", "\xe0"):
            text = _SPECIAL_KEYS.get(msvcrt.getwch(), "")
        elif ch == "\r":
            text = "\r"
        elif ch == "\x08":
            text = "\x7f"
        elif ch == "\x1d":  # Ctrl+]
            loop.call_soon_threadsafe(queue.put_nowait, _EXIT)
            return
        else:
            text = ch
        if text:
            loop.call_soon_threadsafe(queue.put_nowait, text)


def _ws_url(base: str, session_id: str) -> str:
    base = base.strip().rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    elif not base.startswith(("ws://", "wss://")):
        base = "ws://" + base
    return f"{base}/ws/session/{session_id}"


async def _run(url: str, session_id: str, title: str) -> int:
    _prepare_console(title)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[object] = asyncio.Queue()
    threading.Thread(target=_key_reader, args=(loop, queue), daemon=True).start()
    read_only = False
    try:
        async with connect(_ws_url(url, session_id), max_size=4 * 1024 * 1024) as ws:

            async def _receive() -> None:
                nonlocal read_only
                async for raw in ws:
                    try:
                        message = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    kind = message.get("type")
                    if kind == "init":
                        read_only = bool(message.get("read_only"))
                        header = (
                            "[npa-mcp] 只读镜像终端 Ctrl+] 关闭窗口\r\n"
                            if read_only
                            else "[npa-mcp] Ctrl+] 关闭窗口\r\n"
                        )
                        _write(header + str(message.get("text") or ""))
                    elif kind in ("data", "notice"):
                        _write(str(message.get("text") or ""))

            async def _send() -> None:
                while True:
                    item = await queue.get()
                    if item is _EXIT:
                        return
                    if read_only:
                        continue
                    await ws.send(
                        json.dumps({"type": "input", "data": item}, ensure_ascii=False)
                    )

            receiver = asyncio.create_task(_receive())
            sender = asyncio.create_task(_send())
            try:
                await asyncio.wait({receiver, sender}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (receiver, sender):
                    if not task.done():
                        task.cancel()
                for task in (receiver, sender):
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
    except (OSError, asyncio.TimeoutError) as exc:
        _write(f"\r\n[npa] 无法连接 npa-mcp 网页服务: {exc}\r\n")
    except Exception as exc:  # noqa: BLE001
        _write(f"\r\n[npa] 终端窗口异常: {exc}\r\n")
    finally:
        _write("\r\n[npa] 会话已结束,按任意键关闭窗口\r\n")
        try:
            while True:
                item = await asyncio.wait_for(queue.get(), timeout=600)
                if isinstance(item, str):
                    break
        except asyncio.TimeoutError:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="npa-mcp-term", description="npa-mcp 独立会话窗口")
    parser.add_argument("--url", required=True, help="npa-mcp 网页服务地址,如 http://127.0.0.1:8765")
    parser.add_argument("--session", required=True, help="会话 ID,如 serial-1")
    parser.add_argument("--title", default="", help="窗口标题")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args.url, args.session, args.title))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

"""M2 冒烟:代码服务器 SSH/SFTP + 网页终端(本机 OpenSSH Server).

用独立临时 NPA_MCP_HOME 启动真实 Runtime,配置指向 127.0.0.1 的
Windows OpenSSH Server(密钥认证),验证:

1. ssh_exec 执行命令(cmd.exe,含 workdir 前缀);
2. remote_write_file / remote_read_file 中文 + LF 往返;
3. remote_list_dir、ssh_upload / ssh_download 的 MD5 校验;
4. 每次远端操作自动出现在"代码服务器"只读镜像终端;
5. session_open + session_send/session_read(交互式 SSH shell);
6. 终端页 /terminals + WebSocket 实时收发;
7. session_close 后状态正确。

运行(需先启用本机 OpenSSH Server 并把公钥加入管理员授权文件)::

    .venv\\Scripts\\python.exe dev\\smoke_m2.py ^
        --key "C:\\path\\id_ed25519" [--user <Windows 用户名>] [--port 8899]
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        raise AssertionError(f"{name} {detail}")


async def wait_ws_text(ws, needle: str, timeout: float) -> str:  # type: ignore[no-untyped-def]
    """在 WebSocket 数据帧里等到包含 needle 的文本."""
    collected = []
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"WebSocket 超时未等到 {needle!r};已收到:{''.join(collected)[-500:]}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            message = None
        text = message.get("text", "") if isinstance(message, dict) else str(raw)
        collected.append(text)
        if needle in "".join(collected):
            return "".join(collected)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True, help="私钥文件路径")
    parser.add_argument("--user", default=getpass.getuser(), help="SSH 用户名(默认当前用户)")
    parser.add_argument("--port", type=int, default=8899, help="网页服务端口")
    parser.add_argument("--keep", action="store_true", help="保留临时目录,便于排查")
    args = parser.parse_args()

    key_file = str(Path(args.key).expanduser().resolve())
    check("私钥文件存在", Path(key_file).is_file(), key_file)

    home = Path(tempfile.mkdtemp(prefix="npa-m2-"))
    remote_dir = home / "remote"
    remote_dir.mkdir()
    image_dir = home / "images"
    image_dir.mkdir()
    os.environ["NPA_MCP_HOME"] = str(home)

    remote_dir_posix = remote_dir.as_posix()  # SFTP 与 cmd.exe 都接受正斜杠
    config = {
        "web": {"host": "127.0.0.1", "port": args.port},
        "paths": {"image_dir": str(image_dir), "report_dir": str(home / "reports")},
        "firmware_server": {
            "tftp": {"enabled": False},
            "ftp": {"enabled": False},
            "http": {"enabled": True},
        },
        "code_server": {
            "host": "127.0.0.1",
            "port": 22,
            "username": args.user,
            "workdir": remote_dir_posix,
            "auth": {"type": "key", "key_file": key_file},
        },
        "device": {
            "prefer": "ssh",
            "ssh": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 22,
                "username": args.user,
                "auth": {"type": "key", "key_file": key_file},
                # Windows OpenSSH 默认 shell 是 cmd.exe
                "shell_prompt": r"[A-Za-z]:\\[^>\r\n]*>",
            },
            "telnet": {"enabled": False},
            "serial": {"enabled": False},
        },
    }
    (home / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    from npa_mcp import server
    from npa_mcp.util import setup_logging

    setup_logging(home / "logs")
    rt = server.Runtime(str(home / "config.json"))
    server.RUNTIME = rt

    print(f"M2 冒烟:临时 NPA_MCP_HOME = {home}")
    print(f"         远端工作目录 = {remote_dir_posix}")
    try:
        await rt.start(no_web=False)
        check("网页服务已启动", bool(rt.web_url), f"web_error={rt.web_error}")
        base_url = rt.web_url

        # -- 1. 凭据总览 ------------------------------------------------
        targets = await server.list_targets.fn()
        check("代码服务器已配置", targets["code_server"]["configured"] is True)
        check("设备 SSH 已配置", targets["device"]["ssh"]["configured"] is True)

        # -- 2. ssh_exec ------------------------------------------------
        echo = await server.ssh_exec.fn("echo npa-m2-hello")
        check(
            "ssh_exec 成功",
            echo.get("ok") is True,
            json.dumps(echo, ensure_ascii=False)[:600],
        )
        check("stdout 含标记", "npa-m2-hello" in echo.get("stdout", ""))
        check("返回 exit_code=0", echo.get("exit_code") == 0)
        check("返回 workdir", echo.get("cwd", "").replace("\\", "/") == remote_dir_posix)
        check("返回终端地址", bool(echo.get("terminal_url")), str(echo.get("terminal_url")))

        # 错误命令:非 0 退出码,但工具不抛异常
        bad = await server.ssh_exec.fn("exit 3")
        check("非 0 退出码标记失败", bad.get("ok") is False and bad.get("exit_code") == 3)

        # -- 3. 文件读写往返(中文 + LF) ---------------------------------
        content = "第一行:中文内容\nsecond line\n\tthird 行\n"
        target_file = f"{remote_dir_posix}/往返-测试.txt"
        written = await server.remote_write_file.fn(target_file, content)
        check(
            "remote_write_file 成功",
            written.get("ok") is True,
            json.dumps(written, ensure_ascii=False)[:600],
        )
        check("写入字节数正确", written.get("bytes") == len(content.encode("utf-8")))
        # 注意:Windows OpenSSH 的管理员会话经 SFTP 建的文件归 BUILTIN\Administrators
        # (提升令牌),本机非提权进程读不了;因此用 SFTP 下载回本地做逐字节校验。
        pulled = await server.ssh_download.fn(
            target_file, str(home / "roundtrip-pulled.txt")
        )
        check("SFTP 回读成功", pulled.get("ok") is True, str(pulled)[:400])
        check(
            "远端字节完全一致(无 CRLF 转换)",
            (home / "roundtrip-pulled.txt").read_bytes() == content.encode("utf-8"),
        )

        read_back = await server.remote_read_file.fn(target_file)
        check(
            "remote_read_file 成功",
            read_back.get("ok") is True,
            json.dumps(read_back, ensure_ascii=False)[:600],
        )
        check("内容完全一致", read_back.get("content") == content)

        # 覆盖写:原子替换 + .bak
        written2 = await server.remote_write_file.fn(target_file, content + "追加\n")
        check("覆盖写成功", written2.get("ok") is True)

        # -- 4. 列目录(同时校验 .bak 与临时文件清理) --------------------
        listing = await server.remote_list_dir.fn("")
        check("remote_list_dir 成功", listing.get("ok") is True, str(listing)[:400])
        names = {item["name"] for item in listing.get("entries", [])}
        check("目录列表含测试文件", "往返-测试.txt" in names, str(names))
        check("已生成 .bak 备份", "往返-测试.txt.bak" in names, str(names))
        check(
            "无残留临时文件",
            not any(name.startswith(".往返-测试") and name.endswith(".npa-tmp")
                    for name in names),
            str(names),
        )
        read_back2 = await server.remote_read_file.fn(target_file)
        check("覆盖后内容正确", read_back2.get("content") == content + "追加\n")

        # -- 5. 上传 / 下载 --------------------------------------------
        local_src = home / "upload-source.bin"
        payload = os.urandom(4096)
        local_src.write_bytes(payload)
        uploaded = await server.ssh_upload.fn(str(local_src), f"{remote_dir_posix}/up.bin")
        check("ssh_upload 成功", uploaded.get("ok") is True, str(uploaded)[:400])
        check("上传后远端 MD5 正确", uploaded.get("local_md5") == hashlib.md5(payload).hexdigest())

        downloaded = await server.ssh_download.fn(
            f"{remote_dir_posix}/up.bin", str(home / "downloaded.bin")
        )
        check("ssh_download 成功", downloaded.get("ok") is True, str(downloaded)[:400])
        check("下载内容一致", (home / "downloaded.bin").read_bytes() == payload)
        check("下载返回 MD5", downloaded.get("md5") == hashlib.md5(payload).hexdigest())

        # -- 6. 镜像终端(每次远端操作都可观看) -------------------------
        sessions = await server.session_list.fn()
        mirrors = [s for s in sessions["sessions"] if s["kind"] == "mirror"]
        check("存在代码服务器镜像终端", bool(mirrors), json.dumps(sessions, ensure_ascii=False)[:400])
        mirror = mirrors[0]
        check("镜像终端只读", mirror.get("read_only") is True)
        mirror_text = rt.hub.tail(mirror["session_id"], 400)  # type: ignore[union-attr]
        check("镜像终端记录了命令", "npa-m2-hello" in mirror_text, mirror_text[-400:])

        # -- 7. 交互式 SSH 会话 ----------------------------------------
        opened = await server.session_open.fn("ssh", "M2 交互会话")
        check("session_open 成功", opened.get("ok") is True, json.dumps(opened, ensure_ascii=False)[:600])
        session_id = opened["session_id"]
        check("返回 terminal_url", bool(opened["terminal_url"]))

        # 用 ver 而不是 echo:命令行回显里不含 "Microsoft Windows",
        # 能证明命中的是命令真实输出而不是输入回显。
        sent = await server.session_send.fn(session_id, "ver", newline=True)
        check("session_send 成功", sent.get("ok") is True, str(sent)[:400])
        read = await server.session_read.fn(
            session_id, wait_pattern=r"Microsoft Windows", timeout=20.0
        )
        check(
            "session_read 等到命令输出",
            read.get("ok") is True and read.get("matched") is True,
            json.dumps(read, ensure_ascii=False)[:600],
        )
        check("输出含系统版本", "Microsoft Windows" in read.get("text", ""))

        # -- 8. 终端页 + WebSocket 实时交互 -----------------------------
        import httpx
        import websockets

        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            page = await client.get("/terminals")
            check("终端页 HTTP 200", page.status_code == 200)
            check("终端页为中文 HTML", "终端" in page.text and "xterm" in page.text.lower())

        ws_url = f"ws://127.0.0.1:{rt.web_port}/ws/session/{session_id}"
        async with websockets.connect(ws_url, open_timeout=10) as ws:
            init = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            check("WebSocket init 帧", init.get("type") == "init", str(init)[:200])
            check("init 带会话信息", init.get("session", {}).get("session_id") == session_id)
            check("init 带历史输出", bool(init.get("text")))

            await ws.send(json.dumps({"type": "input", "data": "echo npa-m2-ws\r\n"}))
            text = await wait_ws_text(ws, "npa-m2-ws", timeout=20.0)
            check("WebSocket 收到实时输出", "npa-m2-ws" in text)

            await ws.send(json.dumps({"type": "ping"}))
            pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            check("WebSocket ping/pong", pong.get("type") == "pong", str(pong))

        # -- 9. 关闭会话 ------------------------------------------------
        closed = await server.session_close.fn(session_id, force=True)
        check("session_close 成功", closed.get("ok") is True, str(closed)[:400])
        after = await server.session_list.fn()
        closed_ids = {s["session_id"] for s in after["sessions"] if s.get("closed")}
        check("会话标记为已关闭", session_id in closed_ids, str(closed_ids))

        print(f"\nM2 冒烟通过:{PASSED} 项检查")
        return 0
    finally:
        await rt.shutdown()
        server.RUNTIME = None
        if not args.keep:
            import shutil

            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except AssertionError as exc:
        print(f"\nM2 冒烟失败: {exc}")
        sys.exit(1)

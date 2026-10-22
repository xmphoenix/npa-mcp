"""假开发板(Telnet):登录提示 + 少量 busybox 风格命令 + 真 TFTP/HTTP 拉取.

用于在没有真实硬件时验证 ``device_exec`` / ``device_pull_firmware`` 全流程。

    python dev/mock_telnet_device.py --port 2323 --root .\\dev\\_mockroot

默认账号 root / root,shell 提示符 ``# ``。
支持的命令:help、uname -a、pwd、ls、cat、md5sum、echo、tftp、wget、reboot、exit。
``tftp -g -r <file> <host>`` 会真的用 tftpy 从本机 TFTP 服务下载文件,
``wget http://host:port/images/<file> -O <out>`` 会真的发 HTTP 请求。
"""

from __future__ import annotations

import argparse
import asyncio
import ftplib
import hashlib
import os
import shlex
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import telnetlib3
import tftpy


def _safe_path(root: Path, name: str) -> Path:
    candidate = (root / name).resolve()
    if root.resolve() not in candidate.parents and candidate != root.resolve():
        raise ValueError("permission denied")
    return candidate


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Commands:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def handle(self, line: str) -> str | None:
        """返回输出文本;None 表示退出会话."""
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            return f"sh: syntax error: {exc}"
        if not argv:
            return ""
        cmd, args = argv[0], argv[1:]
        if cmd in ("exit", "logout"):
            return None
        if cmd == "help":
            return (
                "mock busybox commands:\n"
                "  help | uname -a | pwd | ls [dir] | cat <file> | md5sum <file>\n"
                "  echo <text> | reboot | poweroff | sleep <sec>\n"
                "  tftp -g -r <file> <host> [-l <local>]\n"
                "  wget <url> [-O <out>]\n"
                "  ftpget -P <port> -u <user> -p <pass> <host> [<local>] [<remote>]"
            )
        if cmd == "uname":
            return "Linux mock-router 5.10.0-mock #1 SMP armv7l GNU/Linux"
        if cmd == "pwd":
            return str(self.root)
        if cmd == "ls":
            target = self.root
            if args:
                target = _safe_path(self.root, args[-1])
            if not target.exists():
                return f"ls: {args[-1]}: No such file or directory"
            names = sorted(os.listdir(target))
            return "  ".join(names) if names else "(empty)"
        if cmd == "cat":
            if not args:
                return "cat: missing operand"
            path = _safe_path(self.root, args[0])
            if not path.is_file():
                return f"cat: can't open '{args[0]}': No such file or directory"
            try:
                return path.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError as exc:
                return f"cat: {exc}"
        if cmd == "md5sum":
            if not args:
                return "md5sum: missing operand"
            path = _safe_path(self.root, args[0])
            if not path.is_file():
                return f"md5sum: {args[0]}: No such file or directory"
            return f"{_md5(path)}  {args[0]}"
        if cmd == "echo":
            return " ".join(args)
        if cmd in ("reboot", "poweroff"):
            return "The system is going down NOW!"
        if cmd == "sleep":
            try:
                time.sleep(float(args[0]) if args else 1.0)
            except ValueError:
                return "sleep: invalid number"
            return ""
        if cmd == "tftp":
            return self._tftp(args)
        if cmd == "wget":
            return self._wget(args)
        if cmd == "ftpget":
            return self._ftpget(args)
        return f"sh: {cmd}: not found"

    def _tftp(self, args: list[str]) -> str:
        remote = ""
        host = ""
        local = ""
        index = 0
        while index < len(args):
            item = args[index]
            if item in ("-r", "--remote-file") and index + 1 < len(args):
                remote = args[index + 1]
                index += 2
            elif item in ("-l", "--local-file") and index + 1 < len(args):
                local = args[index + 1]
                index += 2
            elif item in ("-g", "-p", "--get", "--put"):
                index += 1
            else:
                host = item
                index += 1
        if not remote or not host:
            return "tftp: usage: tftp -g -r <file> <host> [-l <local>]"
        target_name = local or os.path.basename(remote)
        target = _safe_path(self.root, target_name)
        try:
            client = tftpy.TftpClient(host, 69)
            client.download(remote, str(target), timeout=15)
        except Exception as exc:  # noqa: BLE001
            return f"tftp: error: {exc}"
        return f"tftp: received {target.stat().st_size} bytes -> {target_name}"

    def _ftpget(self, args: list[str]) -> str:
        """busybox 风格: ftpget [-P port] [-u user] [-p pass] host [local] [remote]."""
        host = ""
        user = "anonymous"
        password = ""
        port = 21
        positional: list[str] = []
        index = 0
        while index < len(args):
            item = args[index]
            if item in ("-P", "--port") and index + 1 < len(args):
                try:
                    port = int(args[index + 1])
                except ValueError:
                    return "ftpget: invalid port"
                index += 2
            elif item in ("-u", "--username") and index + 1 < len(args):
                user = args[index + 1]
                index += 2
            elif item in ("-p", "--password") and index + 1 < len(args):
                password = args[index + 1]
                index += 2
            elif item in ("-c", "--continue"):
                index += 1
            else:
                positional.append(item)
                index += 1
        if not positional:
            return "ftpget: usage: ftpget [-P port] [-u user] [-p pass] host [local] [remote]"
        host = positional[0]
        local = positional[1] if len(positional) > 1 else ""
        remote = positional[2] if len(positional) > 2 else local
        if not remote:
            return "ftpget: missing remote file"
        local_name = local or os.path.basename(remote)
        target = _safe_path(self.root, local_name)
        try:
            with ftplib.FTP() as client:
                client.connect(host, port, timeout=15)
                client.login(user, password)
                with target.open("wb") as fh:
                    client.retrbinary(f"RETR {remote}", fh.write)
        except Exception as exc:  # noqa: BLE001
            return f"ftpget: error: {exc}"
        return f"ftpget: received {target.stat().st_size} bytes -> {local_name}"

    def _wget(self, args: list[str]) -> str:
        url = ""
        output = ""
        index = 0
        while index < len(args):
            item = args[index]
            if item in ("-O", "--output-document") and index + 1 < len(args):
                output = args[index + 1]
                index += 2
            elif item.startswith("http://") or item.startswith("https://"):
                url = item
                index += 1
            else:
                index += 1
        if not url:
            return "wget: missing URL"
        name = output or os.path.basename(urllib.parse.urlparse(url).path) or "index.html"
        target = _safe_path(self.root, name)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = resp.read()
            target.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            return f"wget: error: {exc}"
        return f"wget: saved {len(data)} bytes -> {name}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="假开发板(Telnet)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2323)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="root")
    parser.add_argument(
        "--root",
        default=str(Path(tempfile.gettempdir()) / "npa-mock-device"),
        help="设备文件系统根目录(默认临时目录)",
    )
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> None:
    commands = Commands(Path(args.root))

    async def shell(reader, writer) -> None:
        peer = writer.get_extra_info("peername")
        print(f"[mock-device] 连接: {peer}", flush=True)
        writer.write("login: ")
        user = (await reader.readline()).strip()
        writer.write("Password: ")
        password = (await reader.readline()).strip()
        if user != args.user or password != args.password:
            writer.write("\r\nLogin incorrect\r\n")
            writer.close()
            print(f"[mock-device] 登录失败: {user!r}", flush=True)
            return
        writer.write("\r\nWelcome to mock-router (busybox)\r\n")
        writer.write("# ")
        while True:
            line = await reader.readline()
            if not line:
                break
            command = line.strip()
            print(f"[mock-device] $ {command}", flush=True)
            if not command:
                writer.write("# ")
                continue
            if command == "exit" or command == "logout":
                writer.write("logout\r\n")
                break
            output = await asyncio.to_thread(commands.handle, command)
            if output is None:
                writer.write("logout\r\n")
                break
            if output:
                writer.write(output.replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n")
            writer.write("# ")
        writer.close()
        print("[mock-device] 会话结束", flush=True)

    server = await telnetlib3.create_server(
        host=args.host,
        port=args.port,
        shell=shell,
        encoding="utf-8",
        encoding_errors="replace",
        connect_maxwait=0.5,
    )
    print(
        f"[mock-device] 监听 telnet://{args.host}:{args.port} "
        f"user={args.user} password={args.password} root={commands.root}",
        flush=True,
    )
    await server.wait_closed()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

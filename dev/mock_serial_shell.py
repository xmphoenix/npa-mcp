"""假设备串口控制台(需要一对虚拟串口,例如 COM0COM 的 COM5 <-> COM6).

    python dev/mock_serial_shell.py --port COM6 --baud 115200

把 npa-mcp 配置页的 ``device.serial.port`` 指向另一端(如 COM5)。
默认账号 root / root,shell 提示符 ``# ``;会回显输入,模拟真实控制台。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

import serial


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Shell:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def run(self, line: str) -> str | None:
        parts = line.split()
        if not parts:
            return ""
        cmd = parts[0]
        if cmd in ("exit", "logout"):
            return None
        if cmd == "help":
            return "commands: help uname pwd ls cat md5sum echo reboot exit"
        if cmd == "uname":
            return "Linux mock-serial 5.10.0-mock #1 SMP armv7l GNU/Linux"
        if cmd == "pwd":
            return str(self.root)
        if cmd == "ls":
            return "  ".join(sorted(os.listdir(self.root))) or "(empty)"
        if cmd == "cat" and len(parts) > 1:
            path = self.root / parts[1]
            if not path.is_file():
                return f"cat: can't open '{parts[1]}'"
            return path.read_text(encoding="utf-8", errors="replace")[:4000]
        if cmd == "md5sum" and len(parts) > 1:
            path = self.root / parts[1]
            if not path.is_file():
                return f"md5sum: {parts[1]}: No such file or directory"
            return f"{md5_file(path)}  {parts[1]}"
        if cmd == "echo":
            return " ".join(parts[1:])
        if cmd == "reboot":
            return "The system is going down NOW!"
        return f"sh: {cmd}: not found"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="假设备串口控制台")
    parser.add_argument("--port", default="COM6")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="root")
    parser.add_argument("--root", default=str(Path.cwd() / "dev" / "_mockserial"))
    parser.add_argument("--no-login", action="store_true", help="跳过登录提示")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shell = Shell(Path(args.root))
    try:
        ser = serial.Serial(
            args.port,
            args.baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.1,
            write_timeout=1,
        )
    except serial.SerialException as exc:
        print(f"[mock-serial] 打开串口失败: {exc}", file=sys.stderr)
        return 2

    def send(text: str) -> None:
        ser.write(text.replace("\n", "\r\n").encode("utf-8", "replace"))
        ser.flush()

    print(f"[mock-serial] 已打开 {args.port} @ {args.baud};root={shell.root}", flush=True)
    try:
        if not args.no_login:
            send("login: ")
            login = ""
            while not login.endswith("\n"):
                login += ser.read(1).decode("utf-8", "replace")
            send("Password: ")
            password = ""
            while not password.endswith("\n"):
                password += ser.read(1).decode("utf-8", "replace")
            if login.strip() != args.user or password.strip() != args.password:
                send("\nLogin incorrect\n")
                return 1
            send("\nWelcome to mock-serial\n")
        send("# ")

        buffer = ""
        while True:
            raw = ser.read(ser.in_waiting or 1)
            if not raw:
                continue
            ser.write(raw)  # 回显
            ser.flush()
            buffer += raw.decode("utf-8", "replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                command = line.strip("\r\0")
                print(f"[mock-serial] $ {command}", flush=True)
                output = shell.run(command)
                if output is None:
                    send("logout\n")
                    return 0
                if output:
                    send(output + "\n")
                send("# ")
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            ser.close()
        except Exception:
            pass
        time.sleep(0.05)


if __name__ == "__main__":
    sys.exit(main())

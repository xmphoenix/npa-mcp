"""假电源管理设备(Telnet):4 路插座,支持 on/off/status.

    python dev/mock_telnet_pdu.py --port 2324

默认账号 admin / admin,提示符 ``PDU> ``。
输出包含 OK/ON/OFF,可直接匹配默认的 ``power.expect``。
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys


class Pdu:
    def __init__(self, outlets: int) -> None:
        self.outlets = {index: False for index in range(1, outlets + 1)}

    def handle(self, line: str) -> str | None:
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not argv:
            return ""
        if argv[0] in ("exit", "quit", "logout"):
            return None
        if argv[0] in ("help", "?"):
            return "commands: outlet <n> on|off|status | status | exit"
        if argv[0] == "status":
            return "\n".join(self._status(index) for index in sorted(self.outlets))
        if argv[0] != "outlet" or len(argv) < 2:
            return "ERROR: unknown command (try help)"
        try:
            index = int(argv[1])
        except ValueError:
            return "ERROR: outlet must be a number"
        if index not in self.outlets:
            return f"ERROR: outlet {index} not found"
        if len(argv) < 3:
            return self._status(index)
        action = argv[2].lower()
        if action == "on":
            self.outlets[index] = True
            return f"OK: outlet {index} is ON"
        if action == "off":
            self.outlets[index] = False
            return f"OK: outlet {index} is OFF"
        if action in ("status", "state"):
            return self._status(index)
        return f"ERROR: action {action!r} not supported"

    def _status(self, index: int) -> str:
        state = "ON" if self.outlets[index] else "OFF"
        return f"outlet {index}: {state}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="假电源管理设备(Telnet)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2324)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--outlets", type=int, default=4)
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> None:
    import telnetlib3

    pdu = Pdu(args.outlets)

    async def shell(reader, writer) -> None:
        print(f"[mock-pdu] 连接: {writer.get_extra_info('peername')}", flush=True)
        writer.write("Login: ")
        user = (await reader.readline()).strip()
        writer.write("Password: ")
        password = (await reader.readline()).strip()
        if user != args.user or password != args.password:
            writer.write("\r\nERROR: login failed\r\n")
            writer.close()
            return
        writer.write(f"\r\nPDU mock (SN: NPA-{args.outlets:02d})\r\nPDU> ")
        while True:
            line = await reader.readline()
            if not line:
                break
            command = line.strip()
            print(f"[mock-pdu] $ {command}", flush=True)
            if not command:
                writer.write("PDU> ")
                continue
            output = pdu.handle(command)
            if output is None:
                writer.write("bye\r\n")
                break
            if output:
                writer.write(output.replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n")
            writer.write("PDU> ")
        writer.close()

    server = await telnetlib3.create_server(
        host=args.host,
        port=args.port,
        shell=shell,
        encoding="utf-8",
        encoding_errors="replace",
        connect_maxwait=0.5,
    )
    print(
        f"[mock-pdu] 监听 telnet://{args.host}:{args.port} "
        f"user={args.user} password={args.password} outlets={args.outlets}",
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

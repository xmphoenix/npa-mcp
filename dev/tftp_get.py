"""最小 TFTP 客户端(Windows 自带没有 tftp.exe,用于冒烟测试).

    python dev/tftp_get.py 127.0.0.1 firmware.bin
    python dev/tftp_get.py 192.168.1.100 build/app.bin -o app.bin -p 69
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import tftpy


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TFTP 下载客户端")
    parser.add_argument("host", help="TFTP 服务器地址")
    parser.add_argument("filename", help="远端文件名(相对服务器根目录)")
    parser.add_argument("-o", "--output", default="", help="本地保存路径(默认同名)")
    parser.add_argument("-p", "--port", type=int, default=69, help="服务器端口(默认 69)")
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = Path(args.output or os.path.basename(args.filename))
    if output.parent and not output.parent.exists():
        output.parent.mkdir(parents=True, exist_ok=True)
    client = tftpy.TftpClient(args.host, args.port)
    try:
        client.download(args.filename, str(output), timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"TFTP 下载失败: {exc}", file=sys.stderr)
        return 1
    digest = hashlib.md5(output.read_bytes()).hexdigest()
    print(f"ok: {args.filename} -> {output} ({output.stat().st_size} bytes) md5={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

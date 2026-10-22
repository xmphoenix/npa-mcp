"""M3 冒烟:开发板设备路由 + TFTP/FTP/HTTP 文件服务 + 固件拉取 + 电源管理.

用临时 NPA_MCP_HOME 启动真实 Runtime,并在本机拉起两个模拟设备进程:

- ``dev/mock_telnet_device.py``(telnet 127.0.0.1:2323,root/root)
- ``dev/mock_telnet_pdu.py``(telnet 127.0.0.1:2324,admin/admin)

覆盖:

1. list_targets:TFTP / FTP / HTTP 文件服务与设备、电源配置状态;
2. device_exec:auto 路由、显式 kind、命令错误、提示符超时;
3. device_pull_firmware:tftp / wget / ftpget 三种方式 + MD5 校验;
4. 校验失败路径(设备端 MD5 与本地不一致);
5. HTTP 镜像服务 token 保护(403 / 200);
6. power_status / power_on / power_off / 非法插座,命令镜像到网页终端;
7. telnet 会话的终端页 WebSocket:init 帧、实时输入输出、ping/pong;
8. session_close(force=True) 释放底层连接。

串口本轮未接硬件:COM3 是 FTDI 适配器但无回环/无 shell,COM0COM 未安装,
串口登录/执行由 ``tests/test_serial_session.py`` 用假 serial 覆盖。

    .venv\\Scripts\\python.exe dev\\smoke_m3.py [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

PASSED = 0
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEV = PROJECT_ROOT / "dev"


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        raise AssertionError(f"{name} {detail}")


async def wait_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            await asyncio.sleep(0.1)
    return False


async def wait_ws_text(ws, needle: str, timeout: float) -> str:  # type: ignore[no-untyped-def]
    collected: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(
                f"WebSocket 超时未等到 {needle!r};已收到:{''.join(collected)[-500:]}"
            )
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            message = None
        text = message.get("text", "") if isinstance(message, dict) else str(raw)
        collected.append(text)
        if needle in "".join(collected):
            return "".join(collected)


def write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def start_mock(name: str, log_path: Path, *args: str) -> subprocess.Popen:
    handle = log_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(DEV / name), *args],
        cwd=str(PROJECT_ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    proc._npa_log = handle  # type: ignore[attr-defined]
    return proc


def stop_mock(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    handle = getattr(proc, "_npa_log", None)
    if handle is not None:
        handle.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8911, help="网页服务端口")
    parser.add_argument("--keep", action="store_true", help="保留临时目录,便于排查")
    args = parser.parse_args()

    home = Path(tempfile.mkdtemp(prefix="npa-m3-"))
    image_dir = home / "images"
    image_dir.mkdir()
    device_root = home / "device-root"
    device_root.mkdir()
    config_path = home / "config.json"
    os.environ["NPA_MCP_HOME"] = str(home)

    device_log = home / "mock-device.log"
    pdu_log = home / "mock-pdu.log"
    device_proc: subprocess.Popen | None = None
    pdu_proc: subprocess.Popen | None = None
    rt = None

    config = {
        "web": {"host": "127.0.0.1", "port": args.port},
        "paths": {"image_dir": str(image_dir), "report_dir": str(home / "reports")},
        "firmware_server": {
            "host": "127.0.0.1",
            "tftp": {"enabled": True, "port": 69, "bind": "127.0.0.1"},
            "ftp": {
                "enabled": True,
                "port": 2121,
                "bind": "127.0.0.1",
                "username": "npa",
                "password": "npa-pass",
                "passive_ports": "30000-30100",
            },
            "http": {"enabled": True, "token": "smoke-token"},
        },
        "device": {
            "prefer": "telnet",
            "ssh": {"enabled": False},
            "telnet": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 2323,
                "username": "root",
                "password": "root",
                "login_prompt": "login:",
                "password_prompt": "Password:",
                "shell_prompt": "# ",
                "echo_strip": True,
            },
            "serial": {"enabled": False},
        },
        "power": {
            "transport": "telnet",
            "host": "127.0.0.1",
            "port": 2324,
            "username": "admin",
            "password": "admin",
            "login_prompt": "Login:",
            "password_prompt": "Password:",
            "prompt": "PDU>",
            "outlet": 1,
            "timeout_sec": 10,
        },
        # 模拟设备的 md5sum 与 ls 不支持 ``&&`` 组合命令,冒烟只保留 md5sum。
        "firmware": {
            "device_pull_dir": ".",
            "verify": {"cmd": "md5sum {remote_path}"},
        },
    }
    write_config(config_path, config)

    print(f"M3 冒烟:临时 NPA_MCP_HOME = {home}")
    print(f"         设备文件根目录 = {device_root}")
    try:
        device_proc = start_mock(
            "mock_telnet_device.py",
            device_log,
            "--port",
            "2323",
            "--root",
            str(device_root),
        )
        pdu_proc = start_mock("mock_telnet_pdu.py", pdu_log, "--port", "2324")
        check("模拟开发板已监听", await wait_port("127.0.0.1", 2323))
        check("模拟电源设备已监听", await wait_port("127.0.0.1", 2324))

        from npa_mcp import server
        from npa_mcp.util import setup_logging

        setup_logging(home / "logs")
        rt = server.Runtime(str(config_path))
        server.RUNTIME = rt
        await rt.start(no_web=False)
        check("网页服务已启动", bool(rt.web_url), f"web_error={rt.web_error}")
        base_url = rt.web_url

        # -- 1. 文件服务与配置总览 --------------------------------------
        targets = await server.list_targets.fn()
        servers = {item["name"]: item for item in targets["fileservers"]["servers"]}
        check("TFTP 服务运行", servers.get("tftp", {}).get("running") is True, str(servers.get("tftp")))
        check("FTP 服务运行", servers.get("ftp", {}).get("running") is True, str(servers.get("ftp")))
        check("HTTP 服务运行", servers.get("http", {}).get("running") is True, str(servers.get("http")))
        check("设备 Telnet 已配置", targets["device"]["telnet"]["configured"] is True)
        check("电源设备已配置", targets["power"]["configured"] is True)

        # -- 2. device_exec ---------------------------------------------
        uname = await server.device_exec.fn("uname -a")
        check(
            "device_exec auto 成功",
            uname.get("ok") is True,
            json.dumps(uname, ensure_ascii=False)[:600],
        )
        check("auto 路由到 telnet", uname.get("kind") == "telnet", str(uname.get("kind")))
        check("输出含设备系统信息", "Linux mock-router" in uname.get("output", ""))
        check("返回终端地址", bool(uname.get("terminal_url")))

        explicit = await server.device_exec.fn("pwd", kind="telnet")
        check("device_exec 显式 kind=telnet", explicit.get("ok") is True)
        check("pwd 输出为设备目录", str(device_root) in explicit.get("output", ""))

        not_found = await server.device_exec.fn("definitely-not-a-command")
        check("未知命令仍等到提示符", not_found.get("ok") is True)
        check("未知命令输出 not found", "not found" in not_found.get("output", ""))

        bogus = await server.device_exec.fn("uname -a", kind="bogus")
        check(
            "非法 kind 返回中文错误",
            bogus.get("ok") is False and "未知设备连接方式" in bogus.get("error", ""),
            json.dumps(bogus, ensure_ascii=False),
        )

        # 设备会话应出现在会话列表并带有终端地址
        listed = await server.session_list.fn()
        telnet_sessions = [s for s in listed["sessions"] if s["kind"] == "telnet"]
        check("会话列表含 telnet 会话", bool(telnet_sessions), json.dumps(listed, ensure_ascii=False)[:400])
        session_id = telnet_sessions[0]["session_id"]

        # -- 3. 固件拉取:tftp / wget / ftpget ---------------------------
        payload = os.urandom(8192)
        firmware = image_dir / "firmware-test.bin"
        firmware.write_bytes(payload)
        local_md5 = hashlib.md5(payload).hexdigest()

        tftp_pull = await server.device_pull_firmware.fn(
            filename="firmware-test.bin", method="tftp"
        )
        check(
            "TFTP 拉取成功",
            tftp_pull.get("ok") is True,
            json.dumps(tftp_pull, ensure_ascii=False)[:900],
        )
        check("TFTP 拉取方式正确", tftp_pull.get("method") == "tftp")
        check("TFTP 拉取返回 local_md5", tftp_pull.get("local_md5") == local_md5)
        check("TFTP 拉取校验通过", tftp_pull.get("verified") is True)
        check("TFTP 远端 MD5 与本地一致", tftp_pull.get("remote_md5") == local_md5)
        check(
            "设备端文件大小正确",
            (device_root / "firmware-test.bin").stat().st_size == len(payload),
        )

        wget_pull = await server.device_pull_firmware.fn(
            filename="firmware-test.bin", method="wget", token="smoke-token"
        )
        check(
            "wget 拉取成功",
            wget_pull.get("ok") is True,
            json.dumps(wget_pull, ensure_ascii=False)[:900],
        )
        check("wget 拉取校验通过", wget_pull.get("verified") is True)

        ftp_pull = await server.device_pull_firmware.fn(
            filename="firmware-test.bin", method="ftpget"
        )
        check(
            "ftpget 拉取成功",
            ftp_pull.get("ok") is True,
            json.dumps(ftp_pull, ensure_ascii=False)[:900],
        )
        check("ftpget 拉取校验通过", ftp_pull.get("verified") is True)
        check(
            "ftpget 命令中的密码已掩码",
            "npa-pass" not in ftp_pull.get("pull_command", ""),
            str(ftp_pull.get("pull_command")),
        )

        # -- 4. 校验失败路径 --------------------------------------------
        bad_config = json.loads(config_path.read_text(encoding="utf-8"))
        bad_config["firmware"]["verify"]["cmd"] = "md5sum missing-check.bin"
        write_config(config_path, bad_config)
        bad_verify = await server.device_pull_firmware.fn(
            filename="firmware-test.bin", method="tftp"
        )
        check(
            "校验不一致时 ok=False",
            bad_verify.get("ok") is False,
            json.dumps(bad_verify, ensure_ascii=False)[:600],
        )
        check("校验不一致给出中文错误", bad_verify.get("error") == "固件校验失败")
        check("校验不一致 verified=False", bad_verify.get("verified") is False)
        config["firmware"]["verify"]["cmd"] = "md5sum {remote_path}"
        write_config(config_path, config)

        # -- 5. HTTP 镜像 token -----------------------------------------
        import httpx

        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            anonymous = await client.get("/images/firmware-test.bin")
            check("无 token 访问镜像被拒", anonymous.status_code == 403, str(anonymous.status_code))
            wrong = await client.get("/images/firmware-test.bin?token=wrong")
            check("错误 token 访问镜像被拒", wrong.status_code == 403, str(wrong.status_code))
            allowed = await client.get("/images/firmware-test.bin?token=smoke-token")
            check("正确 token 下载镜像成功", allowed.status_code == 200, str(allowed.status_code))
            check("HTTP 下载字节一致", allowed.content == payload)

        # -- 6. 电源管理 ------------------------------------------------
        status0 = await server.power_status.fn()
        check(
            "电源初始状态查询成功",
            status0.get("ok") is True,
            json.dumps(status0, ensure_ascii=False)[:600],
        )
        check("初始状态为 off", status0.get("state") == "off", str(status0.get("state")))

        on = await server.power_on.fn(1)
        check(
            "power_on 成功",
            on.get("ok") is True,
            json.dumps(on, ensure_ascii=False)[:600],
        )
        check("power_on 输出含 ON", "ON" in on.get("output", ""))

        status1 = await server.power_status.fn(1)
        check("通电后状态为 on", status1.get("state") == "on", str(status1.get("state")))

        off = await server.power_off.fn(1)
        check("power_off 成功", off.get("ok") is True, json.dumps(off, ensure_ascii=False)[:600])
        status2 = await server.power_status.fn(1)
        check("断电后状态为 off", status2.get("state") == "off", str(status2.get("state")))

        bad_outlet = await server.power_on.fn(9)
        check(
            "非法插座返回失败",
            bad_outlet.get("ok") is False and bad_outlet.get("determined") is True,
            json.dumps(bad_outlet, ensure_ascii=False)[:600],
        )
        check("非法插座给出中文错误", bool(bad_outlet.get("error")))

        listed = await server.session_list.fn()
        mirrors = [s for s in listed["sessions"] if s["label"] == "电源管理"]
        check("存在电源管理镜像终端", bool(mirrors), json.dumps(listed, ensure_ascii=False)[:400])
        power_text = rt.hub.tail(mirrors[0]["session_id"], 2000)  # type: ignore[union-attr]
        check("镜像终端记录了电源命令", "outlet=1" in power_text, power_text[-400:])
        check("镜像终端记录了设备回包", "OK: outlet 1 is ON" in power_text, power_text[-400:])

        # -- 7. 网页终端 WebSocket --------------------------------------
        reopened = await server.session_open.fn("telnet", "M3 交互会话")
        check("session_open 成功", reopened.get("ok") is True)
        check("session_open 复用会话", reopened.get("session_id") == session_id)

        import websockets

        ws_url = f"ws://127.0.0.1:{rt.web_port}/ws/session/{session_id}"
        async with websockets.connect(ws_url, open_timeout=10) as ws:
            init = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            check("WebSocket init 帧", init.get("type") == "init", str(init)[:200])
            check("init 带会话信息", init.get("session", {}).get("session_id") == session_id)
            check("init 带历史输出", bool(init.get("text")))

            await ws.send(json.dumps({"type": "input", "data": "uname -a\r\n"}))
            text = await wait_ws_text(ws, "Linux mock-router", timeout=20.0)
            check("WebSocket 收到实时输出", "Linux mock-router" in text)

            await ws.send(json.dumps({"type": "ping"}))
            pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            check("WebSocket ping/pong", pong.get("type") == "pong", str(pong))

        # -- 8. 提示符超时 ----------------------------------------------
        slow = await server.device_exec.fn("sleep 3", timeout=0.6)
        check(
            "慢命令超时返回失败",
            slow.get("ok") is False and slow.get("timed_out") is True,
            json.dumps(slow, ensure_ascii=False)[:600],
        )
        check("超时给出中文错误", bool(slow.get("error")))

        closed = await server.session_close.fn(session_id, force=True)
        check("session_close(force) 成功", closed.get("ok") is True, str(closed)[:400])
        check("底层连接已关闭", closed.get("transport_closed") is True, str(closed))

        print(f"\nM3 冒烟通过:{PASSED} 项检查")
        return 0
    finally:
        if rt is not None:
            await rt.shutdown()
        server.RUNTIME = None
        stop_mock(device_proc)
        stop_mock(pdu_proc)
        if not args.keep:
            # 文件日志 handler 还开着,Windows 下会删不掉 logs 目录
            logging.shutdown()
            shutil.rmtree(home, ignore_errors=True)
            if home.exists():
                print(f"临时目录未能完全删除(可能仍有文件句柄): {home}")
        else:
            print(f"保留临时目录: {home}")
            print(f"模拟设备日志: {device_log}")
            print(f"模拟电源日志: {pdu_log}")


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except AssertionError as exc:
        print(f"\nM3 冒烟失败: {exc}")
        sys.exit(1)

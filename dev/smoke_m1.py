"""M1 冒烟:无凭据指引 / 配置页保存热加载 / 密钥掩码.

用独立临时 NPA_MCP_HOME 启动真实的 Runtime(内含网页服务与文件服务),
通过 HTTP 模拟浏览器保存配置,再调用工具验证"无需重启即生效"。
不连接任何真实 SSH/设备,不打开浏览器。

运行::

    .venv\\Scripts\\python.exe dev\\smoke_m1.py
"""

from __future__ import annotations

import asyncio
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


async def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="npa-m1-"))
    os.environ["NPA_MCP_HOME"] = str(home)
    (home / "config.json").write_text(
        json.dumps(
            {
                "web": {"host": "127.0.0.1", "port": 8899},
                # 冒烟只验证 M1,不触发防火墙/端口占用:关闭 TFTP/FTP,保留 HTTP
                "firmware_server": {
                    "tftp": {"enabled": False},
                    "ftp": {"enabled": False},
                    "http": {"enabled": True},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    import httpx

    from npa_mcp import server
    from npa_mcp.util import setup_logging

    setup_logging(home / "logs")
    rt = server.Runtime(str(home / "config.json"))
    server.RUNTIME = rt

    print(f"M1 冒烟:临时 NPA_MCP_HOME = {home}")
    try:
        await rt.start(no_web=False)
        check("网页服务已启动", bool(rt.web_url), f"web_error={rt.web_error}")

        # -- 1. 无凭据时:list_targets 总览 + 中文警告 ---------------------
        targets = await server.list_targets.fn()
        check("list_targets 返回 ok", targets.get("ok") is True)
        check("代码服务器未配置", targets["code_server"]["configured"] is False)
        check(
            "给出中文配置警告",
            any("代码服务器未配置完整" in item for item in targets["warnings"]),
            str(targets.get("warnings")),
        )
        check("配置页地址正确", targets["web"]["url"] == f"{rt.web_url}/config")
        check("返回掩码后的路径不含密码字段", "password" not in json.dumps(targets))

        # -- 2. 无凭据时:工具调用返回中文指引 ----------------------------
        result = await server.ssh_exec.fn("uname -a")
        check("ssh_exec 未配置时失败", result.get("ok") is False)
        check(
            "错误为中文且指向配置页",
            "未配置" in result.get("error", "") and "配置页" in result.get("hint", ""),
            json.dumps(result, ensure_ascii=False),
        )

        async with httpx.AsyncClient(base_url=rt.web_url, timeout=10.0) as client:
            # -- 3. 配置页可访问 ------------------------------------------
            page = await client.get("/config")
            check("配置页 HTTP 200", page.status_code == 200, str(page.status_code))
            check("配置页为中文 HTML", "配置" in page.text and "text/html" in page.headers.get(
                "content-type", ""
            ))

            before = await client.get("/api/config")
            check("GET /api/config 正常", before.status_code == 200)
            check(
                "初始密码为空而不是 ***",
                before.json()["config"]["code_server"]["auth"]["password"] == "",
            )

            # -- 4. 模拟网页保存凭据(唯一写入口) -------------------------
            saved = await client.post(
                "/api/config",
                json={
                    "code_server": {
                        "host": "demo.example.com",
                        "port": 22,
                        "username": "dev",
                        "workdir": "/home/dev/project",
                        "auth": {"type": "password", "password": "smoke-pass"},
                    }
                },
            )
            body = saved.json()
            check("保存配置返回 200", saved.status_code == 200, json.dumps(body))
            check("提示无需重启", "无需重启" in body.get("message", ""))
            check("端口未变化", body.get("port_changed") is False)

            on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
            check(
                "配置已落盘且密码明文保存(v1 已知边界)",
                on_disk["code_server"]["auth"]["password"] == "smoke-pass",
            )

            after = await client.get("/api/config")
            check(
                "API 返回密码掩码 ***",
                after.json()["config"]["code_server"]["auth"]["password"] == "***",
            )

            # -- 5. 不重启进程,工具立即读到新配置 -------------------------
            hot = await server.list_targets.fn()
            check(
                "热加载:list_targets 立即看到代码服务器",
                hot["code_server"]["configured"] is True
                and hot["code_server"]["host"] == "demo.example.com",
                json.dumps(hot["code_server"], ensure_ascii=False),
            )
            check(
                "热加载:凭据摘要为 password",
                hot["code_server"]["auth"] == "password",
            )

            # -- 6. *** 哨兵不覆盖真实密码 --------------------------------
            kept = await client.post(
                "/api/config",
                json={
                    "code_server": {
                        "host": "demo2.example.com",
                        "auth": {"type": "password", "password": "***"},
                    }
                },
            )
            check("哨兵保存成功", kept.status_code == 200)
            on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
            check(
                "*** 保留原密码",
                on_disk["code_server"]["auth"]["password"] == "smoke-pass",
            )
            check(
                "其他字段已更新",
                on_disk["code_server"]["host"] == "demo2.example.com",
            )

            # -- 7. 非法配置返回中文校验错误 ------------------------------
            bad = await client.post(
                "/api/config", json={"work_windows": [{"name": "x", "days": []}]}
            )
            check("非法配置被拒绝", bad.status_code == 400, str(bad.status_code))
            check(
                "校验错误为中文",
                "配置校验失败" in bad.json().get("error", ""),
                json.dumps(bad.json(), ensure_ascii=False),
            )

        # -- 8. open_config_page(替身 open_url,避免弹浏览器) -------------
        opened: dict[str, str] = {}
        server.open_url = lambda url: bool(opened.setdefault("url", url))  # type: ignore[assignment]
        page_result = await server.open_config_page.fn()
        check("open_config_page 成功", page_result.get("ok") is True)
        check("已请求打开浏览器", page_result.get("opened") is True)
        check(
            "打开的地址是配置页",
            opened.get("url", "").endswith("/config")
            and page_result.get("url") == opened.get("url"),
        )

        print(f"\nM1 冒烟通过:{PASSED} 项检查")
        return 0
    finally:
        await rt.shutdown()
        server.RUNTIME = None


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except AssertionError:
        print("\nM1 冒烟失败")
        sys.exit(1)

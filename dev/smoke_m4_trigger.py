"""M4 R7:验证「任务计划 -> pythonw -> sched_run」这条真实触发链路.

做法:用与生产相同的 ``build_task_xml`` 生成任务,只把动作参数追加
``--config <临时配置> --dry-run``,触发时间设为 now+2 分钟。任务计划在
触发时刻启动 pythonw 运行 ``sched_run --dry-run``,后者只打印执行计划
就退出 0,不会启动真实 claude。脚本轮询 ``LastRunTime`` / ``LastTaskResult``
确认任务真的被触发并成功结束,然后删除任务与临时目录。

安全边界:
- 动作里带 ``--dry-run``,注册前断言 XML 中确实存在该参数;
- 即使 dry-run 意外丢失,job 也配置了非法 model + max_turns=1,真实
  claude 会立即失败而不会真的干活;
- 任务名固定 ``npa-mcp-m4fire``,清理时只删这一个名字。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PASSED = 0
TASK_NAME = "npa-mcp-m4fire"


def check(name: str, condition: bool, detail: object = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        raise AssertionError(f"{name} {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留临时目录与任务,便于排查")
    parser.add_argument("--wait", type=float, default=300.0, help="等待触发的秒数")
    args = parser.parse_args()

    from npa_mcp.config import ConfigManager
    from npa_mcp.scheduler import (
        build_task_xml,
        query_task,
        task_name_for,
        unregister_task,
    )
    from npa_mcp.util import run_command

    home = Path(tempfile.mkdtemp(prefix="npa-m4fire-"))
    config_path = home / "config.json"
    task_file = home / "task.md"
    task_file.write_text("M4 触发验证任务(不会真正执行)。\n", encoding="utf-8")

    now = datetime.now()
    trigger = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)
    if trigger.date() != now.date() or (trigger - now).total_seconds() < 60:
        print("已接近午夜或时间不足 60s,跳过触发验证。")
        shutil.rmtree(home, ignore_errors=True)
        return 0
    end = trigger + timedelta(minutes=30)

    config = {
        "paths": {
            "image_dir": str(home / "images"),
            "report_dir": str(home / "reports"),
            "log_dir": str(home / "logs"),
        },
        "firmware_server": {
            "tftp": {"enabled": False},
            "ftp": {"enabled": False},
            "http": {"enabled": False},
        },
        "work_windows": [
            {
                "name": "m4fire",
                "days": [trigger.weekday()],
                "start": trigger.strftime("%H:%M"),
                "end": end.strftime("%H:%M"),
                "job": {
                    "project_dir": str(home),
                    "task_file": str(task_file),
                    # 安全网:dry-run 若意外丢失,非法 model + 1 轮快速失败
                    "model": "npa-m4-trigger-guard",
                    "max_turns": 1,
                    "allowed_tools": "Read",
                    "resume_session_id": "",
                },
            }
        ],
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    (home / "images").mkdir()

    print(f"R7 触发验证:临时目录 {home}")
    print(f"触发时间 {trigger:%Y-%m-%d %H:%M:%S}(当前 {now:%H:%M:%S})")

    manager = ConfigManager(str(config_path))
    cfg = manager.load(force=True)
    window = cfg.work_windows[0]
    task_name = task_name_for(window, 0)
    check("任务名为约定的 ASCII 测试名", task_name == TASK_NAME, task_name)

    existing = query_task(task_name)
    if existing.get("registered"):
        print(f"{task_name} 已存在,为避免误删他人任务,终止。")
        shutil.rmtree(home, ignore_errors=True)
        return 1

    xml = build_task_xml(window, task_name)
    new_args = subprocess.list2cmdline(
        [
            "-m",
            "npa_mcp.sched_run",
            "--window",
            window.name,
            "--config",
            str(config_path),
            "--dry-run",
        ]
    )
    xml, count = re.subn(
        r"<Arguments>.*?</Arguments>",
        lambda _m: f"<Arguments>{escape(new_args)}</Arguments>",
        xml,
        count=1,
        flags=re.S,
    )
    check("动作参数替换成功", count == 1, count)
    check("动作包含 --dry-run 安全开关", "--dry-run" in xml)
    check("动作携带临时配置路径", str(config_path) in xml)
    check(
        "触发器锚定在 now+2 分钟",
        trigger.strftime("%Y-%m-%dT%H:%M:%S") in xml,
        trigger,
    )

    xml_path = home / f"task_{task_name}.xml"
    xml_path.write_bytes(xml.encode("utf-16"))

    before: dict = {}
    try:
        result = run_command(
            ["schtasks", "/create", "/tn", task_name, "/xml", str(xml_path), "/f"],
            timeout=60,
        )
        check(
            "schtasks 注册成功",
            result.returncode == 0,
            (result.stdout or result.stderr).strip()[:300],
        )

        before = query_task(task_name)
        check("注册后可查询", before.get("registered") is True, before)

        deadline = time.time() + args.wait
        after = before
        while time.time() < deadline:
            time.sleep(10)
            after = query_task(task_name)
            if after.get("registered") and after.get("last_result") == "0":
                break
        print(f"  查询结果: {json.dumps(after, ensure_ascii=False)}")

        check("任务已被触发(LastRunTime 变化)", after.get("last_run_time") != before.get("last_run_time"))
        check("任务成功结束(LastTaskResult=0)", after.get("last_result") == "0", after.get("last_result"))
        check("任务已回到 Ready 状态", after.get("state") == "Ready", after.get("state"))
    finally:
        if not args.keep:
            removed = unregister_task(task_name)
            print(f"  删除任务: ok={removed.get('ok')}")
            check("任务已删除", query_task(task_name).get("registered") is False)
            shutil.rmtree(home, ignore_errors=True)
        else:
            print(f"  --keep:任务与临时目录保留({home})")

    print(f"\nR7 触发验证通过:{PASSED} 项检查")
    return 0


if __name__ == "__main__":
    sys.exit(main())

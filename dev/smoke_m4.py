"""M4 冒烟:任务计划注册/查询/删除 + sched_run 窗口壳 + HTML 报告.

真实 Claude 不会被调用(避免消耗 token):

- ``sched_run --dry-run`` 用真实 claude 路径**只构造并打印**命令行;
- 其余运行用 ``dev/fake_claude.cmd`` 冒充 claude CLI,记录 argv / stdin /
  cwd / pid,并可设置睡眠与退出码。

覆盖:

1. work_window_check:活动窗口、剩余秒数、任务名、窗口外判定;
2. schedule_register:三个窗口一次注册,schtasks 返回成功;
3. schedule_status:注册状态、next_start、last_run 合并;
4. sched_run --dry-run:``-p`` / ``--output-format json`` /
   ``--max-turns`` / 显式 ``--allowedTools``,且绝不出现
   ``--dangerously-skip-permissions``;
5. fake 第一轮:退出码 0、提示词注入、无 ``--resume``、last_session 写入;
6. fake 第二轮:自动携带 ``--resume <上一轮 session_id>``;
7. 窗口外补跑:退出码 2、last_run 记录 ``outside_window``、不启动 claude;
8. 窗口结束强杀:``taskkill /T /F``、last_run ``killed_by_window``、日志;
9. generate_report:零外链 HTML、网页可访问、/reports 列表可见;
10. schedule_remove:按窗口名与任务名各删一次,删除后查询不到。

    .venv\\Scripts\\python.exe dev\\smoke_m4.py [--keep] [--port 8912]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

PASSED = 0
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEV = PROJECT_ROOT / "dev"
TASKS = ["npa-mcp-m4dry", "npa-mcp-m4kill", "npa-mcp-m4outside"]


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        raise AssertionError(f"{name} {detail}")


def write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def run_sched(
    window: str,
    config_path: Path,
    extra_env: dict[str, str] | None = None,
    timeout: float = 120.0,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["NPA_MCP_HOME"] = str(config_path.parent)
    env.pop("NPA_CLAUDE_CMD", None)
    for key, value in (extra_env or {}).items():
        if value == "":
            env.pop(key, None)
        else:
            env[key] = value
    argv = [
        sys.executable,
        "-m",
        "npa_mcp.sched_run",
        "--window",
        window,
        "--config",
        str(config_path),
    ]
    if dry_run:
        argv.append("--dry-run")
    proc = subprocess.Popen(
        argv,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 连进程树一起杀掉,避免留下真实 claude 子进程
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            proc.kill()
        proc.communicate(timeout=30)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return str(pid) in (result.stdout or "")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8912, help="网页服务端口")
    parser.add_argument("--keep", action="store_true", help="保留临时目录,便于排查")
    args = parser.parse_args()

    home = Path(tempfile.mkdtemp(prefix="npa-m4-"))
    config_path = home / "config.json"
    runs = home / "logs" / "runs"
    os.environ["NPA_MCP_HOME"] = str(home)

    now = datetime.now()
    today = now.weekday()
    tomorrow = (today + 1) % 7
    task_file = home / "task.md"
    task_file.write_text(
        "M4 冒烟任务:只做平凡操作,不要修改任何代码,完成后生成报告。\n",
        encoding="utf-8",
    )

    def window(name: str, days: list[int], start: str, end: str) -> dict:
        return {
            "name": name,
            "days": days,
            "start": start,
            "end": end,
            "job": {
                "project_dir": str(home),
                "task_file": str(task_file),
                "resume_session_id": "",
                "model": "",
                "max_turns": 5,
                "allowed_tools": "mcp__npa_mcp__*,Bash,Read",
            },
        }

    active_windows = [
        window("m4dry", [today], "00:00", "23:59"),
        window("m4kill", [today], "00:00", "23:59"),
        window("m4outside", [tomorrow], "00:00", "23:59"),
    ]
    config = {
        "web": {"host": "127.0.0.1", "port": args.port},
        "paths": {
            "image_dir": str(home / "images"),
            "report_dir": str(home / "reports"),
            "log_dir": str(home / "logs"),
        },
        # 本轮不测文件服务,关掉以免占用 69/2121 与上一里程碑冲突
        "firmware_server": {
            "tftp": {"enabled": False},
            "ftp": {"enabled": False},
            "http": {"enabled": False},
        },
        "work_windows": active_windows,
    }
    (home / "images").mkdir()
    write_config(config_path, config)

    print(f"M4 冒烟:临时 NPA_MCP_HOME = {home}")
    rt = None
    server = None
    fake_pids: list[int] = []
    try:
        from npa_mcp import server
        from npa_mcp.util import setup_logging

        setup_logging(home / "logs")
        rt = server.Runtime(str(config_path))
        server.RUNTIME = rt
        await rt.start(no_web=False)
        check("网页服务已启动", bool(rt.web_url), f"web_error={rt.web_error}")
        base_url = rt.web_url

        # -- 1. work_window_check --------------------------------------
        wc = await server.work_window_check.fn()
        check("work_window_check 成功", wc.get("ok") is True, json.dumps(wc, ensure_ascii=False)[:500])
        check(
            "当前活动窗口为 m4dry/m4kill",
            {"m4dry", "m4kill"} <= set(wc.get("active_windows", [])),
            str(wc.get("active_windows")),
        )
        dry_item = next(w for w in wc["windows"] if w["name"] == "m4dry")
        check("活动窗口剩余秒数 > 0", dry_item["remaining_sec"] > 0, str(dry_item["remaining_sec"]))
        check("活动窗口 active_end 非空", bool(dry_item["active_end"]))
        check("任务名生成正确", dry_item["task_name"] == "npa-mcp-m4dry", str(dry_item["task_name"]))
        outside_item = next(w for w in wc["windows"] if w["name"] == "m4outside")
        check("明天窗口当前不活动", outside_item["active_now"] is False)

        # -- 2. dry-run:真实 claude 路径,只验证命令行构造 ----------------
        dry = run_sched("m4dry", config_path, timeout=60, dry_run=True)
        check(
            "dry-run 退出码 0",
            dry.returncode == 0,
            f"rc={dry.returncode} out={dry.stdout[-500:]} err={dry.stderr[-500:]}",
        )
        plan = json.loads(dry.stdout)
        check("dry-run 返回计划", plan.get("dry_run") is True and plan.get("ok") is True)
        command = plan.get("command", "")
        padded = f" {command} "
        check("命令含 -p", " -p " in padded, command)
        check("命令含 --output-format json", "--output-format json" in command, command)
        check("命令含 --max-turns 5", "--max-turns 5" in command, command)
        check(
            "命令含显式 --allowedTools",
            "--allowedTools mcp__npa_mcp__*,Bash,Read" in command,
            command,
        )
        check("命令不含 --resume", "--resume" not in command, command)
        check("命令绝不含 dangerously", "dangerously" not in command, command)
        check("首轮续做 id 为空", plan.get("resume_session_id") == "", str(plan.get("resume_session_id")))
        check("任务文件存在", Path(plan.get("prompt_file", "")).exists(), str(plan.get("prompt_file")))
        check(
            "工作目录为 project_dir",
            Path(plan.get("cwd", "")).resolve() == home.resolve(),
            str(plan.get("cwd")),
        )

        # -- 3. 注册任务计划 --------------------------------------------
        # 触发时间放到未来,避免 StartWhenAvailable 在注册后立刻补跑真实任务。
        # 留足 1 小时余量:即使冒烟意外中断,触发时用的也是默认配置,
        # 找不到同名窗口会立刻退出,不会启动真实 Claude。
        fut = datetime.now() + timedelta(minutes=60)
        fut_end = fut + timedelta(minutes=15)
        config["work_windows"] = [
            window("m4dry", [fut.weekday()], fut.strftime("%H:%M"), fut_end.strftime("%H:%M")),
            window("m4kill", [fut.weekday()], fut.strftime("%H:%M"), fut_end.strftime("%H:%M")),
            window("m4outside", [tomorrow], "00:00", "23:59"),
        ]
        write_config(config_path, config)
        rt.manager.load(force=True)

        registered = await server.schedule_register.fn()
        detail = json.dumps(registered, ensure_ascii=False)[:1500]
        check("注册全部任务成功", registered.get("ok") is True, detail)
        names = {t.get("task_name") for t in registered.get("tasks", [])}
        check("注册了三个任务", names == set(TASKS), detail)

        # -- 4. schedule_status ----------------------------------------
        status = await server.schedule_status.fn()
        check("schedule_status 成功", status.get("ok") is True, json.dumps(status, ensure_ascii=False)[:800])
        by_name = {item["task_name"]: item for item in status.get("tasks", [])}
        check("三个任务均显示已注册", all(by_name.get(n, {}).get("registered") for n in TASKS), str(by_name))
        check(
            "每个任务都有 next_start",
            all(by_name.get(n, {}).get("next_start") for n in TASKS),
            str(by_name),
        )
        check(
            "runs_dir 位于临时目录",
            Path(status.get("runs_dir", "")).resolve() == runs.resolve(),
            str(status.get("runs_dir")),
        )

        # 恢复活动窗口,供 sched_run 手工执行
        config["work_windows"] = active_windows
        write_config(config_path, config)
        rt.manager.load(force=True)

        # -- 5. fake 第一轮 ---------------------------------------------
        fake_cli = DEV / "fake_claude.cmd"
        check("假 claude 包装存在", fake_cli.exists(), str(fake_cli))
        fake1 = home / "fake1.json"
        env1 = {
            "NPA_CLAUDE_CMD": str(fake_cli),
            "NPA_PYTHON": sys.executable,
            "NPA_FAKE_RECORD": str(fake1),
            "NPA_FAKE_SESSION": "m4-sid-1",
            "NPA_FAKE_RESULT": "第一轮模拟完成",
            "NPA_FAKE_EXIT": "0",
        }
        run1 = run_sched("m4dry", config_path, env1, timeout=120)
        check(
            "fake 第一轮退出码 0",
            run1.returncode == 0,
            f"rc={run1.returncode} out={run1.stdout[-800:]} err={run1.stderr[-400:]}",
        )
        rec1 = read_json(fake1)
        prompt1 = rec1.get("stdin", "")
        check("fake 收到窗口提示词", "自主工作窗口 [m4dry]" in prompt1, prompt1[:300])
        check("提示词包含任务文件内容", "M4 冒烟任务" in prompt1)
        argv1 = rec1.get("argv", [])
        check("fake argv 含 -p", "-p" in argv1, str(argv1))
        check(
            "fake argv 含 --output-format json",
            "--output-format" in argv1 and "json" in argv1,
            str(argv1),
        )
        check("fake argv 含 --max-turns", "--max-turns" in argv1, str(argv1))
        check("fake argv 含显式 --allowedTools", "--allowedTools" in argv1, str(argv1))
        check("fake argv 无 --resume", "--resume" not in argv1, str(argv1))
        check(
            "fake argv 无 dangerously",
            not any("dangerously" in a for a in argv1),
            str(argv1),
        )
        check(
            "fake 工作目录为 project_dir",
            Path(rec1.get("cwd", "")).resolve() == home.resolve(),
            str(rec1.get("cwd")),
        )
        check("子进程继承窗口环境变量", rec1.get("window_env") == "m4dry", str(rec1.get("window_env")))
        fake_pids.append(int(rec1.get("pid") or 0))

        last_session = read_json(runs / "last_session.json")
        check(
            "last_session 写入 m4dry",
            last_session.get("sessions", {}).get("m4dry") == "m4-sid-1",
            json.dumps(last_session, ensure_ascii=False),
        )
        last_run = read_json(runs / "last_run.json")
        check("last_run ok=True", last_run.get("ok") is True, json.dumps(last_run, ensure_ascii=False)[:800])
        check("last_run 记录 session_id", last_run.get("session_id") == "m4-sid-1")
        check("last_run 记录 num_turns", last_run.get("num_turns") == 1)
        check(
            "last_run 带结果预览",
            "第一轮模拟完成" in str(last_run.get("result_preview", "")),
            str(last_run.get("result_preview")),
        )
        check("last_run 日志文件存在", Path(last_run.get("log_path", "")).exists())

        # -- 6. fake 第二轮:续做 ---------------------------------------
        fake2 = home / "fake2.json"
        env2 = dict(env1)
        env2.update(
            {
                "NPA_FAKE_RECORD": str(fake2),
                "NPA_FAKE_SESSION": "m4-sid-2",
                "NPA_FAKE_RESULT": "第二轮模拟完成",
            }
        )
        run2 = run_sched("m4dry", config_path, env2, timeout=120)
        check(
            "fake 第二轮退出码 0",
            run2.returncode == 0,
            f"rc={run2.returncode} out={run2.stdout[-800:]} err={run2.stderr[-400:]}",
        )
        rec2 = read_json(fake2)
        argv2 = rec2.get("argv", [])
        check("第二轮 argv 含 --resume", "--resume" in argv2, str(argv2))
        if "--resume" in argv2:
            index = argv2.index("--resume")
            check(
                "第二轮续做上一会话",
                index + 1 < len(argv2) and argv2[index + 1] == "m4-sid-1",
                str(argv2),
            )
        fake_pids.append(int(rec2.get("pid") or 0))
        last_session = read_json(runs / "last_session.json")
        check(
            "last_session 更新为第二轮",
            last_session.get("sessions", {}).get("m4dry") == "m4-sid-2",
            json.dumps(last_session, ensure_ascii=False),
        )
        last_run = read_json(runs / "last_run.json")
        check(
            "last_run 记录续做来源",
            last_run.get("resume_session_id") == "m4-sid-1",
            str(last_run.get("resume_session_id")),
        )
        check("last_run 记录第二轮结果", "第二轮模拟完成" in str(last_run.get("result_preview", "")))

        status2 = await server.schedule_status.fn()
        item = next(i for i in status2["tasks"] if i["window"] == "m4dry")
        check(
            "schedule_status 合并最近运行",
            item.get("last_run", {}).get("session_id") == "m4-sid-2",
            json.dumps(item.get("last_run", {}), ensure_ascii=False)[:500],
        )

        # -- 7. 窗口外补跑 ----------------------------------------------
        fake_outside = home / "fake-outside.json"
        env_out = dict(env1)
        env_out.update(
            {
                "NPA_FAKE_RECORD": str(fake_outside),
                "NPA_FAKE_SESSION": "should-not-run",
            }
        )
        outside = run_sched("m4outside", config_path, env_out, timeout=60)
        check(
            "窗口外退出码 2",
            outside.returncode == 2,
            f"rc={outside.returncode} out={outside.stdout[-500:]} err={outside.stderr[-400:]}",
        )
        check("窗口外未启动 claude", not fake_outside.exists(), str(fake_outside))
        last_run = read_json(runs / "last_run.json")
        check(
            "窗口外写入 skipped 原因",
            last_run.get("skipped") == "outside_window",
            json.dumps(last_run, ensure_ascii=False)[:600],
        )

        # -- 8. 窗口结束强杀 --------------------------------------------
        kill_now = datetime.now()
        end_dt = kill_now + timedelta(minutes=2 if kill_now.second >= 45 else 1)
        end_hhmm = end_dt.strftime("%H:%M")
        if end_dt.date() != kill_now.date():
            end_hhmm = "23:59"
        for item in config["work_windows"]:
            if item["name"] == "m4kill":
                item["start"] = "00:00"
                item["end"] = end_hhmm
                item["days"] = [today]
        write_config(config_path, config)
        rt.manager.load(force=True)

        wc2 = await server.work_window_check.fn()
        kill_item = next(w for w in wc2["windows"] if w["name"] == "m4kill")
        check("强杀测试窗口当前活动", kill_item["active_now"] is True, json.dumps(kill_item, ensure_ascii=False))

        fake_kill = home / "fake-kill.json"
        env_kill = dict(env1)
        env_kill.update(
            {
                "NPA_FAKE_RECORD": str(fake_kill),
                "NPA_FAKE_SESSION": "m4-killed",
                "NPA_FAKE_RESULT": "不应正常完成",
                "NPA_FAKE_SLEEP": "300",
            }
        )
        started = time.monotonic()
        killed_run = run_sched("m4kill", config_path, env_kill, timeout=240)
        elapsed = time.monotonic() - started
        check(
            "强杀运行返回 1",
            killed_run.returncode == 1,
            f"rc={killed_run.returncode} elapsed={elapsed:.1f}s "
            f"out={killed_run.stdout[-800:]} err={killed_run.stderr[-400:]}",
        )
        last_run = read_json(runs / "last_run.json")
        check(
            "last_run 标记 killed_by_window",
            last_run.get("killed_by_window") is True,
            json.dumps(last_run, ensure_ascii=False)[:800],
        )
        check("last_run ok=False", last_run.get("ok") is False)
        log_text = Path(last_run.get("log_path", "")).read_text(encoding="utf-8", errors="replace")
        check("日志记录窗口结束强杀", "工作窗口结束" in log_text, log_text[-800:])
        rec_kill = read_json(fake_kill)
        kill_pid = int(rec_kill.get("pid") or 0)
        fake_pids.append(kill_pid)
        check("fake 进程已记录 pid", kill_pid > 0, str(rec_kill)[:300])
        check("被强杀的 fake 进程已退出", process_alive(kill_pid) is False, f"pid={kill_pid} 仍在运行")

        # -- 9. generate_report ----------------------------------------
        report = await server.generate_report.fn(
            title="M4 冒烟报告",
            summary="任务计划注册/查询/删除、窗口壳与报告生成验证。",
            changes=[
                {
                    "category": "调度",
                    "description": "注册三个工作窗口并在窗口内启动假 claude",
                    "file": "src/npa_mcp/scheduler.py",
                }
            ],
            issues=[
                {
                    "severity": "low",
                    "description": "冒烟使用假 claude,真实 CLI 仅做 dry-run",
                    "status": "已覆盖",
                }
            ],
            next_steps=["接入真实开发板后补测硬件路径"],
            status="success",
            project="npa-mcp",
        )
        check("报告生成成功", report.get("ok") is True, json.dumps(report, ensure_ascii=False))
        report_path = Path(report.get("path", ""))
        check("报告文件存在", report_path.exists(), str(report_path))
        html = report_path.read_text(encoding="utf-8", errors="replace")
        check("报告含标题", "M4 冒烟报告" in html)
        check("报告零 http 外链", "http://" not in html and "https://" not in html)
        check("报告无脚本", "<script" not in html.lower())
        check("报告无 @import", "@import" not in html)
        check(
            "报告 URL 指向网页端口",
            f":{rt.web_port}/reports/" in report.get("url", ""),
            str(report.get("url")),
        )

        import httpx

        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            fetched = await client.get(f"/reports/{quote(report.get('filename', ''))}")
            check("报告网页可访问", fetched.status_code == 200, str(fetched.status_code))
            check("网页内容与文件一致", fetched.text == html)
            listing = await client.get("/reports")
            check(
                "报告列表包含新报告",
                report.get("filename", "") in listing.text,
                listing.text[:400],
            )

        # -- 10. 删除任务计划 ------------------------------------------
        removed_window = await server.schedule_remove.fn(window="m4dry")
        check(
            "按窗口名删除成功",
            removed_window.get("ok") is True,
            json.dumps(removed_window, ensure_ascii=False),
        )
        removed_name = await server.schedule_remove.fn(task_name="npa-mcp-m4kill")
        check(
            "按任务名删除成功",
            removed_name.get("ok") is True,
            json.dumps(removed_name, ensure_ascii=False),
        )
        removed_outside = await server.schedule_remove.fn(window="m4outside")
        check(
            "删除第三个任务成功",
            removed_outside.get("ok") is True,
            json.dumps(removed_outside, ensure_ascii=False),
        )

        from npa_mcp.scheduler import query_task

        check("删除后查询不到 m4dry", query_task("npa-mcp-m4dry").get("registered") is False)
        final = await server.schedule_status.fn()
        check(
            "最终状态无已注册任务",
            all(not item.get("registered") for item in final.get("tasks", [])),
            json.dumps(final.get("tasks"), ensure_ascii=False)[:800],
        )

        print(f"\nM4 冒烟通过:{PASSED} 项检查")
        return 0
    finally:
        if rt is not None:
            await rt.shutdown()
        if server is not None:
            server.RUNTIME = None
        try:
            from npa_mcp.scheduler import unregister_task

            for task_name in TASKS:
                try:
                    unregister_task(task_name)
                except Exception:
                    pass
        except Exception:
            pass
        for pid in fake_pids:
            if process_alive(pid):
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=30,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
        if not args.keep:
            # 文件日志 handler 还开着,Windows 下会删不掉 logs 目录
            logging.shutdown()
            shutil.rmtree(home, ignore_errors=True)
            if home.exists():
                print(f"临时目录未能完全删除(可能仍有文件句柄): {home}")
        else:
            print(f"保留临时目录: {home}")


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except AssertionError as exc:
        print(f"\nM4 冒烟失败: {exc}")
        sys.exit(1)

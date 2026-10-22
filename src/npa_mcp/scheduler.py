"""Windows 任务计划集成.

每个工作窗口对应一个 ASCII 命名的任务 ``npa-mcp-<window>``,由
``schtasks /create /xml`` 注册。任务只负责在窗口起点启动
``sched_run.py``;是否真的开工、何时收工由 wrapper 自己判断。

这样即使在窗口外被手动启动(或 StartWhenAvailable 补跑),也不会
在白天消耗 token。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .config import Config, ConfigManager, WorkWindow
from .util import atomic_write_text, ensure_dir, get_logger, iso_now, run_command, safe_slug

log = get_logger(__name__)

TASK_PREFIX = "npa-mcp-"
TASK_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
_DAYS_XML = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class ScheduleError(RuntimeError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


def runs_dir(cfg: Config) -> Path:
    path = cfg.paths.log_path() / "runs"
    ensure_dir(path)
    return path


def task_name_for(window: WorkWindow, index: int) -> str:
    slug = safe_slug(window.name, f"window{index + 1}")
    name = f"{TASK_PREFIX}{slug}"
    if not TASK_NAME_RE.match(name):
        name = f"{TASK_PREFIX}window{index + 1}"
    return name[:120]


def _python_command() -> str:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if os.name == "nt" and pythonw.exists():
        return str(pythonw)
    return str(exe)


def build_task_xml(window: WorkWindow, task_name: str) -> str:
    """生成 Task Scheduler 1.4 XML(UTF-16 写盘前的内容)."""
    start = window.start_time()
    anchor = datetime.combine(date.today(), start)
    days = "".join(f"<{_DAYS_XML[d]} />" for d in sorted(set(window.days)))
    # 用 -m 而不是脚本路径:保证包内相对导入可用(site-packages 安装时也一样)
    arguments = subprocess.list2cmdline(["-m", "npa_mcp.sched_run", "--window", window.name])
    working_dir = str(Path.home())
    description = (
        f"npa-mcp 自主工作窗口 {window.name} "
        f"({window.start}-{window.end});仅在窗口内运行"
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>npa-mcp</Author>
    <Description>{escape(description)}</Description>
    <URI>\\{task_name}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{anchor.strftime('%Y-%m-%dT%H:%M:%S')}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>{days}</DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(_python_command())}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def register_window(manager: ConfigManager, window: WorkWindow, index: int) -> dict[str, Any]:
    cfg = manager.load()
    task_name = task_name_for(window, index)
    xml = build_task_xml(window, task_name)
    xml_path = runs_dir(cfg) / f"task_{task_name}.xml"
    # schtasks /xml 需要 Unicode(UTF-16) 文档
    xml_path.write_bytes(xml.encode("utf-16"))
    result = run_command(
        ["schtasks", "/create", "/tn", task_name, "/xml", str(xml_path), "/f"],
        timeout=60,
    )
    ok = result.returncode == 0
    info = {
        "window": window.name,
        "task_name": task_name,
        "ok": ok,
        "xml_path": str(xml_path),
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "exit_code": result.returncode,
    }
    if not ok:
        info["error"] = (
            "注册任务计划失败。请确认当前用户有权限创建任务,且任务名未被其他程序占用。"
        )
    return info


def unregister_task(task_name: str) -> dict[str, Any]:
    if not TASK_NAME_RE.match(task_name):
        raise ScheduleError(f"非法任务名: {task_name}", "任务名只能包含字母、数字、下划线和短横线。")
    result = run_command(["schtasks", "/delete", "/tn", task_name, "/f"], timeout=60)
    return {
        "task_name": task_name,
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "exit_code": result.returncode,
    }


def _powershell(script: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return run_command(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        timeout=timeout,
    )


def query_task(task_name: str) -> dict[str, Any]:
    """返回注册状态与最近/下次运行时间(不依赖系统语言)."""
    if not TASK_NAME_RE.match(task_name):
        return {"registered": False, "error": "非法任务名"}
    quoted = task_name.replace("'", "''")
    script = (
        f"$t = Get-ScheduledTask -TaskName '{quoted}' -ErrorAction SilentlyContinue;"
        "if ($null -eq $t) { Write-Output 'NPA_NOT_FOUND'; exit 0 };"
        f"$i = Get-ScheduledTaskInfo -TaskName '{quoted}' -ErrorAction SilentlyContinue;"
        "Write-Output ('NPA_TASK|' + [string]$t.State + '|' + "
        "[string]$i.LastRunTime + '|' + [string]$i.LastTaskResult + '|' + "
        "[string]$i.NextRunTime + '|' + [string]$i.NumberOfMissedRuns)"
    )
    result = _powershell(script)
    stdout = (result.stdout or "").strip()
    if "NPA_NOT_FOUND" in stdout:
        return {"registered": False}
    if "NPA_TASK|" not in stdout:
        return {
            "registered": False,
            "error": (result.stderr or stdout or "查询任务计划失败").strip()[:400],
        }
    try:
        line = next(line for line in stdout.splitlines() if line.startswith("NPA_TASK|"))
        _, state, last_run, last_result, next_run, missed = line.split("|", 5)
        return {
            "registered": True,
            "state": state,
            "last_run_time": last_run,
            "last_result": last_result,
            "next_run_time": next_run,
            "missed_runs": int(missed or 0),
        }
    except (ValueError, StopIteration):
        return {"registered": False, "error": f"无法解析任务查询结果: {stdout[:300]}"}


def list_npa_tasks() -> list[dict[str, Any]]:
    # Windows PowerShell 5.1 的 ConvertTo-Json 没有 -AsArray
    script = (
        "$tasks = @(Get-ScheduledTask -TaskName 'npa-mcp-*' -ErrorAction SilentlyContinue | "
        "Select-Object -Property TaskName,State);"
        "if ($tasks.Count -eq 0) { Write-Output '[]' } "
        "else { $tasks | ConvertTo-Json -Compress }"
    )
    result = _powershell(script)
    stdout = (result.stdout or "").strip()
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [item for item in data if isinstance(item, dict)]


def read_last_run(cfg: Config) -> dict[str, Any]:
    path = runs_dir(cfg) / "last_run.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_last_session(cfg: Config) -> dict[str, Any]:
    path = runs_dir(cfg) / "last_session.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if not isinstance(data.get("sessions"), dict):
        # 兼容旧格式:单条 session_id + window
        data["sessions"] = (
            {str(data.get("window") or ""): str(data["session_id"])}
            if data.get("session_id")
            else {}
        )
    return data


def write_last_session(cfg: Config, session_id: str, window: str) -> None:
    if not session_id:
        return
    path = runs_dir(cfg) / "last_session.json"
    existing = read_last_session(cfg)
    sessions = dict(existing.get("sessions") or {})
    sessions[window] = session_id
    payload = {
        "session_id": session_id,
        "window": window,
        "sessions": sessions,
        "updated_at": iso_now(),
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def status_report(manager: ConfigManager) -> dict[str, Any]:
    cfg = manager.load()
    windows = cfg.work_windows
    items = []
    now = datetime.now()
    for index, window in enumerate(windows):
        task_name = task_name_for(window, index)
        query = query_task(task_name)
        last_run = read_last_run(cfg)
        if last_run.get("window") != window.name:
            last_run = {}
        items.append(
            {
                "window": window.name,
                "task_name": task_name,
                "days": window.days,
                "start": window.start,
                "end": window.end,
                "active_now": window.is_active(now),
                "next_start": window.next_start(now).isoformat(timespec="seconds"),
                **query,
                "last_run": last_run,
            }
        )
    return {
        "tasks": items,
        "all_npa_tasks": list_npa_tasks(),
        "runs_dir": str(runs_dir(cfg)),
        "python": _python_command(),
        "sched_run": str(Path(__file__).with_name("sched_run.py")),
    }


def register_all(manager: ConfigManager) -> dict[str, Any]:
    cfg = manager.load()
    if not cfg.work_windows:
        raise ScheduleError(
            "没有配置任何工作时间段",
            "先在配置页“定时自主工作”中添加 work_windows(JSON 数组),再注册。",
        )
    results = [register_window(manager, w, i) for i, w in enumerate(cfg.work_windows)]
    expected = {task_name_for(w, i) for i, w in enumerate(cfg.work_windows)}
    stale = [
        item.get("TaskName")
        for item in list_npa_tasks()
        if item.get("TaskName") and item.get("TaskName") not in expected
    ]
    return {
        "ok": all(item["ok"] for item in results),
        "tasks": results,
        "stale_tasks": stale,
        "hint": (
            "存在不属于当前配置的旧任务,可用 schedule_remove 删除: " + ", ".join(stale)
            if stale
            else ""
        ),
    }

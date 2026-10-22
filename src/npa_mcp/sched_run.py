"""计划任务入口:在窗口内启动无头 Claude Code,窗口结束即收工.

由 ``scheduler.py`` 生成的任务计划调用::

    pythonw -m npa_mcp.sched_run --window nightly

设计要点:
- **窗口守卫**:任务计划可能因 StartWhenAvailable 在窗口外补跑,启动后
  先判 ``WorkWindow.is_active``,不在窗口内立刻退出,避免白天烧 token。
- **续做链**:优先用配置里的 ``resume_session_id``,否则复用
  ``runs/last_session.json`` 中同一窗口上次的会话 id;本轮结束把新
  session_id 写回。
- **显式白名单**:无头模式默认拒绝一切工具授权,必须传 ``--allowedTools``;
  绝不使用 ``--dangerously-skip-permissions``。
- **窗口末强杀**:每 15s 轮询,窗口结束用 ``taskkill /T /F`` 连 MCP 子进程
  一起结束;``--force`` 仅供人工测试(同时跳过窗口守卫与强杀)。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config, ConfigManager, WorkWindow, npa_home
from .scheduler import read_last_session, runs_dir, task_name_for, write_last_session
from .util import atomic_write_text, iso_now

POLL_SECONDS = 15.0
CLAUDE_JSON_TAIL_BYTES = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# 日志(pythonw 下没有 stdout/stderr,必须落文件)
# ---------------------------------------------------------------------------
def _emit(message: str) -> None:
    if sys.stdout is not None:
        try:
            print(message, flush=True)
        except Exception:
            pass


def _append_log(path: Path, message: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"{iso_now()} {message}\n")
    except OSError:
        pass
    _emit(message)


# ---------------------------------------------------------------------------
# 窗口 / 提示词
# ---------------------------------------------------------------------------
def find_window(cfg: Config, name: str) -> tuple[WorkWindow, int] | None:
    for index, window in enumerate(cfg.work_windows):
        if window.name == name:
            return window, index
    return None


def _read_prompt(path_text: str, window_name: str) -> tuple[str, Path]:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise FileNotFoundError(
            f"任务文件不存在: {path}。请创建该文件并写明本窗口要完成的工作。"
        )
    prompt = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not prompt:
        raise ValueError(f"任务文件为空: {path}")
    return (
        "你现在处于 npa-mcp 的自主工作窗口 "
        f"[{window_name}] 内。请按下面的任务文件自主推进,遇到需要人确认的破坏性操作"
        "一律跳过并记录原因。完成或到达窗口结束时,务必调用 generate_report 生成"
        "中文 HTML 总结报告。\n\n=== 任务文件 ===\n" + prompt,
        path,
    )


# ---------------------------------------------------------------------------
# claude 命令
# ---------------------------------------------------------------------------
def find_claude() -> str:
    override = os.environ.get("NPA_CLAUDE_CMD", "").strip()
    if override:
        found = shutil.which(override) or (override if Path(override).exists() else None)
        if found:
            return found
    for name in ("claude.cmd", "claude.exe", "claude"):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        "找不到 claude 命令。请确认 Claude Code CLI 已安装并在 PATH 中,"
        "或设置环境变量 NPA_CLAUDE_CMD 指向 claude.cmd。"
    )


def build_claude_command(job: Any, resume_id: str) -> tuple[list[str] | str, str]:
    """返回 (Popen 参数, 展示用命令行).

    Windows 的 ``.cmd`` 必须经 cmd.exe 执行。这里刻意把整条命令行作为
    **单个字符串**交给 Popen:若传 list,Popen 会用 ``list2cmdline`` 把内层
    引号转义成 ``\\"``,而 cmd.exe 不认这种转义,路径带空格或参数需要引号
    时会直接报"不是内部或外部命令"。
    """
    claude = find_claude()
    args = ["-p", "--output-format", "json"]
    args += ["--max-turns", str(max(1, int(job.max_turns or 100)))]
    if job.allowed_tools:
        args += ["--allowedTools", str(job.allowed_tools)]
    if job.model:
        args += ["--model", str(job.model)]
    if resume_id:
        args += ["--resume", resume_id]

    if os.name == "nt" and claude.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        inner = f'"{claude}" ' + subprocess.list2cmdline(args)
        # 外层再包一对引号,配合 cmd /s 的规则:去掉首尾引号后,
        # 剩下的 "claude.cmd" -p ... 原样作为命令执行。
        argv = f'"{comspec}" /d /s /c "{inner}"'
        display = argv
    else:
        argv = [claude, *args]
        display = subprocess.list2cmdline(argv)
    return argv, display


def _child_env(window_name: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["NPA_MCP_WINDOW"] = window_name
    return env


# ---------------------------------------------------------------------------
# 进程控制
# ---------------------------------------------------------------------------
def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def run_until_done(
    proc: subprocess.Popen[Any],
    window: WorkWindow,
    *,
    force: bool,
    log_path: Path,
) -> tuple[int, bool]:
    """轮询子进程;窗口结束时杀进程树。返回 (exit_code, killed)."""
    killed = False
    while True:
        try:
            return proc.wait(timeout=POLL_SECONDS), killed
        except subprocess.TimeoutExpired:
            if force:
                continue
            if window.active_end(datetime.now()) is None:
                killed = True
                _append_log(log_path, "[npa] 工作窗口结束,结束 claude 进程树")
                _kill_tree(proc.pid)
                try:
                    return proc.wait(timeout=30), killed
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        return proc.wait(timeout=15), killed
                    except subprocess.TimeoutExpired:
                        return -9, killed


def parse_session_id(log_path: Path) -> str:
    """从 --output-format json 的输出里取 session_id(取最后一个)."""
    try:
        size = log_path.stat().st_size
    except OSError:
        return ""
    if size <= 0:
        return ""
    try:
        with log_path.open("rb") as fh:
            fh.seek(max(0, size - CLAUDE_JSON_TAIL_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("session_id"):
            return str(obj["session_id"])
    # 兜底:整段就是一个 JSON 对象
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if isinstance(obj, dict) and obj.get("session_id"):
        return str(obj["session_id"])
    return ""


def parse_result_summary(log_path: Path) -> dict[str, Any]:
    """取 json 输出中的 cost / turns / result 摘要,仅用于 last_run.json."""
    summary: dict[str, Any] = {}
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return summary
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for key in ("total_cost_usd", "num_turns", "duration_ms", "is_error"):
            if key in obj:
                summary[key] = obj[key]
        result = obj.get("result")
        if isinstance(result, str):
            summary["result_preview"] = result[:600]
        break
    return summary


def write_last_run(cfg: Config, payload: dict[str, Any]) -> Path:
    path = runs_dir(cfg) / "last_run.json"
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="npa_mcp.sched_run",
        description="npa-mcp 自主工作窗口入口(由 Windows 任务计划调用)",
    )
    parser.add_argument("--window", required=True, help="work_windows 中的时间段名称")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令,不启动 claude")
    parser.add_argument("--force", action="store_true", help="人工测试:忽略窗口判断且窗口末不强杀")
    parser.add_argument("--config", default="", help="可选:配置文件路径(默认 ~/.npa-mcp/config.json)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manager = ConfigManager(args.config or None)
    fallback_log = npa_home() / "runs" / "sched_run_errors.log"
    try:
        cfg = manager.load(force=True)
    except Exception as exc:  # noqa: BLE001
        _append_log(fallback_log, f"[npa] 读取配置失败: {exc}")
        return 3

    log_dir = runs_dir(cfg)
    found = find_window(cfg, args.window)
    if found is None:
        _append_log(fallback_log, f"[npa] 找不到工作时段: {args.window}")
        _emit("找不到工作时段,可用时间段: " + ", ".join(w.name for w in cfg.work_windows))
        return 3
    window, index = found
    task_name = task_name_for(window, index)
    started = datetime.now()

    stamp = started.strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"claude_{task_name}_{stamp}.log"

    active = window.is_active(started)
    if not active and not (args.force or args.dry_run):
        message = (
            f"[npa] 当前不在工作时段 [{window.name}] "
            f"({window.start}-{window.end}, days={window.days}),跳过本次补跑"
        )
        _append_log(log_path, message)
        write_last_run(
            cfg,
            {
                "window": window.name,
                "task_name": task_name,
                "started_at": started.isoformat(timespec="seconds"),
                "ended_at": iso_now(),
                "ok": False,
                "skipped": "outside_window",
                "message": message,
            },
        )
        return 2

    job = window.job
    try:
        prompt, prompt_file = _read_prompt(job.task_file, window.name)
    except (OSError, ValueError) as exc:
        _append_log(log_path, f"[npa] 读取任务文件失败: {exc}")
        write_last_run(
            cfg,
            {
                "window": window.name,
                "task_name": task_name,
                "started_at": started.isoformat(timespec="seconds"),
                "ended_at": iso_now(),
                "ok": False,
                "skipped": "bad_task_file",
                "message": str(exc),
            },
        )
        return 3

    last = read_last_session(cfg)
    last_sessions = last.get("sessions") if isinstance(last.get("sessions"), dict) else {}
    resume_id = (job.resume_session_id or "").strip() or str(
        last_sessions.get(window.name, "")
    )

    try:
        argv, display = build_claude_command(job, resume_id)
    except FileNotFoundError as exc:
        _append_log(log_path, f"[npa] {exc}")
        write_last_run(
            cfg,
            {
                "window": window.name,
                "task_name": task_name,
                "started_at": started.isoformat(timespec="seconds"),
                "ended_at": iso_now(),
                "ok": False,
                "skipped": "claude_not_found",
                "message": str(exc),
            },
        )
        return 4

    cwd = Path(job.project_dir).expanduser() if job.project_dir else Path.home()
    if not cwd.exists():
        _append_log(log_path, f"[npa] project_dir 不存在: {cwd},改用用户主目录")
        cwd = Path.home()

    plan = {
        "window": window.name,
        "task_name": task_name,
        "active_now": active,
        "window_start": window.start,
        "window_end": window.end,
        "command": display,
        "cwd": str(cwd),
        "prompt_file": str(prompt_file),
        "resume_session_id": resume_id,
        "log_path": str(log_path),
        "model": job.model,
        "max_turns": job.max_turns,
        "allowed_tools": job.allowed_tools,
    }
    if args.dry_run:
        _emit(json.dumps({"ok": True, "dry_run": True, **plan}, ensure_ascii=False, indent=2))
        return 0

    _append_log(log_path, f"[npa] 开始窗口 [{window.name}] task={task_name}")
    _append_log(log_path, f"[npa] 命令: {display}")
    _append_log(log_path, f"[npa] 工作目录: {cwd};续做会话: {resume_id or '(新建)'}")

    exit_code = -1
    killed = False
    spawn_error = ""
    try:
        log_fh = log_path.open("a", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                env=_child_env(window.name),
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        finally:
            log_fh.close()
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            _append_log(log_path, f"[npa] 写入提示词失败: {exc}")
        exit_code, killed = run_until_done(proc, window, force=args.force, log_path=log_path)
    except Exception as exc:  # noqa: BLE001
        spawn_error = str(exc)
        _append_log(log_path, f"[npa] 启动 claude 失败: {exc}\n{traceback.format_exc()}")

    ended = datetime.now()
    session_id = parse_session_id(log_path)
    if session_id:
        write_last_session(cfg, session_id, window.name)

    payload = {
        "window": window.name,
        "task_name": task_name,
        "started_at": started.isoformat(timespec="seconds"),
        "ended_at": ended.isoformat(timespec="seconds"),
        "duration_sec": round((ended - started).total_seconds(), 1),
        "ok": (exit_code == 0 and not spawn_error and not killed),
        "exit_code": exit_code,
        "killed_by_window": killed,
        "resume_session_id": resume_id,
        "session_id": session_id,
        "prompt_file": str(prompt_file),
        "log_path": str(log_path),
        "cwd": str(cwd),
        "command": display,
        **parse_result_summary(log_path),
    }
    if spawn_error:
        payload["error"] = spawn_error
    write_last_run(cfg, payload)
    _append_log(
        log_path,
        f"[npa] 结束 exit={exit_code} killed={killed} session={session_id or '(未知)'} "
        f"用时 {payload['duration_sec']}s",
    )
    return 0 if payload["ok"] else 1


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()

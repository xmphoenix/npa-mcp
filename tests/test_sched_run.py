"""sched_run 命令构造测试:重点是 Windows ``.cmd`` 包装不被转义破坏.

真实 ``claude`` 在 Windows 是 ``claude.cmd``。如果只把
``[cmd.exe, "/c", '"claude.cmd" -p ...']`` 交给 Popen,``list2cmdline`` 会把
内层引号写成 ``\\"``,cmd.exe 不认,任务计划启动即失败。这里锁定修复后的
"整条命令行作为单个字符串" 形式。
"""

from __future__ import annotations

import os
import subprocess

import pytest

from npa_mcp.config import JobConfig
from npa_mcp.sched_run import build_claude_command


def _job(**overrides: object) -> JobConfig:
    data = {
        "max_turns": 7,
        "allowed_tools": "Bash,Read",
        "model": "test-model",
        "resume_session_id": "",
    }
    data.update(overrides)
    return JobConfig(**data)  # type: ignore[arg-type]


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe 包装仅在 Windows 生效")
def test_cmd_wrapper_is_single_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "npa_mcp.sched_run.find_claude",
        lambda: r"C:\Program Files\nodejs\claude.cmd",
    )
    argv, display = build_claude_command(_job(), "sid-1")

    assert isinstance(argv, str)
    assert argv == display
    assert argv.startswith('"')
    assert "/d /s /c" in argv
    assert argv.endswith('"')
    # 路径整体带引号,且没有被反斜杠转义
    assert r'"C:\Program Files\nodejs\claude.cmd" -p' in argv
    assert '\\"' not in argv
    assert "--output-format json" in argv
    assert "--max-turns 7" in argv
    assert "--allowedTools Bash,Read" in argv
    assert "--model test-model" in argv
    assert "--resume sid-1" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_exe_uses_argv_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("npa_mcp.sched_run.find_claude", lambda: "claude.exe")
    argv, display = build_claude_command(_job(model=""), "")

    assert isinstance(argv, list)
    assert argv == [
        "claude.exe",
        "-p",
        "--output-format",
        "json",
        "--max-turns",
        "7",
        "--allowedTools",
        "Bash,Read",
    ]
    assert display == subprocess.list2cmdline(argv)
    assert "--model" not in argv
    assert "--resume" not in argv

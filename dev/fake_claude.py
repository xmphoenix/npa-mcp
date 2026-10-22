"""假的 Claude Code CLI,供 M4 冒烟验证 sched_run 的进程壳。

它只做四件事,全部由环境变量控制:

- 读完 stdin(提示词),把 argv / stdin / cwd / pid / NPA_MCP_WINDOW 写进
  ``NPA_FAKE_RECORD`` 指定的 JSON 文件;
- 可选 ``NPA_FAKE_SLEEP`` 秒(用于验证窗口结束强杀);
- 向 stdout 打印一行 ``--output-format json`` 形状的结果,带
  ``NPA_FAKE_SESSION`` 作为 session_id;
- 以 ``NPA_FAKE_EXIT`` 退出。

真正的 Claude CLI 不会被调用,避免冒烟消耗 token;命令构造本身由
``sched_run --dry-run`` 用真实 claude 路径验证。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    record_path = os.environ.get("NPA_FAKE_RECORD", "").strip()
    session_id = os.environ.get("NPA_FAKE_SESSION", "fake-session")
    sleep_sec = float(os.environ.get("NPA_FAKE_SLEEP", "0") or 0)
    result_text = os.environ.get("NPA_FAKE_RESULT", "fake claude ok")
    exit_code = int(os.environ.get("NPA_FAKE_EXIT", "0") or 0)

    prompt = sys.stdin.read() if sys.stdin is not None else ""
    record = {
        "argv": sys.argv[1:],
        "stdin": prompt,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "window_env": os.environ.get("NPA_MCP_WINDOW", ""),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if record_path:
        Path(record_path).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if sleep_sec > 0:
        time.sleep(sleep_sec)

    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success" if exit_code == 0 else "error",
                "is_error": exit_code != 0,
                "result": result_text,
                "session_id": session_id,
                "total_cost_usd": 0.0,
                "num_turns": 1,
                "duration_ms": 10,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

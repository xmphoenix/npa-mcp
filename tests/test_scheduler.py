"""任务计划 XML 生成、会话续链与状态查询测试."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from npa_mcp import scheduler
from npa_mcp.config import ConfigManager, WorkWindow
from npa_mcp.scheduler import (
    ScheduleError,
    build_task_xml,
    query_task,
    read_last_session,
    register_all,
    status_report,
    task_name_for,
    unregister_task,
    write_last_session,
)

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _xml_root(window: WorkWindow, task_name: str = "npa-mcp-test") -> ET.Element:
    xml = build_task_xml(window, task_name)
    return ET.fromstring(xml.encode("utf-16"))


def test_task_name_uses_ascii_slug() -> None:
    assert task_name_for(WorkWindow(name="nightly"), 0) == "npa-mcp-nightly"


def test_task_name_chinese_falls_back_to_index() -> None:
    assert task_name_for(WorkWindow(name="夜间构建"), 1) == "npa-mcp-window2"


def test_build_task_xml_weekly_trigger() -> None:
    window = WorkWindow(name="nightly", days=[2, 0], start="23:00", end="06:00")
    root = _xml_root(window)

    days = root.find("t:Triggers/t:CalendarTrigger/t:ScheduleByWeek/t:DaysOfWeek", NS)
    assert days is not None
    assert [child.tag.split("}")[-1] for child in days] == ["Monday", "Wednesday"]

    boundary = root.findtext("t:Triggers/t:CalendarTrigger/t:StartBoundary", namespaces=NS)
    assert boundary is not None
    assert boundary.endswith("T23:00:00")


def test_build_task_xml_settings_and_action() -> None:
    root = _xml_root(WorkWindow(name="nightly", days=[0]))

    assert root.findtext("t:Settings/t:StartWhenAvailable", namespaces=NS) == "true"
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=NS) == "PT12H"

    args = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS)
    assert args is not None
    assert "-m npa_mcp.sched_run" in args
    assert "--window nightly" in args


def test_last_session_round_trip(manager: ConfigManager) -> None:
    cfg = manager.load()
    write_last_session(cfg, "sid-1", "nightly")
    write_last_session(cfg, "sid-2", "weekend")

    data = read_last_session(cfg)
    assert data["sessions"] == {"nightly": "sid-1", "weekend": "sid-2"}
    assert data["session_id"] == "sid-2"


def test_write_last_session_ignores_empty_id(manager: ConfigManager) -> None:
    cfg = manager.load()
    write_last_session(cfg, "", "nightly")
    assert read_last_session(cfg) == {}


def test_read_last_session_legacy_format(manager: ConfigManager) -> None:
    cfg = manager.load()
    path = scheduler.runs_dir(cfg) / "last_session.json"
    path.write_text(
        json.dumps({"session_id": "legacy-1", "window": "nightly"}),
        encoding="utf-8",
    )
    data = read_last_session(cfg)
    assert data["sessions"] == {"nightly": "legacy-1"}


def test_status_report_merges_task_state(
    manager: ConfigManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = manager.load()
    cfg.work_windows = [WorkWindow(name="nightly", days=[0], start="23:00", end="06:00")]
    manager.save(cfg)

    monkeypatch.setattr(
        scheduler, "query_task", lambda name: {"registered": True, "state": "Ready"}
    )
    monkeypatch.setattr(
        scheduler,
        "list_npa_tasks",
        lambda: [{"TaskName": "npa-mcp-nightly", "State": "Ready"}],
    )

    report = status_report(manager)
    item = report["tasks"][0]
    assert item["window"] == "nightly"
    assert item["task_name"] == "npa-mcp-nightly"
    assert item["registered"] is True
    assert item["state"] == "Ready"
    assert report["all_npa_tasks"] == [{"TaskName": "npa-mcp-nightly", "State": "Ready"}]
    assert Path(report["runs_dir"]).is_dir()


def test_register_all_requires_windows(manager: ConfigManager) -> None:
    with pytest.raises(ScheduleError) as excinfo:
        register_all(manager)
    assert "没有配置" in excinfo.value.message


def test_unregister_rejects_invalid_name() -> None:
    with pytest.raises(ScheduleError):
        unregister_task("bad name; rm -rf")


def test_query_task_parses_powershell_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scheduler,
        "_powershell",
        lambda script, timeout=30.0: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="NPA_TASK|Ready|2026-10-22 23:00:00|0|2026-10-23 23:00:00|1\n",
            stderr="",
        ),
    )
    result = query_task("npa-mcp-nightly")
    assert result["registered"] is True
    assert result["state"] == "Ready"
    assert result["last_result"] == "0"
    assert result["missed_runs"] == 1


def test_query_task_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scheduler,
        "_powershell",
        lambda script, timeout=30.0: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="NPA_NOT_FOUND\n", stderr=""
        ),
    )
    assert query_task("npa-mcp-missing") == {"registered": False}

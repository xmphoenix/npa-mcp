"""HTML 报告生成测试:内容、转义、零外链、文件名与会话附录."""

from __future__ import annotations

from pathlib import Path

from npa_mcp.config import ConfigManager
from npa_mcp.report import _normalize_issues, generate_report
from npa_mcp.sessions import SessionHub


def _read(result: dict) -> str:
    return Path(result["path"]).read_text(encoding="utf-8")


def test_generate_report_content(manager: ConfigManager) -> None:
    result = generate_report(
        manager,
        title="M1 骨架完成",
        summary="配置页与两个基础工具已完成。",
        changes=[
            "新增 config.py",
            {"category": "文档", "description": "README 初稿", "file": "README.md"},
            {"desc": "仅有描述也应接受"},
            "",
        ],
        issues=[
            {"severity": "高", "description": "凭据明文存储", "status": "已记录"},
            "未启用 HTTPS",
        ],
        next_steps="- 跑冒烟\n2. 补测试",
        status="失败",
        project="npa-mcp",
    )

    assert result["ok"] is True
    assert result["changes"] == 3
    assert result["issues"] == 2
    assert result["status"] == "failed"

    html = _read(result)
    assert "M1 骨架完成" in html
    assert "配置页与两个基础工具已完成。" in html
    assert "README 初稿" in html
    assert "仅有描述也应接受" in html
    assert "凭据明文存储" in html
    assert "已记录" in html
    assert "跑冒烟" in html
    assert "补测试" in html
    assert "失败" in html
    assert "npa-mcp" in html


def test_report_escapes_html(manager: ConfigManager) -> None:
    payload = "<script>alert('x')</script>"
    result = generate_report(manager, title="转义 <测试>", summary=payload)
    html = _read(result)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "转义 &lt;测试&gt;" in html


def test_report_has_no_external_references(manager: ConfigManager) -> None:
    html = _read(generate_report(manager, title="零外链"))
    assert "http://" not in html
    assert "https://" not in html
    assert "<script" not in html.lower()
    assert "@import" not in html


def test_report_filenames_are_unique(manager: ConfigManager) -> None:
    first = generate_report(manager, title="same title")
    second = generate_report(manager, title="same title")

    assert first["path"] != second["path"]
    assert Path(first["path"]).is_file()
    assert Path(second["path"]).is_file()


def test_report_url_reflects_web_port(manager: ConfigManager) -> None:
    without_port = generate_report(manager, title="无端口")
    assert without_port["url"] == ""

    with_port = generate_report(manager, title="有端口", web_port=8765)
    assert with_port["url"].startswith("http://127.0.0.1:8765/reports/")
    assert with_port["filename"] in with_port["url"]


def test_unknown_status_becomes_warning(manager: ConfigManager) -> None:
    result = generate_report(manager, title="状态", status="whatever")
    assert result["status"] == "warning"


async def test_report_includes_session_log(manager: ConfigManager) -> None:
    hub = SessionHub(manager)
    try:
        session = hub.open_mirror("设备控制台", "root@dev")
        session.publish("$ make -j8\r\nbuild ok\r\n")

        result = generate_report(
            manager,
            title="会话附录",
            hub=hub,
            session_ids=[session.session_id, "missing-id"],
        )
        html = _read(result)

        assert result["sessions"] == 2
        assert "设备控制台" in html
        assert "build ok" in html
        assert "无法读取日志" in html
    finally:
        await hub.close_all()


def test_normalize_issues_severity_mapping() -> None:
    items = _normalize_issues(
        [
            {"severity": "高", "description": "a"},
            {"severity": "critical", "description": "b"},
            {"severity": "low", "description": "c"},
            {"severity": "unknown", "description": "d"},
            {"desc": "e"},
        ]
    )
    assert [item["severity"] for item in items] == ["high", "high", "low", "medium", "medium"]
    assert items[0]["severity_text"] == "高"

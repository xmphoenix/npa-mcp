"""HTML 工作总结生成.

报告是独立单文件(内联 CSS、零 CDN、零 JS),可直接归档、发邮件或放进
``report_dir`` 由网页浏览。文件名为 ``{slug}_{YYYYMMDD_HHMM}.html``。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ConfigManager
from .sessions import Console, SessionError, SessionHub
from .util import atomic_write_text, ensure_dir, get_logger, safe_slug

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent / "web"
TEMPLATE_NAME = "report.html.j2"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SEVERITY_MAP = {
    "high": "high",
    "严重": "high",
    "高": "high",
    "critical": "high",
    "medium": "medium",
    "中": "medium",
    "一般": "medium",
    "warning": "medium",
    "low": "low",
    "低": "low",
    "info": "low",
    "提示": "low",
}
_SEVERITY_TEXT = {"high": "高", "medium": "中", "low": "低"}
_STATUS_MAP = {
    "success": "success",
    "成功": "success",
    "ok": "success",
    "done": "success",
    "warning": "warning",
    "部分完成": "warning",
    "有风险": "warning",
    "failed": "failed",
    "失败": "failed",
    "error": "failed",
}
_STATUS_TEXT = {"success": "成功", "warning": "部分完成", "failed": "失败"}


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(WEB_DIR)),
        autoescape=select_autoescape(default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _normalize_changes(items: Iterable[Any] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in items or []:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append({"category": "改动", "description": text, "file": ""})
            continue
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or item.get("desc") or "").strip()
        if not description:
            continue
        result.append(
            {
                "category": str(item.get("category") or "改动").strip(),
                "description": description,
                "file": str(item.get("file") or item.get("path") or "").strip(),
            }
        )
    return result


def _normalize_issues(items: Iterable[Any] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in items or []:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append(
                    {"severity": "medium", "severity_text": "中", "description": text, "status": ""}
                )
            continue
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or item.get("desc") or "").strip()
        if not description:
            continue
        raw = str(item.get("severity") or "medium").strip().lower()
        severity = _SEVERITY_MAP.get(raw, "medium")
        result.append(
            {
                "severity": severity,
                "severity_text": _SEVERITY_TEXT[severity],
                "description": description,
                "status": str(item.get("status") or "").strip(),
            }
        )
    return result


def _normalize_steps(items: Iterable[Any] | str | None) -> list[str]:
    if items is None:
        return []
    if isinstance(items, str):
        items = items.splitlines()
    result = []
    for item in items:
        text = str(item).strip().lstrip("-*0123456789. ")
        if text:
            result.append(text)
    return result


def _tail_text(path: Path, *, max_bytes: int = 200_000, max_lines: int = 1200) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - max_bytes))
            raw = fh.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", "replace")
    # 从中间截断时丢掉第一行残片
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    text = _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        text = f"(仅显示最后 {max_lines} 行)\n" + "\n".join(lines)
    else:
        text = "\n".join(lines)
    return text.strip()


def _collect_sessions(hub: SessionHub | None, session_ids: Iterable[str] | None) -> list[dict[str, Any]]:
    if hub is None or not session_ids:
        return []
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_id in session_ids:
        session_id = str(raw_id).strip()
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        try:
            session = hub.get(session_id)
        except SessionError:
            collected.append(
                {
                    "session_id": session_id,
                    "label": f"{session_id}(不存在)",
                    "kind": "-",
                    "target": "",
                    "closed": True,
                    "text": "(会话已不存在,无法读取日志)",
                }
            )
            continue
        collected.append(_session_entry(session))
    return collected


def _session_entry(session: Console) -> dict[str, Any]:
    text = ""
    try:
        text = _tail_text(session.log.path)
    except Exception:  # noqa: BLE001
        text = session.ring.tail(400)
    if not text:
        text = session.ring.tail(400).strip()
    info = session.info()
    return {
        "session_id": info.session_id,
        "label": info.label,
        "kind": info.kind,
        "target": info.target,
        "closed": info.closed,
        "text": text,
    }


def _unique_path(report_dir: Path, slug: str, now: datetime) -> Path:
    base = f"{slug}_{now.strftime('%Y%m%d_%H%M')}"
    candidate = report_dir / f"{base}.html"
    index = 2
    while candidate.exists():
        candidate = report_dir / f"{base}_{index}.html"
        index += 1
    return candidate


def generate_report(
    manager: ConfigManager,
    *,
    title: str,
    summary: str = "",
    changes: Iterable[Any] | None = None,
    issues: Iterable[Any] | None = None,
    next_steps: Iterable[Any] | str | None = None,
    session_ids: Iterable[str] | None = None,
    status: str = "success",
    project: str = "",
    hub: SessionHub | None = None,
    web_port: int = 0,
) -> dict[str, Any]:
    title = (title or "").strip() or "开发工作总结"
    raw_status = (status or "success").strip().lower()
    status_key = _STATUS_MAP.get(raw_status, "warning")
    now = datetime.now()
    cfg = manager.load()

    normalized_changes = _normalize_changes(changes)
    normalized_issues = _normalize_issues(issues)
    normalized_steps = _normalize_steps(next_steps)
    sessions = _collect_sessions(hub, session_ids)

    html = _env().get_template(TEMPLATE_NAME).render(
        title=title,
        project=project.strip(),
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        status=status_key,
        status_text=_STATUS_TEXT[status_key],
        summary=(summary or "").strip(),
        changes=normalized_changes,
        issues=normalized_issues,
        next_steps=normalized_steps,
        sessions=sessions,
    )

    report_dir = cfg.paths.report_path()
    ensure_dir(report_dir)
    path = _unique_path(report_dir, safe_slug(title, "report"), now)
    atomic_write_text(path, html)
    url = ""
    if web_port:
        url = f"http://127.0.0.1:{web_port}/reports/{quote(path.name)}"
    log.info("已生成报告: %s", path)
    return {
        "ok": True,
        "path": str(path),
        "filename": path.name,
        "url": url,
        "bytes": len(html.encode("utf-8")),
        "changes": len(normalized_changes),
        "issues": len(normalized_issues),
        "sessions": len(sessions),
        "status": status_key,
    }

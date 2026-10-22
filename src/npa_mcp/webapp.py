"""本地网页:配置页、xterm 终端页、WebSocket、镜像下载.

安全边界:
- 配置页/终端页/WebSocket/管理 API 只允许本机访问(127.0.0.1/::1)。
- ``/images`` 与 ``/reports`` 可对局域网开放(设备拉固件、同事看报告);
  镜像下载可选用 token 保护。
- 所有文件访问都经过 safe_join,不能越出配置目录。
"""

from __future__ import annotations

import asyncio
import html
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.templating import Jinja2Templates

from .config import ConfigError, ConfigManager, detect_lan_ip
from .fileservers import FileserverManager
from .sessions import SessionError, SessionHub
from .util import get_logger, safe_join

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent / "web"
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

LOCAL_ONLY_PREFIXES = ("/config", "/api", "/terminals", "/ws")


def _client_is_local(host: str | None) -> bool:
    return host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


class RuntimeState:
    """web 端口在启动时可能自增,这里保存真实值."""

    def __init__(self) -> None:
        self.web_port = 0
        self.started_at = time.time()
        self.version = ""


def create_app(
    manager: ConfigManager,
    hub: SessionHub,
    fileservers: FileserverManager,
    runtime: RuntimeState,
) -> FastAPI:
    app = FastAPI(title="npa-mcp", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # -- 本机访问限制 -----------------------------------------------------
    @app.middleware("http")
    async def local_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path.startswith(LOCAL_ONLY_PREFIXES) and not _client_is_local(
            request.client.host if request.client else None
        ):
            return JSONResponse(
                {"ok": False, "error": "配置/终端页面仅允许本机访问"},
                status_code=403,
            )
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse(url="/config")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "npa-mcp", "uptime_sec": round(time.time() - runtime.started_at)}

    @app.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):  # type: ignore[no-untyped-def]
        return templates.TemplateResponse(
            request,
            "config.html",
            {"version": runtime.version, "web_port": runtime.web_port},
        )

    @app.get("/terminals", response_class=HTMLResponse)
    async def terminals_page(request: Request):  # type: ignore[no-untyped-def]
        return templates.TemplateResponse(
            request,
            "terminals.html",
            {"version": runtime.version, "web_port": runtime.web_port},
        )

    # -- 配置 API ---------------------------------------------------------
    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        try:
            data = manager.masked_dict()
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "config": data, "path": str(manager.path)})

    @app.post("/api/config")
    async def save_config(request: Request) -> JSONResponse:
        try:
            patch = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
        if not isinstance(patch, dict):
            return JSONResponse({"ok": False, "error": "配置必须是 JSON 对象"}, status_code=400)
        try:
            cfg = manager.update_from_dict(patch)
        except ValidationError as exc:
            messages = []
            for item in exc.errors():
                location = ".".join(str(part) for part in item.get("loc", ()))
                messages.append(f"{location}: {item.get('msg', '')}")
            return JSONResponse(
                {"ok": False, "error": "配置校验失败", "detail": messages[:20]}, status_code=400
            )
        except (ValueError, ConfigError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "message": "配置已保存,后续工具调用立即生效(无需重启)",
                "config": manager.masked_dict(),
                "port_changed": cfg.web.port != runtime.web_port,
                "hint": (
                    "网页端口修改需重启 MCP 进程后生效"
                    if cfg.web.port != runtime.web_port
                    else ""
                ),
            }
        )

    @app.get("/api/status")
    async def status_api() -> JSONResponse:
        return JSONResponse({"ok": True, "status": build_status(manager, hub, fileservers, runtime)})

    @app.get("/api/lan-ip")
    async def lan_ip() -> dict[str, str]:
        return {"ok": True, "lan_ip": detect_lan_ip(manager.load().lan_peer())}

    # -- 会话 API ---------------------------------------------------------
    @app.get("/api/sessions")
    async def sessions_list() -> dict[str, Any]:
        return {"ok": True, "sessions": hub.list()}

    @app.post("/api/sessions/open")
    async def sessions_open(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        kind = str(body.get("kind", "")).strip() or None
        try:
            session = await hub.open_device(kind)
        except SessionError as exc:
            return JSONResponse(
                {"ok": False, "error": exc.message, "hint": exc.hint}, status_code=400
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("打开会话失败")
            return JSONResponse({"ok": False, "error": f"打开会话失败: {exc}"}, status_code=500)
        info = session.info().to_dict()
        return JSONResponse({"ok": True, "session": info})

    @app.post("/api/sessions/{session_id}/close")
    async def sessions_close(session_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        force = bool(body.get("force", False))
        try:
            result = await hub.close_session(session_id, force=force)
        except SessionError as exc:
            return JSONResponse({"ok": False, "error": exc.message}, status_code=404)
        return JSONResponse({"ok": True, **result})

    # -- WebSocket 终端 ---------------------------------------------------
    @app.websocket("/ws/session/{session_id}")
    async def ws_session(websocket: WebSocket, session_id: str) -> None:
        if not _client_is_local(websocket.client.host if websocket.client else None):
            await websocket.close(code=1008)
            return
        try:
            session = hub.get(session_id)
        except SessionError:
            await websocket.close(code=1008)
            return
        await websocket.accept()

        def on_data(text: str) -> None:
            async def _send() -> None:
                try:
                    await websocket.send_text(
                        json.dumps({"type": "data", "text": text}, ensure_ascii=False)
                    )
                except Exception:
                    pass

            try:
                asyncio.create_task(_send())
            except RuntimeError:
                pass

        history = session.ring.tail(400)
        next_seq = session.ring.next_seq
        unsubscribe = hub.subscribe(session_id, on_data)
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "init",
                        "session": session.info().to_dict(),
                        "text": history,
                        "next_seq": next_seq,
                        "read_only": session.read_only,
                    },
                    ensure_ascii=False,
                )
            )
        except Exception:
            unsubscribe()
            await websocket.close()
            return
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 65536:
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    message = None
                if isinstance(message, dict):
                    kind = message.get("type")
                    if kind == "ping":
                        await websocket.send_text('{"type":"pong"}')
                        continue
                    if kind == "input":
                        data = str(message.get("data", ""))
                    else:
                        continue
                else:
                    data = raw
                if not data:
                    continue
                try:
                    await hub.send(session, data)
                except SessionError as exc:
                    await websocket.send_text(
                        json.dumps(
                            {"type": "notice", "text": f"\r\n[npa] {exc.message}\r\n"},
                            ensure_ascii=False,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    await websocket.send_text(
                        json.dumps(
                            {"type": "notice", "text": f"\r\n[npa] 发送失败: {exc}\r\n"},
                            ensure_ascii=False,
                        )
                    )
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("WebSocket 会话异常", exc_info=True)
        finally:
            unsubscribe()
            try:
                await websocket.close()
            except Exception:
                pass

    # -- 固件镜像下载 -----------------------------------------------------
    @app.get("/images/{filename:path}")
    async def download_image(filename: str, request: Request, token: str = ""):
        cfg = manager.load()
        expected = cfg.firmware_server.http.token
        if expected and token != expected:
            supplied = request.headers.get("x-npa-token", "")
            if supplied != expected:
                raise HTTPException(status_code=403, detail="token 不正确")
        root = cfg.paths.image_path()
        path = safe_join(root, filename)
        if path is None:
            raise HTTPException(status_code=400, detail="非法路径")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(path, filename=path.name)

    # -- 报告浏览 ---------------------------------------------------------
    @app.get("/reports", response_class=HTMLResponse)
    async def reports_index() -> str:
        cfg = manager.load()
        root = cfg.paths.report_path()
        rows = []
        if root.exists():
            files = sorted(root.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
            for item in files[:200]:
                try:
                    size = item.stat().st_size
                    mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.stat().st_mtime))
                except OSError:
                    continue
                rows.append(
                    f'<tr><td><a href="/reports/{html.escape(item.name)}">'
                    f"{html.escape(item.name)}</a></td>"
                    f"<td>{mtime}</td><td>{size / 1024:.1f} KB</td></tr>"
                )
        body = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>npa-mcp 报告</title><style>"
            "body{font-family:'Microsoft YaHei',sans-serif;margin:32px;background:#f6f7f9;color:#1f2937}"
            "h1{font-size:22px}table{border-collapse:collapse;width:100%;background:#fff;"
            "box-shadow:0 1px 4px rgba(0,0,0,.08)}th,td{padding:10px 14px;text-align:left;"
            "border-bottom:1px solid #e5e7eb;font-size:14px}a{color:#2563eb;text-decoration:none}"
            "</style></head><body><h1>npa-mcp 工作报告</h1>"
            "<p><a href='/config'>返回配置页</a></p>"
            "<table><thead><tr><th>文件</th><th>修改时间</th><th>大小</th></tr></thead><tbody>"
            + ("".join(rows) or "<tr><td colspan='3'>还没有报告</td></tr>")
            + "</tbody></table></body></html>"
        )
        return body

    @app.get("/reports/{filename:path}")
    async def report_file(filename: str):
        cfg = manager.load()
        path = safe_join(cfg.paths.report_path(), filename)
        if path is None or not path.is_file() or path.suffix.lower() != ".html":
            raise HTTPException(status_code=404, detail="报告不存在")
        return FileResponse(path)

    return app


def build_status(
    manager: ConfigManager,
    hub: SessionHub,
    fileservers: FileserverManager,
    runtime: RuntimeState,
) -> dict[str, Any]:
    cfg = manager.load()
    device = cfg.device
    return {
        "version": runtime.version,
        "web": {
            "host": cfg.web.host,
            "port": runtime.web_port,
            "url": f"http://127.0.0.1:{runtime.web_port}/config",
            "terminals_url": f"http://127.0.0.1:{runtime.web_port}/terminals",
        },
        "config_path": str(manager.path),
        "paths": cfg.paths.model_dump(),
        "code_server": {
            "host": cfg.code_server.host,
            "port": cfg.code_server.port,
            "username": cfg.code_server.username,
            "workdir": cfg.code_server.workdir,
            "configured": bool(
                cfg.code_server.host
                and cfg.code_server.username
                and cfg.code_server.auth.is_configured()
            ),
        },
        "device": {
            "prefer": device.prefer,
            "ssh": {
                "enabled": device.ssh.enabled,
                "host": device.ssh.host,
                "configured": bool(device.ssh.host and device.ssh.auth.is_configured()),
            },
            "telnet": {
                "enabled": device.telnet.enabled,
                "host": device.telnet.host,
                "configured": bool(device.telnet.host),
            },
            "serial": {
                "enabled": device.serial.enabled,
                "port": device.serial.port,
                "configured": bool(device.serial.port),
            },
        },
        "power": {
            "transport": cfg.power.transport,
            "host": cfg.power.host,
            "outlet": cfg.power.outlet,
            "configured": bool(cfg.power.host),
        },
        "fileservers": fileservers.status(),
        "sessions": hub.list(),
        "work_windows": [w.model_dump(mode="json") for w in cfg.work_windows],
        "lan_ip": detect_lan_ip(cfg.lan_peer()),
    }

# -*- coding: utf-8 -*-
"""WebUI：设备总览、运行状态、服务日志、高级工具调试。

使用 .env 中的 WEBUI_TOKEN 登录后，再用 HA_URL / HA_TOKEN 连接 Home Assistant：
    python -m homeassistant_xiaozhiserver.webui

安全约定：
- 所有页面与 API 都需要 WEBUI_TOKEN 登录；未设置时会生成临时令牌并写入日志；
- 请只绑定可信内网，不要直接暴露公网；
- HA_TOKEN、MCP_ENDPOINT 的令牌只做掩码展示，绝不回传明文；
- 工具名白名单，只允许调用 server.py 暴露的七个工具；
- 日志来自共享日志文件，包含 WebUI 与 MCP bridge 两个进程。
"""
from __future__ import annotations

try:  # 独立运行 WebUI 时也读取项目 .env；容器内由 env_file 注入，不覆盖已有值
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

import asyncio
import hashlib
import hmac
import html
import inspect
import ipaddress
import json
import logging
import os
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import __version__
from . import server as mcp_server
from .client import HAError
from .config import (
    DANGEROUS_SERVICE_DOMAINS,
    DEFAULT_LIST_LIMIT,
    EXCLUDED_DEVICE_KEYWORDS,
    EXCLUDED_DOMAINS,
    HA_SERVICES_CACHE_TTL,
    HA_STATE_CACHE_TTL,
    HA_STATE_WS_ENABLED,
    HA_STATE_WS_MAX_FAILURES,
    LOG_LEVEL,
    METRICS_ENABLED,
    MAX_LIST_LIMIT,
    SERVICE_CONFIRM_DELAY,
    SERVICE_CONFIRM_RETRIES,
    SERVICE_CONFIRM_TIMEOUT,
    WEBUI_ALLOWED_HOSTS,
    WEBUI_CACHE_TTL,
    WEBUI_HOST,
    WEBUI_PORT,
    WEBUI_TOKEN,
    WEBUI_TOKEN_GENERATED,
)
from .danger import service_is_dangerous
from .logging_setup import log_file_path, read_recent_logs, setup_logging
from .metrics import metrics

log = logging.getLogger("homeassistant_xiaozhiserver.webui")

_STARTED_AT = time.time()
_MAX_TOOL_BODY = 64 * 1024
_DANGER_GATED_TOOLS = frozenset({"control_device", "control_devices", "call_service"})
_AUTH_COOKIE = "xiaozhi_webui"
_PUBLIC_PATHS = frozenset({"/login", "/healthz", "/readyz", "/metrics"})


def _auth_cookie_value() -> str:
    return hashlib.sha256(("webui-session:" + WEBUI_TOKEN).encode("utf-8")).hexdigest()


def _token_ok(raw: str) -> bool:
    return bool(WEBUI_TOKEN) and hmac.compare_digest(str(raw or ""), WEBUI_TOKEN)


def _cookie_ok(raw: str) -> bool:
    return bool(WEBUI_TOKEN) and hmac.compare_digest(str(raw or ""), _auth_cookie_value())


def _metric_path(path: str) -> str:
    return "/api/tool/{name}" if path.startswith("/api/tool/") else path

# 工具白名单：名称 -> (说明, 参数示例)
_TOOLS: list[tuple[str, str, str]] = [
    ("list_devices", "列出/查询设备（keyword/domain/area/分页）", '{"keyword": "客厅"}'),
    ("get_device_state", "查询单个设备状态", '{"entity": "客厅灯"}'),
    ("control_device", "控制设备（危险设备需 confirm_token）", '{"entity": "客厅灯", "action": "turn_on"}'),
    ("control_devices", "批量控制多个设备（同一动作）", '{"entities": ["客厅灯", "switch.fan"], "action": "turn_on"}'),
    ("list_services", "查询某类设备支持的服务与参数", '{"domain": "light"}'),
    ("call_service", "调用任意 HA 服务", '{"entity": "客厅灯", "service": "light.turn_on", "data": {"brightness_pct": 50}}'),
    ("list_domains", "列出设备类型", "{}"),
]
_TOOL_FUNCS = {
    name: getattr(mcp_server, name)
    for name, _, _ in _TOOLS
    if hasattr(mcp_server, name)
}
# 只展示当前 mcp 版本实际注册成功的工具，避免 UI 可选中但调用 404。
_TOOLS = [tool for tool in _TOOLS if tool[0] in _TOOL_FUNCS]

_DOMAIN_LABELS = {
    "light": "灯具",
    "switch": "开关",
    "climate": "空调",
    "cover": "窗帘/门",
    "lock": "门锁",
    "media_player": "媒体",
    "fan": "风扇",
    "vacuum": "扫地机",
    "sensor": "传感器",
    "binary_sensor": "传感器",
    "scene": "场景",
    "script": "脚本",
    "automation": "自动化",
    "camera": "摄像头",
    "humidifier": "加湿器",
    "water_heater": "热水器",
    "alarm_control_panel": "安防",
    "button": "按钮",
    "number": "数值",
    "select": "选择",
    "input_boolean": "虚拟开关",
    "input_number": "虚拟数值",
    "input_select": "虚拟选择",
    "person": "人员",
    "device_tracker": "定位",
    "sun": "太阳",
    "weather": "天气",
    "update": "更新",
    "remote": "遥控",
    "siren": "警报",
    "valve": "阀门",
    "lawn_mower": "割草机",
    "todo": "待办",
    "calendar": "日历",
}


def _env(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return raw.strip() if raw is not None and raw.strip() else default


def _domain_label(domain: str) -> str:
    return _DOMAIN_LABELS.get((domain or "").lower(), domain or "未知")


def _mask_secret(value: str) -> str:
    return "已设置（掩码）" if value else "（未设置）"


def _mask_url(raw: str) -> str:
    if not raw:
        return "（未设置）"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "（格式无法解析）"
    if not parts.scheme or not parts.netloc:
        return "（格式无法解析）"
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _mask_endpoint(raw: str) -> str:
    if not raw:
        return "（未设置）"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "（格式无法解析）"
    if not parts.scheme or not parts.netloc:
        return "（格式无法解析）"
    netloc = parts.netloc.rsplit("@", 1)[-1]
    sensitive = ("token", "key", "secret", "password", "sig", "auth", "credential")
    query = "&".join(
        key + "=***" if any(marker in key.lower() for marker in sensitive) else key + "=" + value
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def create_app() -> FastAPI:
    app = FastAPI(title="homeassistant-xiaozhiserver WebUI", docs_url=None, redoc_url=None, openapi_url=None)

    def _host_name(host: str) -> str:
        value = (host or "").strip().lower()
        if value.startswith("["):
            return value[1:].split("]", 1)[0]
        if value.count(":") == 1:
            return value.rsplit(":", 1)[0]
        return value

    def _host_port(host: str) -> int | None:
        try:
            return urlsplit("//" + (host or "").strip()).port
        except ValueError:
            return None

    def _origin_allowed(parts, host: str) -> bool:
        """比较 Origin 与 Host，并按 scheme 归一化默认端口。"""
        if parts.scheme not in ("http", "https"):
            return False
        origin_name = (parts.hostname or "").lower()
        if not origin_name:
            return False
        default_port = 443 if parts.scheme == "https" else 80
        return (origin_name == _host_name(host)
                and (parts.port or default_port) == (_host_port(host) or default_port))

    def _host_allowed(host: str) -> bool:
        name = _host_name(host)
        if not name:
            return False
        if name == "localhost":
            return True
        if name in WEBUI_ALLOWED_HOSTS:
            return True
        try:
            ip = ipaddress.ip_address(name)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private or ip.is_link_local

    def _harden(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'",
        )
        return response

    def _login_html(error: str = "") -> str:
        message = ('<p class="bad">' + html.escape(error) + '</p>') if error else ""
        return (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>登录 · 小智 MCP</title>'
            '<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;'
            'background:#f5f6f8;display:flex;min-height:100vh;align-items:center;justify-content:center}'
            'form{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:28px;width:320px;'
            'box-shadow:0 10px 30px rgba(15,23,42,.08)}h1{font-size:18px;margin:0 0 6px}'
            'p{font-size:13px;color:#6b7280;margin:0 0 14px}input{width:100%;box-sizing:border-box;'
            'padding:10px;border:1px solid #d1d5db;border-radius:6px;font:inherit;margin-bottom:12px}'
            'button{width:100%;padding:10px;border:0;border-radius:6px;background:#2563eb;color:#fff;'
            'font:inherit;cursor:pointer}.bad{color:#b91c1c;font-size:13px}</style>'
            '<form method="post" action="/login"><h1>小智 MCP 控制台</h1>'
            '<p>请输入 .env 中的 WEBUI_TOKEN</p>' + message +
            '<input type="password" name="token" placeholder="访问令牌" autofocus autocomplete="current-password">'
            '<button type="submit">进入</button></form></html>'
        )

    @app.middleware("http")
    async def _same_origin(request: Request, call_next):
        """安全中间件：Host/Origin 校验、WEBUI_TOKEN 登录校验、安全响应头与访问指标。"""
        host = request.headers.get("host", "")
        if not _host_allowed(host):
            return _harden(JSONResponse(
                {"ok": False, "code": "host_not_allowed", "message": "Host 不在允许范围内。"},
                status_code=403,
            ))
        path = request.url.path
        if path not in _PUBLIC_PATHS and not _cookie_ok(request.cookies.get(_AUTH_COOKIE, "")):
            if path.startswith("/api/") or path.startswith("/tool"):
                return _harden(JSONResponse(
                    {"ok": False, "code": "unauthorized", "message": "请先登录。"},
                    status_code=401,
                ))
            return _harden(RedirectResponse("/login"))
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if not origin:
                return _harden(JSONResponse(
                    {"ok": False, "code": "cross_origin_blocked", "message": "写请求必须携带同源 Origin。"},
                    status_code=403,
                ))
            parts = urlsplit(origin)
            if not _origin_allowed(parts, host):
                return _harden(JSONResponse(
                    {"ok": False, "code": "cross_origin_blocked", "message": "拒绝跨站请求。"},
                    status_code=403,
                ))
        started = time.perf_counter()
        response = await call_next(request)
        if METRICS_ENABLED:
            metrics.record_http(request.method, _metric_path(path), response.status_code,
                                time.perf_counter() - started)
        return _harden(response)

    _status_cache: tuple[float, dict[str, Any]] | None = None

    async def _status_data_uncached() -> dict[str, Any]:
        ha_url = _env("HA_URL")
        ha_token = _env("HA_TOKEN")
        configured = bool(ha_url and ha_token)
        reachable: bool | None = None
        error: str | None = None
        if configured:
            try:
                reachable = await asyncio.wait_for(mcp_server.get_ha().ping(), timeout=3.0)
            except asyncio.TimeoutError:
                reachable = False
                error = "timeout"
            except HAError as exc:
                reachable = False
                error = exc.code
            except Exception:  # noqa: BLE001
                reachable = False
                error = "internal_error"
        return {
            "ok": True,
            "version": __version__,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_STARTED_AT)),
            "uptime_seconds": int(time.time() - _STARTED_AT),
            "ha": {
                "configured": configured,
                "url": _mask_url(ha_url),
                "token": _mask_secret(ha_token),
                "reachable": reachable,
                "error": error,
            },
            "mcp_endpoint": {
                "configured": bool(_env("MCP_ENDPOINT")),
                "display": _mask_endpoint(_env("MCP_ENDPOINT")),
            },
            "tools": [
                {"name": name, "description": desc, "example": example}
                for name, desc, example in _TOOLS
            ],
            "target": "stdio MCP server + WebSocket bridge",
        }

    async def _status_data(force: bool = False) -> dict[str, Any]:
        nonlocal _status_cache
        if not force and _status_cache is not None and time.monotonic() - _status_cache[0] < WEBUI_CACHE_TTL:
            return _status_cache[1]
        data = await _status_data_uncached()
        _status_cache = (time.monotonic(), data)
        return data

    def _config_data() -> dict[str, Any]:
        try:
            ha = mcp_server.get_ha()
            timeout = str(ha.timeout)
            verify_ssl = str(ha.verify_ssl).lower()
            ws_enabled = str(ha.ws_enabled).lower()
        except Exception:  # noqa: BLE001 - 未配置时仍返回只读视图
            timeout = _env("HA_TIMEOUT", "10")
            verify_ssl = _env("HA_VERIFY_SSL", "true").lower()
            ws_enabled = _env("HA_WS_ENABLED", "true").lower()

        return {
            "ok": True,
            "items": [
                {"key": "HA_URL", "value": _mask_url(_env("HA_URL")), "secret": False},
                {"key": "HA_TOKEN", "value": _mask_secret(_env("HA_TOKEN")), "secret": True},
                {"key": "MCP_ENDPOINT", "value": _mask_endpoint(_env("MCP_ENDPOINT")), "secret": True},
                {"key": "HA_TIMEOUT", "value": timeout, "secret": False},
                {"key": "HA_VERIFY_SSL", "value": verify_ssl, "secret": False},
                {"key": "HA_WS_ENABLED", "value": ws_enabled, "secret": False},
                {"key": "HA_STATE_WS_ENABLED", "value": str(HA_STATE_WS_ENABLED).lower(), "secret": False},
                {"key": "HA_STATE_WS_MAX_FAILURES", "value": str(HA_STATE_WS_MAX_FAILURES), "secret": False},
                {"key": "HA_STATE_CACHE_TTL", "value": str(HA_STATE_CACHE_TTL), "secret": False},
                {"key": "HA_SERVICES_CACHE_TTL", "value": str(HA_SERVICES_CACHE_TTL), "secret": False},
                {"key": "SERVICE_CONFIRM_DELAY", "value": str(SERVICE_CONFIRM_DELAY), "secret": False},
                {"key": "SERVICE_CONFIRM_RETRIES", "value": str(SERVICE_CONFIRM_RETRIES), "secret": False},
                {"key": "SERVICE_CONFIRM_TIMEOUT", "value": str(SERVICE_CONFIRM_TIMEOUT), "secret": False},
                {"key": "EXCLUDED_DOMAINS", "value": ",".join(sorted(EXCLUDED_DOMAINS)), "secret": False},
                {"key": "EXCLUDED_DEVICE_KEYWORDS", "value": ",".join(sorted(EXCLUDED_DEVICE_KEYWORDS)), "secret": False},
                {"key": "DANGEROUS_SERVICE_DOMAINS", "value": ",".join(sorted(DANGEROUS_SERVICE_DOMAINS)), "secret": False},
                {"key": "LOG_LEVEL", "value": LOG_LEVEL, "secret": False},
                {"key": "DEFAULT_LIST_LIMIT", "value": str(DEFAULT_LIST_LIMIT), "secret": False},
                {"key": "MAX_LIST_LIMIT", "value": str(MAX_LIST_LIMIT), "secret": False},
                {"key": "WEBUI_HOST", "value": WEBUI_HOST, "secret": False},
                {"key": "WEBUI_PORT", "value": str(WEBUI_PORT), "secret": False},
            ],
            "note": "配置为只读视图，展示实际生效值；WebUI 无独立密码，直接使用 HA_URL / HA_TOKEN 连接 HA；修改请更新 .env 并重启服务。",
        }

    _devices_cache: tuple[float, dict[str, Any]] | None = None

    async def _devices_data_uncached() -> dict[str, Any]:
        """按物理设备聚合可控实体，供首页设备总览使用。"""
        if not (_env("HA_URL") and _env("HA_TOKEN")):
            return {
                "ok": False,
                "code": "config_missing",
                "message": "未配置 HA_URL/HA_TOKEN，无法加载设备。",
                "rows": [],
                "total": 0,
                "truncated": False,
            }
        try:
            result = await asyncio.wait_for(
                mcp_server.list_devices(
                    group_by="device",
                    controllable_only=True,
                    limit=MAX_LIST_LIMIT,
                ),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "code": "timeout",
                "message": "加载设备超时（Home Assistant 响应过慢）。",
                "rows": [],
                "total": 0,
                "truncated": False,
            }
        except HAError as exc:
            return {
                "ok": False,
                "code": exc.code,
                "message": "加载设备失败：" + exc.message,
                "rows": [],
                "total": 0,
                "truncated": False,
            }
        except Exception:  # noqa: BLE001
            log.exception("webui load devices failed")
            return {
                "ok": False,
                "code": "internal_error",
                "message": "加载设备失败，详见服务端日志。",
                "rows": [],
                "total": 0,
                "truncated": False,
            }
        if not result.get("ok"):
            return {
                "ok": False,
                "code": result.get("code", "error"),
                "message": str(result.get("message") or "加载设备失败。"),
                "rows": [],
                "total": 0,
                "truncated": False,
            }

        rows: list[dict[str, Any]] = []
        for group in result.get("devices") or []:
            if not isinstance(group, dict):
                continue
            group_area = str(group.get("area_name") or "")
            group_device = str(group.get("name") or "")
            for entity in group.get("entities") or []:
                if not isinstance(entity, dict):
                    continue
                actions = entity.get("actions") or []
                if not isinstance(actions, (list, tuple)):
                    actions = [actions]
                rows.append({
                    "area": str(entity.get("area_name") or group_area or "未分配区域"),
                    "device": str(entity.get("device_name") or group_device or "未命名设备"),
                    "name": str(entity.get("name") or entity.get("entity_id") or ""),
                    "entity_id": str(entity.get("entity_id") or ""),
                    "domain": str(entity.get("domain") or ""),
                    "type": _domain_label(str(entity.get("domain") or "")),
                    "state": str(entity.get("state") or "unknown"),
                    "unit": str(entity.get("unit") or ""),
                    "actions": [str(a) for a in actions],
                    "dangerous": bool(entity.get("dangerous")),
                })
        rows.sort(key=lambda item: (item["area"], item["type"], item["name"], item["entity_id"]))
        return {
            "ok": True,
            "rows": rows,
            "total": len(rows),
            "truncated": bool(result.get("has_more")),
            "message": str(result.get("message") or ""),
        }

    async def _devices_data(force: bool = False) -> dict[str, Any]:
        nonlocal _devices_cache
        if not force and _devices_cache is not None and time.monotonic() - _devices_cache[0] < WEBUI_CACHE_TTL:
            return _devices_cache[1]
        data = await _devices_data_uncached()
        _devices_cache = (time.monotonic(), data)
        return data

    def _logs_data(limit_raw: str, level_raw: str) -> tuple[dict[str, Any], int]:
        try:
            limit = int(limit_raw) if str(limit_raw).strip() else 200
        except (TypeError, ValueError):
            return {"ok": False, "code": "invalid_argument", "message": "limit 必须是整数。"}, 400
        if limit < 1 or limit > 500:
            return {"ok": False, "code": "invalid_argument", "message": "limit 必须在 1 到 500 之间。"}, 400
        level = (level_raw or "").strip().upper()
        if level == "ALL":
            level = ""
        if level and level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            return {"ok": False, "code": "invalid_argument", "message": "level 不合法。"}, 400
        return {
            "ok": True,
            "entries": read_recent_logs(limit=limit, level=level or None),
            "file": log_file_path(),
            "version": __version__,
            "level": level or "ALL",
            "limit": limit,
        }, 200

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/login", response_class=HTMLResponse)
    async def login_page() -> HTMLResponse:
        return HTMLResponse(_login_html())

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        token = str(form.get("token") or "")
        if not _token_ok(token):
            return HTMLResponse(_login_html("访问令牌不正确，请重试。"), status_code=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(_AUTH_COOKIE, _auth_cookie_value(), httponly=True, samesite="lax", path="/")
        return response

    @app.post("/logout")
    async def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(_AUTH_COOKIE, path="/")
        return response

    @app.get("/metrics")
    async def metrics_endpoint() -> PlainTextResponse:
        if not METRICS_ENABLED:
            return PlainTextResponse("metrics disabled\n", status_code=404)
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        status = await _status_data()
        ready = bool(status.get("ha", {}).get("configured") and status.get("ha", {}).get("reachable"))
        return JSONResponse({"ok": ready, "ha": status.get("ha")}, status_code=200 if ready else 503)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        status, devices = await asyncio.gather(_status_data(), _devices_data())
        logs = await asyncio.to_thread(read_recent_logs, limit=200)
        return HTMLResponse(_render_index(status, devices, logs, _config_data()))

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse(await _status_data())

    @app.get("/api/config")
    async def api_config() -> JSONResponse:
        return JSONResponse(_config_data())

    @app.get("/api/devices")
    async def api_devices() -> JSONResponse:
        return JSONResponse(await _devices_data())

    @app.get("/api/logs")
    async def api_logs(limit: str = "200", level: str = "") -> JSONResponse:
        data, status_code = await asyncio.to_thread(_logs_data, limit, level)
        return JSONResponse(data, status_code=status_code)

    @app.post("/tool", response_class=HTMLResponse)
    async def run_tool_form(request: Request) -> HTMLResponse:
        form = await request.form()
        name = str(form.get("name") or "")
        raw_params = str(form.get("params") or "").strip() or "{}"
        result, status = await _execute_tool(name, raw_params)
        return HTMLResponse(_render_tool_result(name, raw_params, result, status), status_code=status)

    @app.post("/api/tool/{name}")
    async def api_tool(name: str, request: Request) -> JSONResponse:
        raw = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw) + len(chunk) > _MAX_TOOL_BODY:
                    return JSONResponse(
                        {"ok": False, "code": "payload_too_large",
                         "message": "请求体超过 64KB 上限。"},
                        status_code=413,
                    )
                raw.extend(chunk)
        except Exception:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "code": "invalid_argument", "message": "读取请求体失败。"},
                status_code=400,
            )
        try:
            payload = json.loads(raw.decode("utf-8")) if raw and raw.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            return JSONResponse(
                {"ok": False, "code": "invalid_argument", "message": "请求体不是合法 JSON：" + str(exc)},
                status_code=400,
            )
        result, status = await _execute_tool(name, payload)
        return JSONResponse(result, status_code=status)

    async def _webui_danger_block(name: str, params: dict) -> dict | None:
        """WebUI 侧独立的高风险闸门，不依赖 DANGER_CONFIRM_ENABLED，也不接受确认令牌。"""
        if str(params.get("confirm_token") or "").strip():
            return {"ok": False, "code": "dangerous_operation_blocked",
                    "message": "WebUI 不执行带确认令牌的危险操作；请通过小智语音确认后再操作。"}
        service = str(params.get("service") or "").strip().lower()
        if "." in service:
            domain, svc = service.split(".", 1)
            dangerous, reason = service_is_dangerous(domain, svc)
            if dangerous:
                return {"ok": False, "code": "dangerous_operation_blocked",
                        "message": "WebUI 不执行高风险服务（" + reason + "）；请通过小智语音确认后再操作。"}
        candidates: list[str] = []
        entity = str(params.get("entity") or "").strip()
        if entity:
            candidates.append(entity)
        raw_entities = params.get("entities")
        if isinstance(raw_entities, list):
            candidates.extend(str(x).strip() for x in raw_entities if str(x).strip())
        for ref in candidates:
            # 安全判定必须失败关闭：HA 抖动、实体不存在或返回结构异常时都不能当作“非危险”。
            try:
                info = await mcp_server.get_device_state(entity=ref)
            except Exception as exc:  # noqa: BLE001
                log.warning("webui danger check failed entity=%s: %s", ref, exc)
                return {"ok": False, "code": "dangerous_operation_blocked",
                        "message": "无法确认设备是否安全（" + str(exc) + "），已拒绝执行。"}
            if not isinstance(info, dict) or info.get("ok") is not True:
                reason = info.get("message") if isinstance(info, dict) else "返回结构异常"
                return {"ok": False, "code": "dangerous_operation_blocked",
                        "message": "无法确认设备是否安全（" + str(reason or "查询失败") + "），已拒绝执行。"}
            view = info.get("entity")
            if not isinstance(view, dict):
                return {"ok": False, "code": "dangerous_operation_blocked",
                        "message": "无法确认设备是否安全（实体信息缺失），已拒绝执行。"}
            if view.get("dangerous"):
                reason = str(view.get("danger_reason") or "该设备属于高风险设备")
                return {"ok": False, "code": "dangerous_operation_blocked",
                        "message": "WebUI 不执行危险设备操作（" + reason + "）；请通过小智语音确认后再操作。"}
        return None

    async def _execute_tool(name: str, params: Any) -> tuple[Any, int]:
        fn = _TOOL_FUNCS.get(name)
        if fn is None:
            return {"ok": False, "code": "unknown_tool",
                    "message": "未知工具：" + str(name) + "。可用工具：" + "、".join(_TOOL_FUNCS)}, 404
        if isinstance(params, str):
            try:
                params = json.loads(params) if params.strip() else {}
            except (json.JSONDecodeError, RecursionError, ValueError) as exc:
                return {"ok": False, "code": "invalid_argument",
                        "message": "参数不是合法 JSON：" + str(exc)}, 400
        if not isinstance(params, dict):
            return {"ok": False, "code": "invalid_argument", "message": "参数必须是 JSON 对象。"}, 400
        try:
            inspect.signature(fn).bind(**params)
        except TypeError as exc:
            return {"ok": False, "code": "invalid_argument",
                    "message": "参数与工具签名不匹配：" + str(exc)}, 400
        if name in _DANGER_GATED_TOOLS:
            blocked = await _webui_danger_block(name, params)
            if blocked is not None:
                return blocked, 403
        try:
            result = await fn(**params)
        except Exception:  # noqa: BLE001
            log.exception("webui tool=%s failed", name)
            return {"ok": False, "code": "internal_error", "message": "工具调用失败，详见服务端日志。"}, 500
        # WebUI 无独立密码，不复用 MCP 的危险设备确认令牌，避免两次请求即可绕过确认。
        if isinstance(result, dict) and result.get("requires_confirmation"):
            return {"ok": False, "code": "dangerous_operation_blocked",
                    "message": "WebUI 不执行危险设备操作；请通过小智语音确认后再操作。"}, 403
        return result, 200

    return app



from .webui_page import _render_index, _render_tool_result

def main() -> None:
    setup_logging("webui")
    import uvicorn

    if WEBUI_TOKEN_GENERATED:
        log.warning("WEBUI_TOKEN 未设置，已生成临时访问令牌：%s（重启会变化，生产环境请在 .env 固定）", WEBUI_TOKEN)
    log.info("startup webui host=%s port=%s version=%s", WEBUI_HOST, WEBUI_PORT, __version__)
    uvicorn.run(create_app(), host=WEBUI_HOST, port=WEBUI_PORT, log_level="warning")


if __name__ == "__main__":
    main()
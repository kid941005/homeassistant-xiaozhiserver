# -*- coding: utf-8 -*-
"""WebUI：设备总览、运行状态、服务日志、高级工具调试。

直接使用 .env 中的 HA_URL / HA_TOKEN 连接 Home Assistant，不再设置独立 WebUI 密码：
    python -m homeassistant_xiaozhiserver.webui

安全约定：
- WebUI 无独立登录口令，请只绑定可信内网，不要直接暴露公网；
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
from fastapi.responses import HTMLResponse, JSONResponse

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
    LOG_LEVEL,
    MAX_LIST_LIMIT,
    SERVICE_CONFIRM_DELAY,
    SERVICE_CONFIRM_RETRIES,
    SERVICE_CONFIRM_TIMEOUT,
    WEBUI_ALLOWED_HOSTS,
)
from .danger import service_is_dangerous
from .logging_setup import log_file_path, read_recent_logs, setup_logging

log = logging.getLogger("homeassistant_xiaozhiserver.webui")

_STARTED_AT = time.time()
WEBUI_HOST = "0.0.0.0"
WEBUI_PORT = 8099
_MAX_TOOL_BODY = 64 * 1024

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
    query = "&".join(
        key + "=***" if key.lower() in ("token", "key", "secret", "password") else key + "=" + value
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

    @app.middleware("http")
    async def _same_origin(request: Request, call_next):
        """无独立密码时的纵深防御：校验 Host/Origin 并补充安全响应头。"""
        host = request.headers.get("host", "")
        if not _host_allowed(host):
            return _harden(JSONResponse(
                {"ok": False, "code": "host_not_allowed", "message": "Host 不在允许范围内。"},
                status_code=403,
            ))
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if not origin:
                return _harden(JSONResponse(
                    {"ok": False, "code": "cross_origin_blocked", "message": "写请求必须携带同源 Origin。"},
                    status_code=403,
                ))
            parts = urlsplit(origin)
            if parts.scheme not in ("http", "https") or parts.netloc.lower() != host.lower():
                return _harden(JSONResponse(
                    {"ok": False, "code": "cross_origin_blocked", "message": "拒绝跨站请求。"},
                    status_code=403,
                ))
        return _harden(await call_next(request))

    async def _status_data() -> dict[str, Any]:
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
            ],
            "note": "配置为只读视图，展示实际生效值；WebUI 无独立密码，直接使用 HA_URL / HA_TOKEN 连接 HA；修改请更新 .env 并重启服务。",
        }

    async def _devices_data() -> dict[str, Any]:
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
            try:
                info = await mcp_server.get_device_state(entity=ref)
            except Exception:  # noqa: BLE001
                info = None
            view = info.get("entity") if isinstance(info, dict) else None
            if isinstance(view, dict) and view.get("dangerous"):
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
            except json.JSONDecodeError as exc:
                return {"ok": False, "code": "invalid_argument",
                        "message": "参数不是合法 JSON：" + str(exc)}, 400
        if not isinstance(params, dict):
            return {"ok": False, "code": "invalid_argument", "message": "参数必须是 JSON 对象。"}, 400
        try:
            inspect.signature(fn).bind(**params)
        except TypeError as exc:
            return {"ok": False, "code": "invalid_argument",
                    "message": "参数与工具签名不匹配：" + str(exc)}, 400
        if name in ("control_device", "control_devices", "call_service"):
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


_PAGE_STYLE = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;margin:0;background:#f5f6f8;color:#1f2328}
header{background:#1f2937;color:#fff;padding:16px 24px}
header h1{margin:0;font-size:18px;font-weight:600}
header .brand{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
header .version{font-size:12px;font-weight:600;color:#bfdbfe;background:#374151;border:1px solid #4b5563;border-radius:999px;padding:2px 8px}
header .sub{margin-top:4px;font-size:12px;color:#cbd5e1}
.ver{font-size:11px;font-weight:600;color:#2563eb;background:#eff6ff;border-radius:999px;padding:1px 7px;margin-left:6px;vertical-align:middle}
.log-ver{color:#38bdf8;margin-right:8px}
main{max-width:1120px;margin:20px auto;padding:0 16px}
section{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px 20px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 12px;color:#111827}
.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:12px}
.section-head h2{margin:0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #eef0f3;vertical-align:top}
table.kv th{width:150px;color:#4b5563;font-weight:500}
table.device-table th{color:#4b5563;font-weight:500;white-space:nowrap}
table.device-table td{font-size:13px}
code,pre{font-family:ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',monospace;font-size:13px}
pre{background:#0f172a;color:#e2e8f0;padding:14px;border-radius:6px;overflow:auto;max-height:520px}
.ok{color:#047857;font-weight:600}.bad{color:#b91c1c;font-weight:600}.muted{color:#6b7280}
.badge{display:inline-block;padding:1px 7px;margin:1px 4px 1px 0;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;white-space:nowrap}
.badge.danger{background:#fee2e2;color:#991b1b}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.toolbar input[type=text]{flex:1;min-width:220px;padding:7px 10px;border:1px solid #d1d5db;border-radius:6px;font:inherit}
.toolbar select,.toolbar button{font:inherit;padding:7px 10px;border:1px solid #d1d5db;border-radius:6px}
.toolbar button{background:#2563eb;color:#fff;border:0;cursor:pointer;width:auto}
.toolbar button:hover{background:#1d4ed8}
.toolbar label.auto{display:inline-flex;align-items:center;gap:5px;font-size:13px;color:#4b5563}
.log-box{background:#0f172a;color:#e2e8f0;border-radius:6px;padding:10px 12px;max-height:360px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',monospace;font-size:12px;line-height:1.55}
.log-line{white-space:pre-wrap;word-break:break-word;padding:1px 0}
.log-time{color:#94a3b8;margin-right:10px}
.log-level{display:inline-block;min-width:58px;margin-right:8px;font-weight:600}
.log-name{color:#64748b;margin-right:8px}
.log-DEBUG .log-level{color:#94a3b8}
.log-INFO .log-level{color:#93c5fd}
.log-WARNING .log-level{color:#fbbf24}
.log-ERROR .log-level,.log-CRITICAL .log-level{color:#f87171}
details.advanced,details.params{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:14px 20px;margin-bottom:16px}
details.params{margin-top:-6px}
details summary{cursor:pointer;font-weight:600;color:#111827}
details[open] summary{margin-bottom:12px}
form{display:grid;gap:10px}
select,textarea,button{font:inherit;padding:8px 10px;border:1px solid #d1d5db;border-radius:6px}
textarea{min-height:120px;font-family:ui-monospace,Consolas,monospace}
button{background:#2563eb;color:#fff;border:0;cursor:pointer;width:120px}
button:hover{background:#1d4ed8}
small{color:#6b7280}
@media (max-width:760px){table.device-table th:nth-child(2),table.device-table td:nth-child(2){display:none}}
"""


def _page(title: str, body: str, script: str = "") -> str:
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>" + html.escape(title) + "</title><style>" + _PAGE_STYLE + "</style></head>"
        "<body><header><div class='brand'><h1>homeassistant-xiaozhiserver · WebUI</h1>"
        "<span class='version'>v" + html.escape(__version__) + "</span></div>"
        "<div class='sub'>设备总览 · 服务日志 · 高级工具调试</div></header>"
        "<main>" + body + "</main>" + script + "</body></html>"
    )


def _render_device_rows(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        return "<tr><td colspan='6' class='muted'>" + html.escape(str(data.get("message") or "加载失败")) + "</td></tr>"
    rows = data.get("rows") or []
    if not rows:
        return "<tr><td colspan='6' class='muted'>没有可控制设备。</td></tr>"
    out: list[str] = []
    for row in rows:
        actions = row.get("actions") or []
        badges = "".join("<span class='badge'>" + html.escape(str(action)) + "</span>" for action in actions)
        if not badges:
            badges = "<span class='muted'>—</span>"
        danger = " <span class='badge danger'>危险</span>" if row.get("dangerous") else ""
        state = html.escape(str(row.get("state") or "unknown"))
        unit = str(row.get("unit") or "")
        if unit:
            state += " " + html.escape(unit)
        if str(row.get("state")) == "unavailable":
            state = "<span class='bad'>" + state + "</span>"
        elif str(row.get("state")) == "unknown":
            state = "<span class='muted'>" + state + "</span>"
        out.append(
            "<tr>"
            "<td>" + html.escape(str(row.get("area") or "未分配区域")) + "</td>"
            "<td>" + html.escape(str(row.get("device") or "未命名设备")) + "</td>"
            "<td>" + html.escape(str(row.get("type") or "未知")) + danger
            + "<br><small>" + html.escape(str(row.get("domain") or "")) + "</small></td>"
            "<td>" + html.escape(str(row.get("name") or ""))
            + "<br><small><code>" + html.escape(str(row.get("entity_id") or "")) + "</code></small></td>"
            "<td>" + state + "</td>"
            "<td>" + badges + "</td>"
            "</tr>"
        )
    return "".join(out)


def _render_log_entries(entries: list[dict[str, str]]) -> str:
    if not entries:
        return "<div class='muted'>暂无日志。</div>"
    out: list[str] = []
    for entry in entries:
        level = str(entry.get("level") or "INFO").upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            level = "INFO"
        level = html.escape(level)
        version = str(entry.get("version") or "")
        version_html = ("<span class='log-ver'>v" + html.escape(version) + "</span>") if version else ""
        message = html.escape(str(entry.get("message") or "")).replace("\n", "<br>")
        out.append(
            "<div class='log-line log-" + level + "'>"
            "<span class='log-time'>" + html.escape(str(entry.get("time") or "")) + "</span>"
            "<span class='log-level'>" + level + "</span>"
            + version_html +
            "<span class='log-name'>[" + html.escape(str(entry.get("logger") or "")) + "]</span>"
            "<span class='log-msg'>" + message + "</span>"
            "</div>"
        )
    return "".join(out)


def _render_index(status: dict[str, Any], devices: dict[str, Any],
                  logs: list[dict[str, str]], config: dict[str, Any]) -> str:
    ha = status["ha"]
    if not ha["configured"]:
        ha_text = "<span class='bad'>未配置</span>"
    elif ha["reachable"] is True:
        ha_text = "<span class='ok'>正常</span> · " + html.escape(str(ha["url"]))
    elif ha["reachable"] is False:
        ha_text = "<span class='bad'>不可用</span>"
        if ha["error"]:
            ha_text += "（" + html.escape(str(ha["error"])) + "）"
        ha_text += " · " + html.escape(str(ha["url"]))
    else:
        ha_text = "<span class='muted'>检测中</span> · " + html.escape(str(ha["url"]))
    endpoint = status["mcp_endpoint"]
    endpoint_text = html.escape(endpoint["display"]) if endpoint["configured"] else "<span class='bad'>未配置</span>"

    status_rows = "".join(
        "<tr><th>" + html.escape(key) + "</th><td>" + value + "</td></tr>"
        for key, value in [
            ("版本", html.escape(status["version"])),
            ("启动时间", html.escape(status["started_at"])),
            ("运行时长", str(status["uptime_seconds"]) + " 秒"),
            ("HA 连通性", ha_text),
            ("MCP 接入点", endpoint_text),
            ("运行形态", html.escape(status["target"])),
        ]
    )
    param_items = [
        item for item in config["items"]
        if item["key"] not in ("HA_URL", "HA_TOKEN", "MCP_ENDPOINT")
    ]
    param_rows = "".join(
        "<tr><th>" + html.escape(item["key"]) + "</th><td>" + html.escape(str(item["value"])) + "</td></tr>"
        for item in param_items
    )
    options = "".join(
        "<option value='" + html.escape(tool["name"]) + "'>" + html.escape(tool["name"])
        + " — " + html.escape(tool["description"]) + "</option>"
        for tool in status["tools"]
    )
    examples = "".join(
        "<tr><th>" + html.escape(tool["name"]) + "</th><td><code>"
        + html.escape(tool["example"]) + "</code></td></tr>"
        for tool in status["tools"]
    )

    if devices.get("ok"):
        device_summary = "共 " + str(devices.get("total", 0)) + " 个可控实体"
        if devices.get("truncated"):
            device_summary += "（已截断，可用工具调试台按条件查询）"
    else:
        device_summary = str(devices.get("message") or "加载失败")

    body = (
        "<section><h2>运行状态</h2><table class='kv'>" + status_rows + "</table></section>"
        "<details class='params'><summary>运行参数（只读）</summary>"
        "<table class='kv'>" + param_rows + "</table>"
        "<p><small>" + html.escape(config["note"]) + "</small></p></details>"
        "<section><div class='section-head'><h2>可调用设备</h2>"
        "<span id='device-summary' class='muted'>" + html.escape(device_summary) + "</span></div>"
        "<div class='toolbar'>"
        "<input id='device-filter' type='text' placeholder='按区域、设备、类型、实体过滤…'>"
        "<button type='button' onclick='loadDevices()'>刷新设备</button>"
        "</div>"
        "<table class='device-table'><thead><tr>"
        "<th>区域</th><th>设备</th><th>类型</th><th>实体</th><th>状态</th><th>可执行动作</th>"
        "</tr></thead><tbody id='device-body'>" + _render_device_rows(devices) + "</tbody></table>"
        "<p><small>仅列出可控制实体；默认不展示门铃、自动化、热水壶、中枢网关与媒体。同一设备下与灯具同名且重复的开关会自动隐藏，只保留灯具。危险设备（门锁、安防、车库门等）操作仍需二次确认。</small></p></section>"
        "<section><div class='section-head'><h2>服务日志<span class='ver'>v" + html.escape(__version__) + "</span></h2>"
        "<span class='muted'>WebUI 与 bridge 共享日志，最近 200 条</span></div>"
        "<div class='toolbar'>"
        "<select id='log-level'><option value='ALL'>全部级别</option><option value='INFO'>INFO</option>"
        "<option value='WARNING'>WARNING</option><option value='ERROR'>ERROR</option>"
        "<option value='DEBUG'>DEBUG</option></select>"
        "<button type='button' onclick='loadLogs()'>刷新日志</button>"
        "<label class='auto'><input id='log-auto' type='checkbox' checked>自动刷新（5 秒）</label>"
        "</div>"
        "<div id='log-box' class='log-box'>" + _render_log_entries(logs) + "</div>"
        "<p><small>日志文件：" + html.escape(log_file_path()) + "</small></p></section>"
        "<details class='advanced'><summary>高级：工具调试台</summary>"
        "<p><small>一般不需要；设备列表与日志已覆盖日常使用。</small></p>"
        "<form method='post' action='/tool'>"
        "<label>选择工具<select name='name'>" + options + "</select></label>"
        "<label>参数（JSON 对象）<textarea name='params' spellcheck='false'>{}</textarea></label>"
        "<button type='submit'>调用</button>"
        "</form>"
        "<p><small>参数示例：</small></p><table>" + examples + "</table></details>"
    )
    return _page("homeassistant-xiaozhiserver WebUI", body, _INDEX_SCRIPT)


def _render_tool_result(name: str, params: str, result: Any, status: int) -> str:
    pretty = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    ok = not (isinstance(result, dict) and result.get("ok") is False)
    badge = ("<span class='ok'>成功</span>" if status == 200 and ok
             else "<span class='bad'>失败（HTTP " + str(status) + "）</span>")
    body = (
        "<section><h2>调用结果 " + badge + "</h2>"
        "<table><tr><th>工具</th><td><code>" + html.escape(name) + "</code></td></tr>"
        "<tr><th>参数</th><td><code>" + html.escape(params) + "</code></td></tr></table></section>"
        "<section><h2>返回</h2><pre>" + html.escape(pretty) + "</pre></section>"
        "<p><a href='/'>← 返回首页</a></p>"
    )
    return _page("调用结果", body)


_INDEX_SCRIPT = r"""
<script>
function cell(tag, text, cls) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  node.textContent = text == null ? '' : String(text);
  return node;
}
function showMessage(tbody, text) {
  tbody.innerHTML = '';
  var tr = document.createElement('tr');
  var td = cell('td', text, 'muted');
  td.colSpan = 6;
  tr.appendChild(td);
  tbody.appendChild(tr);
}
async function loadDevices() {
  var tbody = document.getElementById('device-body');
  var summary = document.getElementById('device-summary');
  showMessage(tbody, '加载中…');
  try {
    var resp = await fetch('/api/devices', {cache: 'no-store'});
    var data = await resp.json();
    tbody.innerHTML = '';
    if (!data.ok) {
      showMessage(tbody, data.message || '加载失败');
      summary.textContent = '';
      return;
    }
    if (!data.rows || !data.rows.length) {
      showMessage(tbody, '没有可控制设备。');
      summary.textContent = '共 0 个可控实体';
      return;
    }
    for (var i = 0; i < data.rows.length; i++) {
      var r = data.rows[i];
      var tr = document.createElement('tr');
      tr.appendChild(cell('td', r.area || '未分配区域'));
      tr.appendChild(cell('td', r.device || '未命名设备'));
      var typeTd = document.createElement('td');
      typeTd.appendChild(cell('span', r.type || r.domain || '未知'));
      if (r.dangerous) typeTd.appendChild(cell('span', '危险', 'badge danger'));
      typeTd.appendChild(document.createElement('br'));
      typeTd.appendChild(cell('small', r.domain || ''));
      tr.appendChild(typeTd);
      var entTd = document.createElement('td');
      entTd.appendChild(cell('div', r.name || r.entity_id || ''));
      entTd.appendChild(cell('small', r.entity_id || '', 'muted'));
      tr.appendChild(entTd);
      tr.appendChild(cell('td', (r.state || 'unknown') + (r.unit ? ' ' + r.unit : '')));
      var actTd = document.createElement('td');
      if (r.actions && r.actions.length) {
        for (var j = 0; j < r.actions.length; j++) actTd.appendChild(cell('span', r.actions[j], 'badge'));
      } else {
        actTd.appendChild(cell('span', '—', 'muted'));
      }
      tr.appendChild(actTd);
      tbody.appendChild(tr);
    }
    summary.textContent = '共 ' + data.total + ' 个可控实体' + (data.truncated ? '（已截断）' : '');
    applyDeviceFilter();
  } catch (err) {
    showMessage(tbody, '加载设备失败：' + err);
    summary.textContent = '';
  }
}
function applyDeviceFilter() {
  var input = document.getElementById('device-filter');
  if (!input) return;
  var q = (input.value || '').trim().toLowerCase();
  var rows = document.querySelectorAll('#device-body tr');
  for (var i = 0; i < rows.length; i++) {
    rows[i].style.display = (!q || rows[i].textContent.toLowerCase().indexOf(q) >= 0) ? '' : 'none';
  }
}
async function loadLogs() {
  var box = document.getElementById('log-box');
  var level = document.getElementById('log-level').value;
  try {
    var resp = await fetch('/api/logs?limit=200&level=' + encodeURIComponent(level), {cache: 'no-store'});
    var data = await resp.json();
    box.innerHTML = '';
    if (!data.ok) {
      box.appendChild(cell('div', data.message || '加载日志失败', 'muted'));
      return;
    }
    if (!data.entries || !data.entries.length) {
      box.appendChild(cell('div', '暂无日志。', 'muted'));
      return;
    }
    for (var i = 0; i < data.entries.length; i++) {
      var e = data.entries[i];
      var line = document.createElement('div');
      line.className = 'log-line log-' + (e.level || 'INFO');
      line.appendChild(cell('span', e.time || '', 'log-time'));
      line.appendChild(cell('span', e.level || '', 'log-level'));
      line.appendChild(cell('span', '[' + (e.logger || '') + ']', 'log-name'));
      line.appendChild(cell('span', e.message || '', 'log-msg'));
      box.appendChild(line);
    }
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    box.innerHTML = '';
    box.appendChild(cell('div', '加载日志失败：' + err, 'muted'));
  }
}
var filterInput = document.getElementById('device-filter');
if (filterInput) filterInput.addEventListener('input', applyDeviceFilter);
var levelSelect = document.getElementById('log-level');
if (levelSelect) levelSelect.addEventListener('change', loadLogs);
setInterval(function () {
  var auto = document.getElementById('log-auto');
  if (auto && auto.checked) loadLogs();
}, 5000);
</script>
"""


def main() -> None:
    setup_logging()
    import uvicorn

    log.info("startup webui host=%s port=%s version=%s", WEBUI_HOST, WEBUI_PORT, __version__)
    uvicorn.run(create_app(), host=WEBUI_HOST, port=WEBUI_PORT, log_level="warning")


if __name__ == "__main__":
    main()

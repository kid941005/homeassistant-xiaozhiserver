# -*- coding: utf-8 -*-
"""WebUI 冒烟测试：WEBUI_TOKEN 登录、状态、配置掩码、工具白名单，不需要真实 HA。

用法：python tests/webui_smoke.py
HA_URL/HA_TOKEN 若已导出则顺带验证真实工具调用，否则只验证未配置分支。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from homeassistant_xiaozhiserver import __version__  # noqa: E402
from homeassistant_xiaozhiserver.config import WEBUI_TOKEN  # noqa: E402
from homeassistant_xiaozhiserver.webui import _TOOLS, _mask_endpoint, create_app  # noqa: E402

client = TestClient(create_app(), base_url="http://127.0.0.1:8099")


def post(path: str, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("Origin", "http://127.0.0.1:8099")
    return client.post(path, headers=headers, **kwargs)


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print("[ok] " + name)
    else:
        print("[FAIL] " + name + ("" if not extra else " -> " + extra))
        sys.exit(1)


anon = TestClient(create_app(), base_url="http://127.0.0.1:8099")
r = anon.get("/api/status")
check("未登录访问 API 返回 401", r.status_code == 401 and r.json().get("code") == "unauthorized",
      str(r.status_code) + " " + r.text[:200])
r = anon.get("/", follow_redirects=False)
check("未登录访问首页跳转登录页", r.status_code in (302, 303, 307) and "/login" in r.headers.get("location", ""),
      str(r.status_code) + " " + str(r.headers.get("location")))
r = anon.get("/metrics")
check("未登录可访问 /metrics", r.status_code == 200 and r.headers.get("content-type", "").startswith("text/plain"),
      str(r.status_code) + " " + r.headers.get("content-type", ""))

r = post("/login", data={"token": "bad-token"})
check("错误令牌登录失败", r.status_code == 401 and "不正确" in r.text, str(r.status_code))
r = post("/login", data={"token": WEBUI_TOKEN})
check("正确令牌登录成功", r.status_code in (200, 303), str(r.status_code))

r = client.get("/")
check("无需登录访问首页 200", r.status_code == 200, str(r.status_code))
check("首页包含运行状态", "运行状态" in r.text)
check("首页包含可调用设备", "可调用设备" in r.text)
check("首页包含服务日志", "服务日志" in r.text)
check("首页含版本号", ("v" + __version__) in r.text, __version__)
check("首页包含折叠的工具调试台", "高级：工具调试台" in r.text)
check("首页不再重复展示只读配置表", "配置（只读" not in r.text)

r = client.get("/api/devices")
check("无需登录访问 /api/devices 200", r.status_code == 200, str(r.status_code))
devices = r.json()
check("devices 返回 rows 列表", isinstance(devices.get("rows"), list), str(type(devices.get("rows"))))
if os.environ.get("HA_URL") and os.environ.get("HA_TOKEN"):
    check("已配置 HA 时设备加载成功", devices.get("ok") is True, json.dumps(devices, ensure_ascii=False)[:300])
    if os.environ.get("FAKE_HA_TOKEN"):
        rows = devices.get("rows") or []
        ids = {row.get("entity_id") for row in rows}
        check("设备总览返回 5 个可控实体", devices.get("total") == 5 and len(rows) == 5,
              str(devices.get("total")) + " rows=" + str(len(rows)))
        check("设备总览不含被隐藏或去重实体",
              not ({"switch.doorbell", "light.hub_indicator", "switch.living_room_duplicate"} & ids),
              str(sorted(ids)))
else:
    check("未配置 HA 时设备返回 config_missing", devices.get("code") == "config_missing", json.dumps(devices, ensure_ascii=False)[:300])

r = client.get("/api/logs")
check("无需登录访问 /api/logs 200", r.status_code == 200, str(r.status_code))
logs = r.json()
check("logs 返回 entries 列表", isinstance(logs.get("entries"), list), str(type(logs.get("entries"))))
check("logs 返回日志文件路径", isinstance(logs.get("file"), str) and bool(logs.get("file")))
check("logs 含版本号", logs.get("version") == __version__, str(logs.get("version")))
entries = logs.get("entries") or []
check("日志条目带版本字段", all("version" in e for e in entries))
check("新日志条目含版本号", any(e.get("version") for e in entries))
r = client.get("/api/logs?limit=abc")
check("logs 非法 limit 返回 400", r.status_code == 400, str(r.status_code))
r = client.get("/api/logs?level=NOT_A_LEVEL")
check("logs 非法 level 返回 400", r.status_code == 400, str(r.status_code))

r = client.get("/api/status")
check("无需登录访问 /api/status 200", r.status_code == 200, str(r.status_code))
check("端点掩码覆盖 access_token", "access_token=***" in _mask_endpoint("ws://h:8004/x?access_token=abc"),
      _mask_endpoint("ws://h:8004/x?access_token=abc"))
status = r.json()
check("status.ok 为 True", status.get("ok") is True)
check("status 含版本号", isinstance(status.get("version"), str) and bool(status["version"]))
check("status 工具数为 7", len(status.get("tools") or []) == 7, str(len(status.get("tools") or [])))
bridge_status = status.get("bridge") or {}
check("status 含 bridge 连接状态", isinstance(bridge_status, dict) and "connected" in bridge_status, str(bridge_status))
ha_token_env = os.environ.get("HA_TOKEN", "")
check("HA_TOKEN 不回传明文", ha_token_env == "" or ha_token_env not in json.dumps(status, ensure_ascii=False))

r = client.get("/api/config")
check("无需登录访问 /api/config 200", r.status_code == 200, str(r.status_code))
config = r.json()
check("配置标注只读", "只读" in str(config.get("note", "")))
keys = {item["key"]: item["value"] for item in config.get("items", [])}
check("HA_TOKEN 掩码展示", keys.get("HA_TOKEN") in ("已设置（掩码）", "（未设置）"), str(keys.get("HA_TOKEN")))
_webui_public = {"WEBUI_HOST", "WEBUI_PORT", "WEBUI_ALLOWED_HOSTS"}
check(
    "配置无 WebUI 密码项",
    not any("PASSWORD" in key.upper()
            or ("WEBUI" in key.upper() and key.upper() not in _webui_public)
            for key in keys),
    str(sorted(keys)),
)
mcp_raw = os.environ.get("MCP_ENDPOINT", "")
if mcp_raw:
    from urllib.parse import parse_qsl, urlsplit
    token_value = dict(parse_qsl(urlsplit(mcp_raw).query)).get("token", "")
    masked_endpoint = str(keys.get("MCP_ENDPOINT", ""))
    check("MCP_ENDPOINT 不回传 token",
          bool(token_value) and token_value not in masked_endpoint,
          masked_endpoint)

r = client.get("/healthz")
check("健康检查 200", r.status_code == 200 and r.json().get("ok") is True, r.text[:200])
check("安全响应头存在", r.headers.get("X-Content-Type-Options") == "nosniff")

r = post("/api/tool/list_devices", json={"limit": "abc"})
check("非法分页参数返回 invalid_pagination",
      r.status_code == 200 and r.json().get("code") == "invalid_pagination", r.text[:200])

r = post("/api/tool/not_a_tool", json={})
check("未知工具返回 404", r.status_code == 404, str(r.status_code))
check("未知工具错误码", r.json().get("code") == "unknown_tool", str(r.json()))

r = post("/api/tool/list_domains", content="{bad json", headers={"Content-Type": "application/json"})
check("非法 JSON 返回 400", r.status_code == 400, str(r.status_code))

r = post("/api/tool/list_domains", json=[1, 2])
check("非对象参数返回 400", r.status_code == 400, str(r.status_code))
check("非对象参数错误码", r.json().get("code") == "invalid_argument", str(r.json()))

r = post("/tool", data={"name": "list_domains", "params": "[1,2]"})
check("表单非对象参数返回失败页", r.status_code == 400 and "失败" in r.text, str(r.status_code))

r = post("/api/tool/list_domains", json={}, headers={"Origin": "http://evil.example"})
check("跨站写请求被拒绝", r.status_code == 403 and r.json().get("code") == "cross_origin_blocked",
      str(r.status_code) + " " + r.text[:200])

if os.environ.get("HA_URL") and os.environ.get("HA_TOKEN"):
    r = post("/api/tool/list_domains", json={})
    check("list_domains 调用成功", r.status_code == 200 and r.json().get("ok") is True, str(r.status_code) + " " + r.text[:200])

    r = post("/api/tool/list_domains", json={}, headers={"Origin": "http://localhost:notaport"})
    check("畸形 Origin 端口被拒绝且不 500", r.status_code == 403 and r.json().get("code") == "cross_origin_blocked",
          str(r.status_code) + " " + r.text[:200])

    r = post("/api/tool/control_device", json={"entity": "大门门锁", "action": "unlock"})
    check("WebUI 拦截危险设备操作", r.status_code == 403 and r.json().get("code") == "dangerous_operation_blocked",
          str(r.status_code) + " " + r.text[:200])
    check("WebUI 不回传 confirm_token", "confirm_token" not in r.text, r.text[:200])

    if os.environ.get("FAKE_HA_TOKEN"):
        import homeassistant_xiaozhiserver.danger as danger_mod
        _old_enabled = danger_mod.DANGER_CONFIRM_ENABLED
        danger_mod.DANGER_CONFIRM_ENABLED = False
        try:
            r = post("/api/tool/control_device", json={"entity": "大门门锁", "action": "unlock"})
        finally:
            danger_mod.DANGER_CONFIRM_ENABLED = _old_enabled
        check("WebUI 危险闸门独立于 DANGER_CONFIRM_ENABLED",
              r.status_code == 403 and r.json().get("code") == "dangerous_operation_blocked",
              str(r.status_code) + " " + r.text[:200])

        import homeassistant_xiaozhiserver.server as mcp_server_mod
        _real_get_device_state = mcp_server_mod.get_device_state
        async def _boom(**kwargs):
            raise RuntimeError("ha unavailable")
        mcp_server_mod.get_device_state = _boom
        try:
            r = post("/api/tool/control_device", json={"entity": "switch.fan", "action": "turn_on"})
        finally:
            mcp_server_mod.get_device_state = _real_get_device_state
        check("WebUI 危险闸门查询异常时失败关闭",
              r.status_code == 403 and r.json().get("code") == "dangerous_operation_blocked",
              str(r.status_code) + " " + r.text[:200])

        async def _fake_script_state(**kwargs):
            return {"ok": True, "entity": {"entity_id": "script.test", "domain": "script",
                                           "name": "测试脚本", "dangerous": False}}
        mcp_server_mod.get_device_state = _fake_script_state
        try:
            r = post("/api/tool/call_service", json={"entity": "客厅灯", "service": "turn_on"})
        finally:
            mcp_server_mod.get_device_state = _real_get_device_state
        check("裸服务名按目标实体域判定脚本风险",
              r.status_code == 403 and r.json().get("code") == "dangerous_operation_blocked",
              str(r.status_code) + " " + r.text[:200])

    examples = {name: example for name, _, example in _TOOLS}
    r = post("/api/tool/call_service", json=json.loads(examples["call_service"]))
    check("工具调试台示例可执行", r.status_code == 200 and r.json().get("ok") is True,
          str(r.status_code) + " " + r.text[:200])
else:
    print("[skip] 未配置 HA_URL/HA_TOKEN，跳过真实工具调用")

print("SMOKE OK")

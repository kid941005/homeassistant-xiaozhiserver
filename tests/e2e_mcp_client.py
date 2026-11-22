#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：通过真实 MCP 客户端以 stdio 启动本服务并调用全部工具。

前置：先启动 fake_ha.py（见 tests/run_local_test.sh 或 README）。
本脚本会以子进程方式启动 server.py，环境变量 HA_URL / HA_TOKEN / HA_WS_URL 由外部提供。

本脚本对关键结果做断言；任一用例失败会以非 0 退出，便于 CI/回归。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = PROJECT_ROOT / "server.py"
EXPECTED_TOOLS = {
    "list_devices", "get_device_state", "control_device", "control_devices",
    "list_services", "call_service", "list_domains",
}
failures = []


def check(cond, label, detail=""):
    if cond:
        print("   [ok] " + label)
    else:
        msg = label + ((" | " + str(detail)) if detail else "")
        failures.append(msg)
        print("   [FAIL] " + msg)


async def call(session, name, args):
    res = await session.call_tool(name, args)
    text = ""
    for c in res.content:
        if getattr(c, "type", "") == "text":
            text += c.text
    try:
        data = json.loads(text)
    except Exception:
        data = text
    return {"isError": bool(res.is_error), "data": data}


def verify_case(index, name, args, r, expected):
    """校验通用断言集合；expected 里的特殊键见下方。"""
    d = r["data"]
    label = str(index) + " " + name + " " + json.dumps(args, ensure_ascii=False)
    check(isinstance(d, dict), label + " 返回 dict", type(d).__name__)
    if not isinstance(d, dict):
        return
    check(not r["isError"], label + " 不是 MCP isError", r["isError"])
    known_keys = {
        "ok", "applied_limit_gt", "applied_limit_eq", "status", "code", "total",
        "count", "grouped_by", "domain", "domain_count", "service_count", "limit",
        "has_more", "next_offset", "deduplicated_switch_count", "suggested_entity_id",
        "entity_id", "status_in", "service", "action", "device_action",
        "response_contains", "response_not_contains", "entity_state", "entity_area_id",
        "entity_area_name", "first_area_name", "first_domain", "entity_dangerous",
        "after_state", "after_brightness", "after_volume", "dangerous_domains_contains",
        "domains_not_contains", "dangerous_entities_contains", "attributes_truncated",
    }
    unknown = sorted(set(expected) - known_keys)
    check(not unknown, label + " 存在未知期望键", unknown)
    if "ok" in expected:
        check(d.get("ok") is expected["ok"], label + " ok", d.get("ok"))
    if "applied_limit_gt" in expected:
        applied_limit = (d.get("applied_defaults") or {}).get("limit")
        check(isinstance(applied_limit, int) and applied_limit > expected["applied_limit_gt"],
              label + " applied_defaults.limit > " + str(expected["applied_limit_gt"]),
              applied_limit)
    if "applied_limit_eq" in expected:
        applied_limit = (d.get("applied_defaults") or {}).get("limit")
        check(applied_limit == expected["applied_limit_eq"],
              label + " applied_defaults.limit == " + str(expected["applied_limit_eq"]),
              applied_limit)
    for key in ("status", "code", "total", "count", "grouped_by", "domain",
                "domain_count", "service_count", "limit", "has_more", "next_offset",
                "deduplicated_switch_count"):
        if key in expected:
            check(d.get(key) == expected[key], label + " " + key, d.get(key))
    if "suggested_entity_id" in expected:
        check(d.get("suggested_entity_id") == expected["suggested_entity_id"],
              label + " suggested_entity_id", d.get("suggested_entity_id"))
    if "entity_id" in expected:
        check(d.get("entity_id") == expected["entity_id"], label + " entity_id", d.get("entity_id"))
    if "status_in" in expected:
        check(d.get("status") in expected["status_in"], label + " status_in",
              d.get("status"))
    if "service" in expected:
        check(d.get("service") == expected["service"], label + " service",
              d.get("service"))
    if "action" in expected:
        check(d.get("action") == expected["action"], label + " action", d.get("action"))
    if "device_action" in expected:
        check(d.get("device_action") == expected["device_action"],
              label + " device_action", d.get("device_action"))
    if "response_contains" in expected:
        resp = str(d.get("response") or "")
        check(expected["response_contains"] in resp,
              label + " response contains " + expected["response_contains"], resp)
    if "response_not_contains" in expected:
        resp = str(d.get("response") or "")
        check(expected["response_not_contains"] not in resp,
              label + " response not contains " + expected["response_not_contains"], resp)
    if "entity_state" in expected:
        check((d.get("entity") or {}).get("state") == expected["entity_state"],
              label + " entity.state", (d.get("entity") or {}).get("state"))
    if "entity_area_id" in expected:
        check((d.get("entity") or {}).get("area_id") == expected["entity_area_id"],
              label + " entity.area_id", (d.get("entity") or {}).get("area_id"))
    if "entity_area_name" in expected:
        check((d.get("entity") or {}).get("area_name") == expected["entity_area_name"],
              label + " entity.area_name", (d.get("entity") or {}).get("area_name"))
    if "first_area_name" in expected:
        devices = d.get("devices") or []
        check(bool(devices) and devices[0].get("area_name") == expected["first_area_name"],
              label + " devices[0].area_name", (devices[0] if devices else {}).get("area_name"))
    if "first_domain" in expected:
        devices = d.get("devices") or []
        check(bool(devices) and devices[0].get("domain") == expected["first_domain"],
              label + " devices[0].domain", (devices[0] if devices else {}).get("domain"))
    if "entity_dangerous" in expected:
        check((d.get("entity") or {}).get("dangerous") is expected["entity_dangerous"],
              label + " entity.dangerous", (d.get("entity") or {}).get("dangerous"))
    if "after_state" in expected:
        check((d.get("after") or {}).get("state") == expected["after_state"],
              label + " after.state", (d.get("after") or {}).get("state"))
    if "after_brightness" in expected:
        check((d.get("after") or {}).get("brightness") == expected["after_brightness"],
              label + " after.brightness", (d.get("after") or {}).get("brightness"))
    if "after_volume" in expected:
        check((d.get("after") or {}).get("volume_level") == expected["after_volume"],
              label + " after.volume_level", (d.get("after") or {}).get("volume_level"))
    if "dangerous_domains_contains" in expected:
        check(expected["dangerous_domains_contains"] in (d.get("dangerous_domains") or []),
              label + " dangerous_domains", d.get("dangerous_domains"))
    if "domains_not_contains" in expected:
        domains = d.get("domains") or {}
        check(all(k not in domains for k in expected["domains_not_contains"]),
              label + " hidden domains", [k for k in expected["domains_not_contains"] if k in domains])
    if "dangerous_entities_contains" in expected:
        ids = [e.get("entity_id") for e in (d.get("dangerous_entities") or [])]
        check(expected["dangerous_entities_contains"] in ids,
              label + " dangerous_entities", ids)
    if "attributes_truncated" in expected:
        check((d.get("entity") or {}).get("attributes_truncated") is expected["attributes_truncated"],
              label + " entity.attributes_truncated",
              (d.get("entity") or {}).get("attributes_truncated"))


def _server_params():
    env = os.environ.copy()
    # 子进程通过 server.py 启动；显式 cwd 保证包导入与 .env 行为稳定
    return StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        env=env,
        cwd=str(PROJECT_ROOT),
    )


async def main():
    async with Client(_server_params()) as session:
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        print("TOOLS:", names)
        check(set(names) == EXPECTED_TOOLS, "工具清单为 7 个预期工具", names)

        cases = [
            # 列表 / 过滤 / 分页 / 聚合
            # 无过滤条件：默认只返回可控制设备且每页 DEFAULT_LIST_LIMIT 条（可见 6 个实体中 5 个可控）
            ("list_devices", {}, {"ok": True, "total": 5, "count": 5,
                                   "grouped_by": "entity",
                                   "applied_limit_eq": 50}),
            ("list_devices", {"domain": "light"}, {"ok": True, "total": 1}),
            ("list_devices", {"keyword": "客厅"}, {"ok": True, "total": 3}),
            ("list_devices", {"keyword": "客厅灯"},
             {"ok": True, "total": 1, "first_domain": "light",
              "deduplicated_switch_count": 1}),
            ("list_devices", {"compact": True}, {"ok": True, "total": 5}),
            ("list_devices", {"area": "客厅"}, {"ok": True, "total": 5,
                                                "first_area_name": "客厅"}),
            ("list_devices", {"area": "玄关"}, {"ok": True, "total": 1}),
            ("list_devices", {"area": "客厅", "group_by": "device"},
             {"ok": True, "total": 4, "grouped_by": "device"}),
            ("list_devices", {"area": "客厅", "domain": "light"},
             {"ok": True, "total": 1, "first_domain": "light"}),
            ("list_devices", {"area": "客厅", "domain": "switch"},
             {"ok": True, "total": 1, "first_domain": "switch"}),
            # 用户指定隐藏：门铃/自动化/热水壶/中枢网关/媒体
            ("list_devices", {"keyword": "门铃"}, {"ok": True, "total": 0}),
            ("list_devices", {"keyword": "水壶"}, {"ok": True, "total": 0}),
            ("list_devices", {"keyword": "中枢网关"}, {"ok": True, "total": 0}),
            ("list_devices", {"domain": "automation"}, {"ok": True, "total": 0}),
            ("list_devices", {"domain": "media_player"}, {"ok": True, "total": 0}),
            ("list_devices", {"area": "不存在"}, {"ok": True, "total": 0}),
            ("list_devices", {"controllable_only": True}, {"ok": True, "total": 5}),
            ("list_devices", {"controllable_only": False}, {"ok": True, "total": 6, "count": 6}),
            ("list_devices", {"limit": 0}, {"ok": True, "total": 5, "count": 5}),
            ("list_devices", {"offset": -3}, {"ok": True, "total": 5, "count": 5}),
            # 分页上限/续拉：limit 超上限被截断，offset 第二页，非法值显式报错
            ("list_devices", {"limit": 10000}, {"ok": True, "limit": 500, "count": 5}),
            ("list_devices", {"limit": 3}, {"ok": True, "count": 3, "has_more": True,
                               "next_offset": 3}),
            ("list_devices", {"offset": 4}, {"ok": True, "count": 1, "has_more": False}),
            # 关键字别名 query/q/search/name 等价于 keyword
            ("list_devices", {"query": "客厅"}, {"ok": True, "total": 3}),
            ("list_devices", {"q": "客厅"}, {"ok": True, "total": 3}),
            ("list_devices", {"search": "客厅"}, {"ok": True, "total": 3}),
            ("list_devices", {"name": "客厅"}, {"ok": True, "total": 3}),
            # 未知参数 / 冲突别名必须显式报错，而不是静默忽略
            ("list_devices", {"bogus": 1}, {"ok": False, "code": "invalid_argument"}),
            ("list_devices", {"query": "客厅", "keyword": "灯"},
             {"ok": False, "code": "invalid_argument"}),
            ("list_devices", {"group_by": "device"}, {"ok": True, "total": 5,
                                                       "grouped_by": "device"}),
            ("list_devices", {"keyword": "客厅", "group_by": "device"},
             {"ok": True, "total": 2, "grouped_by": "device"}),
            ("list_devices", {"group_by": "bogus"},
             {"ok": False, "code": "invalid_group_by"}),
            ("list_domains", {}, {"ok": True, "dangerous_domains_contains": "lock",
                                   "dangerous_entities_contains": "lock.front_door",
                                   "deduplicated_switch_count": 1,
                                   "domains_not_contains": ["automation", "media_player"]}),
            # 能力自描述
            ("list_services", {}, {"ok": True, "domain_count": 5, "service_count": 15}),
            ("list_services", {"domain": "light"}, {"ok": True, "domain": "light"}),
            ("list_services", {"domain": "media_player"}, {"ok": False, "code": "no_services"}),
            # 状态查询
            ("get_device_state", {"entity": "客厅灯"},
             {"ok": True, "entity_state": "off", "entity_area_id": "living_room",
              "entity_area_name": "客厅"}),
            ("get_device_state", {"entity": "sensor.living_temp"},
             {"ok": True, "entity_state": "24.5"}),
            ("get_device_state", {"entity": "大门门锁"},
             {"ok": True, "entity_dangerous": True}),
            ("get_device_state", {"entity": "sensor.living_temp"},
             {"ok": True, "attributes_truncated": True}),
            ("get_device_state", {"entity": "sensor.living_temp", "include_all_attributes": True},
             {"ok": True, "attributes_truncated": False}),
            ("get_device_state", {"entity": "电视"}, {"ok": False, "code": "unsupported_device"}),
            ("get_device_state", {"entity": "智能门铃"}, {"ok": False, "code": "unsupported_device"}),
            ("get_device_state", {"entity": "switch.living_room_duplicate"},
             {"ok": False, "code": "duplicate_switch",
              "entity_id": "switch.living_room_duplicate",
              "suggested_entity_id": "light.living_room"}),
            # 常用控制
            ("control_device", {"entity": "客厅灯", "action": "set_brightness", "value": 50},
             {"ok": True, "status": "confirmed", "after_brightness": 127,
              "action": "RESPONSE", "device_action": "set_brightness",
              "response_contains": "亮度", "response_not_contains": "turn_off"}),
            ("control_device", {"entity": "switch.fan", "action": "turn_on"},
             {"ok": True, "status": "confirmed", "after_state": "on",
              "action": "RESPONSE", "device_action": "turn_on",
              "response_contains": "已打开"}),
            ("control_device", {"entity": "switch.fan", "action": "turn_off"},
             {"ok": True, "status": "confirmed", "after_state": "off",
              "action": "RESPONSE", "device_action": "turn_off",
              "response_contains": "已关闭", "response_not_contains": "turn_off"}),
            ("control_device", {"entity": "空调", "action": "set_temperature", "value": 26},
             {"ok": True}),
            ("control_device", {"entity": "窗帘", "action": "turn_on"},
             {"ok": True, "after_state": "open"}),
            ("control_device", {"entity": "窗帘", "action": "stop"},
             {"ok": True, "service": "cover.stop_cover"}),
            ("control_device", {"entity": "电视", "action": "set_volume", "value": 30},
             {"ok": False, "code": "unsupported_device"}),
            ("control_device", {"entity": "智能门铃", "action": "turn_on"},
             {"ok": False, "code": "unsupported_device"}),
            ("control_device", {"entity": "switch.living_room_duplicate", "action": "turn_on"},
             {"ok": False, "code": "duplicate_switch",
              "suggested_entity_id": "light.living_room"}),
            ("control_devices", {"entities": ["switch.fan", "客厅灯"], "action": "turn_on"},
             {"ok": True, "count": 2, "action": "RESPONSE", "response_contains": "已打开"}),
            ("control_devices", {"entities": ["switch.fan", "不存在设备"], "action": "turn_off"},
             {"ok": True, "count": 1, "action": "RESPONSE", "response_contains": "已关闭"}),
            ("control_devices", {"entities": [], "action": "turn_on"},
             {"ok": False, "code": "invalid_argument"}),
            ("control_devices", {"entities": ["sensor.living_temp"], "action": "turn_on"},
             {"ok": False, "code": "no_valid_entities"}),
            # 边界与错误
            ("control_device", {"entity": "sensor.living_temp", "action": "turn_on"},
             {"ok": False, "code": "not_controllable"}),
            ("control_device", {"entity": "不存在设备", "action": "turn_on"},
             {"ok": False, "code": "not_found"}),
            ("control_device", {"entity": "客厅灯", "action": "fly"},
             {"ok": False, "code": "unsupported_action"}),
            ("control_device", {"entity": "客厅灯", "action": "lock"},
             {"ok": False, "code": "unsupported_action"}),
            # 通用服务调用
            ("call_service", {"entity": "客厅灯", "service": "light.turn_on",
                              "data": {"brightness_pct": 50}}, {"ok": True}),
            ("call_service", {"entity": "switch.fan", "service": "switch.turn_on"},
             {"ok": True, "action": "RESPONSE", "device_action": "turn_on",
              "response_contains": "已打开", "response_not_contains": "turn_on"}),
            ("call_service", {"entity": "switch.fan", "service": "turn_off"},
             {"ok": True, "action": "RESPONSE", "device_action": "turn_off",
              "response_contains": "已关闭", "response_not_contains": "turn_off"}),
            ("call_service", {"entity": "switch.fan", "service": "turn_off"},
             {"ok": True, "status": "no_change", "action": "RESPONSE",
              "response_contains": "本来就是关着"}),
            ("call_service", {"entity": "switch.fan", "service": "silent"},
             {"ok": True, "status": "accepted_pending"}),
            ("call_service", {"entity": "不存在设备", "service": "turn_on"},
             {"ok": False, "code": "not_found"}),
            ("call_service", {"entity": "客厅灯", "service": "light.turn_on",
                              "data": {"entity_id": "switch.fan"}},
             {"ok": False, "code": "invalid_data"}),
            ("call_service", {"entity": "客厅灯", "service": "light.turn_on",
                              "data": {"rgb_color": [300, 0, 0]}},
             {"ok": False, "code": "invalid_data"}),
            ("control_device", {"entity": "客厅灯", "action": "set_color", "value": "#ff0000"},
             {"ok": True}),
            # P2：延迟生效与属性不生效
            ("call_service", {"entity": "客厅灯", "service": "light.turn_off_delayed"},
             {"ok": True, "status": "confirmed", "after_state": "off"}),
            ("call_service", {"entity": "客厅灯", "service": "light.turn_on_ignore_brightness",
                              "data": {"brightness_pct": 80}},
             {"ok": True, "status": "mismatch"}),
        ]
        for index, (name, args, expected) in enumerate(cases, 1):
            r = await call(session, name, args)
            d = r["data"]
            print("\n== " + name + " " + json.dumps(args, ensure_ascii=False))
            print("   result=" + json.dumps(d, ensure_ascii=False)[:1000])
            verify_case(index, name, args, r, expected)

        # 精简返回体：compact 只保留关键字段
        compact = await call(session, "list_devices", {"compact": True})
        compact_data = compact["data"] if isinstance(compact["data"], dict) else {}
        compact_views = compact_data.get("devices") or []
        first_view = compact_views[0] if compact_views else {}
        check(bool(compact_views) and "attributes" not in first_view
              and "controllable" in first_view and "entity_id" in first_view,
              "compact 仅保留关键字段", sorted(first_view.keys()))

        # P3：危险设备二次确认完整流程
        print("\n== P3 危险设备二次确认 ==")
        first = await call(session, "control_device", {"entity": "大门门锁", "action": "unlock"})
        d1 = first["data"] if isinstance(first["data"], dict) else {}
        token = d1.get("confirm_token")
        print("first=" + json.dumps(d1, ensure_ascii=False)[:1000])
        check(d1.get("code") == "confirmation_required", "P3 首次 confirmation_required", d1.get("code"))
        check(d1.get("dangerous") is True, "P3 首次 dangerous=true", d1.get("dangerous"))
        check(bool(token), "P3 首次返回 confirm_token", token)
        check("action" not in d1, "P3 确认分支不带 action", d1.get("action"))
        second = await call(session, "control_device", {"entity": "大门门锁", "action": "unlock",
                                                       "confirm_token": token})
        d2 = second["data"] if isinstance(second["data"], dict) else {}
        check(d2.get("ok") is True and d2.get("status") == "confirmed",
              "P3 带 token 执行成功", d2)
        check((d2.get("after") or {}).get("state") == "unlocked",
              "P3 门锁已解锁", d2.get("after"))
        check(d2.get("action") == "RESPONSE" and d2.get("device_action") == "unlock",
              "P3 成功控制走 RESPONSE", {"action": d2.get("action"), "device_action": d2.get("device_action")})
        check("已解锁" in str(d2.get("response") or "") and "unlock" not in str(d2.get("response") or ""),
              "P3 播报为中文且不含协议名", d2.get("response"))
        reuse = await call(session, "control_device", {"entity": "大门门锁", "action": "unlock",
                                                      "confirm_token": token})
        d3 = reuse["data"] if isinstance(reuse["data"], dict) else {}
        check(d3.get("code") == "invalid_confirm_token", "P3 复用令牌被拒", d3.get("code"))
        bad = await call(session, "control_device", {"entity": "大门门锁", "action": "lock",
                                                    "confirm_token": "bad-token"})
        d4 = bad["data"] if isinstance(bad["data"], dict) else {}
        check(d4.get("code") == "invalid_confirm_token", "P3 无效令牌被拒", d4.get("code"))
        req = await call(session, "call_service", {"entity": "大门门锁", "service": "lock.lock"})
        d5 = req["data"] if isinstance(req["data"], dict) else {}
        tok2 = d5.get("confirm_token")
        check(d5.get("code") == "confirmation_required", "P3 call_service 也需确认", d5.get("code"))
        mismatch = await call(session, "call_service", {"entity": "大门门锁", "service": "lock.unlock",
                                                       "confirm_token": tok2})
        d6 = mismatch["data"] if isinstance(mismatch["data"], dict) else {}
        check(d6.get("code") == "confirm_token_mismatch", "P3 意图不匹配被拒", d6.get("code"))
        # 普通实体上的脚本/系统级服务也必须二次确认，不能借实体域绕过危险判定
        script = await call(session, "call_service", {"entity": "客厅灯", "service": "script.turn_on"})
        d7 = script["data"] if isinstance(script["data"], dict) else {}
        check(d7.get("code") == "confirmation_required",
              "P3 普通实体上的脚本服务也需确认", d7.get("code"))

    if failures:
        print("\n===== FAILED " + str(len(failures)) + " 项 =====")
        for f in failures:
            print(" - " + f)
        return 1
    print("\n===== ALL CHECKS PASSED =====")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

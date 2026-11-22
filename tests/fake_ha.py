#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 Home Assistant 模拟器，仅用于离线自测本 MCP 服务。

只实现本服务用到的 /api 子集，并刻意复刻 HA 的真实行为：
对「不存在的实体」调用 service 也返回 200 []（静默假成功），以验证回读复核逻辑。

运行：FAKE_HA_TOKEN=yourpass python tests/fake_ha.py
（默认监听 127.0.0.1:8123；令牌通过 FAKE_HA_TOKEN 指定，缺省 devtoken）
"""
import asyncio
import copy
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import websockets
except Exception:  # noqa: BLE001
    websockets = None

TOKEN = os.environ.get("FAKE_HA_TOKEN", "devtoken")
PORT = int(os.environ.get("FAKE_HA_PORT", "8123"))
WS_PORT = int(os.environ.get("FAKE_HA_WS_PORT", "8124"))
BIND = os.environ.get("FAKE_HA_BIND", "127.0.0.1")

STATES = {
    "light.living_room": {
        "entity_id": "light.living_room", "state": "off",
        "attributes": {"friendly_name": "客厅灯", "brightness": None,
                       "color_temp_kelvin": 2700, "rgb_color": [255, 255, 255]},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "switch.fan": {
        "entity_id": "switch.fan", "state": "off",
        "attributes": {"friendly_name": "电风扇"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "switch.living_room_duplicate": {
        "entity_id": "switch.living_room_duplicate", "state": "off",
        "attributes": {"friendly_name": "客厅灯"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "sensor.living_temp": {
        "entity_id": "sensor.living_temp", "state": "24.5",
        "attributes": {"friendly_name": "客厅温度", "unit_of_measurement": "°C", "device_class": "temperature", "extra_debug": {"a": 1}},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "climate.ac": {
        "entity_id": "climate.ac", "state": "off",
        "attributes": {"friendly_name": "空调", "temperature": 26, "current_temperature": 28, "hvac_mode": "off"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "media_player.tv": {
        "entity_id": "media_player.tv", "state": "idle",
        "attributes": {"friendly_name": "电视", "volume_level": 0.3, "is_volume_muted": False,
                       "source_list": ["HDMI1", "HDMI2"], "extra_debug": {"a": 1}},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "switch.doorbell": {
        "entity_id": "switch.doorbell", "state": "off",
        "attributes": {"friendly_name": "智能门铃 开关"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "switch.kettle": {
        "entity_id": "switch.kettle", "state": "on",
        "attributes": {"friendly_name": "米家恒温电水壶3Pro 热水壶 开关"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "light.hub_indicator": {
        "entity_id": "light.hub_indicator", "state": "off",
        "attributes": {"friendly_name": "Xiaomi 中枢网关 指示灯"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "automation.guan_ji": {
        "entity_id": "automation.guan_ji", "state": "off",
        "attributes": {"friendly_name": "关机"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "cover.curtain": {
        "entity_id": "cover.curtain", "state": "closed",
        "attributes": {"friendly_name": "窗帘", "current_position": 0},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "lock.front_door": {
        "entity_id": "lock.front_door", "state": "locked",
        "attributes": {"friendly_name": "大门门锁"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
}

# 延迟生效队列：模拟设备状态在服务调用后若干次轮询才真正变化
PENDING = {}
# 已订阅 state_changed 的 WebSocket 连接；HTTP 线程通过 WS_LOOP 投递事件。
WS_CLIENTS = set()
WS_LOOP = None


def _snapshot_state(eid):
    st = STATES[eid]
    return {"entity_id": eid, "state": st.get("state"),
            "attributes": copy.deepcopy(st.get("attributes") or {}),
            "last_changed": st.get("last_changed"), "last_updated": st.get("last_updated")}


def _broadcast_state_change(eid, old_state):
    """从 HTTP 线程把 state_changed 事件推送给所有订阅连接。"""
    if WS_LOOP is None or not WS_CLIENTS:
        return
    payload = json.dumps({
        "id": None, "type": "event",
        "event": {"event_type": "state_changed", "data": {
            "entity_id": eid, "old_state": old_state, "new_state": _snapshot_state(eid)}},
    }, ensure_ascii=False)

    def _send():
        for ws in list(WS_CLIENTS):
            WS_LOOP.create_task(ws.send(payload))

    WS_LOOP.call_soon_threadsafe(_send)


SERVICES = [
    {"domain": "light", "services": {
        "turn_on": {"name": "打开", "description": "Turn on a light", "fields": {
            "brightness_pct": {"required": False, "selector": {"number": {"min": 0, "max": 100}}, "example": 50},
            "color_temp_kelvin": {"required": False, "selector": {"number": {}}},
            "rgb_color": {"required": False, "selector": {"color_rgb": {}}},
        }},
        "turn_off": {"name": "关闭", "description": "Turn off a light", "fields": {}},
        "toggle": {"name": "切换", "description": "Toggle a light", "fields": {}},
    }},
    {"domain": "switch", "services": {
        "turn_on": {"name": "打开", "description": "Turn on a switch", "fields": {}},
        "turn_off": {"name": "关闭", "description": "Turn off a switch", "fields": {}},
        "toggle": {"name": "切换", "description": "Toggle a switch", "fields": {}},
    }},
    {"domain": "climate", "services": {
        "turn_on": {"name": "打开", "description": "Turn on climate", "fields": {}},
        "turn_off": {"name": "关闭", "description": "Turn off climate", "fields": {}},
        "set_temperature": {"name": "设定温度", "description": "Set target temperature", "fields": {
            "temperature": {"required": True, "selector": {"number": {"min": 5, "max": 35}}, "example": 26},
        }},
        "set_hvac_mode": {"name": "设定模式", "description": "Set HVAC mode", "fields": {
            "hvac_mode": {"required": True, "selector": {"select": {"options": ["off", "heat", "cool", "auto"]}}},
        }},
    }},
    {"domain": "media_player", "services": {
        "volume_set": {"name": "设定音量", "description": "Set volume", "fields": {
            "volume_level": {"required": True, "selector": {"number": {"min": 0, "max": 1}}, "example": 0.3},
        }},
        "volume_mute": {"name": "静音", "description": "Mute or unmute", "fields": {
            "is_volume_muted": {"required": True, "selector": {"boolean": {}}},
        }},
        "media_play": {"name": "播放", "description": "Play media", "fields": {}},
        "media_pause": {"name": "暂停", "description": "Pause media", "fields": {}},
        "media_stop": {"name": "停止", "description": "Stop media", "fields": {}},
    }},
    {"domain": "cover", "services": {
        "open_cover": {"name": "打开", "description": "Open the cover", "fields": {}},
        "close_cover": {"name": "关闭", "description": "Close the cover", "fields": {}},
        "stop_cover": {"name": "停止", "description": "Stop the cover", "fields": {}},
    }},
    {"domain": "lock", "services": {
        "lock": {"name": "上锁", "description": "Lock the door", "fields": {}},
        "unlock": {"name": "开锁", "description": "Unlock the door", "fields": {}},
    }},
]


# 设备注册表 / 实体注册表 / 区域注册表（复刻 HA WebSocket API config/*_registry/list 的返回结构）
DEVICE_REGISTRY = [
    {"id": "dev_living", "name": "客厅环境组", "name_by_user": None, "manufacturer": "DemoTech",
     "model": "Living Kit", "model_id": "LK-1", "area_id": "living_room", "disabled_by": None},
    {"id": "dev_climate", "name": "客厅空调", "name_by_user": None, "manufacturer": "CoolCo",
     "model": "AC-1", "model_id": "AC-1", "area_id": "living_room", "disabled_by": None},
    {"id": "dev_fan", "name": "电风扇", "name_by_user": None, "manufacturer": "WindCo",
     "model": "F1", "model_id": "F1", "area_id": "living_room", "disabled_by": None},
    {"id": "dev_tv", "name": "电视", "name_by_user": None, "manufacturer": "Vision",
     "model": "TV-4K", "model_id": "TV4K", "area_id": "living_room", "disabled_by": None},
    {"id": "dev_doorbell", "name": "智能门铃", "name_by_user": None, "manufacturer": "DemoTech",
     "model": "Doorbell", "model_id": "DB-1", "area_id": "living_room", "disabled_by": None},
    {"id": "dev_kettle", "name": "米家恒温电水壶3Pro", "name_by_user": None, "manufacturer": "DemoTech",
     "model": "Kettle", "model_id": "K-1", "area_id": "living_room", "disabled_by": None},
    {"id": "dev_hub", "name": "Xiaomi 中枢网关", "name_by_user": None, "manufacturer": "DemoTech",
     "model": "Hub", "model_id": "H-1", "area_id": "living_room", "disabled_by": None},
    {"id": "dev_curtain", "name": "窗帘", "name_by_user": None, "manufacturer": "HomeAuto",
     "model": "C1", "model_id": "C1", "area_id": "living_room", "disabled_by": None},
    {"id": "dev_lock", "name": "大门门锁", "name_by_user": None, "manufacturer": "SafeCo",
     "model": "L1", "model_id": "L1", "area_id": "entrance", "disabled_by": None},
]
AREA_REGISTRY = [
    {"area_id": "living_room", "name": "客厅"},
    {"area_id": "entrance", "name": "玄关"},
]
ENTITY_REGISTRY = [
    {"entity_id": "light.living_room", "device_id": "dev_living", "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "sensor.living_temp", "device_id": "dev_living", "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "switch.living_room_duplicate", "device_id": "dev_living", "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "climate.ac", "device_id": "dev_climate", "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "switch.fan", "device_id": "dev_fan", "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "media_player.tv", "device_id": "dev_tv", "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "switch.doorbell", "device_id": "dev_doorbell", "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "switch.kettle", "device_id": "dev_kettle", "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "light.hub_indicator", "device_id": "dev_hub", "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "automation.guan_ji", "device_id": None, "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "cover.curtain", "device_id": "dev_curtain", "area_id": None,
     "disabled_by": None, "hidden_by": None},
    {"entity_id": "lock.front_door", "device_id": "dev_lock", "area_id": None,
     "disabled_by": None, "hidden_by": None},
]


def _state_sig(st):
    return json.dumps({"state": st.get("state"), "attributes": st.get("attributes")},
                      sort_keys=True, ensure_ascii=False)


def _apply_pending(eid):
    """每次状态读取时推进延迟生效队列（模拟真实设备的异步状态更新）。"""
    p = PENDING.get(eid)
    if not p:
        return
    p["remaining"] -= 1
    if p["remaining"] <= 0:
        old = _snapshot_state(eid)
        STATES[eid]["state"] = p["state"]
        PENDING.pop(eid, None)
        _broadcast_state_change(eid, old)


def apply_service(domain, service, body):
    eid = body.get("entity_id")
    if eid not in STATES:
        return []  # 复刻 HA：对不存在实体返回 200 空列表
    if service in ("silent", "noop"):
        # 模拟新版 HA 的 null 返回：调用被接受但不返回结果、也不产生变化
        return None
    st = STATES[eid]
    a = st["attributes"]
    old_state = _snapshot_state(eid)
    before = _state_sig(st)
    # 延迟服务：先返回 null，状态在后续第 3 次读取时才生效（2 次读取仍为旧值）
    if service.endswith("_delayed"):
        base = service[:-len("_delayed")]
        if base == "open_cover":
            target = "open"
        elif base in ("turn_off", "close_cover", "media_stop"):
            target = "off"
        elif base == "lock":
            target = "locked"
        elif base == "unlock":
            target = "unlocked"
        else:
            target = "on"
        PENDING[eid] = {"state": target, "remaining": 2}
        return None
    # 故意忽略参数的异常场景：用于验证属性回读校验能识别“状态变了但参数没生效”
    if service == "turn_on_ignore_brightness":
        st["state"] = "on"
        after = _state_sig(st)
        if after != before:
            _broadcast_state_change(eid, old_state)
            return [dict(st)]
        return []
    if service == "turn_on":
        if domain == "cover":
            st["state"] = "open"
        else:
            st["state"] = "on"
        if "brightness_pct" in body:
            a["brightness"] = int(round(float(body["brightness_pct"]) * 2.55))
        if "color_temp_kelvin" in body:
            a["color_temp_kelvin"] = body["color_temp_kelvin"]
        if "rgb_color" in body:
            a["rgb_color"] = body["rgb_color"]
    elif service == "open_cover":
        st["state"] = "open"
    elif service in ("turn_off", "close_cover", "stop", "media_stop"):
        st["state"] = "closed" if domain == "cover" else "off"
    elif service == "start":
        st["state"] = "on"
    elif service == "toggle":
        st["state"] = "off" if st["state"] == "on" else "on"
    elif service == "volume_set":
        a["volume_level"] = body.get("volume_level")
    elif service == "volume_mute":
        a["is_volume_muted"] = body.get("is_volume_muted")
    elif service == "set_temperature":
        a["temperature"] = body.get("temperature")
    elif service == "set_hvac_mode":
        mode = body.get("hvac_mode")
        a["hvac_mode"] = mode
        if mode:
            st["state"] = mode
    elif service == "media_play":
        st["state"] = "playing"
    elif service == "media_pause":
        st["state"] = "paused"
    elif service == "lock":
        st["state"] = "locked"
    elif service == "unlock":
        st["state"] = "unlocked"
    after = _state_sig(st)
    if after != before:
        _broadcast_state_change(eid, old_state)
        return [dict(st)]
    return []


async def _ws_handler(ws, *args):
    """极简 HA WebSocket：auth 握手 + 注册表查询 + state_changed 订阅。"""
    await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.1.0"}))
    try:
        raw = await ws.recv()
    except Exception:  # noqa: BLE001
        return
    req = json.loads(raw)
    if req.get("type") != "auth" or req.get("access_token") != TOKEN:
        await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
        return
    await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.1.0"}))
    WS_CLIENTS.add(ws)
    try:
        async for raw in ws:
            req = json.loads(raw)
            rid = req.get("id")
            typ = req.get("type")
            if typ == "subscribe_events":
                await ws.send(json.dumps({"id": rid, "type": "result", "success": True, "result": None}))
                continue
            if typ == "config/device_registry/list":
                result = DEVICE_REGISTRY
            elif typ == "config/entity_registry/list":
                result = ENTITY_REGISTRY
            elif typ == "config/area_registry/list":
                result = AREA_REGISTRY
            else:
                await ws.send(json.dumps({"id": rid, "type": "result", "success": False,
                                          "error": {"code": "unknown_command", "message": "Unknown command."}}))
                continue
            await ws.send(json.dumps({"id": rid, "type": "result", "success": True, "result": result}))
    finally:
        WS_CLIENTS.discard(ws)


def _run_ws():
    async def runner():
        global WS_LOOP
        WS_LOOP = asyncio.get_running_loop()
        async with websockets.serve(_ws_handler, BIND, WS_PORT):
            await asyncio.Future()
    asyncio.run(runner())


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self._send({"message": "Unauthorized"}, 401)
            return False
        return True

    def do_GET(self):
        if not self._authed():
            return
        path = self.path.split("?")[0]
        if path in ("/api", "/api/"):
            self._send({"message": "API running."})
        elif path == "/api/services":
            self._send(SERVICES)
        elif path == "/api/states":
            self._send(list(STATES.values()))
        elif path.startswith("/api/states/"):
            eid = path[len("/api/states/"):]
            if eid in STATES:
                _apply_pending(eid)
                self._send(STATES[eid])
            else:
                self._send({"message": "Entity not found."}, 404)
        else:
            self._send({"message": "Not found"}, 404)

    def do_POST(self):
        if not self._authed():
            return
        path = self.path.split("?")[0]
        if path.startswith("/api/services/"):
            parts = path[len("/api/services/"):].split("/")
            domain = parts[0] if parts else ""
            service = parts[1] if len(parts) > 1 else ""
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send(apply_service(domain, service, body))
        else:
            self._send({"message": "Not found"}, 404)

    def log_message(self, fmt, *args):
        print("[fake-ha] " + (fmt % args))


if __name__ == "__main__":
    print("fake HA on " + BIND + ":" + str(PORT) + " (token from FAKE_HA_TOKEN)", flush=True)
    if websockets is not None:
        threading.Thread(target=_run_ws, daemon=True).start()
        print("fake HA ws on ws://" + BIND + ":" + str(WS_PORT) + "/api/websocket", flush=True)
    else:
        print("[fake-ha] websockets 未安装，跳过注册表 WS；设备聚合将降级为按实体展示", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()

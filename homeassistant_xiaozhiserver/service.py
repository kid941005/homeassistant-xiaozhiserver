# -*- coding: utf-8 -*-
"""HA 服务映射、状态预期与回读校验（与 MCP 传输层解耦）。

本模块只依赖 HAClient/HAError 与配置，不导入 MCPServer，便于单独测试。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from typing import Any, Optional

from .config import (
    SERVICE_CONFIRM_DELAY,
    SERVICE_CONFIRM_INTERVAL,
    SERVICE_CONFIRM_RETRIES,
    SERVICE_CONFIRM_TIMEOUT,
)
from .client import HAError, HAClient

log = logging.getLogger("homeassistant_xiaozhiserver.service")

def _accepts_force(get_state: Any) -> bool:
    """判断客户端 get_state 是否强制接受 force 参数。

    通过 signature.bind 判断，兼容 get_state(eid, *args, **kwargs) 这类包装函数；
    签名不可读时保守地按支持处理，真实调用异常由上层失败关闭。
    """
    try:
        sig = inspect.signature(get_state)
    except (TypeError, ValueError):
        return True
    try:
        sig.bind("__entity__", force=True)
        return True
    except TypeError:
        return False


SUPPORTED_ACTIONS = [
    "turn_on", "turn_off", "toggle", "open", "close", "stop", "pause", "continue", "resume", "play",
    "set_brightness", "set_percentage", "set_position", "set_volume", "volume_mute", "volume_unmute",
    "set_temperature", "set_hvac_mode", "set_fan_mode", "set_color_temp", "set_color", "lock", "unlock",
]

# 各设备类型内置支持的控制动作（与 build_service 对齐）。不在表内的类型视为“无内置控制”，
# 仍可用 list_services + call_service 直接调用任意 HA 服务。
DOMAIN_ACTIONS = {
    "light": ["turn_on", "turn_off", "toggle", "set_brightness", "set_color_temp", "set_color"],
    "switch": ["turn_on", "turn_off", "toggle"],
    "fan": ["turn_on", "turn_off", "toggle", "set_percentage"],
    "input_boolean": ["turn_on", "turn_off", "toggle"],
    "automation": ["turn_on", "turn_off", "toggle"],
    "cover": ["open", "close", "stop", "set_position", "turn_on", "turn_off"],
    "climate": ["turn_on", "turn_off", "set_temperature", "set_hvac_mode", "set_fan_mode"],
    "media_player": ["turn_on", "turn_off", "play", "pause", "stop", "continue", "resume",
                     "set_volume", "volume_mute", "volume_unmute"],
    "vacuum": ["turn_on", "turn_off", "stop", "pause", "continue", "resume"],
    "lock": ["lock", "unlock"],
    "scene": ["turn_on"],
    "script": ["turn_on"],
}

# 用于判断控制前后状态是否真的变化（排除 last_changed 之类的时间戳噪声）
SNAP_KEYS = [
    "brightness", "color_temp_kelvin", "rgb_color", "volume_level", "is_volume_muted",
    "temperature", "fan_mode", "percentage", "current_position", "hvac_mode",
    "preset_mode", "swing_mode", "source", "current_tilt_position", "direction", "effect",
]

def _domain_of(entity_id: str) -> str:
    return str(entity_id).split(".", 1)[0]


def _friendly_name(st: dict) -> str:
    attrs = st.get("attributes")
    name = attrs.get("friendly_name") if isinstance(attrs, dict) else None
    return str(name or st.get("entity_id"))


# 对外播报用的中文标签：不要把 turn_off / light.turn_on / off 这类协议词暴露给语音助手。
_ACTION_LABELS = {
    "turn_on": "打开", "turn_off": "关闭", "toggle": "切换",
    "open": "打开", "close": "关闭", "stop": "停止", "pause": "暂停",
    "continue": "继续", "resume": "继续", "play": "播放",
    "set_brightness": "调亮度", "set_volume": "调音量", "volume_set": "调音量",
    "set_percentage": "调风速", "set_speed": "调风速",
    "volume_mute": "静音", "volume_unmute": "取消静音",
    "set_temperature": "调温", "set_color_temp": "调色温", "set_color": "调颜色",
    "lock": "上锁", "unlock": "解锁", "set_hvac_mode": "切换模式",
    "set_fan_mode": "调风速", "set_swing_mode": "调摆风", "set_preset_mode": "调模式",
    "set_humidity": "调湿度", "set_position": "调位置", "set_cover_position": "调位置",
    "media_play": "播放", "media_pause": "暂停", "media_stop": "停止",
    "open_cover": "打开", "close_cover": "关闭", "stop_cover": "停止",
}

# 这些服务名可以直接生成中文播报；call_service 只对可控 domain 使用，避免把脚本/场景说成设备动作。
DIRECT_RESPONSE_SERVICES = frozenset(_ACTION_LABELS)

_STATE_LABELS = {
    "on": "打开", "off": "关闭", "open": "打开", "closed": "关闭",
    "locked": "上锁", "unlocked": "解锁", "playing": "播放",
    "paused": "暂停", "idle": "待机", "standby": "待机",
    "heat": "制热", "cool": "制冷", "auto": "自动", "dry": "除湿",
    "fan_only": "送风", "unavailable": "不可用", "unknown": "未知",
}


def _action_label(action: Any) -> str:
    """把 turn_off / light.turn_on 之类的协议名转成中文动作。"""
    s = str(action or "").strip().lower()
    if "." in s:
        s = s.split(".", 1)[1]
    return _ACTION_LABELS.get(s, "执行" if s else "操作")


def _state_label(state: Any) -> str:
    s = str(state or "").strip().lower()
    return _STATE_LABELS.get(s, "目标状态" if s else "未知")


def _num_text(value: Any, scale: float = 1.0) -> str:
    """把数值格式化成短文本；非法值返回空串，避免播报阶段抛异常。"""
    try:
        v = float(value) * scale
    except (TypeError, ValueError):
        return ""
    return ("%g" % v)


def _voice_message(name: str, action: str, status: str, after: Optional[dict],
                   verified: bool, payload: Optional[dict]) -> str:
    """生成适合 TTS 直接播报的中文短句，不包含服务名、entity_id 或英文状态。"""
    label = _action_label(action)
    state = (after or {}).get("state")
    p = payload if isinstance(payload, dict) else {}
    if status == "confirmed":
        if label == "切换":
            if state in ("on", "off"):
                return name + "已" + _state_label(state) + "。"
            return name + "已切换。"
        if label == "调亮度" and (p.get("brightness_pct") == 0
                                  or (p.get("brightness_pct") is None and state == "off")):
            return name + "已关闭。"
        if label == "调亮度":
            v = _num_text(p.get("brightness_pct"))
            if v:
                return name + "亮度已调到" + v + "%。"
        if label == "切换模式":
            mode = p.get("hvac_mode")
            if mode:
                return name + "已切换到" + _state_label(mode) + "模式。"
            return name + "模式已切换。"
        if label == "调风速":
            v = _num_text(p.get("percentage"))
            if v:
                return name + "风速已调到" + v + "%。"
            return name + "风速已调整。"
        if label == "调摆风":
            return name + "摆风已调整。"
        if label == "调模式":
            return name + "模式已切换。"
        if label == "调湿度":
            v = _num_text(p.get("humidity"))
            if v:
                return name + "湿度已调到" + v + "%。"
            return name + "湿度已调整。"
        if label == "调位置":
            v = _num_text(p.get("position"))
            if v:
                return name + "位置已调到" + v + "%。"
            return name + "位置已调整。"
        if label == "调音量":
            v = _num_text(p.get("volume_level"), 100)
            if v:
                return name + "音量已调到" + v + "%。"
        if label == "调温":
            v = _num_text(p.get("temperature"))
            if v:
                return name + "温度已调到" + v + "度。"
        if label == "调色温":
            v = _num_text(p.get("color_temp_kelvin"))
            if v:
                return name + "色温已调到" + v + "K。"
        if label == "调颜色":
            return name + "颜色已调整。"
        if label == "静音":
            return name + "已静音。"
        if label == "取消静音":
            return name + "已取消静音。"
        if label == "继续":
            return name + "已继续播放。"
        if label == "播放":
            return name + "已开始播放。"
        if label == "暂停":
            return name + "已暂停。"
        if label == "停止":
            return name + "已停止。"
        if label == "上锁":
            return name + "已上锁。"
        if label == "解锁":
            return name + "已解锁。"
        return name + "已" + label + "。"
    if status == "accepted":
        # 即发即忘型服务：HA 已接受调用，但没有可验证的状态变化。
        return name + "已" + label + "。"
    if status == "no_change":
        if verified:
            if state == "on":
                return name + "本来就是开着的，不用操作。"
            if state == "off":
                return name + "本来就是关着的，不用操作。"
            return name + "本来就是" + _state_label(state) + "，不用操作。"
        return name + "没有变化，可能不支持这个操作。"
    if status == "mismatch":
        return name + "已执行，但状态没达到预期。"
    return name + "的指令已发送，稍后再确认。"


def snapshot(st: Optional[dict]) -> Optional[dict]:
    if not st:
        return None
    a = st.get("attributes") or {}
    d = {"state": st.get("state")}
    for k in SNAP_KEYS:
        if k in a:
            d[k] = a[k]
    return d


def _classify_service_response(resp: Any) -> str:
    """把 HA service 调用的返回体归类为三态之一。

    - states : 非空数组/对象（旧版 HA 返回受影响的实体状态，或 return_response 有数据）
    - empty  : 空数组 []（调用被接受，但没有任何状态变化）
    - none   : null（新版 HA 不返回调用结果，需乐观推断 + 延迟回读确认）
    """
    if resp is None:
        return "none"
    if isinstance(resp, list):
        return "states" if len(resp) > 0 else "empty"
    if isinstance(resp, dict):
        return "states" if resp else "empty"
    return "none"


# 通用服务 -> 乐观预期状态（HA 不返回结果时，告诉调用方“预期会变成什么”）。
# 注意：turn_on/turn_off 对部分 domain 不能一概预期为 on/off（例如 climate 打开后是
# heat/cool/auto，media_player 打开后可能是 idle/playing），需由 _expected_state 按 domain 判断。
_EXPECTED_STATE = {
    "open_cover": "open", "close_cover": "closed",
    "media_play": "playing", "media_pause": "paused", "media_stop": "idle",
    "lock": "locked", "unlock": "unlocked",
}

# turn_on/turn_off 在多数二元 domain 上可直接预期 on/off；其余 domain 由设备能力决定。
_BINARY_TURN_DOMAINS = {
    "light", "switch", "fan", "input_boolean", "siren", "humidifier", "remote", "automation",
}
# cover/valve 的 turn_on/turn_off 语义是 open/closed，不能按 on/off 回读。
_OPEN_CLOSE_TURN_DOMAINS = {"cover", "valve"}

# 即发即忘型服务：HA 通常返回 null 且不保证实体状态变化，不能因为回读无变化就判失败。
FIRE_AND_FORGET_SERVICES = frozenset({
    ("scene", "turn_on"),
    ("script", "turn_on"),
    ("automation", "trigger"),
    ("cover", "stop_cover"),
})


def _expected_state(service: str, before_state: Optional[str],
                    domain: Optional[str] = None) -> Optional[str]:
    """按服务语义 + 设备 domain 推导乐观预期状态；无法可靠推导时返回 None。"""
    svc = (service or "").strip().lower()
    dom = (domain or "").strip().lower()
    if svc == "toggle":
        if before_state == "on":
            return "off"
        if before_state == "off":
            return "on"
        return None
    if svc in ("turn_on", "turn_off"):
        if dom in _OPEN_CLOSE_TURN_DOMAINS:
            return "open" if svc == "turn_on" else "closed"
        if dom in _BINARY_TURN_DOMAINS:
            return "on" if svc == "turn_on" else "off"
        if dom == "climate":
            # climate 的 turn_off 状态是 off；turn_on 时若已经处于某个制热/制冷模式，
            # 幂等调用不会改变状态，按当前模式回读即可，避免误判 accepted_pending。
            if svc == "turn_off":
                return "off"
            if before_state not in (None, "off", "unavailable", "unknown"):
                return str(before_state)
        if dom == "vacuum":
            # 仅覆盖“本来已是目标状态”的幂等场景；不臆造自定义集成状态名。
            if svc == "turn_on" and before_state == "cleaning":
                return "cleaning"
            if svc == "turn_off" and before_state == "docked":
                return "docked"
        return None
    if dom == "vacuum":
        if svc == "pause" and before_state == "paused":
            return "paused"
        if svc == "stop" and before_state == "docked":
            return "docked"
    return _EXPECTED_STATE.get(svc)


def _field_brief(fname: str, fdef: dict) -> dict:
    """把 HA 服务参数字段压缩成便于大模型理解的说明。"""
    if not isinstance(fdef, dict):
        return {"name": fname, "required": False}
    sel = fdef.get("selector") or {}
    sel_type = next(iter(sel.keys()), None) if isinstance(sel, dict) else None
    out = {"name": fname, "required": bool(fdef.get("required"))}
    if fdef.get("description"):
        out["description"] = fdef["description"]
    if sel_type:
        out["selector"] = sel_type
    if "example" in fdef:
        out["example"] = fdef["example"]
    return out


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN/Infinity 无法用于范围比较（所有比较都为 False），必须在入口拒绝。
    return number if math.isfinite(number) else None


def _value_matches(actual: Any, expected: Any, tolerance: float = 0.0) -> bool:
    """比较回读值与预期值，数字按容差、列表逐项、布尔按真值。"""
    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)) or len(actual) != len(expected):
            return False
        return all(_value_matches(a, e, tolerance) for a, e in zip(actual, expected, strict=True))
    if isinstance(expected, bool) or isinstance(actual, bool):
        # 属性缺失不能当作 expected=False 的匹配：那会把“没读到”误判成“已确认关闭/静音”。
        if actual is None:
            return False
        return bool(actual) == bool(expected)
    na, ne = _num(actual), _num(expected)
    if na is not None and ne is not None:
        return abs(na - ne) <= float(tolerance or 0)
    return actual == expected


def _rgb_components(value: Any, code: str = "invalid_value", label: str = "颜色") -> list:
    """校验并归一化 [r,g,b]；非法值抛 HAError（不冒泡为 internal_error）。"""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise HAError(code, label + " 必须是 [r,g,b] 三个 0-255 整数，收到：" + str(value))
    try:
        # 契约要求整数分量：bool 不是整数，任何 float（包括 255.0）也拒绝，避免
        # int(1.5) 静默截断或等值浮点让调用方误以为接口接受浮点。
        if any(isinstance(x, bool) or not isinstance(x, int) for x in value):
            raise ValueError("non-integer")
        rgb = [int(x) for x in value]
    except (TypeError, ValueError, OverflowError) as exc:
        raise HAError(code, label + " 必须是 [r,g,b] 三个 0-255 整数，收到：" + str(value)) from exc
    if any(c < 0 or c > 255 for c in rgb):
        raise HAError(code, label + " 每个分量必须在 0-255，收到：" + str(value))
    return rgb


def validate_action_attributes(domain: str, extra: dict, attributes: Any) -> None:
    """按实体属性校验参数范围/枚举，避免把明显越界的值发给 HA 再等 400。"""
    if not isinstance(extra, dict):
        return
    attrs = attributes if isinstance(attributes, dict) else {}
    # 统一数值边界，避免 brightness_pct=1000 / volume_level=2 / position=999 直接发给 HA。
    for key, (low, high) in {"brightness_pct": (0, 100), "brightness": (0, 255),
                             "volume_level": (0.0, 1.0), "position": (0, 100),
                             "percentage": (0, 100), "tilt_position": (0, 100)}.items():
        if key in extra:
            val = _num(extra.get(key))
            if val is None:
                raise HAError("invalid_value", key + " 必须是有限数值，收到：" + str(extra.get(key)))
            if val < low or val > high:
                raise HAError("invalid_value", key + " 必须在 " + str(low) + "-" + str(high)
                              + " 之间，收到：" + str(extra.get(key)))
    if "temperature" in extra:
        value = _num(extra.get("temperature"))
        low, high = _num(attrs.get("min_temp")), _num(attrs.get("max_temp"))
        if value is None:
            raise HAError("invalid_value", "temperature 必须是有限数值，收到：" + str(extra.get("temperature")))
        if (low is not None and value < low) or (high is not None and value > high):
            raise HAError("invalid_value", "温度 " + str(value) + " 超出设备允许范围 "
                          + str(low if low is not None else "?") + "-"
                          + str(high if high is not None else "?") + "。")
        # 华氏设备若收到典型摄氏度值（26），静默按华氏执行会严重偏差；明确提示改用设备单位。
        unit = str(attrs.get("temperature_unit") or "")
        # 仅在设备未提供完整温度范围时才用启发式；已有 min/max 时以范围为准，
        # 避免把合法的 45°F 当成摄氏度拒绝。
        if (value is not None and "f" in unit.lower()
                and (low is None or high is None) and value <= 45):
            raise HAError("invalid_value", "该设备温度单位为华氏度（" + unit + "），"
                          + str(value) + " 可能是摄氏度；请使用华氏温度或 call_service 明确指定。")
    if "color_temp_kelvin" in extra:
        value = _num(extra.get("color_temp_kelvin"))
        low, high = _num(attrs.get("min_color_temp_kelvin")), _num(attrs.get("max_color_temp_kelvin"))
        if value is None:
            raise HAError("invalid_value", "color_temp_kelvin 必须是有限数值，收到：" + str(extra.get("color_temp_kelvin")))
        if (low is not None and value < low) or (high is not None and value > high):
            raise HAError("invalid_value", "色温 " + str(value) + "K 超出设备允许范围 "
                          + str(low if low is not None else "?") + "-"
                          + str(high if high is not None else "?") + "K。")
    if "hvac_mode" in extra:
        modes = attrs.get("hvac_modes")
        if isinstance(modes, list) and modes and extra.get("hvac_mode") not in modes:
            raise HAError("invalid_value", "设备不支持模式 " + str(extra.get("hvac_mode"))
                          + "；可用模式：" + ", ".join(str(m) for m in modes) + "。")
    if "fan_mode" in extra:
        modes = attrs.get("fan_modes")
        if isinstance(modes, list) and modes and extra.get("fan_mode") not in modes:
            raise HAError("invalid_value", "设备不支持风速 " + str(extra.get("fan_mode"))
                          + "；可用风速：" + ", ".join(str(m) for m in modes) + "。")


def _expected_attributes(payload: dict) -> dict:
    """从服务调用参数推断需要回读核对的属性预期（含合理容差）。"""
    exp: dict = {}
    if not isinstance(payload, dict):
        return exp
    pct = _num(payload.get("brightness_pct"))
    if pct is not None:
        # 不同灯具把百分比换算成 brightness 的刻度略有差异，容差放宽到 5，避免误报 mismatch。
        exp["brightness"] = {"expected": int(round(pct * 2.55)), "tolerance": 5}
    bright = _num(payload.get("brightness"))
    if bright is not None:
        exp["brightness"] = {"expected": int(round(bright)), "tolerance": 1}
    kelvin = _num(payload.get("color_temp_kelvin"))
    if kelvin is not None:
        exp["color_temp_kelvin"] = {"expected": int(round(kelvin)), "tolerance": 10}
    if "rgb_color" in payload:
        exp["rgb_color"] = {"expected": _rgb_components(payload.get("rgb_color"), "invalid_data", "data.rgb_color"),
                            "tolerance": 0}
    vol = _num(payload.get("volume_level"))
    if vol is not None:
        exp["volume_level"] = {"expected": vol, "tolerance": 0.02}
    temp = _num(payload.get("temperature"))
    if temp is not None:
        exp["temperature"] = {"expected": temp, "tolerance": 0.5}
    if "is_volume_muted" in payload:
        exp["is_volume_muted"] = {"expected": bool(payload.get("is_volume_muted")), "tolerance": 0}
    if "hvac_mode" in payload:
        exp["hvac_mode"] = {"expected": payload.get("hvac_mode"), "tolerance": 0}
    for field in ("preset_mode", "swing_mode", "source", "effect", "direction"):
        if field in payload:
            exp[field] = {"expected": payload.get(field), "tolerance": 0}
    pct = _num(payload.get("percentage"))
    if pct is not None:
        exp["percentage"] = {"expected": pct, "tolerance": 1}
    pos = _num(payload.get("position"))
    if pos is not None:
        exp["current_position"] = {"expected": pos, "tolerance": 1}
    tilt = _num(payload.get("tilt_position"))
    if tilt is not None:
        exp["current_tilt_position"] = {"expected": tilt, "tolerance": 1}
    return exp


def _check_after(after: Optional[dict], expected_state: Optional[str],
                 attr_expect: dict) -> dict:
    """对一次回读做状态 + 属性逐项校验，返回 checks / mismatches。"""
    checks: list = []
    mismatches: list = []
    if expected_state is not None:
        actual = after.get("state") if after else None
        ok = actual == expected_state
        item = {"kind": "state", "field": "state", "expected": expected_state,
                "actual": actual, "ok": ok}
        checks.append(item)
        if not ok:
            mismatches.append(item)
    for field, spec in attr_expect.items():
        actual = after.get(field) if after else None
        expected = spec.get("expected")
        ok = _value_matches(actual, expected, spec.get("tolerance", 0))
        item = {"kind": "attribute", "field": field, "expected": expected,
                "actual": actual, "ok": ok}
        checks.append(item)
        if not ok:
            mismatches.append(item)
    return {"verified": not mismatches, "checks": checks, "mismatches": mismatches}


async def _apply_and_confirm(ha: "HAClient", st: dict, service_domain: str,
                             service: str, payload: dict, action_label: str,
                             precheck=None, dispatch_state: Optional[dict] = None) -> dict:
    """同一实体的控制串行执行，避免并发下发与回读互相覆盖。

    precheck 在锁内 force 刷新后的最新状态上再次执行危险确认；dispatch_state
    用于标记 POST 是否真正开始，便于批量超时区分“未下发/已下发待确认”。
    """
    eid = str(st.get("entity_id"))
    lock_factory = getattr(ha, "entity_lock", None)
    if not callable(lock_factory):
        return await _apply_and_confirm_unlocked(
            ha, st, service_domain, service, payload, action_label,
            precheck=precheck, dispatch_state=dispatch_state
        )
    async with lock_factory(eid):
        # 初始状态也必须在实体锁内读取：否则两个并发 toggle 都基于旧状态计算预期，
        # 第二个可能把正确的最终状态误判为 mismatch。强制回源一次，锁内串行。
        try:
            if _accepts_force(ha.get_state):
                fresh = await ha.get_state(eid, force=True)
            else:
                fresh = await ha.get_state(eid)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # 除取消外，任何刷新异常都按“无法确认最新状态”失败关闭，绝不用旧缓存
            # 去执行门锁/车库门等危险动作。
            fresh = None
        if isinstance(fresh, dict) and str(fresh.get("entity_id") or "") == eid:
            st = fresh
        else:
            # 取得实体锁后必须拿到最新状态：刷新失败一律失败关闭，避免线路抖动时
            # 用旧缓存下发门锁/车库门等危险动作；带 confirm_token 也不能降级。
            return {"_danger_guard": {
                "ok": False, "code": "state_unavailable",
                "message": "无法刷新设备最新状态，无法确认是否属于危险设备，已拒绝执行。"}}
        return await _apply_and_confirm_unlocked(
            ha, st, service_domain, service, payload, action_label,
            precheck=precheck, dispatch_state=dispatch_state
        )


async def _apply_and_confirm_unlocked(ha: "HAClient", st: dict, service_domain: str,
                                      service: str, payload: dict, action_label: str,
                                      precheck=None, dispatch_state: Optional[dict] = None) -> dict:
    """执行 HA 服务调用，并在超时窗口内轮询回读，校验「状态 + 属性」是否达到预期。

    不再只判断「有没有变化」，而是按服务语义推导预期：
      - 状态预期：turn_on→on、turn_off→off、open_cover→open、set_hvac_mode→指定模式等；
      - 属性预期：brightness_pct→brightness、temperature、volume_level、color_temp_kelvin、rgb_color 等。
    回读在 SERVICE_CONFIRM_TIMEOUT 秒内按 SERVICE_CONFIRM_INTERVAL 轮询，直到全部匹配或超时。

    status 语义：
      confirmed        回读确认状态/属性已达到预期（且确有变化）
      no_change        HA 已接受但无变化；若校验通过表示「本来就是目标状态」
      mismatch         回读观察到变化，但状态或属性与预期不符（设备能力/参数问题）
      accepted          即发即忘服务（场景/脚本/停止等）HA 已接受调用，不保证状态变化
      accepted_pending HA 返回 null（新版 HA 常见），回读窗口内仍未完全匹配预期
    """
    eid = str(st.get("entity_id"))
    # 危险判断必须基于实体锁内 force 刷新后的最新状态：缓存里的 attributes 可能缺少
    # device_class，刷新后才发现是车库门时不能再下发。先复查，命中则直接返回拦截结果。
    if precheck is not None:
        blocked = precheck(st)
        if blocked is not None:
            return {"_danger_guard": blocked}
    before = snapshot(st)
    before_state = st.get("state") if st else None
    expected = _expected_state(service, before_state, _domain_of(eid))
    # set_hvac_mode 等服务的预期状态来自参数
    if isinstance(payload, dict) and payload.get("hvac_mode"):
        expected = str(payload["hvac_mode"])
    attr_expect = _expected_attributes(payload)
    has_expectation = expected is not None or bool(attr_expect)

    request_started = time.monotonic()
    if dispatch_state is not None:
        dispatch_state["dispatched"] = True
    try:
        resp = await ha.call_service(service_domain, service, payload)
    except asyncio.TimeoutError as exc:
        raise HAError("post_timeout", "调用 Home Assistant 服务超时，无法确认指令是否已下发。") from exc
    except HAError as exc:
        # httpx 的 TimeoutException/网络中断在 HAClient 内会变成 HAError("timeout" /
        # "network_error" / "upstream_error")；请求可能已经到达 HA，不能当作确定未下发。
        if exc.code in ("timeout", "network_error", "upstream_error"):
            raise HAError("post_timeout", "调用 Home Assistant 服务超时或网络中断，无法确认指令是否已下发。") from exc
        raise
    # 确认窗口从 POST 返回后开始计时：慢 POST 不应耗尽回读预算而制造假失败。
    started = time.monotonic()
    resp_kind = _classify_service_response(resp)

    # 硬截止时间：SERVICE_CONFIRM_TIMEOUT 是上限，不允许 interval=30 把单设备拖到约 120s。
    deadline = started + max(0.1, float(SERVICE_CONFIRM_TIMEOUT))
    min_attempts = max(1, int(SERVICE_CONFIRM_RETRIES) + 1)
    interval = SERVICE_CONFIRM_INTERVAL if SERVICE_CONFIRM_INTERVAL > 0 else 0.0
    # RETRIES 只是最少回读次数，真正的上限是 TIMEOUT；否则慢设备会在 ~0.6s 就被判为无变化。
    max_attempts = max(min_attempts, int(SERVICE_CONFIRM_TIMEOUT / max(interval, 0.05)) + 2)
    attempts = 0
    after = None
    changed = False
    last_force = 0.0
    readback_errors: list = []
    check = {"verified": False, "checks": [], "mismatches": []}

    def _matches(candidate: dict | None) -> bool:
        snap = snapshot(candidate)
        if has_expectation:
            return bool(_check_after(snap, expected, attr_expect)["verified"])
        return snap is not None and snap != before

    stream_ready = getattr(ha, "state_stream_ready", None)
    wait_state = getattr(ha, "wait_for_state", None)
    ensure_stream = getattr(ha, "_ensure_state_stream", None)
    can_events = callable(stream_ready) and callable(wait_state)
    if callable(ensure_stream):
        try:
            ensure_stream()
        except Exception as exc:  # noqa: BLE001 事件流只是加速手段，失败继续轮询
            log.warning("HA 事件流启动失败，回退 REST 轮询：%s", exc)

    while True:
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        after_st = None
        if can_events and stream_ready():
            try:
                after_st = await wait_state(eid, _matches, timeout=min(max(0.05, interval), remaining))
            except Exception as e:  # noqa: BLE001
                log.debug("wait_for_state 失败，回退轮询：%s", e)
                after_st = None
            if after_st is None:
                # 事件流未命中（含 fake HA 的延迟服务：读取才推进状态）时回退读状态。
                # 事件流就绪时缓存通常已由 WS 更新，因此 REST 最多每 0.5s 一次；
                # 最后一轮强制 REST，确保拿到最终状态，避免每 0.15s 轰击 HA。
                now = time.monotonic()
                force = ((now - last_force) >= max(0.5, interval)
                         or attempts >= max_attempts)
                try:
                    after_st = await asyncio.wait_for(
                        ha.get_state(eid, force=force),
                        timeout=max(0.05, deadline - time.monotonic()))
                    if force:
                        last_force = now
                except (asyncio.TimeoutError, HAError) as e:
                    after_st = None
                    code = e.code if isinstance(e, HAError) else "timeout"
                    if code not in readback_errors:
                        readback_errors.append(code)
        else:
            if attempts == 1:
                if SERVICE_CONFIRM_DELAY > 0:
                    await asyncio.sleep(min(SERVICE_CONFIRM_DELAY,
                                            max(0.0, deadline - time.monotonic())))
            else:
                # 即使配置为 0 也让出事件循环，避免在超时窗口内忙等轰击 HA。
                await asyncio.sleep(min(max(0.05, interval), max(0.05, deadline - time.monotonic())))
            try:
                after_st = await asyncio.wait_for(
                    ha.get_state(eid),
                    timeout=max(0.05, deadline - time.monotonic()))
            except (asyncio.TimeoutError, HAError) as e:
                after_st = None
                # 服务已下发，回读失败不应伪造成“无变化”；记录错误供调用方判断。
                code = e.code if isinstance(e, HAError) else "timeout"
                if code not in readback_errors:
                    readback_errors.append(code)
        after = snapshot(after_st)
        changed = after is not None and after != before
        check = _check_after(after, expected, attr_expect)
        if has_expectation:
            if check["verified"]:
                break
        elif changed:
            break
        # 即发即忘服务通常不会有状态变化，读满最小次数即返回，避免白等整个超时窗口。
        if ((service_domain, service) in FIRE_AND_FORGET_SERVICES
                and not has_expectation and not changed and attempts >= min_attempts):
            break
        # 至少回读 min_attempts 次，但最多到 TIMEOUT 对应的次数；
        # 旧写法 attempts>=min_attempts 先退出，使 TIMEOUT 形同虚设。
        if attempts >= max_attempts:
            break
        if attempts >= min_attempts and (time.monotonic() - started) >= SERVICE_CONFIRM_TIMEOUT:
            break

    verified = bool(check["verified"]) if has_expectation else bool(changed)
    if has_expectation and check["verified"]:
        status = "confirmed" if changed else "no_change"
    elif (service_domain, service) in FIRE_AND_FORGET_SERVICES and not changed:
        # 场景/脚本/停止等即发即忘服务：HA 已接受调用即可，不能因无状态变化判失败。
        status = "accepted"
        verified = False
    elif (service_domain == "cover" and isinstance(after, dict)
          and ((service == "open_cover" and str(after.get("state")) == "opening")
               or (service == "close_cover" and str(after.get("state")) == "closing"))):
        # 慢速卷帘/车库门在超时窗口内仍处于过渡态，不能判失败；按方向判断，
        # 反向过渡说明没有朝目标动作，不能当作成功。
        status = "accepted_pending"
        verified = False
    elif changed:
        # 无明确预期时只能确认「有变化」，不能判为 mismatch
        status = "mismatch" if has_expectation else "confirmed"
    elif readback_errors and after is None:
        # 服务已下发但一次都没回读到状态：不能当作 no_change，标记为待确认。
        status = "accepted_pending"
    elif resp_kind in ("empty", "states"):
        status = "no_change"
    else:  # none
        status = "accepted_pending"

    svc_str = service_domain + "." + service
    name = _friendly_name(st)
    voice = _voice_message(name, action_label, status, after, verified, payload)
    msg = voice
    if status == "mismatch":
        msg += "请检查设备能力或参数。"
    elif status == "no_change" and not verified:
        msg += "HA 已接受调用，但回读未见状态变化。"
    elif status == "accepted_pending":
        if readback_errors and after is None:
            msg += "HA 已接受调用，但回读状态失败（" + "、".join(readback_errors) + "），请稍后复查。"
        else:
            msg += "HA 未返回调用结果，已下发但回读 " + str(attempts) + " 次仍未完全匹配预期，请稍后复查。"

    verification = {
        "verified": verified,
        "attempts": attempts,
        "elapsed_ms": int(round((time.monotonic() - request_started) * 1000)),
        "expectations": {"state": expected, "attributes": attr_expect},
        "checks": check["checks"],
        "mismatches": check["mismatches"],
        "readback_errors": readback_errors,
    }
    log.info("action entity=%s service=%s status=%s verified=%s attempts=%s elapsed_ms=%s",
             eid, svc_str, status, verified, attempts, verification["elapsed_ms"])
    if status == "mismatch":
        log.warning("action mismatch entity=%s service=%s mismatches=%s", eid, svc_str, check["mismatches"])
    return {
        "entity_id": eid, "name": name, "service": svc_str,
        "response_kind": resp_kind, "status": status,
        "expected_state": expected, "before": before, "after": after,
        "changed": changed, "verified": verified,
        "readback_errors": readback_errors,
        "verification": verification, "message": msg, "voice": voice,
    }


def _need_value(action: str, value: Any) -> None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise HAError("missing_value", "动作 " + action + " 需要提供 value 参数（如亮度 0-100、温度、色温开尔文、颜色 #RRGGBB）。")


def _clamp_pct(value: Any) -> int:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise HAError("invalid_value", "value 必须是 0-100 的数字，收到：" + str(value)) from exc
    if not math.isfinite(v):
        raise HAError("invalid_value", "value 必须是有限数字，收到：" + str(value))
    if v < 0 or v > 100:
        raise HAError("invalid_value", "value 必须在 0-100 之间，收到：" + str(value))
    return int(round(v))


def _parse_color(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return _rgb_components(value)
    s = str(value).strip().lstrip("#")
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
        base = 10  # "r,g,b" 按十进制解析
    elif len(s) == 6:
        parts = [s[0:2], s[2:4], s[4:6]]
        base = 16  # #RRGGBB 按十六进制解析
    else:
        raise HAError("invalid_value", "颜色格式应为 #RRGGBB 或 'r,g,b'，收到：" + str(value))
    try:
        rgb = [int(p, base) for p in parts]
    except (TypeError, ValueError) as exc:
        raise HAError("invalid_value", "颜色格式应为 #RRGGBB 或 'r,g,b'，收到：" + str(value)) from exc
    if len(rgb) != 3 or any(c < 0 or c > 255 for c in rgb):
        raise HAError("invalid_value", "RGB 每个分量必须在 0-255，收到：" + str(value))
    return rgb


def build_service(action: str, domain: str, value: Any) -> tuple:
    """把通用动作翻译为 (service_domain, service, extra_data)。"""
    a = (action or "").strip().lower()
    if a == "turn_on":
        if domain == "cover":
            return "cover", "open_cover", {}
        if domain == "vacuum":
            return "vacuum", "start", {}
        return domain, "turn_on", {}
    if a == "turn_off":
        if domain == "cover":
            return "cover", "close_cover", {}
        if domain == "vacuum":
            return "vacuum", "stop", {}
        return domain, "turn_off", {}
    if a == "toggle":
        return "homeassistant", "toggle", {}
    if a == "lock":
        return "lock", "lock", {}
    if a == "unlock":
        return "lock", "unlock", {}
    if a == "open":
        if domain == "cover":
            return "cover", "open_cover", {}
        if domain == "vacuum":
            return "vacuum", "start", {}
        return domain, "turn_on", {}
    if a == "close":
        if domain == "cover":
            return "cover", "close_cover", {}
        return domain, "turn_off", {}
    if a == "stop":
        if domain == "cover":
            return "cover", "stop_cover", {}
        if domain == "vacuum":
            return "vacuum", "stop", {}
        if domain == "media_player":
            return "media_player", "media_stop", {}
        raise HAError("unsupported_action", "设备类型 " + domain + " 不支持动作 stop。")
    if a == "pause":
        if domain == "media_player":
            return "media_player", "media_pause", {}
        if domain == "vacuum":
            return "vacuum", "pause", {}
        raise HAError("unsupported_action", "设备类型 " + domain + " 不支持动作 pause。")
    if a in ("continue", "resume"):
        if domain == "media_player":
            return "media_player", "media_play", {}
        if domain == "vacuum":
            return "vacuum", "start", {}
        raise HAError("unsupported_action", "设备类型 " + domain + " 不支持动作 " + a + "。")
    if a == "play":
        if domain != "media_player":
            raise HAError("unsupported_action", "设备类型 " + domain + " 不支持动作 play。")
        return "media_player", "media_play", {}
    if a == "set_percentage":
        _need_value(a, value)
        return "fan", "set_percentage", {"percentage": _clamp_pct(value)}
    if a == "set_position":
        _need_value(a, value)
        return "cover", "set_position", {"position": _clamp_pct(value)}
    if a == "set_hvac_mode":
        _need_value(a, value)
        return "climate", "set_hvac_mode", {"hvac_mode": str(value).strip()}
    if a == "set_fan_mode":
        _need_value(a, value)
        return "climate", "set_fan_mode", {"fan_mode": str(value).strip()}
    if a == "set_brightness":
        _need_value(a, value)
        pct = _clamp_pct(value)
        # 亮度 0 在 HA 中等价于关闭；直接用 turn_off，避免部分灯具忽略 brightness_pct=0 仍保持点亮。
        if pct == 0:
            return "light", "turn_off", {}
        return "light", "turn_on", {"brightness_pct": pct}
    if a == "set_volume":
        _need_value(a, value)
        return "media_player", "volume_set", {"volume_level": round(_clamp_pct(value) / 100.0, 3)}
    if a == "volume_mute":
        return "media_player", "volume_mute", {"is_volume_muted": True}
    if a == "volume_unmute":
        return "media_player", "volume_mute", {"is_volume_muted": False}
    if a == "set_temperature":
        _need_value(a, value)
        try:
            temp = float(value)
        except (TypeError, ValueError) as exc:
            raise HAError("invalid_value", "温度 value 必须是数字，收到：" + str(value)) from exc
        if not math.isfinite(temp):
            raise HAError("invalid_value", "温度 value 必须是有限数字，收到：" + str(value))
        return "climate", "set_temperature", {"temperature": temp}
    if a == "set_color_temp":
        _need_value(a, value)
        try:
            kelvin = float(value)
        except (TypeError, ValueError) as exc:
            raise HAError("invalid_value", "色温 value 必须是开尔文数字，收到：" + str(value)) from exc
        if not math.isfinite(kelvin):
            raise HAError("invalid_value", "色温 value 必须是有限数字，收到：" + str(value))
        return "light", "turn_on", {"color_temp_kelvin": int(round(kelvin))}
    if a == "set_color":
        _need_value(a, value)
        return "light", "turn_on", {"rgb_color": _parse_color(value)}
    raise HAError(
        "unsupported_action",
        "不支持的控制动作：" + str(action) + "。可用动作：" + ", ".join(SUPPORTED_ACTIONS),
    )


def _hint() -> str:
    return "请检查 HA_URL / HA_TOKEN 配置与网络连通性；可查看容器日志或 README「排查」章节定位问题。"


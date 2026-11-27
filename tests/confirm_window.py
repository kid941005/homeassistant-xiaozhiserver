#!/usr/bin/env python3
"""回读窗口自测：慢设备 1s 后才回写状态，确认不会在 ~0.6s 被误判为无变化。"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant_xiaozhiserver.service import (  # noqa: E402
    HAError, _apply_and_confirm, _rgb_components, build_service,
    validate_action_attributes,
)
from homeassistant_xiaozhiserver.views import _filter_actions  # noqa: E402


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print("[ok] " + label)


class SlowHA:
    """模拟 1 秒后才回写状态的慢设备。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.started = 0.0
        self.reads = 0
        self.state = "off"

    def entity_lock(self, entity_id: str) -> asyncio.Lock:
        return self._lock

    async def call_service(self, domain: str, service: str, payload: dict):
        self.started = time.monotonic()
        return None

    async def get_state(self, entity_id: str):
        self.reads += 1
        if self.started and time.monotonic() - self.started >= 1.0:
            self.state = "on"
        return {"entity_id": entity_id, "state": self.state, "attributes": {}}

    def state_stream_ready(self) -> bool:
        return False


class RefreshFailHA:
    """模拟实体锁内 force 刷新失败：危险复查拿不到最新状态时必须拒绝下发。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.called = False

    def entity_lock(self, entity_id: str) -> asyncio.Lock:
        return self._lock

    async def call_service(self, domain: str, service: str, payload: dict):
        self.called = True
        return None

    async def get_state(self, entity_id: str, force: bool = False):
        raise HAError("timeout", "刷新超时")

    def state_stream_ready(self) -> bool:
        return False


class StaticHA:
    """模拟即发即忘服务：调用返回 null，实体状态始终不变。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.reads = 0

    def entity_lock(self, entity_id: str) -> asyncio.Lock:
        return self._lock

    async def call_service(self, domain: str, service: str, payload: dict):
        return None

    async def get_state(self, entity_id: str, force: bool = False):
        self.reads += 1
        return {"entity_id": entity_id, "state": "open", "attributes": {}}

    def state_stream_ready(self) -> bool:
        return False


def check_invalid_attributes() -> None:
    cases = [
        ({"temperature": 35}, {"min_temp": 16, "max_temp": 30}, "温度超上限被拒绝"),
        ({"color_temp_kelvin": 7000},
         {"min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500}, "色温超上限被拒绝"),
        ({"hvac_mode": "dry"}, {"hvac_modes": ["off", "heat", "cool"]}, "不支持的空调模式被拒绝"),
        ({"fan_mode": "turbo"}, {"fan_modes": ["low", "high"]}, "不支持的风速被拒绝"),
        ({"brightness_pct": 1000}, {}, "亮度百分比超范围被拒绝"),
        ({"volume_level": 2}, {}, "音量超范围被拒绝"),
        ({"position": 999}, {}, "位置超范围被拒绝"),
        ({"temperature": 26}, {"temperature_unit": "°F", "min_temp": 60, "max_temp": 80},
         "华氏设备收到疑似摄氏温度被拒绝"),
    ]
    for extra, attrs, label in cases:
        try:
            validate_action_attributes("climate", extra, attrs)
        except HAError as exc:
            check(exc.code == "invalid_value", label)
        else:
            raise AssertionError(label + "（未拒绝 " + repr(extra) + "）")
    try:
        _rgb_components([1.5, 2, 3])
    except HAError as exc:
        check(exc.code == "invalid_value", "RGB 浮点分量被拒绝")
    else:
        raise AssertionError("RGB 浮点分量未被拒绝")


def check_filter_actions() -> None:
    onoff = _filter_actions("light", ["turn_on", "set_brightness", "set_color_temp", "set_color"],
                            {"supported_color_modes": ["onoff"]})
    check(onoff == ["turn_on"], "onoff 灯不暴露亮度/色温/颜色动作")
    basic_cover = _filter_actions("cover", ["open", "close", "stop", "set_position"],
                                  {"supported_features": 3})
    check(basic_cover == ["open", "close"], "不支持 stop/position 的窗帘被过滤")
    full_cover = _filter_actions("cover", ["open", "close", "stop", "set_position"],
                                 {"supported_features": 15})
    check(len(full_cover) == 4, "能力完整的窗帘保留全部动作")
    no_color = _filter_actions("light", ["turn_on", "set_brightness", "set_color_temp", "set_color"],
                               {"supported_color_modes": []})
    check(no_color == ["turn_on"], "颜色模式为空时只保留开关动作")
    basic_vacuum = _filter_actions("vacuum", ["turn_on", "turn_off", "stop", "pause", "continue", "resume"],
                                   {"supported_features": 8192})
    check(basic_vacuum == ["turn_on", "continue", "resume"], "只支持启动的扫地机保留启动类动作")
    full_vacuum = _filter_actions("vacuum", ["turn_on", "turn_off", "stop", "pause", "continue", "resume"],
                                  {"supported_features": 15})
    check(full_vacuum == ["turn_off", "stop", "pause"], "扫地机按能力位保留停止/暂停动作")
    range_only = _filter_actions("climate", ["turn_on", "turn_off", "set_temperature", "set_hvac_mode"],
                                 {"supported_features": 2, "hvac_modes": ["heat", "off"]})
    check("set_temperature" not in range_only, "仅支持温度区间的空调不暴露单值 set_temperature")
    full_climate = _filter_actions("climate", ["turn_on", "turn_off", "set_temperature"],
                                   {"supported_features": 1})
    check("set_temperature" in full_climate, "支持单值目标温度时保留 set_temperature")


def check_invalid_number(value, action: str, label: str) -> None:
    try:
        build_service(action, "climate" if action == "set_temperature" else "light", value)
    except HAError as exc:
        check(exc.code == "invalid_value", label)
    else:
        raise AssertionError(label + "（未拒绝 " + repr(value) + "）")


async def run() -> None:
    check_invalid_number(float("nan"), "set_brightness", "set_brightness 拒绝 NaN")
    check_invalid_number(float("inf"), "set_brightness", "set_brightness 拒绝 Infinity")
    check_invalid_number(float("nan"), "set_temperature", "set_temperature 拒绝 NaN")
    check_invalid_number(float("inf"), "set_temperature", "set_temperature 拒绝 Infinity")
    check_invalid_number(float("inf"), "set_color_temp", "set_color_temp 拒绝 Infinity")
    check_invalid_attributes()
    check_filter_actions()
    ha = SlowHA()
    st = {"entity_id": "light.slow", "state": "off", "attributes": {}}
    result = await _apply_and_confirm(
        ha, st, "light", "turn_on", {"entity_id": "light.slow"}, "打开")
    check(result.get("status") == "confirmed", "慢设备 1s 回写仍被确认")
    check((result.get("after") or {}).get("state") == "on", "慢设备回读状态为 on")
    check(ha.reads >= 4, "回读次数超过旧的 4 次上限（窗口确实延长）")
    static = StaticHA()
    accepted = await _apply_and_confirm(
        static, {"entity_id": "cover.c", "state": "open", "attributes": {}},
        "cover", "stop_cover", {"entity_id": "cover.c"}, "停止")
    check(accepted.get("status") == "accepted", "即发即忘服务返回 accepted 而非失败")
    check(static.reads <= 6, "即发即忘服务不会等满回读超时")
    fail = RefreshFailHA()
    guarded = await _apply_and_confirm(
        fail, {"entity_id": "cover.garage", "state": "closed", "attributes": {}},
        "cover", "open_cover", {"entity_id": "cover.garage"}, "打开",
        precheck=lambda fresh: {"ok": False, "code": "danger"})
    check((guarded.get("_danger_guard") or {}).get("code") == "state_unavailable",
          "锁内刷新失败且需要危险复查时返回 state_unavailable")
    check(fail.called is False, "刷新失败时不调用 call_service")
    print("CONFIRM WINDOW OK")


if __name__ == "__main__":
    asyncio.run(run())

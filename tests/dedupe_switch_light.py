#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""switch/light 去重判据的单点自测（不依赖网络与 HA）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from homeassistant_xiaozhiserver.views import _normalize_label, redundant_switch_map  # noqa: E402


def state(eid: str, name: str) -> dict:
    return {"entity_id": eid, "state": "off", "attributes": {"friendly_name": name}}


def ent(device_id: str | None) -> dict:
    return {"device_id": device_id}


def run() -> None:
    assert _normalize_label("  Living   Room Light ") == "living room light"

    # 同一设备、1 light + 1 switch、同名：只隐藏 switch
    states = [state("light.living_room", "客厅灯"), state("switch.dup", "客厅灯")]
    hidden, ambiguous = redundant_switch_map(
        states, {"dev_living": {"name": "客厅环境组"}},
        {"light.living_room": ent("dev_living"), "switch.dup": ent("dev_living")})
    assert hidden == {"switch.dup": "light.living_room"}, hidden
    assert ambiguous == [], ambiguous

    # 只有开关没有灯具：保留
    hidden, ambiguous = redundant_switch_map(
        [state("switch.only", "电风扇")], {},
        {"switch.only": ent("dev_fan")})
    assert hidden == {} and ambiguous == []

    # 1 light + 2 switches：无法唯一确定，不隐藏并记录
    states = [state("light.a", "客厅灯"), state("switch.a", "客厅灯"), state("switch.b", "客厅灯")]
    hidden, ambiguous = redundant_switch_map(
        states, {}, {s["entity_id"]: ent("dev_multi") for s in states})
    assert hidden == {}, hidden
    assert len(ambiguous) == 1 and ambiguous[0]["device_id"] == "dev_multi", ambiguous

    # 1 light + 1 switch 但名称不同（独立配置开关）：不隐藏并记录
    states = [state("light.monitor", "显示器挂灯"), state("switch.power_on", "显示器挂灯 灯 默认上电状态")]
    hidden, ambiguous = redundant_switch_map(
        states, {}, {s["entity_id"]: ent("dev_monitor") for s in states})
    assert hidden == {}, hidden
    assert len(ambiguous) == 1, ambiguous

    # 无 device_id：不能判定为同一物理设备
    hidden, ambiguous = redundant_switch_map(
        [state("light.x", "同名灯"), state("switch.x", "同名灯")], {}, {})
    assert hidden == {} and ambiguous == []

    print("DEDUPE OK")


if __name__ == "__main__":
    run()

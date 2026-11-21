# -*- coding: utf-8 -*-
"""实体摘要、可控性标记与按设备聚合视图。"""
from __future__ import annotations

from typing import Any, Optional

from .config import EXCLUDED_DEVICE_KEYWORDS, EXCLUDED_DOMAINS
from .danger import _danger_info
from .service import DOMAIN_ACTIONS, _domain_of, _friendly_name

# get_device_state 默认返回的关键属性；避免媒体/摄像头等实体把巨大 attributes 全塞给 LLM。
KEY_ATTRIBUTES = {
    "friendly_name", "device_class", "unit_of_measurement", "icon",
    "brightness", "color_mode", "color_temp_kelvin", "rgb_color", "supported_color_modes",
    "volume_level", "is_volume_muted", "media_title", "media_artist", "source",
    "temperature", "current_temperature", "target_temp_high", "target_temp_low",
    "hvac_mode", "hvac_action", "fan_mode", "swing_mode", "preset_mode",
    "current_position", "current_tilt_position", "percentage", "speed",
    "battery_level", "battery_icon", "state_class",
}


def _brief_attributes(attributes: Any) -> tuple[dict, bool]:
    """返回 (关键属性, 是否被截断)。非 dict 属性按空处理。"""
    if not isinstance(attributes, dict):
        return {}, False
    out = {k: v for k, v in attributes.items() if k in KEY_ATTRIBUTES}
    return out, len(out) < len(attributes)


def summarize(st: dict) -> dict:
    a = st.get("attributes")
    if not isinstance(a, dict):
        a = {}
    return {
        "entity_id": st.get("entity_id"),
        "name": _friendly_name(st),
        "domain": _domain_of(st.get("entity_id", "")),
        "state": st.get("state"),
        "unit": a.get("unit_of_measurement"),
        "device_class": a.get("device_class"),
    }

def _controllability(entity_id: str, state: Any) -> dict:
    """判断一个实体是否可控制，并给出内置动作列表。"""
    domain = _domain_of(entity_id)
    actions = list(DOMAIN_ACTIONS.get(domain, []))
    if not actions:
        return {"controllable": False, "actions": [],
                "controllable_reason": "该类型无内置控制动作，可先用 list_services 查能力，再用 call_service 调用"}
    if str(state) in ("unavailable", "unknown"):
        return {"controllable": False, "actions": actions,
                "controllable_reason": "设备当前状态不可用（" + str(state) + "）"}
    return {"controllable": True, "actions": actions, "controllable_reason": "可控制"}

def _registry_maps(reg: Optional[dict]) -> tuple:
    devices: dict = {}
    ents: dict = {}
    areas: dict = {}
    if not reg:
        return devices, ents, areas
    for d in reg.get("devices") or []:
        if isinstance(d, dict) and d.get("id"):
            devices[d["id"]] = d
    for e in reg.get("entities") or []:
        if isinstance(e, dict) and e.get("entity_id"):
            ents[e["entity_id"]] = e
    for a in reg.get("areas") or []:
        if isinstance(a, dict) and a.get("area_id"):
            areas[a["area_id"]] = a.get("name") or a.get("area_id")
    return devices, ents, areas

def _device_label(d: Optional[dict]) -> Optional[str]:
    if not d:
        return None
    return d.get("name_by_user") or d.get("name") or d.get("id")


def is_excluded_entity(st: dict, devices: Optional[dict] = None, ents: Optional[dict] = None) -> bool:
    """判断实体是否属于本 MCP 服务不对外暴露的类型或设备。

    domain 命中 EXCLUDED_DOMAINS，或实体名/entity_id/设备名包含
    EXCLUDED_DEVICE_KEYWORDS 中任一关键字时返回 True。
    """
    eid = str(st.get("entity_id", ""))
    if _domain_of(eid) in EXCLUDED_DOMAINS:
        return True
    if not EXCLUDED_DEVICE_KEYWORDS:
        return False
    texts = [eid, _friendly_name(st)]
    re = (ents or {}).get(eid) or {}
    did = re.get("device_id")
    d = (devices or {}).get(did) if did else None
    label = _device_label(d)
    if label:
        texts.append(str(label))
    hay = " ".join(texts).lower()
    return any(k in hay for k in EXCLUDED_DEVICE_KEYWORDS)


def _normalize_label(value: Any) -> str:
    """归一化名称：折叠连续空白并忽略大小写。"""
    return " ".join(str(value or "").split()).casefold()


def redundant_switch_map(
    states: list,
    devices: Optional[dict] = None,
    ents: Optional[dict] = None,
) -> tuple[dict, list]:
    """找出与灯具重复的开关实体。

    判据：同一非空 device_id 下恰好 1 个 light 与 1 个 switch，且两者
    friendly_name 归一化后完全相等。此时 switch 视为同一被控对象的重复
    入口，返回 {switch_entity_id: light_entity_id}。
    其余 light+switch 混合组无法唯一确定，返回在 ambiguous 列表中而不隐藏。
    """
    groups: dict = {}
    for st in states or []:
        if not isinstance(st, dict):
            continue
        eid = str(st.get("entity_id") or "")
        domain = _domain_of(eid)
        if domain not in ("light", "switch"):
            continue
        re_ent = (ents or {}).get(eid) or {}
        did = re_ent.get("device_id")
        if not did:
            continue
        g = groups.setdefault(str(did), {"lights": [], "switches": []})
        g["lights" if domain == "light" else "switches"].append(st)

    hidden: dict = {}
    ambiguous: list = []
    for did, g in groups.items():
        lights = g["lights"]
        switches = g["switches"]
        if not lights or not switches:
            continue
        d = (devices or {}).get(did) or {}
        info = {
            "device_id": did,
            "device_name": _device_label(d),
            "lights": [str(x.get("entity_id") or "") for x in lights],
            "switches": [str(x.get("entity_id") or "") for x in switches],
        }
        if len(lights) == 1 and len(switches) == 1:
            light_label = _normalize_label(_friendly_name(lights[0]))
            switch_label = _normalize_label(_friendly_name(switches[0]))
            if light_label and light_label == switch_label:
                hidden[str(switches[0].get("entity_id") or "")] = str(lights[0].get("entity_id") or "")
                continue
            info["reason"] = "同一设备下开关与灯具名称不同，无法确认是重复项，未自动隐藏"
        else:
            info["reason"] = "同一设备下存在多个灯具或多个开关，无法唯一确定重复项，未自动隐藏"
        ambiguous.append(info)
    return hidden, ambiguous


def _drop_redundant_switches(states: list, devices: Optional[dict] = None, ents: Optional[dict] = None) -> tuple[list, dict, list]:
    """过滤掉与灯具重复的开关，返回 (过滤后 states, hidden, ambiguous)。"""
    hidden, ambiguous = redundant_switch_map(states, devices, ents)
    if not hidden:
        return states, hidden, ambiguous
    filtered = [s for s in states if str((s or {}).get("entity_id") or "") not in hidden]
    return filtered, hidden, ambiguous


def _entity_view(st: dict, devices: dict, ents: dict, areas: Optional[dict] = None) -> dict:
    """在实体摘要基础上补充 controllable 标记、所属设备与区域信息。"""
    v = summarize(st)
    v.update(_controllability(st.get("entity_id", ""), st.get("state")))
    v.update(_danger_info(st.get("entity_id", ""), st.get("attributes") or {}))
    eid = str(st.get("entity_id", ""))
    re = ents.get(eid) or {}
    did = re.get("device_id")
    v["device_id"] = did
    d = devices.get(did) if did else None
    v["device_name"] = _device_label(d)
    # HA 语义：实体级 area 覆盖设备级 area。
    area_id = re.get("area_id") or (d.get("area_id") if d else None)
    v["area_id"] = area_id
    v["area_name"] = (areas or {}).get(area_id) if area_id else None
    return v

def _group_by_device(views: list, devices: dict, areas: Optional[dict] = None) -> list:
    """把实体按 device_id 聚合为设备；无 device_id 的实体各自成组。"""
    groups: dict = {}
    order: list = []
    for v in views:
        did = v.get("device_id")
        key = did or ("entity:" + str(v.get("entity_id")))
        g = groups.get(key)
        if g is None:
            d = devices.get(did) if did else None
            area_id = (d.get("area_id") if d else None) or v.get("area_id")
            area_name = (areas or {}).get(area_id) if area_id else None
            if not area_name:
                area_name = v.get("area_name")
            g = {
                "device_id": did,
                "name": _device_label(d) if d else v.get("name"),
                "manufacturer": (d.get("manufacturer") if d else None),
                "model": (d.get("model") if d else None),
                "area_id": area_id,
                "area_name": area_name,
                "controllable": False,
                "dangerous": False,
                "entity_count": 0,
                "entities": [],
            }
            groups[key] = g
            order.append(key)
        g["entities"].append(v)
        g["entity_count"] += 1
        if v.get("controllable"):
            g["controllable"] = True
        if v.get("dangerous"):
            g["dangerous"] = True
    return [groups[k] for k in order]

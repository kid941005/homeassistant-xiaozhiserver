# -*- coding: utf-8 -*-
"""DeviceService：设备查询、状态、控制、服务查询的业务层。

从 server.py 抽出，MCP 工具只做薄包装；WebUI 与 MCP 共用同一套业务逻辑。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .client import HAError, HAClient
from .config import (
    CONTROL_BATCH_TIMEOUT,
    CONTROL_CONCURRENCY,
    DEFAULT_LIST_LIMIT,
    EXCLUDED_DOMAINS,
    MAX_LIST_LIMIT,
)
from .danger import _danger_block, _danger_flags, _service_is_dangerous
from .service import (
    DIRECT_RESPONSE_SERVICES,
    DOMAIN_ACTIONS,
    _apply_and_confirm,
    _domain_of,
    _field_brief,
    _friendly_name,
    _hint,
    build_service,
    validate_action_attributes,
)
from .views import (
    _brief_attributes,
    _controllability,
    _entity_view,
    _drop_redundant_switches,
    _group_by_device,
    _registry_maps,
    is_excluded_entity,
    redundant_switch_map,
)

log = logging.getLogger("homeassistant_xiaozhiserver.devices")

_VERIFIED_OK_STATUSES = ("confirmed", "accepted", "no_change")

def _result_ok(result: dict) -> bool:
    """只有 confirmed、accepted 或 verified 的 no_change 才算真正成功；
    no_change 但 verified=false 说明服务已接受但没看到预期变化，不能报成功。"""
    status = str(result.get("status") or "")
    if status in ("confirmed", "accepted"):
        return True
    return status == "no_change" and bool(result.get("verified"))


def _failure_code(result: dict) -> str:
    """失败时区分“回读不符”与“还没确认”，避免把 no_change 一律当 pending。"""
    status = str(result.get("status") or "")
    if status == "mismatch":
        return "not_verified"
    if status == "no_change" and not bool(result.get("verified")):
        return "not_verified"
    return "pending_confirmation"
_TARGET_SELECTOR_KEYS = frozenset({
    "entity_id", "target", "device_id", "area_id", "floor_id", "label_id",
})
_COMPACT_ENTITY_KEYS = ("entity_id", "name", "domain", "state", "controllable",
                        "device_id", "device_name", "area_id", "area_name")

def _as_bool(value: Any, default: bool = False) -> bool:
    """兼容直接 Python 调用传入字符串的布尔值，避免 "false" 被当成 True。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on", "y"):
            return True
        if v in ("0", "false", "no", "off", "n", ""):
            return False
    return bool(value)


def _parse_pagination(limit: Any, offset: Any) -> Optional[tuple[int, int]]:
    """把 limit/offset 归一化为合法分页。

    limit<=0 回退到 DEFAULT_LIST_LIMIT、offset<0 回退到 0；只有无法转成整数时才返回
    None，由调用方返回 invalid_pagination。
    """
    try:
        if limit is None or (isinstance(limit, str) and not limit.strip()):
            lim = DEFAULT_LIST_LIMIT
        else:
            lim = int(limit)
            if lim <= 0:
                lim = DEFAULT_LIST_LIMIT
        lim = min(lim, MAX_LIST_LIMIT)
        if offset is None or (isinstance(offset, str) and not offset.strip()):
            off = 0
        else:
            off = int(offset)
        return lim, max(0, off)
    except (TypeError, ValueError):
        return None



async def _excluded_entity_error(ha: HAClient, st: dict) -> Optional[dict]:
    """实体属于隐藏类型/设备时返回错误结构，否则返回 None。"""
    try:
        reg = await ha.get_registries()
    except HAError:
        reg = None
    devices_map, ents_map, _ = _registry_maps(reg)
    if not is_excluded_entity(st, devices_map, ents_map):
        return None
    return {"ok": False, "code": "unsupported_device",
            "message": "该设备不在本 MCP 服务开放范围内，已隐藏。",
            "entity_id": str(st.get("entity_id") or "")}


async def _redundant_switch_context(ha: HAClient, states: Optional[list] = None) -> tuple:
    """惰性加载实体状态与注册表，返回 (hidden_switch_map, ambiguous_groups)。

    states 可由 resolve_entity 复用，避免按名称解析时重复拉取全量状态。
    """
    if states is None:
        try:
            states = await ha.get_states()
        except HAError:
            return {}, []
    try:
        reg = await ha.get_registries()
    except HAError:
        reg = None
    devices_map, ents_map, _ = _registry_maps(reg)
    return redundant_switch_map(states, devices_map, ents_map)


def _duplicate_switch_error(st: dict, light_id: str) -> dict:
    eid = str(st.get("entity_id") or "")
    return {
        "ok": False, "code": "duplicate_switch",
        "message": "『" + _friendly_name(st) + "』是与灯具重复的开关，已由灯具 " + light_id + " 替代；请直接使用该灯具实体。",
        "entity_id": eid, "suggested_entity_id": light_id,
    }


async def _resolve_entity_or_error(ha: HAClient, entity: str, verb: str,
                                  states: Optional[list] = None,
                                  hidden: Optional[dict] = None) -> tuple:
    """统一实体解析失败结构，避免四处复制歧义/未找到分支。

    states/hidden 可由批量入口预加载，避免逐设备重复拉取全量状态与重算重复开关映射。
    """
    st, cands, states = await ha.resolve_entity(entity, states)
    switch_ids = []
    if st is not None:
        switch_ids = [str(st.get("entity_id") or "")]
    elif cands:
        switch_ids = [str(c.get("entity_id") or "") for c in cands]
    if hidden is None:
        hidden = {}
        if any(_domain_of(eid) == "switch" for eid in switch_ids):
            hidden, _ = await _redundant_switch_context(ha, states)
    if st is not None:
        eid = str(st.get("entity_id") or "")
        blocked = await _excluded_entity_error(ha, st)
        if blocked is not None:
            return None, blocked
        light_id = hidden.get(eid)
        if light_id:
            return None, _duplicate_switch_error(st, light_id)
        return st, None
    if cands:
        visible = [c for c in cands if str(c.get("entity_id") or "") not in hidden]
        if len(visible) == 1:
            st = visible[0]
            blocked = await _excluded_entity_error(ha, st)
            if blocked is not None:
                return None, blocked
            return st, None
        if len(visible) > 1:
            cands = visible
        elif hidden:
            switch = next((c for c in cands if str(c.get("entity_id") or "") in hidden), None)
            if switch is not None:
                return None, _duplicate_switch_error(
                    switch, hidden[str(switch.get("entity_id") or "")])
        return None, {
            "ok": False, "code": "ambiguous",
            "message": "『" + str(entity) + "』匹配到多个设备，请指定更精确的名称或 entity_id。",
            "candidates": [str(c.get("entity_id")) for c in cands[:10]],
        }
    return None, {
        "ok": False, "code": "not_found",
        "message": "未找到设备『" + str(entity) + "』，无法" + verb + "。可先用 list_devices 查询。",
    }



def _compact_view(view: dict) -> dict:
    return {k: view.get(k) for k in _COMPACT_ENTITY_KEYS if view.get(k) is not None}

def _compact_page(page: list, grouped_by: str) -> list:
    if grouped_by == "entity":
        return [_compact_view(v) for v in page]
    out = []
    for group in page:
        # _group_by_device 的组名字段是 name；兼容读取，避免 compact 模式丢设备名。
        item = {k: group.get(k) for k in
                ("device_id", "device_name", "area_id", "area_name", "entity_count")
                if group.get(k) is not None}
        if "device_name" not in item and group.get("name"):
            item["device_name"] = group["name"]
        item["entities"] = [_compact_view(e) for e in (group.get("entities") or [])]
        out.append(item)
    return out


class DeviceService:
    """设备业务门面：所有方法都是 async，不依赖 MCP 框架。"""

    def __init__(self, ha_provider):
        self._ha_provider = ha_provider

    @property
    def ha(self) -> HAClient:
        return self._ha_provider()

    async def list_devices(self, 
        keyword: Optional[str] = None,
        query: Optional[str] = None,
        q: Optional[str] = None,
        search: Optional[str] = None,
        name: Optional[str] = None,
        domain: Optional[str] = None,
        area: Optional[str] = None,
        controllable_only: Optional[bool] = None,
        group_by: str = "entity",
        compact: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> dict:
        """列出/查询 Home Assistant 中的设备。

        keyword 按设备名称、entity_id 或设备名模糊匹配；也可使用等价别名
        query / q / search / name（只能选一个，传入不同值会报 invalid_argument）。
        domain 指定设备类型（light/switch/sensor/climate/cover/media_player/fan/
        vacuum/scene）；area 指定区域名称或 area_id（如「客厅」「living_room」）。
        多个条件同时生效（如 area="客厅", domain="light" 只返回客厅的灯）。
        group_by="device" 时按物理设备聚合（一个设备下多个实体合并为一项，含
        manufacturer/model/area 与 entities 列表）。compact=true 时只返回
        entity_id/name/domain/state/controllable/device_name/area_name 等精简字段。
        结果支持分页：limit 每页条数，offset 起始偏移（默认 0）。
        未传任何过滤条件（keyword/domain/area）时，为控制返回体积默认只返回可控制
        设备且每页 DEFAULT_LIST_LIMIT（通常 50）条；可用 controllable_only=false、
        limit 显式覆盖。
        未知参数由 MCP SDK 按工具签名过滤，不会传给本方法。

        每个实体带 controllable（是否可控制）、actions（内置动作）、
        device_id/device_name/area_id/area_name。

        重要：当用户明确点名某个设备要开/关时（如「打开客厅灯」「关一下风扇」
        「关掉餐厅氛围灯」），直接调用 control_device(entity="客厅灯",
        action="turn_on")；带房间名和类型时也直接传完整名称，不要先调用
        list_devices 再控制，更不要根据未过滤列表的前几页推断设备不存在或状态。

        适用场景：用户问「有哪些设备」「列出所有的灯」「客厅有什么设备」「哪些设备能控制」。
        """
        aliases = [("keyword", keyword), ("query", query), ("q", q),
                   ("search", search), ("name", name)]
        provided = [(k, v.strip()) for k, v in aliases if isinstance(v, str) and v.strip()]
        distinct = {v for _, v in provided}
        if len(distinct) > 1:
            detail = "、".join(k + "=" + v for k, v in provided)
            return {"ok": False, "code": "invalid_argument",
                    "message": "关键字参数只能提供一个（keyword/query/q/search/name），"
                               "但收到不同取值：" + detail + "。"}
        keyword = provided[0][1] if provided else None

        has_filter = bool(keyword or domain or area)
        applied_defaults: dict = {}
        if controllable_only is None:
            controllable_only = not has_filter
            if not has_filter:
                applied_defaults["controllable_only"] = True
        else:
            controllable_only = _as_bool(controllable_only)
        if limit is None and not has_filter:
            limit = DEFAULT_LIST_LIMIT
            applied_defaults["limit"] = limit

        try:
            page_args = _parse_pagination(limit, offset)
            if page_args is None:
                return {"ok": False, "code": "invalid_pagination",
                        "message": "limit/offset 必须是整数：limit 为正整数，offset 为非负整数。"}
            lim, off = page_args
            ha = self._ha_provider()
            states, reg = await asyncio.gather(ha.get_states(), ha.get_registries(), return_exceptions=True)
            if isinstance(states, BaseException):
                raise states
            if isinstance(reg, asyncio.CancelledError):
                raise reg
            if isinstance(reg, BaseException):
                reg = None
            if area and reg is None:
                return {"ok": False, "code": "registry_unavailable",
                        "message": "无法读取设备/区域注册表，无法按 area 过滤。请稍后重试，或改用 keyword/domain 过滤。"}
            devices_map, ents_map, areas_map = _registry_maps(reg)
            states = [s for s in states if not is_excluded_entity(s, devices_map, ents_map)]
            states, hidden_switches, ambiguous_switch_groups = _drop_redundant_switches(
                states, devices_map, ents_map)
            if hidden_switches:
                log.info("list_devices hidden redundant switches=%d", len(hidden_switches))
            if ambiguous_switch_groups:
                log.warning("list_devices ambiguous switch/light groups=%s", ambiguous_switch_groups)
            views = [_entity_view(s, devices_map, ents_map, areas_map) for s in states]
            if domain:
                d = domain.strip().lower()
                views = [v for v in views if str(v.get("entity_id", "")).startswith(d + ".")]
            if keyword:
                kw = keyword.strip().lower()
                views = [
                    v for v in views
                    if kw in str(v.get("name", "")).lower()
                    or kw in str(v.get("entity_id", "")).lower()
                    or kw in str(v.get("device_name") or "").lower()
                ]
            if area:
                av = area.strip().lower()
                views = [
                    v for v in views
                    if av in str(v.get("area_name") or "").lower()
                    or av in str(v.get("area_id") or "").lower()
                ]
            if controllable_only:
                views = [v for v in views if v.get("controllable")]

            mode = (group_by or "entity").strip().lower()
            if mode in ("device", "devices", "设备"):
                items = _group_by_device(views, devices_map, areas_map)
                grouped_by = "device"
            elif mode in ("entity", "entities", "实体"):
                items = views
                grouped_by = "entity"
            else:
                return {"ok": False, "code": "invalid_group_by",
                        "message": "group_by 仅支持 entity 或 device，收到：" + str(group_by) + "。entity 按实体逐条返回，device 按物理设备聚合。"}

            total = len(items)
            page = items[off:off + lim]
            if compact:
                page = _compact_page(page, grouped_by)
            next_off = off + len(page)
            has_more = next_off < total
            if grouped_by == "device":
                ent_total = sum(int(g.get("entity_count") or 0) for g in items)
                msg = "共 " + str(total) + " 个设备（聚合 " + str(ent_total) + " 个实体），本次返回 " + str(len(page)) + " 个"
            else:
                msg = "共 " + str(total) + " 个实体，本次返回 " + str(len(page)) + " 个"
            msg += "（还有更多，可用 offset=" + str(next_off) + " 继续）" if has_more else "。"
            if applied_defaults:
                notes = []
                hints = []
                if applied_defaults.get("controllable_only"):
                    notes.append("默认仅返回可控制设备")
                    hints.append("controllable_only=false")
                if "limit" in applied_defaults:
                    notes.append("每页 " + str(applied_defaults["limit"]) + " 条")
                    hints.append("limit")
                tail = "（可用 " + " 或 ".join(hints) + " 覆盖）。" if hints else "。"
                msg = "未指定过滤条件，" + "、".join(notes) + tail + msg
            if has_more and not has_filter:
                msg += " 目标设备可能不在当前页，请用 keyword/area 精确过滤后再控制。"
            if hidden_switches:
                msg += "（已自动隐藏 " + str(len(hidden_switches)) + " 个与灯具重复的开关）"
            return {
                "ok": True, "grouped_by": grouped_by, "registry_available": reg is not None,
                "area": (area.strip() if area else None),
                "total": total, "count": len(page), "offset": off,
                "limit": lim, "has_more": has_more,
                "next_offset": next_off if has_more else None,
                "applied_defaults": applied_defaults or None,
                "deduplicated_switch_count": len(hidden_switches),
                "ambiguous_switch_groups": ambiguous_switch_groups or None,
                "devices": page, "message": msg,
            }
        except HAError as e:
            return {"ok": False, **e.to_dict(), "hint": _hint()}
        except Exception as e:  # noqa: BLE001 保证不崩溃
            return {"ok": False, "code": "internal_error", "message": "查询设备列表失败：" + str(e)}

    async def get_device_state(self, entity: str, include_all_attributes: bool = False) -> dict:
        """查询单个设备的当前状态与关键属性。

        entity 可传 entity_id（如 light.living_room）或设备的友好名称（如 客厅灯），
        服务端会先直查、再按名称精确/模糊匹配。多义时返回候选列表。

        默认只返回关键属性（亮度、色温、音量、温度、当前位置等），避免媒体/摄像头等
        实体的巨大 attributes 占用上下文；需要完整属性时传 include_all_attributes=true。
        适用场景：用户问「客厅灯现在开着吗」「卧室温度多少」「空调什么状态」。
        """
        try:
            include_all_attributes = _as_bool(include_all_attributes)
            ha = self._ha_provider()
            st, error = await _resolve_entity_or_error(ha, entity, "查询状态")
            if error is not None:
                return error
            try:
                reg = await ha.get_registries()
            except HAError:
                reg = None
            devices_map, ents_map, areas_map = _registry_maps(reg)
            s = _entity_view(st, devices_map, ents_map, areas_map)
            s["last_changed"] = st.get("last_changed")
            s["last_updated"] = st.get("last_updated")
            all_attrs = st.get("attributes")
            if not isinstance(all_attrs, dict):
                all_attrs = {}
            if include_all_attributes:
                attrs, truncated = all_attrs, False
            else:
                attrs, truncated = _brief_attributes(all_attrs)
            s["attributes"] = attrs
            s["attributes_truncated"] = truncated
            s["attribute_count"] = len(all_attrs)
            msg = s["name"] + " 当前状态：" + str(s["state"])
            if s.get("unit"):
                msg += " " + str(s["unit"])
            if truncated:
                msg += "（默认仅关键属性；完整属性请传 include_all_attributes=true）"
            return {"ok": True, "entity": s, "message": msg}
        except HAError as e:
            return {"ok": False, **e.to_dict(), "hint": _hint()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "code": "internal_error", "message": "查询设备状态失败：" + str(e)}

    async def control_device(self, entity: str, action: str, value: Optional[Any] = None,
                             confirm_token: Optional[str] = None) -> dict:
        """控制设备（开关、亮度、音量、温度、颜色等）。

        参数：
          entity  目标设备：entity_id 或友好名称（会先精确/模糊匹配）。
          action  动作，支持：
            turn_on/turn_off   开关（cover 自动映射 open/close，vacuum 映射 start/stop）
            toggle             切换
            open/close/stop/pause/continue/resume/play  按设备能力映射到对应服务
            lock/unlock        上锁 / 解锁（仅 lock）
            set_brightness     value=亮度百分比 0-100（仅 light，0 表示关闭）
            set_percentage     value=风速百分比 0-100（fan）
            set_position       value=开合百分比 0-100（cover）
            set_hvac_mode      value=模式名（climate，如 heat/cool/auto）
            set_fan_mode       value=风速名（climate，如 low/high/auto）
            set_volume         value=音量百分比 0-100（media_player）
            volume_mute/volume_unmute  静音/取消静音
            set_temperature    value=目标温度（摄氏度，climate）
            set_color_temp     value=色温（开尔文，light）
            set_color          value=颜色 #RRGGBB、"r,g,b" 或 [r,g,b]（light）
          value   动作参数，按 action 需要提供：数字（亮度/音量百分比、温度、色温）、
                  颜色（#RRGGBB 或 "r,g,b"，也可传 [r,g,b] 数组）。
          confirm_token 危险设备二次确认令牌。门锁/安防面板/车库门等危险设备首次调用会返回
                       code=confirmation_required 与 confirm_token（不执行）；用户确认后带上该
                       token 再调用一次才真正执行。令牌一次性且绑定本次操作。

        控制后会在超时窗口内轮询回读，校验状态 + 亮度/温度/音量/颜色等属性是否达到预期，
        返回 verified 与 verification（attempts/checks/mismatches/elapsed_ms）。
        若 HA 接受调用但未达预期会明确返回 mismatch 或 no_change，避免静默假成功。
        适用场景：「把客厅灯亮度调到 50」「关掉风扇」「空调设到 26 度」「电视音量 30」。
        设备名称能直接匹配时不需要先调 list_devices；只有名称不明确或设备不存在时才先查询。
        """
        try:
            ha = self._ha_provider()
            st, error = await _resolve_entity_or_error(ha, entity, "控制")
            if error is not None:
                return error
            eid = str(st.get("entity_id"))
            domain = _domain_of(eid)
            info = _controllability(eid, st.get("state"), st.get("attributes"))
            if not info["controllable"]:
                return {
                    "ok": False, "code": "not_controllable",
                    "message": "『" + _friendly_name(st) + "』（" + domain + "）不可控制：" + info["controllable_reason"] + "。",
                    "entity_id": eid, "domain": domain, "actions": info["actions"],
                }
            action_name = (action or "").strip().lower()
            allowed = info.get("actions") or []
            if action_name not in allowed:
                return {
                    "ok": False, "code": "unsupported_action",
                    "message": "『" + _friendly_name(st) + "』（" + domain + "）不支持动作：" + str(action) +
                               "。该类型可用动作：" + ", ".join(allowed) +
                               "。如需调用其它服务，请先用 list_services 查询，再用 call_service 调用。",
                    "entity_id": eid, "domain": domain, "actions": allowed,
                    "hint": "control_device 只做常用动作封装；任意 HA 服务请用 call_service。",
                }
            sd, svc, extra = build_service(action_name, domain, value)
            validate_action_attributes(domain, extra, st.get("attributes"))
            payload = {"entity_id": eid}
            payload.update(extra)
            guard = _danger_block(st, sd, svc, payload, confirm_token)
            if guard is not None:
                return guard
            result = await _apply_and_confirm(ha, st, sd, svc, payload, action_name)
            result["ok"] = _result_ok(result)
            if not result["ok"]:
                result["code"] = _failure_code(result)
            # 注意：xiaozhi MCP 接入点把结果里的 action 当作 Action 枚举名解析；
            # turn_off / turn_on 不是合法枚举，会被转成 Action.ERROR 并把 "turn_off" 直接 TTS。
            # 因此设备动作放在 device_action，成功时用 action=RESPONSE + 中文 response 让助手直接播报，
            # 省掉一次 LLM 往返；失败/确认/错误分支不返回 action，交给 LLM 处理。
            result["device_action"] = action_name
            if result["ok"]:
                result["action"] = "RESPONSE"
                result["response"] = result.get("voice") or result.get("message") or "操作已完成。"
            else:
                result.pop("action", None)
                result.pop("response", None)
            return result
        except HAError as e:
            return {"ok": False, **e.to_dict(), "hint": "可先用 list_devices / get_device_state 确认设备与能力后重试。"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "code": "internal_error", "message": "控制设备失败：" + str(e)}

    async def control_devices(self, entities: list[str], action: str, value: Optional[Any] = None,
                              confirm_token: Optional[str] = None) -> dict:
        """批量控制多个设备（同一动作、一次危险确认，减少 N 次 MCP 往返）。

        entities 传多个 entity_id 或中文名称；action 与 control_device 相同。
        对每个设备分别回读确认，返回 results 列表与合并后的中文 response。
        任一目标属于危险设备或本次服务高风险时，整批只申请一次 confirm_token；
        用户确认后带同一 token 重试整批即可。单次最多 20 个设备。
        """
        try:
            ha = self._ha_provider()
            action_name = (action or "").strip().lower()
            refs = [str(x).strip() for x in (entities or []) if str(x).strip()]
            if not refs:
                return {"ok": False, "code": "invalid_argument", "message": "entities 不能为空，请提供至少一个设备名称或 entity_id。"}
            if len(refs) > 20:
                return {"ok": False, "code": "invalid_argument", "message": "单次最多批量控制 20 个设备，收到 " + str(len(refs)) + " 个。"}

            resolved: list = []
            errors: list = []
            seen: set[str] = set()
            deduped: list = []
            # 批量入口只拉一次全量状态与重复开关映射，避免每个设备各扫一遍（N+1）。
            batch_states = None
            batch_hidden: Optional[dict] = {}
            try:
                batch_states = await ha.get_states()
                batch_hidden, _ = await _redundant_switch_context(ha, batch_states)
            except HAError:
                batch_states = None
                batch_hidden = None
            for ref in refs:
                st, error = await _resolve_entity_or_error(
                    ha, ref, "控制", batch_states, batch_hidden)
                if error is not None:
                    errors.append({"entity": ref, "error": error})
                    continue
                eid = str(st.get("entity_id"))
                if not eid:
                    continue
                if eid in seen:
                    if eid not in deduped:
                        deduped.append(eid)
                    continue
                seen.add(eid)
                domain = _domain_of(eid)
                info = _controllability(eid, st.get("state"), st.get("attributes"))
                if not info["controllable"]:
                    errors.append({"entity": ref, "entity_id": eid, "code": "not_controllable",
                                   "message": "『" + _friendly_name(st) + "』不可控制：" + info["controllable_reason"]})
                    continue
                allowed = info.get("actions") or []
                if action_name not in allowed:
                    errors.append({"entity": ref, "entity_id": eid, "code": "unsupported_action",
                                   "message": "『" + _friendly_name(st) + "』不支持动作 " + action_name})
                    continue
                try:
                    sd, svc, extra = build_service(action_name, domain, value)
                    validate_action_attributes(domain, extra, st.get("attributes"))
                except HAError as exc:
                    errors.append({"entity": ref, "entity_id": eid, "code": exc.code,
                                   "message": _friendly_name(st) + " 动作参数无效：" + exc.message})
                    continue
                payload = {"entity_id": eid}
                payload.update(extra)
                resolved.append({"st": st, "entity_id": eid, "domain": domain,
                                 "service_domain": sd, "service": svc,
                                 "extra": extra, "payload": payload})

            if not resolved:
                return {"ok": False, "code": "no_valid_entities",
                        "message": "没有可执行的目标设备。", "errors": errors or None}

            rep = None
            for op in resolved:
                dangerous, _reason = _danger_flags(op["entity_id"], op["st"].get("attributes") or {})
                if not dangerous:
                    dangerous, _reason = _service_is_dangerous(op["service_domain"], op["service"])
                if dangerous:
                    rep = op
                    break
            if rep is not None:
                combined = [{"entity_id": op["entity_id"],
                             "service": op["service_domain"] + "." + op["service"],
                             "data": op["extra"]} for op in resolved]
                guard_payload = {"entity_id": [op["entity_id"] for op in resolved], "batch": combined}
                guard = _danger_block(rep["st"], rep["service_domain"], rep["service"],
                                      guard_payload, confirm_token)
                if guard is not None:
                    guard["entities"] = [op["entity_id"] for op in resolved]
                    return guard

            sem = asyncio.Semaphore(CONTROL_CONCURRENCY)
            deadline = time.monotonic() + CONTROL_BATCH_TIMEOUT

            async def _run_one(op):
                async with sem:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    return await asyncio.wait_for(
                        _apply_and_confirm(ha, op["st"], op["service_domain"],
                                           op["service"], op["payload"], action_name),
                        timeout=remaining)

            tasks = [_run_one(op) for op in resolved]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
            per: list = []
            voices: list = []
            failed: list = []
            failed_names: list = []
            for op, result in zip(resolved, raw_results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, asyncio.TimeoutError):
                    per.append({"ok": False, "entity_id": op["entity_id"],
                                "status": "accepted_pending", "code": "pending_confirmation",
                                "device_action": action_name,
                                "message": "批量控制超时，指令已发送但未在时限内确认。"})
                    failed.append(op["entity_id"])
                    failed_names.append(_friendly_name(op["st"]))
                    continue
                if isinstance(result, BaseException):
                    per.append({"ok": False, "entity_id": op["entity_id"], "code": "internal_error",
                                "message": str(result)})
                    failed.append(op["entity_id"])
                    failed_names.append(_friendly_name(op["st"]))
                    continue
                result["ok"] = _result_ok(result)
                if not result["ok"]:
                    result["code"] = _failure_code(result)
                result["device_action"] = action_name
                if result["ok"]:
                    result["action"] = "RESPONSE"
                    result["response"] = result.get("voice") or result.get("message") or "操作已完成。"
                    voices.append(str(result["response"]))
                else:
                    result.pop("action", None)
                    result.pop("response", None)
                    failed.append(op["entity_id"])
                    failed_names.append(_friendly_name(op["st"]))
                per.append(result)

            ok = bool(per) and all(bool(item.get("ok")) for item in per) and not errors
            out = {"ok": ok, "count": len(per), "action": "RESPONSE" if voices else None,
                   "response": "；".join(voices) if voices else "", "results": per}
            if errors:
                out["errors"] = errors
            if deduped:
                out["deduplicated"] = deduped
            failed_count = len(errors) + len(failed)
            if failed_count:
                labels = [str(item.get("entity") or item.get("entity_id") or "") for item in errors]
                labels.extend(failed_names)
                labels = [x for x in labels if x]
                detail = ""
                if labels:
                    detail = "：" + "、".join(labels[:5]) + ("等" if len(labels) > 5 else "")
                out["partial"] = ok is False and bool(voices)
                out["message"] = "部分设备未成功，共 " + str(failed_count) + " 个" + detail + "。"
                if voices:
                    # 直出播报必须把未成功数量说出来，避免只播成功项造成“全部成功”的错觉。
                    out["response"] = out["response"] + "；另有 " + str(failed_count) + " 个设备未成功" + detail + "。"
            if not out["response"]:
                out.pop("response", None)
            return out
        except HAError as e:
            return {"ok": False, **e.to_dict(), "hint": _hint()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "code": "internal_error", "message": "批量控制失败：" + str(e)}

    async def list_services(self, entity: Optional[str] = None, domain: Optional[str] = None,
                            keyword: Optional[str] = None, detailed: Optional[bool] = None) -> dict:
        """查询 Home Assistant 中某类设备/某个设备支持的服务（能力）及参数。

        这是“能力自描述”工具：让调用方在不了解设备细节时，先查清能对这个设备做什么、
        每个服务需要什么参数，再用 call_service 精确调用。

        参数：
          entity   可选，设备 entity_id 或友好名称；给出时只返回该设备所属 domain 的服务。
          domain   可选，直接指定设备类型（如 light/climate/media_player/cover/vacuum）。
          keyword  可选，按服务名或描述关键字过滤（如 "temperature"）。
          detailed 是否返回每个服务的字段参数明细（默认：指定 entity/domain 时为 true，否则仅名称）。
        返回：domain 列表及每个 domain 的服务名/描述/字段。

        适用场景：「空调能做什么操作」「灯支持调色温吗」「这个设备有哪些可控项」。
        """
        try:
            ha = self._ha_provider()
            catalog = await ha.get_services()
            target = None
            if entity:
                st, error = await _resolve_entity_or_error(ha, entity, "查询能力")
                if error is not None:
                    return error
                target = _domain_of(str(st.get("entity_id")))
            elif domain:
                target = domain.strip().lower()

            if target and target in EXCLUDED_DOMAINS:
                return {"ok": False, "code": "no_services",
                        "message": "设备类型『" + str(target) + "』不在本 MCP 服务开放范围内。"}

            kw = (keyword or "").strip().lower()
            detailed = _as_bool(detailed, default=bool(target))

            def match(svc_name: str, svc_def: Any) -> bool:
                if not kw:
                    return True
                if not isinstance(svc_def, dict):
                    svc_def = {}
                text = (svc_name + " " + str(svc_def.get("description", "")) + " " +
                        str(svc_def.get("name", ""))).lower()
                return kw in text

            domains_out = []
            service_total = 0
            for entry in catalog:
                if not isinstance(entry, dict):
                    continue
                d = str(entry.get("domain", ""))
                if not d or d in EXCLUDED_DOMAINS:
                    continue
                if target and d != target:
                    continue
                services = entry.get("services") or {}
                if not isinstance(services, dict):
                    continue
                svc_list = []
                for sname, sdef in services.items():
                    if not match(sname, sdef):
                        continue
                    item = {"service": sname}
                    if isinstance(sdef, dict):
                        if sdef.get("name"):
                            item["name"] = sdef["name"]
                        if sdef.get("description"):
                            item["description"] = sdef["description"]
                        if detailed:
                            fields = sdef.get("fields") or {}
                            if isinstance(fields, dict) and fields:
                                item["fields"] = [_field_brief(fn, fd) for fn, fd in fields.items()]
                    svc_list.append(item)
                if svc_list:
                    svc_list.sort(key=lambda x: x["service"])
                    service_total += len(svc_list)
                    domains_out.append({
                        "domain": d,
                        "service_count": len(svc_list),
                        "services": svc_list if detailed else [x["service"] for x in svc_list],
                    })

            if target and not domains_out:
                return {"ok": False, "code": "no_services",
                        "message": "没有找到 domain=『" + str(target) + "』的服务（或该设备类型无可用服务）。"}
            domains_out.sort(key=lambda x: x["domain"])
            if target:
                msg = "domain=「" + str(target) + "」共 " + str(service_total) + " 个服务。"
            else:
                msg = "共 " + str(len(domains_out)) + " 个 domain、" + str(service_total) + " 个服务。"
            return {"ok": True, "domain": target, "domain_count": len(domains_out),
                    "service_count": service_total, "domains": domains_out, "message": msg}
        except HAError as e:
            return {"ok": False, **e.to_dict(), "hint": _hint()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "code": "internal_error", "message": "查询服务列表失败：" + str(e)}

    async def call_service(self, entity: str, service: str, data: Optional[dict] = None,
                          confirm_token: Optional[str] = None) -> dict:
        """通用服务调用：对任意设备执行 Home Assistant 的任意服务（覆盖 control_device 未封装的场景）。

        参数：
          entity  目标设备：entity_id 或友好名称（会先精确/模糊匹配）。
          service 服务名，可写 "turn_on"（默认用设备所属 domain），或 "climate.set_hvac_mode"
                  （显式带 domain 时以该 domain 为准）。可用 list_services 查询可用服务名。
          data    额外参数（JSON 对象），如 {"brightness":128}、{"temperature":26}、
                  {"hvac_mode":"heat"}、{"rgb_color":[255,0,0]}；无需参数时省略。
          confirm_token 危险设备二次确认令牌，机制同 control_device：对 lock/alarm_control_panel
                  及车库门/门窗类 cover 等危险设备的服务调用，首次只返回确认令牌不执行。

        返回状态回读校验结果（verified / verification）：
          status=confirmed        回读确认状态/属性已达到预期
          status=no_change        HA 已接受但无变化（verified=true 表示本来就是目标状态）
          status=mismatch         回读有变化，但状态/属性与预期不符
          status=accepted_pending HA 返回 null（新版 HA），回读窗口内仍未完全匹配预期
        适用场景：「把空调设成制热模式」「灯切成暖光」「设置风扇风速」「执行自定义脚本」。
        """
        try:
            ha = self._ha_provider()
            st, error = await _resolve_entity_or_error(ha, entity, "调用服务")
            if error is not None:
                return error
            eid = str(st.get("entity_id"))
            spec = (service or "").strip()
            if not spec:
                return {"ok": False, "code": "invalid_service", "message": "service 不能为空。"}
            if "." in spec:
                service_domain, svc = spec.split(".", 1)
                service_domain = service_domain.strip()
                svc = svc.strip()
            else:
                service_domain = _domain_of(eid)
                svc = spec
            if not service_domain or not svc:
                return {"ok": False, "code": "invalid_service",
                        "message": "服务名格式应为 'service' 或 'domain.service'，收到：" + str(service)}
            if data is None:
                extra = {}
            elif isinstance(data, dict):
                extra = dict(data)
            else:
                return {"ok": False, "code": "invalid_data",
                        "message": "data 必须是 JSON 对象（键值对），收到：" + str(type(data).__name__)}
            # 目标选择器只允许来自 entity 参数。否则调用方可在 data 里用 target/device_id
            # 等字段把服务作用到门锁、车库门等另一批实体，绕过 _danger_block 的主实体判定。
            blocked_keys = sorted(set(extra) & _TARGET_SELECTOR_KEYS)
            if blocked_keys:
                return {"ok": False, "code": "invalid_data",
                        "message": "data 不应包含目标选择器 " + "、".join(blocked_keys)
                        + "；目标实体只能由 entity 参数指定，避免绕过危险操作校验。"}
            validate_action_attributes(service_domain, extra, st.get("attributes"))
            payload = {"entity_id": eid}
            payload.update(extra)
            guard = _danger_block(st, service_domain, svc, payload, confirm_token)
            if guard is not None:
                return guard
            result = await _apply_and_confirm(ha, st, service_domain, svc, payload, spec)
            result["ok"] = _result_ok(result)
            if not result["ok"]:
                result["code"] = _failure_code(result)
            result["device_action"] = svc
            direct = (service_domain in DOMAIN_ACTIONS
                      and service_domain not in ("script", "scene", "automation")
                      and svc in DIRECT_RESPONSE_SERVICES) or (
                service_domain == "homeassistant" and svc == "toggle")
            if direct and result["ok"]:
                result["action"] = "RESPONSE"
                result["response"] = result.get("voice") or result.get("message") or "操作已完成。"
            elif not result["ok"]:
                result.pop("action", None)
                result.pop("response", None)
            return result
        except HAError as e:
            out = {"ok": False, **e.to_dict()}
            if e.code in ("not_found", "http_error", "upstream_error", "invalid_data", "invalid_value"):
                out["hint"] = ("该服务可能不存在或参数不匹配。可先用 list_services(domain=…) 查询支持的服"
                               "务与字段，再核对 data 参数名。")
            else:
                out["hint"] = _hint()
            return out
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "code": "internal_error", "message": "调用服务失败：" + str(e)}

    async def list_domains(self, ) -> dict:
        """列出当前所有设备类型（domain）及其数量。

        便于了解家庭有哪些类别的设备，或为 list_devices 选择 domain 过滤条件。
        同时返回危险设备明细（含 garage/door 类 cover），便于确认哪些操作需要二次确认。
        适用场景：用户问「家里都有哪些类型的设备」「哪些设备操作要确认」。
        """
        try:
            ha = self._ha_provider()
            states, reg = await asyncio.gather(ha.get_states(), ha.get_registries(), return_exceptions=True)
            if isinstance(states, BaseException):
                raise states
            if isinstance(reg, asyncio.CancelledError):
                raise reg
            if isinstance(reg, BaseException):
                reg = None
            devices_map, ents_map, _ = _registry_maps(reg)
            states = [s for s in states if not is_excluded_entity(s, devices_map, ents_map)]
            states, hidden_switches, _ = _drop_redundant_switches(states, devices_map, ents_map)
            counts: dict = {}
            dangerous_entities: list = []
            for s in states:
                eid = str(s.get("entity_id", ""))
                d = _domain_of(eid)
                if d:
                    counts[d] = counts.get(d, 0) + 1
                is_danger, reason = _danger_flags(eid, s.get("attributes") or {})
                if is_danger:
                    dangerous_entities.append({
                        "entity_id": eid,
                        "name": _friendly_name(s),
                        "domain": d,
                        "state": s.get("state"),
                        "reason": reason,
                    })
            counts = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
            ctrl = [d for d in counts if DOMAIN_ACTIONS.get(d)]
            danger = sorted({str(e.get("domain") or "") for e in dangerous_entities if e.get("domain")})
            msg = "共 " + str(len(counts)) + " 种设备类型：" + ", ".join(k + "(" + str(v) + ")" for k, v in counts.items())
            if ctrl:
                msg += "；可控类型：" + ", ".join(ctrl)
            if danger:
                msg += "；需二次确认的危险类型：" + ", ".join(danger)
            if dangerous_entities:
                msg += "（共 " + str(len(dangerous_entities)) + " 个危险实体）"
            if hidden_switches:
                msg += "；已自动隐藏 " + str(len(hidden_switches)) + " 个与灯具重复的开关"
            return {"ok": True, "domains": counts, "controllable_domains": ctrl,
                    "dangerous_domains": danger,
                    "dangerous_entities": dangerous_entities[:50],
                    "dangerous_entity_count": len(dangerous_entities),
                    "deduplicated_switch_count": len(hidden_switches),
                    "message": msg}
        except HAError as e:
            return {"ok": False, **e.to_dict(), "hint": _hint()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "code": "internal_error", "message": "查询设备类型失败：" + str(e)}

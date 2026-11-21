# -*- coding: utf-8 -*-
"""homeassistant-xiaozhiserver — 小智 MCP 服务（Home Assistant 桥）。

把 Home Assistant 的设备能力封装成标准 MCP 工具，供小智（xiaozhi-esp32-server）
通过 MCP 接入点调用。本进程固定以 stdio 运行，由 mcp_pipe.py 桥接 WebSocket。

模块划分：
  config.py    环境变量解析与运行常量（非法值回退并告警）
  client.py    HA REST/WebSocket 客户端、鉴权与错误归一化
  service.py   动作映射、状态/属性预期、回读校验
  danger.py    危险设备判定与一次性确认令牌
  views.py     实体摘要、可控性标记、按设备聚合
  server.py    MCP 工具定义与 stdio 进程入口
"""
from __future__ import annotations

import functools
import logging
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from mcp.server.mcpserver import MCPServer

from .config import DEFAULT_LIST_LIMIT, LOG_LEVEL, MAX_LIST_LIMIT
from .client import HAError, HAClient
from .danger import _danger_block, _danger_flags
from .service import (
    DIRECT_RESPONSE_SERVICES,
    DOMAIN_ACTIONS,
    _apply_and_confirm,
    _domain_of,
    _field_brief,
    _friendly_name,
    _hint,
    build_service,
)
from .views import (
    _brief_attributes,
    _controllability,
    _entity_view,
    _group_by_device,
    _registry_maps,
)
from .logging_setup import reset_call_id, set_call_id, setup_logging

log = logging.getLogger("homeassistant_xiaozhiserver.server")
setup_logging()

mcp = MCPServer(
    "homeassistant-xiaozhiserver",
    instructions=(
        "把 Home Assistant 的智能家居设备封装为 MCP 工具。"
        "查看设备用 list_devices（可按 keyword 名称关键字或 domain 设备类型过滤，支持分页；"
        "结果带 controllable 标记说明是否可控制；controllable_only=true 只看可控设备；"
        "group_by=\"device\" 按物理设备聚合多个实体）；"
        "查看单个设备状态用 get_device_state（entity 可传 entity_id 或中文名称；"
        "默认只返回关键属性，include_all_attributes=true 可返回完整属性）；"
        "查询某类设备支持哪些服务与参数用 list_services；"
        "常规控制直接用 control_device 并传用户说的设备名称，不要先 list_devices；"
        "只有名称不明确、需要确认设备能力或设备不存在时才查列表/服务（action 见工具描述，"
        "value 用于亮度/音量/温度等，控制后自动回读校验状态与属性）；"
        "对 lock、alarm_control_panel 以及车库门/门窗类 cover 等危险设备的操作会先返回 "
        "confirmation_required 与 confirm_token；必须让用户明确确认后，带 confirm_token 再调用一次；"
        "需要执行上述未覆盖的任意 HA 服务时用 call_service（服务名 + 参数 JSON）；"
        "不确定设备类型时先用 list_domains 查看有哪些类别。"
    ),
    log_level=LOG_LEVEL,
)

_ha: Optional[HAClient] = None


def get_ha() -> HAClient:
    global _ha
    if _ha is None:
        _ha = HAClient()
    return _ha


_SENSITIVE_EXACT = {
    "code", "pin", "passcode", "password", "token", "secret", "authorization",
    "cookie", "credential", "api_key", "apikey", "access_key", "private_key",
}


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    if k in _SENSITIVE_EXACT:
        return True
    return any(m in k for m in ("token", "password", "secret", "authorization",
                                "cookie", "passcode", "credential"))


def _short(value: Any, limit: int = 200) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _safe_value(value: Any, depth: int = 0) -> Any:
    """递归脱敏日志参数；限制深度/数量，避免超大对象拖慢日志。"""
    if depth >= 3:
        if isinstance(value, (dict, list, tuple)):
            return "<max-depth>"
        return _short(value)
    if isinstance(value, dict):
        return {str(k): ("***" if _is_sensitive(str(k)) else _safe_value(v, depth + 1))
                for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_safe_value(v, depth + 1) for v in list(value)[:20]]
    return _short(value)


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
    """把 limit/offset 归一化为合法分页；非法输入返回 None，由调用方返回结构化错误。"""
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


def _accept_extra_arguments(tool_name: str) -> bool:
    """让指定工具能收到未声明的参数，以便显式报错而不是被 MCP SDK 静默丢弃。

    MCP SDK 默认按函数签名生成参数模型并忽略多余键，导致 model 传错参数名时
    悄悄拿到全量数据。这里把该工具的校验模型换成允许额外字段的版本，
    由工具函数自己检查 **extra 并返回 invalid_argument。

    该函数依赖 mcp 私有内部结构；任何不兼容都应降级为“不启用严格校验”，
    不能让 MCP server 或 WebUI 在导入阶段崩溃。
    """
    try:
        from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
        from pydantic import ConfigDict, create_model

        tool = mcp._tool_manager.get_tool(tool_name)
        if tool is None:
            log.warning("无法为 %s 启用未知参数校验（工具未注册）", tool_name)
            return False
        base_model = tool.fn_metadata.arg_model
        # **extra 会被 SDK 当成必填字段，发布前要剔除；extra="allow" 仍能接住多余参数
        field_defs = {name: (fi.annotation, fi) for name, fi in base_model.model_fields.items()
                      if name != "extra"}

        class _ArgsWithExtra(ArgModelBase):
            model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

            def model_dump_one_level(self) -> dict:
                data = super().model_dump_one_level()
                extra = self.__pydantic_extra__
                if extra:
                    data.update(extra)
                return data

        tool.fn_metadata.arg_model = create_model(
            base_model.__name__ + "WithExtra", __base__=_ArgsWithExtra, **field_defs
        )
        params = dict(tool.parameters)
        properties = dict(params.get("properties") or {})
        properties.pop("extra", None)
        params["properties"] = properties
        if "required" in params:
            required = [n for n in params["required"] if n != "extra"]
            if required:
                params["required"] = required
            else:
                params.pop("required", None)
        tool.parameters = params
        return True
    except Exception:  # noqa: BLE001 mcp 内部 API 变动时优雅降级
        log.warning("无法为 %s 启用未知参数校验（mcp 内部 API 不可用或不兼容）", tool_name, exc_info=True)
        return False


def _safe_url(raw: str) -> str:
    """日志脱敏：去掉 URL 中的 userinfo、query 与 fragment。"""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "(invalid)"
    if not parsed.scheme or not parsed.netloc:
        return raw
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _safe_log_args(args: tuple, kwargs: dict) -> dict:
    out: dict = {}
    for i, v in enumerate(args):
        out["arg" + str(i)] = _safe_value(v)
    for k, v in kwargs.items():
        out[str(k)] = "***" if _is_sensitive(str(k)) else _safe_value(v)
    return out


def _log_tool(name: str):
    """记录每次 MCP 工具调用的入参摘要、耗时与结果码；不记录令牌。"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            call_id = uuid.uuid4().hex[:8]
            token = set_call_id(call_id)
            started = time.perf_counter()
            log.debug("tool=%s start args=%s", name, _safe_log_args(args, kwargs))
            try:
                result = await fn(*args, **kwargs)
            except Exception:
                log.exception("tool=%s exception", name)
                raise
            else:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                if isinstance(result, dict) and result.get("ok") is False:
                    log.warning("tool=%s done ok=false code=%s elapsed_ms=%s message=%s",
                                name, result.get("code"), elapsed_ms, _short(result.get("message"), 300))
                elif isinstance(result, dict) and result.get("status") == "mismatch":
                    log.warning("tool=%s done status=mismatch elapsed_ms=%s entity=%s service=%s",
                                name, elapsed_ms, result.get("entity_id"), result.get("service"))
                else:
                    log.info("tool=%s done ok=true elapsed_ms=%s", name, elapsed_ms)
                return result
            finally:
                reset_call_id(token)
        return wrapper
    return deco


async def _resolve_entity_or_error(ha: HAClient, entity: str, verb: str) -> tuple:
    """统一实体解析失败结构，避免四处复制歧义/未找到分支。"""
    st, cands = await ha.resolve_entity(entity)
    if st is not None:
        return st, None
    if cands:
        return None, {
            "ok": False, "code": "ambiguous",
            "message": "『" + str(entity) + "』匹配到多个设备，请指定更精确的名称或 entity_id。",
            "candidates": [str(c.get("entity_id")) for c in cands[:10]],
        }
    return None, {
        "ok": False, "code": "not_found",
        "message": "未找到设备『" + str(entity) + "』，无法" + verb + "。可先用 list_devices 查询。",
    }


@mcp.tool()
@_log_tool("list_devices")
async def list_devices(
    keyword: Optional[str] = None,
    query: Optional[str] = None,
    q: Optional[str] = None,
    search: Optional[str] = None,
    name: Optional[str] = None,
    domain: Optional[str] = None,
    area: Optional[str] = None,
    controllable_only: Optional[bool] = None,
    group_by: str = "entity",
    limit: Optional[int] = None,
    offset: int = 0,
    **extra: Any,
) -> dict:
    """列出/查询 Home Assistant 中的设备。

    keyword 按设备名称、entity_id 或设备名模糊匹配；也可使用等价别名
    query / q / search / name（只能选一个，传入不同值会报 invalid_argument）。
    domain 指定设备类型（light/switch/sensor/climate/cover/media_player/fan/
    vacuum/scene）；area 指定区域名称或 area_id（如「客厅」「living_room」）。
    多个条件同时生效（如 area="客厅", domain="light" 只返回客厅的灯）。
    group_by="device" 时按物理设备聚合（一个设备下多个实体合并为一项，含
    manufacturer/model/area 与 entities 列表）。
    结果支持分页：limit 每页条数，offset 起始偏移（默认 0）。
    未传任何过滤条件（keyword/domain/area）时，为控制返回体积默认只返回可控制
    设备且每页 DEFAULT_LIST_LIMIT（通常 50）条；可用 controllable_only=false、
    limit 显式覆盖。
    传入本工具不支持的参数会返回 invalid_argument 并列出可用参数。

    每个实体带 controllable（是否可控制）、actions（内置动作）、
    device_id/device_name/area_id/area_name。

    重要：当用户明确点名某个设备要开/关时（如「打开客厅灯」「关一下风扇」
    「关掉餐厅氛围灯」），直接调用 control_device(entity="客厅灯",
    action="turn_on")；带房间名和类型时也直接传完整名称，不要先调用
    list_devices 再控制，更不要根据未过滤列表的前几页推断设备不存在或状态。

    适用场景：用户问「有哪些设备」「列出所有的灯」「客厅有什么设备」「哪些设备能控制」。
    """
    known_args = ("keyword", "query", "q", "search", "name", "domain", "area",
                  "controllable_only", "group_by", "limit", "offset")
    if extra:
        unknown = "、".join(sorted(str(k) for k in extra))
        return {"ok": False, "code": "invalid_argument",
                "message": "不支持的参数：" + unknown + "。可用参数：" + "、".join(known_args) + "。"}

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
        ha = get_ha()
        states = await ha.get_states()
        try:
            reg = await ha.get_registries()
        except HAError:
            reg = None
        devices_map, ents_map, areas_map = _registry_maps(reg)
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
        return {
            "ok": True, "grouped_by": grouped_by, "registry_available": reg is not None,
            "area": (area.strip() if area else None),
            "total": total, "count": len(page), "offset": off,
            "limit": lim, "has_more": has_more,
            "next_offset": next_off if has_more else None,
            "applied_defaults": applied_defaults or None,
            "devices": page, "message": msg,
        }
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": _hint()}
    except Exception as e:  # noqa: BLE001 保证不崩溃
        return {"ok": False, "code": "internal_error", "message": "查询设备列表失败：" + str(e)}


# 让 list_devices 显式拒绝未知参数（否则会被 MCP SDK 静默忽略）
_accept_extra_arguments("list_devices")


@mcp.tool()
@_log_tool("get_device_state")
async def get_device_state(entity: str, include_all_attributes: bool = False) -> dict:
    """查询单个设备的当前状态与关键属性。

    entity 可传 entity_id（如 light.living_room）或设备的友好名称（如 客厅灯），
    服务端会先直查、再按名称精确/模糊匹配。多义时返回候选列表。

    默认只返回关键属性（亮度、色温、音量、温度、当前位置等），避免媒体/摄像头等
    实体的巨大 attributes 占用上下文；需要完整属性时传 include_all_attributes=true。
    适用场景：用户问「客厅灯现在开着吗」「卧室温度多少」「空调什么状态」。
    """
    try:
        include_all_attributes = _as_bool(include_all_attributes)
        ha = get_ha()
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


@mcp.tool()
@_log_tool("control_device")
async def control_device(entity: str, action: str, value: Optional[Any] = None,
                         confirm_token: Optional[str] = None) -> dict:
    """控制设备（开关、亮度、音量、温度、颜色等）。

    参数：
      entity  目标设备：entity_id 或友好名称（会先精确/模糊匹配）。
      action  动作，支持：
        turn_on/turn_off   开关（cover 自动映射 open/close，vacuum 映射 start/stop）
        toggle             切换
        open/close/stop/pause/continue/resume/play  按设备能力映射到对应服务
        lock/unlock        上锁 / 解锁（仅 lock）
        set_brightness     value=亮度百分比 1-100（仅 light）
        set_volume         value=音量百分比 1-100（media_player）
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
        ha = get_ha()
        st, error = await _resolve_entity_or_error(ha, entity, "控制")
        if error is not None:
            return error
        eid = str(st.get("entity_id"))
        domain = _domain_of(eid)
        info = _controllability(eid, st.get("state"))
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
        payload = {"entity_id": eid}
        payload.update(extra)
        guard = _danger_block(st, sd, svc, payload, confirm_token)
        if guard is not None:
            return guard
        result = await _apply_and_confirm(ha, st, sd, svc, payload, action_name)
        result["ok"] = True
        # 注意：xiaozhi MCP 接入点把结果里的 action 当作 Action 枚举名解析；
        # turn_off / turn_on 不是合法枚举，会被转成 Action.ERROR 并把 "turn_off" 直接 TTS。
        # 因此设备动作放在 device_action，成功时用 action=RESPONSE + 中文 response 让助手直接播报，
        # 省掉一次 LLM 往返；确认/错误分支不返回 action，仍交给 LLM 处理。
        result["device_action"] = action_name
        result["action"] = "RESPONSE"
        result["response"] = result.get("voice") or result.get("message") or "操作已完成。"
        return result
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": "可先用 list_devices / get_device_state 确认设备与能力后重试。"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "internal_error", "message": "控制设备失败：" + str(e)}


@mcp.tool()
@_log_tool("list_services")
async def list_services(entity: Optional[str] = None, domain: Optional[str] = None,
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
        ha = get_ha()
        catalog = await ha.get_services()
        target = None
        if entity:
            st, error = await _resolve_entity_or_error(ha, entity, "查询能力")
            if error is not None:
                return error
            target = _domain_of(str(st.get("entity_id")))
        elif domain:
            target = domain.strip().lower()

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
            if not d:
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


@mcp.tool()
@_log_tool("call_service")
async def call_service(entity: str, service: str, data: Optional[dict] = None,
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
        ha = get_ha()
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
        if "entity_id" in extra:
            return {"ok": False, "code": "invalid_data",
                    "message": "data 不应包含 entity_id；目标实体由 entity 参数指定，避免覆盖解析结果。"}
        payload = {"entity_id": eid}
        payload.update(extra)
        guard = _danger_block(st, service_domain, svc, payload, confirm_token)
        if guard is not None:
            return guard
        result = await _apply_and_confirm(ha, st, service_domain, svc, payload, spec)
        result["ok"] = True
        result["device_action"] = svc
        direct = (service_domain in DOMAIN_ACTIONS and svc in DIRECT_RESPONSE_SERVICES) or (
            service_domain == "homeassistant" and svc == "toggle")
        if direct:
            result["action"] = "RESPONSE"
            result["response"] = result.get("voice") or result.get("message") or "操作已完成。"
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


@mcp.tool()
@_log_tool("list_domains")
async def list_domains() -> dict:
    """列出当前所有设备类型（domain）及其数量。

    便于了解家庭有哪些类别的设备，或为 list_devices 选择 domain 过滤条件。
    同时返回危险设备明细（含 garage/door 类 cover），便于确认哪些操作需要二次确认。
    适用场景：用户问「家里都有哪些类型的设备」「哪些设备操作要确认」。
    """
    try:
        ha = get_ha()
        states = await ha.get_states()
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
        return {"ok": True, "domains": counts, "controllable_domains": ctrl,
                "dangerous_domains": danger,
                "dangerous_entities": dangerous_entities[:50],
                "dangerous_entity_count": len(dangerous_entities),
                "message": msg}
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": _hint()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "internal_error", "message": "查询设备类型失败：" + str(e)}


def main() -> None:
    setup_logging()
    log.info("startup transport=stdio log_level=%s", LOG_LEVEL)
    try:
        ha = get_ha()
    except HAError as e:
        log.error("HA 配置无效，拒绝启动：%s（%s）", e.message, e.code)
        raise SystemExit(2)
    except Exception as e:  # noqa: BLE001
        log.error("HA 客户端初始化失败，拒绝启动：%s", e)
        raise SystemExit(2)
    log.info(
        "HA config url=%s token=set timeout=%ss verify_ssl=%s ws_enabled=%s ws_url=%s",
        _safe_url(ha.base_url),
        ha.timeout,
        ha.verify_ssl,
        ha.ws_enabled,
        _safe_url(ha.ws_url),
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

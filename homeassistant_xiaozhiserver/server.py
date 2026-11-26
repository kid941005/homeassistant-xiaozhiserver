# -*- coding: utf-8 -*-
"""homeassistant-xiaozhiserver — 小智 MCP 服务（Home Assistant 桥）。

MCP 工具薄包装层：业务逻辑在 devices.DeviceService，进程入口在 main()。
"""
from __future__ import annotations

import functools
import logging
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from mcp.server.mcpserver import MCPServer

from .config import LOG_LEVEL
from .client import HAError, HAClient
from .devices import DeviceService
from .logging_setup import set_call_id, reset_call_id, setup_logging
from .metrics import metrics

log = logging.getLogger("homeassistant_xiaozhiserver.server")

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
        "一次控制多个设备用 control_devices（同一 action，只做一次危险确认）；"
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
_service: Optional[DeviceService] = None


def get_ha() -> HAClient:
    global _ha
    if _ha is None:
        _ha = HAClient()
    return _ha


def get_device_service() -> DeviceService:
    global _service
    if _service is None:
        _service = DeviceService(get_ha)
    return _service


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
            except Exception as exc:
                metrics.record_tool(
                    name, False, time.perf_counter() - started, type(exc).__name__
                )
                log.exception("tool=%s exception", name)
                raise
            else:
                elapsed = time.perf_counter() - started
                elapsed_ms = int(elapsed * 1000)
                result_code = result.get("code", "") if isinstance(result, dict) else ""
                metrics.record_tool(
                    name,
                    not (isinstance(result, dict) and result.get("ok") is False),
                    elapsed,
                    str(result_code or "ok"),
                )
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
    每个实体带 controllable（是否可控制）、actions（内置动作）、
    device_id/device_name/area_id/area_name。

    重要：当用户明确点名某个设备要开/关时（如「打开客厅灯」「关一下风扇」
    「关掉餐厅氛围灯」），直接调用 control_device(entity="客厅灯",
    action="turn_on")；带房间名和类型时也直接传完整名称，不要先调用
    list_devices 再控制，更不要根据未过滤列表的前几页推断设备不存在或状态。

    适用场景：用户问「有哪些设备」「列出所有的灯」「客厅有什么设备」「哪些设备能控制」。
    """
    return await get_device_service().list_devices(**locals())

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
    return await get_device_service().get_device_state(**locals())

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
        set_brightness     value=亮度百分比 0-100（仅 light，0 表示关闭）
        set_percentage     value=百分比 0-100（fan）
        set_position       value=位置百分比 0-100（cover）
        set_volume         value=音量百分比 0-100（media_player）
        volume_mute/volume_unmute  静音/取消静音
        set_temperature    value=目标温度（摄氏度，climate）
        set_hvac_mode      value=模式名（climate，如 heat/cool/auto）
        set_fan_mode       value=风速名（climate）
        set_color_temp     value=色温（开尔文，light）
        set_color          value=颜色 #RRGGBB、"r,g,b" 或 [r,g,b]（light）
      value   动作参数，按 action 需要提供：数字（亮度/音量百分比、温度、色温）、
              颜色（#RRGGBB 或 "r,g,b"，也可传 [r,g,b] 数组）。
      confirm_token 危险设备二次确认令牌。门锁/安防面板/车库门等危险设备首次调用会返回
                   code=confirmation_required 与 confirm_token（不执行）；用户确认后带上该
                   token 再调用一次才真正执行。令牌一次性且绑定本次操作。

    控制后会在超时窗口内轮询回读，校验状态 + 亮度/温度/音量/颜色等属性是否达到预期，
    返回 verified 与 verification（attempts/checks/mismatches/elapsed_ms）。
    若 HA 接受调用但未达预期会明确返回 mismatch 或 no_change；scene/script 等无状态
    服务无法回读时返回 accepted，避免误判失败。
    适用场景：「把客厅灯亮度调到 50」「关掉风扇」「空调设到 26 度」「电视音量 30」。
    设备名称能直接匹配时不需要先调 list_devices；只有名称不明确或设备不存在时才先查询。
    """
    return await get_device_service().control_device(**locals())

@mcp.tool()
@_log_tool("control_devices")
async def control_devices(entities: list[str], action: str, value: Optional[Any] = None,
                          confirm_token: Optional[str] = None) -> dict:
    """批量控制多个设备（同一动作、一次危险确认，减少 N 次 MCP 往返）。

    entities 传多个 entity_id 或中文名称；action 与 control_device 相同。
    对每个设备分别回读确认，返回 results 列表与合并后的中文 response。
    任一目标属于危险设备或本次服务高风险时，整批只申请一次 confirm_token；
    用户确认后带同一 token 重试整批即可。单次最多 20 个设备，重复目标自动去重；
    整批受 CONTROL_BATCH_TIMEOUT 总时限约束：超时且尚未下发的条目返回 status=not_sent/code=batch_timeout；已下发但未在时限内确认的条目返回 status=accepted_pending/code=pending_confirmation。
    """
    return await get_device_service().control_devices(**locals())

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
    return await get_device_service().list_services(**locals())

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
      status=accepted         scene/script/stop_cover 等无状态服务已下发，无需回读确认
      status=no_change        HA 已接受但无变化（verified=true 表示本来就是目标状态）
      status=mismatch         回读有变化，但状态/属性与预期不符
      status=accepted_pending HA 返回 null（新版 HA），回读窗口内仍未完全匹配预期
    适用场景：「把空调设成制热模式」「灯切成暖光」「设置风扇风速」「执行自定义脚本」。
    """
    return await get_device_service().call_service(**locals())

@mcp.tool()
@_log_tool("list_domains")
async def list_domains() -> dict:
    """列出当前所有设备类型（domain）及其数量。

    便于了解家庭有哪些类别的设备，或为 list_devices 选择 domain 过滤条件。
    同时返回危险设备明细（含 garage/door 类 cover），便于确认哪些操作需要二次确认。
    适用场景：用户问「家里都有哪些类型的设备」「哪些设备操作要确认」。
    """
    return await get_device_service().list_domains(**locals())


def main() -> None:
    setup_logging()
    log.info("startup transport=stdio log_level=%s", LOG_LEVEL)
    try:
        ha = get_ha()
    except HAError as e:
        log.error("HA 配置无效，拒绝启动：%s（%s）", e.message, e.code)
        raise SystemExit(2) from e
    except Exception as e:  # noqa: BLE001
        log.error("HA 客户端初始化失败，拒绝启动：%s", e)
        raise SystemExit(2) from e
    log.info(
        "startup HA base_url=%s ws_url=%s verify_ssl=%s",
        _safe_url(ha.base_url), _safe_url(ha.ws_url), ha.verify_ssl,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""回读窗口自测：慢设备 1s 后才回写状态，确认不会在 ~0.6s 被误判为无变化。"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant_xiaozhiserver import devices as devices_module  # noqa: E402
from homeassistant_xiaozhiserver.danger import _danger_flags  # noqa: E402
from homeassistant_xiaozhiserver.devices import DeviceService  # noqa: E402
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


class DangerSwapHA:
    """初始缓存缺少 device_class（看似安全），force 刷新后发现是车库门。"""

    def __init__(self, first_benign: bool = False) -> None:
        self._lock = asyncio.Lock()
        self.reads = 0
        self.called = False
        self.calls = 0
        self.first_benign = first_benign

    def entity_lock(self, entity_id: str) -> asyncio.Lock:
        return self._lock

    async def get_state(self, entity_id: str, force: bool = False):
        self.reads += 1
        attrs = {} if (self.first_benign and self.reads == 1) else {"device_class": "garage"}
        return {"entity_id": entity_id, "state": "closed", "attributes": attrs}

    async def call_service(self, domain: str, service: str, payload: dict):
        self.called = True
        self.calls += 1
        return None

    async def get_states(self):
        return [{"entity_id": "cover.x", "state": "closed", "attributes": {}}]

    async def get_registries(self):
        return {"devices": [], "entities": [], "areas": []}

    def state_stream_ready(self) -> bool:
        return False


class ReplayHA(DangerSwapHA):
    """令牌消费回归：首次危险确认，令牌下发一次后必须失效。"""

    def __init__(self) -> None:
        super().__init__()
        self.opened = False

    async def get_state(self, entity_id: str, force: bool = False):
        self.reads += 1
        state = "open" if self.opened else "closed"
        return {"entity_id": entity_id, "state": state,
                "attributes": {"device_class": "garage"}}

    async def call_service(self, domain: str, service: str, payload: dict):
        self.called = True
        self.opened = True
        return None


class TimeoutDangerHA(DangerSwapHA):
    """首次确认后 fresh 变安全但 POST 超时：令牌仍必须消费。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def get_state(self, entity_id: str, force: bool = False):
        self.reads += 1
        attrs = {"device_class": "garage"} if self.reads in (1, 3) else {}
        return {"entity_id": entity_id, "state": "closed", "attributes": attrs}

    async def call_service(self, domain: str, service: str, payload: dict):
        self.called = True
        self.calls += 1
        raise HAError("post_timeout", "模拟 POST 超时")


class CancelDangerHA(DangerSwapHA):
    """锁内 fresh 变安全后调用被取消：令牌仍必须消费。"""

    def __init__(self) -> None:
        super().__init__()
        self.called = False

    async def get_state(self, entity_id: str, force: bool = False):
        self.reads += 1
        attrs = {"device_class": "garage"} if self.reads in (1, 3) else {}
        return {"entity_id": entity_id, "state": "closed", "attributes": attrs}

    async def call_service(self, domain: str, service: str, payload: dict):
        self.called = True
        await asyncio.Event().wait()


class InvalidResponseHA(DangerSwapHA):
    """真实 POST 后返回 invalid_response：必须按已下发待确认处理并消费令牌。"""

    async def call_service(self, domain: str, service: str, payload: dict):
        self.called = True
        raise HAError("invalid_response", "模拟 POST 后响应非法")


class RuntimeRefreshHA(DangerSwapHA):
    """阶段一刷新抛非 HAError 异常：应返回 state_unavailable 而非 internal_error。"""

    async def get_state(self, entity_id: str, force: bool = False):
        raise RuntimeError("模拟刷新崩溃")


class CancelGarageHA(DangerSwapHA):
    """锁内 fresh 始终是车库门，POST 开始后挂起，用于验证取消后的令牌消费。"""

    async def call_service(self, domain: str, service: str, payload: dict):
        self.called = True
        await asyncio.Event().wait()


class PartialDangerHA:
    """批量阶段二 late-danger：一个目标已下发，另一个才发现是车库门。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.called = 0
        self.opened: set[str] = set()
        self.cover_reads = 0

    def entity_lock(self, entity_id: str) -> asyncio.Lock:
        return self._lock

    async def get_state(self, entity_id: str, force: bool = False):
        attrs: dict = {}
        if entity_id == "cover.b":
            self.cover_reads += 1
            if self.cover_reads > 1:
                attrs = {"device_class": "garage"}
        return {"entity_id": entity_id,
                "state": "open" if entity_id in self.opened else "closed",
                "attributes": attrs}

    async def call_service(self, domain: str, service: str, payload: dict):
        self.called += 1
        eid = str(payload.get("entity_id") or "")
        self.opened.add(eid)
        return None

    async def get_states(self):
        return [{"entity_id": "cover.a", "state": "closed", "attributes": {}},
                {"entity_id": "cover.b", "state": "closed", "attributes": {}}]

    async def get_registries(self):
        return {"devices": [], "entities": [], "areas": []}

    def state_stream_ready(self) -> bool:
        return False


async def _fake_resolve(ha, entity, verb, *args):
    return {"entity_id": "cover.x", "state": "closed", "attributes": {}}, None


async def _fake_resolve_any(ha, entity, verb, *args):
    eid = str(entity)
    state = "open" if eid in getattr(ha, "opened", set()) else "closed"
    return {"entity_id": eid, "state": state, "attributes": {}}, None


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
    for cn_name in ("主卧窗", "厨房门", "阳台门", "入户门", "庭院门"):
        check(_danger_flags("cover.x", {"friendly_name": cn_name})[0],
              "中文门窗名称按高风险处理：" + cn_name)
    for cn_safe in ("窗帘", "窗纱", "门铃", "门磁", "门帘"):
        check(not _danger_flags("cover.x", {"friendly_name": cn_safe})[0],
              "非门窗本体不误判：" + cn_safe)
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
    swap = DangerSwapHA()
    service = DeviceService(lambda: swap)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve):
        blocked = await service.control_device("cover.x", "open", confirm_token="bogus")
    check(blocked.get("code") == "invalid_confirm_token",
          "缓存未识别危险时任意非空令牌不会跳过锁内 fresh 复查")
    check(swap.called is False, "锁内 fresh 发现车库门后拒绝下发")
    swap2 = DangerSwapHA(first_benign=True)
    service2 = DeviceService(lambda: swap2)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve):
        batch_blocked = await service2.control_devices(["cover.x"], "open", confirm_token="bogus")
    check(batch_blocked.get("code") == "invalid_confirm_token",
          "批量阶段二 late-danger 仍复核确认令牌")
    check(swap2.called is False, "批量 late-danger 时不调用 call_service")
    call_swap = DangerSwapHA()
    call_service = DeviceService(lambda: call_swap)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve):
        call_blocked = await call_service.call_service(
            "cover.x", "cover.open_cover", confirm_token="bogus")
    check(call_blocked.get("code") == "invalid_confirm_token",
          "通用 call_service 非空令牌不能跳过锁内 fresh 危险复查")
    check(call_swap.called is False, "call_service 锁内 fresh 发现车库门后拒绝下发")
    timeout_ha = TimeoutDangerHA()
    timeout_service = DeviceService(lambda: timeout_ha)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve):
        first_timeout = await timeout_service.control_device("cover.x", "open")
        timeout_token = str(first_timeout.get("confirm_token") or "")
        check(bool(timeout_token), "超时场景首次仍返回确认令牌")
        timed_out = await timeout_service.control_device(
            "cover.x", "open", confirm_token=timeout_token)
        check(timed_out.get("code") == "pending_confirmation",
              "fresh 变安全后 POST 超时返回 pending_confirmation")
        reused_timeout = await timeout_service.control_device(
            "cover.x", "open", confirm_token=timeout_token)
    check(reused_timeout.get("code") == "invalid_confirm_token",
          "POST 超时后令牌也必须失效，不能重放")
    call_timeout_ha = TimeoutDangerHA()
    call_timeout_service = DeviceService(lambda: call_timeout_ha)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve):
        first_call_timeout = await call_timeout_service.call_service(
            "cover.x", "cover.open_cover")
        call_timeout_token = str(first_call_timeout.get("confirm_token") or "")
        check(bool(call_timeout_token), "call_service 超时场景首次仍返回确认令牌")
        call_timed_out = await call_timeout_service.call_service(
            "cover.x", "cover.open_cover", confirm_token=call_timeout_token)
        check(call_timed_out.get("code") == "pending_confirmation",
              "call_service fresh 变安全后 POST 超时返回 pending_confirmation")
        reused_call_timeout = await call_timeout_service.call_service(
            "cover.x", "cover.open_cover", confirm_token=call_timeout_token)
    check(reused_call_timeout.get("code") == "invalid_confirm_token",
          "call_service POST 超时后令牌同样必须失效")
    invalid_ha = InvalidResponseHA()
    invalid_service = DeviceService(lambda: invalid_ha)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve_any):
        first_invalid = await invalid_service.control_devices(["cover.x"], "open")
        invalid_token = str(first_invalid.get("confirm_token") or "")
        check(bool(invalid_token), "批量非超时异常场景首次返回确认令牌")
        invalid_out = await invalid_service.control_devices(
            ["cover.x"], "open", confirm_token=invalid_token)
        check((invalid_out.get("results") or [{}])[0].get("status") == "accepted_pending",
              "已下发后返回 invalid_response 标记 accepted_pending")
        reused_invalid = await invalid_service.control_devices(
            ["cover.x"], "open", confirm_token=invalid_token)
    check(reused_invalid.get("code") == "invalid_confirm_token",
          "批量 POST 后 invalid_response 也必须消费令牌")
    check(invalid_ha.called is True, "invalid_response 测试经过真实 call_service 边界")
    cancel_ha = CancelDangerHA()
    cancel_service = DeviceService(lambda: cancel_ha)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve):
        first_cancel = await cancel_service.control_device("cover.x", "open")
        cancel_token = str(first_cancel.get("confirm_token") or "")
        cancel_task = asyncio.create_task(
            cancel_service.control_device("cover.x", "open", confirm_token=cancel_token))
        for _ in range(100):
            if cancel_ha.called:
                break
            await asyncio.sleep(0.01)
        cancel_task.cancel()
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass
        reused_cancel = await cancel_service.control_device(
            "cover.x", "open", confirm_token=cancel_token)
    check(reused_cancel.get("code") == "invalid_confirm_token",
          "control_device 取消后令牌必须失效")
    call_cancel_ha = CancelDangerHA()
    call_cancel_service = DeviceService(lambda: call_cancel_ha)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve):
        first_call_cancel = await call_cancel_service.call_service("cover.x", "cover.open_cover")
        call_cancel_token = str(first_call_cancel.get("confirm_token") or "")
        call_cancel_task = asyncio.create_task(
            call_cancel_service.call_service("cover.x", "cover.open_cover",
                                             confirm_token=call_cancel_token))
        for _ in range(100):
            if call_cancel_ha.called:
                break
            await asyncio.sleep(0.01)
        call_cancel_task.cancel()
        try:
            await call_cancel_task
        except asyncio.CancelledError:
            pass
        reused_call_cancel = await call_cancel_service.call_service(
            "cover.x", "cover.open_cover", confirm_token=call_cancel_token)
    check(reused_call_cancel.get("code") == "invalid_confirm_token",
          "call_service 取消后令牌必须失效")
    batch_cancel_ha = CancelGarageHA()
    batch_cancel_service = DeviceService(lambda: batch_cancel_ha)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve_any):
        first_batch_cancel = await batch_cancel_service.control_devices(["cover.x"], "open")
        batch_cancel_token = str(first_batch_cancel.get("confirm_token") or "")
        batch_cancel_task = asyncio.create_task(
            batch_cancel_service.control_devices(["cover.x"], "open",
                                                 confirm_token=batch_cancel_token))
        for _ in range(100):
            if batch_cancel_ha.called:
                break
            await asyncio.sleep(0.01)
        batch_cancel_task.cancel()
        try:
            await batch_cancel_task
        except asyncio.CancelledError:
            pass
        reused_batch_cancel = await batch_cancel_service.control_devices(
            ["cover.x"], "open", confirm_token=batch_cancel_token)
    check(reused_batch_cancel.get("code") == "invalid_confirm_token",
          "批量控制取消后令牌必须失效")
    check(batch_cancel_ha.called is True, "批量取消测试经过真实 call_service 边界")
    lock_wait_ha = ReplayHA()
    lock_wait_service = DeviceService(lambda: lock_wait_ha)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve):
        first_lock = await lock_wait_service.control_device("cover.x", "open")
        lock_token = str(first_lock.get("confirm_token") or "")
        check(bool(lock_token), "锁等待场景首次返回确认令牌")
        await lock_wait_ha._lock.acquire()
        lock_task = asyncio.create_task(
            lock_wait_service.control_device("cover.x", "open", confirm_token=lock_token))
        await asyncio.sleep(0.05)
        lock_task.cancel()
        try:
            await lock_task
        except asyncio.CancelledError:
            pass
        lock_wait_ha._lock.release()
        retried_lock = await lock_wait_service.control_device(
            "cover.x", "open", confirm_token=lock_token)
    check(retried_lock.get("code") != "invalid_confirm_token",
          "锁等待取消未下发时令牌必须保留")
    check(lock_wait_ha.opened is True, "锁等待取消后同一令牌仍可执行一次")
    replay = ReplayHA()
    replay_service = DeviceService(lambda: replay)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve):
        first = await replay_service.control_device("cover.x", "open")
        replay_token = str(first.get("confirm_token") or "")
        check(bool(replay_token), "危险操作首次返回确认令牌")
        executed = await replay_service.control_device(
            "cover.x", "open", confirm_token=replay_token)
        check(replay.called is True and executed.get("code") != "invalid_confirm_token",
              "有效令牌可执行一次危险操作")
        reused = await replay_service.control_device(
            "cover.x", "open", confirm_token=replay_token)
    check(reused.get("code") == "invalid_confirm_token", "同一令牌不能重放执行第二次")
    partial = PartialDangerHA()
    partial_service = DeviceService(lambda: partial)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve_any):
        partial_out = await partial_service.control_devices(["cover.a", "cover.b"], "open")
    check(partial_out.get("code") == "confirmation_required",
          "批量 late-danger 返回确认请求")
    check(partial.called == 1, "批量 late-danger 前已执行的目标不被丢弃")
    check("cover.a" in (partial_out.get("executed_entities") or []),
          "部分结果列出已下发目标")
    check(not partial_out.get("confirm_token"),
          "late-danger 有已执行目标时不得返回可授权整批的令牌")
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve_any):
        retried_partial = await partial_service.control_devices(["cover.a", "cover.b"], "open")
    check(retried_partial.get("code") == "confirmation_required",
          "重试整批仍先要求确认而非直接执行")
    check(partial.called == 1, "重试整批不会重复执行已执行目标")
    pending_ha = PartialDangerHA()

    async def _fake_pending_apply(ha, st, *args, **kwargs):
        if str(st.get("entity_id")) == "cover.a":
            return {"ok": False, "entity_id": str(st.get("entity_id")),
                    "status": "accepted_pending",
                    "code": "pending_confirmation",
                    "dispatch_state": "may_be_dispatched",
                    "message": "模拟已下发待确认"}
        return {"_danger_guard": {"ok": False, "code": "confirmation_required",
                                  "message": "模拟 late danger"}}

    pending_service = DeviceService(lambda: pending_ha)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve_any), \
            patch.object(devices_module, "_apply_and_confirm", new=_fake_pending_apply):
        pending_out = await pending_service.control_devices(["cover.a", "cover.b"], "open")
    check("cover.a" in (pending_out.get("executed_entities") or []),
          "late-danger 部分结果包含 accepted_pending 已下发目标")
    fail = RefreshFailHA()
    guarded = await _apply_and_confirm(
        fail, {"entity_id": "cover.garage", "state": "closed", "attributes": {}},
        "cover", "open_cover", {"entity_id": "cover.garage"}, "打开",
        precheck=lambda fresh: {"ok": False, "code": "danger"})
    check((guarded.get("_danger_guard") or {}).get("code") == "state_unavailable",
          "锁内刷新失败且需要危险复查时返回 state_unavailable")
    check(fail.called is False, "刷新失败时不调用 call_service")
    runtime_ha = RuntimeRefreshHA()
    runtime_service = DeviceService(lambda: runtime_ha)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve_any):
        runtime_out = await runtime_service.control_devices(["cover.x"], "open")
    check(runtime_out.get("code") == "state_unavailable",
          "阶段一非 HAError 刷新失败返回 state_unavailable")
    check(runtime_out.get("status") == "not_sent", "刷新崩溃时标记 not_sent")
    concurrent_ha = DangerSwapHA()
    concurrent_service = DeviceService(lambda: concurrent_ha)
    with patch.object(devices_module, "_resolve_entity_or_error", new=_fake_resolve_any):
        first_concurrent = await concurrent_service.control_devices(["cover.x"], "open")
        concurrent_token = str(first_concurrent.get("confirm_token") or "")
        check(bool(concurrent_token), "并发场景首次返回确认令牌")
        task_a = asyncio.create_task(
            concurrent_service.control_devices(["cover.x"], "open", confirm_token=concurrent_token))
        task_b = asyncio.create_task(
            concurrent_service.control_devices(["cover.x"], "open", confirm_token=concurrent_token))
        outcomes = await asyncio.gather(task_a, task_b, return_exceptions=True)
    check(concurrent_ha.calls == 1, "同一令牌并发批量只执行一次危险操作")
    rejected = [o for o in outcomes
                if isinstance(o, dict) and o.get("code") == "invalid_confirm_token"]
    check(len(rejected) == 1, "同一令牌并发请求中有一个被拒绝")
    print("CONFIRM WINDOW OK")


if __name__ == "__main__":
    asyncio.run(run())

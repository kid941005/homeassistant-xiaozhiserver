#!/usr/bin/env python3
"""状态与服务缓存的自测：不联网，用假 _request 统计 REST 调用次数。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant_xiaozhiserver.client import HAClient  # noqa: E402


def make_client() -> HAClient:
    ha = HAClient("http://127.0.0.1:8123", "token")
    ha.ws_enabled = False
    ha.state_ws_enabled = False
    ha.state_cache_ttl = 30.0
    ha.services_ttl = 30.0
    return ha


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print("[ok] " + label)


async def run() -> None:
    ha = make_client()
    calls = {"states": 0, "services": 0, "service": 0}
    states = [
        {"entity_id": "light.a", "state": "off", "attributes": {}},
        {"entity_id": "switch.b", "state": "on", "attributes": {}},
    ]

    async def fake_request(method: str, path: str, **kwargs):
        if path == "/api/states":
            calls["states"] += 1
            return [dict(s) for s in states]
        if path == "/api/services":
            calls["services"] += 1
            return [{"domain": "light", "services": {"turn_on": {}}}]
        if method == "POST":
            calls["service"] += 1
            return None
        raise AssertionError("unexpected path " + path)

    ha._request = fake_request  # type: ignore[method-assign]

    first = await ha.get_states()
    second = await ha.get_states()
    check(len(first) == 2 and len(second) == 2, "get_states 返回全部实体")
    check(calls["states"] == 1, "get_states 命中短 TTL 缓存，只请求一次 REST")
    await ha.get_states(force=True)
    check(calls["states"] == 2, "get_states(force=True) 绕过缓存")

    await ha.get_services()
    await ha.get_services()
    check(calls["services"] == 1, "get_services 命中缓存，只请求一次 REST")

    await ha.call_service("light", "turn_on", {"entity_id": "light.a"})
    check(calls["service"] == 1, "call_service 已下发")
    await ha.get_states()
    check(calls["states"] == 3, "服务调用后状态缓存失效，重新拉取")

    lock_a = ha.entity_lock("light.a")
    check(lock_a is ha.entity_lock("light.a"), "同一实体复用同一把控制锁")
    check(lock_a is not ha.entity_lock("light.b"), "不同实体使用不同控制锁")
    order = []

    async def locked_worker(name: str) -> None:
        async with ha.entity_lock("switch.b"):
            order.append(name + "-start")
            await asyncio.sleep(0.01)
            order.append(name + "-end")

    await asyncio.gather(locked_worker("one"), locked_worker("two"))
    check(
        order[0].endswith("start") and order[1].endswith("end")
        and order[2].endswith("start") and order[3].endswith("end"),
        "同一实体并发控制被锁串行化",
    )

    ha._state_ws_ready.set()
    ha._store_states([{"entity_id": "light.a", "state": "on", "attributes": {}}])
    before = calls["states"]
    cached = await ha.get_states()
    check(len(cached) == 1 and calls["states"] == before, "事件流就绪时 get_states 走内存缓存")

    await ha.aclose()
    print("STATE CACHE OK")


if __name__ == "__main__":
    asyncio.run(run())

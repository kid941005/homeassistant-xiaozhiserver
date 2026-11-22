#!/usr/bin/env python3
"""回读窗口自测：慢设备 1s 后才回写状态，确认不会在 ~0.6s 被误判为无变化。"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant_xiaozhiserver.service import _apply_and_confirm  # noqa: E402


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


async def run() -> None:
    ha = SlowHA()
    st = {"entity_id": "light.slow", "state": "off", "attributes": {}}
    result = await _apply_and_confirm(
        ha, st, "light", "turn_on", {"entity_id": "light.slow"}, "打开")
    check(result.get("status") == "confirmed", "慢设备 1s 回写仍被确认")
    check((result.get("after") or {}).get("state") == "on", "慢设备回读状态为 on")
    check(ha.reads >= 4, "回读次数超过旧的 4 次上限（窗口确实延长）")
    print("CONFIRM WINDOW OK")


if __name__ == "__main__":
    asyncio.run(run())

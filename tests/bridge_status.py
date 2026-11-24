#!/usr/bin/env python3
"""bridge 状态文件的自测：原子写入、读取补充字段、stale 判定与损坏回退。"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant_xiaozhiserver.bridge_status import read_status, write_status  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit("FAIL: " + message)
    print("[ok] " + message)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "status.json"
        now = time.time()
        write_status(
            {"connected": True, "connected_at": now - 5, "attempt": 0, "server_pid": 123},
            str(path),
        )
        check(path.is_file(), "状态文件已写入")
        check(not Path(str(path) + ".tmp").exists(), "临时文件已被原子替换")

        data = read_status(str(path), now=now)
        check(bool(data) and data.get("connected") is True, "读取 connected 字段")
        check(bool(data) and data.get("stale") is False, "新心跳不标记 stale")
        check(bool(data) and data.get("age_seconds") is not None, "补充 age_seconds")

        write_status({"connected": True, "updated_at": now - 100}, str(path))
        stale = read_status(str(path), now=now)
        check(bool(stale) and stale.get("stale") is True, "超时心跳标记 stale")

        path.write_text("{bad json", encoding="utf-8")
        check(read_status(str(path)) is None, "损坏文件返回 None")
    print("BRIDGE STATUS OK")


if __name__ == "__main__":
    main()

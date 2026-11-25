# -*- coding: utf-8 -*-
"""验证 LOG_ROLE=mcp 子进程写入的工具指标能被 WebUI 进程的 render() 合并。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ)
    env["XIAOZHI_METRICS_DIR"] = tmp
    env["LOG_ROLE"] = "mcp"
    env["PYTHONPATH"] = str(ROOT)
    child = (
        "from homeassistant_xiaozhiserver.metrics import Metrics\n"
        "m = Metrics(persist=True)\n"
        "m.record_tool('demo', True, 0.25)\n"
        "m.flush()\n"
    )
    subprocess.run([sys.executable, "-c", child], cwd=str(ROOT), env=env, check=True)
    os.environ["XIAOZHI_METRICS_DIR"] = tmp
    from homeassistant_xiaozhiserver.metrics import Metrics

    text = Metrics(persist=False).render()
    assert 'ha_mcp_tool_calls_total{code="ok",ok="true",tool="demo"} 1' in text, text
    assert 'ha_mcp_tool_duration_seconds_count{tool="demo"} 1' in text, text

print("METRICS SHARED OK")

# -*- coding: utf-8 -*-
"""日志目录不可写时回退到临时目录，且不输出 Python logging Traceback。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            LOG_FILE=str(blocker / "homeassistant-xiaozhiserver.log"),
            LOG_ROLE="bridge",
            PYTHONPATH=str(ROOT),
            PYTHONDONTWRITEBYTECODE="1",
        )
        code = """
from homeassistant_xiaozhiserver.logging_setup import log_file_path, setup_logging
import logging
setup_logging("bridge")
logging.getLogger("homeassistant_xiaozhiserver").info("fallback test")
print("PATH=" + log_file_path("bridge"))
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            raise SystemExit("logging fallback subprocess failed:\n" + output)
        if "Traceback (most recent call last)" in output:
            raise SystemExit("logging fallback leaked a traceback:\n" + output)
        if "日志路径不可写" not in output or "回退到" not in output:
            raise SystemExit("missing fallback warning:\n" + output)
        paths = [line[len("PATH="):] for line in proc.stdout.splitlines() if line.startswith("PATH=")]
        if not paths or "xiaozhi-logs" not in paths[0]:
            raise SystemExit("unexpected effective log path:\n" + output)
    print("LOGGING FALLBACK OK")


if __name__ == "__main__":
    main()

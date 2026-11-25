# -*- coding: utf-8 -*-
"""环境变量一致性检查：.env.example / .env / 代码读取键三方一致。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from homeassistant_xiaozhiserver.settings import APP_ENV_KEYS  # noqa: E402

# 仅由 compose / docker-entrypoint.sh 设置，不由应用配置模板管理。
COMPOSE_ONLY = {"PUID", "PGID", "TZ"}
INTERNAL_KEYS = {"LOG_ROLE", "XIAOZHI_METRICS_DIR"}
known = set(APP_ENV_KEYS) | COMPOSE_ONLY | INTERNAL_KEYS


def parse_env(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        # 模板中的可选键是注释形式（# KEY=value），也应计入覆盖检查。
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=", line)
        if match:
            keys.add(match.group(1))
    return keys


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print("[ok] " + name)
    else:
        print("[FAIL] " + name + ("" if not extra else " -> " + extra))
        sys.exit(1)


example = parse_env(ROOT / ".env.example")
required = {"MCP_ENDPOINT", "HA_URL", "HA_TOKEN", "WEBUI_TOKEN"}
check("示例模板包含全部必填键", required <= example, str(sorted(required - example)))
documented = set(APP_ENV_KEYS) | COMPOSE_ONLY
missing_example = documented - example
extra_example = example - documented
check(".env.example 覆盖全部可配置键", not missing_example, str(sorted(missing_example)))
check(".env.example 没有未知键", not extra_example, str(sorted(extra_example)))

actual = parse_env(ROOT / ".env")
if actual:
    check(".env 没有未知键", not (actual - known), str(sorted(actual - known)))

# 扫描代码中读取的键，防止出现文档/模板漏项；同时允许 client.py 直接 os.environ.get。
pattern = re.compile(r'(?:env_(?:str|bool|int|float|set)|os\.environ\.get)\(\s*"([A-Z0-9_]+)"')
code_keys: set[str] = set()
for path in (ROOT / "homeassistant_xiaozhiserver").glob("*.py"):
    code_keys.update(pattern.findall(path.read_text(encoding="utf-8")))
code_keys |= {"HA_URL", "HA_TOKEN", "MCP_ENDPOINT"}
unknown_code = code_keys - known
check("代码读取的环境变量全部登记", not unknown_code, str(sorted(unknown_code)))
uncovered = known - code_keys - COMPOSE_ONLY - INTERNAL_KEYS
check("登记的键都有代码读取", not uncovered, str(sorted(uncovered)))

print("ENV CONSISTENCY OK")

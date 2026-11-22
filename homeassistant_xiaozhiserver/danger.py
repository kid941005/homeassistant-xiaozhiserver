# -*- coding: utf-8 -*-
"""危险设备判定与一次性确认令牌。"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Optional

from .config import (
    DANGER_CONFIRM_ENABLED,
    DANGER_CONFIRM_TTL,
    DANGEROUS_COVER_CLASSES,
    DANGEROUS_DOMAINS,
    DANGEROUS_SERVICE_DOMAINS,
)
from .service import _domain_of, _friendly_name

log = logging.getLogger("homeassistant_xiaozhiserver.danger")

# homeassistant 域只有系统级动作需要确认；toggle 等日常动作不在此列。
_DANGEROUS_HOMEASSISTANT_SERVICES = {"restart", "stop", "reload_all", "reload_core_config"}


def _danger_flags(entity_id: str, attributes: Optional[dict]) -> tuple:
    """判断实体是否属于高风险设备（操作需二次确认）。"""
    domain = _domain_of(entity_id)
    if domain in DANGEROUS_DOMAINS:
        return True, "该设备类型（" + domain + "）属于高风险设备"
    if domain == "cover":
        attrs = attributes if isinstance(attributes, dict) else {}
        dc = str(attrs.get("device_class") or "").strip().lower()
        if dc in DANGEROUS_COVER_CLASSES:
            return True, "该 cover 设备类型（" + dc + "）属于高风险（门窗/车库等）"
    return False, None


def _service_is_dangerous(service_domain: str, service: str) -> tuple:
    """服务本身是否高风险：脚本/系统命令/系统级动作即使目标实体普通也要确认。"""
    dom = str(service_domain or "").strip().lower()
    svc = str(service or "").strip().lower()
    if dom in DANGEROUS_SERVICE_DOMAINS:
        return True, "服务类型（" + dom + "." + svc + "）属于高风险"
    if dom == "homeassistant" and svc in _DANGEROUS_HOMEASSISTANT_SERVICES:
        return True, "系统级服务（homeassistant." + svc + "）属于高风险"
    return False, None


def service_is_dangerous(service_domain: str, service: str) -> tuple:
    """公开封装：供 WebUI 等调用方判断服务本身是否高风险。"""
    return _service_is_dangerous(service_domain, service)


def _danger_info(entity_id: str, attributes: Optional[dict]) -> dict:
    dangerous, reason = _danger_flags(entity_id, attributes)
    return {"dangerous": dangerous, "danger_reason": reason}


_CONFIRM_TOKENS: dict = {}
# 单用户场景下正常不会堆积；加硬上限防止异常循环不断申请令牌导致内存增长。
_MAX_CONFIRM_TOKENS = 512


def _prune_confirm_tokens() -> None:
    now = time.monotonic()
    for token in [k for k, v in _CONFIRM_TOKENS.items() if float(v.get("expires_at", 0)) <= now]:
        _CONFIRM_TOKENS.pop(token, None)
    overflow = len(_CONFIRM_TOKENS) - _MAX_CONFIRM_TOKENS
    if overflow > 0:
        oldest = sorted(_CONFIRM_TOKENS, key=lambda k: float(_CONFIRM_TOKENS[k].get("expires_at", 0)))
        for token in oldest[:overflow]:
            _CONFIRM_TOKENS.pop(token, None)


def _confirm_intent(service_domain: str, service: str, payload: dict) -> str:
    try:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        body = str(payload)
    return str(service_domain) + "." + str(service) + "|" + body


def _issue_confirm_token(entity_id: str, service_domain: str, service: str, payload: dict) -> str:
    _prune_confirm_tokens()
    token = secrets.token_urlsafe(12)
    _CONFIRM_TOKENS[token] = {
        "intent": _confirm_intent(service_domain, service, payload),
        "entity_id": entity_id,
        "service": str(service_domain) + "." + str(service),
        "expires_at": time.monotonic() + max(1.0, DANGER_CONFIRM_TTL),
    }
    return token


def _danger_block(st: dict, service_domain: str, service: str,
                  payload: dict, confirm_token: Optional[str]) -> Optional[dict]:
    """危险设备二次确认闸门：返回拦截结果；非危险或确认通过返回 None。"""
    if not DANGER_CONFIRM_ENABLED:
        return None
    _prune_confirm_tokens()
    eid = str(st.get("entity_id"))
    entity_dangerous, reason = _danger_flags(eid, st.get("attributes") or {})
    dangerous = entity_dangerous
    if not dangerous:
        dangerous, reason = _service_is_dangerous(service_domain, service)
    if not dangerous:
        return None
    name = _friendly_name(st)
    svc_str = str(service_domain) + "." + str(service)
    expected_intent = _confirm_intent(service_domain, service, payload)
    ttl = max(1, int(round(DANGER_CONFIRM_TTL)))
    base = {"ok": False, "dangerous": True, "requires_confirmation": True,
            "entity_id": eid, "name": name, "service": svc_str,
            "risk_reason": reason}
    token = (confirm_token or "").strip()
    if not token:
        new_token = _issue_confirm_token(eid, service_domain, service, payload)
        log.warning("danger confirmation required entity=%s service=%s reason=%s", eid, svc_str, reason)
        subject = "是危险设备" if entity_dangerous else "的本次操作是高风险操作"
        return {**base, "code": "confirmation_required",
                "confirm_token": new_token, "expires_in": ttl,
                "message": "『" + name + "』" + subject + "（" + str(reason) + "），为避免误操作未执行。"
                           "请先向用户明确确认动作，确认后再带上 confirm_token 调用一次；"
                           "令牌 " + str(ttl) + " 秒内有效且仅可使用一次。"}
    rec = _CONFIRM_TOKENS.get(token)
    if rec is None:
        log.warning("danger confirmation rejected entity=%s service=%s code=invalid_confirm_token", eid, svc_str)
        return {**base, "code": "invalid_confirm_token",
                "message": "确认令牌无效或已使用。请重新发起原操作获取新的 confirm_token，并在用户确认后使用。"}
    if float(rec.get("expires_at", 0)) <= time.monotonic():
        _CONFIRM_TOKENS.pop(token, None)
        new_token = _issue_confirm_token(eid, service_domain, service, payload)
        log.warning("danger confirmation rejected entity=%s service=%s code=confirm_token_expired", eid, svc_str)
        return {**base, "code": "confirm_token_expired", "confirm_token": new_token,
                "expires_in": ttl,
                "message": "确认令牌已过期。已生成新的 confirm_token；请让用户确认后重新调用。"}
    if rec.get("intent") != expected_intent:
        log.warning("danger confirmation rejected entity=%s service=%s code=confirm_token_mismatch", eid, svc_str)
        return {**base, "code": "confirm_token_mismatch",
                "message": "确认令牌与本次操作不匹配（设备、服务或参数已变化）。请重新发起原操作获取新的 confirm_token。"}
    _CONFIRM_TOKENS.pop(token, None)  # 一次性：校验通过即消费
    log.info("danger confirmation passed entity=%s service=%s", eid, svc_str)
    return None

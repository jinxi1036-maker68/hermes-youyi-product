"""Deterministic identity and permission guard for all chat channels."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .models import UserIdentity


AUDIT_FILE = "permission_security_audit.jsonl"

_PERMISSION_QUERIES = (
    "我有什么权限",
    "我的权限是什么",
    "我能做什么",
    "当前权限",
)
_ROLE_CHANGE_PATTERNS = (
    r"(?:把|将|给)?我(?:的角色)?(?:改|设|设置|提升|升级|变更)(?:成|为)?(?:老板|店长|管理员)",
    r"(?:把|将|设置|修改).{0,20}(?:角色|权限)(?:为|成)?.{0,12}(?:老板|店长|管理员|老师)",
    r"(?:提升|升级).{0,12}(?:权限|角色)",
)
_CAMPUS_PATTERNS = (
    r"(?:帮我|给我|把我)?(?:关联|绑定|分配|更换).{0,12}(?:校区|门店)",
    r"(?:校区|门店).{0,8}(?:关联|绑定|分配)",
)
_STAFF_PATTERNS = (
    r"(?:配置|添加|绑定|新增|修改).{0,16}(?:人员|老师|店长|账号|角色|权限)",
    r"(?:把|将).{1,20}(?:绑定|设为|配置为).{0,12}(?:老师|店长|老板)",
)


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return any(re.search(pattern, compact) for pattern in patterns)


def append_permission_audit(store: Any, identity: UserIdentity, raw_text: str, action: str, result: str) -> None:
    entry = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "actor_user_id": identity.canonical_user_id,
        "actor_platform_id": identity.platform_user_id,
        "actor_name": identity.person_name,
        "actor_role": identity.role,
        "platform": identity.platform,
        "action": action,
        "result": result,
        "raw_text": str(raw_text or ""),
    }
    path = store.path_for(AUDIT_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with store._lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def permission_query_reply(identity: UserIdentity, text: str) -> str | None:
    compact = re.sub(r"\s+", "", str(text or ""))
    if not any(phrase in compact for phrase in _PERMISSION_QUERIES):
        return None
    if identity.role == "boss":
        return (
            f"{identity.person_name or '当前账号'}，你是老板。你可以查看全局经营信息，发起人员、角色和校区配置，"
            "以及确认关键业务操作。人员和角色变更必须经过二次确认并写入审计记录。"
        )
    if identity.role in {"manager", "store_manager", "summer_manager"}:
        return (
            f"{identity.person_name or '当前账号'}，你是店长。你可以查看职责范围内的学生、老师、任务和看板，"
            "并督促业务闭环；不能把自己提升为老板，不能修改全局角色或越权关联校区。需要调整请联系金总。"
        )
    return (
        f"{identity.person_name or '当前账号'}，你是老师。你可以记录负责学生、查看和处理本人任务、查看本人看板；"
        "不能配置人员、修改角色、关联校区、绑定其他账号或提升权限。需要调整请联系金总。"
    )


def restricted_change_reply(store: Any, identity: UserIdentity, text: str) -> str | None:
    """Reject non-owner identity/permission changes before model routing.

    Boss requests deliberately return ``None`` so existing proposal and
    second-confirmation handlers remain the only execution path.
    """
    action = ""
    if _matches(text, _ROLE_CHANGE_PATTERNS):
        action = "role_change"
    elif _matches(text, _CAMPUS_PATTERNS):
        action = "campus_assignment"
    elif _matches(text, _STAFF_PATTERNS):
        action = "staff_configuration"
    if not action:
        return None
    if identity.role == "boss":
        append_permission_audit(store, identity, text, action, "forwarded_to_confirmed_admin_flow")
        return None
    append_permission_audit(store, identity, text, action, "denied")
    role_name = "店长" if identity.role in {"manager", "store_manager", "summer_manager"} else "老师"
    return (
        f"你当前是{role_name}，不能修改自己或他人的角色、配置人员、关联校区或绑定其他账号。"
        "如需调整，请联系金总；必须由金总/老板账号发起并确认，系统会进行二次确认并记录审计。"
    )


def guard_permission_request(store: Any, identity: UserIdentity, text: str) -> str | None:
    return permission_query_reply(identity, text) or restricted_change_reply(store, identity, text)

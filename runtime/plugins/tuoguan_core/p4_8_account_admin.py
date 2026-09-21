"""Owner-only administration for the isolated P4-8 reviewer test accounts."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .conversation_state import (
    clear_conversation_state,
    is_state_expired,
    load_conversation_state,
    remember_conversation_state,
)
from .employee_identity import owner_user_id
from .models import UserIdentity
from .store import TuoguanStore


CONFIG_FILE = "p4_8_test_account_config.json"
PENDING_USERID = "pending_p4_8_account_userid"
PENDING_CONFIRM = "pending_p4_8_account_config_confirm"

_CONFIRM_WORDS = {"确认配置", "确认保存", "确认", "是的", "没问题"}
_CANCEL_WORDS = {"取消", "取消配置", "暂不配置", "不用了"}
_USER_ID_RE = re.compile(
    r"(?:user\s*_?\s*id|用户\s*id)\s*(?:是|为|：|:|=)?\s*([A-Za-z0-9][A-Za-z0-9_.@-]{0,63})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class P48AccountAdminResult:
    handled: bool
    reply: str
    action: str


def default_p4_8_test_account_config() -> dict[str, Any]:
    return {
        "stage": "P4-8",
        "status": "prelaunch_account_verification",
        "mode": "test_account_configuration_only",
        "teacher": {"name": "", "user_id": "", "configured": False, "enabled": False},
        "manager": {"name": "", "user_id": "", "configured": False, "enabled": False},
        "active_test_role": None,
        "safety_switches": {
            "preview_only": True,
            "allow_real_push": False,
            "allow_real_state_write": False,
            "allow_print": False,
            "allow_parent_send": False,
        },
        "updated_by": "",
        "updated_at": "",
    }


def load_p4_8_test_account_config(store: TuoguanStore) -> dict[str, Any]:
    raw = store.read_json(CONFIG_FILE, {})
    config = default_p4_8_test_account_config()
    if not isinstance(raw, dict):
        return config
    for role in ("teacher", "manager"):
        value = raw.get(role)
        if isinstance(value, dict):
            config[role].update(
                {
                    "name": str(value.get("name") or ""),
                    "user_id": str(value.get("user_id") or ""),
                    "configured": bool(value.get("user_id")),
                    "enabled": bool(value.get("enabled")),
                }
            )
    active_role = str(raw.get("active_test_role") or "")
    config["active_test_role"] = active_role if active_role in {"teacher", "manager"} else None
    config["updated_by"] = str(raw.get("updated_by") or "")
    config["updated_at"] = str(raw.get("updated_at") or "")
    return _enforce_safety(config)


def handle_p4_8_account_admin_message(
    *,
    user_id: str,
    raw_text: str,
    store: TuoguanStore | None = None,
) -> P48AccountAdminResult | None:
    text = str(raw_text or "").strip()
    if not text:
        return None
    data_store = store or TuoguanStore()
    identity = _identity(user_id, data_store)
    pending = load_conversation_state(data_store, identity, include_expired=True)
    pending_type = str((pending or {}).get("state_type") or "")

    if pending_type in {PENDING_USERID, PENDING_CONFIRM} and _is_pending_reply(text, pending_type):
        if not _is_owner(data_store, user_id):
            return _denied()
        if pending and is_state_expired(pending):
            clear_conversation_state(data_store, identity)
            return P48AccountAdminResult(True, "这次账号配置确认已经过期，请重新发起配置。", "expired")
        return _handle_pending(data_store, identity, text, pending or {})

    action = _classify_action(text)
    if not action:
        return None
    if not _is_owner(data_store, user_id):
        return _denied()

    if action == "query":
        return P48AccountAdminResult(True, _format_config(load_p4_8_test_account_config(data_store)), "query")
    if action == "clear":
        return _propose(data_store, identity, "clear", None, "", "")

    roles = _roles_in_text(text)
    if len(roles) != 1:
        return P48AccountAdminResult(
            True,
            "一次只能配置一个测试角色。请分别设置普通老师账号或相关老师终审账号。",
            "reject_multiple_roles",
        )
    role = next(iter(roles))
    if action == "activate":
        config = load_p4_8_test_account_config(data_store)
        if not config[role].get("configured"):
            return P48AccountAdminResult(
                True,
                f"{_role_label(role)}测试账号尚未配置，请先提供姓名和明确 UserID。",
                "not_configured",
            )
        return _propose(data_store, identity, "activate", role, "", "")
    user_value = _extract_user_id(text)
    name = _extract_name(text, role)
    if not user_value:
        prompt = f"请补充{_role_label(role)}的企业微信 UserID。Hermes 不会根据姓名猜测 UserID。"
        remember_conversation_state(
            data_store,
            identity,
            state_type=PENDING_USERID,
            last_system_prompt=prompt,
            expected_replies=["UserID 是 xxx", "取消配置"],
            payload={"action": "set", "role": role, "name": name},
            source_handler="p4_8_account_admin",
            ttl_minutes=30,
        )
        return P48AccountAdminResult(True, prompt, "request_user_id")
    return _propose(data_store, identity, "set", role, name, user_value)


def _handle_pending(
    store: TuoguanStore,
    identity: UserIdentity,
    text: str,
    state: dict[str, Any],
) -> P48AccountAdminResult:
    compact = _compact(text)
    if compact in _CANCEL_WORDS:
        clear_conversation_state(store, identity)
        return P48AccountAdminResult(True, "已取消本次 P4-8 测试账号配置，没有保存任何变更。", "cancel")

    payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
    if state.get("state_type") == PENDING_USERID:
        user_value = _extract_user_id(text)
        if not user_value:
            return P48AccountAdminResult(
                True,
                "没有识别到明确的 UserID。请按“UserID 是 xxx”补充；Hermes 不会猜测。",
                "request_user_id",
            )
        return _propose(
            store,
            identity,
            "set",
            str(payload.get("role") or ""),
            str(payload.get("name") or ""),
            user_value,
        )

    if compact not in _CONFIRM_WORDS:
        return P48AccountAdminResult(
            True,
            "请回复“确认配置”保存，或回复“取消配置”。本次尚未写入测试配置。",
            "await_confirmation",
        )
    result = _apply_confirmed_change(store, identity, payload)
    clear_conversation_state(store, identity)
    return result


def _propose(
    store: TuoguanStore,
    identity: UserIdentity,
    action: str,
    role: str | None,
    name: str,
    user_id: str,
) -> P48AccountAdminResult:
    if action == "clear":
        prompt = "准备清空 P4-8 普通老师和相关老师测试账号配置。该操作不会修改生产老师表。回复“确认配置”执行，或回复“取消配置”。"
        payload = {"action": "clear"}
    elif action == "activate":
        label = _role_label(str(role))
        prompt = (
            f"准备将{label}设为本轮唯一待测账号。另一角色不会同时启用；本次只验证账号身份和审核权限。\n"
            "回复“确认配置”保存，或回复“取消配置”。"
        )
        payload = {"action": "activate", "role": role}
    else:
        label = _role_label(str(role))
        shown_name = name or "未填写姓名"
        prompt = (
            f"请确认 P4-8 测试账号配置：\n"
            f"角色：{label}\n姓名：{shown_name}\nUserID：{user_id}\n"
            "保存后只用于账号身份和审核权限验证，不写真实状态。项目交付流程不包含系统自动打印或自动发家长。\n"
            "回复“确认配置”保存，或回复“取消配置”。"
        )
        payload = {"action": "set", "role": role, "name": name, "user_id": user_id}
    remember_conversation_state(
        store,
        identity,
        state_type=PENDING_CONFIRM,
        last_system_prompt=prompt,
        expected_replies=["确认配置", "取消配置"],
        payload=payload,
        source_handler="p4_8_account_admin",
        ttl_minutes=30,
    )
    return P48AccountAdminResult(True, prompt, "pending_confirmation")


def _apply_confirmed_change(
    store: TuoguanStore,
    identity: UserIdentity,
    payload: dict[str, Any],
) -> P48AccountAdminResult:
    action = str(payload.get("action") or "")
    if action == "clear":
        config = default_p4_8_test_account_config()
        config["updated_by"] = identity.canonical_user_id
        config["updated_at"] = datetime.now().isoformat(timespec="seconds")
        store.write_json(CONFIG_FILE, config)
        return P48AccountAdminResult(True, "P4-8 测试账号配置已清空。生产老师表和业务数据没有修改。", "cleared")

    if action == "activate":
        role = str(payload.get("role") or "")
        config = load_p4_8_test_account_config(store)
        if role not in {"teacher", "manager"} or not config[role].get("configured"):
            return P48AccountAdminResult(True, "待测账号配置不完整，请重新发起。", "invalid_payload")
        for candidate in ("teacher", "manager"):
            config[candidate]["enabled"] = candidate == role
        config["active_test_role"] = role
        config["updated_by"] = identity.canonical_user_id
        config["updated_at"] = datetime.now().isoformat(timespec="seconds")
        store.write_json(CONFIG_FILE, _enforce_safety(config))
        return P48AccountAdminResult(
            True,
            f"已将{_role_label(role)}设为本轮唯一待测账号。本轮只验证账号身份和审核权限，不执行生产动作。",
            "activated",
        )

    role = str(payload.get("role") or "")
    user_id = str(payload.get("user_id") or "")
    if role not in {"teacher", "manager"} or not user_id:
        return P48AccountAdminResult(True, "配置内容不完整，请重新发起。", "invalid_payload")
    config = load_p4_8_test_account_config(store)
    config[role] = {
        "name": str(payload.get("name") or ""),
        "user_id": user_id,
        "configured": True,
        "enabled": False,
    }
    config["active_test_role"] = None
    config["updated_by"] = identity.canonical_user_id
    config["updated_at"] = datetime.now().isoformat(timespec="seconds")
    store.write_json(CONFIG_FILE, _enforce_safety(config))
    return P48AccountAdminResult(
        True,
        f"已保存{_role_label(role)}测试账号：{payload.get('name') or '未填写姓名'}（UserID：{user_id}）。\n"
        "当前仅完成账号身份和审核权限测试配置，账号未启用，也没有写入生产老师表。",
        "saved",
    )


def _enforce_safety(config: dict[str, Any]) -> dict[str, Any]:
    safe = deepcopy(config)
    safe["stage"] = "P4-8"
    safe["status"] = "prelaunch_account_verification"
    safe["mode"] = "test_account_configuration_only"
    safe["safety_switches"] = {
        "preview_only": True,
        "allow_real_push": False,
        "allow_real_state_write": False,
        "allow_print": False,
        "allow_parent_send": False,
    }
    enabled_roles = [role for role in ("teacher", "manager") if safe.get(role, {}).get("enabled")]
    if len(enabled_roles) != 1:
        safe["active_test_role"] = None
        for role in ("teacher", "manager"):
            safe[role]["enabled"] = False
    return safe


def _format_config(config: dict[str, Any]) -> str:
    lines = ["当前 P4-8 上线前账号验证配置："]
    for role in ("teacher", "manager"):
        item = config[role]
        if item.get("configured"):
            state = "本轮待测" if item.get("enabled") else "未启用"
            lines.append(f"- {_role_label(role)}：{item.get('name') or '未填写姓名'}，UserID：{item.get('user_id')}，{state}")
        else:
            lines.append(f"- {_role_label(role)}：未配置")
    lines.extend(
        [
            "测试范围：仅验证账号身份、角色和审核权限；不写真实业务状态。",
            "交付流程：审核完成后系统生成 Word，机构人员人工打开、打印并线下交给家长。",
            "项目不包含系统自动发家长、自动打印、自动群发或自动交付。",
            "一次只能测试一个账号，配置账号不等于启用测试。",
        ]
    )
    return "\n".join(lines)


def _classify_action(text: str) -> str:
    compact = _compact(text).lower()
    has_scope = "p4-8" in compact or "p4_8" in compact or "测试账号" in compact
    if not has_scope:
        return ""
    if any(word in compact for word in ("查看", "查询", "当前", "配置情况")):
        return "query"
    if any(word in compact for word in ("清空", "删除测试账号", "移除测试账号")):
        return "clear"
    if "启用" in compact and _roles_in_text(text):
        return "activate"
    if any(word in compact for word in ("添加", "设置", "修改", "配置")) and _roles_in_text(text):
        return "set"
    return ""


def _roles_in_text(text: str) -> set[str]:
    compact = _compact(text)
    roles: set[str] = set()
    if "普通老师" in compact:
        roles.add("teacher")
    if "相关老师" in compact or "终审老师" in compact or "终审账号" in compact:
        roles.add("manager")
    return roles


def _extract_user_id(text: str) -> str:
    match = _USER_ID_RE.search(text)
    return str(match.group(1) if match else "").strip()


def _extract_name(text: str, role: str) -> str:
    if role == "manager" and "相关老师" in text:
        return "相关老师"
    explicit = re.search(
        r"(?:测试账号|账号)\s*(?:为|：|:)\s*([\u4e00-\u9fff]{1,4}老师)",
        text,
    )
    if explicit and explicit.group(1) not in {"普通老师", "终审老师"}:
        return explicit.group(1)
    candidates = re.findall(r"([\u4e00-\u9fff]{1,6}老师)", text)
    for candidate in candidates:
        if candidate not in {"普通老师", "终审老师"} and not candidate.startswith(("账号为", "设置为")):
            return candidate
    return ""


def _is_pending_reply(text: str, state_type: str) -> bool:
    compact = _compact(text)
    if state_type == PENDING_USERID:
        return bool(_extract_user_id(text)) or compact in _CANCEL_WORDS or "userid" in compact.lower()
    return compact in _CONFIRM_WORDS or compact in _CANCEL_WORDS


def _is_owner(store: TuoguanStore, user_id: str) -> bool:
    """P4-8 remains owner-only, without embedding a person or test account."""

    return bool(user_id and user_id == owner_user_id(store))


def _identity(user_id: str, store: TuoguanStore) -> UserIdentity:
    is_owner = _is_owner(store, user_id)
    return UserIdentity(
        platform="wecom_callback",
        platform_user_id=user_id,
        canonical_user_id=user_id,
        person_name="机构负责人" if is_owner else "",
        role="boss" if is_owner else "unknown",
        approval_state="approved" if is_owner else "pending",
    )


def _denied() -> P48AccountAdminResult:
    return P48AccountAdminResult(True, "P4-8 测试账号只能由机构负责人在企业微信中配置。", "denied")


def _role_label(role: str) -> str:
    return "普通老师" if role == "teacher" else "相关老师终审"


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip("。；;！!")

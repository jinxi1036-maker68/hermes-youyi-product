"""Read-only staff directory resolution for model-led Xiaoyou turns."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
import re
import unicodedata

from .store import TuoguanStore
from .personnel_identity_authority import (
    ACCESS_KEY,
    AUTHORITY_KEY,
    DATA_FILE as PERSONNEL_AUTHORITY_DATA_FILE,
    IdentityAuthorityError,
    active_identity_snapshot,
    authority_is_enforced,
    validate_runtime_identity_document,
)
from .tenant_context import current_tenant_id, institution_display_names


STAFF_DIRECTORY_FACT_TYPES = {
    "staff_directory_repair",
    "staff_directory_alias",
    "staff_role_confirmation",
}


def query_staff_directory(
    store: TuoguanStore,
    *,
    query: str = "",
    role: str = "",
    include_inactive: bool = False,
    include_pending_verified: bool = False,
    limit: int = 30,
) -> dict[str, Any]:
    """Return a read-only staff roster with aliases and repair candidates.

    The result is evidence for the model. It does not update aliases, roles, or
    WeCom whitelist files; those changes must go through confirmed facts or a
    dedicated staff configuration flow.
    """

    entries = _build_entries(store, include_pending_verified=include_pending_verified)
    raw_query = str(query or "")
    query_keys = _name_keys(raw_query, institution_prefixes=institution_display_names(store))
    requested_role = str(role or "").strip().lower() or _infer_role(raw_query)
    filtered: list[dict[str, Any]] = []
    for entry in entries:
        if requested_role and str(entry.get("role") or "") != requested_role:
            continue
        if not include_inactive and not entry.get("is_active_staff"):
            if not (include_pending_verified and query_keys and entry.get("server_verified_pending_identity")):
                continue
        score = _match_score(entry, query_keys)
        if query_keys and score <= 0:
            continue
        item = deepcopy(entry)
        item["match_score"] = score
        filtered.append(item)
    if query_keys and not filtered and _is_directory_browse_query(raw_query):
        query_keys = set()
        for entry in entries:
            if requested_role and str(entry.get("role") or "") != requested_role:
                continue
            if not include_inactive and not entry.get("is_active_staff"):
                continue
            item = deepcopy(entry)
            item["match_score"] = 0
            filtered.append(item)
    filtered.sort(key=lambda item: (
        -int(item.get("match_score") or 0),
        -int(item.get("identity_authority_rank") or 0),
        str(item.get("role") or ""),
        str(item.get("business_name") or ""),
    ))
    safe_limit = max(1, min(int(limit or 30), 100))
    rows = filtered[:safe_limit]
    repair_candidates = [entry["repair_candidate"] for entry in rows if isinstance(entry.get("repair_candidate"), dict)]
    return {
        "ok": True,
        "query": str(query or ""),
        "role": requested_role,
        "result_count": len(rows),
        "total_staff_candidates": len(entries),
        "staff": rows,
        "repair_candidates": repair_candidates,
        "repair_candidate_count": len(repair_candidates),
        "rendered_text": _render_directory(rows, repair_candidates, query=str(query or "")),
        "render_verified": True,
        "writeback_verified": None,
    }


def _build_entries(store: TuoguanStore, *, include_pending_verified: bool = False) -> list[dict[str, Any]]:
    staff = _dict(store.read_json("staff.json", {}))
    mapping = _dict(store.read_json("teacher_wecom_map.json", {}))
    whitelist = _dict(store.read_json("wecom_whitelist.json", {}))
    directory = _dict(store.read_json("wecom_directory_cache.json", {}))
    recipient_bindings = _dict(whitelist.get("wecom_contacts"))
    operational_facts = _staff_facts(store)
    runtime_identities = active_identity_snapshot(store, tenant_id=current_tenant_id())
    pending_identities = _pending_runtime_identities(store) if include_pending_verified else {}
    institution_prefixes = institution_display_names(store)
    directory_members = {
        str(item.get("user_id") or item.get("userid") or ""): item
        for item in directory.get("members") or []
        if isinstance(item, dict) and str(item.get("user_id") or item.get("userid") or "")
    }
    allowed = {str(item) for item in whitelist.get("allowed_users") or [] if str(item)}
    supers = {str(item) for item in whitelist.get("super_users") or [] if str(item)}
    roles = _dict(whitelist.get("user_roles"))
    user_ids = set(staff) | set(directory_members) | allowed | supers | set(roles) | set(recipient_bindings)
    user_ids.update(str(value) for value in mapping.values() if str(value))
    if runtime_identities is not None:
        user_ids.update(runtime_identities)
    user_ids.update(pending_identities)

    entries: list[dict[str, Any]] = []
    aliases_by_user: dict[str, list[str]] = {}
    for alias, user_id in mapping.items():
        aliases_by_user.setdefault(str(user_id), []).append(str(alias))

    for user_id in sorted(user_ids):
        profile = _dict(staff.get(user_id))
        member = _dict(directory_members.get(user_id))
        fact = _dict(operational_facts.get(user_id))
        runtime_identity = runtime_identities.get(user_id) if isinstance(runtime_identities, dict) else None
        pending_identity = _dict(pending_identities.get(user_id))
        role = (
            str(runtime_identity.role or "unknown")
            if runtime_identity is not None
            else "unknown"
            if pending_identity
            else _role_for(user_id, profile, member, roles, supers)
        )
        raw_aliases = _unique(
            [
                user_id,
                str(profile.get("name") or ""),
                str(member.get("name") or ""),
                str(pending_identity.get("display_hint") or ""),
                *aliases_by_user.get(user_id, []),
                *[str(item) for item in fact.get("aliases") or [] if str(item)],
                str(fact.get("business_name") or fact.get("confirmed_name") or ""),
            ]
        )
        normalized_aliases = sorted({
            key
            for alias in raw_aliases
            for key in _name_keys(alias, institution_prefixes=institution_prefixes)
        })
        directory_name = str(member.get("name") or "")
        staff_name = str(profile.get("name") or "")
        business_name = (
            str(getattr(runtime_identity, "display_name", "") or "").strip()
            or str(pending_identity.get("display_hint") or "").strip()
            or str(fact.get("business_name") or fact.get("confirmed_name") or "").strip()
            or staff_name
            or _clean_business_name(directory_name, institution_prefixes=institution_prefixes)
            or user_id
        )
        in_directory = bool(member)
        is_whitelisted = user_id in allowed or user_id in supers or user_id in roles
        has_recipient_binding = user_id in recipient_bindings
        employment_status = (
            str(runtime_identity.approval_state or "unknown").strip().lower()
            if runtime_identity is not None
            else "pending"
            if pending_identity
            else str(profile.get("status") or "active").strip().lower()
        )
        is_active_staff = (
            employment_status == "approved"
            if runtime_identity is not None
            else False
            if pending_identity
            else employment_status not in {"inactive", "left", "offboarded", "terminated", "离职", "停用"} and bool(in_directory or is_whitelisted or has_recipient_binding)
        )
        outbound_eligible, outbound_reason = _current_outbound_eligibility(store, user_id)
        status = _membership_status(
            in_directory=in_directory,
            is_whitelisted=is_whitelisted,
            has_recipient_binding=has_recipient_binding,
            member=member,
            employment_status=employment_status,
        )
        entry = {
            "user_id": user_id,
            "business_name": business_name,
            "role": role,
            "role_label": _role_label(role),
            "staff_name": staff_name,
            "directory_name": directory_name,
            "known_aliases": raw_aliases,
            "normalized_aliases": normalized_aliases,
            "in_wecom_directory": in_directory,
            "has_wecom_recipient_binding": has_recipient_binding,
            "is_whitelisted": is_whitelisted,
            "outbound_eligible": outbound_eligible,
            "outbound_eligibility_reason": outbound_reason,
            "membership_status": status,
            "employment_status": employment_status,
            "identity_authority_rank": 3 if runtime_identity is not None else 2 if pending_identity else 1,
            "identity_approval_state": employment_status if pending_identity or runtime_identity is not None else "",
            "server_verified_pending_identity": bool(pending_identity),
            "pending_display_hint": str(pending_identity.get("display_hint") or ""),
            "is_active_staff": is_active_staff,
            "department_names": list(member.get("department_names") or []),
            "source_evidence": _evidence(
                user_id, profile, member, aliases_by_user.get(user_id, []), fact,
                is_whitelisted, has_recipient_binding, pending_identity,
            ),
        }
        repair = _repair_candidate(entry, institution_prefixes=institution_prefixes)
        if repair:
            entry["repair_candidate"] = repair
        entries.append(entry)
    return entries


def _pending_runtime_identities(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    """Expose only server-recorded pending WeCom identities to boss directory reads."""

    doc = _dict(store.read_json(PERSONNEL_AUTHORITY_DATA_FILE, {}))
    if not authority_is_enforced(doc):
        return {}
    try:
        validate_runtime_identity_document(doc)
    except IdentityAuthorityError:
        return {}
    authority = _dict(doc.get(AUTHORITY_KEY))
    if str(authority.get("tenant_id") or "").strip() != str(current_tenant_id() or "").strip():
        return {}
    access = _dict(doc.get(ACCESS_KEY))
    pending = _dict(access.get("pending"))
    return {
        str(user_id): _dict(row)
        for user_id, row in pending.items()
        if str(user_id).strip() and isinstance(row, dict)
    }


def _staff_facts(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    payload = _dict(store.read_json("operational_facts.json", {}))
    result: dict[str, dict[str, Any]] = {}
    for item in payload.get("facts") or []:
        if not isinstance(item, dict) or item.get("status") != "active":
            continue
        if str(item.get("fact_type") or "") not in STAFF_DIRECTORY_FACT_TYPES:
            continue
        user_id = str(item.get("subject") or "")
        value = _dict(item.get("value"))
        if user_id:
            result[user_id] = {**value, "source_fact_id": item.get("fact_id")}
    return result


def _match_score(entry: dict[str, Any], query_keys: set[str]) -> int:
    if not query_keys:
        return 0
    aliases = {str(item) for item in entry.get("normalized_aliases") or [] if str(item)}
    user_id_key = str(entry.get("user_id") or "").lower()
    best = 0
    for key in query_keys:
        if not key:
            continue
        if key == user_id_key or key in aliases:
            best = max(best, 100)
        elif any(key in alias for alias in aliases):
            best = max(best, 70)
        elif len(key) >= 2 and any(alias in key for alias in aliases if len(alias) >= 2):
            best = max(best, 50)
    return best


def _repair_candidate(entry: dict[str, Any], *, institution_prefixes: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if entry.get("server_verified_pending_identity"):
        return None
    directory_name = str(entry.get("directory_name") or "")
    business_name = str(entry.get("business_name") or "")
    user_id = str(entry.get("user_id") or "")
    if not user_id:
        return None
    clean_directory_name = _clean_business_name(directory_name, institution_prefixes=institution_prefixes)
    needs_cleanup = bool(directory_name and clean_directory_name and clean_directory_name != directory_name)
    missing_staff_name = not str(entry.get("staff_name") or "")
    configured_without_directory = (
        bool(entry.get("is_whitelisted"))
        and not bool(entry.get("in_wecom_directory"))
        and not bool(entry.get("has_wecom_recipient_binding"))
    )
    if not (needs_cleanup or missing_staff_name or configured_without_directory):
        return None
    value = {
        "user_id": user_id,
        "business_name": business_name,
        "directory_name": directory_name,
        "role": entry.get("role"),
        "aliases": entry.get("known_aliases") or [],
        "in_wecom_directory": bool(entry.get("in_wecom_directory")),
        "has_wecom_recipient_binding": bool(entry.get("has_wecom_recipient_binding")),
        "outbound_eligible": bool(entry.get("outbound_eligible")),
        "outbound_eligibility_reason": entry.get("outbound_eligibility_reason"),
        "is_whitelisted": bool(entry.get("is_whitelisted")),
        "membership_status": entry.get("membership_status"),
        "evidence": entry.get("source_evidence") or [],
    }
    return {
        "fact_type": "staff_directory_repair",
        "subject": user_id,
        "scope": "staff_directory",
        "value": value,
        "needs_owner_confirmation": True,
        "reason": "人员显示名、企业微信目录名或权限映射需要老板确认后才能作为运营事实使用。",
    }


def _render_directory(rows: list[dict[str, Any]], repair_candidates: list[dict[str, Any]], *, query: str) -> str:
    title = "人员目录只读结果"
    if query:
        title += f"（查询：{query}）"
    lines = [title]
    if not rows:
        lines.append("没有在当前人员目录、白名单或已确认人员事实里找到匹配对象。")
        lines.append("我已经查过当前可读目录；下一步只需要补一个最关键的信息：对方的姓名、昵称、手机号或企业微信 user_id。")
        return "\n".join(lines)
    for item in rows:
        aliases = "、".join(_public_aliases(item.get("known_aliases") or [], item.get("user_id"))) or "无"
        delivery_text = (
            "当前可主动联系"
            if item.get("outbound_eligible")
            else "当前不可主动联系（" + str(item.get("outbound_eligibility_reason") or "缺少可信投递事实") + "）"
        )
        lines.append(
            f"- {item.get('business_name')}（{item.get('role_label')}）："
            f"状态={item.get('membership_status')}；主动联系={delivery_text}；别名={aliases}"
        )
    if repair_candidates:
        lines.append("可整理为人员事实候选；必须由老板确认后才写入长期运营事实，未确认前不能说已经保存。")
    return "\n".join(lines)


def _public_aliases(values: list[Any], user_id: Any) -> list[str]:
    output = []
    for value in values:
        text = str(value or "").strip()
        if not text or text == str(user_id or ""):
            continue
        output.append(text)
    return _unique(output)[:8]


def _evidence(
    user_id: str,
    profile: dict[str, Any],
    member: dict[str, Any],
    mapped_aliases: list[str],
    fact: dict[str, Any],
    is_whitelisted: bool,
    has_recipient_binding: bool,
    pending_identity: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if profile:
        rows.append({"source": "staff.json", "user_id": user_id, "name": profile.get("name"), "role": profile.get("role")})
    if member:
        rows.append({"source": "wecom_directory_cache.json", "user_id": user_id, "name": member.get("name"), "department_names": member.get("department_names") or []})
    if mapped_aliases:
        rows.append({"source": "teacher_wecom_map.json", "user_id": user_id, "aliases": mapped_aliases})
    if is_whitelisted:
        rows.append({"source": "wecom_whitelist.json", "user_id": user_id})
    if has_recipient_binding:
        rows.append({"source": "wecom_recipient_binding", "user_id": user_id})
    if fact:
        rows.append({"source": "operational_facts.json", "user_id": user_id, "fact_id": fact.get("source_fact_id")})
    if pending_identity:
        rows.append({
            "source": "runtime_identity_authority.pending",
            "user_id": user_id,
            "platform": pending_identity.get("platform"),
            "display_hint": pending_identity.get("display_hint"),
            "first_seen_at": pending_identity.get("first_seen_at"),
            "last_seen_at": pending_identity.get("last_seen_at"),
        })
    return rows


def _role_for(user_id: str, profile: dict[str, Any], member: dict[str, Any], roles: dict[str, Any], supers: set[str]) -> str:
    if user_id in supers:
        return "boss"
    role = str(profile.get("role") or roles.get(user_id) or "").strip()
    if role:
        return role
    name = str(member.get("name") or profile.get("name") or "")
    if "店长" in name or "校长" in name:
        return "manager"
    if "老师" in name:
        return "teacher"
    return "staff"


def _membership_status(*, in_directory: bool, is_whitelisted: bool, has_recipient_binding: bool, member: dict[str, Any], employment_status: str = "active") -> str:
    if str(employment_status or "").lower() == "pending":
        return "企业微信账号已验证，待老板正式确认"
    if str(employment_status or "").lower() in {"inactive", "left", "offboarded", "terminated", "离职", "停用"}:
        return "已离职停用（保留历史）"
    if in_directory and is_whitelisted:
        return "企业微信在职且已授权"
    if has_recipient_binding and is_whitelisted:
        return "在职、身份可信且已绑定企业微信收件人"
    if in_directory:
        return "企业微信目录存在但未授权"
    if is_whitelisted:
        return "系统已授权但企业微信目录未找到"
    return "历史映射或待确认"


def _current_outbound_eligibility(store: TuoguanStore, user_id: str) -> tuple[bool, str]:
    """Project the one current delivery-authority decision into directory facts.

    This does not send, select a recipient, or interpret business content.  It
    lets Hermes see the same active/trusted/bound/tenant decision that the
    durable outbox will enforce at execution time.
    """

    try:
        from .proactive_delivery_authority import ProactiveDeliveryAuthority
        from .tenant_context import current_tenant_id
        from .work_runtime import ReplyDestination

        tenant_id = str(current_tenant_id() or "")
        decision = ProactiveDeliveryAuthority(store.data_dir).decide(ReplyDestination(
            tenant_id=tenant_id,
            channel="wecom_callback",
            recipient_id=str(user_id or ""),
            source_identity="relationship_touch:" + tenant_id,
        ))
        return bool(decision.allowed), str(decision.reason or "")
    except Exception:
        return False, "delivery_authority_unavailable"


def _role_label(role: str) -> str:
    return {
        "boss": "老板",
        "manager": "店长",
        "teacher": "老师",
        "staff": "员工",
        "unknown": "待确认",
    }.get(str(role or ""), str(role or "未知"))


def _infer_role(value: Any) -> str:
    text = str(value or "")
    wants_teacher = "老师" in text
    wants_manager = "店长" in text or "校长" in text
    wants_boss = "老板" in text or "总" in text
    selected = [role for role, enabled in (("teacher", wants_teacher), ("manager", wants_manager), ("boss", wants_boss)) if enabled]
    return selected[0] if len(selected) == 1 else ""


def _is_directory_browse_query(value: Any) -> bool:
    text = str(value or "")
    return any(
        term in text
        for term in (
            "企业微信",
            "通讯录",
            "都有谁",
            "有哪些",
            "名单",
            "人员",
            "员工",
            "系统里挂",
            "配置人员",
            "乱码",
        )
    )


def _clean_business_name(value: Any, *, institution_prefixes: tuple[str, ...] = ()) -> str:
    keys = _name_keys(value, institution_prefixes=institution_prefixes)
    if not keys:
        return ""
    prefixed = tuple(_ascii_cjk_key(item) for item in institution_prefixes if _ascii_cjk_key(item))
    unprefixed = [key for key in keys if not key.startswith(prefixed)]
    teacher_names = [key for key in unprefixed if key.endswith(_ascii_cjk_key("老师"))]
    if teacher_names:
        return sorted(teacher_names, key=len, reverse=True)[0]
    if unprefixed:
        return sorted(unprefixed, key=len, reverse=True)[0]
    return sorted(keys, key=len, reverse=True)[0]


def _name_keys(value: Any, *, institution_prefixes: tuple[str, ...] = ()) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return set()
    compact = "".join(
        ch
        for ch in text
        if ("\u4e00" <= ch <= "\u9fff") or ch.isascii() and ch.isalnum()
    ).lower()
    if not compact:
        return set()
    variants = {compact}
    for prefix in institution_prefixes:
        key = _ascii_cjk_key(prefix)
        variants.update(item.removeprefix(key) for item in list(variants) if item.startswith(key))
    for suffix in ("老师", "店长", "校长", "执行校长"):
        key = _ascii_cjk_key(suffix)
        variants.update(item.removesuffix(key) for item in list(variants) if item.endswith(key))
    variants = {item for item in variants if item}
    return variants


def _ascii_cjk_key(value: str) -> str:
    return "".join(ch for ch in value if ("\u4e00" <= ch <= "\u9fff") or ch.isascii() and ch.isalnum()).lower()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _unique(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result

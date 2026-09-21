"""Confirmation-first staff configuration backed by the WeCom directory cache."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from .models import UserIdentity
from .programs import SUMMER_PROGRAM_ID
from .store import TuoguanStore


DIRECTORY_CACHE_FILE = "wecom_directory_cache.json"
PENDING_STAFF_CONFIGS_FILE = "pending_staff_configs.json"
STAFF_CONFIG_AUDIT_FILE = "staff_config_audit.jsonl"

_COURSES = ("语文", "数学", "英语", "练字", "科学实验", "科学", "活动")
_GENERIC_NAMES = {"暑假班老师", "暑假老师", "普通老师", "课程老师", "托管班老师"}
_CONFIRM_WORDS = {"确认配置", "确认执行", "确认"}
_CANCEL_WORDS = {"取消配置", "取消执行", "取消"}


def is_staff_config_query(text: str) -> bool:
    compact = _compact(text)
    return any(
        phrase in compact
        for phrase in (
            "查看暑假班老师名单",
            "查看2026暑假班老师名单",
            "查看未配置权限的暑假班老师",
            "查看未加入企业微信的暑假班老师",
            "查看2026暑假班人员权限",
            "谁还没有加入企业微信",
            "哪些老师未绑定权限",
        )
    )


def is_staff_config_request(text: str) -> bool:
    compact = _compact(text)
    if is_staff_config_query(text):
        return False
    explicit = any(
        phrase in compact
        for phrase in (
            "配置暑假班老师名单",
            "暑假班老师名单如下",
            "设置相关老师为暑假班店长",
            "加入2026暑假班",
            "设为暑假班",
            "负责语文",
            "负责数学",
            "负责英语",
            "负责练字",
            "负责科学实验",
            "继续配置暑假班老师",
        )
    )
    return explicit or bool(
        "老师" in compact
        and ("暑假班" in compact or "2026暑假班" in compact)
        and any(action in compact for action in ("配置", "名单", "设置", "设为", "加入", "负责"))
    )


def is_staff_config_confirmation(text: str) -> bool:
    return _compact(text) in _CONFIRM_WORDS


def is_staff_config_cancel(text: str) -> bool:
    return _compact(text) in _CANCEL_WORDS


def is_staff_candidate_selection(text: str) -> bool:
    compact = _compact(text)
    return bool(re.fullmatch(r"(?:选择)?[\u4e00-\u9fffA-Za-z·]{1,8}老师(?:选|选择)?[1-9]", compact))


def load_directory_cache(store: TuoguanStore) -> dict[str, Any]:
    data = store.read_json(DIRECTORY_CACHE_FILE, {})
    return data if isinstance(data, dict) else {}


def search_directory(
    store: TuoguanStore,
    query: str,
    *,
    field: str = "auto",
    department: str = "",
) -> list[dict[str, Any]]:
    """Search cached WeCom members by exact name, mobile, userid or department."""

    cache = load_directory_cache(store)
    members = [item for item in cache.get("members") or [] if isinstance(item, dict)]
    value = str(query or "").strip()
    normalized_mobile = _digits(value)
    results: list[dict[str, Any]] = []
    for item in members:
        matched = False
        if field in {"auto", "name"} and _compact(item.get("name")) == _compact(value):
            matched = True
        if field in {"auto", "mobile"} and normalized_mobile and _digits(item.get("mobile")) == normalized_mobile:
            matched = True
        if field in {"auto", "userid"} and value and str(item.get("user_id") or "").lower() == value.lower():
            matched = True
        if department:
            names = {_compact(name) for name in item.get("department_names") or []}
            ids = {str(dep_id) for dep_id in item.get("department_ids") or []}
            matched = matched and (_compact(department) in names or str(department) in ids)
        if matched:
            results.append(dict(item))
    return results


def build_staff_config_proposal(
    store: TuoguanStore,
    identity: UserIdentity,
    text: str,
) -> dict[str, Any]:
    requests = parse_staff_requests(text)
    directory = load_directory_cache(store)
    candidates: list[dict[str, Any]] = []
    permission_changes: list[dict[str, Any]] = []
    missing_staff: list[dict[str, Any]] = []
    ambiguous_staff: list[dict[str, Any]] = []
    for request in requests:
        if request.get("user_id"):
            matches = search_directory(store, request["user_id"], field="userid")
        elif request.get("mobile"):
            matches = search_directory(store, request["mobile"], field="mobile")
        else:
            matches = search_directory(store, request["name"], field="name")
        entry = dict(request)
        if len(matches) == 1:
            selected = _public_member(matches[0])
            entry.update({"match_status": "unique", "selected": selected})
            permission_changes.append(_permission_change(request, selected))
        elif len(matches) > 1:
            choices = [_public_member(item) for item in matches]
            entry.update({"match_status": "multiple", "choices": choices})
            ambiguous_staff.append({"name": request["name"], "choices": choices})
        else:
            entry.update({"match_status": "missing", "selected": None})
            missing_staff.append({"name": request["name"], "course_role": request["course_role"]})
        candidates.append(entry)

    now = datetime.now()
    proposal = {
        "id": f"staff_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
        "status": "needs_selection" if ambiguous_staff else "waiting_confirmation",
        "program_id": SUMMER_PROGRAM_ID,
        "requested_by": identity.canonical_user_id,
        "requested_by_name": identity.person_name or identity.platform_user_id,
        "source_text": text,
        "staff_candidates": candidates,
        "missing_staff": missing_staff,
        "ambiguous_staff": ambiguous_staff,
        "permission_changes": permission_changes,
        "directory_status": str(directory.get("status") or "unavailable"),
        "member_create_available": bool((directory.get("capabilities") or {}).get("member_create")),
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(hours=24)).isoformat(timespec="seconds"),
    }
    items = _load_proposals(store)
    items.append(proposal)
    store.write_json(PENDING_STAFF_CONFIGS_FILE, items[-500:])
    return proposal


def refresh_latest_staff_config_proposal(
    store: TuoguanStore,
    identity: UserIdentity,
) -> dict[str, Any] | None:
    items = _load_proposals(store)
    previous = next(
        (
            item for item in reversed(items)
            if isinstance(item, dict)
            and str(item.get("requested_by") or "") == identity.canonical_user_id
            and str(item.get("status") or "") in {"waiting_confirmation", "needs_selection"}
        ),
        None,
    )
    if previous is None:
        return None
    previous["status"] = "superseded"
    _save_proposals(store, items)
    return build_staff_config_proposal(store, identity, str(previous.get("source_text") or ""))


def select_staff_candidate(
    store: TuoguanStore,
    identity: UserIdentity,
    proposal_id: str,
    text: str,
) -> dict[str, Any]:
    proposal, items = _proposal_for_actor(store, identity, proposal_id)
    if proposal is None:
        return {"ok": False, "error": "proposal_not_found"}
    match = re.fullmatch(r"(?:选择)?([\u4e00-\u9fffA-Za-z·]{1,8}老师)(?:选|选择)?([1-9])", _compact(text))
    if not match:
        return {"ok": False, "error": "invalid_selection"}
    name, number = match.group(1), int(match.group(2))
    target = next(
        (item for item in proposal.get("staff_candidates") or [] if isinstance(item, dict) and item.get("name") == name),
        None,
    )
    choices = target.get("choices") if isinstance(target, dict) else None
    if not isinstance(choices, list) or number > len(choices):
        return {"ok": False, "error": "candidate_not_found"}
    selected = dict(choices[number - 1])
    target["selected"] = selected
    target["match_status"] = "selected"
    proposal["ambiguous_staff"] = [
        item for item in proposal.get("ambiguous_staff") or []
        if isinstance(item, dict) and item.get("name") != name
    ]
    proposal["permission_changes"] = [
        item for item in proposal.get("permission_changes") or []
        if isinstance(item, dict) and item.get("name") != name
    ] + [_permission_change(target, selected)]
    proposal["status"] = "needs_selection" if proposal["ambiguous_staff"] else "waiting_confirmation"
    _save_proposals(store, items)
    return {"ok": True, "proposal": proposal, "selected": selected}


def confirm_staff_config(
    store: TuoguanStore,
    identity: UserIdentity,
    proposal_id: str,
) -> dict[str, Any]:
    proposal, items = _proposal_for_actor(store, identity, proposal_id)
    if proposal is None:
        return {"ok": False, "error": "proposal_not_found"}
    if _expired(proposal):
        proposal["status"] = "expired"
        _save_proposals(store, items)
        return {"ok": False, "error": "proposal_expired"}
    if proposal.get("status") != "waiting_confirmation" or proposal.get("ambiguous_staff"):
        return {"ok": False, "error": "proposal_not_ready"}

    staff = store.read_json("staff.json", {})
    staff = staff if isinstance(staff, dict) else {}
    mapping = store.read_json("teacher_wecom_map.json", {})
    mapping = mapping if isinstance(mapping, dict) else {}
    whitelist = store.read_json("wecom_whitelist.json", {})
    whitelist = whitelist if isinstance(whitelist, dict) else {}
    allowed = {str(item) for item in whitelist.get("allowed_users") or [] if str(item)}
    roles = whitelist.get("user_roles") if isinstance(whitelist.get("user_roles"), dict) else {}
    summer_managers = {str(item) for item in whitelist.get("summer_manager_ids") or [] if str(item)}
    now = datetime.now().isoformat(timespec="seconds")

    applied: list[dict[str, Any]] = []
    for change in proposal.get("permission_changes") or []:
        if not isinstance(change, dict) or not change.get("user_id"):
            continue
        user_id = str(change["user_id"])
        name = str(change.get("name") or user_id)
        profile = staff.get(user_id) if isinstance(staff.get(user_id), dict) else {}
        programs = {str(item) for item in profile.get("program_ids") or [] if str(item)}
        programs.add(SUMMER_PROGRAM_ID)
        profile.update(
            {
                "user_id": user_id,
                "name": name,
                "role": "manager" if change.get("program_role") == "summer_manager" else "teacher",
                "program_id": SUMMER_PROGRAM_ID,
                "program_ids": sorted(programs),
                "program_role": str(change.get("program_role") or "summer_teacher"),
                "course_role": str(change.get("course_role") or ""),
                "permission_scope": str(change.get("permission_scope") or "summer_course_record"),
                "status": "active",
                "created_by": profile.get("created_by") or identity.canonical_user_id,
                "created_at": profile.get("created_at") or now,
                "updated_by": identity.canonical_user_id,
                "updated_at": now,
            }
        )
        staff[user_id] = profile
        mapping[name] = user_id
        allowed.add(user_id)
        if change.get("program_role") == "summer_manager":
            roles[user_id] = "manager"
            summer_managers.add(user_id)
        else:
            roles[user_id] = "teacher"
            summer_managers.discard(user_id)
        applied.append(dict(change))

    whitelist["allowed_users"] = sorted(allowed)
    whitelist["user_roles"] = roles
    whitelist["summer_manager_ids"] = sorted(summer_managers)
    store.write_json("staff.json", staff)
    store.write_json("teacher_wecom_map.json", mapping)
    store.write_json("wecom_whitelist.json", whitelist)
    proposal["status"] = "applied"
    proposal["confirmed_by"] = identity.canonical_user_id
    proposal["confirmed_at"] = now
    proposal["applied_count"] = len(applied)
    _save_proposals(store, items)
    _append_audit(store, proposal, applied)
    return {"ok": True, "proposal": proposal, "applied": applied}


def cancel_staff_config(store: TuoguanStore, identity: UserIdentity, proposal_id: str) -> dict[str, Any]:
    proposal, items = _proposal_for_actor(store, identity, proposal_id)
    if proposal is None:
        return {"ok": False, "error": "proposal_not_found"}
    proposal["status"] = "cancelled"
    proposal["cancelled_at"] = datetime.now().isoformat(timespec="seconds")
    _save_proposals(store, items)
    return {"ok": True, "proposal": proposal}


def latest_staff_config_proposal(store: TuoguanStore, user_id: str) -> dict[str, Any] | None:
    for item in reversed(_load_proposals(store)):
        if not isinstance(item, dict):
            continue
        if str(item.get("requested_by") or "") != str(user_id):
            continue
        if str(item.get("status") or "") in {"waiting_confirmation", "needs_selection"}:
            return item
    return None


def proposal_reply(proposal: dict[str, Any]) -> str:
    found = [item for item in proposal.get("staff_candidates") or [] if isinstance(item, dict) and item.get("match_status") in {"unique", "selected"}]
    lines = ["已生成 2026暑假班人员配置待确认方案。", "", "已找到："]
    if found:
        for index, item in enumerate(found, 1):
            member = item.get("selected") or {}
            lines.append(
                f"{index}. {item.get('name')} user_id={member.get('user_id')} 手机号={_mask_mobile(member.get('mobile'))}；{_role_label(item)}"
            )
    else:
        lines.append("暂无唯一匹配人员。")
    ambiguous = proposal.get("ambiguous_staff") or []
    if ambiguous:
        lines.extend(["", "同名待选择："])
        for item in ambiguous:
            lines.append(f"{item.get('name')}：")
            for index, member in enumerate(item.get("choices") or [], 1):
                lines.append(f"  {index}. user_id={member.get('user_id')} 手机号={_mask_mobile(member.get('mobile'))}")
        lines.append("请回复“张老师选1”这类指令完成选择。")
    missing = proposal.get("missing_staff") or []
    if missing:
        lines.extend(["", "未找到："])
        lines.extend(f"- {item.get('name')}（待管理员加入企业微信）" for item in missing)
        if not proposal.get("member_create_available"):
            lines.append("当前未检测到可用的企业微信成员邀请/创建权限，请先由管理员添加进企业微信。")
            lines.append("添加完成后，回复“继续配置暑假班老师”即可继续匹配。")
    lines.extend(["", f"方案编号：{proposal.get('id')}"])
    if ambiguous:
        lines.append("同名人员选择完成后才能确认配置。")
    else:
        lines.append("核对无误后回复“确认配置”；不确认不会写入任何真实权限。")
    return "\n".join(lines)


def staff_roster_reply(store: TuoguanStore) -> str:
    staff = store.read_json("staff.json", {})
    staff = staff if isinstance(staff, dict) else {}
    configured = [
        item for item in staff.values()
        if isinstance(item, dict) and SUMMER_PROGRAM_ID in {str(value) for value in item.get("program_ids") or []}
    ]
    lines = ["【2026暑假班人员权限】", "", "已配置："]
    if configured:
        for item in sorted(configured, key=lambda value: (str(value.get("program_role")), str(value.get("name")))):
            role = "暑假班店长" if item.get("program_role") == "summer_manager" else f"{item.get('course_role') or '普通'}老师"
            lines.append(f"- {item.get('name') or item.get('user_id')}：{role}")
    else:
        lines.append("暂无已配置人员。")
    pending = [
        item for item in _load_proposals(store)
        if isinstance(item, dict) and item.get("status") in {"waiting_confirmation", "needs_selection"}
    ]
    lines.extend(["", "待处理："])
    pending_lines: list[str] = []
    for proposal in pending[-5:]:
        for item in proposal.get("staff_candidates") or []:
            if isinstance(item, dict) and item.get("match_status") in {"unique", "selected"}:
                pending_lines.append(f"- {item.get('name')}：已匹配，待机构负责人确认配置")
        for item in proposal.get("missing_staff") or []:
            pending_lines.append(f"- {item.get('name')}：未在企业微信通讯录中找到")
        for item in proposal.get("ambiguous_staff") or []:
            pending_lines.append(f"- {item.get('name')}：存在同名人员，待选择")
    lines.extend(pending_lines or ["暂无待处理人员。"])
    return "\n".join(lines)


def parse_staff_requests(text: str) -> list[dict[str, str]]:
    normalized = str(text or "").replace("\r", "\n")
    parts: list[str] = []
    for block in re.split(r"[；;\n]+", normalized):
        if "：" in block or ":" in block:
            parts.extend(re.split(r"[：:]", block))
        else:
            parts.append(block)
    requests: dict[str, dict[str, str]] = {}
    for raw in parts:
        segment = str(raw).strip(" ，,。.")
        segment = re.sub(r"^(?:配置|设置|把|将|添加|新增)", "", segment)
        match = re.match(r"([\u4e00-\u9fffA-Za-z·]{1,8}老师)", segment)
        if not match:
            continue
        name = match.group(1)
        if name in _GENERIC_NAMES or name.startswith("暑假班"):
            continue
        manager = "店长" in segment or "负责人" in segment
        course = next((item for item in _COURSES if item in segment), "")
        if course == "科学":
            course = "科学实验"
        requests[name] = {
            "name": name,
            "program_id": SUMMER_PROGRAM_ID,
            "program_role": "summer_manager" if manager else "summer_teacher",
            "course_role": "暑假班店长" if manager else course,
            "permission_scope": "summer_manager_dashboard" if manager else "summer_course_record",
            "mobile": (re.search(r"1[3-9]\d{9}", segment) or [""])[0],
            "user_id": _extract_user_id(segment),
        }
    return list(requests.values())


def _permission_change(request: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(member.get("user_id") or ""),
        "name": str(request.get("name") or member.get("name") or ""),
        "program_id": SUMMER_PROGRAM_ID,
        "program_role": str(request.get("program_role") or "summer_teacher"),
        "course_role": str(request.get("course_role") or ""),
        "permission_scope": str(request.get("permission_scope") or "summer_course_record"),
    }


def _public_member(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(item.get("user_id") or item.get("userid") or ""),
        "name": str(item.get("name") or ""),
        "mobile": str(item.get("mobile") or ""),
        "department_ids": list(item.get("department_ids") or item.get("department") or []),
        "department_names": list(item.get("department_names") or []),
    }


def _role_label(item: dict[str, Any]) -> str:
    if item.get("program_role") == "summer_manager":
        return "2026暑假班店长"
    return f"{item.get('course_role') or '普通'}老师"


def _proposal_for_actor(
    store: TuoguanStore,
    identity: UserIdentity,
    proposal_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    items = _load_proposals(store)
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "") == str(proposal_id) and str(item.get("requested_by") or "") == identity.canonical_user_id:
            return item, items
    return None, items


def _load_proposals(store: TuoguanStore) -> list[dict[str, Any]]:
    data = store.read_json(PENDING_STAFF_CONFIGS_FILE, [])
    return data if isinstance(data, list) else []


def _save_proposals(store: TuoguanStore, items: list[dict[str, Any]]) -> None:
    store.write_json(PENDING_STAFF_CONFIGS_FILE, items[-500:])


def _append_audit(store: TuoguanStore, proposal: dict[str, Any], applied: list[dict[str, Any]]) -> None:
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "action": "staff_config_applied",
        "proposal_id": str(proposal.get("id") or ""),
        "program_id": SUMMER_PROGRAM_ID,
        "confirmed_by": str(proposal.get("confirmed_by") or ""),
        "applied": applied,
    }
    path = store.path_for(STAFF_CONFIG_AUDIT_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _expired(proposal: dict[str, Any]) -> bool:
    try:
        return datetime.fromisoformat(str(proposal.get("expires_at") or "")) < datetime.now()
    except ValueError:
        return False


def _compact(value: Any) -> str:
    return str(value or "").replace(" ", "").strip()


def _digits(value: Any) -> str:
    return "".join(re.findall(r"\d", str(value or "")))


def _mask_mobile(value: Any) -> str:
    mobile = _digits(value)
    if len(mobile) >= 7:
        return mobile[:3] + "****" + mobile[-4:]
    return mobile or "未提供"


def _extract_user_id(text: str) -> str:
    match = re.search(r"(?:user_?id|userid)\s*[=：:]?\s*([A-Za-z0-9._-]+)", str(text or ""), re.IGNORECASE)
    return match.group(1) if match else ""

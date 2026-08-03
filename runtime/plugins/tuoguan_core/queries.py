"""Permission-scoped student queries for tutoring-center users."""

from __future__ import annotations

from typing import Any

from .models import UserIdentity
from .permissions import PermissionService
from .records import AmbiguousStudentError, UnknownStudentError, recognize_student
from .store import TuoguanStore


_CLASS_QUERY_TERMS = (
    "我班",
    "我们班",
    "我负责",
    "我的学生",
    "我的孩子",
    "全部学生",
    "所有学生",
    "学生名单",
)
_QUERY_PREFIXES = ("查", "查询", "查一下", "看一下", "看看")
_QUERY_PATTERNS = (
    "的信息",
    "的资料",
    "最近记录",
    "有哪些记录",
    "哪些记录",
    "最近表现",
    "最近怎么样",
    "现在什么情况",
    "什么情况",
    "情况怎么样",
    "表现咋样",
    "表现怎么样",
)
_QUERY_INTENT_TERMS = ("知道", "了解", "想看看", "想看")
_QUERY_CONTEXT_TERMS = ("情况", "表现", "记录", "资料", "信息")
_SCOPE_TERMS = ("我班", "我们班", "负责", "我带", "名下", "我的学生", "我的孩子")
_SCOPE_OBJECT_TERMS = ("学生", "孩子")
_ROSTER_QUERY_TERMS = ("谁", "哪些", "多少", "几个", "名单", "都有")


def _is_class_scope_query(compact: str) -> bool:
    if any(term in compact for term in _CLASS_QUERY_TERMS):
        return any(
            term in compact
            for term in ("多少", "几个", "哪些", "名单", "学生", "孩子", "谁", "都有")
        )
    return (
        any(term in compact for term in _SCOPE_TERMS)
        and any(term in compact for term in _SCOPE_OBJECT_TERMS)
        and any(term in compact for term in _ROSTER_QUERY_TERMS)
    )


def is_student_query(text: str) -> bool:
    compact = str(text or "").replace(" ", "")
    if _is_class_scope_query(compact):
        return True
    if compact.startswith(_QUERY_PREFIXES) or any(
        term in compact for term in _QUERY_PATTERNS
    ):
        return True
    return any(term in compact for term in _QUERY_INTENT_TERMS) and any(
        term in compact for term in _QUERY_CONTEXT_TERMS
    )


def _visible_students(
    store: TuoguanStore,
    identity: UserIdentity,
) -> dict[str, dict[str, Any]]:
    students = store.read_json("students.json", {})
    if not isinstance(students, dict):
        return {}
    permissions = PermissionService(store)
    return {
        str(name): profile
        for name, profile in students.items()
        if isinstance(profile, dict)
        and permissions.can_view_student(identity, str(name))
    }


def _class_summary(store: TuoguanStore, identity: UserIdentity) -> str:
    students = _visible_students(store, identity)
    names = sorted(students)
    if not names:
        return "当前没有查到你负责范围内的学生。"
    label = "你当前负责" if identity.role == "teacher" else "你当前可查看"
    return f"{label}{len(names)}名学生：\n" + "、".join(names)


def _recent_records(store: TuoguanStore, student_name: str) -> list[dict[str, Any]]:
    records = store.read_json("records.json", [])
    if not isinstance(records, list):
        return []
    matching = [
        item
        for item in records
        if isinstance(item, dict)
        and str(item.get("student_name") or item.get("student") or "")
        == student_name
    ]
    return sorted(
        matching,
        key=lambda item: str(item.get("timestamp") or item.get("time") or ""),
        reverse=True,
    )[:3]


def _student_summary(
    store: TuoguanStore,
    identity: UserIdentity,
    student_name: str,
) -> str:
    students = store.read_json("students.json", {})
    profile = students.get(student_name, {}) if isinstance(students, dict) else {}
    if not isinstance(profile, dict):
        return "没有查到这名学生。"
    if not PermissionService(store).can_view_student(identity, student_name):
        return f"学生“{student_name}”不在你的负责范围内，不能查看其资料。"

    lines = [f"【学生信息】{student_name}"]
    if profile.get("class"):
        lines.append(f"班级：{profile['class']}")
    if profile.get("phone"):
        lines.append(f"联系电话：{profile['phone']}")
    scores = profile.get("scores")
    if isinstance(scores, dict) and scores:
        score_text = "、".join(f"{key} {value}" for key, value in scores.items())
        lines.append(f"成绩：{score_text}")

    records = _recent_records(store, student_name)
    if records:
        lines.append("最近记录：")
        for item in records:
            timestamp = str(item.get("timestamp") or item.get("time") or "")
            content = str(item.get("content") or "")
            lines.append(f"- {timestamp[:10]} {content}".strip())
    else:
        lines.append("最近暂无记录。")
    return "\n".join(lines)


def answer_student_query(
    text: str,
    identity: UserIdentity,
    store: TuoguanStore,
) -> str:
    compact = str(text or "").replace(" ", "")
    if _is_class_scope_query(compact):
        return _class_summary(store, identity)
    try:
        student_name = recognize_student(text, store)
    except AmbiguousStudentError:
        return "匹配到多名学生，请发送完整姓名后再查询。"
    except UnknownStudentError:
        return "没有识别到学生姓名，请发送“查询+学生完整姓名”。"
    return _student_summary(store, identity, student_name)

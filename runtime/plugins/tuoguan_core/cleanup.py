"""Conservative cleanup helpers for imported tutoring data."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any


_CLASS_PREFIX = re.compile(
    r"^(?:正泰)?[一二三四五六][0-9一二三四五六七八九十]?[ 班]+"
)
_CLASS_ONLY = re.compile(r"^(?:正泰)?[一二三四五六][0-9一二三四五六七八九十]?$")
_MOBILE = re.compile(r"^1[3-9]\d{9}$")


@dataclass(frozen=True)
class CleanupResult:
    students: dict[str, dict[str, Any]]
    records: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    removed_names: list[str]


def _canonical_name(name: str) -> str:
    return _CLASS_PREFIX.sub("", name).strip()


def _rewrite_student_name(
    items: list[dict[str, Any]],
    old_name: str,
    new_name: str,
) -> None:
    for item in items:
        if isinstance(item, dict) and item.get("student_name") == old_name:
            item["student_name"] = new_name


def merge_student_names(
    students: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    renames: dict[str, str],
) -> CleanupResult:
    cleaned_students = copy.deepcopy(students)
    cleaned_records = copy.deepcopy(records)
    cleaned_tasks = copy.deepcopy(tasks)
    removed: list[str] = []

    for old_name, canonical_name in renames.items():
        if old_name not in cleaned_students or canonical_name not in cleaned_students:
            continue
        canonical = cleaned_students[canonical_name]
        duplicate = cleaned_students[old_name]
        canonical_phone = str(canonical.get("phone") or "").strip()
        duplicate_phone = str(duplicate.get("phone") or "").strip()
        if canonical_phone in {"", "无"} and _MOBILE.fullmatch(duplicate_phone):
            canonical["phone"] = duplicate_phone
        aliases = list(canonical.get("aliases") or [])
        if old_name not in aliases:
            aliases.append(old_name)
        canonical["aliases"] = aliases
        _rewrite_student_name(cleaned_records, old_name, canonical_name)
        _rewrite_student_name(cleaned_tasks, old_name, canonical_name)
        del cleaned_students[old_name]
        removed.append(old_name)

    return CleanupResult(
        students=cleaned_students,
        records=cleaned_records,
        tasks=cleaned_tasks,
        removed_names=removed,
    )


def clean_student_duplicates(
    students: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
) -> CleanupResult:
    cleaned_students = copy.deepcopy(students)
    cleaned_records = copy.deepcopy(records)
    cleaned_tasks = copy.deepcopy(tasks)
    removed: list[str] = []

    for old_name in list(cleaned_students):
        canonical_name = _canonical_name(old_name)
        if canonical_name == old_name or canonical_name not in cleaned_students:
            continue

        canonical = cleaned_students[canonical_name]
        duplicate = cleaned_students[old_name]
        canonical_phone = str(canonical.get("phone") or "").strip()
        duplicate_phone = str(duplicate.get("phone") or "").strip()
        if canonical_phone in {"", "无"} and _MOBILE.fullmatch(duplicate_phone):
            canonical["phone"] = duplicate_phone

        aliases = list(canonical.get("aliases") or [])
        if old_name not in aliases:
            aliases.append(old_name)
        canonical["aliases"] = aliases

        _rewrite_student_name(cleaned_records, old_name, canonical_name)
        _rewrite_student_name(cleaned_tasks, old_name, canonical_name)
        del cleaned_students[old_name]
        removed.append(old_name)

    for name in list(cleaned_students):
        info = cleaned_students[name]
        referenced_student = str(info.get("phone") or "").strip()
        if (
            _CLASS_ONLY.fullmatch(name)
            and referenced_student in cleaned_students
            and referenced_student != name
        ):
            del cleaned_students[name]
            removed.append(name)

    return CleanupResult(
        students=cleaned_students,
        records=cleaned_records,
        tasks=cleaned_tasks,
        removed_names=removed,
    )

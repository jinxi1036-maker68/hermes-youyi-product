"""One-time import of legacy tutoring data into the Hermes-owned store."""

from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from utils import atomic_json_write


REQUIRED_FILES = (
    "students.json",
    "records.json",
    "tasks.json",
    "teacher_wecom_map.json",
    "teacher_feishu_map.json",
    "wecom_whitelist.json",
    "feishu_whitelist.json",
)
OPTIONAL_FILES = (
    "holidays.json",
    "manual_id_mapping.json",
    "operation_log.json",
    "pending_notifications.json",
)


class MigrationError(RuntimeError):
    """Raised before or during a tutoring-data migration."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"Unable to read {path.name}: {exc}") from exc


def _item_count(name: str, value: Any) -> int:
    if name == "tasks.json" and isinstance(value, dict):
        tasks = value.get("tasks")
        return len(tasks) if isinstance(tasks, list) else 0
    return len(value) if isinstance(value, (dict, list)) else 1


def _validated_source(source_dir: Path) -> tuple[dict[str, Any], dict[str, str]]:
    loaded: dict[str, Any] = {}
    hashes: dict[str, str] = {}
    for name in REQUIRED_FILES:
        path = source_dir / name
        if not path.is_file():
            raise MigrationError(f"Required source file is missing: {name}")
        loaded[name] = _load_json(path)
        hashes[name] = _sha256(path)
    for name in OPTIONAL_FILES:
        path = source_dir / name
        if path.is_file():
            loaded[name] = _load_json(path)
            hashes[name] = _sha256(path)

    if not isinstance(loaded["students.json"], dict):
        raise MigrationError("students.json must contain an object")
    if not isinstance(loaded["records.json"], list):
        raise MigrationError("records.json must contain a list")
    tasks = loaded["tasks.json"]
    if not (
        isinstance(tasks, list)
        or (isinstance(tasks, dict) and isinstance(tasks.get("tasks"), list))
    ):
        raise MigrationError("tasks.json must be a list or contain a tasks list")
    for name in (
        "teacher_wecom_map.json",
        "teacher_feishu_map.json",
        "wecom_whitelist.json",
        "feishu_whitelist.json",
    ):
        if not isinstance(loaded[name], dict):
            raise MigrationError(f"{name} must contain an object")
    return loaded, hashes


def _canonical_ids(data: dict[str, Any]) -> dict[str, str]:
    wecom = data["teacher_wecom_map.json"]
    feishu = data["teacher_feishu_map.json"]
    return {
        str(feishu[name]): str(wecom[name])
        for name in feishu
        if name in wecom and feishu[name] and wecom[name]
    }


def _normalize_students(
    students: dict[str, Any],
    canonical_ids: dict[str, str],
) -> dict[str, Any]:
    normalized = deepcopy(students)
    for profile in normalized.values():
        if not isinstance(profile, dict):
            continue
        teacher = str(profile.get("teacher") or "")
        if teacher:
            profile["teacher"] = canonical_ids.get(teacher, teacher)
        profile.setdefault("campus_id", "main")
    return normalized


def _task_level(task: dict[str, Any]) -> str:
    priority = str(task.get("priority") or "").lower()
    if priority in {"urgent", "critical", "s"}:
        return "S"
    if priority in {"high", "a", "高", "紧急"}:
        return "A"
    if priority in {"medium", "normal", "b", "中"}:
        return "B"
    return "C"


def _normalize_tasks(value: Any, canonical_ids: dict[str, str]) -> Any:
    normalized = deepcopy(value)
    tasks = normalized if isinstance(normalized, list) else normalized["tasks"]
    for task in tasks:
        if not isinstance(task, dict):
            continue
        for field in ("teacher", "sender_id", "assignee_userid"):
            user_id = str(task.get(field) or "")
            if user_id:
                task[field] = canonical_ids.get(user_id, user_id)
        assignee = str(
            task.get("assignee_userid")
            or task.get("teacher")
            or task.get("sender_id")
            or ""
        )
        if assignee:
            task["assignee_userid"] = canonical_ids.get(assignee, assignee)
        task.setdefault("campus_id", "main")
        task.setdefault("level", _task_level(task))
        task.setdefault(
            "title",
            str(
                task.get("content")
                or task.get("type")
                or task.get("student_name")
                or "Legacy task"
            ),
        )
    return normalized


def _manager_ids(data: dict[str, Any], canonical_ids: dict[str, str]) -> set[str]:
    managers: set[str] = set()
    for name in ("wecom_whitelist.json", "feishu_whitelist.json"):
        whitelist = data[name]
        manager_id = whitelist.get("manager_id")
        if manager_id:
            managers.add(str(manager_id))
        managers.update(str(item) for item in whitelist.get("manager_ids") or [])
        roles = whitelist.get("user_roles") or {}
        if isinstance(roles, dict):
            managers.update(
                str(user_id)
                for user_id, role in roles.items()
                if str(role) == "manager"
            )
    return {canonical_ids.get(user_id, user_id) for user_id in managers}


def _normalized_payloads(data: dict[str, Any]) -> dict[str, Any]:
    canonical_ids = _canonical_ids(data)
    payloads = {name: deepcopy(value) for name, value in data.items()}
    payloads["students.json"] = _normalize_students(
        data["students.json"],
        canonical_ids,
    )
    payloads["tasks.json"] = _normalize_tasks(
        data["tasks.json"],
        canonical_ids,
    )
    payloads["staff.json"] = {
        user_id: {"campus_ids": ["main"], "role": "manager"}
        for user_id in sorted(_manager_ids(data, canonical_ids))
    }
    return payloads


def _backup_files(
    names: tuple[str, ...] | list[str],
    source_dir: Path,
    backup_dir: Path,
) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = source_dir / name
        if source.is_file():
            shutil.copy2(source, backup_dir / name)


def migrate_tuoguan_data(
    source_dir: str | Path,
    target_dir: str | Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and import legacy data without modifying the source directory."""

    source = Path(source_dir).expanduser()
    target = Path(target_dir).expanduser()
    data, source_hashes = _validated_source(source)
    payloads = _normalized_payloads(data)
    counts = {name: _item_count(name, value) for name, value in data.items()}
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "source": str(source.resolve()),
        "target": str(target.resolve()),
        "counts": counts,
        "source_hashes": source_hashes,
    }
    if dry_run:
        return result

    timestamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S%f")
    source_backup = target / "backup" / "migration-source" / timestamp
    target_backup = target / "backup" / "pre-migration" / timestamp
    imported_names = list(data)
    target_names = list(payloads)

    try:
        _backup_files(imported_names, source, source_backup)
        existing = [
            name
            for name in target_names + ["migration_manifest.json"]
            if (target / name).is_file()
        ]
        if existing:
            _backup_files(existing, target, target_backup)
        target.mkdir(parents=True, exist_ok=True)
        for name, payload in payloads.items():
            atomic_json_write(target / name, payload)
        target_hashes = {name: _sha256(target / name) for name in target_names}
        manifest = {
            **result,
            "dry_run": False,
            "migrated_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "source_backup": str(source_backup),
            "target_backup": str(target_backup),
            "target_hashes": target_hashes,
        }
        atomic_json_write(target / "migration_manifest.json", manifest)
    except (OSError, TypeError, ValueError) as exc:
        raise MigrationError(f"Migration write failed: {exc}") from exc

    return manifest

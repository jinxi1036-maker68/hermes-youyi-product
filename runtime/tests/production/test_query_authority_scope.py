"""Release-gate coverage for authority-backed query visibility."""

from __future__ import annotations

from datetime import datetime, timedelta

from tuoguan_core.models import UserIdentity
from tuoguan_core.personnel_identity_authority import ACCESS_KEY, AUTHORITY_KEY
from tuoguan_core.operations_query import query_operations
from tuoguan_core.permissions import PermissionService
from tuoguan_core.store import TuoguanStore
from tuoguan_core.tool_service import TuoguanToolService


TENANT = "tenant-query"


def _authority_document() -> dict:
    return {
        "schema_version": 4,
        AUTHORITY_KEY: {"mode": "enforced", "tenant_id": TENANT, "activated_at": "2026-09-23"},
        ACCESS_KEY: {"pending": {}, "rejected": {}},
        "people": [
            {"person_id": "p-boss", "tenant_id": TENANT, "staff_user_id": "wx-boss", "display_name": "金总", "state": "active"},
            {"person_id": "p-manager", "tenant_id": TENANT, "staff_user_id": "wx-manager", "display_name": "店长", "state": "active"},
            {"person_id": "p-teacher-a", "tenant_id": TENANT, "staff_user_id": "wx-teacher-a", "display_name": "李老师", "state": "active"},
            {"person_id": "p-teacher-b", "tenant_id": TENANT, "staff_user_id": "wx-teacher-b", "display_name": "王老师", "state": "active"},
        ],
        "employments": [
            {"employment_id": "e-boss", "tenant_id": TENANT, "staff_user_id": "wx-boss", "role": "boss", "campus_id": "*", "managed_campus_ids": [], "state": "active", "effective_from": "2026-01-01", "effective_until": ""},
            {"employment_id": "e-manager", "tenant_id": TENANT, "staff_user_id": "wx-manager", "role": "manager", "campus_id": "main", "managed_campus_ids": ["main"], "state": "active", "effective_from": "2026-01-01", "effective_until": ""},
            {"employment_id": "e-teacher-a", "tenant_id": TENANT, "staff_user_id": "wx-teacher-a", "role": "teacher", "campus_id": "main", "managed_campus_ids": [], "state": "active", "effective_from": "2026-01-01", "effective_until": ""},
            {"employment_id": "e-teacher-b", "tenant_id": TENANT, "staff_user_id": "wx-teacher-b", "role": "teacher", "campus_id": "main", "managed_campus_ids": [], "state": "active", "effective_from": "2026-01-01", "effective_until": ""},
        ],
        "students": [],
        "service_relations": [],
        "service_records": [],
        "attentions": [],
        "work_links": [],
        "handovers": [],
        "change_requests": [],
        "identity_claim_reconciliations": [],
        "operations": {},
        "audit": [],
    }


def _store(tmp_path, monkeypatch) -> TuoguanStore:
    monkeypatch.setenv("HERMES_TENANT_ID", TENANT)
    store = TuoguanStore(tmp_path)
    store.write_json("personnel_service_governance_v1.json", _authority_document())
    # Deliberately wrong legacy scopes prove current query authorization no
    # longer depends on these fields after authority enforcement.
    store.write_json("staff.json", {
        "wx-manager": {"name": "旧店长", "role": "teacher", "campus_ids": ["wrong-campus"]},
        "wx-teacher-a": {"name": "旧李老师", "role": "manager", "campus_ids": ["wrong-campus"]},
        "legacy-only": {"name": "李老师", "role": "teacher", "campus_ids": ["main"]},
    })
    store.write_json("teacher_wecom_map.json", {"李老师": "legacy-only"})
    store.write_json("wecom_whitelist.json", {
        "allowed_users": ["wx-boss", "wx-manager", "wx-teacher-a", "wx-teacher-b", "legacy-only"],
        "user_roles": {"legacy-only": "teacher"},
    })
    store.write_json("students.json", {
        "张同学": {"teacher": "wx-teacher-a", "campus_id": "", "grade": "3"},
        "王同学": {"teacher": "wx-teacher-b", "campus_id": "main", "grade": "4"},
        "外部同学": {"teacher": "wx-teacher-a", "tenant_id": "other-tenant", "campus_id": "main"},
    })
    store.write_json("records.json", [])
    store.write_json("tasks.json", [
        {"id": "t-legacy-blank", "title": "旧任务", "assignee_userid": "wx-teacher-a", "status": "pending", "campus_id": "", "due_at": "2026-09-23T12:00:00"},
        {"id": "t-current", "title": "当前任务", "assignee_userid": "wx-teacher-b", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": "2026-09-23T13:00:00"},
        {"id": "t-foreign", "title": "其他机构任务", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": "other-tenant", "campus_id": "main", "due_at": "2026-09-23T14:00:00"},
    ])
    return store


def _identity(user_id: str, name: str, role: str) -> UserIdentity:
    return UserIdentity("wecom_callback", user_id, user_id, name, role, "approved")


def _service(store: TuoguanStore, identity: UserIdentity) -> TuoguanToolService:
    return TuoguanToolService(
        store,
        platform="wecom_callback",
        user_id=identity.canonical_user_id,
        identity=identity,
    )


def test_manager_student_query_uses_current_role_not_legacy_staff_scope(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    result = _service(store, _identity("wx-manager", "店长", "manager")).query_students(limit=100)

    assert result["ok"] is True
    assert result["data"]["total_count"] == 2
    assert {row["name"] for row in result["data"]["students"]} == {"张同学", "王同学"}


def test_query_scope_change_does_not_change_student_write_permission(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    manager = _identity("wx-manager", "店长", "manager")
    permissions = PermissionService(store)

    assert permissions.can_query_student(manager, "张同学") is True
    assert permissions.can_write_student_record(manager, "张同学") is False


def test_teacher_student_query_remains_person_scoped(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    result = _service(store, _identity("wx-teacher-a", "李老师", "teacher")).query_students(limit=100)

    assert result["ok"] is True
    assert {row["name"] for row in result["data"]["students"]} == {"张同学"}


def test_manager_task_query_sees_single_store_legacy_rows_and_rejects_foreign_tenant(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    result = _service(store, _identity("wx-manager", "店长", "manager")).query_tasks(limit=100, write_focus=False)

    assert result["ok"] is True
    assert result["data"]["effective_scope"] == "all"
    assert result["data"]["total_count"] == 2
    assert set(result["data"]["task_ids"]) == {"t-legacy-blank", "t-current"}


def test_teacher_task_query_remains_assignee_scoped_and_tenant_safe(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    result = _service(store, _identity("wx-teacher-a", "李老师", "teacher")).query_tasks(limit=100, write_focus=False)

    assert result["ok"] is True
    assert result["data"]["effective_scope"] == "mine"
    assert result["data"]["total_count"] == 1
    assert result["data"]["task_ids"] == ["t-legacy-blank"]


def test_manager_teacher_query_resolves_authoritative_teacher_not_legacy_alias_owner(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    service = _service(store, _identity("wx-manager", "店长", "manager"))

    resolved = service._query_teacher_identity_by_name("李老师")

    assert resolved is not None
    assert resolved.canonical_user_id == "wx-teacher-a"
    assert resolved.role == "teacher"
    assert service._query_manager_can_view_teacher("legacy-only") is False



def _due_on(day_offset: int, hour: int = 12) -> str:
    now = datetime.now().astimezone()
    target = now + timedelta(days=day_offset)
    return target.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def test_task_query_today_scope_excludes_history_future_and_missing_due_at(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    store.write_json("tasks.json", [
        {"id": "today-open", "title": "今天未完成", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": _due_on(0)},
        {"id": "yesterday-open", "title": "昨天未完成", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": _due_on(-1)},
        {"id": "tomorrow-open", "title": "明天未完成", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": _due_on(1)},
        {"id": "undated-open", "title": "无日期未完成", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": ""},
        {"id": "today-closed", "title": "今天已完成", "assignee_userid": "wx-teacher-a", "status": "completed", "tenant_id": TENANT, "campus_id": "main", "due_at": _due_on(0)},
    ])

    service = _service(store, _identity("wx-teacher-a", "李老师", "teacher"))
    result = service.query_tasks(
        scope="mine",
        status="open",
        date_scope="today",
        limit=100,
        write_focus=False,
    )

    assert result["ok"] is True
    assert result["data"]["date_scope"] == "today"
    assert result["data"]["task_ids"] == ["today-open"]
    assert result["data"]["total_count"] == 1
    assert result["data"]["rendered_text"].startswith("今天我的任务共 1 条")


def test_task_query_without_date_scope_keeps_all_visible_open_tasks(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    store.write_json("tasks.json", [
        {"id": "today-open", "title": "今天未完成", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": _due_on(0)},
        {"id": "yesterday-open", "title": "昨天未完成", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": _due_on(-1)},
        {"id": "tomorrow-open", "title": "明天未完成", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": _due_on(1)},
        {"id": "undated-open", "title": "无日期未完成", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": ""},
    ])

    result = _service(store, _identity("wx-teacher-a", "李老师", "teacher")).query_tasks(
        scope="mine",
        status="open",
        limit=100,
        write_focus=False,
    )

    assert result["ok"] is True
    assert result["data"]["date_scope"] == ""
    assert set(result["data"]["task_ids"]) == {
        "today-open",
        "yesterday-open",
        "tomorrow-open",
        "undated-open",
    }
    assert result["data"]["total_count"] == 4


def test_operations_open_tasks_means_today_open_tasks_not_all_backlog(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    store.write_json("tasks.json", [
        {"id": "today-open", "title": "今天未完成", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": _due_on(0)},
        {"id": "yesterday-open", "title": "历史积压", "assignee_userid": "wx-teacher-a", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": _due_on(-1)},
        {"id": "tomorrow-open", "title": "明天任务", "assignee_userid": "wx-teacher-b", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": _due_on(1)},
        {"id": "undated-open", "title": "长期待办", "assignee_userid": "wx-teacher-b", "status": "pending", "tenant_id": TENANT, "campus_id": "main", "due_at": ""},
    ])

    result = query_operations(
        store,
        identity=_identity("wx-boss", "金总", "boss"),
        query_type="open_tasks",
    )

    assert result["ok"] is True
    assert result["summary"]["open_task_count"] == 4
    assert result["summary"]["today_open_task_count"] == 1
    assert result["rendered_text"] == "今天未完成任务\n今日待办任务：1条"

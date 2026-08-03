"""Role-scoped access checks for tutoring-center data."""

from __future__ import annotations

from .models import UserIdentity
from .programs import can_access_program, item_program_id, student_program_ids
from .store import TuoguanStore


class PermissionService:
    def __init__(self, store: TuoguanStore) -> None:
        self.store = store

    def _student(self, student_name: str) -> dict:
        students = self.store.read_json("students.json", {})
        if not isinstance(students, dict):
            return {}
        student = students.get(student_name)
        return student if isinstance(student, dict) else {}

    def can_view_student(self, identity: UserIdentity, student_name: str) -> bool:
        if identity.approval_state != "approved":
            return False
        if identity.role == "boss":
            return True

        student = self._student(student_name)
        if not student:
            return False
        if not any(can_access_program(self.store, identity, item) for item in student_program_ids(student)):
            return False
        if identity.role == "teacher":
            if _is_summer_student(student):
                return True
            return str(student.get("teacher") or "") == identity.canonical_user_id
        if identity.role == "manager":
            if _is_summer_student(student):
                return True
            staff = self.store.read_json("staff.json", {})
            manager = staff.get(identity.canonical_user_id, {}) if isinstance(staff, dict) else {}
            campus_ids = set(manager.get("campus_ids") or []) if isinstance(manager, dict) else set()
            return str(student.get("campus_id") or "") in campus_ids
        return False

    def can_write_student_record(
        self,
        identity: UserIdentity,
        student_name: str,
    ) -> bool:
        return self.can_view_student(identity, student_name)

    @staticmethod
    def can_manage_tasks(identity: UserIdentity) -> bool:
        return (
            identity.approval_state == "approved"
            and identity.role in {"manager", "boss"}
        )


def _is_summer_student(student: dict) -> bool:
    if str(student.get("summer_status") or "") in {"active", "phone_pending"}:
        return True
    enrollments = student.get("program_enrollments")
    if not isinstance(enrollments, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("program_id") or "") in {"summer_2026", "2026_summer"}
        and str(item.get("status") or "") in {"active", "phone_pending"}
        for item in enrollments
    )

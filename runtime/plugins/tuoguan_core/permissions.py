"""Role-scoped access checks for tutoring-center data."""

from __future__ import annotations

from .models import UserIdentity
from .programs import can_access_program, item_program_id, student_program_ids
from .store import TuoguanStore
from .student_directory import find_student_candidates
from .tenant_context import current_tenant_id


class PermissionService:
    def __init__(self, store: TuoguanStore) -> None:
        self.store = store

    def _student(self, student_name: str) -> dict:
        candidates = find_student_candidates(self.store, student_name)
        # A display name with more than one repository identity is never a
        # valid authorisation target.  The caller must obtain a specific ID.
        if len(candidates) != 1:
            return {}
        profile = candidates[0].get("profile")
        return profile if isinstance(profile, dict) else {}

    def can_query_student(self, identity: UserIdentity, student_name: str) -> bool:
        """Authorize current single-institution student reads without legacy staff scope.

        Empty tenant fields are legacy in-workspace data and remain readable to
        an otherwise authorized current employee.  An explicit conflicting
        tenant never crosses the institution boundary.
        """

        if identity.approval_state != "approved":
            return False
        student = self._student(student_name)
        if not student:
            return False
        item_tenant = str(student.get("tenant_id") or "").strip()
        trusted_tenant = str(current_tenant_id() or "").strip()
        if item_tenant and trusted_tenant and item_tenant != trusted_tenant:
            return False
        if identity.role in {"boss", "manager"}:
            return True
        if identity.role == "teacher":
            if _is_summer_student(student):
                return True
            return str(student.get("teacher") or "") == identity.canonical_user_id
        return False

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

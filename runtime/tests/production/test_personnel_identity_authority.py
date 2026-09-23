"""Release-gate coverage for the personnel identity authority cutover."""

from __future__ import annotations

from copy import deepcopy

import pytest

from tuoguan_core.employee_identity import owner_user_id
from tuoguan_core.identity import IdentityService
from tuoguan_core.models import UserIdentity
from tuoguan_core.personnel_identity_authority import (
    AUTHORITY_KEY,
    ACCESS_KEY,
    build_legacy_identity_authority_bootstrap,
)
from tuoguan_core.governance_claims_tool_surface_v1 import PENDING_IDENTITY_ACTIVATION_SCHEMA
from tuoguan_core.personnel_service_governance_v1 import GovernanceError, PersonnelServiceGovernance
from tuoguan_core.staff_administration import offboard_staff
from tuoguan_core.store import TuoguanStore
from tuoguan_core.tool_service import TuoguanToolService


TENANT = "tenant-alpha"


def _authority_document() -> dict:
    people = [
        {"person_id": "p-boss", "tenant_id": TENANT, "staff_user_id": "wx-boss", "display_name": "负责人", "state": "active"},
        {"person_id": "p-manager", "tenant_id": TENANT, "staff_user_id": "wx-manager", "display_name": "店长甲", "state": "active"},
        {"person_id": "p-teacher", "tenant_id": TENANT, "staff_user_id": "wx-teacher", "display_name": "老师甲", "state": "active"},
        {"person_id": "p-left", "tenant_id": TENANT, "staff_user_id": "wx-left", "display_name": "历史老师", "state": "left"},
    ]
    employments = [
        {"employment_id": "e-boss", "tenant_id": TENANT, "staff_user_id": "wx-boss", "role": "boss", "campus_id": "*", "managed_campus_ids": [], "state": "active", "effective_from": "2026-01-01", "effective_until": ""},
        {"employment_id": "e-manager", "tenant_id": TENANT, "staff_user_id": "wx-manager", "role": "manager", "campus_id": "campus-a", "managed_campus_ids": ["campus-a"], "state": "active", "effective_from": "2026-01-01", "effective_until": ""},
        {"employment_id": "e-teacher", "tenant_id": TENANT, "staff_user_id": "wx-teacher", "role": "teacher", "campus_id": "campus-a", "managed_campus_ids": [], "state": "active", "effective_from": "2026-01-01", "effective_until": ""},
        {"employment_id": "e-left", "tenant_id": TENANT, "staff_user_id": "wx-left", "role": "teacher", "campus_id": "campus-a", "managed_campus_ids": [], "state": "closed", "effective_from": "2025-01-01", "effective_until": "2025-12-31"},
    ]
    return {
        "schema_version": 3,
        AUTHORITY_KEY: {"mode": "enforced", "tenant_id": TENANT, "activated_at": "2026-01-01"},
        ACCESS_KEY: {"pending": {"wx-pending": {"migrated_from": "legacy_pending"}}, "rejected": {"wx-rejected": {"migrated_from": "legacy_rejected"}}},
        "people": people,
        "employments": employments,
        "students": [], "service_relations": [], "service_records": [], "attentions": [],
        "work_links": [], "handovers": [], "change_requests": [], "operations": {}, "audit": [],
    }


def _store(tmp_path, monkeypatch) -> TuoguanStore:
    monkeypatch.setenv("HERMES_TENANT_ID", TENANT)
    store = TuoguanStore(tmp_path)
    # Deliberately conflicting legacy data proves it cannot alter an enforced
    # authority result. It remains on disk for history/display compatibility.
    store.write_json("wecom_whitelist.json", {
        "super_users": ["wx-teacher"],
        "allowed_users": ["wx-boss", "wx-manager", "wx-teacher", "wx-left"],
        "user_roles": {"wx-boss": "teacher", "wx-manager": "teacher", "wx-teacher": "boss", "wx-left": "teacher"},
    })
    store.write_json("staff.json", {
        "wx-boss": {"business_name": "错误旧显示", "status": "active", "role": "teacher"},
        "wx-manager": {"business_name": "旧店长", "status": "active", "role": "teacher"},
        "wx-teacher": {"business_name": "旧老师", "status": "active", "role": "boss"},
        "wx-left": {"business_name": "旧历史老师", "status": "active", "role": "teacher"},
    })
    store.write_json("personnel_service_governance_v1.json", _authority_document())
    return store


def test_enforced_authority_uses_verified_userid_not_legacy_name_or_role(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    identities = IdentityService(store)

    boss = identities.resolve("wecom_callback", "wx-boss", user_name="任意昵称", tenant_id=TENANT)
    teacher = identities.resolve("wecom_callback", "wx-teacher", user_name="负责人", tenant_id=TENANT)
    left = identities.resolve("wecom_callback", "wx-left", tenant_id=TENANT)

    assert (boss.canonical_user_id, boss.role, boss.approval_state, boss.person_name) == ("wx-boss", "boss", "approved", "负责人")
    assert (teacher.canonical_user_id, teacher.role, teacher.approval_state, teacher.person_name) == ("wx-teacher", "teacher", "approved", "老师甲")
    assert (left.canonical_user_id, left.role, left.approval_state) == ("wx-left", "unknown", "left")
    assert owner_user_id(store) == "wx-boss"


def test_lifecycle_and_role_changes_take_effect_on_the_next_wecom_resolution(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    governance = PersonnelServiceGovernance(store)
    boss = IdentityService(store).resolve("wecom_callback", "wx-boss", tenant_id=TENANT)

    handover = governance.open_handover(
        identity=boss, tenant_id=TENANT, outgoing_user_id="wx-teacher", kind="emergency", operation_id="suspend-teacher",
    )
    assert handover["access_revoked_immediately"] is True
    suspended = IdentityService(store).resolve("wecom_callback", "wx-teacher", tenant_id=TENANT)
    assert (suspended.role, suspended.approval_state) == ("unknown", "suspended")

    governance.reactivate_person(identity=boss, tenant_id=TENANT, staff_user_id="wx-teacher", operation_id="restore-teacher")
    restored = IdentityService(store).resolve("wecom_callback", "wx-teacher", tenant_id=TENANT)
    assert (restored.role, restored.approval_state) == ("teacher", "approved")

    governance.change_employment_role(
        identity=boss, tenant_id=TENANT, employment_id="e-teacher", target_role="manager", operation_id="promote-teacher",
    )
    promoted = IdentityService(store).resolve("wecom_callback", "wx-teacher", tenant_id=TENANT)
    assert (promoted.canonical_user_id, promoted.role, promoted.approval_state) == ("wx-teacher", "manager", "approved")


def test_unknown_pending_is_recorded_in_authority_not_legacy_whitelist(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    before = deepcopy(store.read_json("wecom_whitelist.json", {}))
    unknown = IdentityService(store).resolve("wecom_callback", "wx-new", user_name="未确认", chat_id="dm", tenant_id=TENANT)
    document = store.read_json("personnel_service_governance_v1.json", {})

    assert (unknown.canonical_user_id, unknown.role, unknown.approval_state) == ("wx-new", "unknown", "pending")
    assert "wx-new" in document[ACCESS_KEY]["pending"]
    assert store.read_json("wecom_whitelist.json", {}) == before


def test_pending_identity_activation_uses_unique_authoritative_campus_when_omitted(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    governance = PersonnelServiceGovernance(store)
    boss = IdentityService(store).resolve("wecom_callback", "wx-boss", tenant_id=TENANT)

    result = governance.activate_confirmed_pending_identity(
        identity=boss,
        tenant_id=TENANT,
        staff_user_id="wx-pending",
        person_name="李老师",
        role="teacher",
        campus_id="",
        operation_id="activate-pending-single-campus",
    )

    assert result["employment"]["campus_id"] == "campus-a"
    activated = IdentityService(store).resolve("wecom_callback", "wx-pending", tenant_id=TENANT)
    assert (activated.person_name, activated.role, activated.approval_state) == ("李老师", "teacher", "approved")


def test_pending_identity_activation_uses_manager_managed_campus_when_scalar_campuses_are_blank(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    document = store.read_json("personnel_service_governance_v1.json", {})
    for employment in document["employments"]:
        if str(employment.get("state") or "") == "active" and not employment.get("effective_until"):
            employment["campus_id"] = ""
        if employment.get("staff_user_id") == "wx-manager":
            employment["managed_campus_ids"] = ["main"]
    store.write_json("personnel_service_governance_v1.json", document)
    governance = PersonnelServiceGovernance(store)
    boss = IdentityService(store).resolve("wecom_callback", "wx-boss", tenant_id=TENANT)

    result = governance.activate_confirmed_pending_identity(
        identity=boss,
        tenant_id=TENANT,
        staff_user_id="wx-pending",
        person_name="李老师",
        role="teacher",
        campus_id="",
        operation_id="activate-pending-manager-campus",
    )

    assert result["employment"]["campus_id"] == "main"
    activated = IdentityService(store).resolve("wecom_callback", "wx-pending", tenant_id=TENANT)
    assert (activated.person_name, activated.role, activated.approval_state) == ("李老师", "teacher", "approved")


def test_pending_identity_activation_is_ambiguous_for_multiple_manager_managed_campuses(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    document = store.read_json("personnel_service_governance_v1.json", {})
    for employment in document["employments"]:
        if str(employment.get("state") or "") == "active" and not employment.get("effective_until"):
            employment["campus_id"] = ""
        if employment.get("staff_user_id") == "wx-manager":
            employment["managed_campus_ids"] = ["main"]
    document["people"].append({
        "person_id": "p-manager-b",
        "tenant_id": TENANT,
        "staff_user_id": "wx-manager-b",
        "display_name": "店长乙",
        "state": "active",
    })
    document["employments"].append({
        "employment_id": "e-manager-b",
        "tenant_id": TENANT,
        "staff_user_id": "wx-manager-b",
        "role": "manager",
        "campus_id": "",
        "managed_campus_ids": ["campus-b"],
        "state": "active",
        "effective_from": "2026-01-01",
        "effective_until": "",
    })
    store.write_json("personnel_service_governance_v1.json", document)
    governance = PersonnelServiceGovernance(store)
    boss = IdentityService(store).resolve("wecom_callback", "wx-boss", tenant_id=TENANT)

    with pytest.raises(GovernanceError, match="pending_identity_campus_ambiguous"):
        governance.activate_confirmed_pending_identity(
            identity=boss,
            tenant_id=TENANT,
            staff_user_id="wx-pending",
            person_name="李老师",
            role="teacher",
            campus_id="",
            operation_id="activate-pending-manager-campus-ambiguous",
        )


def test_pending_identity_activation_rejects_explicit_campus_outside_trusted_scope(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    document = store.read_json("personnel_service_governance_v1.json", {})
    for employment in document["employments"]:
        if str(employment.get("state") or "") == "active" and not employment.get("effective_until"):
            employment["campus_id"] = ""
        if employment.get("staff_user_id") == "wx-manager":
            employment["managed_campus_ids"] = ["main"]
    store.write_json("personnel_service_governance_v1.json", document)
    governance = PersonnelServiceGovernance(store)
    boss = IdentityService(store).resolve("wecom_callback", "wx-boss", tenant_id=TENANT)

    with pytest.raises(GovernanceError, match="pending_identity_campus_not_current"):
        governance.activate_confirmed_pending_identity(
            identity=boss,
            tenant_id=TENANT,
            staff_user_id="wx-pending",
            person_name="李老师",
            role="teacher",
            campus_id="campus-not-trusted",
            operation_id="activate-pending-manager-campus-rejected",
        )


def test_pending_identity_activation_requires_choice_only_when_current_campus_is_ambiguous(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    document = store.read_json("personnel_service_governance_v1.json", {})
    document["people"].append({
        "person_id": "p-campus-b",
        "tenant_id": TENANT,
        "staff_user_id": "wx-campus-b",
        "display_name": "老师乙",
        "state": "active",
    })
    document["employments"].append({
        "employment_id": "e-campus-b",
        "tenant_id": TENANT,
        "staff_user_id": "wx-campus-b",
        "role": "teacher",
        "campus_id": "campus-b",
        "managed_campus_ids": [],
        "state": "active",
        "effective_from": "2026-01-01",
        "effective_until": "",
    })
    store.write_json("personnel_service_governance_v1.json", document)
    governance = PersonnelServiceGovernance(store)
    boss = IdentityService(store).resolve("wecom_callback", "wx-boss", tenant_id=TENANT)

    with pytest.raises(GovernanceError, match="pending_identity_campus_ambiguous"):
        governance.activate_confirmed_pending_identity(
            identity=boss,
            tenant_id=TENANT,
            staff_user_id="wx-pending",
            person_name="李老师",
            role="teacher",
            campus_id="",
            operation_id="activate-pending-ambiguous-campus",
        )


def test_pending_identity_tool_schema_does_not_force_owner_to_restate_single_campus() -> None:
    required = set(PENDING_IDENTITY_ACTIVATION_SCHEMA["parameters"]["required"])

    assert required == {"staff_user_id", "person_name", "role"}
    assert "campus_id" in PENDING_IDENTITY_ACTIVATION_SCHEMA["parameters"]["properties"]


def test_non_approved_identity_never_inherits_a_legacy_staff_display_name(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    # These old rows intentionally look like real staff.  Once the authority
    # is enforced they remain historical reference, not a source of a current
    # employee presentation for a pending/rejected/left transport identity.
    staff = store.read_json("staff.json", {})
    staff.update({
        "wx-pending": {"business_name": "历史老师甲", "status": "active", "role": "teacher"},
        "wx-rejected": {"business_name": "历史老师乙", "status": "active", "role": "teacher"},
    })
    store.write_json("staff.json", staff)
    identities = IdentityService(store)

    pending = identities.resolve("wecom_callback", "wx-pending", tenant_id=TENANT)
    rejected = identities.resolve("wecom_callback", "wx-rejected", tenant_id=TENANT)
    left = identities.resolve("wecom_callback", "wx-left", tenant_id=TENANT)

    assert (pending.person_name, pending.role, pending.approval_state) == ("", "unknown", "pending")
    assert (rejected.person_name, rejected.role, rejected.approval_state) == ("", "unknown", "rejected")
    assert (left.person_name, left.role, left.approval_state) == ("", "unknown", "left")


def test_unapproved_identity_message_requires_boss_confirmation_not_manager_review(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    pending = IdentityService(store).resolve("wecom_callback", "wx-pending", tenant_id=TENANT)
    result = TuoguanToolService(
        store,
        platform="wecom_callback",
        user_id="wx-pending",
        identity=pending,
    ).read_agenda_work_facts()

    assert result["ok"] is False
    assert result["error"] == "account_not_approved"
    assert "身份确认" in result["message"]
    assert "老板" in result["message"]
    assert "店长" not in result["message"]


def test_non_wecom_sender_cannot_inherit_a_wecom_role_from_matching_text(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    result = IdentityService(store).resolve("feishu", "wx-boss", tenant_id=TENANT)

    assert (result.canonical_user_id, result.role, result.approval_state) == (
        "wx-boss", "unknown", "unmapped_platform_identity",
    )


def test_invalid_two_boss_authority_fails_closed_without_legacy_fallback(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    invalid = _authority_document()
    invalid["employments"].append({
        "employment_id": "e-second-boss", "tenant_id": TENANT, "staff_user_id": "wx-manager", "role": "boss",
        "campus_id": "*", "managed_campus_ids": [], "state": "active", "effective_from": "2026-01-02", "effective_until": "",
    })
    store.write_json("personnel_service_governance_v1.json", invalid)
    result = IdentityService(store).resolve("wecom_callback", "wx-boss", tenant_id=TENANT)

    assert (result.role, result.approval_state) == ("unknown", "authority_invalid")
    assert owner_user_id(store) == ""


def test_legacy_offboard_tool_cannot_recreate_a_second_identity_authority(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    boss = IdentityService(store).resolve("wecom_callback", "wx-boss", tenant_id=TENANT)
    before_directory = deepcopy(store.read_json("wecom_whitelist.json", {}))
    result = offboard_staff(store, identity=boss, target_user_id="wx-teacher")

    assert result["ok"] is False
    assert result["error"] == "personnel_identity_authority_requires_governance_handover"
    assert store.read_json("wecom_whitelist.json", {}) == before_directory


def test_legacy_bootstrap_is_counted_and_blocks_lifecycle_conflicts() -> None:
    document, report = build_legacy_identity_authority_bootstrap(
        tenant_id=TENANT,
        whitelist={
            "super_users": ["wx-boss"],
            "allowed_users": ["wx-boss", "wx-teacher"],
            "user_roles": {"wx-boss": "super_admin", "wx-teacher": "teacher"},
            "pending_users": ["wx-pending"],
            "rejected_users": ["wx-rejected"],
        },
        staff={
            "wx-boss": {"status": "active", "business_name": "负责人"},
            "wx-teacher": {"status": "active", "business_name": "老师甲"},
        },
    )
    assert report["safe_to_apply"] is True
    assert report["active_boss_count"] == 1
    assert document[AUTHORITY_KEY]["mode"] == "enforced"

    _, conflict = build_legacy_identity_authority_bootstrap(
        tenant_id=TENANT,
        whitelist={"super_users": ["wx-boss"], "allowed_users": ["wx-boss"], "user_roles": {"wx-boss": "boss"}},
        staff={"wx-boss": {"status": "left"}},
    )
    assert conflict["safe_to_apply"] is False
    assert any(item.startswith("legacy_approval_lifecycle_conflict:") for item in conflict["blockers"])

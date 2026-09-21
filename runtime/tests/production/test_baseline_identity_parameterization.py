"""Regression coverage for PII-free, trusted baseline parameterization."""

from tuoguan_core.employee_identity import owner_display_names, owner_user_id
from tuoguan_core.p4_8_account_admin import _is_owner
from tuoguan_core.runtime_foundation import _sanitize_external_reply
from tuoguan_core.staff_directory import query_staff_directory
from tuoguan_core.store import TuoguanStore
from tuoguan_core.tenant_context import institution_display_names
from tuoguan_core.tool_service import _legacy_institution_rollout_target_allowed


def test_trusted_super_user_is_the_owner_without_a_display_name(tmp_path) -> None:
    store = TuoguanStore(tmp_path)
    store.write_json("wecom_whitelist.json", {"super_users": ["owner-001"]})
    store.write_json("teacher_wecom_map.json", {"arbitrary-alias": "other-002"})

    assert owner_user_id(store) == "owner-001"
    assert _is_owner(store, "owner-001") is True
    assert _is_owner(store, "other-002") is False


def test_canonical_active_boss_is_a_legacy_fallback_but_aliases_are_not(tmp_path) -> None:
    store = TuoguanStore(tmp_path)
    store.write_json("teacher_wecom_map.json", {"arbitrary-alias": "other-002"})
    assert owner_user_id(store) == ""

    store.write_json(
        "staff.json",
        {"owner-001": {"role": "boss", "status": "active", "business_name": "负责人"}},
    )
    assert owner_user_id(store) == "owner-001"


def test_legacy_rollout_requires_explicit_server_owned_policy(tmp_path) -> None:
    store = TuoguanStore(tmp_path)
    assert _legacy_institution_rollout_target_allowed(store, "teacher-002") is False

    store.write_json("institution_rollout_policy.json", {"allowed_user_ids": ["teacher-002"]})
    assert _legacy_institution_rollout_target_allowed(store, "teacher-002") is True
    assert _legacy_institution_rollout_target_allowed(store, "teacher-003") is False


def test_institution_labels_and_owner_salutation_come_from_workspace_facts(tmp_path) -> None:
    store = TuoguanStore(tmp_path)
    store.write_json("institution_operating_model.json", {"institution_name": "机构甲"})
    store.write_json("wecom_whitelist.json", {
        "super_users": ["owner-001"],
        "allowed_users": ["teacher-001"],
        "user_roles": {"owner-001": "boss", "teacher-001": "teacher"},
    })
    store.write_json("staff.json", {
        "owner-001": {"role": "boss", "status": "active", "business_name": "机构负责人甲"},
        "teacher-001": {"role": "teacher", "status": "active"},
    })
    store.write_json("teacher_wecom_map.json", {
        "机构负责人甲": "owner-001",
        "机构甲托管老师甲": "teacher-001",
    })

    assert institution_display_names(store) == ("机构甲", "机构甲托管")
    rows = query_staff_directory(store, query="机构甲托管老师甲", role="teacher")
    assert rows["result_count"] == 1
    assert rows["staff"][0]["user_id"] == "teacher-001"

    reply = _sanitize_external_reply(
        "您好，机构负责人甲，请放心。",
        actor_role="teacher",
        actor_name="老师甲",
        protected_owner_names=owner_display_names(store),
    )
    assert reply.startswith("您好，老师甲")

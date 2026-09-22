"""Release-gate regression for the public trusted-turn contract.

This test intentionally exercises only server-attested channel facts.  It
does not ask a model to choose a Tool: its purpose is to prove that a Tool
service cannot take its tenant, actor or role from model-supplied arguments.
"""

from tuoguan_core.runtime_contract import (
    bind_trusted_turn,
    clear_all_trusted_turns,
    current_trusted_turn,
)
from tuoguan_core.store import TuoguanStore
from tuoguan_core.tools import _service


def _enforced_identity_authority(tenant_id: str) -> dict:
    return {
        "schema_version": 3,
        "runtime_identity_authority": {"mode": "enforced", "tenant_id": tenant_id},
        "identity_access": {"pending": {}, "rejected": {}},
        "people": [
            {"person_id": "p-owner", "tenant_id": tenant_id, "staff_user_id": "owner-001", "display_name": "机构负责人", "state": "active"},
            {"person_id": "p-teacher", "tenant_id": tenant_id, "staff_user_id": "teacher-001", "display_name": "老师甲", "state": "active"},
        ],
        "employments": [
            {"employment_id": "e-owner", "tenant_id": tenant_id, "staff_user_id": "owner-001", "role": "boss", "campus_id": "*", "state": "active", "effective_until": ""},
            {"employment_id": "e-teacher", "tenant_id": tenant_id, "staff_user_id": "teacher-001", "role": "teacher", "campus_id": "campus-a", "state": "active", "effective_until": ""},
        ],
        "students": [], "service_relations": [], "service_records": [], "attentions": [], "work_links": [],
        "handovers": [], "change_requests": [], "operations": {}, "audit": [],
    }


def test_public_trusted_turn_rejects_actor_or_tenant_rebinding(monkeypatch, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    data = workspace / "data"
    monkeypatch.setenv("XIAOYOU_INSTITUTION_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_TUOGUAN_DATA_DIR", str(data))
    monkeypatch.setenv("HERMES_TENANT_ID", "tenant-a")
    store = TuoguanStore(data)
    store.write_json("wecom_whitelist.json", {"super_users": ["owner-001"]})
    store.write_json("staff.json", {
        "owner-001": {
            "role": "boss",
            "status": "active",
            "business_name": "机构负责人",
        },
    })

    clear_all_trusted_turns()
    try:
        bound = bind_trusted_turn(
            platform="wecom_callback",
            actor_user_id="owner-001",
            session_id="session-001",
            turn_id="turn-001",
            message_id="message-001",
            chat_id="dm-owner-001",
            tenant_id="tenant-a",
            source="release_gate_test",
        )
        assert bound is not None
        assert bound.actor_user_id == "owner-001"
        assert bound.tenant_id == "tenant-a"
        assert bound.identity.role == "boss"

        # A second caller cannot replace either protected part of the
        # authenticated binding under the same Hermes turn correlation.
        assert bind_trusted_turn(
            platform="wecom_callback",
            actor_user_id="attacker-002",
            session_id="session-001",
            turn_id="turn-001",
            message_id="message-001",
            chat_id="dm-owner-001",
            tenant_id="tenant-a",
            source="forged_actor_test",
        ) is None
        assert bind_trusted_turn(
            platform="wecom_callback",
            actor_user_id="owner-001",
            session_id="session-001",
            turn_id="turn-001",
            message_id="message-001",
            chat_id="dm-owner-001",
            tenant_id="tenant-b",
            source="forged_tenant_test",
        ) is None

        # Tool construction disregards an untrusted model envelope and uses
        # the immutable server-bound record instead.
        service = _service({"user_id": "attacker-002", "tenant_id": "tenant-b", "role": "boss"})
        assert service is not None
        assert service.user_id == "owner-001"
        assert service.identity.role == "boss"
        assert current_trusted_turn() is not None
        assert current_trusted_turn().tenant_id == "tenant-a"
    finally:
        clear_all_trusted_turns()

    assert _service({"user_id": "owner-001"}) is None


def test_new_hermes_session_re_resolves_the_same_authoritative_wecom_identity(monkeypatch, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    data = workspace / "data"
    monkeypatch.setenv("XIAOYOU_INSTITUTION_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_TUOGUAN_DATA_DIR", str(data))
    monkeypatch.setenv("HERMES_TENANT_ID", "tenant-a")
    store = TuoguanStore(data)
    store.write_json("personnel_service_governance_v1.json", _enforced_identity_authority("tenant-a"))

    clear_all_trusted_turns()
    try:
        first = bind_trusted_turn(
            platform="wecom_callback", actor_user_id="teacher-001", session_id="session-old", turn_id="turn-old",
            message_id="message-old", chat_id="dm-teacher-001", tenant_id="tenant-a", source="identity_authority_test",
        )
        assert first is not None and first.identity.role == "teacher"

        # Simulates loss of the process-local Hermes session binding. The next
        # verified callback must recover role and continuity from Workspace,
        # not prior chat text or the old session id.
        clear_all_trusted_turns()
        second = bind_trusted_turn(
            platform="wecom_callback", actor_user_id="teacher-001", session_id="session-new", turn_id="turn-new",
            message_id="message-new", chat_id="dm-teacher-001", tenant_id="tenant-a", source="identity_authority_test",
        )
        assert second is not None
        assert second.identity.canonical_user_id == "teacher-001"
        assert second.identity.role == "teacher"
        assert second.tenant_id == "tenant-a"
        assert second.continuity_key == first.continuity_key
    finally:
        clear_all_trusted_turns()

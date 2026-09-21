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

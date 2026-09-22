"""Release-gate coverage for direct execution of clear authorised commands."""

from __future__ import annotations

import json

from tuoguan_core.governance_claims_v1 import CLAIMS_FILE, GovernanceClaimService
from tuoguan_core.governance_claims_tool_surface_v1 import build_governance_claim_tools
from tuoguan_core.identity import IdentityService
from tuoguan_core.personnel_identity_authority import ACCESS_KEY, AUTHORITY_KEY, record_pending_runtime_identity
from tuoguan_core.personnel_service_governance_v1 import GovernanceError, PersonnelServiceGovernance
from tuoguan_core.store import TuoguanStore
from tuoguan_core.tool_service import TuoguanToolService


TENANT = "tenant-authorised-command"


def _authority_document() -> dict:
    return {
        "schema_version": 3,
        AUTHORITY_KEY: {"mode": "enforced", "tenant_id": TENANT, "activated_at": "2026-01-01"},
        ACCESS_KEY: {"pending": {}, "rejected": {}},
        "people": [{
            "person_id": "person-owner", "tenant_id": TENANT, "staff_user_id": "wx-owner",
            "display_name": "机构负责人", "state": "active",
        }],
        "employments": [{
            "employment_id": "employment-owner", "tenant_id": TENANT, "staff_user_id": "wx-owner",
            "role": "boss", "campus_id": "*", "managed_campus_ids": [], "state": "active",
            "effective_from": "2026-01-01", "effective_until": "",
        }],
        "students": [], "service_relations": [], "service_records": [], "attentions": [],
        "work_links": [], "handovers": [], "change_requests": [], "operations": {}, "audit": [],
    }


def _service(tmp_path, monkeypatch) -> tuple[TuoguanStore, GovernanceClaimService]:
    monkeypatch.setenv("HERMES_TENANT_ID", TENANT)
    store = TuoguanStore(tmp_path / "authority")
    store.write_json("personnel_service_governance_v1.json", _authority_document())
    legacy = tmp_path / "legacy-reference"
    legacy.mkdir()
    return store, GovernanceClaimService(
        governance=PersonnelServiceGovernance(store),
        legacy_data_dir=legacy,
    )


def test_boss_explicit_complete_person_assignment_is_one_atomic_confirmed_write(tmp_path, monkeypatch) -> None:
    store, claims = _service(tmp_path, monkeypatch)
    boss = IdentityService(store).resolve("wecom_callback", "wx-owner", tenant_id=TENANT)

    result = claims.record_confirmed_claim(
        identity=boss,
        tenant_id=TENANT,
        claim_type="person_assignment",
        reference_ids=[],
        payload={
            "staff_user_id": "wx-teacher-new",
            "person_name": "示例老师",
            "role": "teacher",
            "campus_id": "campus-a",
        },
        operation_id="authorised-command-1",
    )

    assert result["recorded"] is True
    assert result["claim"]["state"] == "confirmed"
    assert result["execution_receipt"]["status"] == "completed"
    assert result["execution_receipt"]["writeback_verified"] is True
    resolved = IdentityService(store).resolve("wecom_callback", "wx-teacher-new", tenant_id=TENANT)
    assert (resolved.canonical_user_id, resolved.person_name, resolved.role, resolved.approval_state) == (
        "wx-teacher-new", "示例老师", "teacher", "approved",
    )
    repeated = claims.record_confirmed_claim(
        identity=boss,
        tenant_id=TENANT,
        claim_type="person_assignment",
        reference_ids=[],
        payload={
            "staff_user_id": "wx-teacher-new",
            "person_name": "示例老师",
            "role": "teacher",
            "campus_id": "campus-a",
        },
        operation_id="authorised-command-1",
    )
    assert repeated["recorded"] is True
    document = store.read_json(CLAIMS_FILE, {})
    assert len(document["claims"]) == 1
    assert len(document["audit"]) == 2  # submit plus confirm, one public Tool action
    authority = store.read_json("personnel_service_governance_v1.json", {})
    assert [row["staff_user_id"] for row in authority["people"]].count("wx-teacher-new") == 1


def test_incomplete_or_non_boss_person_assignment_cannot_be_silently_activated(tmp_path, monkeypatch) -> None:
    store, claims = _service(tmp_path, monkeypatch)
    boss = IdentityService(store).resolve("wecom_callback", "wx-owner", tenant_id=TENANT)

    try:
        claims.record_confirmed_claim(
            identity=boss,
            tenant_id=TENANT,
            claim_type="person_assignment",
            reference_ids=[],
            payload={"staff_user_id": "wx-ambiguous", "role": "teacher"},
            operation_id="authorised-command-incomplete",
        )
    except ValueError as exc:
        assert "claim_missing_required_facts:campus_id" in str(exc)
    else:
        raise AssertionError("missing campus must require clarification rather than write a guessed assignment")

    document = store.read_json("personnel_service_governance_v1.json", {})
    assert all(row.get("staff_user_id") != "wx-ambiguous" for row in document["people"])


def test_boss_can_atomically_activate_one_server_recorded_pending_wecom_identity(tmp_path, monkeypatch) -> None:
    store, claims = _service(tmp_path, monkeypatch)
    boss = IdentityService(store).resolve("wecom_callback", "wx-owner", tenant_id=TENANT)
    record_pending_runtime_identity(
        store,
        tenant_id=TENANT,
        user_id="wx-pending-teacher",
        platform="wecom_callback",
        user_name="临时显示名",
        chat_id="chat-pending-teacher",
    )

    result = claims.activate_confirmed_pending_identity(
        identity=boss,
        tenant_id=TENANT,
        staff_user_id="wx-pending-teacher",
        person_name="李老师",
        role="teacher",
        campus_id="campus-a",
        operation_id="pending-identity-confirmation-1",
    )

    assert result["recorded"] is True
    assert result["execution_receipt"]["status"] == "completed"
    assert result["execution_receipt"]["writeback_verified"] is True
    resolved = IdentityService(store).resolve("wecom_callback", "wx-pending-teacher", tenant_id=TENANT)
    assert (resolved.canonical_user_id, resolved.person_name, resolved.role, resolved.approval_state) == (
        "wx-pending-teacher", "李老师", "teacher", "approved",
    )
    authority = store.read_json("personnel_service_governance_v1.json", {})
    assert "wx-pending-teacher" not in authority[ACCESS_KEY]["pending"]
    assert [row["staff_user_id"] for row in authority["people"]].count("wx-pending-teacher") == 1
    assert [row["staff_user_id"] for row in authority["employments"]].count("wx-pending-teacher") == 1

    repeated = claims.activate_confirmed_pending_identity(
        identity=boss,
        tenant_id=TENANT,
        staff_user_id="wx-pending-teacher",
        person_name="李老师",
        role="teacher",
        campus_id="campus-a",
        operation_id="pending-identity-confirmation-1",
    )
    assert repeated["recorded"] is True
    replayed = store.read_json("personnel_service_governance_v1.json", {})
    assert [row["staff_user_id"] for row in replayed["people"]].count("wx-pending-teacher") == 1


def test_direct_confirmed_failure_is_terminal_audit_not_an_agenda_confirmation(tmp_path, monkeypatch) -> None:
    store, claims = _service(tmp_path, monkeypatch)
    boss = IdentityService(store).resolve("wecom_callback", "wx-owner", tenant_id=TENANT)

    # ``active`` here incorrectly tries to restore a person that does not
    # exist.  The protected write must preserve the domain error, leave the
    # authority unchanged and never create an awaiting-confirmation Claim.
    result = claims.record_confirmed_claim(
        identity=boss,
        tenant_id=TENANT,
        claim_type="person_status",
        reference_ids=[],
        payload={"staff_user_id": "wx-no-person", "campus_id": "campus-a", "state": "active"},
        operation_id="direct-failure-no-reask",
    )

    assert result["recorded"] is False
    assert result["execution_failed"] is True
    assert result["error"] == "person_not_suspended"
    claim = result["claim"]
    assert claim["state"] == "execution_failed"
    assert claim["requires_human_confirmation"] is False
    assert claim["execution_error_code"] == "person_not_suspended"
    assert claims.query_claims(identity=boss, tenant_id=TENANT) == []
    authority = store.read_json("personnel_service_governance_v1.json", {})
    assert all(row.get("staff_user_id") != "wx-no-person" for row in authority["people"])

    # Retrying the same direct command must report the original terminal
    # domain failure, not revive a stale awaiting-confirmation snapshot.
    replay = claims.record_confirmed_claim(
        identity=boss,
        tenant_id=TENANT,
        claim_type="person_status",
        reference_ids=[],
        payload={"staff_user_id": "wx-no-person", "campus_id": "campus-a", "state": "active"},
        operation_id="direct-failure-no-reask",
    )
    assert replay["execution_failed"] is True
    assert replay["error"] == "person_not_suspended"
    assert replay["claim"]["state"] == "execution_failed"


def test_pending_identity_tool_uses_one_protected_receipted_operation(tmp_path, monkeypatch) -> None:
    store, _claims = _service(tmp_path, monkeypatch)
    boss = IdentityService(store).resolve("wecom_callback", "wx-owner", tenant_id=TENANT)
    record_pending_runtime_identity(
        store, tenant_id=TENANT, user_id="wx-pending-tool", platform="wecom_callback", user_name="临时", chat_id="chat-pending-tool",
    )
    legacy = tmp_path / "legacy-reference"
    monkeypatch.setenv("XIAOYOU_GOVERNANCE_LEGACY_REFERENCE_DIR", str(legacy))
    monkeypatch.setattr("tuoguan_core.write_guard.guard_enabled", lambda _path: False)

    service = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="wx-owner",
        user_name="机构负责人",
        chat_id="chat-owner",
        identity=boss,
    )

    class Turn:
        message_id = "message-pending-tool"
        tenant_id = TENANT

    tools = dict(
        (name, handler)
        for name, _schema, handler in build_governance_claim_tools(
            service_provider=lambda: service,
            activate_turn=lambda **_kwargs: None,
            current_turn=lambda: Turn(),
            tool_result=lambda value: json.dumps(value, ensure_ascii=False),
        )
    )
    result = json.loads(tools["tuoguan_activate_confirmed_pending_identity"]({
        "staff_user_id": "wx-pending-tool",
        "person_name": "李老师",
        "role": "teacher",
        "campus_id": "campus-a",
    }))

    assert result["ok"] is True
    assert result["execution_receipt"]["status"] == "completed"
    assert result["execution_receipt"]["writeback_verified"] is True
    resolved = IdentityService(store).resolve("wecom_callback", "wx-pending-tool", tenant_id=TENANT)
    assert (resolved.person_name, resolved.role, resolved.approval_state) == ("李老师", "teacher", "approved")


def test_capability_receipt_preserves_stable_governance_error(tmp_path, monkeypatch) -> None:
    store, _claims = _service(tmp_path, monkeypatch)
    boss = IdentityService(store).resolve("wecom_callback", "wx-owner", tenant_id=TENANT)
    monkeypatch.setattr("tuoguan_core.write_guard.guard_enabled", lambda _path: False)
    service = TuoguanToolService(
        store=store, platform="wecom_callback", user_id="wx-owner", user_name="机构负责人", chat_id="chat-owner", identity=boss,
    )

    result = service.execute_capability_write(
        operation_id="stable-domain-error-receipt",
        operation="governance_claim_record",
        execute=lambda: (_ for _ in ()).throw(GovernanceError("person_not_suspended")),
    )

    assert result["ok"] is False
    assert result["error"] == "person_not_suspended"
    assert result["execution_receipt"]["idempotency_result"] == "not_applied"
    assert result["execution_receipt"]["writeback_verified"] is False


def test_model_contract_distinguishes_direct_execution_from_required_clarification() -> None:
    from tuoguan_core import _xiaoyou_core_skill_context
    from tuoguan_core.governance_claims_tool_surface_v1 import PERSON_ASSIGNMENT_CLAIM_SCHEMA
    from tuoguan_core.models import UserIdentity

    context = _xiaoyou_core_skill_context(
        identity=UserIdentity("wecom_callback", "wx-owner", "wx-owner", "机构负责人", "boss", "approved"),
    )
    description = PERSON_ASSIGNMENT_CLAIM_SCHEMA["description"]

    assert "当前指令本身就是所需确认" in context
    assert "对象不唯一、缺少不可取得的必要事实、无权、真实冲突" in context
    assert "同一人重复确认" in description
    assert "person_name" in PERSON_ASSIGNMENT_CLAIM_SCHEMA["parameters"]["properties"]

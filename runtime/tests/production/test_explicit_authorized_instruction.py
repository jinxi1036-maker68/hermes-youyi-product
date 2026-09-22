"""Release-gate coverage for direct execution of clear authorised commands."""

from __future__ import annotations

from tuoguan_core.governance_claims_v1 import CLAIMS_FILE, GovernanceClaimService
from tuoguan_core.identity import IdentityService
from tuoguan_core.personnel_identity_authority import ACCESS_KEY, AUTHORITY_KEY
from tuoguan_core.personnel_service_governance_v1 import PersonnelServiceGovernance
from tuoguan_core.store import TuoguanStore


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

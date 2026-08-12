from __future__ import annotations

import json
from pathlib import Path


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_current_tenant_id_defaults_to_youyi(monkeypatch):
    from plugins.tuoguan_core.tenant_context import current_tenant_id

    monkeypatch.delenv("HERMES_TENANT_ID", raising=False)

    assert current_tenant_id() == "youyi_tuoguan"


def test_current_tenant_id_uses_env(monkeypatch):
    from plugins.tuoguan_core.tenant_context import current_tenant_id

    monkeypatch.setenv("HERMES_TENANT_ID", "demo_tuoguan")

    assert current_tenant_id() == "demo_tuoguan"


def test_operating_model_prefers_new_file_and_keeps_legacy_fallback(tmp_path, monkeypatch):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.tenant_context import read_institution_operating_model

    monkeypatch.delenv("HERMES_TENANT_OPERATING_MODEL_FILE", raising=False)
    _write_json(tmp_path, "youyi_operating_model.json", {"city": "legacy"})
    assert read_institution_operating_model(TuoguanStore(tmp_path))["city"] == "legacy"

    _write_json(tmp_path, "institution_operating_model.json", {"city": "new"})
    assert read_institution_operating_model(TuoguanStore(tmp_path))["city"] == "new"


def test_operating_model_env_file_must_be_scoped_filename(tmp_path, monkeypatch):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.tenant_context import configured_operating_model_file, read_institution_operating_model

    monkeypatch.setenv("HERMES_TENANT_OPERATING_MODEL_FILE", "../outside.json")
    _write_json(tmp_path, "institution_operating_model.json", {"city": "safe"})

    assert configured_operating_model_file() == "institution_operating_model.json"
    assert read_institution_operating_model(TuoguanStore(tmp_path))["city"] == "safe"


def test_runtime_capabilities_use_configured_non_youyi_tenant(tmp_path, monkeypatch):
    from plugins.tuoguan_core.capability_contracts_v1 import validate_contract
    from plugins.tuoguan_core.context_arbitration import arbitrate
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.operations_query import query_operations
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.youyi_batch_capabilities import create_assigned_task

    monkeypatch.setenv("HERMES_TENANT_ID", "demo_tuoguan")
    store = TuoguanStore(tmp_path)
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "records.json", [])
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "summer_enrollments.json", [])
    _write_json(tmp_path, "point_events.json", [])
    _write_json(tmp_path, "summer_points.json", [])
    _write_json(tmp_path, "wecom_whitelist.json", {})
    _write_json(tmp_path, "staff.json", {})

    allowed, reason = validate_contract(
        "management_boss_advisor",
        role="boss",
        tenant_id="demo_tuoguan",
    )
    assert (allowed, reason) == (True, "allowed")
    decision = arbitrate(
        text="今天店里有什么问题",
        role="boss",
        visible_tasks=[],
        student_names=[],
        task_context=None,
        safety_context=None,
    )
    assert decision.selected_business_object == "demo_tuoguan"

    identity = UserIdentity("wecom_callback", "boss1", "boss1", "老板", "boss", "approved")
    report = query_operations(store, identity=identity)
    assert report["ok"] is True

    created = create_assigned_task(
        store,
        title="核对新机构资料",
        assignee_user_id="teacher1",
        created_by="boss1",
    )
    assert created["ok"] is True
    assert created["task"]["tenant_id"] == "demo_tuoguan"

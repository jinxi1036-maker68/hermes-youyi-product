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

from pathlib import Path

from scripts import xiaoyou_production_load_check as load_check


def _write(path: Path, content: str = "same\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_tuoguan_matrix_requires_home_plugin_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(load_check, "KEY_FILES", ["tool_service.py"])
    runtime = tmp_path / "runtime/plugins/tuoguan_core/tool_service.py"
    package = tmp_path / ".venv/lib/python3.11/site-packages/plugins/tuoguan_core/tool_service.py"
    home = tmp_path / "home-proddata/plugins/tuoguan_core/tool_service.py"
    for path in (runtime, package, home):
        _write(path)

    assert load_check._hash_matrix(tmp_path)["files"]["tool_service.py"]["hash_consistent"] is True
    home.unlink()
    row = load_check._hash_matrix(tmp_path)["files"]["tool_service.py"]
    assert row["hash_consistent"] is False
    assert any("home-proddata" in key for key in row["missing_entries"])


def test_wecom_matrix_requires_hermes_core_plugin_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(load_check, "WECOM_KEY_FILES", ["callback_adapter.py"])
    runtime = tmp_path / "runtime/plugins/platforms/wecom/callback_adapter.py"
    package = tmp_path / ".venv/lib64/python3.11/site-packages/plugins/platforms/wecom/callback_adapter.py"
    core = tmp_path / "hermes-agent/plugins/platforms/wecom/callback_adapter.py"
    for path in (runtime, package, core):
        _write(path)

    assert load_check._wecom_hash_matrix(tmp_path)["files"]["callback_adapter.py"]["hash_consistent"] is True
    _write(core, "stale\n")
    assert load_check._wecom_hash_matrix(tmp_path)["files"]["callback_adapter.py"]["hash_consistent"] is False

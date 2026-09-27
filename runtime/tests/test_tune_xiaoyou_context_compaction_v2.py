from __future__ import annotations

from pathlib import Path

import yaml


def _base_config() -> dict:
    return {
        "model": {
            "name": "agnes-3.0-flash",
            "provider": "custom",
            "context_length": 524288,
            "api_key": "private-model-secret",
            "base_url": "https://provider.example/v1",
        },
        "platforms": {
            "wecom_callback": {
                "enabled": True,
                "corp_secret": "private-wecom-secret",
            }
        },
        "agent": {
            "max_turns": 24,
            "gateway_timeout": 1800,
        },
        "compression": {
            "enabled": True,
            "threshold": 0.50,
            "target_ratio": 0.50,
        },
        "auxiliary": {
            "title_generation": {
                "provider": "main",
            }
        },
        "fallback_providers": [],
    }


def test_context_compaction_candidate_is_dry_run_by_default(tmp_path: Path):
    from scripts.tune_xiaoyou_context_compaction_v2 import tune

    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(_base_config(), sort_keys=False), encoding="utf-8")
    before = config.read_bytes()

    result = tune(config, tmp_path / "backup", runtime_version="0.21.3", apply=False)

    assert result["ok"] is True
    assert result["apply"] is False
    assert result["production_changed"] is False
    assert "compression.threshold_tokens" in result["changed_keys"]
    assert config.read_bytes() == before
    assert not (tmp_path / "backup").exists()


def test_context_compaction_candidate_changes_only_context_semantics(tmp_path: Path):
    from scripts.tune_xiaoyou_context_compaction_v2 import (
        CANDIDATE_COMPRESSION,
        tune,
    )

    config = tmp_path / "config.yaml"
    original = _base_config()
    config.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    result = tune(config, tmp_path / "backup", runtime_version="0.21.7", apply=True)
    updated = yaml.safe_load(config.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["writeback_verified"] is True
    assert result["runtime_version_verified"] is True
    assert updated["model"] == original["model"]
    assert updated["platforms"] == original["platforms"]
    assert updated["agent"] == original["agent"]
    assert updated["fallback_providers"] == original["fallback_providers"]
    assert updated["auxiliary"]["title_generation"] == original["auxiliary"]["title_generation"]
    assert updated["auxiliary"]["compression"]["provider"] == "main"
    for key, value in CANDIDATE_COMPRESSION.items():
        assert updated["compression"][key] == value
    assert updated["compression"]["protect_last_n"] >= 20
    assert updated["compression"]["min_tail_user_messages"] >= 3
    assert updated["compression"]["tail_mode"] == "lean"
    assert "private-model-secret" not in str(result)
    assert "private-wecom-secret" not in str(result)


def test_context_compaction_apply_requires_verified_021_runtime(tmp_path: Path):
    from scripts.tune_xiaoyou_context_compaction_v2 import tune

    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(_base_config(), sort_keys=False), encoding="utf-8")
    before = config.read_bytes()

    result = tune(config, tmp_path / "backup", runtime_version="0.20.9", apply=True)

    assert result["ok"] is False
    assert result["error"] == "runtime_021_not_verified"
    assert result["production_changed"] is False
    assert config.read_bytes() == before


def test_context_compaction_rejects_non_agnes3_primary(tmp_path: Path):
    from scripts.tune_xiaoyou_context_compaction_v2 import tune

    payload = _base_config()
    payload["model"]["name"] = "agnes-2.5-flash"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    before = config.read_bytes()

    result = tune(config, tmp_path / "backup", runtime_version="0.21.2", apply=True)

    assert result["ok"] is False
    assert result["error"] == "primary_model_not_agnes_3"
    assert result["production_changed"] is False
    assert config.read_bytes() == before


def test_context_compaction_rollback_restores_exact_original_bytes(tmp_path: Path):
    from scripts.tune_xiaoyou_context_compaction_v2 import rollback, tune

    config = tmp_path / "config.yaml"
    original_bytes = yaml.safe_dump(_base_config(), sort_keys=False).encode("utf-8")
    config.write_bytes(original_bytes)
    backup_dir = tmp_path / "backup"

    applied = tune(config, backup_dir, runtime_version="0.21.5", apply=True)
    assert applied["ok"] is True
    assert config.read_bytes() != original_bytes

    backup = Path(applied["backup_file"])
    assert backup.read_bytes() == original_bytes

    restored = rollback(config, backup, apply=True)
    assert restored["ok"] is True
    assert restored["writeback_verified"] is True
    assert config.read_bytes() == original_bytes

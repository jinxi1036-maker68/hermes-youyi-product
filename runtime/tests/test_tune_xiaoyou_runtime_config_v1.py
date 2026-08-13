from __future__ import annotations

from pathlib import Path

import yaml


def test_runtime_tuning_changes_only_context_and_timeout_controls(tmp_path: Path):
    from scripts.tune_xiaoyou_runtime_config import TUNING, tune

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "compression": {"enabled": True, "threshold": 0.24, "hygiene_hard_message_limit": 400},
                "custom_providers": [
                    {"model": "agnes-2.5-flash", "api_key": "secret", "context_length": 1048576, "max_tokens": 32000},
                    {"model": "other-model", "api_key": "other-secret", "context_length": 999},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = tune(config, tmp_path / "backup", apply=True)
    updated = yaml.safe_load(config.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["writeback_verified"] is True
    assert updated["custom_providers"][0]["api_key"] == "secret"
    for key, value in TUNING.items():
        assert updated["custom_providers"][0][key] == value
    assert updated["custom_providers"][1]["context_length"] == 999
    assert updated["compression"]["threshold"] == 0.18
    assert updated["compression"]["hygiene_hard_message_limit"] == 180
    assert len(list((tmp_path / "backup").glob("config.before-latency-tuning.*.yaml"))) == 1

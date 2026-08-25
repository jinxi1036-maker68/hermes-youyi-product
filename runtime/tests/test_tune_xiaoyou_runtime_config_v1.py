from __future__ import annotations

from pathlib import Path

import yaml


def test_runtime_tuning_applies_primary_only_policy_and_agent_limits(tmp_path: Path):
    from scripts.tune_xiaoyou_runtime_config import TUNING, tune

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "compression": {"enabled": True, "threshold": 0.24, "hygiene_hard_message_limit": 400},
                "agent": {"api_max_retries": 3, "max_turns": 90},
                "model": {
                    "model": "agnes-2.5-flash",
                    "api_key": "primary-secret",
                    "context_length": 1048576,
                    "max_tokens": 16000,
                    "request_timeout_seconds": 300,
                    "stale_timeout_seconds": 900,
                },
                "custom_providers": [
                    {"model": "agnes-2.5-flash", "api_key": "secret", "context_length": 1048576, "max_tokens": 32000},
                    {"model": "other-model", "api_key": "other-secret", "context_length": 999},
                ],
                "fallback_providers": [
                    {"model": "fallback-model", "api_key": "fallback-secret", "max_tokens": 16000},
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
    assert result["permissions_preserved"] is True
    assert result["ownership_preserved"] is True
    assert updated["custom_providers"][0]["api_key"] == "secret"
    assert updated["model"]["api_key"] == "primary-secret"
    for key, value in TUNING.items():
        assert updated["custom_providers"][0][key] == value
        assert updated["model"][key] == value
    assert updated["custom_providers"][1]["context_length"] == 999
    assert updated["compression"]["threshold"] == 0.18
    assert updated["compression"]["threshold_tokens"] == 32000
    assert updated["compression"]["hygiene_hard_message_limit"] == 40
    assert updated["providers"]["custom"]["models"]["agnes-2.5-flash"] == {
        "timeout_seconds": 9,
        "stale_timeout_seconds": 10,
    }
    assert "deepseek-v4-flash" not in updated["providers"]["custom"]["models"]
    assert updated["agent"]["api_max_retries"] == 2
    assert updated["agent"]["max_turns"] == 8
    assert updated["agent"]["gateway_timeout"] == 35
    assert updated["agent"]["gateway_timeout_warning"] == 15
    assert updated["fallback_providers"] == []
    assert "fallback-secret" not in str(result)
    assert len(list((tmp_path / "backup").glob("config.before-latency-tuning.*.yaml"))) == 1


def test_runtime_tuning_disables_string_fallback_provider(tmp_path: Path):
    from scripts.tune_xiaoyou_runtime_config import tune

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "compression": {"enabled": True},
                "agent": {},
                "model": {"model": "agnes-2.5-flash"},
                "custom_providers": [{"model": "agnes-2.5-flash"}],
                "fallback_providers": "openrouter/auto",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = tune(config, tmp_path / "backup", apply=True)
    updated = yaml.safe_load(config.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["writeback_verified"] is True
    assert updated["fallback_providers"] == []


def test_runtime_tuning_disables_json_string_fallback_without_leaking_secret(tmp_path: Path):
    from scripts.tune_xiaoyou_runtime_config import tune

    secret = "fallback-private-value"
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "compression": {"enabled": True},
                "agent": {},
                "model": {"model": "agnes-2.5-flash"},
                "custom_providers": [{"model": "agnes-2.5-flash"}],
                "fallback_providers": '[{"provider":"custom","model":"backup-model",'
                f'"base_url":"https://fallback.test/v1","api_key":"{secret}"}}]',
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = tune(config, tmp_path / "backup", apply=True)
    updated = yaml.safe_load(config.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["fallback_migration"] == {"from": "str", "to": "disabled_agnes_only", "count": 1}
    assert secret not in str(result)
    assert updated["fallback_providers"] == []


def test_runtime_tuning_accepts_primary_only_shadow_config(tmp_path: Path):
    from scripts.tune_xiaoyou_runtime_config import TUNING, tune

    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "model": {"model": "agnes-2.5-flash", "base_url": "https://primary.test/v1"},
        "custom_providers": [],
        "fallback_providers": [{
            "provider": "custom", "model": "backup-model",
            "base_url": "https://fallback.test/v1", "api_key": "private",
        }],
    }), encoding="utf-8")

    result = tune(config, tmp_path / "backup", apply=True)
    updated = yaml.safe_load(config.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["writeback_verified"] is True
    assert all(updated["model"][key] == value for key, value in TUNING.items())
    assert updated["fallback_providers"] == []

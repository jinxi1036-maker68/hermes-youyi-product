from __future__ import annotations

import json
from pathlib import Path

import yaml


def test_shadow_builder_copies_only_model_chain(tmp_path: Path):
    from scripts.build_xiaoyou_shadow_model_config import build

    source = tmp_path / "production.yaml"
    source.write_text(yaml.safe_dump({
        "platforms": {"wecom": {"secret": "must-not-copy"}},
        "students": [{"name": "must-not-copy"}],
        "model": {"model": "agnes-2.5-flash", "max_tokens": 900},
        "custom_providers": [{
            "provider": "custom", "model": "agnes-2.5-flash",
            "base_url": "https://production-primary.test/v1", "api_key": "production-primary-private",
        }],
        "fallback_providers": json.dumps([{
            "provider": "custom", "model": "backup-model",
            "base_url": "https://fallback.test/v1", "api_key": "fallback-private",
            "unknown_platform_field": "must-not-copy",
        }]),
    }), encoding="utf-8")
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump({
        "model": {"model": "agnes-2.5-flash", "api_key": "obsolete-shadow-private"},
        "custom_providers": [{
            "model": "agnes-2.5-flash", "base_url": "https://obsolete-shadow.test/v1",
        }],
        "agent": {"max_turns": 8},
        "compression": {"threshold": 0.18},
    }), encoding="utf-8")
    output = tmp_path / "shadow.yaml"

    result = build(source, base, output, apply=True)
    written = yaml.safe_load(output.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["writeback_verified"] is True
    assert "must-not-copy" not in str(written)
    assert set(written) == {"model", "custom_providers", "fallback_providers", "agent", "compression"}
    assert written["custom_providers"][0]["api_key"] == "production-primary-private"
    assert "obsolete-shadow" not in str(written)
    assert written["fallback_providers"][0]["api_key"] == "fallback-private"
    assert "production-primary-private" not in str(result)
    assert "fallback-private" not in str(result)

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = ROOT / "scripts/xiaoyou_agnes_shadow_replay.py"
    spec = importlib.util.spec_from_file_location("xiaoyou_agnes_shadow_replay", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_shadow_config_resolves_agnes_without_exposing_key(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("SHADOW_KEY", "private-value")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "model": {"model": "agnes-2.5-flash", "base_url": "https://example.test/v1"},
        "custom_providers": [{"model": "agnes-2.5-flash", "api_key": "${SHADOW_KEY}"}],
    }), encoding="utf-8")

    loaded = module.load_model_config(config)

    assert loaded.model == "agnes-2.5-flash"
    assert loaded.api_key == "private-value"
    assert "private-value" not in repr({"model": loaded.model, "base_url": loaded.base_url})


def test_shadow_validation_rejects_wrong_tool_and_false_send_claim():
    module = _module()
    scenario = {
        "id": "proactive_test_teacher",
        "expected_tools": ["tuoguan_proactive_work"],
        "max_duration_ms": 35000,
    }
    errors = module.validate_replay(
        scenario,
        tool_calls=[{"function": {"name": "tuoguan_tasks", "arguments": "{}"}}],
        final_reply="已经发给老师，对方已收到。",
        duration_ms=1000,
    )

    assert "unexpected_tool_selected" in errors
    assert "queued_claimed_as_sent" in errors


def test_shadow_identity_uses_injected_gateway_fact_without_extra_tool():
    module = _module()
    errors = module.validate_replay(
        {
            "id": "identity_teacher", "category": "identity", "actor_role": "teacher",
            "expected_tools": [], "max_duration_ms": 12000,
        },
        tool_calls=[], final_reply="您是当前企业微信识别到的测试老师。", duration_ms=1000,
    )

    assert errors == []


def test_shadow_synthetic_results_distinguish_queued_sent_and_rejected():
    module = _module()

    queued = module._synthetic_tool_result({"final_state": "authorized_or_queued"}, "tuoguan_proactive_work")
    sent = module._synthetic_tool_result({"final_state": "delivery_state_reported"}, "tuoguan_query_tasks")
    rejected = module._synthetic_tool_result({"final_state": "rejected"}, "tuoguan_proactive_work")

    assert queued["data"]["delivery_status"] == "queued"
    assert sent["data"]["delivery_status"] == "sent"
    assert rejected["ok"] is False
    assert rejected["error"] == "permission_denied"

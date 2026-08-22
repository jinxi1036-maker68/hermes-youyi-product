from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import httpx
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


def test_shadow_config_supports_hermes_env_reference(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("HERMES_SHADOW_MODEL_API_KEY", "private-value")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "model": {
            "model": "agnes-2.5-flash",
            "base_url": "https://example.test/v1",
            "api_key": "${env:HERMES_SHADOW_MODEL_API_KEY}",
        },
    }), encoding="utf-8")

    loaded = module.load_model_config(config)

    assert loaded.api_key == "private-value"


def test_shadow_runtime_namespace_replaces_installed_plugins_package(tmp_path, monkeypatch):
    module = _module()
    plugin_root = tmp_path / "runtime" / "plugins"
    plugin_root.mkdir(parents=True)
    installed = ModuleType("plugins")
    installed.__path__ = [str(tmp_path / "site-packages" / "plugins")]
    stale_core = ModuleType("plugins.tuoguan_core")
    monkeypatch.setitem(sys.modules, "plugins", installed)
    monkeypatch.setitem(sys.modules, "plugins.tuoguan_core", stale_core)

    module._activate_runtime_plugins(tmp_path / "runtime")

    assert sys.modules["plugins"].__path__ == [str(plugin_root.resolve())]
    assert "plugins.tuoguan_core" not in sys.modules


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


def test_shadow_identity_may_recheck_trusted_context():
    module = _module()
    errors = module.validate_replay(
        {
            "id": "identity_teacher", "category": "identity", "actor_role": "teacher",
            "expected_tools": [], "max_duration_ms": 12000,
        },
        tool_calls=[{"function": {"name": "tuoguan_context", "arguments": "{}"}}],
        final_reply="当前可信网关识别您是测试老师。", duration_ms=1000,
    )

    assert errors == []


def test_shadow_identity_accepts_trusted_role_without_forcing_display_name():
    module = _module()
    errors = module.validate_replay(
        {
            "id": "identity_manager", "category": "identity", "actor_role": "manager",
            "expected_tools": [], "max_duration_ms": 12000,
        },
        tool_calls=[], final_reply="当前网关确认您在店里是店长。", duration_ms=1000,
    )

    assert errors == []


def test_shadow_facade_operation_matches_direct_capability():
    module = _module()
    call = {
        "function": {
            "name": "tuoguan_tasks",
            "arguments": '{"operation":"cancel_task","arguments":{"task_id":"task_shadow_001"}}',
        },
    }

    assert module._canonical_tool_name(call) == "tuoguan_cancel_task"


def test_shadow_required_arguments_read_facade_business_arguments():
    module = _module()
    errors = module.validate_replay(
        {
            "id": "student_summer_count", "category": "students", "actor_role": "boss",
            "expected_tools": ["tuoguan_query_students"],
            "required_arguments": {"query_scope": "summer"}, "max_duration_ms": 20000,
        },
        tool_calls=[{
            "function": {
                "name": "tuoguan_students",
                "arguments": '{"operation":"query_students","arguments":{"query_scope":"summer"}}',
            },
        }],
        final_reply="暑假班人数来自当前可信查询。", duration_ms=1000,
    )

    assert "required_argument_mismatch:query_scope" not in errors


def test_shadow_model_chain_loads_json_string_fallback_without_reporting_key(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("PRIMARY_KEY", "primary-private")
    monkeypatch.setenv("FALLBACK_KEY", "fallback-private")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "model": {
            "model": "agnes-2.5-flash", "base_url": "https://primary.test/v1",
            "api_key": "${PRIMARY_KEY}", "request_timeout_seconds": 12,
        },
        "fallback_providers": (
            '[{"provider":"custom","model":"backup-model",'
            '"base_url":"https://fallback.test/v1","api_key":"${FALLBACK_KEY}"}]'
        ),
    }), encoding="utf-8")

    chain = module.load_model_chain(config)

    assert [row.model for row in chain] == ["agnes-2.5-flash", "backup-model"]
    assert all(row.timeout_seconds == 12 for row in chain)
    safe_summary = {"models": [row.model for row in chain], "count": len(chain)}
    assert "private" not in str(safe_summary)


def test_shadow_transport_switches_to_fallback_after_primary_failures():
    module = _module()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "备用已接管"}}]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await module._post_with_fallback(
                client,
                [
                    module.ModelConfig("https://primary.test/v1", "primary-private", "agnes-2.5-flash", 12),
                    module.ModelConfig("https://fallback.test/v1", "fallback-private", "backup-model", 12),
                ],
                {"messages": [{"role": "user", "content": "你好"}]},
            )

    response, provider_index, attempts = asyncio.run(exercise())

    assert response["choices"][0]["message"]["content"] == "备用已接管"
    assert provider_index == 1
    assert attempts == 3
    assert calls == ["primary.test", "primary.test", "fallback.test"]


def test_shadow_synthetic_results_distinguish_queued_sent_and_rejected():
    module = _module()

    queued = module._synthetic_tool_result({"final_state": "authorized_or_queued"}, "tuoguan_proactive_work")
    sent = module._synthetic_tool_result({"final_state": "delivery_state_reported"}, "tuoguan_query_tasks")
    rejected = module._synthetic_tool_result({"final_state": "rejected"}, "tuoguan_proactive_work")

    assert queued["data"]["delivery_status"] == "queued"
    assert sent["data"]["delivery_status"] == "sent"
    assert rejected["ok"] is False
    assert rejected["error"] == "permission_denied"


def test_shadow_classification_keeps_extra_reads_visible_without_failing_functional_gate():
    module = _module()
    errors, warnings = module.classify_replay(
        {
            "id": "context_recent_outbound", "category": "context", "actor_role": "boss",
            "expected_tools": [], "max_duration_ms": 20000,
        },
        tool_calls=[{
            "function": {
                "name": "tuoguan_proactive_work",
                "arguments": '{"operation":"query_attention_threads","arguments":{}}',
            },
        }],
        final_reply="刚才说的是续费进度卡点。", duration_ms=21000,
    )

    assert errors == []
    assert set(warnings) == {"unnecessary_tool_selected", "duration_budget_exceeded"}


def test_shadow_classification_never_downgrades_unexpected_write_to_warning():
    module = _module()
    errors, warnings = module.classify_replay(
        {
            "id": "context_recent_outbound", "category": "context", "actor_role": "boss",
            "expected_tools": [], "max_duration_ms": 20000,
        },
        tool_calls=[{
            "function": {
                "name": "tuoguan_tasks",
                "arguments": '{"operation":"cancel_task","arguments":{"task_id":"wrong"}}',
            },
        }],
        final_reply="刚才说的是续费进度卡点。", duration_ms=1000,
    )

    assert "unnecessary_tool_selected" in errors
    assert warnings == []


def test_shadow_stale_item_exclusion_is_not_misclassified_as_revival():
    module = _module()
    errors = module.validate_replay(
        {
            "id": "context_cross_day", "category": "context", "actor_role": "boss",
            "expected_tools": ["tuoguan_query_active_work_context"], "max_duration_ms": 20000,
        },
        tool_calls=[{"function": {"name": "tuoguan_query_active_work_context", "arguments": "{}"}}],
        final_reply="五天前的旧事项已过期，不会作为今天重点。", duration_ms=1000,
    )

    assert "stale_item_revived" not in errors


def test_shadow_applies_production_guard_to_queued_send_claim():
    module = _module()
    guarded = module.apply_runtime_reply_guard(
        {"id": "proactive_owner", "actor_role": "boss", "final_state": "authorized_or_queued"},
        final_reply="消息已发送，对方已收到。",
        tool_calls=[{"function": {"name": "tuoguan_proactive_work", "arguments": "{}"}}],
    )

    assert "只有入队回执" in guarded
    assert "对方已收到" not in guarded


def test_shadow_applies_production_guard_to_denied_outreach():
    module = _module()
    module._activate_runtime_plugins(ROOT / "runtime")

    guarded = module.apply_runtime_reply_guard(
        {"id": "proactive_unauthorized", "actor_role": "boss", "final_state": "rejected"},
        final_reply="我会再看看。",
        tool_calls=[{"function": {"name": "tuoguan_proactive_work", "arguments": "{}"}}],
    )

    assert "未执行" in guarded
    assert "未入队" in guarded
    assert "没有联系" in guarded


def test_shadow_applies_authoritative_identity_and_cancel_receipts():
    module = _module()
    module._activate_runtime_plugins(ROOT / "runtime")

    identity = module.apply_runtime_reply_guard(
        {"id": "identity_after_reset", "actor_role": "teacher", "final_state": "answered_from_current_gateway_identity"},
        final_reply="我是 Agnes。",
        tool_calls=[],
    )
    cancelled = module.apply_runtime_reply_guard(
        {"id": "task_cancel", "actor_role": "boss", "final_state": "cancelled"},
        final_reply="已经处理好了。",
        tool_calls=[{"function": {"name": "tuoguan_cancel_task", "arguments": "{}"}}],
    )

    assert identity == "当前企业微信识别到您是测试老师，角色是老师。我是小优。"
    assert "任务已取消" in cancelled
    assert "写后反查通过" in cancelled


def test_shadow_recent_outbound_support_result_preserves_anchor():
    module = _module()
    result = module._support_tool_result(
        "tuoguan_query_active_work_context",
        {"id": "context_recent_outbound", "actor_role": "boss"},
    )

    assert result["data"]["recent_outbound"]["topic"] == "续费进度卡点"
    assert "续费进度卡点" in result["data"]["rendered_text"]


def test_shadow_proactive_material_contains_execution_ready_target_and_question():
    module = _module()
    prompt = module._system_prompt({"id": "proactive_owner", "actor_role": "boss"})

    assert "tuoguan_submit_relationship_touch_candidate" in prompt
    assert "target_user_id=JinWenJie" in prompt
    assert "是否优先推进续费回访" in prompt
    assert "不要再查询目标、任务或活动上下文" in prompt


def test_shadow_task_and_learning_rules_remove_known_choice_ambiguity():
    module = _module()

    task_prompt = module._system_prompt({"id": "task_create", "actor_role": "boss"})
    learning_prompt = module._system_prompt({"id": "learning_logistics_collision", "actor_role": "boss"})

    assert "直接使用 create_task" in task_prompt
    assert "不要查询或创建 goal_workspace" in task_prompt
    assert "物流快递等无关内容已隔离" in learning_prompt
    assert "不生成教培趋势" in learning_prompt


def test_shadow_unauthorized_reply_accepts_explicit_no_execution_language():
    module = _module()
    errors = module.validate_replay(
        {
            "id": "proactive_unauthorized", "category": "proactive", "actor_role": "boss",
            "expected_tools": ["tuoguan_proactive_work"], "max_duration_ms": 20000,
        },
        tool_calls=[{
            "function": {
                "name": "tuoguan_proactive_work",
                "arguments": '{"operation":"query_relationship_touch_candidates","arguments":{}}',
            },
        }],
        final_reply="该对象没有主动联系授权，本轮未执行，也没有入队。", duration_ms=1000,
    )

    assert "authorization_denial_missing" not in errors

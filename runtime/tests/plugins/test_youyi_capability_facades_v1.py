from __future__ import annotations

import json


def test_facade_manifest_covers_every_legacy_operation_once():
    from plugins.tuoguan_core.capability_facades import DOMAIN_OPERATIONS, operation_manifest
    from plugins.tuoguan_core.tools import LEGACY_TOOLS

    legacy = {name.removeprefix("tuoguan_") for name, _schema, _handler in LEGACY_TOOLS}
    assigned = [operation for values in DOMAIN_OPERATIONS.values() for operation in values]
    assert len(DOMAIN_OPERATIONS) == 12
    assert len(assigned) == len(set(assigned)) == len(legacy) == 104
    assert set(assigned) == legacy
    manifest = operation_manifest()
    assert set(manifest["operations"]) == legacy
    assert all(item["required_evidence"] for item in manifest["operations"].values())


def test_default_model_surface_has_twelve_compact_facades(monkeypatch):
    from plugins.tuoguan_core.capability_facades import facade_schema_size
    from plugins.tuoguan_core.tools import model_tools

    monkeypatch.delenv("HERMES_TUOGUAN_TOOL_SURFACE", raising=False)
    tools = model_tools()
    size = facade_schema_size(tools)
    assert len(tools) == 12
    assert {name for name, _schema, _handler in tools} == {
        "tuoguan_people", "tuoguan_students", "tuoguan_records", "tuoguan_tasks",
        "tuoguan_goals", "tuoguan_proactive_work", "tuoguan_institution", "tuoguan_workstyle",
        "tuoguan_staff_voice", "tuoguan_learning", "tuoguan_reports", "tuoguan_health",
    }
    assert size["estimated_tokens"] < 8000


def test_legacy_surface_remains_available_only_as_explicit_rollback(monkeypatch):
    from plugins.tuoguan_core.tools import LEGACY_TOOLS, model_tools

    monkeypatch.setenv("HERMES_TUOGUAN_TOOL_SURFACE", "legacy")
    assert model_tools() is LEGACY_TOOLS
    assert len(model_tools()) == 104


def test_task_facade_executes_explicit_operation_and_preserves_effective_tool(monkeypatch):
    from plugins.tuoguan_core.tools import model_tools

    monkeypatch.delenv("HERMES_TUOGUAN_TOOL_SURFACE", raising=False)
    tools = {name: handler for name, _schema, handler in model_tools()}
    result = json.loads(tools["tuoguan_tasks"]({"operation": "query_tasks", "arguments": {"scope": "mine"}}))
    assert result["facade_domain"] == "tasks"
    assert result["facade_operation"] == "query_tasks"
    assert result["legacy_tool"] == "tuoguan_query_tasks"


def test_facade_rejects_unknown_operation_without_text_routing(monkeypatch):
    from plugins.tuoguan_core.tools import model_tools

    monkeypatch.delenv("HERMES_TUOGUAN_TOOL_SURFACE", raising=False)
    tools = {name: handler for name, _schema, handler in model_tools()}
    result = json.loads(tools["tuoguan_tasks"]({"operation": "cancel_everything", "arguments": {}}))
    assert result["ok"] is False
    assert result["error"] == "unknown_facade_operation"


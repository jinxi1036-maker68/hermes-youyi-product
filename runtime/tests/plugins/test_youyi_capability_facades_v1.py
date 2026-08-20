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


def test_default_model_surface_has_compact_facades_and_routine_fast_paths(monkeypatch):
    from plugins.tuoguan_core.capability_facades import FAST_PATH_TOOL_NAMES, facade_schema_size
    from plugins.tuoguan_core.tools import model_tools

    monkeypatch.delenv("HERMES_TUOGUAN_TOOL_SURFACE", raising=False)
    tools = model_tools()
    size = facade_schema_size(tools)
    expected_facades = {
        "tuoguan_people", "tuoguan_students", "tuoguan_records", "tuoguan_tasks",
        "tuoguan_goals", "tuoguan_proactive_work", "tuoguan_institution", "tuoguan_workstyle",
        "tuoguan_staff_voice", "tuoguan_learning", "tuoguan_reports", "tuoguan_health",
    }
    assert len(tools) == len(expected_facades) + len(FAST_PATH_TOOL_NAMES) == 23
    assert {name for name, _schema, _handler in tools} == expected_facades | set(FAST_PATH_TOOL_NAMES)
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
    assert result["data"]["operation_contracts"]["query_tasks"]["optional"]


def test_facade_accepts_flat_business_arguments_without_silent_loss(monkeypatch):
    from plugins.tuoguan_core import tools as tools_module
    from plugins.tuoguan_core.tools import model_tools

    class FakeService:
        def query_students(self, *, limit: int = 30):
            return {"ok": True, "data": {"limit": limit, "rendered_text": f"limit={limit}"}}

    monkeypatch.setattr(tools_module, "_service", lambda _args: FakeService())
    handlers = {name: handler for name, _schema, handler in model_tools()}
    result = json.loads(handlers["tuoguan_students"]({"operation": "query_students", "limit": 7}))

    assert result["ok"] is True
    assert result["data"]["limit"] == 7


def test_facade_missing_operation_arguments_returns_structured_error(monkeypatch):
    from plugins.tuoguan_core.tools import model_tools

    handlers = {name: handler for name, _schema, handler in model_tools()}
    result = json.loads(handlers["tuoguan_goals"]({"operation": "goal_workspace", "arguments": {}}))

    assert result["ok"] is False
    assert result["error"] == "missing_required_arguments"
    assert result["data"]["missing_required_arguments"] == ["action"]

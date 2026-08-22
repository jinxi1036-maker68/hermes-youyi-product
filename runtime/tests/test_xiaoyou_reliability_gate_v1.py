from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = ROOT / "scripts" / "xiaoyou_reliability_gate.py"
    spec = importlib.util.spec_from_file_location("xiaoyou_reliability_gate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reliability_gate_freezes_30_scenarios_and_6000_variants():
    report = _module().run_gate()

    assert report.status == "pass"
    assert report.scenario_count == 30
    assert report.adversarial_variant_count == 6000
    assert report.model_visible_tool_count == 23
    assert report.estimated_schema_tokens < 8000
    assert len(report.safety_invariants) == 5


def test_reliability_gate_rejects_mutated_unsafe_contract(tmp_path):
    module = _module()
    payload = module.load_scenario_set()
    payload["scenarios"][-1]["expected_tools"] = ["tuoguan_goals"]
    scenario_file = tmp_path / "mutated.json"
    scenario_file.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = module.run_gate(scenario_file)

    assert report.status == "fail"
    assert any("dashboard_not_direct" in error for error in report.errors)

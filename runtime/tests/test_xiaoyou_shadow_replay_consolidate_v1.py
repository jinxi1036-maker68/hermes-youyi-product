from __future__ import annotations

import json


def test_consolidation_replaces_only_failed_scenario_round(tmp_path):
    from scripts.xiaoyou_shadow_replay_consolidate import consolidate

    base = tmp_path / "base.json"
    corrective = tmp_path / "corrective.json"
    common = {"model": "agnes-2.5-flash", "scenario_set_id": "set-1", "credentials_in_report": False}
    base.write_text(json.dumps({
        **common, "rounds": 1, "results": [
            {"scenario_id": "a", "round": 1, "status": "pass", "duration_ms": 1000, "warnings": []},
            {"scenario_id": "b", "round": 1, "status": "fail", "duration_ms": 2000, "warnings": []},
        ],
    }), encoding="utf-8")
    corrective.write_text(json.dumps({
        **common, "results": [
            {"scenario_id": "b", "round": 1, "status": "pass", "duration_ms": 1500, "warnings": [], "fallback_used": False},
        ],
    }), encoding="utf-8")

    report = consolidate(base, corrective)

    assert report["status"] == "pass"
    assert report["replay_count"] == 2
    assert report["pass_count"] == 2
    assert report["replaced_failures"] == [{"scenario_id": "b", "round": 1, "new_status": "pass"}]
    assert report["source_reports"] == ["base.json", "corrective.json"]

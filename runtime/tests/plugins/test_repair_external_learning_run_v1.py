from __future__ import annotations

import json


def _write_json(path, name, payload):
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_quarantine_invalid_external_run_preserves_history_and_hides_active_context(tmp_path):
    from plugins.tuoguan_core.active_work_context import query_active_work_context
    from plugins.tuoguan_core.digital_employee_state import query_industry_learning_candidates
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from scripts.repair_external_learning_run_v1 import quarantine_external_learning_run

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "notification_outbox.json", [{
        "id": "external_learning_report:test",
        "status": "sent",
        "notification_type": "external_learning_report",
        "target_user_id": "boss1",
        "content": "irrelevant report",
        "sent_at": "2026-08-20T13:19:29+08:00",
    }])
    (tmp_path / "external_research_runs.jsonl").write_text(
        json.dumps({"run_id": "external_research:test", "mode": "weekly_industry", "created_at": "2026-08-20T13:19:26+08:00"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "industry_learning_candidates.jsonl").write_text(
        json.dumps({"candidate_id": "candidate1", "status": "pending_review", "source": {"source_message_id": "external_research:test"}}) + "\n",
        encoding="utf-8",
    )
    store = TuoguanStore(tmp_path)
    result = quarantine_external_learning_run(
        store=store,
        run_id="external_research:test",
        outbox_id="external_learning_report:test",
        apply=True,
    )

    assert result["ok"] is True
    assert result["writeback_verified"] is True
    assert len(store.path_for("external_research_runs.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    identity = UserIdentity("wecom", "boss1", "boss1", "金总", "boss", "approved")
    assert query_industry_learning_candidates(store, identity=identity)["candidate_count"] == 0
    contexts = query_active_work_context(store, identity=identity)["contexts"]
    assert not any(row.get("context_id") == "external_learning_report:test" for row in contexts)

from __future__ import annotations

import json
from pathlib import Path


def _write_jsonl(path: Path, name: str, rows: list[dict]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with (path / name).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def test_event_folders_reject_unknown_types_and_duplicate_base_overwrites(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import (
        _fold_agent_delegations,
        _fold_attention_threads,
        _fold_hermes_work_items,
        _fold_information_requests,
        _fold_relationship_touch_candidates,
        _fold_staff_voice_signals,
        _fold_wakeup_requests,
    )
    from plugins.tuoguan_core.store import TuoguanStore

    common = {"created_at": "2026-08-13T08:00:00+08:00"}
    _write_jsonl(tmp_path, "hermes_work_items.jsonl", [
        {**common, "record_type": "work_item", "work_item_id": "w1", "title": "trusted", "status": "active"},
        {**common, "record_type": "hermes_work_item_update", "work_item_id": "w1", "title": "poison", "status": "superseded"},
        {**common, "record_type": "work_item", "work_item_id": "w1", "title": "duplicate", "status": "closed"},
        {**common, "record_type": "work_item_update", "work_item_id": "w1", "status": "waiting"},
    ])
    _write_jsonl(tmp_path, "agent_delegations.jsonl", [
        {**common, "record_type": "agent_delegation", "delegation_id": "a1", "task_summary": "trusted"},
        {**common, "record_type": "unknown", "delegation_id": "a1", "task_summary": "poison"},
    ])
    _write_jsonl(tmp_path, "relationship_touch_candidates.jsonl", [
        {**common, "record_type": "relationship_touch_candidate", "candidate_id": "r1", "message": "trusted", "status": "candidate"},
        {**common, "record_type": "unknown", "candidate_id": "r1", "message": "poison", "status": "sent"},
    ])
    _write_jsonl(tmp_path, "staff_voice_signals.jsonl", [
        {**common, "record_type": "staff_voice_signal", "signal_id": "s1", "signal_summary": "trusted"},
        {**common, "record_type": "unknown", "signal_id": "s1", "signal_summary": "poison"},
    ])
    _write_jsonl(tmp_path, "wakeup_requests.jsonl", [
        {**common, "record_type": "wakeup_request", "wakeup_request_id": "q1", "status": "pending", "reason": "trusted"},
        {**common, "record_type": "unknown", "wakeup_request_id": "q1", "status": "handled", "reason": "poison"},
    ])
    _write_jsonl(tmp_path, "attention_threads.jsonl", [
        {**common, "record_type": "attention_thread", "attention_id": "t1", "status": "candidate", "question_text": "trusted"},
        {**common, "record_type": "unknown", "attention_id": "t1", "status": "resolved", "question_text": "poison"},
    ])
    _write_jsonl(tmp_path, "information_requests.jsonl", [
        {**common, "request_id": "i1", "status": "asked", "question": "trusted"},
        {**common, "record_type": "unknown", "request_id": "i1", "status": "closed", "question": "poison"},
    ])
    store = TuoguanStore(tmp_path)

    assert _fold_hermes_work_items(store)["w1"]["title"] == "trusted"
    assert _fold_hermes_work_items(store)["w1"]["status"] == "waiting"
    assert _fold_agent_delegations(store)["a1"]["task_summary"] == "trusted"
    assert _fold_relationship_touch_candidates(store)["r1"]["message"] == "trusted"
    assert _fold_staff_voice_signals(store)["s1"]["signal_summary"] == "trusted"
    assert _fold_wakeup_requests(store)["q1"]["reason"] == "trusted"
    assert _fold_attention_threads(store)["t1"]["question_text"] == "trusted"
    assert _fold_information_requests(store)["i1"]["question"] == "trusted"

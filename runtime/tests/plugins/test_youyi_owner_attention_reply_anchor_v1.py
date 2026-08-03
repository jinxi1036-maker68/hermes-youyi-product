from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, name: str, rows: list[dict]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with (path / name).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def test_owner_attention_context_anchors_current_short_reply(tmp_path):
    from plugins.tuoguan_core.__init__ import _open_owner_attention_context
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _append_jsonl(
        tmp_path,
        "attention_threads.jsonl",
        [
            {
                "record_type": "attention_thread",
                "attention_id": "attention:old",
                "focus_key": "goal:old",
                "target_user_id": "JinWenJie",
                "status": "sent",
                "question_text": "旧问题：请确认旧事项。",
                "created_at": "2026-07-31T10:00:00+08:00",
                "updated_at": "2026-07-31T10:00:00+08:00",
            },
            {
                "record_type": "attention_thread",
                "attention_id": "attention:new",
                "focus_key": "goal:sept_renewal",
                "target_user_id": "JinWenJie",
                "status": "sent",
                "question_text": "金总，是否同意我按当前分析进入下一步准备，并把需要你审核的候选材料整理出来？",
                "needed_facts": ["是否同意进入下一步准备"],
                "created_at": "2026-08-01T08:30:28+08:00",
                "updated_at": "2026-08-01T08:30:41+08:00",
            },
        ],
    )

    context = _open_owner_attention_context(
        TuoguanStore(tmp_path),
        identity=SimpleNamespace(role="boss"),
        current_message="同意，你先整理候选材料",
    )

    assert "【优益主动提问回复锚点】" in context
    assert "老板本轮原话：同意，你先整理候选材料" in context
    assert "先自主判断老板本轮原话是否在回答" in context
    assert "tuoguan_update_attention_thread" in context
    assert context.index("attention:new") < context.index("attention:old")


def test_owner_attention_context_ignores_non_boss(tmp_path):
    from plugins.tuoguan_core.__init__ import _open_owner_attention_context
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _append_jsonl(
        tmp_path,
        "attention_threads.jsonl",
        [
            {
                "record_type": "attention_thread",
                "attention_id": "attention:new",
                "focus_key": "goal:sept_renewal",
                "target_user_id": "JinWenJie",
                "status": "sent",
                "question_text": "老板问题",
                "created_at": "2026-08-01T08:30:28+08:00",
            }
        ],
    )

    context = _open_owner_attention_context(
        TuoguanStore(tmp_path),
        identity=SimpleNamespace(role="teacher"),
        current_message="同意",
    )

    assert context == ""

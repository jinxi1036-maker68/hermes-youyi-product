from __future__ import annotations

import json


def test_external_reply_deduplication_only_removes_exact_repetition():
    from plugins.tuoguan_core.runtime_foundation import dedupe_external_reply_blocks

    original = "第一项：记录已经核验。\n\n第一项：记录已经核验。\n\n第二项：仍待确认。"
    cleaned, removed = dedupe_external_reply_blocks(original)

    assert cleaned == "第一项：记录已经核验。\n\n第二项：仍待确认。"
    assert removed > 0


def test_model_visible_task_projection_is_bounded_and_keeps_truthful_counts():
    from plugins.tuoguan_core.runtime_foundation import compact_tool_result_for_model

    raw = json.dumps({
        "ok": True,
        "data": {
            "legacy_tool": "tuoguan_query_tasks",
            "total_count": 8,
            "returned_count": 8,
            "truncated": False,
            "as_of": "2026-08-25T20:00:00+08:00",
            "status_counts": {"pending": 6, "completed": 2},
            "task_summaries": [
                {"task_id": f"task-{index}", "title": f"任务{index}", "status": "pending", "closure_events": [{"large": "x" * 2000}]}
                for index in range(8)
            ],
        },
    }, ensure_ascii=False)

    projected = compact_tool_result_for_model(tool_name="tuoguan_query_tasks", args={}, result=raw)
    assert projected is not None and len(projected) < 4000
    data = json.loads(projected)["data"]
    assert data["total_count"] == 8
    assert data["returned_count"] == 5
    assert data["truncated"] is True
    assert "closure_events" not in projected


def test_model_visible_student_projection_marks_sample_without_claiming_all_students():
    from plugins.tuoguan_core.runtime_foundation import compact_tool_result_for_model

    raw = json.dumps({
        "ok": True,
        "data": {
            "legacy_tool": "tuoguan_query_students",
            "result_scope": "sample",
            "total_count": 153,
            "returned_count": 5,
            "truncated": True,
            "as_of": "2026-08-25T20:00:00+08:00",
            "students": [{
                "name": "学生甲",
                "profile": {"grade": "二年级", "parent_phone": "13800000000"},
                "recent_records": [{"recorded_at": "2026-07-12", "record_type": "学习"}],
            }],
        },
    }, ensure_ascii=False)

    projected = compact_tool_result_for_model(tool_name="tuoguan_query_students", args={}, result=raw)
    assert projected is not None
    data = json.loads(projected)["data"]
    assert data["result_scope"] == "sample"
    assert data["total_count"] == 153
    assert "parent_phone" not in projected


def test_wecom_internal_context_status_is_suppressed_before_network_send():
    from plugins.platforms.wecom.callback_adapter import _is_internal_context_status

    assert _is_internal_context_status("📦 Preflight compression: 40,000 tokens") is True
    assert _is_internal_context_status("上下文压缩失败，请等待") is True
    assert _is_internal_context_status("今天请查看学生记录制度草案。") is False

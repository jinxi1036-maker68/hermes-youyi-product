from __future__ import annotations


def test_simple_context_is_hard_bounded_and_keeps_identity_contract():
    from plugins.tuoguan_core.model_context_budget import (
        SIMPLE_CONTEXT_CHAR_BUDGET,
        ContextSection,
        render_bounded_context,
    )

    context, sources, meta = render_bounded_context(
        [
            ContextSection("task_companion", "任务材料" * 1000),
            ContextSection("xiaoyou_core_skill", "小优身份契约" * 1000),
            ContextSection("public_employee_identity", "可信身份" * 1000, first=True),
            ContextSection("work_context_snapshot", "活动线程" * 1000),
        ],
        raw_text="你好",
    )

    assert len(context) <= SIMPLE_CONTEXT_CHAR_BUDGET
    assert sources[:2] == ["public_employee_identity", "xiaoyou_core_skill"]
    assert meta["request_complexity"] == "simple"


def test_complex_context_can_retain_task_material_without_exceeding_cap():
    from plugins.tuoguan_core.model_context_budget import (
        COMPLEX_CONTEXT_CHAR_BUDGET,
        ContextSection,
        render_bounded_context,
    )

    context, sources, meta = render_bounded_context(
        [
            ContextSection("xiaoyou_core_skill", "身份" * 600),
            ContextSection("task_companion", "当前任务" * 800),
            ContextSection("work_context_snapshot", "当前活动" * 800),
        ],
        raw_text="我今天有什么任务吗",
    )

    assert len(context) <= COMPLEX_CONTEXT_CHAR_BUDGET
    assert "task_companion" in sources
    assert meta["request_complexity"] == "complex"

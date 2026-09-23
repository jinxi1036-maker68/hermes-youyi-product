from __future__ import annotations

from plugins.tuoguan_core.active_work_context import query_active_work_context
from plugins.tuoguan_core.models import UserIdentity
from plugins.tuoguan_core.store import TuoguanStore
from plugins.tuoguan_core.tools import (
    TUOGUAN_QUERY_ACTIVE_WORK_CONTEXT_SCHEMA,
    TUOGUAN_QUERY_TASKS_SCHEMA,
)


def _boss() -> UserIdentity:
    return UserIdentity(
        platform="wecom_callback",
        platform_user_id="boss-test",
        canonical_user_id="boss-test",
        person_name="测试老板",
        role="boss",
        approval_state="approved",
    )


def test_active_work_context_contract_is_not_task_inventory() -> None:
    active_description = TUOGUAN_QUERY_ACTIVE_WORK_CONTEXT_SCHEMA["description"]
    active_limit = TUOGUAN_QUERY_ACTIVE_WORK_CONTEXT_SCHEMA["parameters"]["properties"]["limit"]["description"]
    task_description = TUOGUAN_QUERY_TASKS_SCHEMA["description"]

    assert "不是任务库存" in active_description
    assert "返回 0 条只表示没有可供本轮衔接的上下文证据" in active_description
    assert "不是任务数量" in active_limit
    assert "任务库存" in task_description
    assert "权威只读入口" in task_description


def test_zero_active_context_cannot_support_zero_task_claim(tmp_path) -> None:
    store = TuoguanStore(tmp_path)

    result = query_active_work_context(store, identity=_boss(), limit=5)

    assert result["ok"] is True
    assert result["context_scope"] == "conversation_continuation_only"
    assert result["authoritative_for_task_inventory"] is False
    assert result["authoritative_for_task_count"] is False
    assert result["authoritative_for_date_scoped_tasks"] is False
    assert result["context_count"] == 0
    assert "不代表任务数量" in result["rendered_text"]
    assert "不能据此判断是否还有待办、今日任务或机构范围内任务" in result["rendered_text"]


def test_active_context_count_remains_context_count_when_task_focus_exists(tmp_path) -> None:
    store = TuoguanStore(tmp_path)
    store.write_json(
        "tasks.json",
        [
            {
                "id": "task-current",
                "title": "当前焦点任务",
                "assignee_userid": "boss-test",
                "status": "pending",
                "updated_at": "2026-09-23T22:00:00+08:00",
            }
        ],
    )
    store.write_json(
        "active_task_context.json",
        {
            "boss-test": {
                "task_id": "task-current",
            }
        },
    )

    result = query_active_work_context(store, identity=_boss(), limit=5)

    assert result["ok"] is True
    assert result["context_count"] == 1
    assert result["contexts"][0]["context_type"] == "task"
    assert result["authoritative_for_task_count"] is False
    assert "这个数量只表示可供当前对话衔接的近期上下文" in result["rendered_text"]

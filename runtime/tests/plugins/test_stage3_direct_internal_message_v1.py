from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace


def _write(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _base_directory(path: Path) -> None:
    _write(path, "wecom_whitelist.json", {
        "super_users": ["owner"],
        "allowed_users": ["owner", "manager", "teacher_a", "teacher_b"],
        "user_roles": {
            "owner": "boss", "manager": "manager",
            "teacher_a": "teacher", "teacher_b": "teacher",
        },
        "wecom_contacts": {
            "owner": {}, "manager": {}, "teacher_a": {}, "teacher_b": {},
        },
        "pending_users": [], "rejected_users": [], "offboarded_users": [],
    })
    _write(path, "staff.json", {
        "owner": {"name": "老板", "role": "boss", "status": "active", "campus_ids": ["c1", "c2"]},
        "manager": {"name": "一店店长", "role": "manager", "status": "active", "campus_ids": ["c1"]},
        "teacher_a": {"name": "王老师", "role": "teacher", "status": "active", "campus_ids": ["c1"]},
        "teacher_b": {"name": "李老师", "role": "teacher", "status": "active", "campus_ids": ["c2"]},
    })


def test_stage3_tool_is_explicit_fast_path_and_not_read_bundle_eligible():
    from plugins.tuoguan_core.capability_facades import FAST_PATH_TOOL_NAMES, operation_manifest
    from plugins.tuoguan_core.tools import model_tools

    assert "tuoguan_send_internal_message" in FAST_PATH_TOOL_NAMES
    manifest = operation_manifest()
    assert manifest["operations"]["send_internal_message"]["access"] == "write"
    names = {name for name, _schema, _handler in model_tools("facade")}
    assert "tuoguan_send_internal_message" in names


def test_stage3_schema_is_one_to_one_internal_human_command():
    from plugins.tuoguan_core.tools import TUOGUAN_SEND_INTERNAL_MESSAGE_SCHEMA

    params = TUOGUAN_SEND_INTERNAL_MESSAGE_SCHEMA["parameters"]
    assert set(params["required"]) == {"target_name", "message"}
    assert "recipients" not in params["properties"]
    assert "group_id" not in params["properties"]
    description = TUOGUAN_SEND_INTERNAL_MESSAGE_SCHEMA["description"]
    assert "人类" in description
    assert "不是小优自主触达" in description
    assert "家长消息或群发" in description


def test_human_command_delivery_does_not_require_proactive_grant_but_requires_current_internal_binding(tmp_path, monkeypatch):
    from plugins.tuoguan_core.proactive_delivery_authority import ProactiveDeliveryAuthority
    from plugins.tuoguan_core.work_runtime import ReplyDestination

    monkeypatch.setenv("HERMES_TENANT_ID", "tenant")
    _base_directory(tmp_path)
    authority = ProactiveDeliveryAuthority(tmp_path)
    destination = ReplyDestination(
        tenant_id="tenant",
        channel="wecom_callback",
        recipient_id="teacher_a",
        source_identity="human_command:tenant:owner",
    )
    allowed = authority.decide(destination)
    assert allowed.allowed
    assert allowed.reason == "explicit_human_command_internal_recipient"

    data = json.loads((tmp_path / "wecom_whitelist.json").read_text(encoding="utf-8"))
    data["wecom_contacts"].pop("teacher_a")
    _write(tmp_path, "wecom_whitelist.json", data)
    blocked = authority.decide(destination)
    assert not blocked.allowed
    assert blocked.reason == "human_command_recipient_wecom_binding_missing"


def test_human_command_delivery_rechecks_sender_and_recipient_lifecycle(tmp_path, monkeypatch):
    from plugins.tuoguan_core.proactive_delivery_authority import ProactiveDeliveryAuthority
    from plugins.tuoguan_core.work_runtime import ReplyDestination

    monkeypatch.setenv("HERMES_TENANT_ID", "tenant")
    _base_directory(tmp_path)
    authority = ProactiveDeliveryAuthority(tmp_path)
    destination = ReplyDestination("tenant", "wecom_callback", "teacher_a", "human_command:tenant:owner")
    assert authority.decide(destination).allowed

    _write(tmp_path, "staff.json", {
        "owner": {"name": "老板", "role": "boss", "status": "active"},
        "teacher_a": {"name": "王老师", "role": "teacher", "status": "left"},
    })
    decision = authority.decide(destination)
    assert not decision.allowed


def test_stage_human_command_notice_is_idempotent_and_content_exact(tmp_path):
    from plugins.tuoguan_core.direct_reply_recovery import DirectReplyRecoveryManager

    manager = DirectReplyRecoveryManager(root=tmp_path)
    first = manager.stage_human_command_notice(
        tenant_id="tenant", actor_id="owner", actor_role="boss",
        recipient_id="teacher_a", notice_id="message-1",
        notice_text="王老师，今天下班前把结果发我。",
        trace_ref="stage3-test",
    )
    second = manager.stage_human_command_notice(
        tenant_id="tenant", actor_id="owner", actor_role="boss",
        recipient_id="teacher_a", notice_id="message-1",
        notice_text="王老师，今天下班前把结果发我。",
        trace_ref="stage3-test",
    )
    assert first.reply_id == second.reply_id
    assert first.delivery_id == second.delivery_id
    assert first.reply_text == "王老师，今天下班前把结果发我。"
    assert first.delivery_state == "pending"


def test_wecom_port_fails_closed_for_human_command_when_authority_unavailable():
    from plugins.tuoguan_core.wecom_outbox_port import WeComCallbackOutboxPort
    from plugins.tuoguan_core.work_runtime import ReplyDestination

    class Client:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("no network request expected")
    port = WeComCallbackOutboxPort(
        corp_id="corp", corp_secret="secret", agent_id="1",
        client=Client(), proactive_authority=None,
    )
    outcome = asyncio.run(port.send_reply(
        destination=ReplyDestination("tenant", "wecom_callback", "teacher_a", "human_command:tenant:owner"),
        reply_text="测试", delivery_id="delivery-1",
    ))
    assert outcome.disposition == "failed"
    assert outcome.error == "wecom_proactive_authority_unavailable"


def test_manager_scope_is_fail_closed_and_only_own_teacher(tmp_path, monkeypatch):
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    monkeypatch.setenv("HERMES_TENANT_ID", "tenant")
    _base_directory(tmp_path)
    service = TuoguanToolService(
        TuoguanStore(tmp_path),
        platform="wecom_callback", user_id="manager",
        identity=UserIdentity("wecom_callback", "manager", "manager", "一店店长", "manager", "approved"),
    )
    teacher_a, error_a = service._resolve_internal_message_target(target_name="王老师")
    teacher_b, error_b = service._resolve_internal_message_target(target_name="李老师")
    assert error_a is None and error_b is None
    assert service._manager_can_send_internal_message_to(teacher_a)
    assert not service._manager_can_send_internal_message_to(teacher_b)
    manager, manager_error = service._resolve_internal_message_target(target_name="一店店长")
    assert manager_error is None
    assert not service._manager_can_send_internal_message_to(manager)


def test_teacher_cannot_direct_message_other_staff(tmp_path):
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    _base_directory(tmp_path)
    service = TuoguanToolService(
        TuoguanStore(tmp_path),
        platform="wecom_callback", user_id="teacher_a",
        identity=UserIdentity("wecom_callback", "teacher_a", "teacher_a", "王老师", "teacher", "approved"),
    )
    result = service.send_internal_message(
        target_name="李老师", message="请把材料给我。", operation_id="op-1",
    )
    assert result["ok"] is False
    assert result["error"] == "permission_denied"


def test_stage3_reply_truth_never_upgrades_queue_to_sent():
    from plugins.tuoguan_core.runtime_foundation import _sanitize_external_reply

    queued = _sanitize_external_reply(
        "已经发给王老师了。",
        outreach_state="queued",
        outreach_guard_applies=True,
    )
    assert queued == "这条消息已经安排发送，但目前只有入队回执，是否送达还没有确认。"

    no_receipt = _sanitize_external_reply(
        "已经发给王老师了。",
        outreach_state="",
        outreach_guard_applies=True,
    )
    assert no_receipt == "当前没有真实发送回执，不能说已经通知对方。"

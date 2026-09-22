"""Release-gate proof that Agenda work material cannot become WeCom text."""

from __future__ import annotations

from tuoguan_core.direct_reply_recovery import DirectReplyRecoveryManager
from tuoguan_core.tools import agenda_governance_tools, agenda_service_tools, agenda_task_tools
from tuoguan_core.work_runtime import ReplyDestination, TrustedPartition


def _capture(manager: DirectReplyRecoveryManager) -> tuple[TrustedPartition, ReplyDestination]:
    partition = TrustedPartition(
        partition_id="agenda-partition-1",
        tenant_id="tenant-agenda-boundary",
        canonical_actor_id="service:agenda:tenant-agenda-boundary",
        principal_kind="service",
        trusted_session_id="agenda-session-1",
        continuity_id=None,
        source_identity="agenda:tenant-agenda-boundary",
    )
    destination = ReplyDestination(
        tenant_id="tenant-agenda-boundary",
        channel="wecom_callback",
        recipient_id="wx-owner",
        source_identity="agenda:tenant-agenda-boundary",
    )
    manager.capture_agenda_turn(
        ticket_id="agenda-ticket-boundary-1",
        agent_session_id="agenda-agent-session-1",
        agent_turn_id="agenda-turn-1",
        tenant_id="tenant-agenda-boundary",
        service_identity="service:agenda:tenant-agenda-boundary",
        partition=partition,
        destination=destination,
        raw_text='{"facts":[{"kind":"governance_claim_awaiting_confirmation","claim_id":"claim_internal"}]}',
        operation_hint="agenda-message:agenda-ticket-boundary-1",
    )
    return partition, destination


def test_agenda_raw_work_material_is_not_a_notice_without_explicit_user_message(tmp_path) -> None:
    manager = DirectReplyRecoveryManager(tmp_path)
    _capture(manager)

    job = manager.stage_agenda_ticket_terminal(
        ticket_id="agenda-ticket-boundary-1",
        terminal_state="completed",
        provider_succeeded=True,
        final_reply_text="governance_claim_awaiting_confirmation: claim_internal agenda_ticket: agenda-ticket-boundary-1",
        raw_trace_ref="test:raw-internal-terminal",
    )

    assert job is None


def test_each_trusted_agenda_surface_exposes_only_its_explicit_message_artifact() -> None:
    assert "agenda_service_publish_user_message" in {name for name, _schema, _handler in agenda_service_tools()}
    assert "agenda_task_publish_user_message" in {name for name, _schema, _handler in agenda_task_tools()}
    assert "agenda_governance_publish_user_message" in {name for name, _schema, _handler in agenda_governance_tools()}


def test_agenda_explicit_model_user_message_is_the_only_no_receipt_notice(tmp_path) -> None:
    manager = DirectReplyRecoveryManager(tmp_path)
    _capture(manager)
    prepared = manager.prepare_agenda_user_message(
        agent_session_id="agenda-agent-session-1",
        agent_turn_id="agenda-turn-1",
        text="有一项人员信息需要您确认，我会在收到确认后继续处理。",
    )
    assert prepared["state"] == "prepared"

    job = manager.stage_agenda_ticket_terminal(
        ticket_id="agenda-ticket-boundary-1",
        terminal_state="completed",
        provider_succeeded=True,
        final_reply_text="runtime_context: this raw terminal text must not be delivered",
        raw_trace_ref="test:typed-user-message-terminal",
    )

    assert job is not None
    assert job.reply_text == "有一项人员信息需要您确认，我会在收到确认后继续处理。"
    assert "runtime_context" not in job.reply_text
    assert "agenda-ticket" not in job.reply_text

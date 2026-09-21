#!/usr/bin/env python3
"""Permanent no-provider regression for XiaoYou outbound Reply Truth.

Run against a Hermes 0.21 checkout with:
``XIAOYOU_TEST_HERMES_RUNTIME_ROOT=/path/to/hermes-agent python runtime/tests/production/reply_truth_runtime_regression.py``.
It deliberately sends no external message.  The live WeCom acceptance check
remains a separate audited deployment validation.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


HERE = Path(__file__).resolve().parent
CAPABILITY_ROOT = HERE.parents[1]
RUNTIME_ROOT = os.environ.get("XIAOYOU_TEST_HERMES_RUNTIME_ROOT")
if not RUNTIME_ROOT:
    print("SKIP reply truth regression: set XIAOYOU_TEST_HERMES_RUNTIME_ROOT to a Hermes 0.21 checkout")
    raise SystemExit(0)
RUNTIME_ROOT_PATH = Path(RUNTIME_ROOT).resolve()
sys.path.insert(0, str(RUNTIME_ROOT_PATH))
sys.path.insert(0, str(CAPABILITY_ROOT / "plugins"))

import yaml

from tuoguan_core.direct_reply_recovery import DirectReplyRecoveryManager
from tuoguan_core.proactive_delivery_authority import ProactiveDeliveryAuthority
from tuoguan_core.wecom_outbox_port import WeComCallbackOutboxPort
from tuoguan_core.work_runtime import ReplyDestination


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def json(self) -> dict[str, object]:
        return self.payload


class _WeComAcceptedClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    async def get(self, *_args: object, **_kwargs: object) -> _Response:
        return _Response({"errcode": 0, "access_token": "test-token", "expires_in": 7200})

    async def post(self, *_args: object, **kwargs: object) -> _Response:
        self.posts.append(dict(kwargs["json"]))
        return _Response({"errcode": 0, "msgid": "accepted-by-test-transport"})


class _HeldJob:
    delivery_id = "delivery_test"

    def __init__(self, reply_id: str) -> None:
        self.reply_id = reply_id


class _MarkerRecovery:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release_wecom_callback_handoff(self, *, reply_id: str) -> _HeldJob:
        self.released.append(reply_id)
        return _HeldJob(reply_id)


def main() -> None:
    config = yaml.safe_load((HERE.parent / "fixtures" / "hermes_display_config.yaml").read_text(encoding="utf-8")) or {}
    # Hermes' documented config bridge suppresses redirect/queue/interrupt
    # technical acknowledgements before any platform send is attempted.
    assert config.get("display", {}).get("busy_ack_enabled") is False

    with tempfile.TemporaryDirectory(prefix="xiaoyou-reply-truth-") as temporary:
        data = Path(temporary) / "data"
        data.mkdir()
        (data / "wecom_whitelist.json").write_text(json.dumps({
            "super_users": ["owner"],
            "allowed_users": ["owner", "teacher"],
            "user_roles": {"owner": "super_admin", "teacher": "teacher"},
            "wecom_contacts": {"owner": {}, "teacher": {}},
            "pending_users": [], "rejected_users": [], "offboarded_users": [],
        }), encoding="utf-8")
        authority = ProactiveDeliveryAuthority(data)
        authority.grant_institutional_wecom_delivery(tenant_id="tenant", authorized_by="owner")
        authority.upgrade_scope_to_current_runtime(tenant_id="tenant")
        destination = ReplyDestination("tenant", "wecom_callback", "teacher", "task_delivery:tenant")
        assert authority.decide(destination).allowed
        assert not authority.decide(ReplyDestination("tenant", "wecom_callback", "missing", "task_delivery:tenant")).allowed
        # An explicit personnel offboarding fact must override a formerly
        # trusted/bound directory entry; an absent legacy profile must not.
        (data / "staff.json").write_text(json.dumps({"teacher": {"status": "left"}}), encoding="utf-8")
        blocked = authority.decide(destination)
        assert not blocked.allowed and blocked.reason == "proactive_recipient_staff_left"
        (data / "staff.json").write_text(json.dumps({"teacher": {"status": "active"}}), encoding="utf-8")
        assert authority.decide(destination).allowed

        manager = DirectReplyRecoveryManager(root=data)
        # A public WeCom callback's chat/session id is opaque Agent state; it
        # must never become an outbound recipient.  The authenticated actor
        # is the sole delivery binding for a direct WeCom reply.
        direct_destination = manager._destination(SimpleNamespace(
            platform="wecom_callback", tenant_id="tenant", chat_id="opaque-agent-session",
            continuity_key="", identity=SimpleNamespace(canonical_user_id="teacher"),
        ))
        assert direct_destination.recipient_id == "teacher"
        assert direct_destination.source_identity == "wecom_callback:teacher"
        job = manager.stage_proactive_notice(
            tenant_id="tenant", recipient_id="teacher", source_kind="task_delivery",
            notice_id="task:one:created", notice_text="已验证的任务通知", trace_ref="regression",
        )
        assert job.delivery_state == "pending"
        # Stable notice identity prevents a replay from creating a second job.
        assert manager.stage_proactive_notice(
            tenant_id="tenant", recipient_id="teacher", source_kind="task_delivery",
            notice_id="task:one:created", notice_text="已验证的任务通知", trace_ref="regression",
        ).reply_id == job.reply_id

        client = _WeComAcceptedClient()
        port = WeComCallbackOutboxPort(
            corp_id="corp", corp_secret="secret", agent_id="1", client=client, proactive_authority=authority,
        )
        outcome = asyncio.run(port.send_reply(destination=destination, reply_text=job.reply_text, delivery_id=job.delivery_id))
        assert outcome.disposition == "delivered"
        assert client.posts == [{"touser": "teacher", "msgtype": "text", "agentid": 1, "text": {"content": job.reply_text}, "safe": 0}]

        # A durable reply locator is a transport control reference, not a
        # person-facing body.  Exercise the real public WeCom adapter's
        # `send` entrypoint with a fake held job: it must consume the marker
        # before app resolution or any HTTP send.  This permanently covers
        # the historical raw `xiaoyou-reply-outbox:reply_...` leak.
        from gateway.config import PlatformConfig
        import plugins.platforms as _platforms_package
        _platforms_package.__path__.append(str(CAPABILITY_ROOT / "plugins" / "platforms"))
        import plugins.platforms.wecom as _wecom_package
        _wecom_package.__path__.insert(0, str(CAPABILITY_ROOT / "plugins" / "platforms" / "wecom"))
        from plugins.platforms.wecom import callback_adapter
        from tuoguan_core import direct_reply_recovery

        marker_recovery = _MarkerRecovery()
        original_import = callback_adapter._import_tuoguan_module
        callback_adapter._import_tuoguan_module = lambda name: type("RecoveryModule", (), {
            "get_direct_reply_recovery_manager": staticmethod(lambda: marker_recovery),
            "parse_control_marker": staticmethod(direct_reply_recovery.parse_control_marker),
        })
        try:
            adapter = callback_adapter.WecomCallbackAdapter(PlatformConfig(enabled=True, extra={}))
            marker = direct_reply_recovery.control_marker("reply_" + "a" * 32)
            result = asyncio.run(adapter.send(chat_id="teacher", content=marker))
            assert bool(getattr(result, "success", getattr(result, "ok", False)))
            assert marker_recovery.released == ["reply_" + "a" * 32]
            assert adapter._http_client is None
        finally:
            callback_adapter._import_tuoguan_module = original_import
    print("reply_truth_runtime_regression: PASS")


if __name__ == "__main__":
    main()

"""XiaoYou-owned durable WeCom delivery port.

This is intentionally an *outbox* transport, not a WeCom inbound adapter and
not an Agent.  It receives only a server-attested destination plus text that
Hermes has already generated and the Work Runtime has durably recorded.  It
cannot inspect an original message, select a recipient, create wording,
invoke a Tool, or alter business truth.
"""

from __future__ import annotations

from hashlib import sha256
import os
import time
from typing import Any

from .work_runtime import ReplyDestination, WorkRuntimeRejected
from .work_runtime_channels import (
    ChannelDeliveryOutcome,
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_UNKNOWN,
    _trace_ref,
)
from .proactive_delivery_authority import ProactiveDeliveryAuthority


_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"


class WeComCallbackOutboxPort:
    """Send one already-generated outbox reply through one configured app.

    Credentials are read only from the server-owned EnvironmentFile.  They
    are never copied into the Institution Workspace, reply job, model prompt,
    Capability configuration, or trace output.
    """

    channel = "wecom_callback"

    def __init__(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
        client: Any,
        proactive_authority: ProactiveDeliveryAuthority | None = None,
        now: callable = time.time,
    ) -> None:
        self._corp_id = str(corp_id)
        self._corp_secret = str(corp_secret)
        self._agent_id = str(agent_id)
        self._client = client
        # This boundary is intentionally evaluated only after the Work
        # Runtime has durably bound tenant/channel/recipient/source.  It has
        # no user text and cannot select a recipient or create a reply.
        self._proactive_authority = proactive_authority
        self._now = now
        self._access_token = ""
        self._access_token_expires_at = 0.0

    @classmethod
    def from_environment(cls) -> "WeComCallbackOutboxPort | None":
        """Build from the already-authorized WeCom callback service config."""

        values = {
            "corp_id": str(os.getenv("WECOM_CALLBACK_CORP_ID") or "").strip(),
            "corp_secret": str(os.getenv("WECOM_CALLBACK_CORP_SECRET") or "").strip(),
            "agent_id": str(os.getenv("WECOM_CALLBACK_AGENT_ID") or "").strip(),
        }
        if not all(values.values()):
            return None
        try:
            import httpx
        except ImportError:
            return None
        proxy = str(os.getenv("HERMES_WECOM_PROXY_URL") or "").strip() or None
        kwargs: dict[str, Any] = {"timeout": 20.0}
        if proxy:
            # Channel-local proxying only.  Provider traffic never reaches
            # this code or inherits this transport choice.
            kwargs["proxy"] = proxy
        try:
            authority: ProactiveDeliveryAuthority | None = ProactiveDeliveryAuthority()
        except WorkRuntimeRejected:
            # Keep the port present so a current direct reply remains
            # deliverable; a proactive Agenda job is separately and
            # truthfully rejected below until the owner grant is persisted.
            authority = None
        return cls(client=httpx.AsyncClient(**kwargs), proactive_authority=authority, **values)

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def _token(self) -> tuple[str, str]:
        if self._access_token and self._access_token_expires_at > self._now() + 60:
            return self._access_token, ""
        try:
            response = await self._client.get(
                _TOKEN_URL,
                params={"corpid": self._corp_id, "corpsecret": self._corp_secret},
            )
            payload = response.json()
        except Exception as exc:
            return "", "token_transport:" + type(exc).__name__
        if not isinstance(payload, dict):
            return "", "token_response_unparseable"
        if int(payload.get("errcode") or 0) != 0:
            # This is a definitive token refusal before any message send.
            return "", "token_rejected:" + str(payload.get("errcode"))
        token = str(payload.get("access_token") or "").strip()
        if not token:
            return "", "token_missing"
        self._access_token = token
        self._access_token_expires_at = self._now() + max(60, int(payload.get("expires_in") or 7200))
        return token, ""

    async def send_reply(
        self,
        *,
        destination: ReplyDestination,
        reply_text: str,
        delivery_id: str,
    ) -> ChannelDeliveryOutcome:
        if destination.channel != self.channel:
            raise WorkRuntimeRejected("reply_destination_channel_mismatch")
        destination.canonical()
        if not str(reply_text or "").strip() or not str(delivery_id or "").strip():
            raise WorkRuntimeRejected("reply_delivery_required_fields_missing")
        # Every proactive Runtime origin is checked against the one
        # institution-level authority.  Direct human replies retain their
        # authenticated ingress binding and are intentionally outside this
        # branch.  The authority itself validates the typed source and tenant;
        # this port never chooses a person, text, or business operation.
        if self._proactive_authority is not None:
            decision = self._proactive_authority.decide(destination)
            if not decision.allowed:
                return ChannelDeliveryOutcome(
                    DELIVERY_FAILED,
                    error="wecom_" + decision.reason,
                    trace_ref=_trace_ref(channel=self.channel, delivery_id=delivery_id, stage="failed", detail=decision.reason),
                )
        elif str(destination.source_identity or "").split(":", 1)[0] in {"agenda", "task_delivery", "relationship_touch"}:
            if self._proactive_authority is None:
                return ChannelDeliveryOutcome(
                    DELIVERY_FAILED,
                    error="wecom_proactive_authority_unavailable",
                    trace_ref=_trace_ref(channel=self.channel, delivery_id=delivery_id, stage="failed", detail="proactive_authority_unavailable"),
                )
        token, token_error = await self._token()
        if token_error:
            disposition = DELIVERY_UNKNOWN if token_error.startswith("token_transport:") else DELIVERY_FAILED
            return ChannelDeliveryOutcome(
                disposition,
                error="wecom_" + token_error,
                trace_ref=_trace_ref(channel=self.channel, delivery_id=delivery_id, stage=disposition, detail=token_error),
            )
        recipient = str(destination.recipient_id)
        # Callback inbound identities may be scoped as ``corp:user``.  The
        # verified destination still owns that scope; only the public WeCom
        # API's expected user segment is extracted here.
        touser = recipient.split(":", 1)[1] if ":" in recipient else recipient
        try:
            response = await self._client.post(
                _SEND_URL + "?access_token=" + token,
                json={
                    "touser": touser,
                    "msgtype": "text",
                    "agentid": int(self._agent_id),
                    "text": {"content": str(reply_text)[:2048]},
                    "safe": 0,
                },
            )
            payload = response.json()
        except Exception as exc:
            error = "wecom_send_transport:" + type(exc).__name__
            # A request may have reached WeCom before its result was lost.
            # Quarantine it as unknown instead of risking a duplicate send.
            return ChannelDeliveryOutcome(
                DELIVERY_UNKNOWN,
                error=error,
                trace_ref=_trace_ref(channel=self.channel, delivery_id=delivery_id, stage="unknown", detail=error),
            )
        if not isinstance(payload, dict):
            return ChannelDeliveryOutcome(
                DELIVERY_UNKNOWN,
                error="wecom_send_response_unparseable",
                trace_ref=_trace_ref(channel=self.channel, delivery_id=delivery_id, stage="unknown", detail="response_unparseable"),
            )
        code = int(payload.get("errcode") or 0)
        if code != 0:
            # A structured WeCom response is a rejection of this exact
            # delivery attempt, not an ambiguous remote timeout.
            error = (
                "wecom_send_recipient_rejected:" + str(code)
                if code == 81013 else "wecom_send_rejected:" + str(code)
            )
            return ChannelDeliveryOutcome(
                DELIVERY_FAILED,
                error=error,
                trace_ref=_trace_ref(channel=self.channel, delivery_id=delivery_id, stage="failed", detail=error),
            )
        message_id = str(payload.get("msgid") or "")
        return ChannelDeliveryOutcome(
            DELIVERY_DELIVERED,
            channel_message_id=message_id,
            trace_ref=_trace_ref(
                channel=self.channel,
                delivery_id=delivery_id,
                stage="accepted",
                detail=sha256(message_id.encode("utf-8")).hexdigest()[:16],
            ),
        )

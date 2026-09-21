"""Content-blind reply delivery ports for the XiaoYou Work Runtime.

This module deliberately sits *after* a verified reply has been stored in the
durable outbox.  It does not consume inbound text, select a Tool, create a
business response, invoke Hermes, or decide whether an operation succeeded.
It can only take one server-attested :class:`ReplyDestination` and ask a
registered public platform adapter to deliver the already-produced text.

The deliberately conservative ``unknown`` outcome is important.  A timeout
after a channel request begins is not proof that the channel did not show the
reply.  The outbox therefore fences it for explicit channel readback rather
than automatically producing a duplicate message (and, critically, it never
replays the completed business operation).
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import inspect
from typing import Any, Mapping, Protocol

from .work_runtime import DurableReplyOutbox, ReplyDestination, ReplyJob, WorkRuntimeRejected


DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"
DELIVERY_UNKNOWN = "unknown"
_KNOWN_DELIVERY_STATES = frozenset({DELIVERY_DELIVERED, DELIVERY_FAILED, DELIVERY_UNKNOWN})


@dataclass(frozen=True)
class ChannelDeliveryOutcome:
    """A transport outcome; never a declaration about business truth."""

    disposition: str
    channel_message_id: str = ""
    error: str = ""
    trace_ref: str = ""

    def validate(self) -> None:
        if self.disposition not in _KNOWN_DELIVERY_STATES:
            raise WorkRuntimeRejected("channel_delivery_disposition_invalid")
        if self.disposition == DELIVERY_DELIVERED and not str(self.trace_ref).strip():
            raise WorkRuntimeRejected("delivered_channel_outcome_requires_trace")
        if self.disposition == DELIVERY_UNKNOWN and (
            not str(self.error).strip() or not str(self.trace_ref).strip()
        ):
            raise WorkRuntimeRejected("unknown_channel_outcome_requires_error_and_trace")


@dataclass(frozen=True)
class ChannelDeliveryResult:
    """One outbox-only dispatch record suitable for a trace/report."""

    operation_id: str
    delivery_id: str
    channel: str
    disposition: str
    delivery_state: str
    channel_message_id: str = ""
    trace_ref: str = ""


class PublicPlatformAdapter(Protocol):
    """The stable public sending surface shared by platform adapters."""

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> object:
        ...


class TrustedReplyChannelPort(Protocol):
    """A statically registered transport port, never selected by the model."""

    channel: str

    async def send_reply(
        self,
        *,
        destination: ReplyDestination,
        reply_text: str,
        delivery_id: str,
    ) -> ChannelDeliveryOutcome:
        ...


def _trace_ref(*, channel: str, delivery_id: str, stage: str, detail: str = "") -> str:
    """Return a content-free correlation reference for a delivery attempt."""

    material = "\x1f".join((str(channel), str(delivery_id), str(stage), str(detail)[:240]))
    return "channel_delivery:" + sha256(material.encode("utf-8")).hexdigest()[:28]


def _failure_is_known_not_delivered(error: str) -> bool:
    """Only classify preflight rejections as safe delivery failures.

    A remote HTTP/SDK error can be emitted after a channel accepted a message,
    so it must remain ``unknown``.  These markers describe local preflight
    rejection before a remote send is attempted.
    """

    value = str(error or "").strip().lower()
    return any(
        marker in value
        for marker in (
            "not connected",
            "not_connected",
            "required",
            "scope_unresolved",
            "uncorrelated",
            "destination_mismatch",
            "channel_unregistered",
        )
    )


class AdapterReplyChannelPort:
    """Adapt an existing public platform ``send`` method without business logic.

    The ``channel`` is configured by the server when the port is registered;
    the model cannot choose it.  ``recipient_id`` was stored in the trusted
    binding at ingress, and no outbound recipient parameter is accepted here.
    The stable delivery id is passed only as opaque transport metadata.  A
    channel that does not support remote idempotency may ignore it; in that
    case uncertain outcomes remain quarantined rather than being re-sent.
    """

    def __init__(self, *, channel: str, adapter: PublicPlatformAdapter) -> None:
        normalized = str(channel or "").strip()
        if not normalized:
            raise ValueError("reply_channel_required")
        self.channel = normalized
        self._adapter = adapter

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
        if not str(reply_text).strip() or not str(delivery_id).strip():
            raise WorkRuntimeRejected("reply_delivery_required_fields_missing")
        metadata = {
            # Opaque correlation only.  This is neither a model argument nor a
            # channel routing choice, and adapters may use it for native
            # idempotency where their public API exposes that capability.
            "xiaoyou_delivery_id": str(delivery_id),
            "xiaoyou_reply_only": True,
        }
        try:
            result = self._adapter.send(
                chat_id=destination.recipient_id,
                content=reply_text,
                reply_to=None,
                metadata=metadata,
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            error = f"adapter_exception:{type(exc).__name__}"
            return ChannelDeliveryOutcome(
                DELIVERY_UNKNOWN,
                error=error,
                trace_ref=_trace_ref(channel=self.channel, delivery_id=delivery_id, stage="exception", detail=error),
            )

        # Hermes 0.20.0 calls the public field ``ok``; newer public adapters
        # expose ``success``.  The result is a transport fact in either case.
        success = bool(getattr(result, "success", getattr(result, "ok", False)))
        message_id = str(getattr(result, "message_id", "") or "")
        if success:
            return ChannelDeliveryOutcome(
                DELIVERY_DELIVERED,
                channel_message_id=message_id,
                trace_ref=_trace_ref(channel=self.channel, delivery_id=delivery_id, stage="accepted", detail=message_id),
            )
        error = str(getattr(result, "error", "") or "channel_send_failed")
        disposition = DELIVERY_FAILED if _failure_is_known_not_delivered(error) else DELIVERY_UNKNOWN
        return ChannelDeliveryOutcome(
            disposition,
            channel_message_id=message_id,
            error=error,
            trace_ref=_trace_ref(channel=self.channel, delivery_id=delivery_id, stage=disposition, detail=error),
        )


class DurableReplyDeliveryDispatcher:
    """Deliver one ready reply through a server-configured channel port.

    It is deliberately a tiny transport executor rather than a second Agent
    loop: it cannot see an original request, cannot create reply text, cannot
    choose a Tool, and has no dependency on Hermes internals.
    """

    def __init__(self, *, outbox: DurableReplyOutbox, ports: Mapping[str, TrustedReplyChannelPort]) -> None:
        self._outbox = outbox
        self._ports = {str(channel): port for channel, port in ports.items()}
        for channel, port in self._ports.items():
            if not channel or str(getattr(port, "channel", "")) != channel:
                raise ValueError("trusted_reply_port_channel_mismatch")

    async def deliver_next(self, *, worker_id: str, now: float) -> ChannelDeliveryResult | None:
        job = self._outbox.claim_delivery(worker_id=worker_id, now=now)
        if job is None:
            return None
        port = self._ports.get(job.destination.channel)
        if port is None:
            # No remote request has begun.  Keep this as a durable local
            # configuration state rather than cycling the same job at worker
            # speed; a trusted adapter registration explicitly requeues it.
            completed = self._outbox.complete_delivery_blocked(
                job,
                worker_id=worker_id,
                error="channel_unregistered",
                now=now,
            )
            return ChannelDeliveryResult(
                job.operation_id,
                job.delivery_id,
                job.destination.channel,
                DELIVERY_FAILED,
                completed.delivery_state,
            )
        try:
            outcome = await port.send_reply(
                destination=job.destination,
                reply_text=job.reply_text,
                delivery_id=job.delivery_id,
            )
            outcome.validate()
        except WorkRuntimeRejected:
            # A trusted binding mismatch is local and preflight-only: no
            # channel delivery was attempted, so it can remain retryable after
            # configuration is corrected.  It still cannot touch business.
            completed = self._outbox.complete_delivery(
                job,
                worker_id=worker_id,
                delivered=False,
                error="destination_mismatch",
                now=now,
            )
            return ChannelDeliveryResult(
                job.operation_id,
                job.delivery_id,
                job.destination.channel,
                DELIVERY_FAILED,
                completed.delivery_state,
            )

        if outcome.disposition == DELIVERY_UNKNOWN:
            completed = self._outbox.complete_delivery_unknown(
                job,
                worker_id=worker_id,
                error=outcome.error,
                delivery_trace_ref=outcome.trace_ref,
                channel_message_id=outcome.channel_message_id,
                now=now,
            )
        elif (
            outcome.disposition == DELIVERY_FAILED
            and str(outcome.error).startswith("wecom_send_recipient_rejected:")
        ):
            # The WeCom API conclusively rejected this server-bound recipient
            # before accepting a message. Park the delivery instead of
            # retrying it at worker speed. A trusted identity correction can
            # explicitly requeue the reply without replaying Hermes or any
            # business Tool; gateway restart must not turn a permanent reject
            # into a duplicate-send loop.
            completed = self._outbox.complete_delivery_blocked(
                job,
                worker_id=worker_id,
                error=str(outcome.error),
                now=now,
            )
        else:
            completed = self._outbox.complete_delivery(
                job,
                worker_id=worker_id,
                delivered=outcome.disposition == DELIVERY_DELIVERED,
                error=outcome.error,
                channel_message_id=outcome.channel_message_id,
                delivery_trace_ref=outcome.trace_ref,
                now=now,
            )
        return ChannelDeliveryResult(
            job.operation_id,
            job.delivery_id,
            job.destination.channel,
            outcome.disposition,
            completed.delivery_state,
            outcome.channel_message_id,
            outcome.trace_ref,
        )

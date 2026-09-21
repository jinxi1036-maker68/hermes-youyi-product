"""Internal public Platform Adapter for a same-Hermes reply-only turn.

It accepts no network input.  Its only source is a leased entry in the
XiaoYou durable reply outbox.  The inherited public ``handle_message`` path
runs the normal Hermes Agent Loop; configuration supplies a deliberately
empty Tool surface and tuoguan_core independently blocks every Tool as a
defence-in-depth fence.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

# These are protocol identifiers, not a business policy or a user-controlled
# channel field.  The manager below verifies the complete trusted binding.
RECOVERY_PLATFORM = "reply_recovery"
RECOVERY_SENDER = "reply-recovery-service"


def _recovery_module() -> Any:
    """Resolve the already-loaded XiaoYou package without a Core import.

    Hermes loads user plugins in its own package namespace, whereas the local
    isolated tests use ``plugins``.  Use the reply adapter's sibling namespace
    first so both adapters share the exact same durable manager singleton.
    This does not access a Hermes private API; it only resolves a sibling
    capability package that was installed together with this adapter.
    """

    package_root = str(__package__ or "").rsplit(".", 1)[0]
    prefixes = [
        f"{package_root}.tuoguan_core" if package_root else "",
        "hermes_plugins.tuoguan_core",
        "plugins.tuoguan_core",
    ]
    seen: set[str] = set()
    for prefix in prefixes:
        if not prefix or prefix in seen:
            continue
        seen.add(prefix)
        name = f"{prefix}.direct_reply_recovery"
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name not in {prefix.split(".", 1)[0], prefix, name}:
                raise
    raise RuntimeError("xiaoyou_direct_reply_recovery_capability_unavailable")


def _platform_value() -> Any:
    try:
        return Platform(RECOVERY_PLATFORM)
    except (TypeError, ValueError):
        return RECOVERY_PLATFORM


def _result(*, ok: bool, message_id: str = "", error: str = "") -> SendResult:
    try:
        return SendResult(success=ok, message_id=message_id, error=error)
    except TypeError:
        return SendResult(ok=ok, message_id=message_id, error=error)


def _framework_failure_text(value: str) -> bool:
    """Identify only Hermes' own terminal diagnostic, never user language."""

    lowered = str(value or "").strip().lower()
    return not lowered or any(
        marker in lowered
        for marker in (
            "model returned no content after all retries",
            "the request failed:",
            "processing completed but no response was generated",
            "the model provider failed after retries",
        )
    )


def check_reply_recovery_requirements() -> bool:
    return True


def validate_reply_recovery_config(config: PlatformConfig) -> bool:
    # This platform must never expose a socket, webhook, API key or client
    # configurable destination.  It is internal-only and reads no extra input.
    extra = getattr(config, "extra", {}) or {}
    return not bool(extra.get("inbound_endpoint") or extra.get("host") or extra.get("port"))


class ReplyRecoveryAdapter(BasePlatformAdapter):
    """Drain reply generation and delivery without a second Agent runtime."""

    supports_async_delivery = True
    interactive_resume = False

    def __init__(self, config: PlatformConfig) -> None:
        platform = _platform_value()
        try:
            super().__init__(config, platform)
        except TypeError:  # Small test stubs do not share the full Base API.
            self.config = config
            self.platform = platform
            self._message_handler = None
            self._running = False
        self._recovery = _recovery_module()
        self._manager = self._recovery.get_direct_reply_recovery_manager()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._session_for_chat: dict[str, str] = {}
        self._worker_id = "reply-recovery:" + hex(id(self))[-8:]
        self._wecom_delivery_port: Any | None = None

    def toolsets_for_source(self, source: SessionSource) -> list[str] | None:
        # ``no_mcp`` is the public config resolver's empty-MCP sentinel. The
        # saved server config additionally declares every known plugin
        # toolset for this platform and selects none, yielding a zero Tool
        # surface. Returning a nonempty list deliberately forces this public
        # resolver route rather than touching a GatewayRunner internal.
        return ["no_mcp"]

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._task is not None:
            return True
        # Delivery is a static, environment-owned channel transport.  It is
        # registered before the worker starts so previously blocked replies
        # can resume without replaying their Agent turn or business Tool.
        try:
            package_root = str(self._recovery.__name__).rsplit(".", 1)[0]
            port_module = importlib.import_module(package_root + ".wecom_outbox_port")
            port = port_module.WeComCallbackOutboxPort.from_environment()
            if port is not None:
                self._manager.register_delivery_port(port)
                self._wecom_delivery_port = port
                logger.info("reply-recovery registered current WeCom durable delivery port")
        except Exception:
            logger.exception("reply-recovery could not register current WeCom durable delivery port")
        self._stop.clear()
        self._running = True
        self._task = asyncio.create_task(self._drain(), name="xiaoyou-reply-recovery")
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._wecom_delivery_port is not None:
            try:
                await self._wecom_delivery_port.close()
            except Exception:
                logger.exception("reply-recovery could not close WeCom durable delivery port")
            self._wecom_delivery_port = None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"id": str(chat_id), "type": "internal_reply_recovery", "is_group": False}

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        session = self._session_for_chat.get(str(chat_id))
        if not session:
            return _result(ok=False, error="reply_recovery_uncorrelated")
        terminal_handled = False
        try:
            # Hermes may emit this framework-level onboarding notice before
            # the actual Agent reply.  It is neither a generated recovery
            # answer nor a business statement.  Keep the same recovery
            # session correlated so the later real ``send`` can complete the
            # durable outbox job.  This is deliberately a Hermes framework
            # marker, not a rule about user text or business semantics.
            if str(content or "").startswith("📬 No home channel is set for "):
                logger.info("reply-only recovery ignored framework home-channel notice chat=%s", chat_id)
                return _result(ok=True, message_id="reply_recovery_framework_notice_ignored")
            if _framework_failure_text(content):
                self._manager.fail_recovery(recovery_session_id=session, worker_id=self._worker_id, reason="framework_terminal_reply_unavailable")
                terminal_handled = True
                return _result(ok=False, error="reply_recovery_framework_terminal")
            job = self._manager.finish_recovery(
                recovery_session_id=session,
                reply_text=str(content),
                worker_id=self._worker_id,
            )
            terminal_handled = True
            return _result(ok=True, message_id=job.reply_id)
        except Exception:
            logger.exception("reply-only recovery terminal handling failed")
            return _result(ok=False, error="reply_recovery_terminal_failed")
        finally:
            if terminal_handled:
                self._session_for_chat.pop(str(chat_id), None)

    async def _drain(self) -> None:
        while not self._stop.is_set():
            progressed = False
            try:
                claimed = self._manager.claim_recovery(worker_id=self._worker_id)
                if claimed is not None:
                    _job, recovery_session, prompt = claimed
                    chat_id = "reply-recovery:" + _job.reply_id
                    self._session_for_chat[chat_id] = recovery_session
                    event = MessageEvent(
                        text=prompt,
                        message_type=MessageType.TEXT,
                        user_id=RECOVERY_SENDER,
                        user_name="XiaoYou Reply Recovery",
                        source=SessionSource(
                            platform=_platform_value(), chat_id=chat_id, chat_type="dm",
                            user_id=RECOVERY_SENDER, user_name="XiaoYou Reply Recovery", thread_id=_job.reply_id,
                        ),
                        message_id="reply-recovery:" + _job.reply_id,
                        raw_message={"reply_id": _job.reply_id, "internal": True},
                        internal=True,
                        allow_gateway_control=False,
                    )
                    # Public BasePlatformAdapter ingress only. It schedules a
                    # normal Gateway Agent Loop whose eventual ``send`` above
                    # records the reply; no Agent/Runner private method is used.
                    await self.handle_message(event)
                    progressed = True
                delivered = await self._manager.deliver_next(worker_id=self._worker_id)
                progressed = progressed or delivered is not None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("durable reply recovery worker iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.15 if progressed else 0.8)
            except asyncio.TimeoutError:
                continue

"""Loopback-only text ingress for the Robot Channel POC.

This adapter deliberately owns transport concerns only: local device
authentication, correlation ids, replay prevention, and reply delivery.  It
never interprets a business phrase or invokes a business tool itself.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import importlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource
from hermes_constants import get_hermes_home


logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "robot-poc-v0"
MAX_LINE_BYTES = 16_384
MAX_VOICE_LINE_BYTES = 8 * 1024 * 1024 + 32_768
MAX_TEXT_CHARS = 2_000
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
FORBIDDEN_CLIENT_IDENTITY_FIELDS = frozenset({"tenant_id", "actor_user_id", "role", "channel"})
VOICE_PROTOCOL_VERSION = "robot-poc-voice-v1"


class RobotPocRejected(ValueError):
    """A rejected transport request with a stable, client-safe code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedRobotIdentity:
    device_id: str
    tenant_id: str
    actor_user_id: str
    role: str
    token_version: int
    capabilities: tuple[str, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:20]


def _json_load(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return deepcopy(fallback)


def _atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw_path, path)
    finally:
        if os.path.exists(raw_path):
            os.unlink(raw_path)


def _platform_value() -> Any:
    try:
        return Platform("robot_poc")
    except (TypeError, ValueError):
        # The small project test stub intentionally knows only WeCom.  The real
        # Hermes 0.20.0 registry materializes registered platform names.
        return "robot_poc"


def _result(*, ok: bool, message_id: str = "", error: str = "") -> SendResult:
    try:
        return SendResult(success=ok, message_id=message_id, error=error)
    except TypeError:
        return SendResult(ok=ok, message_id=message_id, error=error)


async def _emit_progress(
    progress_sink: Callable[[dict[str, Any]], Any] | None,
    event: dict[str, Any],
) -> None:
    """Deliver a factual transport/lifecycle event without affecting work."""

    if progress_sink is None:
        return
    result = progress_sink(deepcopy(event))
    if asyncio.iscoroutine(result):
        await result


def _is_loopback(host: str) -> bool:
    candidate = str(host or "").strip()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _config_extra(config: PlatformConfig) -> dict[str, Any]:
    extra = getattr(config, "extra", None)
    if isinstance(extra, dict):
        return extra
    options = getattr(config, "options", None)
    return options if isinstance(options, dict) else {}


def _resolve_path(value: str, default_name: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return get_hermes_home() / "robot-poc" / default_name
    path = Path(raw).expanduser()
    return path if path.is_absolute() else get_hermes_home() / path


def _configured_auto_skills(value: Any) -> tuple[str, ...]:
    """Normalize trusted, static Hermes skill bindings from server config.

    This intentionally accepts no per-message or client-supplied value.  It is
    the same channel-level capability binding that Hermes already supports for
    Slack, Discord, and Telegram; it does not inspect text or choose a Tool.
    """

    candidates = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    result: list[str] = []
    for candidate in candidates:
        name = str(candidate or "").strip()
        if not SAFE_SKILL_NAME.fullmatch(name) or name in result:
            continue
        result.append(name)
        if len(result) >= 8:
            break
    return tuple(result)


def check_robot_poc_requirements() -> bool:
    """The Step A endpoint uses Python's standard library only."""

    return True


def validate_robot_poc_config(config: PlatformConfig) -> bool:
    extra = _config_extra(config)
    host = str(extra.get("host") or "127.0.0.1")
    try:
        port = int(extra.get("port") or 18761)
    except (TypeError, ValueError):
        return False
    return _is_loopback(host) and 1 <= port <= 65535


class RobotPocAdapter(BasePlatformAdapter):
    """A local text channel with server-side device binding and turn fencing."""

    supports_async_delivery = False
    interactive_resume = False

    def __init__(self, config: PlatformConfig) -> None:
        platform = _platform_value()
        try:
            super().__init__(config, platform)
        except TypeError:
            # Compatibility with the deliberately minimal local adapter stub.
            self.config = config
            self.platform = platform
            self._message_handler = None
            self._running = False
        extra = _config_extra(config)
        self.host = str(extra.get("host") or "127.0.0.1").strip()
        self.port = int(extra.get("port") or 18761)
        if not _is_loopback(self.host):
            raise ValueError("robot_poc_requires_loopback_host")
        self.registry_path = _resolve_path(str(extra.get("registry_path") or ""), "robot_device_registry.json")
        self.state_path = _resolve_path(str(extra.get("state_path") or ""), "robot_turn_state.json")
        self.response_timeout_seconds = max(5.0, min(float(extra.get("response_timeout_seconds") or 45.0), 120.0))
        self.identity_platform = str(extra.get("identity_platform") or "wecom_callback")
        self.auto_skills = _configured_auto_skills(extra.get("auto_skills"))
        self.voice_enabled = bool(extra.get("voice_enabled") is True)
        self.voice_port = int(extra.get("voice_port") or (self.port + 1))
        if self.voice_enabled and (not _is_loopback(self.host) or not 1 <= self.voice_port <= 65535):
            raise ValueError("robot_poc_voice_requires_loopback_port")
        # ``small`` is the tested POC baseline.  The former ``base`` default
        # materially degraded Mandarin names and educational vocabulary in
        # real-human-audio evaluation; this changes only STT, never business
        # interpretation or Tool selection.
        self.voice_stt_model = str(extra.get("voice_stt_model") or "small")
        self.voice_stt_runtime_site_packages = str(extra.get("voice_stt_runtime_site_packages") or "")
        self.voice_stt_model_dir = str(extra.get("voice_stt_model_dir") or "robot-poc/stt-model-cache")
        self._server: asyncio.AbstractServer | None = None
        self._voice_server: asyncio.AbstractServer | None = None
        self._voice_ingress: Any | None = None
        self._lock = asyncio.Lock()
        self._futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._active_operation_by_chat: dict[str, str] = {}
        # A physical terminal supplies one authenticated voice/text stream.
        # Serializing its in-flight turns is transport safety, not business
        # routing. It also lets the Runtime Contract fail closed instead of
        # guessing which of two same-actor ingress records a public lifecycle
        # callback belongs to after a host Skill expands the model message.
        self._active_operation_by_device: dict[str, str] = {}
        self._trace_session_by_operation: dict[str, str] = {}

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._server is not None:
            return True
        if not self.registry_path.exists():
            logger.error("Robot POC registry is missing: %s", self.registry_path)
            return False
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                host=self.host,
                port=self.port,
                limit=MAX_LINE_BYTES + 1,
            )
        except OSError:
            logger.exception("Robot POC loopback listener failed to bind host=%s port=%s", self.host, self.port)
            return False
        if self.voice_enabled:
            try:
                # Do not expose a voice listener until the local STT engine is
                # ready.  The PC client can therefore treat a successful
                # connection as voice-ready, rather than placing cold model
                # loading on the first teacher utterance.
                self._ensure_voice_ingress()
                await self._warm_voice_engine()
                self._voice_server = await asyncio.start_server(
                    self._handle_voice_connection,
                    host=self.host,
                    port=self.voice_port,
                    limit=MAX_VOICE_LINE_BYTES + 1,
                )
            except OSError:
                logger.exception("Robot POC voice listener failed to bind host=%s port=%s", self.host, self.voice_port)
                self._server.close()
                await self._server.wait_closed()
                self._server = None
                return False
        self._running = True
        try:
            recovery = self._business_module("direct_reply_recovery").get_direct_reply_recovery_manager()
            recovery.register_delivery_adapter(channel="robot_poc", adapter=self)
        except Exception:
            # A normal Robot turn remains usable if this optional durable
            # recovery capability was not installed. A server that enables
            # the reply-recovery platform verifies registration at startup.
            logger.exception("Robot POC could not register durable reply delivery port")
        logger.info("Robot POC listener started text=%s:%s voice=%s", self.host, self.port, self.voice_port if self.voice_enabled else "disabled")
        return True

    async def disconnect(self) -> None:
        server, self._server = self._server, None
        voice_server, self._voice_server = self._voice_server, None
        self._running = False
        if server is not None:
            server.close()
            await server.wait_closed()
        if voice_server is not None:
            voice_server.close()
            await voice_server.wait_closed()

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"id": str(chat_id), "type": "robot_poc", "is_group": False}

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        metadata = dict(metadata or {})
        # A Work Runtime delivery has already been generated by the same
        # Hermes in a zero-Tool recovery turn. Verify its immutable delivery
        # id/destination before completing the original direct turn; a model
        # or client cannot supply this path because only the outbox dispatcher
        # owns the opaque metadata.
        if metadata.get("xiaoyou_reply_only") is True and metadata.get("xiaoyou_delivery_id"):
            recovery = None
            job = None
            try:
                recovery = self._business_module("direct_reply_recovery").get_direct_reply_recovery_manager()
                job = recovery.verify_robot_delivery(
                    delivery_id=str(metadata["xiaoyou_delivery_id"]),
                    chat_id=str(chat_id),
                    reply_text=str(content or ""),
                )
            except Exception:
                # Existing generic outbox/adapter certification uses opaque
                # delivery metadata without activating direct reply recovery.
                # Preserve that public send contract when there is no matching
                # direct-recovery job. If a job *does* exist, fail closed.
                if recovery is not None and recovery.outbox.get_by_delivery_id(str(metadata["xiaoyou_delivery_id"])) is not None:
                    logger.exception("Robot POC rejected untrusted durable reply delivery chat=%s", _digest(chat_id))
                    return _result(ok=False, error="robot_poc_reply_delivery_unverified")
                job = None
            if job is not None:
                operation_id = self._active_operation_by_chat.get(str(chat_id))
                if not operation_id or operation_id != job.operation_id:
                    return _result(ok=False, error="robot_poc_reply_delivery_uncorrelated")
                response = {
                    "ok": True,
                    "state": "completed",
                    "operation_id": operation_id,
                    "reply_text": str(content or ""),
                    "delivery_status": "outbox_delivered",
                    "reply_recovery": True,
                    "delivery_id": job.delivery_id,
                }
                await self._complete_turn(operation_id, response)
                # A durable reply can finish after the original synchronous
                # Agent request already returned a provider error.  In that
                # case ``submit_text`` intentionally retained this trusted
                # correlation until the outbox delivery.  Clear it only now,
                # after the verified delivery has completed the exact turn.
                async with self._lock:
                    if self._active_operation_by_chat.get(str(chat_id)) == operation_id:
                        self._active_operation_by_chat.pop(str(chat_id), None)
                    for device_id, active_operation in tuple(self._active_operation_by_device.items()):
                        if active_operation == operation_id:
                            self._active_operation_by_device.pop(device_id, None)
                trace_session = self._trace_session_by_operation.pop(operation_id, "")
                if trace_session:
                    self._finalize_transport_trace(
                        session_id=trace_session,
                        delivery_status="outbox_delivered",
                        final_reply=str(content or ""),
                        terminal_failure_type="",
                    )
                return _result(ok=True, message_id=job.delivery_id)

        operation_id = self._active_operation_by_chat.get(str(chat_id))
        if not operation_id:
            logger.warning("Robot POC dropped an uncorrelated reply chat=%s", _digest(chat_id))
            return _result(ok=False, error="robot_poc_reply_uncorrelated")
        # Gateway startup may emit this framework onboarding notice before the
        # Agent Loop's actual reply.  It is not a reply to the Robot turn and
        # must not resolve the transport future early.  This is deliberately a
        # fixed Hermes system-message marker, never an interpretation of user
        # text or a business response rule.
        if str(content or "").startswith("📬 No home channel is set for "):
            logger.info("Robot POC ignored framework home-channel notice operation=%s", operation_id)
            return _result(ok=True, message_id="robot_poc_framework_notice_ignored")
        # Hermes 0.21 can turn an exhausted Provider exception into its own
        # fixed terminal diagnostic and still invoke the public channel
        # ``send`` callback.  That diagnostic is not a reply generated from
        # the verified business result.  When (and only when) the trusted
        # direct turn already owns a verified Receipt, hand the exact held
        # outbox job to same-Hermes reply-only recovery.  The branch has no
        # access to user text, Tool choice or business state beyond the
        # durable receipt fence.
        if self._framework_provider_failure(content):
            try:
                recovery = self._business_module("direct_reply_recovery").get_direct_reply_recovery_manager()
                job = recovery.release_pending_robot_operation(operation_id=operation_id, chat_id=str(chat_id))
                if job is not None:
                    logger.info("Robot POC handed framework provider terminal to durable reply recovery operation=%s", operation_id)
                    return _result(ok=True, message_id="robot_poc_reply_recovery_provider_terminal:" + job.reply_id)
            except Exception:
                logger.exception("Robot POC could not hand framework provider terminal to reply recovery operation=%s", operation_id)
                return _result(ok=False, error="robot_poc_reply_recovery_handoff_failed")
        try:
            recovery_module = self._business_module("direct_reply_recovery")
            reply_id = recovery_module.parse_control_marker(content)
            if reply_id is not None:
                recovery_module.get_direct_reply_recovery_manager().release_robot_handoff(
                    reply_id=reply_id,
                    operation_id=operation_id,
                    chat_id=str(chat_id),
                )
                # The opaque hand-off marker is terminal for Hermes' normal
                # delivery but intentionally non-terminal for this device
                # turn. The shared durable outbox will call this adapter again
                # with the verified natural-language reply.
                return _result(ok=True, message_id="robot_poc_reply_handoff:" + reply_id)
        except Exception:
            logger.exception("Robot POC durable reply handoff failed operation=%s", operation_id)
            return _result(ok=False, error="robot_poc_reply_handoff_failed")
        response = {
            "ok": True,
            "state": "completed",
            "operation_id": operation_id,
            "reply_text": str(content or ""),
            "delivery_status": "prepared",
        }
        await self._complete_turn(operation_id, response)
        # ``send`` is the one terminal transport callback that is present even
        # when the Agent Loop returns a provider diagnostic before normal
        # post-response hooks run.  Finalising here preserves the real Agent
        # output and never substitutes text.  A later core finaliser is a
        # harmless no-op because turn traces are single-terminal.
        trace_session = self._trace_session_by_operation.pop(operation_id, "")
        if trace_session:
            self._finalize_transport_trace(
                session_id=trace_session,
                delivery_status="prepared",
                final_reply=str(content or ""),
                terminal_failure_type=self._framework_provider_failure(content),
            )
        return _result(ok=True, message_id=operation_id)

    async def submit_text(
        self,
        envelope: dict[str, Any],
        *,
        ingress_metadata: dict[str, Any] | None = None,
        progress_sink: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Validate one terminal text event and submit it to Hermes unchanged."""

        request = self._validate_envelope(envelope)
        identity = self._resolve_device_identity(request)
        operation_id = self._operation_id(identity.device_id, request["session_id"], request["turn_id"])
        payload_hash = _digest(json.dumps({
            "device_id": identity.device_id,
            "session_id": request["session_id"],
            "turn_id": request["turn_id"],
            "text": request["text"],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        action, cached, future = await self._claim_turn(
            operation_id=operation_id,
            request=request,
            identity=identity,
            payload_hash=payload_hash,
            ingress_metadata=ingress_metadata,
        )
        if action == "cached":
            await _emit_progress(progress_sink, {"kind": "robot_turn_replayed", "source": "transport"})
            replay = deepcopy(cached or {})
            replay["replayed"] = True
            return replay
        if action == "wait":
            await _emit_progress(progress_sink, {"kind": "robot_turn_already_processing", "source": "transport"})
            replay = deepcopy(await asyncio.shield(future))
            replay["replayed"] = True
            return replay

        # A device is an identity credential, not a conversation.  Preserve
        # the authenticated device prefix while incorporating the validated
        # client session correlation ID, so separate conversations on one
        # terminal cannot bleed Hermes history or active-object context into
        # each other.  This performs no business interpretation.
        chat_id = self._chat_id(identity, str(request["session_id"]))
        # The device registry is the only Robot identity source.  Bind its
        # canonical actor and tenant before Hermes sees text so subsequent
        # public lifecycle hooks and Tools use this immutable turn contract.
        try:
            contract = self._business_module("runtime_contract")
            trusted_turn = contract.bind_robot_device_turn(
                actor_user_id=identity.actor_user_id,
                tenant_id=identity.tenant_id,
                session_id=chat_id,
                turn_id=operation_id,
                message_id=operation_id,
                chat_id=chat_id,
                message_text=str(request["text"]),
                host_transforms_model_user_message=bool(self.auto_skills),
            )
            if trusted_turn is None:
                raise RobotPocRejected("trusted_turn_rejected", "设备可信身份上下文无法建立。")
        except RobotPocRejected:
            raise
        except Exception as exc:
            logger.exception("Robot POC could not bind trusted turn operation=%s", operation_id)
            raise RobotPocRejected("trusted_turn_unavailable", "设备可信身份上下文不可用。") from exc
        async with self._lock:
            active_operation = self._active_operation_by_chat.get(chat_id)
            if active_operation and active_operation != operation_id:
                # Step A is deliberately one active turn per physical device.
                # Barge-in/cancellation belongs to a later protocol revision.
                raise RobotPocRejected("session_busy", "设备当前仍有一轮处理，不能并发提交。")
            device_operation = self._active_operation_by_device.get(identity.device_id)
            if device_operation and device_operation != operation_id:
                raise RobotPocRejected("device_busy", "设备当前仍有一轮处理，不能并发提交。")
            self._active_operation_by_chat[chat_id] = operation_id
            self._active_operation_by_device[identity.device_id] = operation_id
        # Start only the privacy-preserving turn trace once a physical device
        # has been authenticated and bound to the existing identity service.
        # This deliberately precedes Agent Loop dispatch so an immediate
        # provider failure still has a real, correlated terminal trace.  It
        # does not classify text, expose tools, call a business capability or
        # compose a reply; the normal ``pre_llm_call`` hook enriches this same
        # trace when the model loop starts.
        self._begin_transport_trace(
            session_id=chat_id,
            operation_id=operation_id,
            identity=identity,
            raw_text=str(request["text"]),
        )
        self._trace_session_by_operation[operation_id] = chat_id
        progress_subscription = ""
        if progress_sink is not None:
            try:
                trace_module = self._business_module("turn_trace")
                loop = asyncio.get_running_loop()

                def receive_lifecycle_event(item: dict[str, Any]) -> None:
                    loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(_emit_progress(progress_sink, item))
                    )

                progress_subscription = trace_module.subscribe_turn_progress(chat_id, receive_lifecycle_event)
            except Exception:
                logger.exception("Robot POC could not subscribe transport progress operation=%s", operation_id)
        await _emit_progress(progress_sink, {"kind": "robot_turn_accepted", "source": "transport"})
        event = self._build_event(request=request, identity=identity, operation_id=operation_id, chat_id=chat_id)
        durable_reply_handoff = False

        async def recover_verified_reply() -> tuple[dict[str, Any] | None, bool]:
            """Release only an already-verified reply recovery job.

            A provider terminal failure after a verified Tool receipt must not
            become a second business turn.  The manager returns a job only
            when its exact operation, destination, receipt and writeback
            evidence have already been persisted.  The reply worker then uses
            the public same-Hermes ingress with a zero-Tool surface.
            """

            try:
                recovery = self._business_module("direct_reply_recovery").get_direct_reply_recovery_manager()
                job = recovery.release_pending_robot_operation(operation_id=operation_id, chat_id=chat_id)
            except Exception:
                logger.exception("Robot POC could not release durable reply handoff operation=%s", operation_id)
                return None, False
            if job is None:
                return None, False
            try:
                response = await asyncio.wait_for(asyncio.shield(future), timeout=self.response_timeout_seconds)
                return deepcopy(response), True
            except asyncio.TimeoutError:
                # The business result is not rewritten as failed merely
                # because the channel waited too long for reply-only recovery.
                # Keep the trusted correlation for the durable worker, which
                # can still complete the exact device delivery later.
                return ({
                    "ok": False,
                    "state": "reply_recovery_pending",
                    "operation_id": operation_id,
                    "error": "robot_poc_reply_recovery_pending",
                    "message": "最终回复仍在恢复；系统未重新执行任何业务操作。",
                }, True)

        try:
            # ``handle_message`` includes the real Agent Loop.  Bound that
            # await as well as reply delivery: otherwise a provider request
            # that never resolves could bypass the documented terminal timeout
            # below and leave a physical device indefinitely busy.  This is a
            # transport deadline only; it neither chooses a Tool nor returns a
            # synthetic business reply.  The turn fence installed on timeout
            # prevents a late Tool result from becoming a write or success.
            await asyncio.wait_for(self._dispatch(event), timeout=self.response_timeout_seconds)
            # An empty Hermes terminal response does not invoke normal adapter
            # send. If a verified business receipt has already created a held
            # outbox job, release precisely that job instead of converting the
            # truthful business completion into a transport failure.
            try:
                recovery = self._business_module("direct_reply_recovery").get_direct_reply_recovery_manager()
                recovery.release_pending_robot_operation(operation_id=operation_id, chat_id=chat_id)
            except Exception:
                logger.exception("Robot POC could not inspect durable reply handoff operation=%s", operation_id)
            response = await asyncio.wait_for(asyncio.shield(future), timeout=self.response_timeout_seconds)
            return deepcopy(response)
        except asyncio.TimeoutError:
            recovered, durable_reply_handoff = await recover_verified_reply()
            if recovered is not None:
                return recovered
            timeout_response = {
                "ok": False,
                "state": "timeout",
                "operation_id": operation_id,
                "error": "robot_poc_reply_timeout",
                "message": "本轮处理超时，系统未把它解释为“没有任务”；请稍后重试。",
            }
            self._finalize_transport_trace(
                session_id=chat_id,
                delivery_status="robot_transport_timeout",
                terminal_failure_type="robot_transport_timeout",
            )
            self._trace_session_by_operation.pop(operation_id, None)
            await self._complete_turn(operation_id, timeout_response)
            return timeout_response
        except Exception as exc:
            logger.exception("Robot POC dispatch failed operation=%s", operation_id)
            recovered, durable_reply_handoff = await recover_verified_reply()
            if recovered is not None:
                return recovered
            failure = {
                "ok": False,
                "state": "failed",
                "operation_id": operation_id,
                "error": type(exc).__name__,
                "message": "本轮未得到 Hermes 的有效回复，未声明任何业务成功。",
            }
            self._finalize_transport_trace(
                session_id=chat_id,
                delivery_status="robot_dispatch_failed",
                terminal_failure_type="robot_dispatch_failed",
            )
            self._trace_session_by_operation.pop(operation_id, None)
            await self._complete_turn(operation_id, failure)
            return failure
        finally:
            if progress_subscription:
                try:
                    self._business_module("turn_trace").unsubscribe_turn_progress(progress_subscription)
                except Exception:
                    logger.exception("Robot POC could not unsubscribe transport progress operation=%s", operation_id)
            if not durable_reply_handoff:
                if self._active_operation_by_chat.get(chat_id) == operation_id:
                    self._active_operation_by_chat.pop(chat_id, None)
                if self._active_operation_by_device.get(identity.device_id) == operation_id:
                    self._active_operation_by_device.pop(identity.device_id, None)

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            data = await reader.readline()
            if not data or len(data) > MAX_LINE_BYTES:
                raise RobotPocRejected("invalid_request", "请求为空或超过 Step A 文本限制。")
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict):
                raise RobotPocRejected("invalid_request", "请求必须是 JSON 对象。")
            response = await self.submit_text(payload)
        except RobotPocRejected as exc:
            response = {"ok": False, "state": "rejected", "error": exc.code, "message": exc.message}
        except (UnicodeError, json.JSONDecodeError):
            response = {"ok": False, "state": "rejected", "error": "invalid_json", "message": "请求不是有效 UTF-8 JSON。"}
        except Exception:
            logger.exception("Robot POC connection failed peer=%s", peer)
            response = {"ok": False, "state": "failed", "error": "gateway_failure", "message": "Robot POC 网关处理失败，未声明业务成功。"}
        writer.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _handle_voice_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one local authenticated voice turn as progress JSON lines.

        The only terminal business text is Hermes' normal final reply.  Every
        preceding message is a source-labelled lifecycle fact so clients may
        present a responsive UI without treating a progress sentence as a
        business result.
        """

        peer = writer.get_extra_info("peername")
        write_lock = asyncio.Lock()

        async def emit(event: dict[str, Any]) -> None:
            async with write_lock:
                writer.write((json.dumps({"type": "progress", "event": event}, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                await writer.drain()

        try:
            data = await reader.readline()
            if not data or len(data) > MAX_VOICE_LINE_BYTES:
                raise RobotPocRejected("invalid_audio", "语音请求为空或超过 Step C 限制。")
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict):
                raise RobotPocRejected("invalid_request", "语音请求必须是 JSON 对象。")
            self._ensure_voice_ingress()
            response = await self._voice_ingress.submit(payload, progress_sink=emit)
            terminal = {"type": "final", "response": response}
        except (RobotPocRejected, Exception) as exc:
            if exc.__class__.__name__ == "VoiceIngressRejected":
                terminal = {"type": "final", "response": {"ok": False, "state": "rejected", "error": getattr(exc, "code", "voice_rejected"), "message": getattr(exc, "message", "语音输入被拒绝，未进入业务处理。")}}
            elif isinstance(exc, RobotPocRejected):
                terminal = {"type": "final", "response": {"ok": False, "state": "rejected", "error": exc.code, "message": exc.message}}
            elif isinstance(exc, (UnicodeError, json.JSONDecodeError)):
                terminal = {"type": "final", "response": {"ok": False, "state": "rejected", "error": "invalid_json", "message": "语音请求不是有效 UTF-8 JSON。"}}
            else:
                logger.exception("Robot POC voice connection failed peer=%s", peer)
                terminal = {"type": "final", "response": {"ok": False, "state": "failed", "error": "voice_gateway_failure", "message": "语音链路未得到有效 Hermes 回复，未声明任何业务成功。"}}
        async with write_lock:
            writer.write((json.dumps(terminal, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    def _ensure_voice_ingress(self) -> None:
        if self._voice_ingress is not None:
            return
        voice_module = importlib.import_module(f"{__package__}.voice")
        model_dir = _resolve_path(self.voice_stt_model_dir, "stt-model-cache")
        self._voice_ingress = voice_module.VoiceIngress(
            adapter=self,
            transcriber=voice_module.FasterWhisperTranscriber(
                model=self.voice_stt_model,
                model_dir=model_dir,
                runtime_site_packages=self.voice_stt_runtime_site_packages or None,
            ),
        )

    async def _warm_voice_engine(self) -> None:
        try:
            self._ensure_voice_ingress()
            await asyncio.to_thread(self._voice_ingress.transcriber.warm)
            logger.info("Robot POC local STT transport is warm model=%s", self.voice_stt_model)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Readiness is non-fatal.  A later real voice request will return a
            # truthful STT failure rather than falling back to text or another
            # recogniser.
            logger.exception("Robot POC local STT warmup failed")

    def _validate_envelope(self, envelope: dict[str, Any]) -> dict[str, str | int]:
        if not isinstance(envelope, dict):
            raise RobotPocRejected("invalid_request", "请求必须是对象。")
        supplied_identity = FORBIDDEN_CLIENT_IDENTITY_FIELDS.intersection(envelope)
        if supplied_identity:
            raise RobotPocRejected("untrusted_identity_field", "设备不得声明 tenant、actor、role 或 channel。")
        if str(envelope.get("protocol_version") or "") != PROTOCOL_VERSION:
            raise RobotPocRejected("unsupported_protocol", "Robot POC 协议版本不匹配。")
        result: dict[str, str | int] = {"protocol_version": PROTOCOL_VERSION}
        for name in ("device_id", "session_id", "turn_id", "message_id"):
            value = str(envelope.get(name) or "").strip()
            if not SAFE_ID.fullmatch(value):
                raise RobotPocRejected("invalid_correlation_id", f"{name} 缺失或格式无效。")
            result[name] = value
        text = str(envelope.get("text") or "").strip()
        if not text or len(text) > MAX_TEXT_CHARS:
            raise RobotPocRejected("invalid_text", "文本不能为空且不得超过 2000 字符。")
        result["text"] = text
        token = str(envelope.get("device_token") or "")
        if not token or len(token) > 512:
            raise RobotPocRejected("device_auth_failed", "设备认证失败。")
        result["device_token"] = token
        try:
            result["token_version"] = int(envelope.get("token_version"))
        except (TypeError, ValueError):
            raise RobotPocRejected("device_auth_failed", "设备认证版本无效。") from None
        return result

    def validate_voice_envelope(self, envelope: dict[str, Any]) -> dict[str, str | int]:
        """Authenticate a voice envelope before the local STT worker runs.

        Audio and its transcript must not smuggle tenant, actor or role data.
        The resulting correlation/authentication values are later revalidated
        by ``submit_text``; this first check prevents unbound audio from using
        POC STT compute.
        """

        if not isinstance(envelope, dict):
            raise RobotPocRejected("invalid_request", "语音请求必须是对象。")
        if str(envelope.get("protocol_version") or "") != VOICE_PROTOCOL_VERSION:
            raise RobotPocRejected("unsupported_protocol", "Robot POC 语音协议版本不匹配。")
        normalized = dict(envelope)
        normalized["protocol_version"] = PROTOCOL_VERSION
        normalized["text"] = "voice_input_pending"
        request = self._validate_envelope(normalized)
        self._resolve_device_identity(request)
        request.pop("text", None)
        return request

    def _resolve_device_identity(self, request: dict[str, str | int]) -> ResolvedRobotIdentity:
        registry = _json_load(self.registry_path, {})
        devices = registry.get("devices") if isinstance(registry, dict) else None
        if not isinstance(devices, list):
            raise RobotPocRejected("device_registry_invalid", "Robot POC 设备注册表无效。")
        record = next((item for item in devices if isinstance(item, dict) and str(item.get("device_id") or "") == request["device_id"]), None)
        if record is None:
            raise RobotPocRejected("device_not_found", "设备未绑定，不能进入 Hermes。")
        if str(record.get("status") or "") != "active":
            raise RobotPocRejected("device_revoked", "设备未激活或已吊销，不能进入 Hermes。")
        try:
            token_version = int(record.get("token_version"))
        except (TypeError, ValueError):
            raise RobotPocRejected("device_registry_invalid", "设备认证配置无效。") from None
        if token_version != request["token_version"]:
            raise RobotPocRejected("device_auth_failed", "设备认证版本已失效。")
        expected_hash = str(record.get("token_sha256") or "").lower()
        actual_hash = hashlib.sha256(str(request["device_token"]).encode("utf-8")).hexdigest()
        if not expected_hash or not hmac.compare_digest(expected_hash, actual_hash):
            raise RobotPocRejected("device_auth_failed", "设备认证失败。")
        tenant_id = str(record.get("tenant_id") or "").strip()
        actor_user_id = str(record.get("bound_actor_user_id") or "").strip()
        if not tenant_id or not actor_user_id:
            raise RobotPocRejected("device_registry_invalid", "设备缺少服务端绑定身份。")
        role = self._verify_existing_identity(actor_user_id)
        capabilities = tuple(str(item) for item in record.get("capabilities") or [] if str(item))
        return ResolvedRobotIdentity(
            device_id=str(request["device_id"]),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            role=role,
            token_version=token_version,
            capabilities=capabilities,
        )

    def _verify_existing_identity(self, actor_user_id: str) -> str:
        """Re-use the business identity approval table; never trust the device."""

        try:
            identity_module = self._business_module("identity")
            store_module = self._business_module("store")
            IdentityService = identity_module.IdentityService
            TuoguanStore = store_module.TuoguanStore

            identity = IdentityService(TuoguanStore()).resolve(
                self.identity_platform,
                actor_user_id,
                chat_id="robot_poc",
                message_text="",
            )
        except Exception as exc:
            logger.exception("Robot POC failed to resolve bound identity")
            raise RobotPocRejected("identity_resolution_failed", "设备绑定身份无法验证。") from exc
        if identity.approval_state != "approved":
            raise RobotPocRejected("actor_not_approved", "设备绑定账号未通过现有身份审核。")
        return str(identity.role or "unknown")

    @staticmethod
    def _business_module(name: str) -> Any:
        """Resolve the same user plugin in both Hermes and the local test tree."""

        # Hermes loads user plugins under a private ``hermes_plugins``
        # namespace.  The repository's lightweight tests import them through
        # ``plugins``.  These are two loaders for the same source, never two
        # distinct authorization implementations.
        for prefix in ("hermes_plugins.tuoguan_core", "plugins.tuoguan_core"):
            try:
                return importlib.import_module(f"{prefix}.{name}")
            except ModuleNotFoundError as exc:
                if exc.name not in {prefix.split(".", 1)[0], prefix, f"{prefix}.{name}"}:
                    raise
        raise RobotPocRejected("business_plugin_unavailable", "托管业务插件未加载，设备不能进入 Hermes。")

    async def _claim_turn(
        self,
        *,
        operation_id: str,
        request: dict[str, str | int],
        identity: ResolvedRobotIdentity,
        payload_hash: str,
        ingress_metadata: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any] | None, asyncio.Future[dict[str, Any]]]:
        async with self._lock:
            state = _json_load(self.state_path, {"schema_version": 1, "turns": {}, "traces": []})
            turns = state.setdefault("turns", {})
            if not isinstance(turns, dict):
                raise RobotPocRejected("state_invalid", "Robot POC turn 状态无效。")
            existing = turns.get(operation_id)
            if isinstance(existing, dict):
                if str(existing.get("payload_hash") or "") != payload_hash:
                    raise RobotPocRejected("turn_payload_conflict", "同一 turn 不能携带不同文本。")
                message_hash = _digest(str(request["message_id"]))
                message_hashes = existing.setdefault("message_id_hashes", [])
                if isinstance(message_hashes, list) and message_hash not in message_hashes:
                    message_hashes.append(message_hash)
                    existing["last_replayed_at"] = _utc_now()
                    _atomic_json_write(self.state_path, state)
                response = existing.get("response")
                if isinstance(response, dict):
                    return "cached", deepcopy(response), self._futures.get(operation_id) or self._resolved_future(response)
                future = self._futures.get(operation_id)
                if future is not None:
                    return "wait", None, future
                raise RobotPocRejected("turn_recovery_required", "该 turn 上次未完成，不能隐式重放。")
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._futures[operation_id] = future
            turns[operation_id] = {
                "payload_hash": payload_hash,
                "device_id": identity.device_id,
                "tenant_id": identity.tenant_id,
                "actor_id_hash": _digest(identity.actor_user_id),
                "role": identity.role,
                "session_id": str(request["session_id"]),
                "turn_id": str(request["turn_id"]),
                "message_id_hashes": [_digest(str(request["message_id"]))],
                "status": "processing",
                "created_at": _utc_now(),
            }
            traces = state.setdefault("traces", [])
            if isinstance(traces, list):
                traces.append({
                    "trace_id": f"robot_{_digest(operation_id)}",
                    "device_id_hash": _digest(identity.device_id),
                    "tenant_id": identity.tenant_id,
                    "actor_id_hash": _digest(identity.actor_user_id),
                    "role": identity.role,
                    "session_id": str(request["session_id"]),
                    "turn_id": str(request["turn_id"]),
                    "message_id_hash": _digest(str(request["message_id"])),
                    "operation_id": operation_id,
                    "input_mode": str((ingress_metadata or {}).get("input_mode") or "text"),
                    "stt_engine": str((ingress_metadata or {}).get("stt_engine") or "")[:80] or None,
                    "stt_model": str((ingress_metadata or {}).get("stt_model") or "")[:120] or None,
                    "stt_language": str((ingress_metadata or {}).get("stt_language") or "")[:32] or None,
                    "stt_latency_ms": (ingress_metadata or {}).get("stt_latency_ms"),
                    "audio_duration_ms": (ingress_metadata or {}).get("audio_duration_ms"),
                    "audio_sha256": str((ingress_metadata or {}).get("audio_sha256") or "")[:64] or None,
                    "tts_latency_ms": None,
                    "received_at": _utc_now(),
                    "state": "processing",
                })
            _atomic_json_write(self.state_path, state)
            return "new", None, future

    async def _complete_turn(self, operation_id: str, response: dict[str, Any]) -> None:
        async with self._lock:
            state = _json_load(self.state_path, {"schema_version": 1, "turns": {}, "traces": []})
            turns = state.get("turns") if isinstance(state, dict) else None
            if not isinstance(turns, dict) or not isinstance(turns.get(operation_id), dict):
                return
            turns[operation_id].update({
                "status": str(response.get("state") or "completed"),
                "completed_at": _utc_now(),
                "response": deepcopy(response),
            })
            for trace in reversed(state.get("traces") or []):
                if isinstance(trace, dict) and trace.get("operation_id") == operation_id:
                    trace.update({
                        "state": str(response.get("state") or "completed"),
                        "completed_at": _utc_now(),
                        "reply_char_count": len(str(response.get("reply_text") or "")),
                        "reply_text_hash": _digest(str(response.get("reply_text") or "")),
                        "final_state": str(response.get("state") or "completed"),
                    })
                    break
            _atomic_json_write(self.state_path, state)
            future = self._futures.pop(operation_id, None)
            if future is not None and not future.done():
                future.set_result(deepcopy(response))

    def _build_event(
        self,
        *,
        request: dict[str, str | int],
        identity: ResolvedRobotIdentity,
        operation_id: str,
        chat_id: str,
    ) -> MessageEvent:
        source = SessionSource(
            platform=_platform_value(),
            chat_id=chat_id,
            chat_name=f"Robot {identity.device_id}",
            chat_type="dm",
            user_id=identity.actor_user_id,
            user_name=identity.actor_user_id,
            thread_id=str(request["session_id"]),
        )
        try:
            source.message_id = operation_id
        except (AttributeError, TypeError):
            pass
        metadata = {
            "robot_poc": True,
            "robot_device_id": identity.device_id,
            "robot_tenant_id": identity.tenant_id,
            "robot_turn_id": str(request["turn_id"]),
            "transport_message_id": str(request["message_id"]),
            "operation_id": operation_id,
        }
        try:
            event = MessageEvent(
                text=str(request["text"]),
                message_type=MessageType.TEXT,
                user_id=identity.actor_user_id,
                user_name=identity.actor_user_id,
                source=source,
                raw_message=None,
                message_id=operation_id,
                metadata=metadata,
            )
        except TypeError:
            event = MessageEvent(
                text=str(request["text"]),
                message_type=MessageType.TEXT,
                source=source,
                message_id=operation_id,
                metadata=metadata,
            )
        if self.auto_skills:
            try:
                # Native Hermes Skill activation is server configuration, not
                # client or Robot business logic. The Runtime Contract records
                # this host-owned message transformation before dispatch so
                # the public lifecycle hook can retain the original trusted
                # device utterance and identity facts.
                event.auto_skill = list(self.auto_skills)
            except (AttributeError, TypeError):
                logger.warning("Robot POC gateway does not support native auto_skill binding")
        return event

    async def _dispatch(self, event: MessageEvent) -> None:
        # On Hermes itself, BasePlatformAdapter owns the normal Agent Loop and
        # interruption/session semantics.  The direct handler path exists only
        # for the intentionally small repository test stub.
        inherited_handler = getattr(self, "handle_message", None)
        if callable(inherited_handler):
            await inherited_handler(event)
            return
        handler = getattr(self, "_message_handler", None)
        if not callable(handler):
            raise RuntimeError("robot_poc_message_handler_missing")
        result = handler(event)
        if asyncio.iscoroutine(result):
            await result

    def _begin_transport_trace(
        self,
        *,
        session_id: str,
        operation_id: str,
        identity: ResolvedRobotIdentity,
        raw_text: str,
    ) -> None:
        """Open an audit trace without adding a Robot business decision."""

        try:
            trace_module = self._business_module("turn_trace")
            store_module = self._business_module("store")
            foundation_module = self._business_module("runtime_foundation")
            store = store_module.TuoguanStore()
            trace_module.begin_turn_trace(
                session_id=session_id,
                message_id=operation_id,
                tenant_id=identity.tenant_id,
                app_id="robot_poc",
                user_id=identity.actor_user_id,
                role=identity.role,
                raw_text=raw_text,
                visible_tool_count=0,
            )
            # Preserve the transport-authenticated utterance separately from
            # any native Hermes Skill payload later prepended for the model.
            # The runtime call is idempotent for this trusted message id.
            foundation_module.begin_inbound(
                store=store,
                message_id=operation_id,
                conversation_id=session_id,
                user_id=identity.actor_user_id,
                role=identity.role,
                raw_text=raw_text,
                actor_name="",
                tenant_id=identity.tenant_id,
                channel="robot_poc",
            )
        except Exception:
            logger.exception("Robot POC could not start turn trace operation=%s", operation_id)

    def _finalize_transport_trace(
        self,
        *,
        session_id: str,
        delivery_status: str,
        terminal_failure_type: str,
        final_reply: str = "",
    ) -> None:
        """Persist an adapter-terminal failure without inventing a reply."""

        try:
            trace_module = self._business_module("turn_trace")
            store_module = self._business_module("store")
            trace_module.finalize_turn_trace(
                store_module.TuoguanStore(),
                session_id=session_id,
                delivery_status=delivery_status,
                final_reply=final_reply,
                terminal_failure_type=terminal_failure_type,
            )
        except Exception:
            logger.exception("Robot POC could not finalize terminal trace session=%s", _digest(session_id))

    @staticmethod
    def _framework_provider_failure(content: str) -> str:
        """Classify only Hermes' own terminal provider diagnostics for audit.

        This has no relation to user language or business tools: it handles a
        framework-generated failure sentence after the model route already
        failed.  The Robot adapter never writes a replacement reply.
        """

        lowered = str(content or "").casefold()
        if "model provider" not in lowered:
            return ""
        if "failed after retries" in lowered:
            return "provider_terminal_failure"
        if "rate-limit" in lowered or "rate limit" in lowered or "rate-limiting" in lowered:
            return "provider_http_429"
        if "timed out" in lowered or "timeout" in lowered or "did not respond in time" in lowered:
            return "provider_timeout"
        return ""

    @staticmethod
    def _operation_id(device_id: str, session_id: str, turn_id: str) -> str:
        material = "\n".join((device_id, session_id, turn_id))
        return "robot-poc:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _chat_id(identity: ResolvedRobotIdentity, session_id: str) -> str:
        """Return a tenant/device-bound, transport-session-scoped chat id.

        ``session_id`` has already passed the protocol's conservative ID
        validation. It scopes only Hermes conversation continuity; tenant,
        actor and role still come exclusively from the device registry.
        """

        return f"robot:{identity.tenant_id}:{identity.device_id}:{session_id}"

    @staticmethod
    def _resolved_future(response: dict[str, Any]) -> asyncio.Future[dict[str, Any]]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        future.set_result(deepcopy(response))
        return future

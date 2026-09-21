"""WeCom callback-mode adapter for self-built enterprise applications.

Unlike the bot/websocket adapter in ``wecom.py``, this handles the standard
WeCom callback flow: WeCom POSTs encrypted XML to an HTTP endpoint, the
adapter decrypts it, queues the message for the agent, and immediately
acknowledges.  The agent's reply is delivered later via the proactive
``message/send`` API using an access-token.

Supports multiple self-built apps under one gateway instance, scoped by
``corp_id:user_id`` to avoid cross-corp collisions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import importlib
import logging
import os
from pathlib import Path
import socket as _socket
import time
from typing import Any, Dict, List, Optional
# Security: parse untrusted, pre-auth request bodies (WeCom callbacks) with
# defusedxml to block billion-laughs / entity-expansion (and XXE) DoS. The
# parsing API (fromstring) is a drop-in for the stdlib calls used below;
# response-building XML lives in wecom_crypto.py and is not parsed here.
try:
    import defusedxml.ElementTree as ET

    DEFUSEDXML_AVAILABLE = True
except ImportError:
    ET = None  # type: ignore[assignment]
    DEFUSEDXML_AVAILABLE = False

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from hermes_constants import get_hermes_home
from plugins.platforms.wecom.inbound_receipts import WecomInboundReceiptStore
from plugins.platforms.wecom.wecom_crypto import WXBizMsgCrypt, WeComCryptoError
from plugins.platforms.http_policy import platform_httpx_limits

logger = logging.getLogger(__name__)


def _send_result(*, ok: bool, message_id: str = "", error: str = "", raw_response: Any = None) -> SendResult:
    """Build the documented public result across supported Hermes versions."""

    try:
        return SendResult(success=ok, message_id=message_id, error=error, raw_response=raw_response)
    except TypeError:
        # Hermes 0.20.0 exposed the same public outcome under ``ok`` and did
        # not accept ``raw_response``.  This is a platform ABI shim, not a
        # retry or a business-path fork.
        return SendResult(ok=ok, message_id=message_id, error=error)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8645
DEFAULT_PATH = "/wecom/callback"
# Cap pre-auth request bodies. WeCom callbacks are small encrypted XML
# envelopes (media is delivered out-of-band via MediaId, never inline), so
# 64 KB is ample for any legitimate message while bounding the work an
# unauthenticated POST can force before signature verification.
_MAX_BODY = 65_536
ACCESS_TOKEN_TTL_SECONDS = 7200
# WeCom callbacks are acknowledged before Agent work enters the background
# queue.  The bounded direct-turn budget therefore protects a real stall, not
# the callback HTTP window.  It remains just below the Hermes gateway ceiling
# so a normal compression/model span cannot be cancelled by a second 38s
# watchdog.
DEFAULT_MODEL_TURN_TIMEOUT_SECONDS = 1770.0
MAX_MODEL_TURN_TIMEOUT_SECONDS = 1770.0

# Hermes may emit lifecycle diagnostics through the gateway status channel.
# They are useful in logs but are not a work message from 小优 and must never
# become a standalone Enterprise WeChat message to a teacher or owner.
_INTERNAL_CONTEXT_STATUS_MARKERS = (
    "context compaction",
    "compacting context",
    "preflight compression",
    "pre-api compression",
    "compression complete",
    "compression blocked",
    "compression aborted",
    "压缩上下文",
    "上下文压缩",
    "上下文已压缩",
)


def _model_turn_timeout_seconds() -> float:
    raw = str(os.getenv("HERMES_WECOM_MODEL_TURN_TIMEOUT_SECONDS", "") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_MODEL_TURN_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        value = DEFAULT_MODEL_TURN_TIMEOUT_SECONDS
    return min(MAX_MODEL_TURN_TIMEOUT_SECONDS, max(10.0, value))


def _wecom_proxy_url() -> str | None:
    """Return the channel-scoped proxy only for WeCom API egress.

    Provider traffic never reads this setting.  This adapter opts out of
    process proxy inheritance and passes the proxy explicitly to its own
    HTTP client, so adding another provider cannot silently inherit WeCom's
    local CONNECT path.
    """

    value = str(os.getenv("HERMES_WECOM_PROXY_URL", "") or "").strip()
    return value or None


def _import_tuoguan_module(name: str):
    """Resolve Tuoguan modules in package and Hermes v0.20 plugin namespaces."""

    last_error: ModuleNotFoundError | None = None
    for root in ("plugins.tuoguan_core", "hermes_plugins.tuoguan_core"):
        module_name = f"{root}.{name}"
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name not in {"plugins", "plugins.tuoguan_core", root, module_name}:
                raise
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ModuleNotFoundError(name)


def _turn_fence_module():
    """Keep the callback usable if a lightweight test omits tuoguan_core."""

    try:
        return _import_tuoguan_module("turn_fence")
    except Exception:
        return None


def _event_chat_id(event: MessageEvent) -> str:
    source = getattr(event, "source", None)
    return str(getattr(source, "chat_id", "") or "")


def _is_internal_context_status(content: str) -> bool:
    normalized = str(content or "").strip().lower()
    return bool(normalized) and any(marker in normalized for marker in _INTERNAL_CONTEXT_STATUS_MARKERS)


def _is_hermes_session_reset_notice(content: str) -> bool:
    """Recognise the complete Hermes control-plane reset envelope.

    This is a protocol boundary, not a keyword filter over model replies.  The
    reset command bypasses the model/output hooks and otherwise exposes its
    English runtime diagnostics directly through every channel adapter.
    """

    lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
    if not lines or lines[0] not in {
        "✨ Session reset! Starting fresh.",
        "✨ New session started!",
    } and not lines[0].startswith("✨ New session started:"):
        return False
    return any(
        line.startswith(("Model:", "Provider:", "Context:", "Endpoint:", "✦ Tip:"))
        for line in lines[1:]
    ) or len(lines) == 1


def _record_session_reset_control_delivery() -> None:
    """Persist content-free provenance for the product-facing reset notice."""

    try:
        TuoguanStore = _import_tuoguan_module("store").TuoguanStore
        authorized_system_write = _import_tuoguan_module("write_guard").authorized_system_write
        store = TuoguanStore()
        with authorized_system_write(
            store.data_dir,
            job_name="wecom_session_reset_control_projection",
            allowed_files={"runtime_status_events.jsonl"},
        ):
            store.append_jsonl_verified("runtime_status_events.jsonl", {
                "record_type": "session_reset_control_projected",
                "platform": "wecom_callback",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "contains_runtime_details": False,
            })
    except Exception:
        logger.debug("[WecomCallback] Failed to record reset projection", exc_info=True)


def _record_suppressed_context_status() -> None:
    """Best-effort, content-free evidence for the owner health query."""

    try:
        TuoguanStore = _import_tuoguan_module("store").TuoguanStore
        authorized_system_write = _import_tuoguan_module("write_guard").authorized_system_write
        store = TuoguanStore()
        with authorized_system_write(
            store.data_dir,
            job_name="wecom_internal_status_suppression",
            allowed_files={"runtime_status_events.jsonl"},
        ):
            store.append_jsonl_verified("runtime_status_events.jsonl", {
                "record_type": "internal_context_status_suppressed",
                "platform": "wecom_callback",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "contains_user_content": False,
            })
    except Exception:
        logger.debug("[WecomCallback] Failed to record suppressed internal status", exc_info=True)


def check_wecom_callback_requirements() -> bool:
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE and DEFUSEDXML_AVAILABLE


class WecomCallbackAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        # The published adapter contract has had two constructor shapes across
        # the Hermes versions XiaoYou certifies.  Keep this compatibility at
        # the public platform boundary instead of reaching into Core state.
        try:
            super().__init__(config, Platform.WECOM_CALLBACK)
        except TypeError:
            self.config = config
            self.platform = Platform.WECOM_CALLBACK
            self._message_handler = None
            self._running = False
        extra = config.extra or {}
        # The callback application's credentials belong in the service
        # environment, not a versioned XiaoYou configuration file.  The
        # explicit config values remain supported for isolated fixtures; the
        # environment fallback makes the production-compatible adapter
        # deployable without copying a secret into the Capability package.
        self._host = str(extra.get("host") or os.getenv("WECOM_CALLBACK_HOST") or DEFAULT_HOST)
        self._port = int(extra.get("port") or os.getenv("WECOM_CALLBACK_PORT") or DEFAULT_PORT)
        self._path = str(extra.get("path") or os.getenv("WECOM_CALLBACK_PATH") or DEFAULT_PATH)
        self._apps: List[Dict[str, Any]] = self._normalize_apps(extra)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._app: Optional[web.Application] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._message_queue: asyncio.Queue[tuple[MessageEvent, str, str]] = asyncio.Queue()
        self._poll_task: Optional[asyncio.Task] = None
        dedupe_path = str(extra.get("dedupe_db_path") or os.getenv("HERMES_WECOM_DEDUPE_DB") or "").strip()
        if not dedupe_path:
            dedupe_path = str(Path(get_hermes_home()) / "state" / "wecom_callback_receipts.sqlite3")
        self._inbound_receipts = WecomInboundReceiptStore(dedupe_path)
        self._user_app_map: Dict[str, str] = {}
        self._access_tokens: Dict[str, Dict[str, Any]] = {}

    def set_message_handler(self, handler) -> None:
        """Guarantee a visible, safe reply when a non-streaming model turn fails."""

        async def reliable_handler(event: MessageEvent):
            fence = _turn_fence_module()
            message_id = str(getattr(event, "message_id", "") or "")
            if fence is not None and message_id:
                try:
                    fence.begin_callback_turn(
                        message_id=message_id,
                        chat_id=_event_chat_id(event),
                        budget_seconds=_model_turn_timeout_seconds(),
                    )
                except Exception:
                    logger.exception("[WecomCallback] Unable to begin turn fence")
            try:
                response = await asyncio.wait_for(
                    handler(event),
                    timeout=_model_turn_timeout_seconds(),
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                if fence is not None:
                    fence.expire_turn(message_id=message_id, reason="callback_deadline_exceeded")
                logger.error(
                    "[WecomCallback] Model turn exceeded %.1fs message_id=%s",
                    _model_turn_timeout_seconds(),
                    event.message_id,
                )
                response = None
            except Exception:
                if fence is not None:
                    fence.expire_turn(message_id=message_id, reason="handler_exception")
                logger.exception("[WecomCallback] Model handler failed before producing a reply")
                response = None
            get_command = getattr(event, "get_command", None)
            command = get_command() if callable(get_command) else (
                str(event.text or "").strip().split(maxsplit=1)[0]
                if str(event.text or "").strip().startswith("/")
                else None
            )
            if (
                response is None
                and event.message_type == MessageType.TEXT
                and str(event.text or "").strip()
                and not command
            ):
                logger.error(
                    "[WecomCallback] Empty model response converted to a visible failure receipt "
                    "message_id=%s",
                    event.message_id,
                )
                if fence is not None:
                    fence.expire_turn(message_id=message_id, reason="empty_or_failed_model_response")
                    return fence.failure_reply(message_id=message_id)
                return "模型服务暂时不稳定，这一轮没有执行写入或发送，请稍后再试。"
            if fence is not None and message_id:
                fence.finish_turn(message_id=message_id)
            return response

        # BasePlatformAdapter.set_message_handler is a direct assignment in
        # Hermes v0.20. Keep the same contract so the plugin also loads in the
        # repository's lightweight compatibility test runtime.
        self._message_handler = reliable_handler

    # ------------------------------------------------------------------
    # App normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _user_app_key(corp_id: str, user_id: str) -> str:
        return f"{corp_id}:{user_id}" if corp_id else user_id

    @staticmethod
    def _normalize_apps(extra: Dict[str, Any]) -> List[Dict[str, Any]]:
        apps = extra.get("apps")
        if isinstance(apps, list) and apps:
            return [dict(app) for app in apps if isinstance(app, dict)]
        if extra.get("corp_id"):
            return [
                {
                    "name": extra.get("name") or "default",
                    "corp_id": extra.get("corp_id", ""),
                    "corp_secret": extra.get("corp_secret", ""),
                    "agent_id": str(extra.get("agent_id", "")),
                    "token": extra.get("token", ""),
                    "encoding_aes_key": extra.get("encoding_aes_key", ""),
                }
            ]
        # A server-owned callback application may instead be supplied by the
        # existing systemd EnvironmentFile.  This is channel configuration,
        # not identity inference: inbound crypto still attests the source and
        # outbound delivery remains bound to the existing trusted destination.
        env_app = {
            "name": str(os.getenv("WECOM_CALLBACK_APP_NAME") or "default"),
            "corp_id": str(os.getenv("WECOM_CALLBACK_CORP_ID") or ""),
            "corp_secret": str(os.getenv("WECOM_CALLBACK_CORP_SECRET") or ""),
            "agent_id": str(os.getenv("WECOM_CALLBACK_AGENT_ID") or ""),
            "token": str(os.getenv("WECOM_CALLBACK_TOKEN") or ""),
            "encoding_aes_key": str(os.getenv("WECOM_CALLBACK_ENCODING_AES_KEY") or ""),
        }
        if all(env_app[key] for key in ("corp_id", "corp_secret", "agent_id", "token", "encoding_aes_key")):
            return [env_app]
        return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # ``is_reconnect`` is forwarded by GatewayRunner on every retry per
        # the BasePlatformAdapter.connect contract. Callback adapters have
        # no server-side queue to preserve, so the flag is accepted-and-
        # ignored — but the kwarg MUST be present or the reconnect watcher
        # dies with TypeError and the platform silently stays offline.
        reconnecting = bool(is_reconnect)
        if not self._apps:
            logger.warning("[WecomCallback] No callback apps configured")
            return False
        if not check_wecom_callback_requirements():
            logger.warning("[WecomCallback] aiohttp/httpx not installed")
            return False

        # Quick port-in-use check.
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("127.0.0.1", self._port))
            logger.error("[WecomCallback] Port %d already in use", self._port)
            return False
        except (ConnectionRefusedError, OSError):
            pass

        try:
            client_options: Dict[str, Any] = {
                "timeout": 20.0,
                "limits": platform_httpx_limits(),
                "trust_env": False,
            }
            proxy_url = _wecom_proxy_url()
            if proxy_url:
                client_options["proxy"] = proxy_url
            self._http_client = httpx.AsyncClient(**client_options)
            # client_max_size rejects oversized bodies at the aiohttp layer
            # (413) before our handler — and before any signature work — runs.
            self._app = web.Application(client_max_size=_MAX_BODY)
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get(self._path, self._handle_verify)
            self._app.router.add_post(self._path, self._handle_callback)
            try:
                dashboard_http = _import_tuoguan_module("dashboard_http")
                register_wecom_callback_routes = dashboard_http.register_wecom_callback_routes

                if register_wecom_callback_routes(self._app, self):
                    logger.info("[WecomCallback] Tuoguan dashboard routes registered")
            except Exception:
                logger.exception("[WecomCallback] Tuoguan dashboard route registration failed")
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
            self._poll_task = asyncio.create_task(self._poll_loop())
            # Register this public adapter as a delivery port only.  It cannot
            # generate text, select a Tool, alter a Receipt, or choose a user.
            try:
                logger.warning("[WecomCallback] Registering current durable reply delivery port")
                _import_tuoguan_module("direct_reply_recovery").get_direct_reply_recovery_manager().register_delivery_adapter(
                    channel="wecom_callback", adapter=self,
                )
                logger.warning("[WecomCallback] Current durable reply delivery port registered")
            except Exception:
                logger.exception("[WecomCallback] Durable reply delivery port registration failed")
            await self._recover_inbound_messages(include_owned=reconnecting)
            self._mark_connected()
            logger.info(
                "[WecomCallback] HTTP server listening on %s:%s%s",
                self._host, self._port, self._path,
            )
            for app in self._apps:
                try:
                    await self._refresh_access_token(app)
                except Exception as exc:
                    logger.warning(
                        "[WecomCallback] Initial token refresh failed for app '%s': %s",
                        app.get("name", "default"), exc,
                    )
            return True
        except Exception:
            await self._cleanup()
            logger.exception("[WecomCallback] Failed to start")
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self._cleanup()
        self._mark_disconnected()
        logger.info("[WecomCallback] Disconnected")

    async def _cleanup(self) -> None:
        self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    # ------------------------------------------------------------------
    # Outbound: proactive send via access-token API
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        metadata = dict(metadata or {})
        # A marker is emitted only by the authenticated same-turn terminal
        # bridge after a verified Receipt.  It never becomes WeCom text: it
        # releases the exact durable job, whose normal or reply-only Hermes
        # answer is delivered later through this same public adapter.
        try:
            recovery_module = _import_tuoguan_module("direct_reply_recovery")
            recovery = recovery_module.get_direct_reply_recovery_manager()
            reply_id = recovery_module.parse_control_marker(content)
            if reply_id is not None:
                # The public Gateway callback supplies an opaque Agent
                # session here, not the authenticated WeCom userid.  The
                # held job has already bound its recipient from the trusted
                # ingress actor, so do not treat this session as a recipient.
                job = recovery.release_wecom_callback_handoff(reply_id=reply_id)
                logger.info(
                    "[WecomCallback] Consumed durable reply handoff reply_hash=%s delivery_id=%s",
                    hashlib.sha256(reply_id.encode("utf-8")).hexdigest()[:16],
                    job.delivery_id,
                )
                return _send_result(ok=True, message_id="wecom_reply_handoff:" + job.reply_id)
            delivery_id = str(metadata.get("xiaoyou_delivery_id") or "")
            if metadata.get("xiaoyou_reply_only") is True and delivery_id:
                try:
                    recovery.verify_delivery(
                        delivery_id=delivery_id, channel="wecom_callback",
                        chat_id=str(chat_id), reply_text=str(content or ""),
                    )
                    logger.info(
                        "[WecomCallback] Verified durable reply delivery delivery_id=%s",
                        delivery_id,
                    )
                except Exception:
                    # Other outbox uses may carry opaque delivery metadata.
                    # Fail closed only when this exact durable direct-reply job
                    # exists; otherwise retain the public adapter contract.
                    if recovery.outbox.get_by_delivery_id(delivery_id) is not None:
                        logger.exception("[WecomCallback] Rejected unverified durable reply delivery")
                        return _send_result(ok=False, error="wecom_reply_delivery_unverified")
        except Exception:
            logger.exception("[WecomCallback] Durable reply handoff failed")
            return _send_result(ok=False, error="wecom_reply_handoff_failed")
        if _is_hermes_session_reset_notice(content):
            _record_session_reset_control_delivery()
            # The command itself succeeded, but its model/provider/context
            # cockpit belongs in logs.  This product-facing control response
            # asserts no business result and never enters Reply Recovery.
            content = "新会话已开始。您的身份、权限和示例机构工作上下文会继续保留。"
        if _is_internal_context_status(content):
            _record_suppressed_context_status()
            logger.info("[WecomCallback] Suppressed internal context lifecycle status")
            # A status callback is not a business delivery.  Treating this as
            # successful prevents the gateway from attempting to re-send the
            # same technical text while no user-facing message is emitted.
            return _send_result(ok=True, message_id="internal-status-suppressed")
        app = self._resolve_app_for_chat(chat_id)
        if app is None:
            return _send_result(ok=False, error="wecom_app_scope_unresolved: outbound target is not bound to exactly one app")
        touser = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        try:
            payload = {
                "touser": touser,
                "msgtype": "text",
                "agentid": int(str(app.get("agent_id") or 0)),
                "text": {"content": content[:2048]},
                "safe": 0,
            }
            for _attempt in range(2):
                token = await self._get_access_token(app)
                resp = await self._http_client.post(
                    f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
                    json=payload,
                )
                data = resp.json()
                errcode = data.get("errcode")
                if errcode in {40001, 42001} and _attempt == 0:
                    # WeCom rejected the token — evict the cached entry so
                    # the next _get_access_token call forces a fresh fetch.
                    logger.warning(
                        "[WecomCallback] Token rejected for app '%s' (errcode=%s), refreshing",
                        app.get("name", "default"), errcode,
                    )
                    self._access_tokens.pop(app["name"], None)
                    continue
                if errcode != 0:
                    return _send_result(ok=False, error=str(data))
                return _send_result(
                    ok=True,
                    message_id=str(data.get("msgid", "")),
                    raw_response=data,
                )
            return _send_result(ok=False, error="send failed after token refresh")
        except Exception as exc:
            return _send_result(ok=False, error=str(exc))

    def _resolve_app_for_chat(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Resolve one app without silently crossing enterprise boundaries."""
        app_name = self._user_app_map.get(chat_id)
        if not app_name and ":" not in chat_id:
            # Legacy bare user_id — try to find a unique match.
            matching = [k for k in self._user_app_map if k.endswith(f":{chat_id}")]
            if len(matching) == 1:
                app_name = self._user_app_map.get(matching[0])
        app = self._get_app_by_name(app_name) if app_name else None
        if app is not None:
            return app
        if len(self._apps) == 1:
            return self._apps[0]
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    # ------------------------------------------------------------------
    # Inbound: HTTP callback handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "platform": "wecom_callback"})

    async def _handle_verify(self, request: web.Request) -> web.Response:
        """GET endpoint — WeCom URL verification handshake."""
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        echostr = request.query.get("echostr", "")
        for app in self._apps:
            try:
                crypt = self._crypt_for_app(app)
                plain = crypt.verify_url(msg_signature, timestamp, nonce, echostr)
                return web.Response(text=plain, content_type="text/plain")
            except Exception:
                continue
        return web.Response(status=403, text="signature verification failed")

    async def _handle_callback(self, request: web.Request) -> web.Response:
        """POST endpoint — receive an encrypted message callback."""
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        # Explicit guard in addition to client_max_size: rejects oversized
        # payloads before any XML parse / signature check (DoS, zip bombs).
        body_bytes = await request.read()
        if len(body_bytes) > _MAX_BODY:
            logger.warning("[WecomCallback] Payload too large (%d bytes) — rejected", len(body_bytes))
            return web.Response(status=413, text="payload too large")
        body = body_bytes.decode("utf-8", errors="replace")

        for app in self._apps:
            try:
                decrypted = self._decrypt_request(
                    app, body, msg_signature, timestamp, nonce,
                )
                event = self._build_event(app, decrypted)
                if event is not None:
                    app_name = str(app.get("name") or app.get("agent_id") or "default")
                    receipt = self._inbound_receipts.claim(
                        app_name=app_name,
                        message_id=event.message_id,
                        user_id=str(getattr(event.source, "user_id", "") or ""),
                        session_id=str(getattr(event.source, "chat_id", "") or ""),
                        payload={"app_name": app_name, "xml_text": decrypted},
                    )
                    if not receipt["accepted"]:
                        logger.info("[WecomCallback] Durable duplicate MsgId %s, skipping", event.message_id)
                        return web.Response(text="success", content_type="text/plain")
                    # Record which app this user belongs to.
                    if event.source and event.source.user_id:
                        map_key = self._user_app_key(
                            str(app.get("corp_id") or ""), event.source.user_id,
                        )
                        self._user_app_map[map_key] = app["name"]
                    try:
                        await self._message_queue.put(
                            (
                                event,
                                str(receipt["receipt_key"]),
                                str(getattr(event.source, "chat_id", "") or ""),
                            )
                        )
                    except Exception as exc:
                        self._inbound_receipts.mark_failed(str(receipt["receipt_key"]), type(exc).__name__)
                        raise
                # Immediately acknowledge — the agent's reply will arrive
                # later via the proactive message/send API.
                return web.Response(text="success", content_type="text/plain")
            except WeComCryptoError:
                continue
            except Exception:
                logger.exception("[WecomCallback] Error handling message")
                break
        return web.Response(status=400, text="invalid callback payload")

    async def _recover_inbound_messages(self, *, include_owned: bool = False) -> None:
        """Requeue callback payloads that survived a process crash."""

        for receipt in self._inbound_receipts.recover_pending(include_owned=include_owned):
            payload = receipt.get("payload") if isinstance(receipt.get("payload"), dict) else {}
            app = self._get_app_by_name(str(payload.get("app_name") or receipt.get("app_name") or ""))
            if app is None:
                self._inbound_receipts.mark_failed(str(receipt.get("receipt_key") or ""), "recovery_app_missing")
                continue
            try:
                event = self._build_event(app, str(payload.get("xml_text") or ""))
            except Exception:
                self._inbound_receipts.mark_failed(str(receipt.get("receipt_key") or ""), "recovery_payload_invalid")
                logger.exception("[WecomCallback] Failed to rebuild durable inbound event")
                continue
            if event is None:
                self._inbound_receipts.mark_processed(
                    str(receipt.get("receipt_key") or ""),
                    session_id=str(receipt.get("session_id") or ""),
                )
                continue
            if event.source and event.source.user_id:
                map_key = self._user_app_key(str(app.get("corp_id") or ""), event.source.user_id)
                self._user_app_map[map_key] = str(app.get("name") or "")
            await self._message_queue.put(
                (
                    event,
                    str(receipt.get("receipt_key") or ""),
                    str(receipt.get("session_id") or getattr(event.source, "chat_id", "") or ""),
                )
            )
            logger.warning(
                "[WecomCallback] Recovered unfinished inbound MsgId %s (attempt %s)",
                receipt.get("message_id"),
                receipt.get("attempt_count"),
            )

    async def _poll_loop(self) -> None:
        """Drain the message queue and dispatch to the gateway runner."""
        while True:
            event, receipt_key, session_id = await self._message_queue.get()
            try:
                task = asyncio.create_task(
                    self._dispatch_claimed_message(event, receipt_key, session_id)
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except Exception:
                self._inbound_receipts.mark_failed(receipt_key, "dispatch_schedule_failed")
                logger.exception("[WecomCallback] Failed to enqueue event")
            finally:
                self._message_queue.task_done()

    async def _dispatch_claimed_message(
        self,
        event: MessageEvent,
        receipt_key: str,
        session_id: str,
    ) -> None:
        """Mark an inbound receipt processed only after gateway handling ends."""

        mark_processing = getattr(self._inbound_receipts, "mark_processing", None)
        if callable(mark_processing):
            try:
                mark_processing(receipt_key)
            except Exception:
                logger.exception("[WecomCallback] Failed to mark inbound receipt processing")
        try:
            await self.handle_message(event)
        except asyncio.CancelledError:
            self._inbound_receipts.mark_failed(receipt_key, "dispatch_cancelled")
            raise
        except Exception as exc:
            self._inbound_receipts.mark_failed(receipt_key, type(exc).__name__)
            logger.exception("[WecomCallback] Claimed message dispatch failed")
        else:
            if not self._inbound_receipts.mark_processed(receipt_key, session_id=session_id):
                logger.error("[WecomCallback] Failed to finalize inbound receipt %s", receipt_key)

    # ------------------------------------------------------------------
    # XML / crypto helpers
    # ------------------------------------------------------------------

    def _decrypt_request(
        self, app: Dict[str, Any], body: str,
        msg_signature: str, timestamp: str, nonce: str,
    ) -> str:
        root = ET.fromstring(body)
        encrypt = root.findtext("Encrypt", default="")
        crypt = self._crypt_for_app(app)
        return crypt.decrypt(msg_signature, timestamp, nonce, encrypt).decode("utf-8")

    def _build_event(self, app: Dict[str, Any], xml_text: str) -> Optional[MessageEvent]:
        root = ET.fromstring(xml_text)
        msg_type = (root.findtext("MsgType") or "").lower()
        # Silently acknowledge lifecycle events.
        if msg_type == "event":
            event_name = (root.findtext("Event") or "").lower()
            if event_name in {"enter_agent", "subscribe"}:
                return None
        if msg_type not in {"text", "event"}:
            return None

        user_id = root.findtext("FromUserName", default="")
        corp_id = root.findtext("ToUserName", default=app.get("corp_id", ""))
        scoped_chat_id = self._user_app_key(corp_id, user_id)
        content = root.findtext("Content", default="").strip()
        if not content and msg_type == "event":
            content = "/start"
        msg_id = root.findtext("MsgId")
        if not msg_id:
            fallback_material = "\n".join(
                (
                    str(corp_id),
                    str(user_id),
                    str(root.findtext("CreateTime", default="0")),
                    str(msg_type),
                    str(content),
                )
            )
            msg_id = "fallback:" + hashlib.sha256(fallback_material.encode("utf-8")).hexdigest()
        source = self.build_source(
            chat_id=scoped_chat_id,
            chat_name=user_id,
            chat_type="dm",
            user_id=user_id,
            user_name=user_id,
        )
        # Hermes session persistence reads the platform id from the source on
        # supported runtimes. Keep it on both source and event for compatibility.
        try:
            source.message_id = msg_id
        except (AttributeError, TypeError):
            pass
        return MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=xml_text,
            message_id=msg_id,
        )

    def _crypt_for_app(self, app: Dict[str, Any]) -> WXBizMsgCrypt:
        return WXBizMsgCrypt(
            token=str(app.get("token") or ""),
            encoding_aes_key=str(app.get("encoding_aes_key") or ""),
            receive_id=str(app.get("corp_id") or ""),
        )

    def _get_app_by_name(self, name: Optional[str]) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        for app in self._apps:
            if app.get("name") == name:
                return app
        return None

    # ------------------------------------------------------------------
    # Access-token management
    # ------------------------------------------------------------------

    async def _get_access_token(self, app: Dict[str, Any]) -> str:
        cached = self._access_tokens.get(app["name"])
        now = time.time()
        if cached and cached.get("expires_at", 0) > now + 60:
            return cached["token"]
        return await self._refresh_access_token(app)

    async def _refresh_access_token(self, app: Dict[str, Any]) -> str:
        resp = await self._http_client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={
                "corpid": app.get("corp_id"),
                "corpsecret": app.get("corp_secret"),
            },
        )
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom token refresh failed: {data}")
        token = data["access_token"]
        expires_in = int(data.get("expires_in", ACCESS_TOKEN_TTL_SECONDS))
        self._access_tokens[app["name"]] = {
            "token": token,
            "expires_at": time.time() + expires_in,
        }
        logger.info(
            "[WecomCallback] Token refreshed for app '%s' (corp=%s), expires in %ss",
            app.get("name", "default"),
            app.get("corp_id", ""),
            expires_in,
        )
        return token

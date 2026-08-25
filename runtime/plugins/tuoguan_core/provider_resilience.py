"""Provider-scoped health guard for real-time XiaoYou conversations.

Hermes owns model selection and business reasoning.  This module only keeps a
small, persistent health state for the known Agnes upstream and prevents a
single sick provider from consuming the entire WeCom reply budget.  It never
stores request content, API keys, or business data.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import importlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

import httpx

from hermes_constants import get_hermes_home


logger = logging.getLogger(__name__)
STATE_FILE_NAME = "xiaoyou_provider_circuit.json"
AGNES_CIRCUIT = "agnes_primary"
OPEN_SECONDS = 10 * 60
PROBE_INTERVAL_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 6
_LOCK = threading.RLock()
_PATCHED = False


def agnes_only_mode() -> bool:
    """Keep Agnes as Xiaoyou's only production model unless explicitly disabled."""

    return str(os.getenv("HERMES_XIAOYOU_AGNES_ONLY", "1") or "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_path() -> Path:
    return Path(get_hermes_home()) / "state" / STATE_FILE_NAME


def _default_entry() -> dict[str, Any]:
    return {
        "state": "closed",
        # Wall-clock expiry survives a service or machine restart.
        "open_until_epoch": 0.0,
        "opened_at": "",
        "last_error_class": "",
        "last_failure_at": "",
        "last_success_at": "",
        "failure_count": 0,
        "half_open_successes": 0,
        "probe_inflight": False,
        "probe_claimed_at": "",
    }


def _read_state_locked() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError, UnicodeError):
        raw = {}
    state = raw if isinstance(raw, dict) else {}
    providers = state.get("providers") if isinstance(state.get("providers"), dict) else {}
    entry = providers.get(AGNES_CIRCUIT) if isinstance(providers.get(AGNES_CIRCUIT), dict) else {}
    merged = _default_entry()
    merged.update({key: value for key, value in entry.items() if key in merged})
    return {"schema_version": 1, "providers": {AGNES_CIRCUIT: merged}}


def _write_state_locked(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _entry_locked() -> tuple[dict[str, Any], dict[str, Any]]:
    state = _read_state_locked()
    return state, state["providers"][AGNES_CIRCUIT]


def _classify_error(error: Any = None, *, status_code: Any = None) -> str:
    code = str(status_code or "").strip()
    if code in {"429", "502", "503", "504"}:
        return f"http_{code}"
    text = f"{type(error).__name__}: {error}".lower()
    if any(token in text for token in ("tls", "ssl", "handshake", "certificate")):
        return "tls"
    if any(token in text for token in ("eof", "connection reset", "connection aborted", "remoteprotocol")):
        return "connection_interrupted"
    if any(token in text for token in ("timeout", "timed out", "readtimeout", "connecttimeout")):
        return "timeout"
    if any(token in text for token in ("truncated", "incomplete", "invalidapiresponse", "empty")):
        return "incomplete_response"
    return "transport_error"


def is_hard_transport_failure(error: Any = None, *, status_code: Any = None) -> bool:
    return _classify_error(error, status_code=status_code) in {
        "tls", "connection_interrupted", "timeout", "incomplete_response",
        "transport_error", "http_429", "http_502", "http_503", "http_504",
    }


def open_circuit(error: Any = None, *, status_code: Any = None) -> dict[str, Any]:
    """Persist the open state before asking Hermes to use its configured fallback."""

    with _LOCK:
        state, entry = _entry_locked()
        now = time.time()
        entry.update({
            "state": "open",
            "open_until_epoch": now + OPEN_SECONDS,
            "opened_at": _now(),
            "last_error_class": _classify_error(error, status_code=status_code),
            "last_failure_at": _now(),
            "failure_count": int(entry.get("failure_count") or 0) + 1,
            "half_open_successes": 0,
            "probe_inflight": False,
            "probe_claimed_at": "",
        })
        _write_state_locked(state)
        return deepcopy(entry)


def circuit_state() -> dict[str, Any]:
    """Return safe health information, moving an elapsed open circuit to half-open."""

    with _LOCK:
        state, entry = _entry_locked()
        now = time.time()
        changed = False
        if str(entry.get("state")) == "open" and now >= float(entry.get("open_until_epoch") or 0):
            entry.update({"state": "half_open", "half_open_successes": 0})
            changed = True
        if changed:
            _write_state_locked(state)
        return _public_entry(entry)


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    remaining = max(0, int(float(entry.get("open_until_epoch") or 0) - time.time()))
    return {
        "provider": "agnes-2.5-flash",
        "state": str(entry.get("state") or "closed"),
        "open_remaining_seconds": remaining,
        "last_error_class": str(entry.get("last_error_class") or ""),
        "last_failure_at": str(entry.get("last_failure_at") or ""),
        "last_success_at": str(entry.get("last_success_at") or ""),
        "failure_count": int(entry.get("failure_count") or 0),
        "half_open_successes": int(entry.get("half_open_successes") or 0),
        "probe_inflight": bool(entry.get("probe_inflight")),
    }


def provider_health_snapshot() -> dict[str, Any]:
    return {
        "agnes": circuit_state(),
        "model_policy": "agnes_only" if agnes_only_mode() else "fallback_enabled",
        "fallback_model": "" if agnes_only_mode() else "configured_by_hermes",
    }


def _mark_probe_result(*, success: bool, error: Any = None) -> dict[str, Any]:
    with _LOCK:
        state, entry = _entry_locked()
        entry["probe_inflight"] = False
        entry["probe_claimed_at"] = ""
        if success:
            successes = int(entry.get("half_open_successes") or 0) + 1
            entry["half_open_successes"] = successes
            entry["last_success_at"] = _now()
            if successes >= 2:
                entry.update({
                    "state": "closed", "open_until_epoch": 0.0,
                    "half_open_successes": 0, "last_error_class": "",
                })
        else:
            entry.update({
                "state": "open", "open_until_epoch": time.time() + OPEN_SECONDS,
                "last_error_class": _classify_error(error), "last_failure_at": _now(),
                "half_open_successes": 0,
                "failure_count": int(entry.get("failure_count") or 0) + 1,
            })
        _write_state_locked(state)
        return deepcopy(entry)


def _start_recovery_probe(agent: Any) -> bool:
    """Run two non-business health probes in a daemon thread at most once."""

    with _LOCK:
        state, entry = _entry_locked()
        if str(entry.get("state")) != "half_open" or entry.get("probe_inflight"):
            return False
        entry["probe_inflight"] = True
        entry["probe_claimed_at"] = _now()
        _write_state_locked(state)
    primary = getattr(agent, "_primary_runtime", {}) or {}
    base_url = str(primary.get("base_url") or getattr(agent, "base_url", "") or "").rstrip("/")
    api_key = str(primary.get("api_key") or getattr(agent, "api_key", "") or "")
    model = str(primary.get("model") or "agnes-2.5-flash")

    def probe() -> None:
        if not base_url or not api_key:
            _mark_probe_result(success=False, error=RuntimeError("primary_probe_configuration_missing"))
            return
        for attempt in range(2):
            try:
                with httpx.Client(timeout=PROBE_TIMEOUT_SECONDS) as client:
                    response = client.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={"model": model, "messages": [{"role": "user", "content": "Reply exactly: OK"}], "max_tokens": 4, "temperature": 0, "stream": False},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    choices = payload.get("choices") if isinstance(payload, dict) else []
                    content = ""
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
                        content = str(message.get("content") or "")
                    if not content.strip():
                        raise RuntimeError("probe_empty_response")
            except Exception as exc:
                _mark_probe_result(success=False, error=exc)
                _record_provider_event(agent, outcome="probe_failed", error_class=_classify_error(exc))
                return
            _mark_probe_result(success=True)
            _record_provider_event(agent, outcome="probe_succeeded", error_class="")
            if attempt == 0:
                time.sleep(PROBE_INTERVAL_SECONDS)

    threading.Thread(target=probe, name="xiaoyou-agnes-health-probe", daemon=True).start()
    return True


def _is_wecom_agnes(agent: Any) -> bool:
    platform = str(getattr(agent, "platform", "") or "").lower()
    primary = getattr(agent, "_primary_runtime", {}) or {}
    primary_model = str(primary.get("model") or "")
    primary_url = str(primary.get("base_url") or "")
    current_model = str(getattr(agent, "model", "") or "")
    current_url = str(getattr(agent, "base_url", "") or "")
    return platform == "wecom_callback" and (
        "agnes" in primary_model.lower()
        or "agnes-ai.com" in primary_url.lower()
        or "agnes" in current_model.lower()
        or "agnes-ai.com" in current_url.lower()
    )


def _record_provider_event(agent: Any, *, outcome: str, error_class: str = "") -> None:
    try:
        module = None
        for name in ("plugins.tuoguan_core.turn_trace", "hermes_plugins.tuoguan_core.turn_trace"):
            try:
                module = importlib.import_module(name)
                break
            except ModuleNotFoundError:
                continue
        if module is not None:
            module.record_provider_event(
                str(getattr(agent, "session_id", "") or ""),
                provider="agnes" if _is_wecom_agnes(agent) else "fallback",
                model=str(getattr(agent, "model", "") or ""),
                outcome=outcome,
                error_class=error_class,
                circuit_state=str(circuit_state().get("state") or "closed"),
                network_egress=(
                    str(os.getenv("HERMES_AGNES_EGRESS_LABEL") or "legacy_local_proxy")
                    if _is_wecom_agnes(agent)
                    else "direct_or_configured_fallback"
                ),
            )
    except Exception:
        logger.debug("Unable to record provider resilience trace", exc_info=True)


def install_hermes_model_resilience_patch() -> bool:
    """Patch Hermes at one narrow runtime seam; safe no-op in lightweight tests."""

    global _PATCHED
    with _LOCK:
        if _PATCHED:
            return True
        try:
            run_agent = importlib.import_module("run_agent")
            agent_type = getattr(run_agent, "AIAgent")
        except Exception:
            logger.warning("XIAOYOU_PROVIDER_RESILIENCE_PATCH_SKIPPED reason=aiagent_import_unavailable")
            return False
        original_restore = getattr(agent_type, "_restore_primary_runtime", None)
        original_recover = getattr(agent_type, "_try_recover_primary_transport", None)
        original_fallback = getattr(agent_type, "_try_activate_fallback", None)
        if not all(callable(item) for item in (original_restore, original_recover, original_fallback)):
            logger.error("XIAOYOU_PROVIDER_RESILIENCE_PATCH_SKIPPED reason=unexpected_aiagent_surface")
            return False

        def restore(self: Any) -> bool:
            restored = original_restore(self)
            if not _is_wecom_agnes(self):
                default = getattr(self, "_xiaoyou_default_api_max_retries", None)
                if default is not None:
                    self._api_max_retries = default
                return restored
            if not hasattr(self, "_xiaoyou_default_api_max_retries"):
                self._xiaoyou_default_api_max_retries = getattr(self, "_api_max_retries", 2)
            self._api_max_retries = 1
            if agnes_only_mode():
                # With no alternate model there is nothing to route around.
                # The real user request is the health check; avoid adding a
                # second background Agnes call during half-open recovery.
                return restored
            state = circuit_state()
            if state["state"] == "open" and not agnes_only_mode():
                original_fallback(self, None)
                _record_provider_event(self, outcome="circuit_fallback", error_class=state.get("last_error_class", ""))
            elif state["state"] == "half_open":
                _start_recovery_probe(self)
                if not agnes_only_mode():
                    original_fallback(self, None)
                    _record_provider_event(self, outcome="half_open_fallback", error_class="")
            return restored

        def recover(self: Any, api_error: Exception, *, retry_count: int, max_retries: int) -> bool:
            if _is_wecom_agnes(self) and is_hard_transport_failure(api_error):
                opened = open_circuit(api_error)
                _record_provider_event(self, outcome="circuit_opened", error_class=opened.get("last_error_class", ""))
                # Do not use Hermes' extra primary client recovery attempt;
                # conversation_loop will immediately traverse fallback.
                return False
            return original_recover(self, api_error, retry_count=retry_count, max_retries=max_retries)

        def fallback(self: Any, reason: Any = None) -> bool:
            if _is_wecom_agnes(self):
                reason_text = str(getattr(reason, "value", reason) or "")
                if any(token in reason_text.lower() for token in ("rate", "timeout", "transport", "upstream", "overload", "invalid")):
                    opened = open_circuit(RuntimeError(reason_text or "fallback_requested"))
                    _record_provider_event(self, outcome="circuit_opened", error_class=opened.get("last_error_class", ""))
                if agnes_only_mode():
                    _record_provider_event(self, outcome="fallback_blocked_agnes_only", error_class=reason_text)
                    return False
            switched = original_fallback(self, reason)
            if switched:
                _record_provider_event(self, outcome="fallback_activated", error_class="")
            return switched

        agent_type._restore_primary_runtime = restore
        agent_type._try_recover_primary_transport = recover
        agent_type._try_activate_fallback = fallback
        _PATCHED = True
        logger.warning(
            "XIAOYOU_PROVIDER_RESILIENCE_PATCH_ENABLED primary=agnes-2.5-flash model_policy=%s",
            "agnes_only" if agnes_only_mode() else "fallback_enabled",
        )
        return True

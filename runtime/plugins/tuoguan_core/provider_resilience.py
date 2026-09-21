"""Provider observations for XiaoYou's portable runtime contract.

Hermes owns provider selection, retries and its agent loop. XiaoYou records
only outcomes exposed through documented lifecycle hooks: it never modifies an
agent class, reads implementation state, sends health prompts, or substitutes
a second model.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from hermes_constants import get_hermes_home

from .workspace import workspace_state_dir


STATE_FILE_NAME = "xiaoyou_provider_circuit.json"
AGNES_CIRCUIT = "agnes_primary"
OPEN_SECONDS = 10 * 60
_LOCK = threading.RLock()


def agnes_only_mode() -> bool:
    """Whether the institution intentionally permits only Agnes."""

    return str(os.getenv("HERMES_XIAOYOU_AGNES_ONLY", "1") or "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_path() -> Path:
    state_dir = workspace_state_dir()
    if state_dir is not None:
        return state_dir / STATE_FILE_NAME
    # Compatibility only for an older local installation without Workspace.
    return Path(get_hermes_home()) / "state" / STATE_FILE_NAME


def _default_entry() -> dict[str, Any]:
    return {
        "state": "closed", "open_until_epoch": 0.0, "opened_at": "",
        "last_error_class": "", "last_failure_at": "", "last_success_at": "",
        "failure_count": 0, "half_open_successes": 0,
    }


def _read_state_locked() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError, UnicodeError):
        raw = {}
    providers = raw.get("providers") if isinstance(raw, dict) and isinstance(raw.get("providers"), dict) else {}
    stored = providers.get(AGNES_CIRCUIT) if isinstance(providers.get(AGNES_CIRCUIT), dict) else {}
    entry = _default_entry()
    entry.update({key: value for key, value in stored.items() if key in entry})
    return {"schema_version": 1, "providers": {AGNES_CIRCUIT: entry}}


def _write_state_locked(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


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
    if any(token in text for token in ("truncated", "incomplete", "invalidapiresponse", "malformed", "empty")):
        return "incomplete_response"
    return "transport_error"


def is_hard_transport_failure(error: Any = None, *, status_code: Any = None) -> bool:
    return _classify_error(error, status_code=status_code) in {
        "tls", "connection_interrupted", "timeout", "incomplete_response", "transport_error",
        "http_429", "http_502", "http_503", "http_504",
    }


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": "agnes-2.5-flash",
        "state": str(entry.get("state") or "closed"),
        "open_remaining_seconds": max(0, int(float(entry.get("open_until_epoch") or 0) - time.time())),
        "last_error_class": str(entry.get("last_error_class") or ""),
        "last_failure_at": str(entry.get("last_failure_at") or ""),
        "last_success_at": str(entry.get("last_success_at") or ""),
        "failure_count": int(entry.get("failure_count") or 0),
        "half_open_successes": int(entry.get("half_open_successes") or 0),
    }


def circuit_state() -> dict[str, Any]:
    """Return operational health only; it does not control Hermes routing."""

    with _LOCK:
        state, entry = _entry_locked()
        if str(entry.get("state")) == "open" and time.time() >= float(entry.get("open_until_epoch") or 0):
            entry.update({"state": "half_open", "half_open_successes": 0})
            _write_state_locked(state)
        return _public_entry(entry)


def open_circuit(error: Any = None, *, status_code: Any = None) -> dict[str, Any]:
    with _LOCK:
        state, entry = _entry_locked()
        entry.update({
            "state": "open", "open_until_epoch": time.time() + OPEN_SECONDS,
            "opened_at": _now(), "last_error_class": _classify_error(error, status_code=status_code),
            "last_failure_at": _now(), "failure_count": int(entry.get("failure_count") or 0) + 1,
            "half_open_successes": 0,
        })
        _write_state_locked(state)
        return deepcopy(entry)


def _mark_probe_result(*, success: bool, error: Any = None) -> dict[str, Any]:
    """Record an externally-operated check; XiaoYou never sends one itself."""

    with _LOCK:
        state, entry = _entry_locked()
        if success:
            successes = int(entry.get("half_open_successes") or 0) + 1
            entry.update({"last_success_at": _now(), "half_open_successes": successes})
            if str(entry.get("state")) == "half_open" and successes >= 2:
                entry.update({"state": "closed", "open_until_epoch": 0.0, "last_error_class": "", "half_open_successes": 0})
        else:
            entry.update({
                "state": "open", "open_until_epoch": time.time() + OPEN_SECONDS,
                "last_error_class": _classify_error(error), "last_failure_at": _now(),
                "failure_count": int(entry.get("failure_count") or 0) + 1, "half_open_successes": 0,
            })
        _write_state_locked(state)
        return deepcopy(entry)


def observe_provider_success() -> dict[str, Any]:
    """Persist a real successful provider lifecycle event."""

    return _mark_probe_result(success=True)


def observe_provider_failure(error: Any = None, *, status_code: Any = None) -> dict[str, Any]:
    """Persist a real failure lifecycle event without attempting recovery."""

    return open_circuit(error, status_code=status_code)


def provider_health_snapshot() -> dict[str, Any]:
    return {
        "agnes": circuit_state(),
        "model_policy": "agnes_only" if agnes_only_mode() else "configured_by_hermes",
        "fallback_model": "",
    }


def _is_trusted_agnes(agent: Any) -> bool:
    """Classify a documented hook payload, not a framework-private runtime."""

    platform = str(getattr(agent, "platform", "") or "").lower()
    model = str(getattr(agent, "model", "") or "").lower()
    base_url = str(getattr(agent, "base_url", "") or "").lower()
    return platform in {"wecom_callback", "robot_poc", "feishu"} and (
        "agnes" in model or "agnes-ai.com" in base_url
    )


_is_wecom_agnes = _is_trusted_agnes

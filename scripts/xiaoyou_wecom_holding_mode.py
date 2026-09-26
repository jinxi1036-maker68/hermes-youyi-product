#!/usr/bin/env python3
"""Operate bootstrap WeCom holding mode without a network control plane."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
from typing import Any

MODE_SCHEMA_VERSION = "xiaoyou_wecom_holding_mode_v1"
HOLD_STATE = "hold"
DEFAULT_SERVICE_USER = "hermes-youyi"


def _service_uid(user_name: str) -> int:
    return pwd.getpwnam(user_name).pw_uid


def _require_service_identity(user_name: str) -> None:
    required = _service_uid(user_name)
    current = os.geteuid()
    if current != required:
        raise PermissionError(
            f"write operations must run as {user_name} "
            f"(uid={required}); current uid={current}"
        )


def _default_mode_file(runtime_home: Path) -> Path:
    configured = str(
        os.getenv("XIAOYOU_WECOM_HOLDING_MODE_FILE") or ""
    ).strip()
    return (
        Path(configured)
        if configured
        else runtime_home
        / "state"
        / "wecom_callback_holding_mode.json"
    )


def _atomic_hold(path: Path, *, reason: str) -> None:
    path.parent.mkdir(
        parents=True,
        mode=0o700,
        exist_ok=True,
    )
    payload = {
        "schema_version": MODE_SCHEMA_VERSION,
        "state": HOLD_STATE,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "reason": str(reason or "bootstrap_cutover"),
    }
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.tmp"
    )
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _resume(path: Path) -> None:
    path.unlink(missing_ok=True)


def _snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "ok": True,
            "mode": "FORWARD",
            "active": False,
            "valid": True,
            "path": str(path),
        }

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return {
            "ok": False,
            "mode": "HOLD",
            "active": True,
            "valid": False,
            "path": str(path),
            "error": "holding_mode_unreadable",
        }

    valid = (
        isinstance(payload, dict)
        and payload.get("schema_version")
        == MODE_SCHEMA_VERSION
        and str(
            payload.get("state") or ""
        ).strip().lower()
        == HOLD_STATE
    )
    return {
        "ok": bool(valid),
        "mode": "HOLD",
        "active": True,
        "valid": bool(valid),
        "path": str(path),
        "requested_at": (
            payload.get("requested_at")
            if isinstance(payload, dict)
            else None
        ),
        "reason": (
            str(payload.get("reason") or "")
            if isinstance(payload, dict)
            else ""
        ),
        **(
            {}
            if valid
            else {"error": "holding_mode_invalid"}
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Enter, inspect, or resume bootstrap "
            "WeCom holding mode."
        )
    )
    parser.add_argument(
        "action",
        choices=("hold", "status", "resume"),
    )
    parser.add_argument(
        "--runtime-home",
        type=Path,
        required=True,
    )
    parser.add_argument("--mode-file", type=Path)
    parser.add_argument(
        "--service-user",
        default=str(
            os.getenv("XIAOYOU_SERVICE_USER")
            or DEFAULT_SERVICE_USER
        ),
    )
    parser.add_argument(
        "--reason",
        default="bootstrap_cutover",
    )
    args = parser.parse_args()

    runtime_home = args.runtime_home.resolve()
    mode_file = (
        args.mode_file
        or _default_mode_file(runtime_home)
    ).resolve()

    try:
        if args.action == "hold":
            _require_service_identity(
                args.service_user
            )
            _atomic_hold(
                mode_file,
                reason=args.reason,
            )
        elif args.action == "resume":
            _require_service_identity(
                args.service_user
            )
            _resume(mode_file)

        result = _snapshot(mode_file)
        result["action"] = args.action
        result["runtime_home"] = str(runtime_home)
        result["secret_content_inspected"] = False
        result["payload_content_inspected"] = False
    except (
        OSError,
        PermissionError,
        KeyError,
    ) as exc:
        result = {
            "ok": False,
            "action": args.action,
            "error": f"{type(exc).__name__}:{exc}",
            "secret_content_inspected": False,
            "payload_content_inspected": False,
        }

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

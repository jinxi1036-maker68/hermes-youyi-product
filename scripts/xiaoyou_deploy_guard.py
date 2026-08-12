"""Fail-closed helpers for Xiaoyou systemd deployment operations."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from typing import Any, Callable


MAIN_SERVICE = "hermes-youyi-019.service"
_TIMER_PATTERN = re.compile(r"\b(hermes-youyi-[A-Za-z0-9_.@-]+\.timer)\b")
_ALLOWED_ACTIONS = {"start", "stop", "restart", "is-active"}


def extract_timer_units(systemctl_output: str) -> list[str]:
    """Extract timer UNIT names and ignore their ACTIVATES service column."""

    return sorted(set(_TIMER_PATTERN.findall(str(systemctl_output or ""))))


def validate_systemctl_target(action: str, unit: str) -> None:
    normalized_action = str(action or "").strip()
    normalized_unit = str(unit or "").strip()
    if normalized_action not in _ALLOWED_ACTIONS:
        raise ValueError(f"unsupported_systemctl_action:{normalized_action}")
    if normalized_unit == MAIN_SERVICE:
        return
    if normalized_unit.startswith("hermes-youyi-") and normalized_unit.endswith(".timer"):
        return
    raise ValueError(f"unsafe_systemctl_target:{normalized_unit}")


def systemctl(
    action: str,
    unit: str,
    *,
    execute: bool = False,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Validate every target before optionally invoking systemctl."""

    validate_systemctl_target(action, unit)
    command = ["systemctl", action, unit]
    if not execute:
        return {"ok": True, "executed": False, "command": command}
    completed = runner(command, text=True, capture_output=True, check=False)
    return {
        "ok": completed.returncode == 0,
        "executed": True,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Xiaoyou systemd deployment targets.")
    parser.add_argument("action", choices=sorted(_ALLOWED_ACTIONS))
    parser.add_argument("unit")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        result = systemctl(args.action, args.unit, execute=args.execute)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

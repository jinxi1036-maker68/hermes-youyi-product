#!/usr/bin/env python3
"""Harmless fixture executor for XiaoU ops-worker V2 transport acceptance.

This executor performs no server inspection, deployment, service control,
business-data access, or model call. It only turns a validated V2 command into
a valid XIAOU_OPS_REPORT_V1 response so the command transport can be tested
independently from Codex authentication/runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

# The production worker intentionally strips PYTHONPATH before invoking the
# fixed executor. Make this script resolvable from its own root-owned install
# location instead of depending on ambient shell/service environment.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.xiaoyou_ops_command_v1 import CommandValidationError, validate_command


def main() -> int:
    try:
        payload = json.loads(input())
        command = validate_command(payload)
    except (EOFError, json.JSONDecodeError, CommandValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2

    report = {
        "protocol": "XIAOU_OPS_REPORT_V1",
        "command_id": command["command_id"],
        "pr_number": command["pr_number"],
        "candidate_sha": command["candidate_sha"],
        "action": command["action"],
        "result": "PASS",
        "code_changed": False,
        "production_changed": False,
        "summary": "V2 transport fixture executed. No production or model action was performed.",
        "evidence": {
            "fixture_executor": True,
            "transport_only": True,
        },
        "anomalies": [],
    }
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Consolidate a full shadow replay with audited corrective reruns."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"report_not_mapping:{path.name}")
    return payload


def consolidate(base_path: Path, corrective_path: Path) -> dict[str, Any]:
    base = _load(base_path)
    corrective = _load(corrective_path)
    for key in ("model", "scenario_set_id"):
        if base.get(key) != corrective.get(key):
            raise ValueError(f"report_identity_mismatch:{key}")
    if base.get("credentials_in_report") or corrective.get("credentials_in_report"):
        raise ValueError("credential_bearing_report_rejected")
    base_rows = [row for row in (base.get("results") or []) if isinstance(row, dict)]
    corrective_rows = [row for row in (corrective.get("results") or []) if isinstance(row, dict)]
    indexed = {
        (str(row.get("scenario_id") or ""), int(row.get("round") or 0)): dict(row)
        for row in base_rows
    }
    failed_before = {key for key, row in indexed.items() if row.get("status") != "pass"}
    replaced = []
    for row in corrective_rows:
        key = (str(row.get("scenario_id") or ""), int(row.get("round") or 0))
        if key not in indexed:
            raise ValueError(f"corrective_result_not_in_base:{key[0]}:{key[1]}")
        if key not in failed_before:
            continue
        indexed[key] = dict(row)
        replaced.append({"scenario_id": key[0], "round": key[1], "new_status": row.get("status")})
    if {row["scenario_id"] + ":" + str(row["round"]) for row in replaced} != {
        key[0] + ":" + str(key[1]) for key in failed_before
    }:
        raise ValueError("not_all_base_failures_replaced")
    rows = [indexed[(str(row.get("scenario_id") or ""), int(row.get("round") or 0))] for row in base_rows]
    failures = [row for row in rows if row.get("status") != "pass"]
    durations = sorted(float(row.get("duration_ms") or 0) for row in rows)
    p95_index = max(0, min(len(durations) - 1, ((95 * len(durations) + 99) // 100) - 1)) if durations else 0
    p95 = durations[p95_index] if durations else 0.0
    maximum = durations[-1] if durations else 0.0
    hard_gate_errors = []
    if p95 > 20000:
        hard_gate_errors.append("overall_latency_p95_exceeded")
    if maximum > 35000:
        hard_gate_errors.append("single_turn_35s_ceiling_exceeded")
    return {
        "report_type": "xiaoyou_agnes_shadow_consolidated_v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "pass" if not failures and not hard_gate_errors else "fail",
        "model": base.get("model"),
        "scenario_set_id": base.get("scenario_set_id"),
        "rounds": base.get("rounds"),
        "replay_count": len(rows),
        "pass_count": len(rows) - len(failures),
        "failure_count": len(failures),
        "warning_count": sum(len(row.get("warnings") or []) for row in rows),
        "fallback_replay_count": sum(1 for row in rows if row.get("fallback_used")),
        "latency_p95_ms": round(p95, 3),
        "latency_max_ms": round(maximum, 3),
        "hard_gate_errors": hard_gate_errors,
        "source_reports": [base_path.name, corrective_path.name],
        "replaced_failures": replaced,
        "real_business_tools_executed": False,
        "real_outbound_enabled": False,
        "production_business_data_read": False,
        "credentials_in_report": False,
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-report", required=True, type=Path)
    parser.add_argument("--corrective-report", required=True, type=Path)
    parser.add_argument("--output-report", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = consolidate(args.base_report, args.corrective_report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = {
            "report_type": "xiaoyou_agnes_shadow_consolidated_v1",
            "status": "fail",
            "error": f"{type(exc).__name__}:{exc}",
            "credentials_in_report": False,
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    args.output_report.parent.mkdir(parents=True, exist_ok=True)
    args.output_report.write_text(rendered + "\n", encoding="utf-8")
    os.chmod(args.output_report, 0o600)
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

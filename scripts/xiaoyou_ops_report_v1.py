#!/usr/bin/env python3
"""Validate and render XiaoU Codex/server operation reports.

This module is deliberately offline and content-blind to XiaoU business data.
It validates the collaboration protocol only. It never connects to GitHub,
executes deployment commands, or mutates production state.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

PROTOCOL = "XIAOU_OPS_REPORT_V1"
ACTIONS = {
    "READ_ONLY_INSPECTION",
    "VERIFY",
    "DEPLOY",
    "ROLLBACK_VERIFY",
}
RESULTS = {"PASS", "FAIL", "BLOCKED"}
READ_ONLY_ACTIONS = {"READ_ONLY_INSPECTION", "VERIFY"}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9._:-]{3,120}$")
MAX_REPORT_BYTES = 64 * 1024
_FORBIDDEN_KEY_PARTS = {
    "authorization",
    "cookie",
    "credential",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
}
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\b(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class ReportValidationError(ValueError):
    pass


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReportValidationError(f"{key}_required")
    return value.strip()


def _validate_sha(value: str, key: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA_RE.fullmatch(normalized):
        raise ReportValidationError(f"{key}_invalid")
    return normalized


def _reject_sensitive_material(value: Any, *, path: str = "report") -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key or "").strip().lower()
            if any(part in key for part in _FORBIDDEN_KEY_PARTS):
                raise ReportValidationError(f"sensitive_key_rejected:{path}.{raw_key}")
            _reject_sensitive_material(child, path=f"{path}.{raw_key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_material(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                raise ReportValidationError(f"sensitive_value_rejected:{path}")


def validate_report(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReportValidationError("report_must_be_object")

    _reject_sensitive_material(payload)

    protocol = _require_text(payload, "protocol")
    if protocol != PROTOCOL:
        raise ReportValidationError("unsupported_protocol")

    command_id = _require_text(payload, "command_id")
    if not _COMMAND_RE.fullmatch(command_id):
        raise ReportValidationError("command_id_invalid")

    pr_number = payload.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise ReportValidationError("pr_number_invalid")

    candidate_sha = _validate_sha(_require_text(payload, "candidate_sha"), "candidate_sha")

    action = _require_text(payload, "action")
    if action not in ACTIONS:
        raise ReportValidationError("action_invalid")

    result = _require_text(payload, "result")
    if result not in RESULTS:
        raise ReportValidationError("result_invalid")

    code_changed = payload.get("code_changed")
    if code_changed is not False:
        raise ReportValidationError("code_changed_must_be_false")

    production_changed = payload.get("production_changed")
    if not isinstance(production_changed, bool):
        raise ReportValidationError("production_changed_must_be_boolean")
    if action in READ_ONLY_ACTIONS and production_changed:
        raise ReportValidationError("read_only_action_cannot_change_production")

    summary = _require_text(payload, "summary")
    if len(summary) > 2000:
        raise ReportValidationError("summary_too_long")

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise ReportValidationError("evidence_must_be_object")

    anomalies = payload.get("anomalies")
    if not isinstance(anomalies, list) or not all(isinstance(item, str) for item in anomalies):
        raise ReportValidationError("anomalies_must_be_string_list")

    normalized = dict(payload)
    normalized.update(
        {
            "protocol": PROTOCOL,
            "command_id": command_id,
            "pr_number": pr_number,
            "candidate_sha": candidate_sha,
            "action": action,
            "result": result,
            "code_changed": False,
            "production_changed": production_changed,
            "summary": summary,
            "evidence": evidence,
            "anomalies": anomalies,
        }
    )

    for key in ("production_before_sha", "production_after_sha"):
        value = normalized.get(key)
        if value not in (None, ""):
            normalized[key] = _validate_sha(str(value), key)

    return normalized


def render_markdown(report: dict[str, Any]) -> str:
    report = validate_report(report)
    anomaly_text = "none" if not report["anomalies"] else "; ".join(report["anomalies"])
    body = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "<!-- xiaou-ops-report:v1 -->\n"
        "## XiaoU Ops Report V1\n\n"
        f"- command: \x60{report['command_id']}\x60\n"
        f"- action: \x60{report['action']}\x60\n"
        f"- candidate: \x60{report['candidate_sha']}\x60\n"
        f"- result: **{report['result']}**\n"
        f"- code changed: \x60false\x60\n"
        f"- production changed: \x60{str(report['production_changed']).lower()}\x60\n"
        f"- anomalies: {anomaly_text}\n\n"
        f"{report['summary']}\n\n"
        "<details><summary>Structured evidence</summary>\n\n"
        "\x60\x60\x60json\n"
        f"{body}\n"
        "\x60\x60\x60\n"
        "</details>\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate/render XIAOU_OPS_REPORT_V1")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--normalized-output", type=Path)
    args = parser.parse_args()

    try:
        raw = args.input.read_bytes()
        if len(raw) > MAX_REPORT_BYTES:
            raise ReportValidationError("report_too_large")
        payload = json.loads(raw.decode("utf-8"))
        normalized = validate_report(payload)
        markdown = render_markdown(normalized)

        if args.normalized_output:
            args.normalized_output.parent.mkdir(parents=True, exist_ok=True)
            args.normalized_output.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if args.markdown_output:
            args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_output.write_text(markdown, encoding="utf-8")
        else:
            print(markdown, end="")
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReportValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

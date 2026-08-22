#!/usr/bin/env python3
"""Probe configured model routes without exposing credentials or response text."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any

import httpx
import yaml


@dataclass(frozen=True)
class ModelRoute:
    index: int
    provider: str
    model: str
    base_url: str
    api_key: str


def _secret(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("${") and text.endswith("}"):
        name = text[2:-1]
        if name.startswith("env:"):
            name = name[4:]
        return str(os.getenv(name, "") or "").strip()
    return text


def load_routes(config_file: Path) -> list[ModelRoute]:
    payload = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    providers = payload.get("custom_providers") if isinstance(payload, dict) else None
    if not isinstance(providers, list):
        raise ValueError("custom_providers_missing")
    routes = []
    seen = set()
    for index, row in enumerate(providers):
        if not isinstance(row, dict):
            continue
        model = str(row.get("model") or "").strip()
        base_url = str(row.get("base_url") or "").strip().rstrip("/")
        api_key = _secret(row.get("api_key"))
        identity = (model.lower(), base_url.lower())
        if not model or not base_url or not api_key or identity in seen:
            continue
        seen.add(identity)
        routes.append(ModelRoute(
            index=index,
            provider=str(row.get("provider") or "custom").strip() or "custom",
            model=model,
            base_url=base_url,
            api_key=api_key,
        ))
    return routes


def probe_routes(
    routes: list[ModelRoute], *, timeout_seconds: float = 12.0,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    rows = []
    with httpx.Client(
        timeout=httpx.Timeout(timeout_seconds, connect=min(8.0, timeout_seconds)),
        transport=transport,
    ) as client:
        for route in routes:
            started = time.monotonic()
            http_status = 0
            error_type = ""
            tool_call_present = False
            try:
                response = client.post(
                    route.base_url + "/chat/completions",
                    headers={"Authorization": f"Bearer {route.api_key}"},
                    json={
                        "model": route.model,
                        "messages": [
                            {"role": "system", "content": "Call the health_ping tool once."},
                            {"role": "user", "content": "Run the synthetic health check."},
                        ],
                        "tools": [{
                            "type": "function",
                            "function": {
                                "name": "health_ping",
                                "description": "Synthetic no-side-effect health check.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }],
                        "tool_choice": "auto",
                        "parallel_tool_calls": False,
                        "temperature": 0,
                        "max_tokens": 64,
                    },
                )
                http_status = response.status_code
                if response.status_code == 200:
                    payload = response.json()
                    message = ((payload.get("choices") or [{}])[0].get("message") or {})
                    tool_call_present = bool(message.get("tool_calls"))
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                error_type = type(exc).__name__
            rows.append({
                "route_index": route.index,
                "provider": route.provider,
                "model": route.model,
                "status": "pass" if http_status == 200 and tool_call_present else "fail",
                "http_status": http_status,
                "tool_call_present": tool_call_present,
                "latency_ms": round((time.monotonic() - started) * 1000, 3),
                "error_type": error_type,
                "credentials_in_report": False,
                "response_text_in_report": False,
            })
    return {
        "report_type": "xiaoyou_model_provider_preflight_v1",
        "status": "pass" if any(row["status"] == "pass" for row in rows) else "fail",
        "route_count": len(rows),
        "passing_route_count": sum(1 for row in rows if row["status"] == "pass"),
        "credentials_in_report": False,
        "response_text_in_report": False,
        "routes": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-file", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=12)
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args()
    try:
        report = probe_routes(load_routes(args.config_file), timeout_seconds=args.timeout_seconds)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        report = {
            "report_type": "xiaoyou_model_provider_preflight_v1",
            "status": "fail",
            "error": f"{type(exc).__name__}:{exc}",
            "credentials_in_report": False,
            "response_text_in_report": False,
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(rendered + "\n", encoding="utf-8")
        os.chmod(args.report_file, 0o600)
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

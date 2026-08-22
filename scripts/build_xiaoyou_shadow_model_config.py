#!/usr/bin/env python3
"""Build a model-only shadow config without copying platform credentials."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

import yaml


ALLOWED_FALLBACK_KEYS = {
    "provider", "model", "base_url", "api_key", "key_env", "api_key_env",
    "context_length", "max_tokens", "request_timeout_seconds", "stale_timeout_seconds",
}
ALLOWED_PRIMARY_KEYS = ALLOWED_FALLBACK_KEYS | {"default", "name"}


def _load(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"config_not_mapping:{path.name}")
    return payload


def _fallbacks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("fallback_providers")
    if isinstance(raw, str) and raw.lstrip().startswith("["):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("fallback_providers_string_invalid") from exc
    if not isinstance(raw, list):
        raise ValueError("fallback_providers_list_required")
    rows = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"fallback_provider_not_mapping:{index}")
        sanitized = {key: deepcopy(value) for key, value in item.items() if key in ALLOWED_FALLBACK_KEYS}
        if not str(sanitized.get("provider") or "").strip() or not str(sanitized.get("model") or "").strip():
            raise ValueError(f"fallback_provider_identity_missing:{index}")
        if not any(str(sanitized.get(key) or "").strip() for key in ("api_key", "key_env", "api_key_env")):
            raise ValueError(f"fallback_provider_credential_reference_missing:{index}")
        rows.append(sanitized)
    if not rows:
        raise ValueError("fallback_provider_chain_empty")
    return rows


def build(source_config: Path, shadow_base_config: Path, output_config: Path, *, apply: bool) -> dict[str, Any]:
    source = _load(source_config)
    shadow = _load(shadow_base_config)
    source_model = source.get("model")
    source_providers = source.get("custom_providers")
    if not isinstance(source_model, dict) or str(source_model.get("model") or "") != "agnes-2.5-flash":
        return {"ok": False, "error": "source_primary_not_agnes"}
    if not isinstance(source_providers, list):
        return {"ok": False, "error": "source_custom_providers_invalid"}
    agnes_rows = [
        row for row in source_providers
        if isinstance(row, dict) and str(row.get("model") or "") == "agnes-2.5-flash"
    ]
    if len(agnes_rows) != 1:
        return {"ok": False, "error": "source_agnes_provider_match_count", "match_count": len(agnes_rows)}
    model = {key: deepcopy(value) for key, value in source_model.items() if key in ALLOWED_PRIMARY_KEYS}
    primary_provider = {
        key: deepcopy(value) for key, value in agnes_rows[0].items() if key in ALLOWED_PRIMARY_KEYS
    }
    if not str(primary_provider.get("base_url") or "").strip():
        return {"ok": False, "error": "source_agnes_base_url_missing"}
    if not any(str(primary_provider.get(key) or "").strip() for key in ("api_key", "key_env", "api_key_env")):
        return {"ok": False, "error": "source_agnes_credential_reference_missing"}
    try:
        fallbacks = _fallbacks(source)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    output = {
        "model": deepcopy(model),
        "custom_providers": [primary_provider],
        "fallback_providers": fallbacks,
        "agent": deepcopy(shadow.get("agent") if isinstance(shadow.get("agent"), dict) else {}),
        "compression": deepcopy(
            shadow.get("compression") if isinstance(shadow.get("compression"), dict) else {}
        ),
    }
    result = {
        "ok": True,
        "apply": apply,
        "primary_model": "agnes-2.5-flash",
        "primary_credential_source": "production_model_route",
        "fallback_models": [str(row.get("model") or "") for row in fallbacks],
        "fallback_count": len(fallbacks),
        "platform_credentials_copied": False,
        "business_data_copied": False,
        "writeback_verified": False,
    }
    if not apply:
        return result
    output_config.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(output, allow_unicode=True, sort_keys=False)
    temporary = output_config.with_name(f".{output_config.name}.{os.getpid()}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, output_config)
    os.chmod(output_config, 0o600)
    verified = _load(output_config)
    result["writeback_verified"] = (
        set(verified) == {"model", "custom_providers", "fallback_providers", "agent", "compression"}
        and len(verified.get("fallback_providers") or []) == len(fallbacks)
    )
    result["ok"] = bool(result["writeback_verified"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config", required=True, type=Path)
    parser.add_argument("--shadow-base-config", required=True, type=Path)
    parser.add_argument("--output-config", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = build(
            args.source_config, args.shadow_base_config, args.output_config, apply=args.apply,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

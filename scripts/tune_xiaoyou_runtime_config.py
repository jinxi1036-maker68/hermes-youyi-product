"""Tune Hermes context limits without printing provider credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import yaml


TUNING = {
    "context_length": 262144,
    "max_tokens": 4096,
    "request_timeout_seconds": 45,
    "stale_timeout_seconds": 60,
}

AGENT_TUNING = {
    "api_max_retries": 2,
    "max_turns": 16,
    "gateway_timeout": 240,
    "gateway_timeout_warning": 60,
}

COMPRESSION_TUNING = {
    "threshold": 0.18,
    "hygiene_hard_message_limit": 80,
}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tune(config_file: Path, backup_dir: Path, *, apply: bool) -> dict[str, object]:
    original = config_file.read_bytes()
    data = yaml.safe_load(original.decode("utf-8"))
    if not isinstance(data, dict):
        return {"ok": False, "error": "config_not_mapping"}

    providers = data.get("custom_providers")
    if not isinstance(providers, list):
        return {"ok": False, "error": "custom_providers_missing"}
    matches = [
        item for item in providers
        if isinstance(item, dict) and str(item.get("model") or "") == "agnes-2.5-flash"
    ]
    if len(matches) != 1:
        return {"ok": False, "error": "agnes_provider_match_count", "match_count": len(matches)}

    provider = matches[0]
    changed: dict[str, dict[str, object]] = {}
    for key, value in TUNING.items():
        before = provider.get(key)
        if before != value:
            changed[f"agnes.{key}"] = {"before": before, "after": value}
            provider[key] = value

    model = data.get("model")
    if not isinstance(model, dict):
        return {"ok": False, "error": "primary_model_not_mapping"}
    if str(model.get("model") or model.get("default") or model.get("name") or "") != "agnes-2.5-flash":
        return {"ok": False, "error": "primary_model_not_agnes"}
    for key, value in TUNING.items():
        before = model.get(key)
        if before != value:
            changed[f"model.{key}"] = {"before": before, "after": value}
            model[key] = value

    agent = data.setdefault("agent", {})
    if not isinstance(agent, dict):
        return {"ok": False, "error": "agent_not_mapping"}
    for key, value in AGENT_TUNING.items():
        before = agent.get(key)
        if before != value:
            changed[f"agent.{key}"] = {"before": before, "after": value}
            agent[key] = value

    fallbacks = data.get("fallback_providers") or []
    if not isinstance(fallbacks, list):
        return {"ok": False, "error": "fallback_providers_not_list"}
    for index, fallback in enumerate(fallbacks):
        if not isinstance(fallback, dict):
            continue
        for key in ("max_tokens", "request_timeout_seconds", "stale_timeout_seconds"):
            value = TUNING[key]
            before = fallback.get(key)
            if before != value:
                changed[f"fallback.{index}.{key}"] = {"before": before, "after": value}
                fallback[key] = value

    compression = data.setdefault("compression", {})
    if not isinstance(compression, dict):
        return {"ok": False, "error": "compression_not_mapping"}
    for key, value in COMPRESSION_TUNING.items():
        before = compression.get(key)
        if before != value:
            changed[f"compression.{key}"] = {"before": before, "after": value}
            compression[key] = value

    result: dict[str, object] = {
        "ok": True,
        "apply": apply,
        "config_file": str(config_file),
        "changed_keys": changed,
        "original_sha256": _hash(original),
        "writeback_verified": False,
        "backup_file": "",
    }
    if not apply or not changed:
        result["writeback_verified"] = not changed
        return result

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    backup = backup_dir / f"config.before-latency-tuning.{stamp}.yaml"
    backup.write_bytes(original)
    os.chmod(backup_dir, 0o700)
    os.chmod(backup, 0o600)

    rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8")
    temporary = config_file.with_name(f".{config_file.name}.{os.getpid()}.tmp")
    temporary.write_bytes(rendered)
    os.replace(temporary, config_file)
    verified_data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    verified_provider = next(
        item for item in verified_data["custom_providers"]
        if isinstance(item, dict) and str(item.get("model") or "") == "agnes-2.5-flash"
    )
    verified = all(verified_provider.get(key) == value for key, value in TUNING.items())
    verified = verified and all(verified_data["model"].get(key) == value for key, value in TUNING.items())
    verified = verified and all(verified_data["agent"].get(key) == value for key, value in AGENT_TUNING.items())
    verified = verified and all(verified_data["compression"].get(key) == value for key, value in COMPRESSION_TUNING.items())
    verified = verified and all(
        fallback.get(key) == TUNING[key]
        for fallback in (verified_data.get("fallback_providers") or [])
        if isinstance(fallback, dict)
        for key in ("max_tokens", "request_timeout_seconds", "stale_timeout_seconds")
    )
    result.update({
        "backup_file": str(backup),
        "updated_sha256": _hash(config_file.read_bytes()),
        "writeback_verified": bool(verified),
        "ok": bool(verified),
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-file", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = tune(args.config_file, args.backup_dir, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

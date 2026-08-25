"""Tune Hermes context limits without printing provider credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import yaml


PRIMARY_TUNING = {
    # Agnes 2.5 Flash currently publishes a 512K context window. Keep this
    # capability declaration separate from the much smaller live-turn budget
    # enforced by COMPRESSION_TUNING below.
    "context_length": 524288,
    "max_tokens": 1600,
    # A WeCom turn gets exactly one Agnes attempt. Production is Agnes-only:
    # transport failure returns an explicit failure instead of switching model.
    "request_timeout_seconds": 9,
    "stale_timeout_seconds": 10,
}

# Compatibility export used by existing deployment tooling: it always means
# the preferred Agnes route, never the fallback route.
TUNING = PRIMARY_TUNING

AGENT_TUNING = {
    # Hermes v0.20 counts the initial attempt here, so 2 means one retry.
    "api_max_retries": 2,
    "max_turns": 8,
    "gateway_timeout": 35,
    "gateway_timeout_warning": 15,
}

COMPRESSION_TUNING = {
    # Hermes v0.20 enforces a model-dependent percentage floor.  The absolute
    # cap is therefore the authority that keeps long WeCom conversations from
    # quietly growing to the model's full 512k context window.
    "threshold": 0.18,
    "threshold_tokens": 32000,
    "target_ratio": 0.22,
    "protect_last_n": 8,
    "min_tail_user_messages": 2,
    "proactive_prune_tokens": 24000,
    "proactive_prune_min_result_chars": 6000,
    "proactive_prune_min_reclaim_tokens": 2048,
    "hygiene_hard_message_limit": 40,
}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owner(path: Path) -> tuple[int, int] | None:
    """Return POSIX ownership when the platform exposes it."""
    if not hasattr(os, "chown"):
        return None
    stat = path.stat()
    if not hasattr(stat, "st_uid") or not hasattr(stat, "st_gid"):
        return None
    return stat.st_uid, stat.st_gid


def _restore_owner(path: Path, expected_owner: tuple[int, int] | None) -> bool:
    """Keep atomic replacements readable by the existing service account."""
    if expected_owner is None:
        return True
    try:
        os.chown(path, *expected_owner)
    except (AttributeError, NotImplementedError):
        return True
    except OSError:
        # A failed chown is only acceptable when the replacement already has
        # the expected owner. Otherwise the caller must reject the write.
        return _owner(path) == expected_owner
    return _owner(path) == expected_owner


def _tune_v020_provider_models(data: dict[str, object], changed: dict[str, dict[str, object]]) -> bool:
    """Write the timeout shape Hermes v0.20 resolves at request time."""

    providers = data.setdefault("providers", {})
    if not isinstance(providers, dict):
        return False
    custom = providers.setdefault("custom", {})
    if not isinstance(custom, dict):
        return False
    models = custom.setdefault("models", {})
    if not isinstance(models, dict):
        return False
    for model_name, tuning in (("agnes-2.5-flash", PRIMARY_TUNING),):
        model_config = models.setdefault(model_name, {})
        if not isinstance(model_config, dict):
            return False
        for key in ("request_timeout_seconds", "stale_timeout_seconds"):
            # Hermes v0.20 calls these timeout_seconds/stale_timeout_seconds
            # under providers.custom.models.<model>.
            configured_key = "timeout_seconds" if key == "request_timeout_seconds" else key
            value = tuning[key]
            before = model_config.get(configured_key)
            if before != value:
                changed[f"providers.custom.models.{model_name}.{configured_key}"] = {
                    "before": before,
                    "after": value,
                }
                model_config[configured_key] = value
    if "deepseek-v4-flash" in models:
        models.pop("deepseek-v4-flash", None)
        changed["providers.custom.models.deepseek-v4-flash"] = {
            "before": "configured",
            "after": "removed_agnes_only",
        }
    return True


def tune(config_file: Path, backup_dir: Path, *, apply: bool) -> dict[str, object]:
    original = config_file.read_bytes()
    original_stat = config_file.stat()
    original_mode = original_stat.st_mode & 0o777
    original_owner = _owner(config_file)
    data = yaml.safe_load(original.decode("utf-8"))
    if not isinstance(data, dict):
        return {"ok": False, "error": "config_not_mapping"}

    providers = data.get("custom_providers", [])
    if not isinstance(providers, list):
        return {"ok": False, "error": "custom_providers_missing"}
    matches = [
        item for item in providers
        if isinstance(item, dict) and str(item.get("model") or "") == "agnes-2.5-flash"
    ]
    if len(matches) > 1:
        return {"ok": False, "error": "agnes_provider_match_count", "match_count": len(matches)}

    changed: dict[str, dict[str, object]] = {}
    if not _tune_v020_provider_models(data, changed):
        return {"ok": False, "error": "v020_provider_models_not_mapping"}
    if matches:
        provider = matches[0]
        for key, value in PRIMARY_TUNING.items():
            before = provider.get(key)
            if before != value:
                changed[f"agnes.{key}"] = {"before": before, "after": value}
                provider[key] = value

    model = data.get("model")
    if not isinstance(model, dict):
        return {"ok": False, "error": "primary_model_not_mapping"}
    if str(model.get("model") or model.get("default") or model.get("name") or "") != "agnes-2.5-flash":
        return {"ok": False, "error": "primary_model_not_agnes"}
    for key, value in PRIMARY_TUNING.items():
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

    fallback_value = data.get("fallback_providers")
    fallback_count = len(fallback_value) if isinstance(fallback_value, list) else (1 if fallback_value else 0)
    fallback_migration: dict[str, object] = {}
    if fallback_value != []:
        changed["fallback_providers"] = {
            "before": f"configured_entries:{fallback_count}",
            "after": "disabled_agnes_only",
        }
        fallback_migration = {
            "from": type(fallback_value).__name__,
            "to": "disabled_agnes_only",
            "count": fallback_count,
        }
    data["fallback_providers"] = []
    if data.get("fallback_model"):
        changed["fallback_model"] = {"before": "configured", "after": "disabled_agnes_only"}
        data["fallback_model"] = ""

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
        "fallback_migration": fallback_migration,
    }
    if not apply or (not changed and not fallback_migration):
        result["writeback_verified"] = not changed and not fallback_migration
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
    os.chmod(temporary, original_mode or 0o600)
    if not _restore_owner(temporary, original_owner):
        return {
            **result,
            "error": "temporary_owner_restore_failed",
            "backup_file": str(backup),
        }
    os.replace(temporary, config_file)
    os.chmod(config_file, original_mode or 0o600)
    ownership_preserved = _restore_owner(config_file, original_owner)
    verified_data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    verified_matches = [
        item for item in (verified_data.get("custom_providers") or [])
        if isinstance(item, dict) and str(item.get("model") or "") == "agnes-2.5-flash"
    ]
    verified = len(verified_matches) <= 1
    verified_providers = verified_data.get("providers")
    verified_custom = verified_providers.get("custom") if isinstance(verified_providers, dict) else {}
    verified_v020_models = verified_custom.get("models") if isinstance(verified_custom, dict) else {}
    verified = verified and isinstance(verified_v020_models, dict)
    if isinstance(verified_v020_models, dict):
        configured = verified_v020_models.get("agnes-2.5-flash")
        verified = verified and isinstance(configured, dict)
        if isinstance(configured, dict):
            verified = verified and configured.get("timeout_seconds") == PRIMARY_TUNING["request_timeout_seconds"]
            verified = verified and configured.get("stale_timeout_seconds") == PRIMARY_TUNING["stale_timeout_seconds"]
        verified = verified and "deepseek-v4-flash" not in verified_v020_models
    if verified_matches:
        verified = verified and all(verified_matches[0].get(key) == value for key, value in PRIMARY_TUNING.items())
    verified = verified and all(verified_data["model"].get(key) == value for key, value in PRIMARY_TUNING.items())
    verified = verified and all(verified_data["agent"].get(key) == value for key, value in AGENT_TUNING.items())
    verified = verified and all(verified_data["compression"].get(key) == value for key, value in COMPRESSION_TUNING.items())
    verified = verified and verified_data.get("fallback_providers") == []
    verified = verified and not verified_data.get("fallback_model")
    permissions_preserved = (config_file.stat().st_mode & 0o777) == (original_mode or 0o600)
    verified = bool(verified and permissions_preserved and ownership_preserved)
    result.update({
        "backup_file": str(backup),
        "updated_sha256": _hash(config_file.read_bytes()),
        "writeback_verified": verified,
        "permissions_preserved": permissions_preserved,
        "ownership_preserved": ownership_preserved,
        "ok": verified,
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

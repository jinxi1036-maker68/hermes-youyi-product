"""Prepare a reversible Hermes-native context-compaction candidate for XiaoU.

This utility only changes semantic context-compression settings. It does not
change model/provider choice, reasoning policy, Tool routing, permissions,
business data or session history.

Production apply is fail-closed unless the caller explicitly confirms a
Hermes 0.21 runtime and the configured primary model is agnes-3.0-flash.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from typing import Any

import yaml


TARGET_MODEL = "agnes-3.0-flash"
SUPPORTED_RUNTIME_PREFIX = "0.21"

# EV-LAT-001 observed a clear latency cliff once provider input reached ~24K.
# Use Hermes' own semantic compressor rather than deleting/rotating history.
# Recent turns stay verbatim; older turns are summarized/recoverable by Hermes.
CANDIDATE_COMPRESSION: dict[str, Any] = {
    "enabled": True,
    "threshold": 0.50,
    "threshold_tokens": 24_000,
    "target_ratio": 0.20,
    "tail_mode": "lean",
    "protect_last_n": 20,
    "min_tail_user_messages": 3,
}

# Use the already-selected main model route for compression summaries. This
# does not switch XiaoU to a weaker model or create a second business brain.
CANDIDATE_AUXILIARY_COMPRESSION: dict[str, Any] = {
    "provider": "main",
}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("config_not_mapping")
    return payload


def _primary_model_name(payload: dict[str, Any]) -> str:
    model = payload.get("model")
    if not isinstance(model, dict):
        return ""
    return str(
        model.get("name")
        or model.get("model")
        or model.get("default")
        or ""
    ).strip()


def _owner(path: Path) -> tuple[int, int] | None:
    if not hasattr(os, "chown"):
        return None
    stat = path.stat()
    if not hasattr(stat, "st_uid") or not hasattr(stat, "st_gid"):
        return None
    return stat.st_uid, stat.st_gid


def _restore_owner(path: Path, expected_owner: tuple[int, int] | None) -> bool:
    if expected_owner is None:
        return True
    try:
        os.chown(path, *expected_owner)
    except (AttributeError, NotImplementedError):
        return True
    except OSError:
        return _owner(path) == expected_owner
    return _owner(path) == expected_owner


def _change_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    changed: dict[str, dict[str, Any]] = {}
    compression = payload.get("compression")
    compression = compression if isinstance(compression, dict) else {}
    for key, value in CANDIDATE_COMPRESSION.items():
        before = compression.get(key)
        if before != value:
            changed[f"compression.{key}"] = {"before": before, "after": value}

    auxiliary = payload.get("auxiliary")
    auxiliary = auxiliary if isinstance(auxiliary, dict) else {}
    aux_compression = auxiliary.get("compression")
    aux_compression = aux_compression if isinstance(aux_compression, dict) else {}
    for key, value in CANDIDATE_AUXILIARY_COMPRESSION.items():
        before = aux_compression.get(key)
        if before != value:
            changed[f"auxiliary.compression.{key}"] = {"before": before, "after": value}
    return changed


def _apply_candidate(payload: dict[str, Any]) -> None:
    compression = payload.setdefault("compression", {})
    if not isinstance(compression, dict):
        raise ValueError("compression_not_mapping")
    compression.update(CANDIDATE_COMPRESSION)

    auxiliary = payload.setdefault("auxiliary", {})
    if not isinstance(auxiliary, dict):
        raise ValueError("auxiliary_not_mapping")
    aux_compression = auxiliary.setdefault("compression", {})
    if not isinstance(aux_compression, dict):
        raise ValueError("auxiliary_compression_not_mapping")
    aux_compression.update(CANDIDATE_AUXILIARY_COMPRESSION)


def tune(
    config_file: Path,
    backup_dir: Path,
    *,
    runtime_version: str = "",
    apply: bool = False,
) -> dict[str, Any]:
    original = config_file.read_bytes()
    original_mode = config_file.stat().st_mode & 0o777
    original_owner = _owner(config_file)
    payload = _load(config_file)

    model_name = _primary_model_name(payload)
    if model_name != TARGET_MODEL:
        return {
            "ok": False,
            "error": "primary_model_not_agnes_3",
            "primary_model": model_name,
            "production_changed": False,
        }

    if apply and not str(runtime_version or "").startswith(SUPPORTED_RUNTIME_PREFIX):
        return {
            "ok": False,
            "error": "runtime_021_not_verified",
            "runtime_version": str(runtime_version or ""),
            "production_changed": False,
        }

    try:
        changed = _change_map(payload)
        _apply_candidate(payload)
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "production_changed": False,
        }

    result: dict[str, Any] = {
        "ok": True,
        "apply": bool(apply),
        "primary_model": model_name,
        "runtime_version_verified": bool(
            str(runtime_version or "").startswith(SUPPORTED_RUNTIME_PREFIX)
        ),
        "changed_keys": changed,
        "original_sha256": _hash(original),
        "backup_file": "",
        "writeback_verified": False,
        "production_changed": False,
    }
    if not apply:
        result["writeback_verified"] = not changed
        return result

    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    backup = backup_dir / f"{config_file.name}.before-context-compaction-v1.yaml"
    if backup.exists():
        return {
            **result,
            "ok": False,
            "error": "backup_already_exists",
            "backup_file": str(backup),
        }
    backup.write_bytes(original)
    os.chmod(backup, 0o600)

    rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")
    temporary = config_file.with_name(f".{config_file.name}.{os.getpid()}.tmp")
    temporary.write_bytes(rendered)
    os.chmod(temporary, original_mode or 0o600)
    if not _restore_owner(temporary, original_owner):
        temporary.unlink(missing_ok=True)
        return {
            **result,
            "ok": False,
            "error": "temporary_owner_restore_failed",
            "backup_file": str(backup),
        }
    os.replace(temporary, config_file)
    os.chmod(config_file, original_mode or 0o600)
    ownership_preserved = _restore_owner(config_file, original_owner)

    verified = _load(config_file)
    verified_model = _primary_model_name(verified)
    verified_compression = verified.get("compression")
    verified_aux = verified.get("auxiliary")
    verified_aux_compression = (
        verified_aux.get("compression")
        if isinstance(verified_aux, dict)
        else None
    )
    semantics_ok = (
        verified_model == TARGET_MODEL
        and isinstance(verified_compression, dict)
        and all(
            verified_compression.get(key) == value
            for key, value in CANDIDATE_COMPRESSION.items()
        )
        and isinstance(verified_aux_compression, dict)
        and all(
            verified_aux_compression.get(key) == value
            for key, value in CANDIDATE_AUXILIARY_COMPRESSION.items()
        )
    )
    mode_preserved = (config_file.stat().st_mode & 0o777) == (original_mode or 0o600)
    ok = bool(semantics_ok and mode_preserved and ownership_preserved)

    result.update({
        "ok": ok,
        "backup_file": str(backup),
        "updated_sha256": _hash(config_file.read_bytes()),
        "writeback_verified": ok,
        "permissions_preserved": mode_preserved,
        "ownership_preserved": ownership_preserved,
        "production_changed": ok,
    })
    return result


def rollback(
    config_file: Path,
    backup_file: Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    if not backup_file.exists():
        return {
            "ok": False,
            "error": "rollback_backup_missing",
            "production_changed": False,
        }
    backup = backup_file.read_bytes()
    current = config_file.read_bytes()
    result: dict[str, Any] = {
        "ok": True,
        "apply": bool(apply),
        "current_sha256": _hash(current),
        "rollback_sha256": _hash(backup),
        "production_changed": False,
        "writeback_verified": not apply,
    }
    if not apply:
        return result

    current_mode = config_file.stat().st_mode & 0o777
    current_owner = _owner(config_file)
    temporary = config_file.with_name(f".{config_file.name}.{os.getpid()}.rollback.tmp")
    temporary.write_bytes(backup)
    os.chmod(temporary, current_mode or 0o600)
    if not _restore_owner(temporary, current_owner):
        temporary.unlink(missing_ok=True)
        return {
            **result,
            "ok": False,
            "error": "rollback_owner_restore_failed",
        }
    os.replace(temporary, config_file)
    os.chmod(config_file, current_mode or 0o600)
    ownership_preserved = _restore_owner(config_file, current_owner)
    verified = _hash(config_file.read_bytes()) == _hash(backup)
    result.update({
        "ok": bool(verified and ownership_preserved),
        "writeback_verified": bool(verified and ownership_preserved),
        "ownership_preserved": ownership_preserved,
        "production_changed": bool(verified and ownership_preserved),
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-file", required=True, type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--runtime-version", default="")
    parser.add_argument("--rollback-from", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        if args.rollback_from is not None:
            result = rollback(
                args.config_file,
                args.rollback_from,
                apply=args.apply,
            )
        else:
            if args.backup_dir is None:
                parser.error("--backup-dir is required unless --rollback-from is used")
            result = tune(
                args.config_file,
                args.backup_dir,
                runtime_version=args.runtime_version,
                apply=args.apply,
            )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "production_changed": False,
        }

    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create and verify a protected exact rollback snapshot of Gateway inputs.

The snapshot copies exact bytes but never prints file contents. The manifest
contains only paths, hashes and filesystem metadata needed for a later
controlled rollback.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any


LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_input(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError("input_must_be_LABEL=PATH")
    label, raw = value.split("=", 1)
    label = label.strip()
    raw = raw.strip()
    if not LABEL_RE.fullmatch(label):
        raise ValueError(f"invalid_input_label:{label}")
    if not raw:
        raise ValueError(f"input_path_missing:{label}")
    return label, Path(raw)


def _inspect_inputs(inputs: list[tuple[str, Path]]) -> dict[str, Any]:
    labels: set[str] = set()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for label, path in inputs:
        if label in labels:
            errors.append(f"duplicate_label:{label}")
            continue
        labels.add(label)
        if path.is_symlink():
            errors.append(f"input_symlink_forbidden:{label}")
            continue
        if not path.is_file():
            errors.append(f"input_regular_file_required:{label}")
            continue
        meta = path.stat()
        rows.append({
            "label": label,
            "source_path": str(path.resolve()),
            "sha256": _sha256(path),
            "size": meta.st_size,
            "mode": f"{meta.st_mode & 0o777:04o}",
            "uid": getattr(meta, "st_uid", None),
            "gid": getattr(meta, "st_gid", None),
        })
    if not inputs:
        errors.append("at_least_one_input_required")
    return {
        "ok": not errors and len(rows) == len(inputs),
        "error": "" if not errors and len(rows) == len(inputs) else "gateway_snapshot_input_invalid",
        "errors": errors,
        "inputs": rows,
    }


def create_gateway_snapshot(
    *,
    inputs: list[tuple[str, Path]],
    output_root: Path,
    snapshot_name: str = "",
    apply: bool = False,
) -> dict[str, Any]:
    inspected = _inspect_inputs(inputs)
    result: dict[str, Any] = {
        **inspected,
        "apply": bool(apply),
        "output_root": str(output_root.absolute()),
        "snapshot_created": False,
        "runtime_inputs_changed": False,
        "secret_content_exposed": False,
    }
    if not inspected.get("ok") or not apply:
        return result

    output_root = output_root.absolute()
    if output_root.is_symlink():
        return {**result, "ok": False, "error": "output_root_symlink_forbidden"}
    if output_root.exists():
        if not output_root.is_dir():
            return {**result, "ok": False, "error": "output_root_not_directory"}
        if (output_root.stat().st_mode & 0o777) != 0o700:
            return {
                **result,
                "ok": False,
                "error": "output_root_mode_must_be_0700",
                "observed_mode": f"{output_root.stat().st_mode & 0o777:04o}",
            }
    else:
        output_root.mkdir(parents=True, mode=0o700)
        os.chmod(output_root, 0o700)

    fingerprint = hashlib.sha256(
        "\\n".join(
            f"{row['label']}:{row['source_path']}:{row['sha256']}"
            for row in inspected["inputs"]
        ).encode("utf-8")
    ).hexdigest()[:12]
    name = snapshot_name.strip() or (
        "gateway-pre-switch-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + fingerprint
    )
    if not LABEL_RE.fullmatch(name):
        return {**result, "ok": False, "error": "invalid_snapshot_name"}

    snapshot_root = output_root / name
    if snapshot_root.exists() or snapshot_root.is_symlink():
        return {**result, "ok": False, "error": "snapshot_already_exists", "snapshot_root": str(snapshot_root)}

    files_root = snapshot_root / "files"
    files_root.mkdir(parents=True, mode=0o700)
    os.chmod(snapshot_root, 0o700)
    os.chmod(files_root, 0o700)

    manifest_rows: list[dict[str, Any]] = []
    try:
        for row in inspected["inputs"]:
            label = str(row["label"])
            source = Path(str(row["source_path"]))
            target = files_root / label
            shutil.copyfile(source, target, follow_symlinks=False)
            os.chmod(target, 0o600)
            copied_hash = _sha256(target)
            if copied_hash != row["sha256"]:
                raise OSError(f"snapshot_hash_mismatch:{label}")
            manifest_rows.append({
                **row,
                "snapshot_relative_path": f"files/{label}",
                "snapshot_sha256": copied_hash,
                "snapshot_mode": "0600",
            })

        manifest = {
            "schema_version": "xiaoyou_gateway_rollback_snapshot_v1",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "snapshot_root": str(snapshot_root),
            "files": manifest_rows,
            "restore_contract": {
                "semantics": "restore exact bytes to source_path and restore recorded mode/uid/gid before service activation",
                "requires_explicit_authorization": True,
                "automatic_restore_performed": False,
            },
            "runtime_inputs_changed": False,
            "secret_content_exposed": False,
        }
        manifest_path = snapshot_root / "rollback_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o600)
        verified = verify_gateway_snapshot(manifest_path)
        if not verified.get("ok"):
            raise OSError("snapshot_post_verify_failed")
        return {
            **result,
            "ok": True,
            "snapshot_created": True,
            "snapshot_root": str(snapshot_root),
            "manifest": str(manifest_path),
            "file_count": len(manifest_rows),
            "verification": verified,
        }
    except OSError as exc:
        shutil.rmtree(snapshot_root, ignore_errors=True)
        return {
            **result,
            "ok": False,
            "error": str(exc),
            "snapshot_root": str(snapshot_root),
            "snapshot_created": False,
        }


def verify_gateway_snapshot(manifest_path: Path) -> dict[str, Any]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return {"ok": False, "error": "snapshot_manifest_missing_or_symlink"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"snapshot_manifest_invalid:{type(exc).__name__}"}
    if manifest.get("schema_version") != "xiaoyou_gateway_rollback_snapshot_v1":
        return {"ok": False, "error": "snapshot_manifest_schema_unsupported"}

    root = manifest_path.parent.resolve()
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    for row in manifest.get("files") or []:
        label = str(row.get("label") or "")
        relative = Path(str(row.get("snapshot_relative_path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"unsafe_snapshot_path:{label}")
            continue
        snapshot_file = root / relative
        if snapshot_file.is_symlink() or not snapshot_file.is_file():
            errors.append(f"snapshot_file_missing_or_symlink:{label}")
            continue
        actual = _sha256(snapshot_file)
        expected = str(row.get("snapshot_sha256") or "")
        mode = snapshot_file.stat().st_mode & 0o777
        if actual != expected:
            errors.append(f"snapshot_hash_mismatch:{label}")
        if mode != 0o600:
            errors.append(f"snapshot_mode_invalid:{label}")
        rows.append({
            "label": label,
            "sha256": actual,
            "expected_sha256": expected,
            "mode": f"{mode:04o}",
            "ok": actual == expected and mode == 0o600,
        })

    return {
        "ok": not errors and bool(rows),
        "error": "" if not errors and rows else "snapshot_verification_failed",
        "errors": errors,
        "manifest": str(manifest_path.resolve()),
        "snapshot_root": str(root),
        "file_count": len(rows),
        "files": rows,
        "runtime_inputs_changed": False,
        "secret_content_exposed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create/verify a protected exact Gateway rollback snapshot.")
    parser.add_argument("--input", action="append", default=[], help="LABEL=PATH; may be repeated")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--snapshot-name", default="")
    parser.add_argument("--verify-manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        if args.verify_manifest is not None:
            result = verify_gateway_snapshot(args.verify_manifest)
        else:
            if args.output_root is None:
                parser.error("--output-root is required unless --verify-manifest is used")
            parsed = [_parse_input(value) for value in args.input]
            result = create_gateway_snapshot(
                inputs=parsed,
                output_root=args.output_root,
                snapshot_name=args.snapshot_name,
                apply=args.apply,
            )
    except (OSError, ValueError) as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "runtime_inputs_changed": False,
            "secret_content_exposed": False,
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

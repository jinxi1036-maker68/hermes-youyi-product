#!/usr/bin/env python3
"""Build and verify a versioned Xiaoyou runtime release package."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "xiaoyou_release_manifest_v1"
PAYLOAD_ROOTS = (
    "runtime/plugins/tuoguan_core",
    "runtime/plugins/platforms/wecom",
    "scripts",
    "systemd",
    "work/commercialization/xiaoyou_runtime_constitution_v1.md",
    "work/commercialization/xiaoyou_reliability_scenarios_v1.json",
)
FORBIDDEN_PARTS = {"__pycache__", ".pytest_cache", ".git", ".venv", "data", "logs", "backups"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3"}
PRODUCTION_FORBIDDEN_OVERLAY_PATHS = {
    "runtime/hermes_constants.py",
    "runtime/gateway/__init__.py",
    "runtime/gateway/config.py",
    "runtime/gateway/session.py",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("git_commit_unavailable")
    return completed.stdout.strip()


def tracked_tree_dirty(root: Path) -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("git_status_unavailable")
    return bool(completed.stdout.strip())


def payload_files(root: Path) -> list[Path]:
    command = ["git", "ls-files", "--", *PAYLOAD_ROOTS]
    completed = subprocess.run(
        command, cwd=root, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("git_file_inventory_unavailable")
    files: list[Path] = []
    for value in completed.stdout.splitlines():
        relative = Path(value)
        path = root / relative
        if not path.is_file():
            continue
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            continue
        if relative.suffix.lower() in FORBIDDEN_SUFFIXES:
            continue
        if relative.as_posix() in PRODUCTION_FORBIDDEN_OVERLAY_PATHS:
            continue
        files.append(relative)
    return sorted(files, key=lambda item: item.as_posix())


def verify_release_directory(release_root: Path) -> dict[str, Any]:
    manifest_path = release_root / "release_manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "error": "release_manifest_missing", "release_root": str(release_root)}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": "release_manifest_invalid", "detail": str(exc)}
    if manifest.get("schema_version") != SCHEMA_VERSION:
        return {"ok": False, "error": "release_manifest_schema_unsupported"}
    mismatches: list[dict[str, str]] = []
    payload = release_root / str(manifest.get("payload_root") or "payload")
    listed_paths: set[str] = set()
    for row in manifest.get("files") or []:
        if not isinstance(row, dict):
            continue
        relative = str(row.get("path") or "")
        listed_paths.add(relative)
        path = payload / relative
        if not path.is_file():
            mismatches.append({"path": relative, "error": "missing"})
            continue
        actual = sha256_file(path)
        expected = str(row.get("sha256") or "")
        if actual != expected:
            mismatches.append({"path": relative, "error": "hash_mismatch"})
    actual_paths = {
        path.relative_to(payload).as_posix()
        for path in payload.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(payload).parts
        and path.suffix.lower() not in {".pyc", ".pyo"}
    } if payload.is_dir() else set()
    unlisted = sorted(actual_paths - listed_paths)
    missing_from_manifest = sorted(listed_paths - actual_paths)
    ok = not mismatches and not unlisted and not missing_from_manifest and len(listed_paths) == int(manifest.get("file_count") or -1)
    return {
        "ok": ok,
        "release_root": str(release_root),
        "release_id": str(manifest.get("release_id") or ""),
        "source_commit": str(manifest.get("source_commit") or ""),
        "file_count": len(listed_paths),
        "mismatches": mismatches,
        "unlisted_files": unlisted,
        "missing_files": missing_from_manifest,
        "manifest": manifest,
    }


def build_release(
    *,
    root: Path,
    output_dir: Path,
    hermes_version: str,
    model: str,
    require_clean: bool = True,
    source_commit: str = "",
) -> dict[str, Any]:
    commit = source_commit or git_commit(root)
    if require_clean and tracked_tree_dirty(root):
        raise RuntimeError("tracked_worktree_dirty")
    release_id = f"xiaoyou-{commit[:12]}"
    release_root = output_dir / release_id
    if release_root.exists():
        shutil.rmtree(release_root)
    payload = release_root / "payload"
    payload.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for relative in payload_files(root):
        source = root / relative
        target = payload / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        rows.append({
            "path": relative.as_posix(),
            "size": target.stat().st_size,
            "sha256": sha256_file(target),
        })
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "source_commit": commit,
        "hermes_version": str(hermes_version),
        "model": str(model),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "payload_root": "payload",
        "file_count": len(rows),
        "files": rows,
        "production_import_policy": "one_canonical_release_with_verified_links",
        "production_overlay_policy": "allowlisted_xiaoyou_payload_only_no_core_shadow",
        "production_forbidden_overlay_paths": sorted(PRODUCTION_FORBIDDEN_OVERLAY_PATHS),
        "contains_business_data": False,
        "contains_credentials": False,
    }
    manifest_path = release_root / "release_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = output_dir / f"{release_id}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as handle:
        for path in sorted(release_root.rglob("*")):
            if path.is_file():
                handle.write(path, Path(release_id) / path.relative_to(release_root))
    verification = verify_release_directory(release_root)
    if not verification.get("ok"):
        raise RuntimeError("release_verification_failed")
    return {
        "ok": True,
        "release_id": release_id,
        "release_root": str(release_root),
        "archive": str(archive),
        "archive_sha256": sha256_file(archive),
        "file_count": len(rows),
        "source_commit": commit,
        "hermes_version": str(hermes_version),
        "model": str(model),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or verify a Xiaoyou release package.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--hermes-version", default="0.20.0")
    parser.add_argument("--model", default="agnes-2.5-flash")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    try:
        if args.verify:
            result = verify_release_directory(args.verify)
        else:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            result = build_release(
                root=args.root,
                output_dir=args.output_dir,
                hermes_version=args.hermes_version,
                model=args.model,
                require_clean=not args.allow_dirty,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create and verify an isolated Xiaoyou tenant backup and restore drill."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
from typing import Any


BACKUP_DIRS = ("config", "data", "skills", "memory", "reports")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(root: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    for directory in BACKUP_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.is_symlink():
                output[path.relative_to(root).as_posix()] = _sha256(path)
    return output


def run_backup_restore_drill(
    *,
    source: Path,
    work_dir: Path | None = None,
    keep_artifacts: bool = False,
) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_dir():
        return {"ok": False, "error": "source_directory_missing", "source": str(source)}
    parent = work_dir or Path(tempfile.mkdtemp(prefix="xiaoyou-restore-drill-"))
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = parent / f"tenant-backup-{stamp}.tar.gz"
    restore_root = parent / "restored"
    before = _inventory(source)
    if not before:
        return {"ok": False, "error": "no_backup_files_found", "source": str(source)}
    with tarfile.open(archive, "w:gz") as handle:
        for directory in BACKUP_DIRS:
            path = source / directory
            if path.is_dir():
                handle.add(path, arcname=directory, recursive=True)
    restore_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as handle:
        members = handle.getmembers()
        unsafe = [member.name for member in members if member.name.startswith(("/", "\\")) or ".." in Path(member.name).parts]
        if unsafe:
            return {"ok": False, "error": "unsafe_archive_member", "members": unsafe[:20]}
        handle.extractall(restore_root, filter="data")
    after = _inventory(restore_root)
    mismatches = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    result = {
        "ok": not mismatches,
        "schema_version": "xiaoyou_backup_restore_drill_v1",
        "source": str(source),
        "file_count": len(before),
        "archive": str(archive),
        "archive_sha256": _sha256(archive),
        "restore_root": str(restore_root),
        "hash_mismatches": mismatches,
        "production_modified": False,
        "offsite_backup": {
            "enabled": False,
            "reason": "offsite_credentials_not_configured",
            "requirement": "encrypted remote copy plus monthly restore evidence",
        },
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    report_path = parent / "restore-drill-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    if not keep_artifacts:
        archive.unlink(missing_ok=True)
        shutil.rmtree(restore_root, ignore_errors=True)
        result["ephemeral_artifacts_removed"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an isolated Xiaoyou tenant backup/restore drill.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--keep-artifacts", action="store_true")
    args = parser.parse_args()
    try:
        result = run_backup_restore_drill(source=args.source, work_dir=args.work_dir, keep_artifacts=args.keep_artifacts)
    except (OSError, tarfile.TarError, ValueError) as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}:{exc}", "production_modified": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

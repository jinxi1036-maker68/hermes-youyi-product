"""Storage audit and conservative archival for Youyi Hermes data."""

from __future__ import annotations

import argparse
import json
import os
import tarfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import resolve_tuoguan_data_dir


POLICY_FILE = "storage_retention_policy.json"
RUN_LEDGER_FILE = "storage_maintenance_runs.jsonl"
REPORT_PREFIX = "autonomous-wakeup-v1-"
AUDIT_PREFIX = "storage-audit-"
DRY_RUN_PREFIX = "storage-dry-run-"

CORE_BUSINESS_FILES = {
    "business_action_audit.jsonl",
    "reply_ledger.jsonl",
    "records.json",
    "tasks.json",
    "students.json",
    "staff.json",
    "teacher_wecom_map.json",
    "wecom_whitelist.json",
    "goal_operator_goals.json",
}

DEFAULT_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "mode": "conservative_compress",
    "detailed_wakeup_report_retention_days": 7,
    "prewrite_backup_retention_days": 7,
    "archive_review_after_days": 30,
    "delete_business_ledgers": False,
    "protected_files": sorted(CORE_BUSINESS_FILES),
    "notes": [
        "Reports and prewrite backups may be compressed after their retention window.",
        "Core business data and audit ledgers are never deleted by this maintenance tool.",
        "Archived attention/work materials remain audit evidence, not model default context.",
    ],
}


@dataclass(frozen=True)
class ArchiveCandidate:
    kind: str
    archive_key: str
    archive_path: Path
    source_paths: tuple[Path, ...]
    total_bytes: int


def load_policy(data_dir: Path) -> dict[str, Any]:
    path = data_dir / POLICY_FILE
    if not path.exists():
        return dict(DEFAULT_POLICY)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(DEFAULT_POLICY)
    policy = dict(DEFAULT_POLICY)
    if isinstance(loaded, dict):
        policy.update(loaded)
    policy["protected_files"] = sorted(set(policy.get("protected_files") or []) | CORE_BUSINESS_FILES)
    return policy


def ensure_policy_file(data_dir: Path) -> Path:
    path = data_dir / POLICY_FILE
    if not path.exists():
        _write_json(path, DEFAULT_POLICY)
    return path


def audit_storage(data_dir: Path, *, now: datetime | None = None, write_report: bool = True) -> dict[str, Any]:
    now = _aware(now)
    data_dir = data_dir.expanduser()
    ensure_policy_file(data_dir)
    policy = load_policy(data_dir)
    files = [p for p in data_dir.rglob("*") if p.is_file()]
    total_bytes = sum(_size(p) for p in files)
    top_files = sorted(
        (_file_row(data_dir, p) for p in files),
        key=lambda row: int(row["size_bytes"]),
        reverse=True,
    )[:40]
    jsonl_counts = sorted(
        (_jsonl_row(data_dir, p) for p in data_dir.glob("*.jsonl") if p.is_file()),
        key=lambda row: row["line_count"],
        reverse=True,
    )
    reports_by_day = _report_stats_by_day(data_dir)
    root_owned = [_file_row(data_dir, p) for p in files if _owner_name(p) == "root"]
    recent_cutoff = now - timedelta(hours=24)
    recent_growth = sorted(
        (_file_row(data_dir, p) for p in files if _mtime(p) >= recent_cutoff),
        key=lambda row: int(row["size_bytes"]),
        reverse=True,
    )[:40]
    dir_sizes = _top_level_dir_sizes(data_dir)
    result = {
        "ok": True,
        "mode": "audit",
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "data_dir": str(data_dir),
        "total_size_bytes": total_bytes,
        "total_size_human": _human_size(total_bytes),
        "policy": policy,
        "top_level_sizes": dir_sizes,
        "largest_files": top_files,
        "jsonl_counts": jsonl_counts,
        "reports_by_day": reports_by_day,
        "root_owned_files": root_owned[:80],
        "root_owned_count": len(root_owned),
        "recent_24h_largest_files": recent_growth,
        "file_classes": _file_classes_summary(data_dir),
        "business_data_modified": False,
    }
    if write_report:
        paths = _write_maintenance_report(
            data_dir,
            AUDIT_PREFIX,
            now,
            result,
            _render_audit_md(result),
        )
        result["report_paths"] = [str(p) for p in paths]
    return result


def plan_archival(data_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = _aware(now)
    ensure_policy_file(data_dir)
    policy = load_policy(data_dir)
    candidates = _archive_candidates(data_dir, policy=policy, now=now)
    result = {
        "ok": True,
        "mode": "dry-run",
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "data_dir": str(data_dir),
        "policy": policy,
        "candidate_count": len(candidates),
        "estimated_reclaimable_bytes": sum(item.total_bytes for item in candidates),
        "estimated_reclaimable_human": _human_size(sum(item.total_bytes for item in candidates)),
        "candidates": [_candidate_row(data_dir, item) for item in candidates],
        "manual_review_candidates": _oversized_recent_reports(data_dir, policy=policy, now=now),
        "protected_files": sorted(CORE_BUSINESS_FILES),
        "business_data_modified": False,
    }
    paths = _write_maintenance_report(
        data_dir,
        DRY_RUN_PREFIX,
        now,
        result,
        _render_dry_run_md(result),
    )
    result["report_paths"] = [str(p) for p in paths]
    return result


def apply_archival(data_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = _aware(now)
    ensure_policy_file(data_dir)
    policy = load_policy(data_dir)
    candidates = _archive_candidates(data_dir, policy=policy, now=now)
    archived: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for candidate in candidates:
        if any(_is_protected(data_dir, p) for p in candidate.source_paths):
            skipped.append({**_candidate_row(data_dir, candidate), "reason": "protected_file"})
            continue
        if candidate.archive_path.exists():
            skipped.append({**_candidate_row(data_dir, candidate), "reason": "archive_already_exists"})
            continue
        try:
            candidate.archive_path.parent.mkdir(parents=True, exist_ok=True)
            _write_tar(candidate.archive_path, data_dir, candidate.source_paths)
            verified = _verify_tar(candidate.archive_path, data_dir, candidate.source_paths)
            if not verified:
                raise RuntimeError("archive verification failed")
            for source in candidate.source_paths:
                if source.exists():
                    source.unlink()
            archived.append(_candidate_row(data_dir, candidate))
        except Exception as exc:  # pragma: no cover - kept defensive for production IO
            errors.append({**_candidate_row(data_dir, candidate), "error": str(exc)})
    reclaimed = sum(int(item.get("total_bytes") or 0) for item in archived)
    result = {
        "ok": not errors,
        "mode": "apply",
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "data_dir": str(data_dir),
        "archived_count": len(archived),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "reclaimed_bytes": reclaimed,
        "reclaimed_human": _human_size(reclaimed),
        "archived": archived,
        "skipped": skipped,
        "errors": errors,
        "business_data_modified": False,
    }
    _append_jsonl(data_dir / RUN_LEDGER_FILE, result)
    paths = _write_maintenance_report(
        data_dir,
        "storage-apply-",
        now,
        result,
        _render_apply_md(result),
    )
    result["report_paths"] = [str(p) for p in paths]
    return result


def _archive_candidates(data_dir: Path, *, policy: dict[str, Any], now: datetime) -> list[ArchiveCandidate]:
    report_days = int(policy.get("detailed_wakeup_report_retention_days") or 7)
    prewrite_days = int(policy.get("prewrite_backup_retention_days") or 7)
    report_cutoff = now - timedelta(days=report_days)
    prewrite_cutoff = now - timedelta(days=prewrite_days)
    candidates: list[ArchiveCandidate] = []
    report_groups: dict[str, list[Path]] = defaultdict(list)
    reports_dir = data_dir / "reports"
    if reports_dir.exists():
        for path in reports_dir.glob(f"{REPORT_PREFIX}*"):
            if not path.is_file() or path.suffix not in {".json", ".md"}:
                continue
            day = _report_day(path)
            if not day:
                continue
            file_time = _mtime(path)
            if file_time < report_cutoff:
                report_groups[day].append(path)
    for day, paths in sorted(report_groups.items()):
        archive = data_dir / "archives" / "reports" / f"{day}.tar.gz"
        candidates.append(_candidate("reports", day, archive, paths))

    prewrite_root = data_dir / "backup" / "prewrite"
    prewrite_groups: dict[str, list[Path]] = defaultdict(list)
    if prewrite_root.exists():
        for path in prewrite_root.iterdir():
            if not path.is_dir():
                continue
            day = _prewrite_day(path)
            if not day:
                continue
            if _mtime(path) < prewrite_cutoff:
                prewrite_groups[day].extend([p for p in path.rglob("*") if p.is_file()])
    for day, paths in sorted(prewrite_groups.items()):
        archive = data_dir / "archives" / "prewrite" / f"{day}.tar.gz"
        candidates.append(_candidate("prewrite", day, archive, paths))
    return [item for item in candidates if item.source_paths]


def _candidate(kind: str, key: str, archive: Path, paths: list[Path]) -> ArchiveCandidate:
    unique = tuple(sorted({p for p in paths if p.is_file()}))
    return ArchiveCandidate(kind, key, archive, unique, sum(_size(p) for p in unique))


def _candidate_row(data_dir: Path, item: ArchiveCandidate) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "archive_key": item.archive_key,
        "archive_path": _rel(data_dir, item.archive_path),
        "source_count": len(item.source_paths),
        "total_bytes": item.total_bytes,
        "total_human": _human_size(item.total_bytes),
        "source_preview": [_rel(data_dir, p) for p in item.source_paths[:20]],
        "omitted_source_count": max(0, len(item.source_paths) - 20),
    }


def _write_tar(archive_path: Path, data_dir: Path, source_paths: tuple[Path, ...]) -> None:
    tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    with tarfile.open(tmp_path, "w:gz") as tar:
        for source in source_paths:
            tar.add(source, arcname=_rel(data_dir, source))
    tmp_path.replace(archive_path)


def _verify_tar(archive_path: Path, data_dir: Path, source_paths: tuple[Path, ...]) -> bool:
    expected = {_rel(data_dir, p) for p in source_paths}
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            actual = {member.name for member in tar.getmembers() if member.isfile()}
    except (tarfile.TarError, OSError):
        return False
    return expected == actual


def _write_maintenance_report(data_dir: Path, prefix: str, now: datetime, payload: dict[str, Any], rendered: str) -> tuple[Path, Path]:
    reports_dir = data_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{prefix}{now.strftime('%Y%m%d-%H%M%S')}"
    json_path = reports_dir / f"{stem}.json"
    md_path = reports_dir / f"{stem}.md"
    _write_json(json_path, payload)
    md_path.write_text(rendered, encoding="utf-8")
    return json_path, md_path


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _render_audit_md(result: dict[str, Any]) -> str:
    return "\n".join([
        f"# Hermes Storage Audit | {result.get('generated_at')}",
        "",
        f"- Data dir: {result.get('data_dir')}",
        f"- Total size: {result.get('total_size_human')}",
        f"- Root-owned files: {result.get('root_owned_count')}",
        f"- JSONL files: {len(result.get('jsonl_counts') or [])}",
        f"- Report days: {len(result.get('reports_by_day') or [])}",
        "",
        "## Largest Files",
        *[
            f"- {item['size_human']} {item['path']}"
            for item in (result.get("largest_files") or [])[:15]
        ],
        "",
        "## Reports By Day",
        *[
            f"- {item['day']}: {item['file_count']} files, {item['size_human']}"
            for item in (result.get("reports_by_day") or [])
        ],
        "",
        "## Boundary",
        "- This audit does not delete or rewrite business data.",
        "- Storage maintenance manages material lifecycle only; it is not a Router and does not constrain model reasoning.",
    ])


def _render_dry_run_md(result: dict[str, Any]) -> str:
    manual_review_lines = [
        f"- {item['path']}: {item['size_human']} ({item['reason']})"
        for item in (result.get("manual_review_candidates") or [])[:40]
    ] or ["- None"]
    return "\n".join([
        f"# Hermes Storage Dry Run | {result.get('generated_at')}",
        "",
        f"- Candidates: {result.get('candidate_count')}",
        f"- Estimated reclaimable: {result.get('estimated_reclaimable_human')}",
        "- No files were moved, compressed, or deleted.",
        "",
        "## Candidates",
        *[
            f"- {item['kind']} {item['archive_key']}: {item['source_count']} files, {item['total_human']} -> {item['archive_path']}"
            for item in (result.get("candidates") or [])[:40]
        ],
        "",
        "## Manual Review Candidates",
        *manual_review_lines,
        "",
        "## Protected",
        "- Core business JSON/JSONL files are never archived by this tool.",
    ])


def _render_apply_md(result: dict[str, Any]) -> str:
    return "\n".join([
        f"# Hermes Storage Apply | {result.get('generated_at')}",
        "",
        f"- Archived: {result.get('archived_count')}",
        f"- Skipped: {result.get('skipped_count')}",
        f"- Errors: {result.get('error_count')}",
        f"- Reclaimed: {result.get('reclaimed_human')}",
        "",
        "## Archived",
        *[
            f"- {item['kind']} {item['archive_key']}: {item['source_count']} files, {item['total_human']}"
            for item in (result.get("archived") or [])[:40]
        ],
    ])


def _file_classes_summary(data_dir: Path) -> dict[str, Any]:
    return {
        "current_facts": sorted([name for name in CORE_BUSINESS_FILES if (data_dir / name).exists()]),
        "audit_ledgers": sorted([p.name for p in data_dir.glob("*audit*.jsonl")]),
        "temporary_reports": len(list((data_dir / "reports").glob(f"{REPORT_PREFIX}*"))) if (data_dir / "reports").exists() else 0,
        "prewrite_backups": len([p for p in (data_dir / "backup" / "prewrite").iterdir() if p.is_dir()]) if (data_dir / "backup" / "prewrite").exists() else 0,
        "candidate_learning": sorted([p.name for p in data_dir.glob("*candidate*.jsonl")]),
        "compressible_history": ["reports/autonomous-wakeup-v1-*", "backup/prewrite/*"],
    }


def _report_stats_by_day(data_dir: Path) -> list[dict[str, Any]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    reports_dir = data_dir / "reports"
    if reports_dir.exists():
        for path in reports_dir.glob(f"{REPORT_PREFIX}*"):
            day = _report_day(path)
            if day:
                groups[day].append(path)
    rows = []
    for day, paths in sorted(groups.items()):
        size = sum(_size(p) for p in paths)
        rows.append({
            "day": day,
            "file_count": len(paths),
            "size_bytes": size,
            "size_human": _human_size(size),
            "average_kb": round(size / max(1, len(paths)) / 1024, 1),
        })
    return rows


def _oversized_recent_reports(data_dir: Path, *, policy: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    retention_days = int(policy.get("detailed_wakeup_report_retention_days") or 7)
    cutoff = now - timedelta(days=retention_days)
    rows = []
    reports_dir = data_dir / "reports"
    if not reports_dir.exists():
        return rows
    for path in reports_dir.glob(f"{REPORT_PREFIX}*.json"):
        if not path.is_file():
            continue
        size = _size(path)
        if _mtime(path) >= cutoff and size >= 512 * 1024:
            row = _file_row(data_dir, path)
            row["reason"] = "recent oversized wakeup report; keep during retention window, review for later compression"
            rows.append(row)
    return sorted(rows, key=lambda row: int(row["size_bytes"]), reverse=True)[:80]


def _top_level_dir_sizes(data_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in data_dir.iterdir() if data_dir.exists() else []:
        if path.is_dir():
            size = sum(_size(p) for p in path.rglob("*") if p.is_file())
        elif path.is_file():
            size = _size(path)
        else:
            continue
        rows.append({"path": _rel(data_dir, path), "size_bytes": size, "size_human": _human_size(size)})
    return sorted(rows, key=lambda row: int(row["size_bytes"]), reverse=True)[:40]


def _jsonl_row(data_dir: Path, path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            line_count = sum(1 for _ in handle)
    except OSError:
        line_count = -1
    return {**_file_row(data_dir, path), "line_count": line_count}


def _file_row(data_dir: Path, path: Path) -> dict[str, Any]:
    size = _size(path)
    return {
        "path": _rel(data_dir, path),
        "size_bytes": size,
        "size_human": _human_size(size),
        "owner": _owner_name(path),
        "mtime": _mtime(path).isoformat(timespec="seconds"),
    }


def _is_protected(data_dir: Path, path: Path) -> bool:
    rel = _rel(data_dir, path)
    parts = rel.split("/")
    if len(parts) == 1 and parts[0] in CORE_BUSINESS_FILES:
        return True
    if parts[0] == "reports":
        return False
    if len(parts) >= 3 and parts[0] == "backup" and parts[1] == "prewrite":
        return False
    return True


def _report_day(path: Path) -> str:
    name = path.name
    if not name.startswith(REPORT_PREFIX):
        return ""
    rest = name[len(REPORT_PREFIX):]
    day = rest.split("-", 1)[0]
    return day if len(day) == 8 and day.isdigit() else ""


def _prewrite_day(path: Path) -> str:
    name = path.name
    if len(name) >= 8 and name[:8].isdigit():
        return name[:8]
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _owner_name(path: Path) -> str:
    try:
        import pwd
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except Exception:
        return str(path.stat().st_uid)


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone()


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).astimezone()
    return value.astimezone()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Youyi storage maintenance")
    parser.add_argument("mode", choices=["audit", "dry-run", "apply"])
    parser.add_argument("--data-dir", default="")
    args = parser.parse_args(argv)
    if not str(args.data_dir or "").strip() and not os.getenv("HERMES_TUOGUAN_DATA_DIR", "").strip():
        print(json.dumps({
            "ok": False,
            "error": "data_dir_required",
            "message": "storage_maintenance must receive --data-dir or HERMES_TUOGUAN_DATA_DIR to avoid writing reports outside the production data directory.",
        }, ensure_ascii=False, indent=2))
        return 2
    data_dir = resolve_tuoguan_data_dir(args.data_dir or None)
    if args.mode == "audit":
        result = audit_storage(data_dir, write_report=True)
    elif args.mode == "dry-run":
        result = plan_archival(data_dir)
    else:
        result = apply_archival(data_dir)
    print(json.dumps({
        "ok": result.get("ok"),
        "mode": result.get("mode"),
        "report_paths": result.get("report_paths"),
        "candidate_count": result.get("candidate_count"),
        "estimated_reclaimable_human": result.get("estimated_reclaimable_human"),
        "archived_count": result.get("archived_count"),
        "reclaimed_human": result.get("reclaimed_human"),
        "error_count": result.get("error_count"),
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

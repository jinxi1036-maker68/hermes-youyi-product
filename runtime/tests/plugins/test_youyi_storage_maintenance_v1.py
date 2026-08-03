from __future__ import annotations

import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _touch(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    path.touch()
    import os
    os.utime(path, (ts, ts))


def _seed_storage(tmp_path: Path, now: datetime) -> None:
    old = now - timedelta(days=8)
    recent = now - timedelta(days=2)
    old_day = old.strftime("%Y%m%d")
    recent_day = recent.strftime("%Y%m%d")
    old_report_json = tmp_path / "reports" / f"autonomous-wakeup-v1-{old_day}-010000.json"
    old_report_md = tmp_path / "reports" / f"autonomous-wakeup-v1-{old_day}-010000.md"
    recent_report = tmp_path / "reports" / f"autonomous-wakeup-v1-{recent_day}-010000.json"
    for path, text in [
        (old_report_json, json.dumps({"old": True}, ensure_ascii=False)),
        (old_report_md, "# old"),
        (recent_report, json.dumps({"recent": True}, ensure_ascii=False)),
    ]:
        _write(path, text)
    for path in [old_report_json, old_report_md]:
        _touch(path, old)
    _touch(recent_report, recent)
    backup_file = tmp_path / "backup" / "prewrite" / f"{old_day}T010000000000" / "students.json"
    _write(backup_file, "{}")
    _touch(backup_file, old)
    _touch(backup_file.parent, old)
    _write(tmp_path / "students.json", "{}")
    _write(tmp_path / "business_action_audit.jsonl", "{}\n")


def test_storage_audit_reports_counts_without_touching_business_files(tmp_path):
    from plugins.tuoguan_core.storage_maintenance import audit_storage

    now = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    _seed_storage(tmp_path, now)
    before = (tmp_path / "students.json").read_text(encoding="utf-8")

    result = audit_storage(tmp_path, now=now, write_report=True)

    assert result["ok"] is True
    assert result["business_data_modified"] is False
    assert result["root_owned_count"] >= 0
    assert any(row["path"] == "business_action_audit.jsonl" for row in result["jsonl_counts"])
    assert any(row["day"] == (now - timedelta(days=8)).strftime("%Y%m%d") for row in result["reports_by_day"])
    assert (tmp_path / "storage_retention_policy.json").exists()
    assert list((tmp_path / "reports").glob("storage-audit-*.json"))
    assert (tmp_path / "students.json").read_text(encoding="utf-8") == before


def test_storage_dry_run_finds_old_reports_and_prewrite_only(tmp_path):
    from plugins.tuoguan_core.storage_maintenance import plan_archival

    now = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    _seed_storage(tmp_path, now)

    result = plan_archival(tmp_path, now=now)

    assert result["ok"] is True
    assert result["mode"] == "dry-run"
    assert result["candidate_count"] == 2
    kinds = {item["kind"] for item in result["candidates"]}
    assert kinds == {"reports", "prewrite"}
    rendered = json.dumps(result, ensure_ascii=False)
    assert "students.json" in rendered  # backup copy can be archived
    assert "business_action_audit.jsonl" not in "".join(
        item["archive_path"] for item in result["candidates"]
    )
    assert list((tmp_path / "reports").glob("storage-dry-run-*.json"))


def test_storage_apply_archives_and_keeps_core_business_files(tmp_path):
    from plugins.tuoguan_core.storage_maintenance import apply_archival

    now = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    _seed_storage(tmp_path, now)
    old_day = (now - timedelta(days=8)).strftime("%Y%m%d")

    result = apply_archival(tmp_path, now=now)

    assert result["ok"] is True
    assert result["archived_count"] == 2
    reports_archive = tmp_path / "archives" / "reports" / f"{old_day}.tar.gz"
    prewrite_archive = tmp_path / "archives" / "prewrite" / f"{old_day}.tar.gz"
    assert reports_archive.exists()
    assert prewrite_archive.exists()
    with tarfile.open(reports_archive, "r:gz") as tar:
        names = {member.name for member in tar.getmembers() if member.isfile()}
    assert f"reports/autonomous-wakeup-v1-{old_day}-010000.json" in names
    assert not (tmp_path / "reports" / f"autonomous-wakeup-v1-{old_day}-010000.json").exists()
    assert (tmp_path / "students.json").exists()
    assert (tmp_path / "business_action_audit.jsonl").exists()
    assert (tmp_path / "storage_maintenance_runs.jsonl").exists()


def test_storage_apply_is_idempotent_when_archives_exist(tmp_path):
    from plugins.tuoguan_core.storage_maintenance import apply_archival

    now = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    _seed_storage(tmp_path, now)

    first = apply_archival(tmp_path, now=now)
    second = apply_archival(tmp_path, now=now)

    assert first["archived_count"] == 2
    assert second["archived_count"] == 0
    assert second["error_count"] == 0

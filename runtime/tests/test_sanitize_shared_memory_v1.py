from __future__ import annotations

from pathlib import Path


def test_shared_memory_sanitizer_archives_personal_memory_before_replacement(tmp_path: Path):
    from scripts.sanitize_shared_memory import NEUTRAL_MEMORY, sanitize

    memory = tmp_path / "memories" / "MEMORY.md"
    memory.parent.mkdir()
    original = "机构负责人是老板。\n§\n示例老师偏好五点后提醒。\n"
    memory.write_text(original, encoding="utf-8")
    archive = tmp_path / "archive"

    dry_run = sanitize(memory, archive, apply=False)
    assert dry_run["changed"] is True
    assert memory.read_text(encoding="utf-8") == original
    assert not archive.exists()

    applied = sanitize(memory, archive, apply=True)
    assert applied["ok"] is True
    assert applied["writeback_verified"] is True
    assert memory.read_text(encoding="utf-8") == NEUTRAL_MEMORY
    backups = list(archive.glob("MEMORY.before-neutralization.*.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original

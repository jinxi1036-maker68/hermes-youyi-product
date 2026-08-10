from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_social_market_preflight_failure_does_not_create_fake_trends(tmp_path):
    from plugins.tuoguan_core.social_market_research import run_social_market_research
    from plugins.tuoguan_core.store import TuoguanStore

    def runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=69,
            stdout=json.dumps({"ok": False, "error": {"code": "BROWSER_CONNECT", "message": "Browser Bridge extension not connected"}}, ensure_ascii=False),
            stderr="",
        )

    result = run_social_market_research(
        "xiaohongshu",
        store=TuoguanStore(tmp_path),
        query="项城 托管机构 招生",
        dry_run=True,
        runner=runner,
        now=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["run"]["status"] == "backend_unavailable"
    assert result["candidates"] == []
    assert not (tmp_path / "social_market_research_candidates.jsonl").exists()


def test_social_market_blocks_write_commands(tmp_path):
    from plugins.tuoguan_core.social_market_research import run_social_market_research
    from plugins.tuoguan_core.store import TuoguanStore

    result = run_social_market_research(
        "douyin",
        store=TuoguanStore(tmp_path),
        query="项城托管",
        command="publish",
        dry_run=True,
    )

    assert result["ok"] is False
    assert result["error"] == "social_market_write_command_blocked"
    assert result["run"]["auto_effects"]["publishes_social_content"] is False


def test_social_market_success_persists_source_candidates(tmp_path):
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.social_market_research import query_social_market_research, run_social_market_research
    from plugins.tuoguan_core.store import TuoguanStore

    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        if "whoami" in command:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True}, ensure_ascii=False), stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "items": [
                    {
                        "note_id": "note-1",
                        "title": "项城托管开学收心班",
                        "content": "主打晚托、作业辅导、家长接送省心。",
                        "url": "https://example.test/note-1",
                        "liked_count": 23,
                        "comment_count": 4,
                    }
                ]
            }, ensure_ascii=False),
            stderr="",
        )

    store = TuoguanStore(tmp_path)
    result = run_social_market_research(
        "xiaohongshu",
        store=store,
        query="项城 托管机构 招生",
        dry_run=False,
        runner=runner,
        now=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["candidate_count"] == 1
    assert any(command[1:3] == ["xiaohongshu", "search"] for command in calls)
    rows = [json.loads(line) for line in (tmp_path / "social_market_research_candidates.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["platform"] == "xiaohongshu"
    assert rows[0]["status"] == "pending_review"
    assert rows[0]["auto_effects"]["publishes_social_content"] is False

    identity = UserIdentity("wecom", "boss1", "boss1", "金总", "boss", "approved")
    queried = query_social_market_research(store, identity=identity, platform="xiaohongshu")
    assert queried["candidate_count"] == 1
    assert "不是优益已确认事实" in queried["rendered_text"]

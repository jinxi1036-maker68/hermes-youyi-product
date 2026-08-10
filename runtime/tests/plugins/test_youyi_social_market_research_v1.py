from __future__ import annotations

from datetime import datetime, timezone
import json
import os
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


def test_social_market_falls_back_to_browser_backend(tmp_path):
    from plugins.tuoguan_core.social_market_research import run_social_market_research
    from plugins.tuoguan_core.store import TuoguanStore

    def opencli_runner(command, **_kwargs):
        if "whoami" in command:
            return SimpleNamespace(
                returncode=69,
                stdout=json.dumps({"ok": False, "error": {"code": "BROWSER_CONNECT", "message": "Browser Bridge extension not connected"}}, ensure_ascii=False),
                stderr="",
            )
        raise AssertionError("search should not call OpenCLI after failed preflight")

    def browser_runner(command, **_kwargs):
        assert "social_market_browser_fallback.js" in command[1]
        assert "--platform" in command
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "ok": True,
                "items": [
                    {
                        "id": "browser-1",
                        "title": "项城晚托招生视频",
                        "content": "同行主打作业辅导、接送省心和开学收心。",
                        "url": "https://example.test/browser-1",
                        "author": "本地托管账号",
                    }
                ],
            }, ensure_ascii=False),
            stderr="",
        )

    result = run_social_market_research(
        "xiaohongshu",
        store=TuoguanStore(tmp_path),
        query="项城晚托",
        dry_run=True,
        runner=opencli_runner,
        browser_runner=browser_runner,
        now=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["run"]["status"] == "completed"
    assert result["run"]["backend"] == "browser"
    assert result["candidates"][0]["author"] == "本地托管账号"


def test_social_market_filters_platform_agreement_pages(tmp_path):
    from plugins.tuoguan_core.social_market_research import run_social_market_research
    from plugins.tuoguan_core.store import TuoguanStore

    def browser_runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "ok": True,
                "items": [
                    {
                        "id": "https://agree.xiaohongshu.com/h5/terms/ZXXY20220331001/-1",
                        "title": "《用户协议》",
                        "content": "我已阅读并同意《用户协议》《隐私政策》《儿童/青少年个人信息保护规则》",
                        "url": "https://agree.xiaohongshu.com/h5/terms/ZXXY20220331001/-1",
                    },
                    {
                        "id": "https://agree.xiaohongshu.com/h5/terms/ZXXY20220509001/-1",
                        "title": "《隐私政策》",
                        "content": "我已阅读并同意《用户协议》《隐私政策》《儿童/青少年个人信息保护规则》",
                        "url": "https://agree.xiaohongshu.com/h5/terms/ZXXY20220509001/-1",
                    },
                ],
            }, ensure_ascii=False),
            stderr="",
        )

    result = run_social_market_research(
        "xiaohongshu",
        store=TuoguanStore(tmp_path),
        query="项城托管招生",
        dry_run=True,
        backend_order=["browser"],
        browser_runner=browser_runner,
        now=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["run"]["status"] == "source_failed"
    assert result["candidates"] == []


def test_social_market_browser_auth_required_is_backend_unavailable(tmp_path):
    from plugins.tuoguan_core.social_market_research import run_social_market_research
    from plugins.tuoguan_core.store import TuoguanStore

    def browser_runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps({
                "ok": False,
                "error": "login_required_or_blocked",
                "error_code": "AUTH_REQUIRED",
                "message": "平台页面未进入可读搜索结果，可能需要登录。",
            }, ensure_ascii=False),
            stderr="",
        )

    result = run_social_market_research(
        "xiaohongshu",
        store=TuoguanStore(tmp_path),
        query="项城托管招生",
        dry_run=True,
        backend_order=["browser"],
        browser_runner=browser_runner,
        now=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["run"]["status"] == "backend_unavailable"
    assert result["candidates"] == []


def test_social_market_browser_profile_in_use_is_backend_unavailable(tmp_path):
    from plugins.tuoguan_core.social_market_research import run_social_market_research
    from plugins.tuoguan_core.store import TuoguanStore

    def browser_runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps({
                "ok": False,
                "error": "browser_profile_in_use",
                "error_code": "PROFILE_IN_USE",
                "message": "Opening in existing browser session. This usually means that the profile is already in use.",
            }, ensure_ascii=False),
            stderr="",
        )

    result = run_social_market_research(
        "douyin",
        store=TuoguanStore(tmp_path),
        query="项城托管招生",
        dry_run=True,
        backend_order=["browser"],
        browser_runner=browser_runner,
        now=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["run"]["status"] == "backend_unavailable"
    assert result["candidates"] == []


def test_social_market_resolves_server_opencli_path(tmp_path):
    from plugins.tuoguan_core import social_market_research

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    opencli = bin_dir / "opencli"
    opencli.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    previous_root = os.environ.get("HERMES_SOCIAL_RESEARCH_ROOT")
    previous_opencli = os.environ.get("HERMES_OPENCLI")
    try:
        os.environ.pop("HERMES_OPENCLI", None)
        os.environ["HERMES_SOCIAL_RESEARCH_ROOT"] = str(tmp_path)
        assert Path(social_market_research._resolve_opencli_executable()) == opencli
    finally:
        if previous_root is None:
            os.environ.pop("HERMES_SOCIAL_RESEARCH_ROOT", None)
        else:
            os.environ["HERMES_SOCIAL_RESEARCH_ROOT"] = previous_root
        if previous_opencli is None:
            os.environ.pop("HERMES_OPENCLI", None)
        else:
            os.environ["HERMES_OPENCLI"] = previous_opencli


def test_social_market_batch_uses_configured_queries(tmp_path):
    from plugins.tuoguan_core.social_market_research import run_social_market_batch
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(
        tmp_path,
        "social_market_research_config.json",
        {
            "platforms": ["xiaohongshu"],
            "queries": ["项城托管", "项城晚托"],
            "backend_order": ["browser"],
            "limit_per_query": 1,
        },
    )

    result = run_social_market_batch(
        store=TuoguanStore(tmp_path),
        dry_run=True,
        now=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["run_count"] == 2
    assert result["platforms"] == ["xiaohongshu"]

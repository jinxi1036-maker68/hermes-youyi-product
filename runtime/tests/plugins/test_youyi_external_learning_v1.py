from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_jsonl(path: Path, name: str) -> list[dict]:
    target = path / name
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def _seed_store(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "wecom_whitelist.json", {"super_users": ["boss1"], "user_roles": {"boss1": "boss"}})
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1"})
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "youyi_operating_model.json", {"city": "项城", "business_area": "向阳"})
    return TuoguanStore(tmp_path)


def _fake_research(_store, *, kind, query, now=None, limit=5, **_kwargs):
    return {
        "kind": kind,
        "query": query,
        "evidence_count": 1,
        "evidence": [{
            "title": f"{query} 公开资料",
            "url": "https://example.test/public-source",
            "description": "公开资料摘要。",
            "query": query,
            "provider": "test",
            "collected_at": (now or datetime.now(timezone.utc)).isoformat(),
        }],
        "errors": [],
        "updated_at": (now or datetime.now(timezone.utc)).isoformat(),
    }


def _failed_research(_store, *, kind, query, now=None, limit=5, **_kwargs):
    return {
        "kind": kind,
        "query": query,
        "evidence_count": 0,
        "evidence": [],
        "errors": ["No web search provider configured"],
        "updated_at": (now or datetime.now(timezone.utc)).isoformat(),
    }


def test_weekly_external_learning_keeps_verified_candidates_without_auto_sending(tmp_path, monkeypatch):
    from plugins.tuoguan_core import external_learning_runner
    from plugins.tuoguan_core.external_learning_runner import run_external_learning

    monkeypatch.setattr(external_learning_runner, "collect_public_research", _fake_research)
    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))

    result = run_external_learning("weekly_industry", store=store, now=datetime(2026, 8, 3, 8, 45, tzinfo=cn_tz))

    assert result["ok"] is True
    assert result["queued"] is False
    assert result["delivery_mode"] == "candidate_only"
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert outbox == []
    candidates = _read_jsonl(tmp_path, "industry_learning_candidates.jsonl")
    assert candidates
    assert candidates[0]["source_count"] == 1
    assert candidates[0]["auto_effects"]["updates_institution_facts"] is False
    runs = _read_jsonl(tmp_path, "external_research_runs.jsonl")
    assert runs and runs[0]["mode"] == "weekly_industry"


def test_source_failure_records_failed_candidate_without_trend_claim(tmp_path, monkeypatch):
    from plugins.tuoguan_core import external_learning_runner
    from plugins.tuoguan_core.external_learning_runner import run_external_learning

    monkeypatch.setattr(external_learning_runner, "collect_public_research", _failed_research)
    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))

    result = run_external_learning("weekly_industry", store=store, now=datetime(2026, 8, 3, 8, 45, tzinfo=cn_tz))

    assert result["ok"] is True
    candidates = _read_jsonl(tmp_path, "industry_learning_candidates.jsonl")
    assert candidates
    assert all(row["status"] == "source_failed" for row in candidates)
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert "不生成趋势结论" in result["report"]["content"]
    assert outbox == []


def test_public_research_rejects_private_and_reference_only_urls(tmp_path):
    from plugins.tuoguan_core.research import collect_public_research

    store = _seed_store(tmp_path)

    def hostile_search(_query, _limit):
        return {
            "success": True,
            "provider": "hostile-test",
            "data": {
                "web": [
                    {"title": "localhost", "url": "http://127.0.0.1/admin", "description": "private"},
                    {"title": "metadata", "url": "http://169.254.169.254/latest/meta-data", "description": "private"},
                    {
                        "title": "generic reference",
                        "url": "https://www.xiangcheng.gov.cn/",
                        "description": "reference",
                        "evidence_level": "reference_only",
                    },
                    {"title": "real public source", "url": "https://example.test/public", "description": "query result"},
                ]
            },
        }

    result = collect_public_research(
        store,
        kind="industry_trend",
        query="托管机构管理",
        search=hostile_search,
        persist=False,
    )

    assert result["evidence_count"] == 1
    assert result["evidence"][0]["url"] == "https://example.test/public"


def test_public_search_prefers_configured_firecrawl_without_exposing_key(monkeypatch):
    from plugins.tuoguan_core import research

    monkeypatch.setenv("FIRECRAWL_API_KEY", "secret-test-key")
    captured = {}

    def fake_request(url, *, payload, headers, timeout):
        captured.update({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return {
            "success": True,
            "data": {"web": [{"title": "托管机构续费方法", "url": "https://example.test/edu", "description": "家校沟通与老师服务记录"}]},
        }

    monkeypatch.setattr(research, "_request_json", fake_request)
    result = research.search_public_web("托管机构 续费", limit=5)

    assert result["success"] is True
    assert result["provider"] == "firecrawl"
    assert result["data"]["web"][0]["provider"] == "firecrawl"
    assert captured["url"].endswith("/v2/search")
    assert captured["headers"]["Authorization"] == "Bearer secret-test-key"


def test_monthly_market_research_uses_market_ledgers_without_auto_sending(tmp_path, monkeypatch):
    from plugins.tuoguan_core import external_learning_runner
    from plugins.tuoguan_core.external_learning_runner import run_external_learning

    monkeypatch.setattr(external_learning_runner, "collect_public_research", _fake_research)
    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))

    result = run_external_learning("monthly_market", store=store, now=datetime(2026, 9, 1, 9, 30, tzinfo=cn_tz))

    assert result["ok"] is True
    market = _read_jsonl(tmp_path, "market_research_candidates.jsonl")
    competitors = _read_jsonl(tmp_path, "competitor_profiles.jsonl")
    assert market and market[0]["status"] == "pending_review"
    assert competitors and competitors[0]["confidence"] == "low"
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert outbox == []


def test_irrelevant_public_sources_are_quarantined_without_trend_or_outbox(tmp_path, monkeypatch):
    from plugins.tuoguan_core import external_learning_runner
    from plugins.tuoguan_core.external_learning_runner import run_external_learning

    def irrelevant(_store, *, kind, query, now=None, limit=5, **_kwargs):
        return {
            "kind": kind,
            "query": query,
            "evidence_count": 1,
            "evidence": [{
                "title": "中通快递",
                "url": "https://www.zto.com/",
                "description": "快递物流服务。",
                "query": query,
                "provider": "test",
                "collected_at": (now or datetime.now(timezone.utc)).isoformat(),
            }],
            "errors": [],
        }

    monkeypatch.setattr(external_learning_runner, "collect_public_research", irrelevant)
    store = _seed_store(tmp_path)
    result = run_external_learning("weekly_industry", store=store)

    assert result["run"]["status"] == "completed_no_relevant_sources"
    assert result["run"]["evidence_count"] == 0
    assert result["run"]["rejected_evidence_count"] == len(result["run"]["queries"])
    assert all(row["status"] == "source_failed" for row in _read_jsonl(tmp_path, "industry_learning_candidates.jsonl"))
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []
    latest = json.loads((tmp_path / "knowledge_research_latest.json").read_text(encoding="utf-8"))
    assert latest["evidence_count"] == 0
    assert latest["raw_evidence_count"] == len(result["run"]["queries"])
    assert latest["content_validity"] == "no_relevant_sources"
    assert not (tmp_path / "pending_knowledge.json").exists()


def test_external_learning_brief_is_read_only_material(tmp_path, monkeypatch):
    from plugins.tuoguan_core import external_learning_runner
    from plugins.tuoguan_core.digital_employee_state import query_external_learning_brief
    from plugins.tuoguan_core.external_learning_runner import run_external_learning
    from plugins.tuoguan_core.models import UserIdentity

    monkeypatch.setattr(external_learning_runner, "collect_public_research", _fake_research)
    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    run_external_learning("monthly_market", store=store, now=datetime(2026, 9, 1, 9, 30, tzinfo=cn_tz))
    identity = UserIdentity("wecom", "boss1", "boss1", "金总", "boss", "approved")

    brief = query_external_learning_brief(store, identity=identity)

    assert brief["ok"] is True
    assert brief["market_research_candidates"]["candidate_count"] >= 1
    assert "不替模型" in brief["rendered_text"] or "是否采纳" in brief["rendered_text"]


def test_public_learning_health_exposes_relevance_evidence_without_actions(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import _xiaoyou_public_learning_health

    store = _seed_store(tmp_path)
    now = datetime.now(timezone.utc)
    (tmp_path / "external_research_runs.jsonl").write_text(
        json.dumps({
            "tenant_id": "youyi_tuoguan",
            "mode": "weekly_industry",
            "status": "completed_no_relevant_sources",
            "evidence_count": 0,
            "raw_evidence_count": 4,
            "rejected_evidence_count": 4,
            "created_at": now.isoformat(),
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    health = _xiaoyou_public_learning_health(store, now.timestamp() - 3600)

    assert health["latest_status"] == "completed_no_relevant_sources"
    assert health["rejected_irrelevant_count_last_24h"] == 4
    assert health["accepted_evidence_count_last_24h"] == 0
    assert health["no_trend_without_sources"] is True
    assert health["updates_institution_facts"] is False
    assert health["sends_messages"] is False

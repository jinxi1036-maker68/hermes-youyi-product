"""Scheduled public learning and market research reports for Hermes.

This runner produces boss-facing evidence candidates from public sources. It
does not contact teachers or parents, change institution facts, or decide what
Hermes must do next.
"""

from __future__ import annotations

from datetime import datetime
import argparse
import json
import uuid
from typing import Any

from .digital_employee_state import submit_industry_learning_candidate
from .employee_identity import owner_user_id as _owner_user_id
from .employee_identity import system_identity as _system_identity
from .research import collect_public_research
from .store import TuoguanStore
from .tenant_context import current_tenant_id, read_institution_operating_model
from .write_guard import authorized_system_write


EXTERNAL_RESEARCH_RUNS_FILE = "external_research_runs.jsonl"
MARKET_RESEARCH_CANDIDATES_FILE = "market_research_candidates.jsonl"
COMPETITOR_PROFILES_FILE = "competitor_profiles.jsonl"
WEEKLY_MARKET_REPORT_RUNS_FILE = "weekly_market_report_runs.jsonl"
INDUSTRY_LEARNING_CANDIDATES_FILE = "industry_learning_candidates.jsonl"
NOTIFICATION_OUTBOX_FILE = "notification_outbox.json"

VALID_MODES = {"weekly_industry", "monthly_market", "manual_topic"}

DEFAULT_INDUSTRY_QUERIES = [
    "托管机构 续费 家校沟通 老师记录 2026",
    "教培机构 招生 新生留存 家长转化 2026",
    "课后服务 托管 老师管理 提升人效",
    "托管班 增项课程 数学 阅读 习惯培养",
]

DEFAULT_MARKET_QUERIES = [
    "项城 向阳 托管机构 招生 价格 服务",
    "项城 课后托管 晚托 午托 招生",
    "项城 小学生托管 教培 课程 公开信息",
]


def run_external_learning(
    mode: str,
    *,
    store: TuoguanStore | None = None,
    now: datetime | None = None,
    topic: str = "",
    query: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    normalized_mode = str(mode or "weekly_industry").strip()
    if normalized_mode not in VALID_MODES:
        normalized_mode = "weekly_industry"
    queries = _queries_for_mode(normalized_mode, topic=topic, query=query, store=actual_store)
    run_id = f"external_research:{normalized_mode}:{timestamp.strftime('%Y%m%d%H%M%S')}"
    tenant_id = current_tenant_id()
    owner_id = _owner_user_id(actual_store)
    identity = _system_identity()

    research_results: list[dict[str, Any]] = []
    industry_candidates: list[dict[str, Any]] = []
    market_candidates: list[dict[str, Any]] = []
    competitor_profiles: list[dict[str, Any]] = []
    errors: list[str] = []

    for idx, search_query in enumerate(queries):
        kind = "local_market" if normalized_mode == "monthly_market" else "industry_trend"
        result = collect_public_research(
            actual_store,
            kind=kind,
            query=search_query,
            now=timestamp,
            limit=5,
            persist=not dry_run,
        )
        research_results.append(result)
        errors.extend(str(item) for item in result.get("errors", []) if item)
        evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
        if normalized_mode == "monthly_market":
            market_candidates.append(_market_candidate(search_query, evidence, timestamp, result))
            competitor_profiles.extend(_competitor_profiles(search_query, evidence, timestamp))
        else:
            industry_candidates.append(_industry_candidate(search_query, evidence, timestamp, result))

    report = _build_report(
        normalized_mode,
        timestamp=timestamp,
        research_results=research_results,
        industry_candidates=industry_candidates,
        market_candidates=market_candidates,
        errors=errors,
        topic=topic,
    )
    outbox_item = _report_outbox_item(
        mode=normalized_mode,
        timestamp=timestamp,
        owner_id=owner_id,
        content=str(report.get("content") or ""),
        summary=str(report.get("summary") or ""),
    ) if owner_id else {}

    run_row = {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "mode": normalized_mode,
        "queries": queries,
        "evidence_count": sum(int(item.get("evidence_count") or 0) for item in research_results),
        "candidate_count": len(industry_candidates) + len(market_candidates),
        "report_notification_id": str(outbox_item.get("id") or ""),
        "status": "dry_run" if dry_run else "completed",
        "errors": errors[:20],
        "created_at": timestamp.isoformat(timespec="seconds"),
        "auto_effects": _safe_auto_effects(),
    }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "run": run_row,
            "report": report,
            "outbox_item": outbox_item,
            "research_results": research_results,
            "market_candidates": market_candidates,
            "industry_candidates": industry_candidates,
        }

    allowed_files = {
        EXTERNAL_RESEARCH_RUNS_FILE,
        MARKET_RESEARCH_CANDIDATES_FILE,
        COMPETITOR_PROFILES_FILE,
        WEEKLY_MARKET_REPORT_RUNS_FILE,
        INDUSTRY_LEARNING_CANDIDATES_FILE,
        NOTIFICATION_OUTBOX_FILE,
    }
    with authorized_system_write(actual_store.data_dir, job_name="external_learning_runner", allowed_files=allowed_files) as auth:
        _append_jsonl(actual_store, EXTERNAL_RESEARCH_RUNS_FILE, {**run_row, "operation_id": auth.operation_id, "ledger_id": auth.ledger_id, "audit_id": auth.audit_id})
        for row in market_candidates:
            _append_jsonl(actual_store, MARKET_RESEARCH_CANDIDATES_FILE, {**row, "operation_id": auth.operation_id, "ledger_id": auth.ledger_id, "audit_id": auth.audit_id})
        for row in competitor_profiles:
            _append_jsonl(actual_store, COMPETITOR_PROFILES_FILE, {**row, "operation_id": auth.operation_id, "ledger_id": auth.ledger_id, "audit_id": auth.audit_id})
        for row in industry_candidates:
            submit_industry_learning_candidate(
                actual_store,
                identity=identity,
                topic=str(row.get("topic") or ""),
                summary=str(row.get("summary") or ""),
                sources=row.get("sources") if isinstance(row.get("sources"), list) else [],
                applicability=str(row.get("applicability") or ""),
                status=str(row.get("status") or "pending_review"),
                source_text="Hermes public external learning runner",
                source_message_id=run_id,
                operation_id=auth.operation_id,
            )
        if outbox_item:
            def merge_outbox(value: Any) -> list[dict[str, Any]]:
                outbox = value if isinstance(value, list) else []
                existing = next(
                    (
                        item for item in outbox
                        if isinstance(item, dict) and str(item.get("id") or "") == outbox_item["id"]
                    ),
                    None,
                )
                if existing and str(existing.get("status") or "") in {"pending", "retry_pending", "sent"}:
                    return outbox[-2000:]
                if existing:
                    existing.update(outbox_item)
                else:
                    outbox.append(outbox_item)
                return outbox[-2000:]

            actual_store.update_json(NOTIFICATION_OUTBOX_FILE, [], merge_outbox)
        _append_jsonl(
            actual_store,
            WEEKLY_MARKET_REPORT_RUNS_FILE,
            {
                "run_id": f"market_report:{normalized_mode}:{timestamp.strftime('%Y%m%d')}",
                "tenant_id": tenant_id,
                "mode": normalized_mode,
                "notification_id": str(outbox_item.get("id") or ""),
                "queued": bool(outbox_item),
                "target_user_id": owner_id,
                "created_at": timestamp.isoformat(timespec="seconds"),
                "operation_id": auth.operation_id,
                "ledger_id": auth.ledger_id,
                "audit_id": auth.audit_id,
                "source_counts": {
                    "query_count": len(queries),
                    "evidence_count": run_row["evidence_count"],
                    "candidate_count": run_row["candidate_count"],
                },
                "auto_effects": _safe_auto_effects(),
            },
        )

    return {
        "ok": True,
        "dry_run": False,
        "run": run_row,
        "report": report,
        "notification_id": str(outbox_item.get("id") or ""),
        "queued": bool(outbox_item),
        "writeback_verified": True,
    }


def _queries_for_mode(mode: str, *, topic: str, query: str, store: TuoguanStore) -> list[str]:
    if str(query or "").strip():
        return [str(query or "").strip()]
    if mode == "manual_topic" and str(topic or "").strip():
        return [f"托管机构 教培 {topic}".strip()]
    if mode == "monthly_market":
        location_hint = _location_hint(store)
        if location_hint:
            return [f"{location_hint} 托管机构 招生 价格 服务", f"{location_hint} 小学生托管 晚托 午托 公开信息"]
        return DEFAULT_MARKET_QUERIES
    return DEFAULT_INDUSTRY_QUERIES


def _location_hint(store: TuoguanStore) -> str:
    model = read_institution_operating_model(store)
    if not isinstance(model, dict):
        return ""
    parts: list[str] = []
    for key in ("city", "district", "business_area", "address"):
        value = str(model.get(key) or "").strip()
        if value:
            parts.append(value)
    institution = model.get("institution") if isinstance(model.get("institution"), dict) else {}
    for key in ("city", "district", "business_area", "address"):
        value = str(institution.get(key) or "").strip()
        if value:
            parts.append(value)
    text = " ".join(dict.fromkeys(parts))
    return text[:80]


def _industry_candidate(query: str, evidence: list[Any], timestamp: datetime, result: dict[str, Any]) -> dict[str, Any]:
    sources = [item for item in evidence if isinstance(item, dict)]
    status = "pending_review" if sources else "source_failed"
    source_titles = "；".join(str(item.get("title") or "") for item in sources[:3])
    summary = (
        f"小优围绕“{query}”收集到 {len(sources)} 条公开资料线索。"
        + (f" 代表来源：{source_titles}。" if source_titles else " 当前没有稳定公开来源，不能生成趋势结论。")
    )
    return {
        "candidate_id": f"industry_external_{uuid.uuid4().hex[:12]}",
        "topic": query,
        "summary": summary,
        "sources": sources[:8],
        "source_count": len(sources),
        "applicability": "仅作为托管经营建议材料；老板审核前不进入正式手册或优益机构事实。",
        "status": status,
        "created_at": timestamp.isoformat(timespec="seconds"),
        "errors": result.get("errors", []),
        "auto_effects": _safe_auto_effects(),
    }


def _market_candidate(query: str, evidence: list[Any], timestamp: datetime, result: dict[str, Any]) -> dict[str, Any]:
    sources = [item for item in evidence if isinstance(item, dict)]
    return {
        "candidate_id": f"market_candidate_{uuid.uuid4().hex[:12]}",
        "tenant_id": current_tenant_id(),
        "market_scope": "xiangcheng_public_web",
        "query": query,
        "finding": (
            f"围绕“{query}”收集到 {len(sources)} 条公开市场线索。"
            if sources else "本轮没有拿到稳定公开市场线索；不生成竞品价格或趋势判断。"
        ),
        "sources": sources[:8],
        "source_count": len(sources),
        "status": "pending_review" if sources else "source_failed",
        "applicability": "用于老板做本地市场判断；公开资料需人工核验，不能直接当成优益或竞品事实。",
        "created_at": timestamp.isoformat(timespec="seconds"),
        "errors": result.get("errors", []),
        "auto_effects": _safe_auto_effects(),
    }


def _competitor_profiles(query: str, evidence: list[Any], timestamp: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in evidence[:6]:
        if not isinstance(item, dict):
            continue
        rows.append({
            "profile_id": f"competitor_profile_{uuid.uuid4().hex[:12]}",
            "tenant_id": current_tenant_id(),
            "query": query,
            "name_or_title": str(item.get("title") or "")[:180],
            "public_url": str(item.get("url") or ""),
            "public_summary": str(item.get("description") or "")[:500],
            "status": "public_candidate",
            "confidence": "low",
            "created_at": timestamp.isoformat(timespec="seconds"),
            "auto_effects": _safe_auto_effects(),
        })
    return rows


def _build_report(
    mode: str,
    *,
    timestamp: datetime,
    research_results: list[dict[str, Any]],
    industry_candidates: list[dict[str, Any]],
    market_candidates: list[dict[str, Any]],
    errors: list[str],
    topic: str,
) -> dict[str, Any]:
    evidence = [source for result in research_results for source in (result.get("evidence") or []) if isinstance(source, dict)]
    titles = [str(item.get("title") or "") for item in evidence[:5] if str(item.get("title") or "").strip()]
    urls = [str(item.get("url") or "") for item in evidence[:5] if str(item.get("url") or "").startswith(("http://", "https://"))]
    if mode == "monthly_market":
        summary = "小优本地市场调研月报"
        headline = "金总，我做了一轮本地公开市场调研，先把可核验线索和判断给你。"
        advice = "我建议先把这些公开线索当成方向：看对方主打午托/晚托/作业辅导/素养增项哪一类，再决定优益九月份重点突出什么。"
    elif mode == "manual_topic":
        summary = "小优外部专题学习报告"
        headline = f"金总，我按专题“{topic or '外部学习'}”查了一轮公开资料。"
        advice = "这批资料适合做经营参考，是否进入正式经验库，需要你看过后确认。"
    else:
        summary = "小优托管行业学习周报"
        headline = "金总，我做了一轮托管/教培行业公开学习，给你汇报一下本周可参考的东西。"
        advice = "我建议优先把外部方法转成续费证据、家校沟通话术、老师减负素材，而不是直接照搬。"
    lines = [
        headline,
        f"时间：{timestamp.strftime('%Y-%m-%d %H:%M')}",
        "",
        "我看到的公开资料：",
    ]
    if titles:
        for idx, title in enumerate(titles, 1):
            url = urls[idx - 1] if idx - 1 < len(urls) else ""
            lines.append(f"{idx}. {title}" + (f"（{url}）" if url else ""))
    else:
        lines.append("1. 本轮没有拿到稳定公开资料，所以我不生成趋势结论。")
    lines.extend([
        "",
        "我的判断：",
        advice,
        "",
        "需要你审核/确认：",
        "这些都是外部公开资料和我的经营建议，不是优益已确认事实；如果你认可，我后续再把它们转成具体话术、记录标准或九月份续费准备材料。",
    ])
    if errors:
        lines.extend(["", f"本轮不确定项：{'; '.join(dict.fromkeys(errors[:3]))}"])
    return {
        "ok": True,
        "mode": mode,
        "summary": summary,
        "content": _limit_message("\n".join(lines), 1900),
        "source_count": len(evidence),
        "candidate_count": len(industry_candidates) + len(market_candidates),
        "auto_effects": _safe_auto_effects(),
    }


def _report_outbox_item(*, mode: str, timestamp: datetime, owner_id: str, content: str, summary: str) -> dict[str, Any]:
    day = timestamp.strftime("%Y%m%d")
    return {
        "id": f"external_learning_report:{day}:{mode}",
        "status": "pending",
        "delivery_mode": "direct_wecom",
        "notification_type": "external_learning_report",
        "task_id": f"external_learning_report:{mode}:{day}",
        "role": "boss",
        "action": f"external_learning_{mode}",
        "target_user_id": owner_id,
        "recipient_user_id": owner_id,
        "to_user_id": owner_id,
        "touser": owner_id,
        "content": content,
        "summary": summary,
        "created_at": timestamp.isoformat(timespec="seconds"),
        "attempt_count": 0,
        "auto_effects": _safe_auto_effects(),
    }


def _append_jsonl(store: TuoguanStore, filename: str, row: dict[str, Any]) -> None:
    store.append_jsonl_verified(filename, row)


def _safe_auto_effects() -> dict[str, bool]:
    return {
        "updates_handbook": False,
        "updates_institution_facts": False,
        "updates_long_term_memory": False,
        "sends_parent_messages": False,
        "sends_teacher_messages": False,
        "creates_teacher_tasks": False,
        "changes_salary": False,
        "changes_permissions": False,
        "changes_responsibility_binding": False,
        "deletes_data": False,
        "changes_router": False,
        "forces_next_action": False,
    }


def _limit_message(text: str, limit: int) -> str:
    normalized = "\n".join(" ".join(line.split()) for line in str(text or "").splitlines()).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Queue Hermes public learning and market research report.")
    parser.add_argument("mode", choices=sorted(VALID_MODES))
    parser.add_argument("--topic", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = run_external_learning(args.mode, topic=args.topic, query=args.query, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

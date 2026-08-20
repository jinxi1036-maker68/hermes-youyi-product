"""Public-source research helpers for Hermes industry learning.

The helpers collect public evidence only. They do not turn external material
into institution facts, contact people, or decide any business action.
"""

from __future__ import annotations

from datetime import datetime
import html
import ipaddress
import json
import os
import re
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Callable

from .digital_employee_state import INDUSTRY_LEARNING_CANDIDATES_FILE, submit_industry_learning_candidate
from .models import UserIdentity
from .store import TuoguanStore
from .write_guard import authorized_system_write


USER_AGENT = "HermesYouyiExternalLearning/1.0 (+public-source-research)"

XIANGCHENG_PUBLIC_FALLBACK = [
    {
        "title": "项城市人民政府公开信息",
        "url": "https://www.xiangcheng.gov.cn/",
        "description": "项城市本地公开政务信息，可作为商圈、学校、民生信息的人工核验入口。",
    },
    {
        "title": "河南省教育厅公开信息",
        "url": "https://jyt.henan.gov.cn/",
        "description": "河南教育政策和公开通知，可用于判断教培/托管外部政策背景。",
    },
    {
        "title": "河南省社会组织公共服务平台",
        "url": "https://www.hngh.org/",
        "description": "河南本地公开组织信息入口，可作为竞品和机构公开资料核验补充。",
    },
]


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now().astimezone()).isoformat(timespec="seconds")


def _limit_text(value: Any, limit: int = 800) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _request_text(url: str, *, timeout: int = 10) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(512_000).decode("utf-8", errors="replace")


def _request_json(
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int = 20,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read(1_000_000).decode("utf-8", errors="replace"))
    return value if isinstance(value, dict) else {"success": False, "error": "search provider returned non-object json"}


def _normalize_result(item: dict[str, Any], *, query: str, provider: str, now: datetime | None = None) -> dict[str, Any]:
    url = str(item.get("url") or item.get("href") or item.get("link") or "").strip()
    title = html.unescape(str(item.get("title") or "").strip())
    description = html.unescape(str(item.get("description") or item.get("body") or item.get("snippet") or "").strip())
    return {
        "title": _limit_text(title, 180),
        "url": url,
        "description": _limit_text(description, 500),
        "query": str(query or "").strip(),
        "provider": str(item.get("provider") or provider),
        "evidence_level": str(item.get("evidence_level") or "public_search_candidate"),
        "collected_at": _now_iso(now),
    }


def _firecrawl_search(query: str, limit: int = 5) -> dict[str, Any]:
    api_key = str(os.getenv("FIRECRAWL_API_KEY") or "").strip()
    if not api_key:
        return {"success": False, "error": "firecrawl unavailable: FIRECRAWL_API_KEY missing"}
    api_url = str(os.getenv("FIRECRAWL_API_URL") or "https://api.firecrawl.dev").strip().rstrip("/")
    try:
        payload = _request_json(
            f"{api_url}/v2/search",
            payload={
                "query": str(query or "").strip(),
                "limit": max(1, min(int(limit or 5), 10)),
                "sources": ["web"],
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=20,
        )
    except Exception as exc:
        return {"success": False, "error": f"firecrawl search failed: {type(exc).__name__}"}
    if payload.get("success") is False:
        return {"success": False, "error": "firecrawl search returned failure"}
    data = payload.get("data")
    if isinstance(data, dict):
        rows = data.get("web") or data.get("results") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = payload.get("web") or payload.get("results") or []
    if not isinstance(rows, list) or not rows:
        return {"success": False, "error": "firecrawl search returned no web results"}
    normalized = [{**row, "provider": "firecrawl"} for row in rows if isinstance(row, dict)]
    return {"success": bool(normalized), "provider": "firecrawl", "data": {"web": normalized}}


def _valid_public_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _ddgs_search(query: str, limit: int = 5) -> dict[str, Any]:
    try:
        from ddgs import DDGS  # type: ignore
    except Exception as exc:
        return {"success": False, "error": f"ddgs unavailable: {exc}"}
    try:
        with DDGS() as client:
            rows = list(client.text(query, max_results=max(1, min(int(limit or 5), 10))))
    except Exception as exc:
        return {"success": False, "error": f"ddgs search failed: {exc}"}
    return {"success": True, "data": {"web": rows}}


def _bing_rss_search(query: str, limit: int = 5) -> dict[str, Any]:
    encoded = urllib.parse.urlencode({"format": "rss", "q": query})
    url = f"https://www.bing.com/search?{encoded}"
    try:
        payload = _request_text(url, timeout=10)
        root = ET.fromstring(payload)
    except Exception as exc:
        return {"success": False, "error": f"bing rss search failed: {exc}"}
    rows: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        description = item.findtext("description") or ""
        if _valid_public_url(link):
            rows.append({"title": title, "url": link, "description": description})
        if len(rows) >= max(1, min(int(limit or 5), 10)):
            break
    if not rows:
        return {"success": False, "error": "bing rss returned no public results"}
    return {"success": True, "data": {"web": rows}}


def _duckduckgo_html_search(query: str, limit: int = 5) -> dict[str, Any]:
    encoded = urllib.parse.urlencode({"q": query})
    url = f"https://duckduckgo.com/html/?{encoded}"
    try:
        payload = _request_text(url, timeout=10)
    except Exception as exc:
        return {"success": False, "error": f"duckduckgo html search failed: {exc}"}
    rows: list[dict[str, Any]] = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?<a[^>]+class="result__snippet"[^>]*>(?P<body>.*?)</a>',
        re.S,
    )
    for match in pattern.finditer(payload):
        href = html.unescape(match.group("href"))
        parsed = urllib.parse.urlparse(href)
        query_args = urllib.parse.parse_qs(parsed.query)
        if "uddg" in query_args:
            href = query_args["uddg"][0]
        title = re.sub("<.*?>", "", match.group("title"))
        body = re.sub("<.*?>", "", match.group("body"))
        if _valid_public_url(href):
            rows.append({"title": title, "url": href, "description": body})
        if len(rows) >= max(1, min(int(limit or 5), 10)):
            break
    if not rows:
        return {"success": False, "error": "duckduckgo html returned no public results"}
    return {"success": True, "data": {"web": rows}}


def _coerce_search_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return {"success": False, "error": "search provider returned non-json text"}
        return payload if isinstance(payload, dict) else {"success": False, "error": "search provider returned non-object json"}
    return {"success": False, "error": "search provider returned unsupported payload"}


def search_public_web(query: str, limit: int = 5) -> dict[str, Any]:
    errors: list[str] = []
    for provider, search in (
        ("firecrawl", _firecrawl_search),
        ("ddgs", _ddgs_search),
        ("bing_rss", _bing_rss_search),
        ("duckduckgo_html", _duckduckgo_html_search),
    ):
        result = search(query, limit)
        if result.get("success"):
            rows = result.get("data", {}).get("web", [])
            if isinstance(rows, list) and rows:
                return {"success": True, "provider": provider, "data": {"web": rows}, "errors": errors}
        errors.append(str(result.get("error") or f"{provider} returned no results"))
    return {"success": False, "error": "; ".join(errors) or "No web search provider configured", "errors": errors}


def collect_public_research(
    store: TuoguanStore,
    *,
    kind: str = "knowledge",
    query: str = "",
    search: Callable[[str, int], dict[str, Any]] | None = None,
    now: datetime | None = None,
    limit: int = 5,
    persist: bool = True,
) -> dict[str, Any]:
    """Collect public research evidence and store latest compatibility files."""

    normalized_kind = str(kind or "knowledge").strip()
    if normalized_kind not in {"knowledge", "competitor", "industry_trend", "local_market", "platform_signal"}:
        normalized_kind = "knowledge"
    default_query = (
        "项城 托管机构 教培 招生 续费 价格"
        if normalized_kind in {"competitor", "local_market"}
        else "托管机构 教培行业 续费 招生 家校沟通 老师管理"
    )
    search_query = str(query or default_query).strip()
    runner = search or search_public_web
    errors: list[str] = []
    result = _coerce_search_payload(runner(search_query, limit))
    if not result.get("success"):
        errors.append(str(result.get("error") or "No web search provider configured"))
        fallback = _ddgs_search(search_query, limit)
        if fallback.get("success"):
            result = fallback
        elif normalized_kind in {"competitor", "local_market"}:
            result = {"success": False, "data": {"web": []}}
            errors.append("curated Xiangcheng reference links are not query evidence")
        else:
            result = fallback
            if fallback.get("error"):
                errors.append(str(fallback.get("error")))

    evidence: list[dict[str, Any]] = []
    if result.get("success"):
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        rows = data.get("web") if isinstance(data, dict) else []
        if not isinstance(rows, list):
            rows = result.get("results") if isinstance(result.get("results"), list) else []
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            item = _normalize_result(raw, query=search_query, provider=str(result.get("provider") or "public_search"), now=now)
            if (
                item["title"]
                and _valid_public_url(item["url"])
                and item["evidence_level"] != "reference_only"
            ):
                evidence.append(item)
            if len(evidence) >= max(1, min(int(limit or 5), 12)):
                break

    latest_name = "competitor_research_latest.json" if normalized_kind in {"competitor", "local_market"} else "knowledge_research_latest.json"
    payload = {
        "kind": normalized_kind,
        "query": search_query,
        "evidence_count": len(evidence),
        "evidence": evidence,
        "errors": errors,
        "updated_at": _now_iso(now),
        "auto_effects": {
            "updates_institution_facts": False,
            "updates_handbook": False,
            "sends_messages": False,
            "forces_next_action": False,
            "changes_router": False,
        },
    }
    if persist:
        store.write_json(latest_name, payload)

    pending_id = ""
    if persist and normalized_kind in {"knowledge", "industry_trend", "platform_signal"} and evidence:
        pending_id = f"pending_{uuid.uuid4().hex[:12]}"
        pending_row = {
            "id": pending_id,
            "kind": normalized_kind,
            "query": search_query,
            "evidence": evidence,
            "status": "pending_review",
            "created_at": _now_iso(now),
            "auto_effects": payload["auto_effects"],
        }

        def append_pending(value: Any) -> list[dict[str, Any]]:
            pending = value if isinstance(value, list) else []
            if any(isinstance(row, dict) and str(row.get("id") or "") == pending_id for row in pending):
                return pending[-500:]
            pending.append(pending_row)
            return pending[-500:]

        store.update_json("pending_knowledge.json", [], append_pending)
    payload["pending_id"] = pending_id
    return payload


def collect_industry_learning_candidates(
    store: TuoguanStore,
    *,
    search: Callable[[str, int], dict[str, Any] | str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compatibility entrypoint for the existing production industry timer."""

    timestamp = now or datetime.now().astimezone()
    allowed_files = {INDUSTRY_LEARNING_CANDIDATES_FILE, "knowledge_research_latest.json", "pending_knowledge.json"}
    with authorized_system_write(store.data_dir, job_name="industry_learning_v1", allowed_files=allowed_files):
        result = collect_public_research(
            store,
            kind="knowledge",
            query="托管机构 教培行业 续费 招生 家校沟通 老师管理",
            search=search,
            now=timestamp,
        )
        evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
        if evidence:
            summary = f"本次公网学习找到 {len(evidence)} 条可复核资料，主题覆盖托管续费、招生、家校沟通、老师管理或增项机会。"
            status = "pending_review"
            applicability = "只作为经营建议材料；老板审核前不得进入正式手册，也不得当成优益机构事实。"
        else:
            errors = result.get("errors") if isinstance(result.get("errors"), list) else []
            summary = "本次公网行业学习没有拿到可靠公开证据；Hermes 不能编造趋势或话术。"
            if errors:
                summary += " 主要原因：" + "；".join(str(item) for item in errors[:3])
            status = "source_failed"
            applicability = "需要配置可用公网搜索源后再生成行业学习候选。"
        candidate = submit_industry_learning_candidate(
            store,
            identity=UserIdentity(
                platform="system",
                platform_user_id="industry_learning_runner",
                canonical_user_id="industry_learning_runner",
                person_name="Hermes industry learning",
                role="boss",
                approval_state="approved",
            ),
            topic=f"{timestamp:%Y-%m-%d} 托管行业经营公开资料学习",
            summary=summary,
            sources=evidence[:12],
            applicability=applicability,
            status=status,
            operation_id=f"system:industry_learning:{timestamp.strftime('%Y%m%d%H%M%S')}",
            source_text="weekly public web research",
            source_message_id=f"industry_learning:{timestamp.strftime('%Y%m%d%H%M%S')}",
        )
    result["industry_learning_candidate"] = candidate
    return result


def format_research_report(result: dict[str, Any]) -> str:
    title = "项城托管竞品公开信息周报" if result.get("kind") in {"competitor", "local_market"} else "托管业务知识待审核"
    lines = [f"【{title}】", f"可复核证据：{result.get('evidence_count', 0)}条"]
    evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
    if not evidence:
        lines.append("本次没有找到足够可靠的公开证据，未生成竞品结论或知识建议。")
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        if errors:
            lines.append("原因：" + "；".join(str(item) for item in errors[:3]))
        return "\n".join(lines)
    for index, item in enumerate(evidence[:8], 1):
        if not isinstance(item, dict):
            continue
        lines.append(f"{index}. {item.get('title') or '未命名页面'}")
        lines.append(f"   {item.get('url') or ''}")
        summary = item.get("summary") or item.get("description")
        if summary:
            lines.append(f"   摘要：{summary}")
    if result.get("pending_id"):
        lines.append(f"待审核编号：{result['pending_id']}")
        lines.append("审核通过前不会进入老师话术知识库。")
    else:
        lines.append("仅供老板判断，不自动生成对外营销内容。")
    return "\n".join(lines)


__all__ = [
    "collect_industry_learning_candidates",
    "collect_public_research",
    "format_research_report",
    "search_public_web",
    "_ddgs_search",
]

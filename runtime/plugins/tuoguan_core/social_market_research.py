"""Read-only social platform market research for Xiaoyou."""

from __future__ import annotations

from datetime import datetime
import json
import shutil
import subprocess
import uuid
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id, read_institution_operating_model
from .write_guard import authorized_system_write


SOCIAL_MARKET_RESEARCH_CANDIDATES_FILE = "social_market_research_candidates.jsonl"
SOCIAL_MARKET_RESEARCH_RUNS_FILE = "social_market_research_runs.jsonl"
VALID_PLATFORMS = {"xiaohongshu", "douyin"}
READ_ONLY_COMMANDS = {
    "xiaohongshu": {"search", "note", "user"},
    "douyin": {"search", "location", "user-videos"},
}
WRITE_COMMAND_WORDS = {
    "publish", "delete", "follow", "unfollow", "comment", "like",
    "draft", "drafts", "stats", "update", "login",
}


def run_social_market_research(
    platform: str,
    *,
    store: TuoguanStore | None = None,
    query: str = "",
    command: str = "search",
    dry_run: bool = False,
    limit: int = 5,
    now: datetime | None = None,
    runner: Any | None = None,
) -> dict[str, Any]:
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    normalized_platform = _normalize_platform(platform)
    normalized_command = str(command or "search").strip()
    normalized_query = str(query or "").strip() or _default_query(actual_store, normalized_platform)
    run_id = f"social_market:{normalized_platform}:{timestamp.strftime('%Y%m%d%H%M%S')}:{uuid.uuid4().hex[:8]}"

    allowed = _command_allowed(normalized_platform, normalized_command)
    if not allowed:
        return _blocked_result(run_id, normalized_platform, normalized_command, normalized_query, timestamp, dry_run=dry_run)

    preflight = _opencli_preflight(normalized_platform, runner=runner)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    status = "dry_run" if dry_run else "completed"

    if not preflight.get("ok"):
        status = "backend_unavailable"
        errors.append(str(preflight.get("message") or "OpenCLI browser bridge unavailable"))
    else:
        result = _call_opencli(normalized_platform, normalized_command, normalized_query, limit=limit, runner=runner)
        if result.get("ok"):
            rows = _normalize_opencli_rows(result.get("payload"), platform=normalized_platform, query=normalized_query, timestamp=timestamp, limit=limit)
            if not rows:
                status = "source_failed"
                errors.append("opencli returned no usable social market rows")
        else:
            status = "backend_unavailable" if str(result.get("error_code") or "") == "BROWSER_CONNECT" else "source_failed"
            errors.append(str(result.get("message") or result.get("error") or "opencli social search failed"))

    run_row = {
        "run_id": run_id,
        "tenant_id": current_tenant_id(),
        "platform": normalized_platform,
        "command": normalized_command,
        "query": normalized_query,
        "status": status,
        "dry_run": bool(dry_run),
        "evidence_count": len(rows),
        "errors": errors[:20],
        "created_at": timestamp.isoformat(timespec="seconds"),
        "auto_effects": _auto_effects(),
    }
    candidate_rows = [_candidate_from_row(run_id, row, timestamp, status=status) for row in rows]

    if dry_run:
        return {"ok": True, "dry_run": True, "run": run_row, "candidates": candidate_rows, "preflight": preflight}

    with authorized_system_write(
        actual_store.data_dir,
        job_name="social_market_research_runner",
        allowed_files={SOCIAL_MARKET_RESEARCH_RUNS_FILE, SOCIAL_MARKET_RESEARCH_CANDIDATES_FILE},
    ) as auth:
        _append_jsonl(actual_store, SOCIAL_MARKET_RESEARCH_RUNS_FILE, {**run_row, "operation_id": auth.operation_id, "ledger_id": auth.ledger_id, "audit_id": auth.audit_id})
        for row in candidate_rows:
            _append_jsonl(actual_store, SOCIAL_MARKET_RESEARCH_CANDIDATES_FILE, {**row, "operation_id": auth.operation_id, "ledger_id": auth.ledger_id, "audit_id": auth.audit_id})

    return {"ok": True, "dry_run": False, "run": run_row, "candidate_count": len(candidate_rows), "writeback_verified": True}


def query_social_market_research(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    platform: str = "",
    status: str = "",
    limit: int = 30,
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"} and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "message": "只有老板、店长或系统巡检可以查看社交市场观察。"}
    platform_filter = str(platform or "").strip()
    status_filter = str(status or "").strip()
    rows: list[dict[str, Any]] = []
    for row in _read_jsonl(store, SOCIAL_MARKET_RESEARCH_CANDIDATES_FILE):
        if platform_filter and str(row.get("platform") or "") != platform_filter:
            continue
        if status_filter and str(row.get("status") or "") != status_filter:
            continue
        rows.append(row)
    rows = rows[-max(1, min(int(limit or 30), 100)):]
    platform_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for row in rows:
        platform_counts[str(row.get("platform") or "unknown")] = platform_counts.get(str(row.get("platform") or "unknown"), 0) + 1
        status_counts[str(row.get("status") or "unknown")] = status_counts.get(str(row.get("status") or "unknown"), 0) + 1
    return {
        "ok": True,
        "report_type": "social_market_research_v0",
        "candidate_count": len(rows),
        "platform_counts": platform_counts,
        "status_counts": status_counts,
        "candidates": rows,
        "rendered_text": f"查到 {len(rows)} 条社交平台市场观察候选。它们只是外部平台观察，不是优益已确认事实。",
        "render_verified": True,
    }


def _normalize_platform(platform: str) -> str:
    value = str(platform or "").strip().lower()
    if value in {"xhs", "rednote", "xiaohongshu"}:
        return "xiaohongshu"
    if value in {"douyin", "抖音"}:
        return "douyin"
    return "xiaohongshu"


def _command_allowed(platform: str, command: str) -> bool:
    normalized = str(command or "").strip()
    if not normalized or normalized in WRITE_COMMAND_WORDS:
        return False
    return normalized in READ_ONLY_COMMANDS.get(platform, set())


def _opencli_preflight(platform: str, *, runner: Any | None = None) -> dict[str, Any]:
    probe = "whoami"
    result = _run_opencli([platform, probe, "-f", "json", "--window", "background", "--site-session", "persistent"], runner=runner)
    if result.get("ok"):
        return {"ok": True, "backend": "opencli", "platform": platform}
    return {"ok": False, "backend": "opencli", "platform": platform, "message": str(result.get("message") or result.get("error") or "OpenCLI unavailable"), "error_code": str(result.get("error_code") or "")}


def _call_opencli(platform: str, command: str, query: str, *, limit: int, runner: Any | None = None) -> dict[str, Any]:
    args = [platform, command, query, "-f", "json", "--window", "background", "--site-session", "persistent"]
    return _run_opencli(args, runner=runner)


def _run_opencli(args: list[str], *, runner: Any | None = None) -> dict[str, Any]:
    if any(str(part).strip() in WRITE_COMMAND_WORDS for part in args):
        return {"ok": False, "error": "write_command_blocked", "message": "社交平台市场扫描只允许只读命令。"}
    opencli = "opencli" if runner is not None else _resolve_opencli_executable()
    if not opencli:
        return {"ok": False, "error": "opencli_missing", "message": "OpenCLI 未安装，无法执行社交平台只读扫描。", "error_code": "OPENCLI_MISSING"}
    command = [opencli, *args]
    try:
        completed = (runner or subprocess.run)(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "opencli_missing", "message": "OpenCLI 未安装，无法执行社交平台只读扫描。", "error_code": "OPENCLI_MISSING"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "opencli_timeout", "message": "OpenCLI 社交平台扫描超时。", "error_code": "TIMEOUT"}
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        payload = _parse_json(stdout) or _parse_json(stderr) or {}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        inferred_code = _infer_opencli_error_code(stdout, stderr)
        return {
            "ok": False,
            "error": str(error.get("code") or inferred_code or "opencli_failed"),
            "error_code": str(error.get("code") or inferred_code or ""),
            "message": str(error.get("message") or stderr or stdout or "OpenCLI command failed")[:1000],
        }
    return {"ok": True, "payload": _parse_json(stdout) or stdout}


def _infer_opencli_error_code(stdout: str, stderr: str) -> str:
    text = f"{stdout}\n{stderr}"
    for code in ("BROWSER_CONNECT", "AUTH_REQUIRED", "RATE_LIMIT", "CAPTCHA_REQUIRED", "NETWORK_ERROR"):
        if code in text:
            return code
    return ""


def _resolve_opencli_executable() -> str:
    return (
        shutil.which("opencli.cmd")
        or shutil.which("opencli")
        or shutil.which("opencli.ps1")
        or ""
    )


def _parse_json(text: str) -> Any:
    try:
        return json.loads(str(text or "").strip())
    except ValueError:
        return None


def _normalize_opencli_rows(payload: Any, *, platform: str, query: str, timestamp: datetime, limit: int) -> list[dict[str, Any]]:
    raw_rows = _extract_rows(payload)
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        title = _first_text(raw, ("title", "desc", "description", "note_display_title", "aweme_desc", "caption", "content"))
        url = _first_text(raw, ("url", "share_url", "link", "note_url", "video_url"))
        item_id = _first_text(raw, ("id", "note_id", "aweme_id", "sec_uid", "user_id"))
        excerpt = _first_text(raw, ("text", "content", "desc", "description", "aweme_desc", "caption"), limit=700)
        metrics = {key: raw.get(key) for key in ("liked_count", "collected_count", "comment_count", "share_count", "digg_count", "play_count") if key in raw}
        if not title and not excerpt and not item_id:
            continue
        rows.append({
            "platform": platform,
            "query": query,
            "source_id": item_id,
            "url": url,
            "title": title or excerpt[:80],
            "text_excerpt": excerpt,
            "metrics": metrics,
            "collected_at": timestamp.isoformat(timespec="seconds"),
            "evidence_level": "platform_observation",
        })
        if len(rows) >= max(1, min(int(limit or 5), 20)):
            break
    return rows


def _extract_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "results", "data", "notes", "videos", "feeds"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _extract_rows(value)
                if nested:
                    return nested
    return []


def _first_text(row: dict[str, Any], keys: tuple[str, ...], *, limit: int = 240) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = " ".join(str(value).split())
        if text:
            return text[:limit]
    return ""


def _candidate_from_row(run_id: str, row: dict[str, Any], timestamp: datetime, *, status: str) -> dict[str, Any]:
    return {
        "candidate_id": f"social_market_{uuid.uuid4().hex[:16]}",
        "tenant_id": current_tenant_id(),
        "run_id": run_id,
        "platform": row.get("platform"),
        "query": row.get("query"),
        "source_id": row.get("source_id"),
        "url": row.get("url"),
        "title": row.get("title"),
        "text_excerpt": row.get("text_excerpt"),
        "metrics": row.get("metrics") or {},
        "status": "pending_review" if status == "completed" else status,
        "evidence_level": row.get("evidence_level") or "platform_observation",
        "dimensions": ["local_peer_content", "enrollment_offer", "parent_pain_point", "opening_season_activity", "short_video_presentation"],
        "collected_at": row.get("collected_at") or timestamp.isoformat(timespec="seconds"),
        "auto_effects": _auto_effects(),
    }


def _default_query(store: TuoguanStore, platform: str) -> str:
    model = read_institution_operating_model(store)
    hints: list[str] = []
    if isinstance(model, dict):
        for key in ("city", "district", "business_area", "address"):
            value = str(model.get(key) or "").strip()
            if value:
                hints.append(value)
        institution = model.get("institution") if isinstance(model.get("institution"), dict) else {}
        for key in ("city", "district", "business_area", "address"):
            value = str(institution.get(key) or "").strip()
            if value:
                hints.append(value)
    location = " ".join(dict.fromkeys(hints))[:80] or "项城"
    platform_hint = "小红书" if platform == "xiaohongshu" else "抖音"
    return f"{location} 托管机构 招生 晚托 开学 {platform_hint}"


def _blocked_result(run_id: str, platform: str, command: str, query: str, timestamp: datetime, *, dry_run: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "dry_run": bool(dry_run),
        "error": "social_market_write_command_blocked",
        "message": "社交平台市场扫描只允许只读搜索和读取，不允许发布、删除、关注、评论、点赞或草稿操作。",
        "run": {
            "run_id": run_id,
            "platform": platform,
            "command": command,
            "query": query,
            "status": "blocked",
            "created_at": timestamp.isoformat(timespec="seconds"),
            "auto_effects": _auto_effects(),
        },
    }


def _append_jsonl(store: TuoguanStore, name: str, row: dict[str, Any]) -> None:
    path = store.path_for(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_jsonl(store: TuoguanStore, name: str) -> list[dict[str, Any]]:
    path = store.path_for(name)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _auto_effects() -> dict[str, bool]:
    return {
        "updates_institution_facts": False,
        "updates_handbook": False,
        "sends_messages": False,
        "publishes_social_content": False,
        "likes_or_comments": False,
        "follows_accounts": False,
        "forces_next_action": False,
    }

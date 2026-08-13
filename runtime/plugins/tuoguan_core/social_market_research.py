"""Read-only social platform market research for Xiaoyou."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import time
import uuid
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id, read_institution_operating_model
from .write_guard import authorized_system_write


SOCIAL_MARKET_RESEARCH_CANDIDATES_FILE = "social_market_research_candidates.jsonl"
SOCIAL_MARKET_RESEARCH_CONFIG_FILE = "social_market_research_config.json"
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
NON_MARKET_URL_MARKERS = (
    "agree.", "terms", "privacy", "protocol", "login", "passport",
    "help", "legal", "policy", "download", "creator",
)
NON_MARKET_TEXT_MARKERS = (
    "用户协议", "隐私政策", "儿童/青少年", "我已阅读并同意", "扫码登录",
    "验证码", "登录后查看更多", "请先登录",
)
DEFAULT_SOCIAL_MARKET_CONFIG = {
    "platforms": ["xiaohongshu", "douyin"],
    "queries": ["项城托管", "项城晚托", "项城作业辅导", "项城小饭桌", "项城托管招生", "开学收心班", "暑假托管"],
    "limit_per_query": 10,
    "backend_order": ["opencli", "browser"],
    "sleep_seconds_min": 3,
    "sleep_seconds_max": 8,
}
BACKEND_UNAVAILABLE_ERROR_CODES = {
    "AUTH_REQUIRED", "BROWSER_BACKEND_MISSING", "BROWSER_CONNECT",
    "BROWSER_SCRIPT_MISSING", "CAPTCHA_REQUIRED", "NODE_MISSING",
    "OPENCLI_MISSING", "PLAYWRIGHT_MISSING", "PROFILE_IN_USE",
}


def run_social_market_research(
    platform: str,
    *,
    store: TuoguanStore | None = None,
    query: str = "",
    command: str = "search",
    dry_run: bool = False,
    limit: int = 5,
    backend_order: list[str] | tuple[str, ...] | None = None,
    now: datetime | None = None,
    runner: Any | None = None,
    browser_runner: Any | None = None,
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

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    status = "dry_run" if dry_run else "completed"
    used_backend = ""

    preflight: dict[str, Any] = {"ok": False, "backend": "not_attempted"}
    effective_browser_runner = browser_runner if browser_runner is not None else runner
    for backend in _normalize_backend_order(backend_order or social_market_research_config(actual_store).get("backend_order")):
        if backend == "opencli":
            preflight = _opencli_preflight(normalized_platform, runner=runner)
            if not preflight.get("ok"):
                errors.append(str(preflight.get("message") or "OpenCLI browser bridge unavailable"))
                continue
            result = _call_opencli(normalized_platform, normalized_command, normalized_query, limit=limit, runner=runner)
        elif backend == "browser":
            result = _call_browser_fallback(
                normalized_platform,
                normalized_command,
                normalized_query,
                limit=limit,
                runner=effective_browser_runner,
            )
        else:
            continue
        if result.get("ok"):
            used_backend = backend
            rows = _normalize_opencli_rows(result.get("payload"), platform=normalized_platform, query=normalized_query, timestamp=timestamp, limit=limit)
            if not rows:
                status = "source_failed"
                errors.append(f"{backend} returned no usable social market rows")
                continue
            status = "completed"
            break
        status = "backend_unavailable" if str(result.get("error_code") or "") in BACKEND_UNAVAILABLE_ERROR_CODES else "source_failed"
        errors.append(str(result.get("message") or result.get("error") or f"{backend} social search failed"))
    if not rows and not errors:
        status = "backend_unavailable"
        errors.append("No social market backend was available.")

    run_row = {
        "run_id": run_id,
        "tenant_id": current_tenant_id(),
        "platform": normalized_platform,
        "command": normalized_command,
        "query": normalized_query,
        "status": status,
        "backend": used_backend,
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


def run_social_market_batch(
    *,
    store: TuoguanStore | None = None,
    dry_run: bool = False,
    platforms: list[str] | None = None,
    queries: list[str] | None = None,
    limit: int | None = None,
    sleep_between: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    actual_store = store or TuoguanStore()
    config = social_market_research_config(actual_store)
    selected_platforms = [_normalize_platform(item) for item in (platforms or config.get("platforms") or []) if str(item or "").strip()]
    selected_platforms = [item for item in selected_platforms if item in VALID_PLATFORMS] or list(DEFAULT_SOCIAL_MARKET_CONFIG["platforms"])
    selected_queries = [str(item).strip() for item in (queries or config.get("queries") or []) if str(item or "").strip()]
    selected_queries = selected_queries or list(DEFAULT_SOCIAL_MARKET_CONFIG["queries"])
    per_query_limit = int(limit or config.get("limit_per_query") or DEFAULT_SOCIAL_MARKET_CONFIG["limit_per_query"])
    backend_order = _normalize_backend_order(config.get("backend_order"))
    min_sleep = max(0, int(config.get("sleep_seconds_min") or 0))
    max_sleep = max(min_sleep, int(config.get("sleep_seconds_max") or min_sleep))
    runs: list[dict[str, Any]] = []
    for platform in selected_platforms:
        for query in selected_queries[:8]:
            result = run_social_market_research(
                platform,
                store=actual_store,
                query=query,
                command="search",
                dry_run=dry_run,
                limit=per_query_limit,
                backend_order=backend_order,
                now=now,
            )
            runs.append(result)
            if sleep_between and not dry_run and max_sleep > 0:
                time.sleep(random.randint(min_sleep, max_sleep))
    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "platforms": selected_platforms,
        "query_count": len(selected_queries[:8]),
        "run_count": len(runs),
        "candidate_count": sum(int(item.get("candidate_count") or len(item.get("candidates") or [])) for item in runs if isinstance(item, dict)),
        "runs": runs,
    }


def social_market_research_config(store: TuoguanStore) -> dict[str, Any]:
    payload = store.read_json(SOCIAL_MARKET_RESEARCH_CONFIG_FILE, {})
    payload = payload if isinstance(payload, dict) else {}
    merged = {**DEFAULT_SOCIAL_MARKET_CONFIG, **payload}
    merged["platforms"] = [item for item in merged.get("platforms") or [] if _normalize_platform(item) in VALID_PLATFORMS]
    merged["queries"] = [str(item).strip() for item in merged.get("queries") or [] if str(item).strip()]
    merged["backend_order"] = _normalize_backend_order(merged.get("backend_order"))
    return merged


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
    quarantined_count = 0
    for row in _read_jsonl(store, SOCIAL_MARKET_RESEARCH_CANDIDATES_FILE):
        if str(row.get("tenant_id") or "") not in {"", current_tenant_id()}:
            continue
        if platform_filter and str(row.get("platform") or "") != platform_filter:
            continue
        if status_filter and str(row.get("status") or "") != status_filter:
            continue
        if not _market_candidate_evidence_complete(row):
            quarantined_count += 1
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
        "quarantined_incomplete_evidence_count": quarantined_count,
        "platform_counts": platform_counts,
        "status_counts": status_counts,
        "candidates": rows,
        "rendered_text": (
            f"查到 {len(rows)} 条有来源的社交平台市场观察候选；"
            f"另有 {quarantined_count} 条因来源、时间或证据等级不完整而隔离。"
            "它们只是外部平台观察，不是优益已确认事实。"
        ),
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


def _normalize_backend_order(value: Any) -> list[str]:
    raw = value if isinstance(value, (list, tuple)) else []
    order: list[str] = []
    for item in raw:
        backend = str(item or "").strip().lower()
        if backend in {"opencli", "browser"} and backend not in order:
            order.append(backend)
    return order or ["opencli", "browser"]


def _opencli_preflight(platform: str, *, runner: Any | None = None) -> dict[str, Any]:
    probe = "whoami"
    result = _run_opencli([platform, probe, "-f", "json", "--window", "background", "--site-session", "persistent"], runner=runner)
    if result.get("ok"):
        return {"ok": True, "backend": "opencli", "platform": platform}
    return {"ok": False, "backend": "opencli", "platform": platform, "message": str(result.get("message") or result.get("error") or "OpenCLI unavailable"), "error_code": str(result.get("error_code") or "")}


def _call_opencli(platform: str, command: str, query: str, *, limit: int, runner: Any | None = None) -> dict[str, Any]:
    args = [platform, command, query, "-f", "json", "--window", "background", "--site-session", "persistent"]
    if command == "search":
        args.extend(["--limit", str(max(1, min(int(limit or 5), 20)))])
    return _run_opencli(args, runner=runner)


def _call_browser_fallback(platform: str, command: str, query: str, *, limit: int, runner: Any | None = None) -> dict[str, Any]:
    if command != "search":
        return {"ok": False, "error": "browser_backend_command_unsupported", "message": "浏览器兜底后端当前只支持只读搜索。", "error_code": "UNSUPPORTED_COMMAND"}
    script = _resolve_browser_fallback_script()
    if not script and runner is None:
        return {"ok": False, "error": "browser_script_missing", "message": "服务器浏览器兜底脚本不存在。", "error_code": "BROWSER_SCRIPT_MISSING"}
    node = shutil.which("node") or ""
    if not node and runner is None:
        return {"ok": False, "error": "node_missing", "message": "Node.js 不可用，无法运行浏览器兜底采集。", "error_code": "NODE_MISSING"}
    social_root = os.environ.get("HERMES_SOCIAL_RESEARCH_ROOT", "/opt/hermes-youyi/social-research")
    profile = os.environ.get("HERMES_SOCIAL_CHROMIUM_PROFILE", f"{social_root}/chromium-profile")
    executable = os.environ.get("HERMES_SOCIAL_CHROMIUM", shutil.which("chromium-browser") or shutil.which("chromium") or "")
    command_line = [
        node or "node",
        str(script or "social_market_browser_fallback.js"),
        "--platform", platform,
        "--query", query,
        "--limit", str(max(1, min(int(limit or 5), 20))),
        "--profile-dir", profile,
    ]
    if executable:
        command_line.extend(["--executable", executable])
    xvfb = shutil.which("xvfb-run")
    if runner is None and xvfb and os.environ.get("DISPLAY", "") == "":
        command_line = [xvfb, "-a", "--server-args=-screen 0 1280x900x24", *command_line]
    try:
        child_env = os.environ.copy()
        child_env.setdefault("HERMES_SOCIAL_RESEARCH_ROOT", social_root)
        child_env.setdefault("HERMES_SOCIAL_CHROMIUM_PROFILE", profile)
        child_env.setdefault("NODE_PATH", f"{social_root}/node_modules")
        completed = (runner or subprocess.run)(
            command_line,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=90,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "browser_backend_missing", "message": "浏览器兜底后端不可用。", "error_code": "BROWSER_BACKEND_MISSING"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "browser_backend_timeout", "message": "浏览器兜底采集超时。", "error_code": "TIMEOUT"}
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    payload = _parse_json(stdout) or _parse_json(stderr) or {}
    if int(getattr(completed, "returncode", 1) or 0) != 0 or not (isinstance(payload, dict) and payload.get("ok")):
        nested_error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
        error_code = str(payload.get("error_code") or nested_error.get("code") or "") if isinstance(payload, dict) else ""
        return {
            "ok": False,
            "error": error_code or str(payload.get("error") or "browser_backend_failed") if isinstance(payload, dict) else "browser_backend_failed",
            "error_code": error_code,
            "message": str(payload.get("message") or nested_error.get("message") or stderr or stdout or "浏览器兜底采集失败。")[:1000] if isinstance(payload, dict) else str(stderr or stdout)[:1000],
        }
    return {"ok": True, "payload": payload}


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
    social_root = os.environ.get("HERMES_SOCIAL_RESEARCH_ROOT", "/opt/hermes-youyi/social-research")
    candidates = [
        os.environ.get("HERMES_OPENCLI", ""),
        f"{social_root}/bin/opencli",
        f"{social_root}/node_modules/.bin/opencli",
        shutil.which("opencli.cmd") or "",
        shutil.which("opencli") or "",
        shutil.which("opencli.ps1") or "",
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        if shutil.which(value):
            return value
        path = Path(value)
        if path.exists():
            return str(path)
    return ""


def _resolve_browser_fallback_script() -> Path | None:
    env_path = os.environ.get("HERMES_SOCIAL_BROWSER_SCRIPT", "")
    candidates = [Path(env_path)] if env_path else []
    cwd = Path.cwd()
    candidates.extend([
        cwd / "scripts" / "social_market_browser_fallback.js",
        cwd.parent / "scripts" / "social_market_browser_fallback.js",
        Path("/opt/hermes-youyi-upgrade-0.19.0/scripts/social_market_browser_fallback.js"),
    ])
    for path in candidates:
        if path and path.exists():
            return path
    return None


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
        author = _first_text(raw, ("author", "nickname", "user_name", "creator", "name"), limit=120)
        published_at = _first_text(raw, ("published_at", "publish_time", "create_time", "time"), limit=120)
        metrics = {key: raw.get(key) for key in ("liked_count", "collected_count", "comment_count", "share_count", "digg_count", "play_count") if key in raw}
        if not title and not excerpt and not item_id:
            continue
        if not url and not item_id:
            continue
        if _is_non_market_row(platform=platform, url=url, title=title, excerpt=excerpt, query=query):
            continue
        rows.append({
            "platform": platform,
            "query": query,
            "source_id": item_id,
            "url": url,
            "title": title or excerpt[:80],
            "text_excerpt": excerpt,
            "author": author,
            "published_at": published_at,
            "metrics": metrics,
            "collected_at": timestamp.isoformat(timespec="seconds"),
            "evidence_level": "platform_observation",
        })
        if len(rows) >= max(1, min(int(limit or 5), 20)):
            break
    return rows


def _is_non_market_row(*, platform: str, url: str, title: str, excerpt: str, query: str) -> bool:
    url_lower = str(url or "").lower()
    text = f"{title} {excerpt}".strip()
    if any(marker in url_lower for marker in NON_MARKET_URL_MARKERS):
        return True
    if any(marker in text for marker in NON_MARKET_TEXT_MARKERS):
        return True
    if platform == "xiaohongshu" and url_lower:
        if "xiaohongshu.com" not in url_lower:
            return False
        if "/explore/" not in url_lower and "/search_result/" not in url_lower and "/user/profile/" not in url_lower:
            return True
    if platform == "douyin" and url_lower:
        if "douyin.com" not in url_lower:
            return False
        if not any(marker in url_lower for marker in ("/video/", "/note/", "/user/")):
            return True
    hints = _query_relevance_hints(query)
    if hints and text:
        if not any(hint.lower() in f"{url_lower} {text.lower()}" for hint in hints):
            return True
    return False


def _query_relevance_hints(query: str) -> list[str]:
    text = " ".join(str(query or "").split())
    dictionary = (
        "项城", "托管", "晚托", "作业", "辅导", "小饭桌", "招生",
        "开学", "收心", "暑假", "寒假", "托班", "自习", "接送",
    )
    hints = [item for item in dictionary if item in text]
    if len(text) >= 2 and text not in hints:
        hints.append(text)
    return hints


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
        "author": row.get("author"),
        "published_at": row.get("published_at"),
        "metrics": row.get("metrics") or {},
        "status": "pending_review" if status == "completed" else status,
        "evidence_level": row.get("evidence_level") or "platform_observation",
        "dimensions": ["local_peer_content", "enrollment_offer", "parent_pain_point", "opening_season_activity", "short_video_presentation"],
        "collected_at": row.get("collected_at") or timestamp.isoformat(timespec="seconds"),
        "auto_effects": _auto_effects(),
    }


def _market_candidate_evidence_complete(row: dict[str, Any]) -> bool:
    return bool(
        str(row.get("platform") or "").strip() in VALID_PLATFORMS
        and str(row.get("query") or "").strip()
        and (str(row.get("url") or "").strip() or str(row.get("source_id") or "").strip())
        and (str(row.get("title") or "").strip() or str(row.get("text_excerpt") or "").strip())
        and str(row.get("collected_at") or "").strip()
        and str(row.get("evidence_level") or "").strip()
    )


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
    store.append_jsonl_verified(name, row)


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

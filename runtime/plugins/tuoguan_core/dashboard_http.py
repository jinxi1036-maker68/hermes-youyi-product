"""Read-only aiohttp routes for the tutoring-center mobile dashboard."""

from __future__ import annotations

import html
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from aiohttp import web

from .dashboard_auth import (
    DashboardAuthError,
    verify_dashboard_token,
    verify_parent_report_token,
)
from .dashboard_builder import load_dashboard_cache, refresh_dashboard_cache
from .dashboard_workbench_v1 import DASHBOARD_WORKBENCH_V1_HTML
from .growth_reports import (
    growth_report_by_id,
    parent_report_payload,
    resolve_parent_report_short_link,
)
from .models import UserIdentity
from .programs import REGULAR_PROGRAM_ID, SUMMER_PROGRAM_ID, user_program_ids
from .store import TuoguanStore


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-XiaoYou-Dashboard": "current-workspace-v1",
}

_DASHBOARD_SERVICE_HEADER = "X-XiaoYou-Dashboard"
_DASHBOARD_SERVICE_VALUE = "current-workspace-v1"


def verify_public_dashboard_route(base_url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
    """Verify the configured public dashboard *route*, not merely its origin.

    The configuration is trusted deployment metadata, but it can become stale
    when a reverse proxy or dashboard service changes.  Link generation calls
    this probe before issuing a user-facing signed URL.  The marker header is
    emitted only by the current XiaoYou dashboard sidecar, so a generic H5
    fallback page cannot be mistaken for the institution dashboard.
    """

    raw_base = str(base_url or "").strip()
    parsed = urlsplit(raw_base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "dashboard_base_url_invalid"
    route = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{parsed.path.rstrip('/')}/tuoguan/dashboard",
            "",
            "",
        )
    )
    request = Request(route, headers={"User-Agent": "XiaoYou-dashboard-route-probe/1"})
    try:
        with urlopen(request, timeout=max(0.1, float(timeout_seconds))) as response:
            status = int(getattr(response, "status", response.getcode()))
            marker = str(response.headers.get(_DASHBOARD_SERVICE_HEADER) or "")
            if 200 <= status < 300 and marker == _DASHBOARD_SERVICE_VALUE:
                return True, ""
            if not 200 <= status < 300:
                return False, f"dashboard_route_http_{status}"
            return False, "dashboard_route_identity_mismatch"
    except HTTPError as exc:
        return False, f"dashboard_route_http_{int(exc.code)}"
    except (URLError, OSError, ValueError):
        return False, "dashboard_route_unreachable"


def dashboard_enabled() -> bool:
    return str(os.getenv("HERMES_TUOGUAN_DASHBOARD_ENABLED") or "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _json_error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def _display_name(store: TuoguanStore, user_id: str) -> str:
    mapping = store.read_json("teacher_wecom_map.json", {})
    if isinstance(mapping, dict):
        for name, uid in mapping.items():
            if str(uid) == user_id:
                return str(name)
    return user_id


class TuoguanDashboardHttp:
    def __init__(self, store: TuoguanStore | None = None) -> None:
        self.store = store or TuoguanStore()

    def register(self, app: web.Application) -> None:
        app.router.add_get("/tuoguan/dashboard", self.handle_dashboard)
        app.router.add_get("/tuoguan/parent-report", self.handle_parent_report_page)
        app.router.add_get("/tuoguan/r/{code}", self.handle_parent_report_short_page)
        app.router.add_get("/tuoguan/api/me", self.handle_me)
        app.router.add_get("/tuoguan/api/teacher", self.handle_teacher)
        app.router.add_get("/tuoguan/api/boss", self.handle_boss)
        app.router.add_get("/tuoguan/api/parent-report", self.handle_parent_report_api)

    def _principal(self, request: web.Request):
        token = str(request.query.get("token") or "")
        return verify_dashboard_token(token, self.store)

    async def handle_dashboard(self, request: web.Request) -> web.Response:
        token = html.escape(str(request.query.get("token") or ""), quote=True)
        return web.Response(
            text=_DASHBOARD_HTML.replace("__TOKEN__", token),
            content_type="text/html",
            charset="utf-8",
            headers=_NO_CACHE_HEADERS,
        )

    async def handle_parent_report_page(self, request: web.Request) -> web.Response:
        token = html.escape(str(request.query.get("token") or ""), quote=True)
        return web.Response(
            text=_PARENT_REPORT_HTML.replace("__TOKEN__", token).replace("__CODE__", ""),
            content_type="text/html",
            charset="utf-8",
        )

    async def handle_parent_report_short_page(self, request: web.Request) -> web.Response:
        code = html.escape(str(request.match_info.get("code") or ""), quote=True)
        return web.Response(
            text=_PARENT_REPORT_HTML.replace("__TOKEN__", "").replace("__CODE__", code),
            content_type="text/html",
            charset="utf-8",
        )

    async def handle_me(self, request: web.Request) -> web.Response:
        try:
            principal = self._principal(request)
        except DashboardAuthError as exc:
            return _json_error(401, exc.code, exc.message)
        role_label = {"teacher": "老师", "manager": "店长", "boss": "老板"}.get(principal.role, principal.role)
        identity = UserIdentity(
            platform="wecom",
            platform_user_id=principal.user_id,
            canonical_user_id=principal.user_id,
            person_name=_display_name(self.store, principal.user_id),
            role=principal.role,
            approval_state="approved",
        )
        scope = user_program_ids(self.store, identity)
        if principal.role == "boss":
            program_role, default_program_id = "global_owner", "global"
        elif scope == {SUMMER_PROGRAM_ID}:
            program_role, default_program_id = "summer_manager" if principal.role == "manager" else "summer_teacher", SUMMER_PROGRAM_ID
        else:
            program_role, default_program_id = "regular_manager" if principal.role == "manager" else "regular_teacher", REGULAR_PROGRAM_ID
        return web.json_response(
            {
                "user_id": principal.user_id,
                "role": principal.role,
                "role_label": role_label,
                "display_name": _display_name(self.store, principal.user_id),
                "expires_at": principal.expires_at,
                "program_role": program_role,
                "default_program_id": default_program_id,
                "program_scope": sorted(scope) if scope is not None else ["global", REGULAR_PROGRAM_ID, SUMMER_PROGRAM_ID],
            },
        )

    async def handle_teacher(self, request: web.Request) -> web.Response:
        try:
            principal = self._principal(request)
        except DashboardAuthError as exc:
            return _json_error(401, exc.code, exc.message)
        if principal.role != "teacher":
            return _json_error(403, "forbidden", "Only teacher dashboard tokens can access this API")
        refresh_dashboard_cache(self.store)
        cache = load_dashboard_cache(self.store)
        data = (cache.get("teacher_dashboards") or {}).get(principal.user_id)
        if not isinstance(data, dict):
            data = {
                "role": "teacher",
                "user_id": principal.user_id,
                "display_name": _display_name(self.store, principal.user_id),
                "summary": {},
                "today_feedback": [],
                "week_contribution": {},
                "uncovered_students": [],
                "open_tasks": [],
                "student_completion": [],
                "readonly_hint": "记录请回企业微信直接说",
            }
        return web.json_response(data)

    async def handle_boss(self, request: web.Request) -> web.Response:
        try:
            principal = self._principal(request)
        except DashboardAuthError as exc:
            return _json_error(401, exc.code, exc.message)
        if principal.role not in {"manager", "boss"}:
            return _json_error(403, "forbidden", "Only manager or boss dashboard tokens can access this API")
        refresh_dashboard_cache(self.store)
        cache = load_dashboard_cache(self.store)
        if principal.role == "manager":
            data = (cache.get("manager_dashboards") or {}).get(principal.user_id)
        else:
            selected_program = str(request.query.get("program_id") or "global").strip()
            if selected_program in {"", "global"}:
                data = cache.get("boss_dashboard")
            elif selected_program in {REGULAR_PROGRAM_ID, SUMMER_PROGRAM_ID}:
                data = (cache.get("boss_project_dashboards") or {}).get(selected_program)
            else:
                return _json_error(400, "invalid_program", "Unknown dashboard program view")
        if not isinstance(data, dict):
            data = {"role": principal.role, "user_id": principal.user_id, "summary": {}}
        return web.json_response(data)

    async def handle_parent_report_api(self, request: web.Request) -> web.Response:
        token = str(request.query.get("token") or "")
        code = str(request.query.get("code") or "")
        try:
            if code:
                link = resolve_parent_report_short_link(
                    self.store,
                    code=code,
                    mark_access=True,
                )
                if not link:
                    raise DashboardAuthError("expired", "Parent report link is invalid or expired")
                token = str(link.get("token") or "")
            principal = verify_parent_report_token(token, self.store)
        except DashboardAuthError as exc:
            return _json_error(401, exc.code, exc.message)
        report = growth_report_by_id(self.store, principal.report_id)
        if report is None:
            return _json_error(404, "not_found", "Parent report not found")
        return web.json_response(parent_report_payload(report))


def register_wecom_callback_routes(app: web.Application, adapter: Any | None = None) -> bool:
    if not dashboard_enabled():
        return False
    TuoguanDashboardHttp().register(app)
    return True


_LEGACY_DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>托管 AI 看板</title>
  <style>
    :root{color-scheme:light;--ink:#172026;--muted:#69747c;--line:#dfe5e8;--bg:#f6f8f7;--panel:#ffffff;--green:#16845b;--amber:#b36b00;--red:#b42318;--blue:#2864b4}
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;letter-spacing:0;overflow-x:hidden}
    .app{width:100%;max-width:min(720px,100vw);margin:0 auto;min-height:100vh;padding:18px 14px 28px;overflow-x:hidden}
    header{padding:10px 2px 16px}
    .eyebrow{font-size:14px;color:var(--green);font-weight:800}
    h1{margin:6px 0 4px;font-size:30px;line-height:1.15}
    .sub{font-size:16px;color:var(--muted);line-height:1.45}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
    .task-grid{grid-template-columns:1fr}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:13px;box-shadow:0 1px 2px rgba(23,32,38,.04)}
    .wide-card{grid-column:1/-1}
    .metric .value{font-size:28px;font-weight:900;line-height:1.05}
    .metric .value{word-break:break-word}
    .metric .label{margin-top:6px;font-size:14px;color:var(--muted)}
    .section{margin-top:14px}
    h2{font-size:20px;margin:0 0 10px}
    .list{display:flex;flex-direction:column;gap:8px}
    .row{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;border-top:1px solid var(--line);padding-top:9px;margin-top:9px}
    .row:first-child{border-top:0;padding-top:0;margin-top:0}
    .title{font-size:17px;font-weight:800;line-height:1.35}
    .desc{font-size:15px;color:var(--muted);line-height:1.45;margin-top:4px}
    .pill{display:inline-flex;align-items:center;border-radius:999px;padding:5px 9px;font-size:14px;font-weight:800;background:#eef5f1;color:var(--green);white-space:nowrap}
    .pill.active{background:var(--green);color:#fff;box-shadow:0 0 0 2px rgba(30,109,85,.14)}
    .pill.red{background:#fff0ee;color:var(--red)}
    .pill.amber{background:#fff6e8;color:var(--amber)}
    .bar{height:8px;background:#edf1f2;border-radius:999px;overflow:hidden;margin-top:8px}
    .bar span{display:block;height:100%;background:var(--green);border-radius:999px}
    .tabs{display:flex;gap:8px;margin:14px 0 8px;overflow:auto;padding-bottom:2px}
    .tab{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:10px 12px;font-size:15px;font-weight:800;color:var(--muted);white-space:nowrap}
    .tab.active{border-color:var(--green);color:var(--green)}
    .clickable{cursor:pointer}
    .hidden{display:none}
    .detail{margin-top:9px;padding:10px;border-radius:8px;background:#f7faf8;color:var(--muted);font-size:14px;line-height:1.6}
    .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}
    .chip{border:1px solid var(--line);border-radius:999px;padding:3px 7px;background:#fff;font-size:12px;color:var(--muted)}
    .hero{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:15px;margin-bottom:12px}
    .cockpit{background:linear-gradient(180deg,#edf4f8 0%,#f8fafb 100%);border:1px solid #d7e3ea;border-radius:8px;padding:12px;margin-bottom:12px;box-shadow:0 6px 18px rgba(23,32,38,.06)}
    .summary-strip{background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px;margin:10px 0;color:var(--ink);font-size:16px;line-height:1.55}
    .boss-brief{display:flex;flex-direction:column;gap:10px}
    .boss-focus{background:#fff;border:1px solid var(--line);border-radius:8px;padding:13px}
    .boss-focus .kicker{font-size:13px;font-weight:800;color:var(--green);margin-bottom:6px}
    .boss-focus .headline{font-size:18px;line-height:1.38;font-weight:900}
    .boss-focus .meta{margin-top:8px;color:var(--muted);font-size:14px;line-height:1.5}
    .brief-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
    .brief-card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:11px;min-height:74px}
    .brief-card .label{font-size:13px;color:var(--muted)}
    .brief-card .value{font-size:17px;font-weight:900;line-height:1.25;margin-top:4px;color:var(--ink);word-break:break-word}
    .decision-list{display:flex;flex-direction:column;gap:8px}
    .toggle-card{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin-top:10px;overflow:hidden}
    .toggle-head{display:flex;justify-content:space-between;gap:10px;padding:13px;cursor:pointer}
    .toggle-head .title{font-size:17px}
    .toggle-body{border-top:1px solid var(--line);padding:12px;background:#fbfcfb}
    .status-red{border-left:4px solid var(--red)}
    .status-amber{border-left:4px solid var(--amber)}
    .status-green{border-left:4px solid var(--green)}
    .status-blue{border-left:4px solid var(--blue)}
    .hero .big{font-size:19px;line-height:1.38;font-weight:900}
    .hero .note{margin-top:8px;color:var(--muted);font-size:15px;line-height:1.5}
    .quote{border-left:3px solid var(--green);padding-left:10px;margin-top:10px;color:var(--ink);font-size:13px;line-height:1.55}
    .money{font-size:28px;font-weight:900;line-height:1.05;color:var(--green)}
    .money small{font-size:13px;color:var(--muted);font-weight:700}
    .split{display:grid;grid-template-columns:1fr;gap:10px}
    @media (min-width:520px){.split{grid-template-columns:1.25fr .75fr}}
    .amount-list{display:flex;flex-direction:column;gap:7px}
    .amount-line{display:flex;justify-content:space-between;gap:10px;font-size:15px;color:var(--muted)}
    .amount-line b{color:var(--ink)}
    .tree-card{position:relative;overflow:hidden;min-height:260px;background:linear-gradient(180deg,#ffffff 0%,#f4faf6 100%)}
    .tree-stage{height:190px;position:relative;margin-top:6px;border-radius:8px;background:linear-gradient(180deg,#eef8f1 0%,#fbfdfb 62%,#e7f0e5 63%,#f7faf6 100%)}
    .sun{position:absolute;right:18px;top:15px;width:34px;height:34px;border-radius:50%;background:#ffd36b;box-shadow:0 0 0 8px rgba(255,211,107,.18)}
    .tree{position:absolute;left:50%;bottom:36px;width:150px;height:140px;transform-origin:50% 100%;transform:translateX(-50%) scale(var(--tree-scale,.75));transition:transform .9s ease}
    .trunk{position:absolute;left:67px;bottom:0;width:18px;height:82px;border-radius:10px;background:linear-gradient(90deg,#8a5528,#b7773d,#7b491f);transform-origin:50% 100%;animation:growTrunk .9s ease both}
    .branch{position:absolute;left:76px;bottom:48px;width:48px;height:8px;border-radius:999px;background:#8a5528;transform-origin:left center;transform:rotate(-30deg);animation:sway 3.8s ease-in-out infinite}
    .branch.b2{left:28px;bottom:61px;transform-origin:right center;transform:rotate(28deg)}
    .leaf{position:absolute;width:48px;height:38px;border-radius:50% 50% 45% 45%;background:#25a66f;opacity:var(--leaf-opacity,.86);box-shadow:0 4px 10px rgba(22,132,91,.14);animation:leafIn .8s ease both, flutter 4.8s ease-in-out infinite}
    .leaf.l1{left:51px;top:18px}.leaf.l2{left:24px;top:46px;animation-delay:.05s}.leaf.l3{left:82px;top:45px;animation-delay:.1s}.leaf.l4{left:58px;top:66px;background:#5dbb63;animation-delay:.15s}.leaf.l5{left:42px;top:0;background:#16845b;animation-delay:.2s}
    .fruit{position:absolute;width:10px;height:10px;border-radius:50%;background:#f0a11a;box-shadow:0 1px 4px rgba(179,107,0,.3);opacity:var(--fruit-opacity,.75);animation:fruitPop .7s ease both}
    .fruit.f1{left:55px;top:47px}.fruit.f2{left:92px;top:63px;animation-delay:.1s}.fruit.f3{left:72px;top:24px;animation-delay:.2s}.fruit.f4{left:41px;top:77px;animation-delay:.3s}
    .ground{position:absolute;left:18%;right:18%;bottom:28px;height:11px;border-radius:50%;background:rgba(22,132,91,.14)}
    .tree-caption{display:flex;justify-content:space-between;gap:8px;margin-top:10px;align-items:flex-start}
    .tree-caption .title{font-size:15px}
    @keyframes growTrunk{from{transform:scaleY(.2);opacity:.4}to{transform:scaleY(1);opacity:1}}
    @keyframes leafIn{from{transform:scale(.25);opacity:0}to{transform:scale(1);opacity:var(--leaf-opacity,.86)}}
    @keyframes fruitPop{from{transform:scale(.1);opacity:0}to{transform:scale(1);opacity:var(--fruit-opacity,.75)}}
    @keyframes flutter{0%,100%{transform:translateY(0) rotate(0deg)}50%{transform:translateY(-2px) rotate(2deg)}}
    @keyframes sway{0%,100%{filter:brightness(1)}50%{filter:brightness(1.08)}}
    .empty{padding:18px;text-align:center;color:var(--muted);font-size:13px}
    .copy-action{width:100%;margin-top:10px;border:1px solid var(--green);background:#fff;color:var(--green);border-radius:8px;padding:10px 12px;font-size:14px;font-weight:800;cursor:pointer}
    footer{margin-top:16px;color:var(--muted);font-size:12px;text-align:center}
    @media (min-width:560px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}.task-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
  </style>
</head>
<body>
  <main class="app">
    <header>
      <div class="eyebrow" id="role">托管 AI 看板</div>
      <h1 id="title">今日运营</h1>
      <div class="sub" id="subtitle">正在读取小优看板数据</div>
    </header>
    <div id="content" class="empty">加载中...</div>
    <footer>记录和任务处理请回企业微信直接说</footer>
  </main>
  <script>
    const TOKEN = "__TOKEN__" || new URLSearchParams(location.search).get("token") || "";
    const currentParams = new URLSearchParams(location.search);
    currentParams.set("token", TOKEN);
    const qs = "?" + currentParams.toString();
    const $ = (id) => document.getElementById(id);
    const publicText = (v) => String(v ?? "").replace(/Hermes/g, "小优");
    const safe = (v) => (v === undefined || v === null || v === "" ? "0" : publicText(v));
    const card = (label,value) => `<div class="card metric"><div class="value">${safe(value)}</div><div class="label">${label}</div></div>`;
    const progress = (v) => `<div class="bar"><span style="width:${Math.max(0,Math.min(100,Number(v)||0))}%"></span></div>`;
    const rows = (items, render) => `<div class="card list">${(items||[]).length ? items.map(render).join("") : '<div class="empty">暂无数据</div>'}</div>`;
    const esc = (v) => publicText(v).replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
    const short = (v, n=54) => { const t = String(v ?? "").replace(/\\s+/g," ").trim(); return t.length > n ? t.slice(0, n-1) + "…" : t; };
    const tabbar = (tabs, active) => `<div class="tabs">${tabs.map(t => `<button class="tab ${t.id===active?'active':''}" data-tab="${t.id}">${t.label}</button>`).join("")}</div>`;
    const fold = (id,title,summary,body,klass="") => `<div class="toggle-card ${klass}"><div class="toggle-head clickable" data-expand="${id}"><div><div class="title">${title}</div><div class="desc">${summary}</div></div><span class="pill">展开</span></div><div id="${id}" class="toggle-body hidden">${body}</div></div>`;
    const payrollLabel = (k) => ({
      base_attendance:"基础出勤",record_performance:"记录绩效",execution_performance:"执行绩效",safety_responsibility:"安全责任",admission_performance:"招生绩效",
      base_salary:"底薪",attendance_bonus:"全勤奖",service_closure:"服务闭环",personal_record_execution:"管理闭环",team_management:"团队管理",risk_service_closure:"风险/服务闭环"
    }[k] || k);
    const priorityClass = (p) => p === "S" ? "red" : (p === "A" || p === "B" ? "amber" : "");
    const qualityLabel = (v) => ({excellent:"优质记录", quality:"优质记录", valid:"有效记录", normal:"普通记录", regular:"普通记录", low:"简单记录", weak:"简单记录", simple:"简单记录", duplicate:"重复记录", invalid:"未计绩效", no_score:"未计绩效"}[safe(v)] || "普通记录");
    const eventLabel = (k) => ({
      attendance_present:"出勤",attendance_leave:"请假",attendance_absent:"缺勤",calendar_temporary_closed:"临时停课",calendar_statutory_holiday:"法定节假日",saturday_duty:"周六值班",admission_confirmed:"招生确认"
    }[k] || "工资事件");
    const reviewClass = (s) => s === "question" ? "red" : (s === "confirmed" ? "" : "amber");
    function growthTree(wc, s){
      const value = Number(wc.growth_value)||0;
      const weekRecords = Number(wc.records)||0;
      const coverage = Number(s.coverage_rate_7d)||0;
      const scale = Math.max(.48, Math.min(1.18, .48 + value / 150));
      const leafOpacity = Math.max(.35, Math.min(.96, .35 + coverage / 140 + weekRecords / 20));
      const fruitOpacity = Math.max(.15, Math.min(.9, weekRecords / 8));
      return `<div class="card tree-card" style="--tree-scale:${scale};--leaf-opacity:${leafOpacity};--fruit-opacity:${fruitOpacity}">
        <div class="tree-stage">
          <div class="sun"></div><div class="ground"></div>
          <div class="tree">
            <div class="trunk"></div><div class="branch"></div><div class="branch b2"></div>
            <div class="leaf l1"></div><div class="leaf l2"></div><div class="leaf l3"></div><div class="leaf l4"></div><div class="leaf l5"></div>
            <div class="fruit f1"></div><div class="fruit f2"></div><div class="fruit f3"></div><div class="fruit f4"></div>
          </div>
        </div>
        <div class="tree-caption"><div><div class="title">班级成长树 · Lv.${safe(wc.growth_level)}</div><div class="desc">记录会让树长高、长叶、结果；连续缺记录时树会变小变淡。</div></div><span class="pill">${safe(value)}</span></div>
      </div>`;
    }
    function hermesTree(tree){
      const score = Number(tree.score)||0;
      const scale = Math.max(.5, Math.min(1.16, .5 + score / 150));
      const leafOpacity = Math.max(.38, Math.min(.98, .38 + score / 130));
      const fruitOpacity = Math.max(.18, Math.min(.92, score / 105));
      return `<div class="card tree-card" style="--tree-scale:${scale};--leaf-opacity:${leafOpacity};--fruit-opacity:${fruitOpacity}">
        <div class="tree-stage">
          <div class="sun"></div><div class="ground"></div>
          <div class="tree">
            <div class="trunk"></div><div class="branch"></div><div class="branch b2"></div>
            <div class="leaf l1"></div><div class="leaf l2"></div><div class="leaf l3"></div><div class="leaf l4"></div><div class="leaf l5"></div>
            <div class="fruit f1"></div><div class="fruit f2"></div><div class="fruit f3"></div><div class="fruit f4"></div>
          </div>
        </div>
        <div class="tree-caption"><div><div class="title">小优协作树 · Lv.${safe(tree.level || 1)}</div><div class="desc">${esc(tree.growth_text || "你和小优配合得越及时、越具体，这棵树就长得越好。")}</div></div><span class="pill">${safe(score)}分</span></div>
      </div>`;
    }
    function teacher(data, me){
      const isSummerTeacher = me.program_role === "summer_teacher" || (data.program_scope || []).includes("summer_2026");
      const s = data.summary || {};
      const m = data.teacher_motivation || {};
      const wc = data.week_contribution || {};
      const pf = data.performance || {};
      const rules = pf.rules || {};
      const payroll = data.payroll || {};
      const companion = data.hermes_companion || {};
      const colleague = data.hermes_colleague_touch || {};
      const tree = data.hermes_performance_tree || {};
      $("role").textContent = me.display_name + (isSummerTeacher ? " · 暑假班老师" : " · 老师看板");
      $("title").textContent = isSummerTeacher ? "2026暑假班课节记录" : "小优老师成长助手";
      $("subtitle").textContent = isSummerTeacher ? safe(m.subtitle || "课节记录和孩子覆盖") : safe(companion.tone || "小优是你的同事，帮你省掉重复整理和不会写的压力。");
      const review = data.record_review || {};
      const reviewCounts = review.counts || {};
      const coach = data.daily_coach || {};
      const templates = data.record_templates || {};
      const tabs = isSummerTeacher
        ? [{id:"summer_today",label:"课节记录"},{id:"summer_focus",label:"重点孩子"},{id:"cover",label:"孩子覆盖"},{id:"summer_tasks",label:"待处理任务"}]
        : [{id:"today",label:"今天"},{id:"tree",label:"绩效树"},{id:"materials",label:"我的素材"}];
      const recordRow = (r,i,prefix="record") => `<div class="row clickable" data-expand="${prefix}-${i}"><div><div class="title">${esc(r.student_name)} · ${esc(r.record_type_label || r.required_bucket || '日常观察')}</div><div class="desc">${esc(r.summary)}</div><div id="${prefix}-${i}" class="detail hidden">入档：${r.valid?'是':'否'}；绩效：${r.payroll_eligible?'计入':'不计入'}；优质：${r.quality?'是':'否'}；类型：${esc(r.record_type_label || r.required_bucket || '日常观察')}<br>原因：${(r.payroll_reasons||[]).map(esc).join("；") || "符合当前规则"}<br>建议：${esc(r.improvement_tip || "")}<br>可用于：${(r.use_cases||[]).map(esc).join(" / ") || "日常观察"}</div></div><span class="pill ${r.payroll_eligible?'':'amber'}">${r.payroll_eligible?safe(r.points)+'分':'入档'}</span></div>`;
      const materialRows = (items) => rows(items, (r,i) => `<div class="row"><div><div class="title">${esc(r.student_name)}</div><div class="desc">${esc(r.summary)}</div><div class="chips">${(r.use_cases||[]).map(x=>`<span class="chip">${esc(x)}</span>`).join("")}</div></div><span class="pill">${safe(r.points)}分</span></div>`);
      const confirmedNumber = (v, suffix="") => Number(v||0) > 0 ? safe(v)+suffix : "本月暂未统计";
      const confirmedMoney = (v) => Number(v||0) > 0 ? "¥"+safe(v) : "待确认";
      const payrollDetails = (items) => `<div class="card list">${(items||[]).length ? items.map((item,i)=>`<div class="row clickable" data-expand="salary-${i}"><div><div class="title">${esc(item.label || payrollLabel(item.key))}</div><div class="desc">${esc(item.formula || "")}</div><div id="salary-${i}" class="detail hidden"><b>规则</b>：${esc(item.rule || "")}<br><b>依据</b>：${(item.evidence||[]).map(esc).join("；") || "暂无"}<br><b>状态</b>：${esc(item.status || "待确认")}</div></div><span class="pill ${item.status==='待接入指标'?'amber':''}">¥${safe(item.amount)}</span></div>`).join("") : '<div class="empty">暂无工资明细</div>'}</div>`;
      const requirementRows = (items) => rows(items, r => `<div class="row"><div><div class="title">${esc(r.student_name || '学生')}</div><div class="desc">${r.done?'已完成':esc(r.reason || '未完成')} · ${safe(r.evidence_count || 0)}/${safe(r.target_count || 1)} · ${esc(r.bucket || '')}</div></div><span class="pill ${r.done?'':'amber'}">${r.done?'完成':'缺项'}</span></div>`);
      const evidenceRows = (items) => rows(items, r => `<div class="row"><div><div class="title">${esc(r.student_name || '学生')} · ${esc(qualityLabel(r.quality_level))}</div><div class="desc">${esc(r.summary || '')}<br>${(r.reason_texts||[]).map(esc).join('；') || '符合当前规则'}</div></div><span class="pill ${Number(r.score)>0?'':'amber'}">${Number(r.score)>0?safe(r.score)+'分':'不计'}</span></div>`);
      const actionRows = (items) => rows(items, r => `<div class="row"><div><div class="title">${esc(r.title || '')}${r.student_name ? ' · '+esc(r.student_name) : ''}</div><div class="desc">${esc(r.reason || '')}<br>${esc(r.action_hint || '')}</div></div><span class="pill ${priorityClass(r.priority)}">${esc(r.priority || 'C')}</span></div>`);
      const templateRows = (items) => rows(items, r => `<div class="row"><div><div class="title">${esc(r.scenario)} · ${esc(r.title)}</div><div class="desc">${esc(r.template)}<br>必须包含：${(r.required_parts||[]).map(esc).join('、')}</div></div><span class="pill">模板</span></div>`);
      const render = (active="coach") => {
        const best = m.best_record || {};
        const panels = {
          summer_today: `<div class="hero"><div class="big">2026暑假班课节记录</div><div class="note">这里只展示当前暑假班的课堂与孩子表现记录。</div></div><section class="section"><h2>今日记录</h2>${rows(data.today_feedback, r => `<div class="row"><div><div class="title">${esc(r.student_name || '课堂记录')} · ${esc(r.record_type_label || '课节观察')}</div><div class="desc">${esc(r.summary || '')}</div></div><span class="pill">已记录</span></div>`)}</section>`,
          summer_focus: `<section class="section"><h2>重点孩子</h2>${rows(data.suggested_records || data.uncovered_students, r => `<div class="row"><div><div class="title">${esc(r.student_name)}</div><div class="desc">${esc(r.reason || '本周需要补充一次真实课堂观察')}</div></div><span class="pill amber">待关注</span></div>`)}</section>`,
          summer_tasks: `<section class="section"><h2>待处理任务</h2>${rows(data.open_tasks, r => `<div class="row"><div><div class="title">${esc(r.title || '暑假班任务')}</div><div class="desc">${esc(r.student_name || '')} · ${esc(r.status || '')}</div></div><span class="pill ${r.level==='S'?'red':''}">${esc(r.level || '')}</span></div>`)}</section>`,
          today: `<div class="hero"><div class="big">${esc(companion.headline || m.headline || "今天小优陪你一起把孩子情况整理清楚")}</div><div class="note">${esc((companion.helped_today||[]).join("；") || "你只要在企业微信顺手说真实情况，小优会帮你整理成记录、素材和绩效证据。")}</div>${best.student_name ? `<div class="quote"><b>今天最有价值的记录</b><br>${esc(best.student_name)}：${esc(best.summary)}<div class="chips">${(best.use_cases||[]).map(x=>`<span class="chip">${esc(x)}</span>`).join("")}</div></div>` : ""}</div><section class="section"><h2>${esc(colleague.headline || "小优同事一句话")}</h2><div class="card"><div class="title">${esc(colleague.message || companion.headline || "今天辛苦了，顺手说一句孩子最明显的表现就行，小优帮你整理。")}</div><div class="desc">${esc(colleague.boundary_note || "未授权前不会自动私聊老师，私人情绪聊天不默认进入老板绩效材料。")}</div></div></section>${hermesTree(tree)}<div class="grid">${card("本月预估","¥"+safe(tree.estimated_amount)+" / ¥"+safe(tree.cap_amount))}${card("协作树",safe(tree.score)+"/100")}${card("今日有效",s.today_valid_records)}${card("可用素材",safe(((data.materials||{}).quality_records||[]).length))}</div><section class="section"><h2>今天最轻松补一下</h2>${rows(companion.easy_next || data.suggested_records || [], r => `<div class="row"><div><div class="title">${esc(r.student_name || "保持今天的记录节奏")}</div><div class="desc">${esc(r.hint || r.reason || "补一句真实观察就能形成成长证据。")}</div></div><span class="pill amber">顺手补</span></div>`)}</section>`,
          tree: `<div class="hero"><div class="big">本月预估 ¥${safe(tree.estimated_amount)} / ¥${safe(tree.cap_amount)}</div><div class="note">${esc(tree.final_note || "这是本月预估和激励展示，最终绩效以老板确认制度为准。")}</div></div>${hermesTree(tree)}<div class="grid">${card("及时配合",safe((tree.components||{}).timely_response)+"/30")}${card("完整度",safe((tree.components||{}).answer_completeness)+"/30")}${card("孩子细节",safe((tree.components||{}).child_detail_quality)+"/20")}${card("素材价值",safe((tree.components||{}).material_value)+"/15")}</div><div class="card"><div class="title">怎么长得更好</div><div class="desc">${esc(tree.next_tip || "再补一句孩子具体表现和老师怎么处理，会更容易长出果实。")}</div></div>`,
          coach: `<div class="hero"><div class="big">${esc(coach.title || "今天最该做的3件事")}</div><div class="note">先处理缺记录、待闭环和家长沟通；工资预演会随着证据变化自动更新。</div></div><div class="grid task-grid">${card("预计记录绩效","¥"+safe((coach.payroll_preview||{}).estimated_amount))}${card("完成度",safe((coach.payroll_preview||{}).progress_rate)+"%")}${card("状态",esc((coach.payroll_preview||{}).status_text || ""))}</div><section class="section"><h2>今天最该做的3件事</h2>${actionRows(coach.top_actions || [])}</section>${fold("teacher-templates","记录模板库","需要参考时再展开，避免首页太长。",templateRows(templates.templates || []))}${fold("teacher-examples","优质记录参考","点开看哪些写法更容易形成有效证据。",rows(templates.quality_examples || [], (r,i)=>recordRow(r,i,"template-example")))}<div class="note">${esc(templates.principle || "模板只提醒结构，必须写真实观察。")}</div>`,
          review: `<div class="hero"><div class="big">本月记录明细</div><div class="note">每条记录都会保留入档和绩效判断，工资按计绩效记录、必达项和优质记录综合预估。</div></div><div class="grid">${card("计绩效",safe(reviewCounts.effective))}${card("不计绩效",safe(reviewCounts.not_payroll))}${card("优质记录",safe(reviewCounts.quality))}${card("入档不计薪",safe(reviewCounts.archived_only))}</div><section class="section"><h2>计绩效记录</h2>${rows(review.effective_records, (r,i)=>recordRow(r,i,"effective-record"))}</section><section class="section"><h2>不计绩效记录</h2>${rows(review.not_payroll_records, (r,i)=>recordRow(r,i,"not-payroll-record"))}</section><section class="section"><h2>优质记录示例</h2>${rows(review.quality_records, (r,i)=>recordRow(r,i,"quality-record"))}</section>`,
          week: `<section class="section"><h2>班级成长树</h2>${growthTree(wc,s)}<div class="card"><div class="title">距离下一级还差 ${safe(wc.points_to_next_level)} 成长值</div>${progress((Number(wc.growth_value)||0) % 20 * 5)}<div class="desc">${safe(wc.records)} 条记录 · ${safe(wc.quality_records)} 条优质证据 · 覆盖率 ${safe(s.coverage_rate_7d)}%</div></div></section>${fold("teacher-open-tasks","待处理任务",`${safe((data.open_tasks||[]).length)} 个任务，点开看明细。`,rows(data.open_tasks, r => `<div class="row"><div><div class="title">${esc(r.title)}</div><div class="desc">${esc(r.student_name || "未关联学生")} · ${esc(r.status)}</div></div><span class="pill ${r.level==='S'?'red':''}">${esc(r.level)}</span></div>`))}`,
          payroll: `<div class="hero"><div class="big">本月预计工资 ¥${safe(payroll.estimated_total)}</div><div class="note">${esc(payroll.position_label || "工资预估")} · ${esc((payroll.review||{}).label || "待本人确认")}。未接入或未确认的数据先显示“待确认”，避免把测试 0 当成扣款。</div></div><div class="grid">${card("出勤",confirmedNumber((payroll.attendance||{}).present_count,"次"))}${card("全勤", confirmedMoney((payroll.attendance||{}).attendance_bonus_actual))}${card("招生", Number((payroll.performance||{}).admission_count||0)>0 ? safe((payroll.performance||{}).admission_count)+"/"+safe((payroll.performance||{}).admission_target) : "本月暂未统计")}${card("确认状态", esc((payroll.review||{}).label || "待确认"))}</div>${fold("teacher-payroll-detail","工资明细","默认收起，点开看公式、依据和状态。",payrollDetails(payroll.breakdown_details))}${fold("teacher-payroll-events","本月事件",`${safe(((payroll.events||{}).recent||[]).length)} 条事件，点开核对。`,rows(((payroll.events||{}).recent||[]), e => `<div class="row"><div><div class="title">${esc(eventLabel(e.event_type))}</div><div class="desc">${esc(e.event_date)} · ${esc(e.reason || e.source_text || "")}</div></div><span class="pill ${e.confirmed?'':'amber'}">${e.confirmed?'已确认':'待确认'}</span></div>`))}`,
          performance: (() => { const mr = pf.monthly_requirements || {}; const req = mr.requirements || {}; const detail = `${fold("perf-missing","还缺哪些必达项",`${safe((mr.missing_required||[]).length)} 项缺口`,requirementRows(mr.missing_required || []),"status-amber")}${fold("perf-parent","家长沟通",`${safe((req.parent_communication||[]).filter(x=>x.done).length)} / ${safe((req.parent_communication||[]).length)} 已完成`,requirementRows(req.parent_communication || []))}${fold("perf-growth","成长观察",`${safe((req.growth_observation||[]).filter(x=>x.done).length)} / ${safe((req.growth_observation||[]).length)} 已完成`,requirementRows(req.growth_observation || []))}${fold("perf-excluded","不计绩效记录",`${safe((mr.excluded_records||[]).length)} 条，点开看原因。`,evidenceRows(mr.excluded_records || []),"status-amber")}${fold("perf-evidence","可计绩效证据",`${safe((mr.evidence_records||[]).length)} 条，点开看明细。`,evidenceRows(mr.evidence_records || []),"status-green")}`; return `<div class="hero"><div class="big">本月记录绩效预估</div><div class="note">${esc(pf.payroll_rule || '必达项为主，优质记录加分')}。最终发放以老板确认规则为准。</div></div><div class="split"><div class="card"><div class="title">预计可得</div><div class="money">¥${safe(pf.estimated_total_amount)} <small>/ ¥${safe((Number(rules.base_amount)||0)+(Number(pf.rank_bonus_amount)||0))}</small></div>${progress(pf.progress_rate)}<div class="desc">${esc(pf.status_text || "")}</div></div><div class="card"><div class="title">必达项完成</div><div class="money">${safe(mr.required_done)} / ${safe(mr.required_total)}</div><div class="desc">家长沟通、成长观察、风险闭环按学生逐项计算。</div></div></div><div class="grid section">${card("完成率",safe(mr.completion_percent || pf.progress_rate)+"%")}${card("质量分",safe(mr.quality_score)+"/"+safe(mr.quality_score_target))}${card("计绩效记录",safe(mr.payroll_eligible_count))}${card("不计绩效",safe((mr.excluded_records||[]).length))}</div>${detail}` })(),
          cover: `<section class="section"><h2>孩子覆盖</h2><div class="card"><div class="title">本周已关注 ${Math.max(0, Number(s.student_count||0)-Number((data.uncovered_students||[]).length||0))} / ${safe(s.student_count)} 个孩子</div>${progress(s.coverage_rate_7d)}<div class="desc">默认只看摘要；点开孩子可看补记原因。</div></div></section>${fold("teacher-child-focus","重点/待关注学生",`${safe(((data.suggested_records || data.uncovered_students)||[]).length)} 个孩子需要顺手关注。`,rows(data.suggested_records || data.uncovered_students, r => `<div class="row"><div><div class="title">${esc(r.student_name)}</div><div class="desc">${esc(r.reason || "本周还缺一条成长证据")} · 档案完整度 ${safe(r.completion)}%</div></div><span class="pill amber">可补记</span></div>`),"status-amber")}`,
          materials: `<div class="hero"><div class="big">素材库是小优帮你整理出来的记录价值</div><div class="note">优质记录会自动进入这里，后续可以变成成长报告、家长沟通话术和学生标签。没有素材时，说明最近还缺少可复用的记录。</div></div><section class="section"><h2>成长报告素材</h2>${materialRows((data.materials||{}).growth_reports)}</section><section class="section"><h2>家长沟通素材</h2>${materialRows((data.materials||{}).parent_communication)}</section><section class="section"><h2>学生标签素材</h2>${materialRows((data.materials||{}).student_tags)}</section>`
        };
        $("content").innerHTML = `${tabbar(tabs, active)}${panels[active]}`;
        document.querySelectorAll(".tab").forEach(btn => btn.onclick = () => render(btn.dataset.tab));
        document.querySelectorAll("[data-expand]").forEach(row => row.onclick = () => document.getElementById(row.dataset.expand)?.classList.toggle("hidden"));
      };
      render(isSummerTeacher ? "summer_today" : "today");
    }
    function boss(data, me){
      const s = data.summary || {};
      const isManager = me.role === "manager";
      const isSummerManager = isManager && (data.program_scope || []).includes("summer_2026");
      const selectedProgram = data.selected_program_id || "global";
      const isSummerView = selectedProgram === "summer_2026" || isSummerManager;
      const isRegularView = selectedProgram === "regular_tuoguan";
      const roleName = isManager ? "店长看板" : "老板看板";
      $("role").textContent = me.display_name + " · " + roleName;
      $("title").textContent = isSummerView ? "2026暑假班视图" : (isManager ? "小优店长助手" : "小优数字员工工作台");
      $("subtitle").textContent = isSummerView ? "只读取 summer_2026 · 课程覆盖、证据缺口和反馈进度" : (isManager ? "小优帮你盯现场、老师支持和待确认事项" : "小优的在岗状态、目标推进、经营价值和需要你确认的事");
      const risks = (data.risk || {});
      const task = (data.task_status || {});
      const performance = data.performance || {};
      const perfRules = performance.rules || {};
      const payroll = data.payroll || {};
      const learning = data.learning || {};
      const projectOpportunities = data.project_opportunities || {data_state:"empty",items:[]};
      const brief = data.daily_brief || {};
      const hermes = data.hermes_employee || {};
      const assistant = data.hermes_assistant || {};
      const presence = hermes.relationship_presence || {};
      const focus = data.business_focus || brief.business_focus || {};
      const dq = data.data_quality || {};
      const renewal = data.renewal_funnel || {};
      const summer = data.summer_import || {};
      const summerLessons = data.summer_lessons || {};
      const summerReports = data.summer_reports || {};
      const summerCoverage = data.summer_course_coverage || {};
      const programViews = data.program_views || {};
      const ruleCenter = data.rule_center || {};
      const tabs = isManager
        ? (isSummerManager ? [{id:"assistant",label:"小优助手"},{id:"brief",label:"今日运行"},{id:"summer_lessons",label:"课节记录"},{id:"summer_coverage",label:"孩子覆盖"},{id:"summer_pending",label:"待处理事项"},{id:"summer_safety",label:"安全关注"},{id:"summer_feedback",label:"反馈进度"}] : [{id:"assistant",label:"小优助手"},{id:"brief",label:"店内状态"},{id:"exec",label:"老师支持"},{id:"closure",label:"遗留任务"}])
        : (isSummerView ? [{id:"brief",label:"暑假总览"},{id:"summer_lessons",label:"课节记录"},{id:"summer_coverage",label:"课程覆盖"},{id:"summer_pending",label:"待处理"},{id:"summer_feedback",label:"周报进度"}] : [{id:"hermes",label:"今日"},{id:"exec",label:"目标"},{id:"risk_ops",label:"风险"},{id:"payroll",label:"工资"},{id:"business",label:"经营"},{id:"family",label:"资料"},{id:"rules_ops",label:"规则"}]);
      const issueRows = (items, label="处理") => rows(items, r => `<div class="row"><div><div class="title">${esc(r.student_name || r.id || '数据项')}</div><div class="desc">${esc(r.summary || r.status || r.teacher || '')}<br>${esc(r.time || '')}</div></div><span class="pill amber">${esc(label)}</span></div>`);
      const closureRows = (items) => rows(items, (r,i) => `<div class="row clickable" data-expand="closure-proof-${i}"><div><div class="title">${esc(r.task_title || '任务')} ${r.student_name ? '· '+esc(r.student_name) : ''}</div><div class="desc">${esc(r.by || '')} · ${esc(r.at || '')}<br>${esc((r.text || '').slice(0,42))}${String(r.text||'').length>42?'…':''}</div><div id="closure-proof-${i}" class="detail hidden"><b>完整证据</b>：${esc(r.text || '暂无')}<br><b>来源</b>：${esc(r.source_label || '任务')}<br>${(r.missing_fields||[]).length ? '<b>仍缺</b>：'+(r.missing_fields||[]).map(esc).join('、') : '闭环字段：已记录'}</div></div><span class="pill ${r.level==='S'?'red':(r.level==='A'?'amber':'')}">${esc(r.action || r.level || '')}</span></div>`);
      const closureGapRows = (items) => rows(items, r => `<div class="row"><div><div class="title">${esc(r.task_title || '任务')} ${r.student_name ? '· '+esc(r.student_name) : ''}</div><div class="desc">${esc(r.source_label || '任务')} · 负责人：${esc(r.assignee_userid || '')} · 状态：${esc(r.status || '')}<br>缺：${(r.missing_fields||[]).map(esc).join('、') || '暂无'}<br>${esc(r.evidence_summary || '')}</div></div><span class="pill ${r.level==='S'?'red':'amber'}">${esc(r.level || '')}</span></div>`);
      const managerRows = (items) => rows(items, (r,i) => `<div class="row clickable" data-expand="manager-exec-${i}"><div><div class="title">${esc(r.name || r.user_id)} · 店长执行</div><div class="desc">管理闭环 ${safe(r.management_closure_rate)}% · 团队管理 ${safe(r.team_management_rate)}% · 风险闭环 ${safe(r.risk_service_rate)}%</div><div id="manager-exec-${i}" class="detail hidden"><div>负责老师：${safe(r.team_teacher_count)} 人</div><div>团队任务：${safe(r.team_task_closed)}/${safe(r.team_task_total)}</div><div>风险任务：${safe(r.team_risk_task_closed)}/${safe(r.team_risk_task_total)}</div><div>记录缺项：${safe(r.team_record_gap_count)} · 低质记录：${safe(r.team_low_quality_count)}</div><div>说明：这里看店长有没有督促老师、处理风险、推动任务闭环；工资结算仍由老板端最终确认。</div></div></div><span class="pill ${Number(r.risk_service_rate||0)<100 || Number(r.team_management_rate||0)<100 ? 'amber' : ''}">${Number(r.management_closure_rate||0) >= 100 ? '正常' : '待闭环'}</span></div>`);
      const summerIssueRows = (items, label="处理") => rows(items, r => { const st = r.import_student || r; const att = r.items || ((st.special_attention||{}).safety || []); return `<div class="row"><div><div class="title">${esc(st.student_name || r.student_name || r.id || "学生")}</div><div class="desc">${esc(st.grade || "")} ${esc(st.summer_group || "")}${att.length ? "<br>"+att.map(esc).join("、") : ""}${r.reason ? "<br>"+esc(r.reason) : ""}</div></div><span class="pill amber">${esc(label)}</span></div>` });
      const programSelector = () => !isManager ? `<div class="summary-strip"><b>项目视角</b><br><a class="pill ${selectedProgram==='global'?'active':''}" href="${programViewLink('global')}">全局</a> ${Object.values(programViews).map(p=>`<a class="pill ${selectedProgram===p.program_id?'active':''}" href="${programViewLink(p.program_id)}">${esc(p.program_name)} · ${safe(p.student_count)}人</a>`).join(' ')}</div>` : "";
      const programViewLink = (id) => { const p = new URLSearchParams(location.search); p.set("program_id", id); return location.pathname+"?"+p.toString(); };
      const hermesReportLine = () => {
        const reports = hermes.daily_reports || {};
        const morning = reports.morning || {};
        const evening = reports.evening || {};
        return `早报：${esc(morning.message || "未生成")} ${morning.sent_at ? "· "+esc(morning.sent_at) : ""}<br>晚报：${esc(evening.message || "未生成")} ${evening.sent_at ? "· "+esc(evening.sent_at) : ""}`;
      };
      const workItemRows = (items) => rows(items, r => `<div class="row"><div><div class="title">${esc(r.title || "小优工作事项")}</div><div class="desc">状态：${esc(r.status || "active")} · 阶段：${esc(r.phase || "持续推进")}<br>${esc(r.blocker || r.next_action || "等待新事实")} ${r.next_attention_at ? "<br>下一关注："+esc(r.next_attention_at) : ""}</div></div><span class="pill ${r.status==='waiting'?'amber':''}">${esc(r.status || "active")}</span></div>`);
      const questionRows = (items) => rows(items, r => `<div class="row"><div><div class="title">${esc(r.question || "待确认事项")}</div><div class="desc">${esc(r.focus_key || "")}</div></div><span class="pill amber">${esc(r.status || "open")}</span></div>`);
      const valueRows = (items) => rows(items, r => `<div class="row"><div><div class="title">${esc(r.subject || "价值推进")}</div><div class="desc">发现：${esc(r.discovered || "已记录观察")}<br>小优：${esc(r.hermes_action || "整理证据")}<br>结果：${esc(r.outcome || "等待真实结果")}</div></div><span class="pill">价值</span></div>`);
      const opportunityStatus = (status) => ({evidence_ready:"证据已就绪",decision_pending:"待老板决定",validation_approved:"已批准验证",validating:"验证中",proposal_ready:"方案已就绪",accepted:"已确认项目"}[status] || "内部观察");
      const opportunitySection = () => {
        const items = (projectOpportunities.items || []).slice(0,3);
        if (!items.length) return `<section class="section"><h2>新项目机会</h2><div class="card"><div class="empty">当前没有达到展示门槛的新项目机会，弱信号仍在内部观察。</div></div></section>`;
        return `<section class="section"><h2>新项目机会</h2><div class="card list">${items.map((r,i) => {
          const prompt = encodeURIComponent(`请展开新项目机会“${r.title || "未命名机会"}”的证据、待验证事实和验证方案。`);
          const missing = (r.missing_facts || []).map(esc).join("；") || "当前无新增事实缺口";
          const plan = r.validation_plan || {};
          return `<div class="row clickable" data-expand="project-opportunity-${i}"><div><div class="title">${esc(r.title || "新项目机会")}</div><div class="desc">${safe(r.affected_student_count)}名学生 · ${safe(r.teacher_count)}名老师 · ${esc(r.evidence_period || "证据周期待核验")}<br>内部证据 · 小优判断 · ${esc(opportunityStatus(r.status))}</div><div id="project-opportunity-${i}" class="detail hidden"><b>共同问题</b>：${esc(r.hypothesis || "等待小优补充判断")}<br><b>小优判断</b>：${esc(r.xiaoyou_judgement || "已达到证据门槛，仍需验证") }<br><b>当前还缺</b>：${missing}<br><b>低成本验证</b>：${esc(plan.method || "等待形成完整验证方案")}<br><b>边界</b>：${esc(r.boundary || "待验证候选，不等于正式项目")}<button class="copy-action" type="button" data-copy-prompt="${prompt}">复制问题，回企业微信问小优</button></div></div><span class="pill ${r.status==='decision_pending'?'amber':''}">${esc(opportunityStatus(r.status))}</span></div>`;
        }).join("")}</div></section>`;
      };
      const relationshipRows = (items) => rows(items, r => `<div class="row"><div><div class="title">${esc(r.message || "小优主动候选")}</div><div class="desc">${esc(r.reason || "")}<br>${esc(r.value || "")}</div></div><span class="pill ${r.external_send_allowed?'':'amber'}">${esc(r.status || "candidate")}</span></div>`);
      const bossDecisionRows = (items) => {
        const list = (items || []).slice(0, 2);
        return `<div class="card decision-list">${list.length ? list.map(r => `<div class="row"><div><div class="title">${esc(short(r.question || "待确认事项", 42))}</div><div class="desc">${esc(short(r.focus_key || "小优会在确认后继续推进", 54))}</div></div><span class="pill amber">${esc(r.status || "待确认")}</span></div>`).join("") : '<div class="empty">现在没有需要你立即拍板的事项</div>'}</div>`;
      };
      const bossBriefCard = (label, value) => `<div class="brief-card"><div class="label">${esc(label)}</div><div class="value">${esc(short(value, 32))}</div></div>`;
      const bossHermesPanel = () => {
        const focusBrief = hermes.focus_brief || {};
        const todayStatus = hermes.today_status || {};
        const riskBrief = hermes.risk_summary || {};
        const reportText = `早报 ${todayStatus.morning_report || "未生成"} · 晚报 ${todayStatus.evening_report || "未生成"}`;
        const focusTitle = focusBrief.title || "暂无活跃目标";
        const blocker = focusBrief.blocker || "暂无明确卡点";
        const nextAction = focusBrief.next_action || "继续巡检记录覆盖、风险信号和目标缺口";
        return `<div class="boss-brief">
          <div class="boss-focus">
            <div class="kicker">小优今日在岗判断</div>
            <div class="headline">${esc(short(focusTitle, 56))}</div>
            <div class="meta">阶段：${esc(short(focusBrief.phase || "持续推进", 42))}<br>卡点：${esc(short(blocker, 72))}<br>下一步：${esc(short(nextAction, 72))}${focusBrief.next_attention_at ? "<br>下次关注："+esc(focusBrief.next_attention_at) : ""}</div>
          </div>
          <div class="brief-grid">
            ${bossBriefCard("待我确认", `${safe((hermes.decision_brief||[]).length)} 项`)}
            ${bossBriefCard("日报状态", reportText)}
            ${bossBriefCard("安全风险", `${safe(riskBrief.safety_count || 0)} 项`)}
            ${bossBriefCard("老师支持", `${safe(riskBrief.teacher_support_count || 0)} 项`)}
          </div>
          <section class="section"><h2>需要我拍板</h2>${bossDecisionRows(hermes.decision_brief || hermes.open_questions || [])}</section>
          <section class="section"><h2>小优主动存在感</h2>${relationshipRows((presence.items || []).slice(0,3))}</section>
          <section class="section"><h2>经营价值</h2>${valueRows(hermes.value_entries || [])}</section>
          ${fold("hermes-more-work","后台跟进事项",`${safe((hermes.work_items||[]).length)} 个正在跟进${Number(hermes.other_work_count||0)>0 ? "，另有 "+safe(hermes.other_work_count)+" 个已收起" : ""}。`, workItemRows(hermes.work_items || []),"status-blue")}
          ${programSelector()}
        </div>`;
      };
      const suggestionRows = (items) => rows(items, r => `<div class="row"><div><div class="title">${esc(r.title || "现场建议")}</div><div class="desc">${esc(r.detail || "")}</div></div><span class="pill">协助</span></div>`);
      const moneyLine = (label, value) => `<div class="amount-line"><span>${esc(label)}</span><b>¥${safe(value)}</b></div>`;
      const detailByKey = (person, key) => (person.breakdown_details || []).find(d => d.key === key) || {};
      const amountByKey = (person, key) => safe(((person.breakdown || {})[key] ?? (detailByKey(person,key).amount ?? 0)));
      const payrollSimpleReason = (person) => {
        const rp = person.performance || {};
        if(person.position === "manager"){
          const bits = [];
          bits.push(`管理闭环 ${safe(rp.management_closure_rate)}%`);
          bits.push(`团队 ${safe(rp.team_teacher_count)} 人`);
          bits.push(`任务 ${safe(rp.team_task_closed)}/${safe(rp.team_task_total)}`);
          bits.push(`风险 ${safe(rp.team_risk_task_closed)}/${safe(rp.team_risk_task_total)}`);
          if(Number(rp.team_record_gap_count||0)) bits.push(`缺项 ${safe(rp.team_record_gap_count)}`);
          if(Number(rp.team_low_quality_count||0)) bits.push(`低质 ${safe(rp.team_low_quality_count)}`);
          return bits.join(" · ");
        }
        const missing = (rp.record_missing_required || []).length;
        const excluded = (rp.record_excluded_records || []).length;
        const tasks = Number(rp.task_total || 0);
        const risks = Number(rp.risk_task_total || 0);
        const bits = [];
        if(person.position === "part_time" && Number(((person.attendance||{}).present_count)||0) === 0) bits.push("出勤待确认");
        bits.push(`记录完成 ${safe(rp.record_rate)}%`);
        bits.push(tasks ? `任务 ${safe(rp.task_closed)}/${safe(rp.task_total)}` : "无任务");
        bits.push(risks ? `风险 ${safe(rp.risk_task_closed)}/${safe(rp.risk_task_total)}` : "无风险扣减");
        if(missing) bits.push(`缺项 ${missing}`);
        if(excluded) bits.push(`不计 ${excluded}`);
        return bits.join(" · ");
      };
      const payrollAction = (person) => {
        const rp = person.performance || {};
        if(person.position === "manager"){
          if(Number(rp.team_teacher_count||0) === 0) return "缺团队";
          if(Number(rp.management_closure_rate||0) < 100) return "看管理闭环";
          if(Number(rp.team_management_rate||0) < 100) return "看团队管理";
          if(Number(rp.manager_risk_service_rate||0) < 100) return "看风险闭环";
          return "正常";
        }
        if(person.position === "part_time" && Number(((person.attendance||{}).present_count)||0) === 0) return "缺出勤";
        if((person.events||{}).pending_count) return "先确认工资事件";
        if((rp.record_missing_required||[]).length) return "看缺项";
        if((rp.record_excluded_records||[]).length) return "抽查记录";
        if(Number(rp.task_total||0) === 0 && Number(rp.risk_task_total||0) === 0) return "无需处理";
        if(Number(rp.execution_rate||100) < 100 || Number(rp.service_closure_rate||100) < 100) return "看任务闭环";
        return "正常";
      };
      const payrollActivePeople = () => (payroll.teachers || []);
      const payrollPersonRow = (r,i) => {
        const rp = r.performance || {};
        const isManager = r.position === "manager";
        const recordKey = isManager ? "personal_record_execution" : "record_performance";
        const executionKey = isManager ? "team_management" : "execution_performance";
        const serviceKey = isManager ? "risk_service_closure" : (r.position === "part_time" ? "safety_responsibility" : "service_closure");
        const missing = (rp.record_missing_required || []).slice(0,5).map(x=>`${x.student_name || "学生"}：${x.reason || "缺项"}`);
        const excluded = (rp.record_excluded_records || []).slice(0,5).map(x=>`${x.student_name || "学生"}：${(x.reason_texts||[]).join("/") || "不计绩效"}`);
        const taskEvidence = (rp.task_evidence_records || []).slice(0,5).map(x=>`${x.source_label || "任务"} · ${x.task_title || "任务"}：${x.action || ""} ${x.at || ""}`);
        const details = [
          moneyLine("底薪/出勤", Number(amountByKey(r,"base_salary")) + Number(amountByKey(r,"base_attendance"))),
          moneyLine("全勤/值班", Number(amountByKey(r,"attendance_bonus")) + Number(amountByKey(r,"saturday_duty"))),
          moneyLine(isManager ? "管理闭环" : "记录绩效", amountByKey(r,recordKey)),
          moneyLine(isManager ? "团队管理" : "执行绩效", amountByKey(r,executionKey)),
          moneyLine(isManager ? "风险/服务闭环" : "服务/安全闭环", amountByKey(r,serviceKey)),
          moneyLine("招生绩效", amountByKey(r,"admission_performance")),
        ].join("");
        return `<div class="row clickable" data-expand="payroll-${i}"><div><div class="title">${esc(r.name)} · ${esc(r.position_label)} · ¥${safe(r.estimated_total)}</div><div class="desc">${esc(payrollSimpleReason(r))}</div><div id="${'payroll-'+i}" class="detail hidden"><div class="amount-list">${details}</div><br><b>任务口径</b>：老板安排 / 系统风险 / 记录触发。无任务则不扣。<br><b>闭环证据</b>：${taskEvidence.map(esc).join("；") || "暂无"}<br><b>记录缺项</b>：${missing.map(esc).join("；") || "暂无"}<br><b>不计记录</b>：${excluded.map(esc).join("；") || "暂无"}<br><b>确认</b>：${esc((r.review||{}).label || "待确认")}</div></div><span class="pill ${payrollAction(r)==='正常'?'':'amber'}">${esc(payrollAction(r))}</span></div>`;
      };
      const studentList = (items, empty="暂无") => (items||[]).length ? `${(items||[]).slice(0,8).map(esc).join("、")}${(items||[]).length>8 ? ` 等${safe((items||[]).length)}人` : ""}` : empty;
      const courseCoverageCards = () => (summerCoverage.courses||[]).map((c,i) => {
        const summary = c.has_course ? `应覆盖 ${safe(c.expected_count)} · 整体 ${safe(c.overall_count)} · 个别 ${safe(c.individual_count)} · 未覆盖 ${safe(c.missing_count)}` : "今日无课，不计漏记";
        const body = c.has_course ? `<div class="detail"><b>负责老师</b>：${studentList(c.teacher_names,'待安排')}<br><b>个别记录</b>：${studentList(c.individual_students)}<br><b>仅整体覆盖</b>：${studentList(c.overall_students)}<br><b>未覆盖</b>：${studentList(c.missing_students)}<br><b>缺勤排除</b>：${studentList(c.absent_students)}<br>${c.needs_record?'<b>建议</b>：课后补一段整体情况，并点名2-3名有明显表现的孩子。':'本课记录覆盖正常。'}</div>` : `<div class="empty">今日无课，无需补记。</div>`;
        return `<div class="course-card ${c.needs_record?'course-abnormal':'course-normal'}">${fold(`course-${i}`,`${esc(c.course)} · ${c.has_course?'今日有课':'今日无课'}`,summary,body,c.needs_record?'status-amber':'status-green')}</div>`;
      }).join("") || '<div class="card empty">今日没有已配置课程，待课程表确认后显示覆盖。</div>';
      const render = (active="brief") => {
        const panels = {
          hermes: bossHermesPanel(),
          assistant: `<div class="cockpit"><div class="hero"><div class="big">${esc((assistant.headline) || "小优今天帮你盯现场执行和老师支持。")}</div><div class="note">这里先给店长看最该处理的现场细节，不展示老板私密确认和全局工资。</div></div><div class="grid">${card("今日记录",safe(((assistant.store_status||{}).today_records)))}${card("7日覆盖",safe(((assistant.store_status||{}).coverage_rate_7d))+"%")}${card("未闭环",safe(((assistant.store_status||{}).open_task_count)))}${card("安全关注",safe(((assistant.store_status||{}).safety_count)))}</div></div><section class="section"><h2>小优建议先看</h2>${suggestionRows(assistant.top_suggestions || [])}</section><section class="section"><h2>现场同事助手候选</h2>${relationshipRows((((assistant.relationship_support||{}).items)||[]).slice(0,3))}</section><section class="section"><h2>待确认事项</h2>${questionRows(assistant.pending_confirmations || [])}</section>${fold("assistant-goal-focus","老板目标落地",esc(((assistant.goal_focus||{}).title) || "暂无活跃目标"), workItemRows((assistant.goal_focus&&assistant.goal_focus.title)?[assistant.goal_focus]:[]),"status-blue")}`,
          brief: `<div class="cockpit"><div class="hero"><div class="big">${isSummerView?'今天先看：课程覆盖、证据缺口、待审核反馈':(isManager?'托管班已冻结，只读查看历史':'今天先看三件事：风险、工资、续费')}</div><div class="note">${isSummerView?'这是2026暑假班工作台，只展示当前项目，不混入托管班。':(isManager?esc(data.readonly_hint||'托管班历史数据保留。'):'这是老板端经营驾驶舱，只展示需要决策的信息；明细点开再看证据。')}</div></div>${programSelector()}${focus.active ? `<div class="summary-strip"><b>当前经营重点</b><br>${esc(focus.summary || "当前使用系统默认经营节奏")}</div>` : ""}<div class="grid">${isSummerView ? `${card('暑假学生',safe(s.student_count || (summer.summary||{}).total_students))}${card('今日有课',safe((summerCoverage.summary||{}).scheduled_course_count))}${card('缺记录科目',safe((summerCoverage.summary||{}).missing_course_count))}${card('本周期记录',safe(s.week_records))}${card('待审核周报',safe(summerReports.draft_count))}${card('证据不足学生',safe((summerCoverage.summary||{}).evidence_gap_student_count))}` : (brief.cards||[]).map(c=>card(c.label,c.value)).join("")}</div></div><section class="section"><h2>${isSummerView?'今日重点提醒':'今日优先处理'}</h2>${rows(brief.top_actions, r => `<div class="row"><div><div class="title">${esc(r.title)}</div><div class="desc">${esc(r.detail)}${r.source ? '<br>'+esc(r.source) : ''}</div></div><span class="pill ${priorityClass(r.priority)}">${esc(r.priority || 'C')}</span></div>`)}</section>`,
          summer_lessons: `<div class="grid">${card("课节记录",safe(summerLessons.lesson_record_count))}${card("每日总评",safe(summerLessons.daily_summary_count))}</div><section class="section"><h2>最近课节</h2>${rows(summerLessons.recent_records, r => `<div class="row"><div><div class="title">${r.summer_group?esc(r.summer_group)+' · ':''}${esc(r.course||'课程')} · ${esc(r.lesson||'')}</div><div class="desc">${esc(r.teacher_name||r.teacher_userid||'')}<br>${esc(r.class_overall||'')}</div></div><span class="pill">${esc(r.record_kind==='daily_summary'?'总评':'课节')}</span></div>`)}</section>`,
          summer_coverage: `<div class="hero"><div class="big">今日课程记录覆盖</div><div class="note">未覆盖、整体覆盖、个别记录分开计算；缺勤和今日无课不算漏记。</div></div><div class="grid">${card("今日有课",safe((summerCoverage.summary||{}).scheduled_course_count))}${card("已完整记录",safe((summerCoverage.summary||{}).recorded_course_count))}${card("缺记录科目",safe((summerCoverage.summary||{}).missing_course_count))}${card("证据不足学生",safe((summerCoverage.summary||{}).evidence_gap_student_count))}</div><button class="tab" id="coverage-anomaly-toggle" type="button">只看异常</button><section class="section"><h2>按科目查看</h2>${courseCoverageCards()}</section>${fold("summer-evidence-gaps","周报证据不足学生",`${safe((summerCoverage.evidence_gap_students||[]).length)} 人需要补个别观察。`,rows(summerCoverage.evidence_gap_students, r => `<div class="row"><div><div class="title">${esc(r.student_name)}</div><div class="desc">整体覆盖 ${safe(r.overall_count)} · 个别记录 ${safe(r.individual_count)}<br>${esc(r.reason||'')}</div></div><span class="pill amber">补观察</span></div>`),"status-amber")}`,
          summer_pending: `<div class="grid">${card("未闭环任务",safe(s.open_task_count))}${card("电话待补",safe((summer.summary||{}).phone_pending_count))}${card("年级待补",safe((summer.summary||{}).grade_pending_count))}${card("时段待补",safe((summer.summary||{}).attendance_mode_pending_count))}${card("疑似重复",safe((summer.summary||{}).duplicate_pending_count))}${card("未入库记录",safe((summer.summary||{}).unknown_record_count))}</div>${fold("summer-pending-import","名单待处理","电话、年级、上课时段、重复和未入库学生需要确认。",`${summerIssueRows(summer.phone_pending_students||[],"补电话")}${summerIssueRows(summer.grade_pending_students||[],"补年级")}${summerIssueRows(summer.attendance_mode_pending_students||[],"补时段")}${summerIssueRows(summer.duplicate_pending_students||[],"确认")}${summerIssueRows(summer.unknown_record_students||[],"补录")}`,"status-amber")}${fold("summer-pending-tasks","任务待处理",`${safe((task.closure_evidence||{}).missing_evidence_count)} 个任务缺证据。`,closureGapRows((task.closure_evidence||{}).missing_evidence_tasks||[]),"status-amber")}`,
          summer_safety: `<div class="hero"><div class="big">安全关注 ${safe((summer.summary||{}).safety_attention_count)} 人</div><div class="note">餐食、活动、接送和身体状态相关提醒优先处理。</div></div>${summerIssueRows(summer.safety_attention_students||[],"安全关注")}`,
          summer_feedback: `<div class="grid">${card("待审核草稿",safe(summerReports.draft_count))}${card("已审核",safe(summerReports.approved_count))}${card("已分享",safe(summerReports.shared_count))}${card("待补观察",safe(summerReports.needs_observation_count))}</div><section class="section"><h2>反馈批次</h2>${rows(summerReports.recent_batches, r => `<div class="row"><div><div class="title">${esc(r.report_type==='graduation'?'结业汇报':'周反馈')}</div><div class="desc">${esc(r.created_at||'')} · ${safe((r.report_ids||[]).length)}份草稿 · 待补${safe((r.needs_observation||[]).length)}人</div></div><span class="pill amber">待审核</span></div>`)}</section>`,
          regular_students: `<div class="hero"><div class="big">托管班学生 ${safe(s.student_count)} 人</div><div class="note">当前为冻结只读视图。</div></div>${rows(data.student_roster, r => `<div class="row"><div><div class="title">${esc(r.student_name)}</div><div class="desc">负责人：${esc(r.teacher_userid||'未分配')}</div></div><span class="pill">历史</span></div>`)}`,
          regular_history: `<section class="section"><h2>近期历史记录</h2>${rows([...(data.recent_progress||[]),...(data.recent_anomalies||[])], r => `<div class="row"><div><div class="title">${esc(r.student_name||'学生')}</div><div class="desc">${esc(r.summary||'')}</div></div><span class="pill">只读</span></div>`)}</section>`,
          exec: `<section class="section"><h2>店长执行情况</h2>${managerRows((data.execution||{}).managers || [])}</section><section class="section"><h2>老师执行</h2>${rows((data.execution||{}).teachers, (r,i) => `<div class="row clickable" data-expand="teacher-${i}"><div><div class="title">${esc(r.teacher_name || r.teacher_id)}</div><div class="desc">${safe(r.week_records)} 条记录 · ${safe(r.student_count)} 名学生 · ${safe(r.open_task_count)} 个任务${progress(r.coverage_rate_7d)}</div><div id="teacher-${i}" class="detail hidden"><div>未覆盖：${(r.stale_students||[]).slice(0,10).map(esc).join("、") || "暂无"}</div><div>缺成长报告：${(r.missing_growth_report_students||[]).slice(0,10).map(esc).join("、") || "暂无"}</div></div></div><span class="pill">${safe(r.coverage_rate_7d)}%</span></div>`)}</section>`,
          closure: `<div class="hero"><div class="big">今日已完成 ${safe(((task.closure_evidence||{}).recent_events||[]).length)} 个任务</div><div class="note">仍有 ${safe((task.closure_evidence||{}).missing_evidence_count)} 个任务缺证据。默认只看摘要，点开查看完整闭环。</div></div><div class="grid">${card("最近闭环",safe(((task.closure_evidence||{}).recent_events||[]).length))}${card("缺证据任务",safe((task.closure_evidence||{}).missing_evidence_count))}</div>${fold("boss-closure-proof","最近闭环证据",`${safe(((task.closure_evidence||{}).recent_events||[]).length)} 条，点开看完整证据。`,closureRows((task.closure_evidence||{}).recent_events || []),"status-green")}${fold("boss-closure-gap","仍缺证据的任务",`${safe((task.closure_evidence||{}).missing_evidence_count)} 个任务需要补证据。`,closureGapRows((task.closure_evidence||{}).missing_evidence_tasks || []),"status-amber")}`,
          payroll: `<div class="hero"><div class="big">本月工资预估 ¥${safe((payroll.summary||{}).estimated_total)}</div><div class="note">${safe(payroll.month)} · ${safe(payrollActivePeople().length)} 人 · 已确认 ${safe((payroll.summary||{}).teacher_confirmed_count)} 人 · 有疑问 ${safe((payroll.summary||{}).teacher_question_count)} 人 · 结算：${(payroll.summary||{}).settlement_locked ? "已锁定" : "未生成"}</div></div><div class="card"><div class="title">任务来源</div><div class="desc">老板安排 / 系统风险 / 记录触发。无任务不扣；有安全、投诉、续费风险必须闭环。</div></div>${(payroll.summary||{}).settlement_locked ? `<div class="card"><div class="title">结算已锁定</div><div class="desc">生成时间：${esc((payroll.settlement||{}).created_at || "")} · 锁定总额 ¥${safe((payroll.settlement||{}).estimated_total)}</div></div>` : ""}<section class="section"><h2>工资总表</h2>${rows(payrollActivePeople(), payrollPersonRow)}</section>`,
          risk_ops: `<div class="hero"><div class="big">风险与异常</div><div class="note">今日暂无 S 级未闭环风险时，重点看 A 级任务和长期无记录学生。</div></div><section class="section"><h2>安全与关系</h2>${rows(task.safety_risks, r => `<div class="row"><div><div class="title">${esc(r.title)}</div><div class="desc">${esc(r.student_name || "未关联学生")} · ${esc(r.status)}</div></div><span class="pill red">${esc(r.level)}</span></div>`)}</section>${fold("boss-long-unrecorded","长期无记录",`${safe((risks.long_unrecorded_students||[]).length)} 个学生需要关注。`,rows(risks.long_unrecorded_students, r => `<div class="row"><div><div class="title">${esc(r)}</div><div class="desc">近 7 天暂无记录</div></div><span class="pill amber">关注</span></div>`),"status-amber")}`,
          family: `<div class="hero"><div class="big">学生与家长重点</div><div class="note">续费漏斗和暑假班导入状态放在这里；发送家长内容前必须老师或老板确认。</div></div><div class="grid">${card("暑假总数",safe(((summer.summary||{}).total_students)))}${card("电话待补",safe(((summer.summary||{}).phone_pending_count)))}${card("年级待补",safe(((summer.summary||{}).grade_pending_count)))}${card("时段待补",safe(((summer.summary||{}).attendance_mode_pending_count)))}${card("疑似重复",safe(((summer.summary||{}).duplicate_pending_count)))}${card("安全关注",safe(((summer.summary||{}).safety_attention_count)))}</div>${fold("summer-safety","暑假班安全关注",`${safe(((summer.summary||{}).safety_attention_count))} 人，店长优先看。`,summerIssueRows(summer.safety_attention_students || [], "安全关注"),"status-red")}${fold("summer-import-issues","暑假班导入问题",`电话待补 ${safe(((summer.summary||{}).phone_pending_count))} · 年级待补 ${safe(((summer.summary||{}).grade_pending_count))} · 时段待补 ${safe(((summer.summary||{}).attendance_mode_pending_count))} · 疑似重复 ${safe(((summer.summary||{}).duplicate_pending_count))} · 未入库记录 ${safe(((summer.summary||{}).unknown_record_count))}`,`${summerIssueRows(summer.phone_pending_students || [], "补电话")}${summerIssueRows(summer.grade_pending_students || [], "补年级")}${summerIssueRows(summer.attendance_mode_pending_students || [], "补时段")}${summerIssueRows(summer.duplicate_pending_students || [], "确认重复")}${summerIssueRows(summer.unknown_record_students || [], "补录确认")}`,"status-amber")}<div class="grid section">${card("30天内",safe(renewal.due_30_count))}${card("15天内",safe(renewal.due_15_count))}${card("7天内",safe(renewal.due_7_count))}${card("已出报告",safe(renewal.with_growth_report_count))}</div>${fold("boss-renewal-students","到期学生",`${safe((renewal.students||[]).length)} 名学生，点开看建议动作。`,rows(renewal.students, r => `<div class="row"><div><div class="title">${esc(r.student_name)} · ${esc(r.due_date)}</div><div class="desc">${safe(r.days_to_due)}天 · ${esc(r.renewal_status)} · ${r.has_recent_growth_report?'已有成长报告':'缺成长报告'}<br>${esc(r.suggested_action)}</div></div><span class="pill ${r.has_recent_growth_report?'':'amber'}">${r.has_recent_growth_report?'可沟通':'补证据'}</span></div>`),"status-blue")}`,
          business: `<div class="hero"><div class="big">经营状态与项目机会</div><div class="note">机会必须来自近期内部运营证据；进入这里仍只是待验证候选，不等于正式立项。</div></div>${opportunitySection()}${fold("boss-learning","学习动态",`待审核 ${safe(learning.pending_count)} 条。`,rows(learning.recent_candidates, r => `<div class="row"><div><div class="title">${esc(r.title || r.id)}</div><div class="desc">${esc(r.category)} · ${esc(r.candidate_type)} · ${esc(r.content)}</div></div><span class="pill amber">${esc(r.id)}</span></div>`),"status-blue")}`,
          rules_ops: `<div class="hero"><div class="big">经营提醒与规则</div><div class="note">数据问题中心、绩效风险、可解释规则中心集中在这里，默认只看结论。</div></div><div class="grid">${card("未评估",safe((dq.summary||{}).missing_evaluation_count))}${card("孤儿记录",safe((dq.summary||{}).orphan_record_count))}${card("重复风险",safe((dq.summary||{}).duplicate_risk_count))}${card("高优任务",safe((dq.summary||{}).open_high_task_count))}</div><section class="section"><h2>结算守门</h2><div class="card"><div class="title">${(dq.settlement_guard||{}).can_lock_snapshot ? "可以生成结算快照" : "不建议生成结算快照"}</div><div class="desc">阻断：${((dq.settlement_guard||{}).blocking_reasons||[]).slice(0,3).map(esc).join("；") || "暂无"}<br>提醒：${((dq.settlement_guard||{}).warnings||[]).slice(0,3).map(esc).join("；") || "暂无"}</div></div></section>${fold("boss-data-issues","数据问题中心",`未评估、孤儿记录和重复风险集中处理。`,`${issueRows(dq.orphan_records, "归档")}${issueRows(dq.missing_evaluation_records, "补评估")}${issueRows(dq.duplicate_risk_records, "抽查")}`,"status-amber")}${fold("boss-rules","可解释规则中心",`${safe((ruleCenter.rules||[]).length)} 条规则，点开查看。`,`${rows(ruleCenter.rules, r => `<div class="row"><div><div class="title">${esc(r.name)} · ${esc(r.version)}</div><div class="desc">${esc(r.basis)}</div></div><span class="pill">${esc(r.key)}</span></div>`)}<div class="card"><div class="title">规则模拟器</div><div class="desc">支持项：${((ruleCenter.simulator||{}).inputs_supported||[]).map(esc).join("、")}<br>记录池：¥${safe(((ruleCenter.simulator||{}).current_month_preview||{}).record_pool_amount)} · 激励券最高 ¥${safe(((ruleCenter.simulator||{}).current_month_preview||{}).max_coupon_amount)} · 可锁定：${((ruleCenter.simulator||{}).current_month_preview||{}).can_lock_settlement ? "是" : "否"}</div></div>`,"status-blue")}`,
          performance: `<div class="hero"><div class="big">本月老师记录绩效预估</div><div class="note">记录绩效按必达项和质量分计算，当前预计合计 ¥${safe(performance.estimated_payout)}。</div></div><section class="section"><h2>绩效排行</h2>${rows(performance.teachers, r => { const p = r.performance || {}; const mr = p.monthly_requirements || {}; return `<div class="row"><div><div class="title">${esc(r.teacher_name || r.teacher_id)} · 第 ${safe(p.rank)} 名</div><div class="desc">必达 ${safe(mr.required_done)}/${safe(mr.required_total)} · 质量分 ${safe(mr.quality_score)}/${safe(mr.quality_score_target)} · 预计 ¥${safe(p.estimated_total_amount)} · ${esc(p.status_text || "")}${progress(p.progress_rate)}</div></div><span class="pill ${Number(p.rank_bonus_amount)>0?'amber':''}">¥${safe(p.estimated_total_amount)}</span></div>` })}</section>`,
          risk: `<section class="section"><h2>安全与关系</h2>${rows(task.safety_risks, r => `<div class="row"><div><div class="title">${esc(r.title)}</div><div class="desc">${esc(r.student_name || "未关联学生")} · ${esc(r.status)}</div></div><span class="pill red">${esc(r.level)}</span></div>`)}</section><section class="section"><h2>长期无记录</h2>${rows(risks.long_unrecorded_students, r => `<div class="row"><div><div class="title">${esc(r)}</div><div class="desc">近 7 天暂无记录</div></div><span class="pill amber">关注</span></div>`)}</section>`,
          opp: opportunitySection(),
          learning: `<div class="hero"><div class="big">待审核学习 ${safe(learning.pending_count)} 条</div><div class="note">新说法、误判反馈、闭环表达会先进入审核队列。老板回企业微信发送“待学习”“批准学习 id”或“驳回学习 id”处理。</div></div><section class="section"><h2>最近候选</h2>${rows(learning.recent_candidates, r => `<div class="row"><div><div class="title">${esc(r.title || r.id)}</div><div class="desc">${esc(r.category)} · ${esc(r.candidate_type)} · ${esc(r.content)}</div></div><span class="pill amber">${esc(r.id)}</span></div>`)}</section>`,
          trend: `<section class="section"><h2>近期进步</h2>${rows(data.recent_progress, r => `<div class="row"><div><div class="title">${esc(r.student_name)}</div><div class="desc">${esc(r.summary)}</div></div><span class="pill">进步</span></div>`)}</section><section class="section"><h2>近期异常</h2>${rows(data.recent_anomalies, r => `<div class="row"><div><div class="title">${esc(r.student_name)}</div><div class="desc">${esc(r.summary)}</div></div><span class="pill amber">跟进</span></div>`)}</section>`
        };
        const topGrid = isSummerView ? `${card("暑假学生",safe(s.student_count || (summer.summary||{}).total_students))}${card("今日有课",safe((summerCoverage.summary||{}).scheduled_course_count))}${card("缺记录科目",safe((summerCoverage.summary||{}).missing_course_count))}${card("待审核周报",safe(summerReports.draft_count))}` : `${card("今日记录",s.today_records)}${card("有效记录",s.today_valid_records)}${card("未闭环任务",s.open_task_count)}${card("风险数量",s.high_risk_count)}`;
        const leadGrid = (!isSummerView && !isManager && active === "hermes") ? "" : `<div class="grid">${topGrid}</div>`;
        $("content").innerHTML = `${leadGrid}${tabbar(tabs, active)}${panels[active]}`;
        document.querySelectorAll(".tab").forEach(btn => btn.onclick = () => render(btn.dataset.tab));
        document.querySelectorAll("[data-expand]").forEach(row => row.onclick = () => document.getElementById(row.dataset.expand)?.classList.toggle("hidden"));
        document.querySelectorAll("[data-copy-prompt]").forEach(button => button.onclick = async (event) => {
          event.stopPropagation();
          const prompt = decodeURIComponent(button.dataset.copyPrompt || "");
          try { await navigator.clipboard.writeText(prompt); button.textContent = "已复制，回企业微信发送"; }
          catch (_) { window.prompt("复制后回企业微信发送给小优", prompt); }
        });
        const abnormalToggle = document.getElementById("coverage-anomaly-toggle");
        if(abnormalToggle) abnormalToggle.onclick = () => {
          const only = abnormalToggle.dataset.only !== "1";
          abnormalToggle.dataset.only = only ? "1" : "0";
          abnormalToggle.textContent = only ? "显示全部科目" : "只看异常";
          document.querySelectorAll(".course-normal").forEach(node => node.classList.toggle("hidden", only));
        };
      };
      render(isManager ? "assistant" : (isSummerView ? "brief" : "hermes"));
    }
    async function main(){
      try{
        const me = await fetch("/tuoguan/api/me"+qs).then(r => { if(!r.ok) throw new Error("身份验证失败"); return r.json(); });
        const api = me.role === "teacher" ? "/tuoguan/api/teacher" : "/tuoguan/api/boss";
        const data = await fetch(api+qs).then(r => { if(!r.ok) throw new Error("看板读取失败"); return r.json(); });
        me.role === "teacher" ? teacher(data, me) : boss(data, me);
      }catch(err){
        $("content").innerHTML = `<div class="card empty">${err.message}</div>`;
      }
    }
    main();
  </script>
</body>
</html>"""


_DASHBOARD_HTML = DASHBOARD_WORKBENCH_V1_HTML


_PARENT_REPORT_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>孩子阶段成长反馈</title>
  <style>
    :root{color-scheme:light;--ink:#172026;--muted:#66747b;--line:#dfe8e3;--bg:#f5f8f3;--panel:#ffffff;--green:#176b51;--deep:#0f4f3f;--gold:#b9821b;--soft:#edf6ee;--warm:#fff8ea}
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;letter-spacing:0;overflow-x:hidden}
    .app{width:100%;max-width:min(720px,100vw);margin:0 auto;min-height:100vh;padding:14px 14px 28px;overflow-x:hidden}
    .cover{position:relative;overflow:hidden;border-radius:8px;background:linear-gradient(145deg,var(--deep),#1d7b5a 58%,#d9b76a);color:#fff;padding:18px 15px 16px;box-shadow:0 10px 30px rgba(15,79,63,.16)}
    .brand{position:relative;font-size:13px;font-weight:800;opacity:.96}
    .seal{position:absolute;right:14px;top:14px;border:1px solid rgba(255,255,255,.45);border-radius:999px;padding:4px 9px;font-size:12px;font-weight:700;background:rgba(255,255,255,.12)}
    h1{position:relative;margin:12px 0 6px;font-size:30px;line-height:1.16;max-width:78%}
    .meta{position:relative;font-size:13px;line-height:1.6;opacity:.9}
    .cover-grid{display:grid;grid-template-columns:minmax(0,1fr) 132px;gap:10px;align-items:end;margin-top:12px}
    .summary{position:relative;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.25);border-radius:8px;padding:12px}
    .summary .label{font-size:14px;font-weight:900;opacity:.9}
    .summary .text{font-size:21px;line-height:1.6;margin-top:7px;font-weight:900}
    .tree-stage{height:150px;position:relative;border-radius:8px;background:rgba(255,255,255,.16);overflow:hidden;max-width:100%;contain:paint}
    .sun{position:absolute;right:14px;top:13px;width:24px;height:24px;border-radius:50%;background:#ffd36b;box-shadow:0 0 0 7px rgba(255,211,107,.18)}
    .ground{position:absolute;left:16%;right:16%;bottom:18px;height:9px;border-radius:50%;background:rgba(255,255,255,.2)}
    .tree{position:absolute;left:50%;bottom:22px;width:108px;height:108px;transform-origin:50% 100%;transform:translateX(-50%) scale(var(--tree-scale,.75));max-width:112px}
    .trunk{position:absolute;left:48px;bottom:0;width:14px;height:60px;border-radius:10px;background:linear-gradient(90deg,#7b491f,#b7773d)}
    .branch{position:absolute;left:54px;bottom:36px;width:34px;height:7px;border-radius:999px;background:#8a5528;transform-origin:left center;transform:rotate(-28deg)}
    .branch.b2{left:21px;bottom:45px;transform-origin:right center;transform:rotate(27deg)}
    .leaf{position:absolute;width:38px;height:30px;border-radius:50%;background:#38a86f;opacity:var(--leaf-opacity,.8)}
    .leaf.l1{left:36px;top:12px}.leaf.l2{left:18px;top:34px}.leaf.l3{left:58px;top:34px}.leaf.l4{left:42px;top:50px;background:#74bd58}.leaf.l5{left:32px;top:0;background:#1d875d}
    .fruit{position:absolute;width:8px;height:8px;border-radius:50%;background:#ffd36b}
    .fruit.f1{left:42px;top:34px}.fruit.f2{left:66px;top:48px}.fruit.f3{left:55px;top:20px}
    .tabs{display:flex;gap:8px;margin:13px 0 10px;overflow:auto;padding-bottom:2px}
    .tab{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:10px 12px;font-size:15px;font-weight:800;color:var(--muted);white-space:nowrap}
    .tab.active{border-color:var(--green);color:var(--green)}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(105px,1fr));gap:9px;margin:0 0 12px}
    .metric{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;box-shadow:0 1px 2px rgba(23,32,38,.04)}
    .metric b{font-size:24px;display:block;color:var(--deep)}
    .metric span{font-size:14px;color:var(--muted);line-height:1.35}
    .progress-card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
    .progress-title{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
    .progress-title b{font-size:18px;color:var(--deep)}
    .progress-title span{font-size:14px;color:var(--muted);line-height:1.45;text-align:right}
    .bar{height:9px;border-radius:999px;overflow:hidden;background:#edf1ee;margin-top:9px}
    .bar span{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--green),#d0a84a)}
    section{margin-top:14px}
    h2{font-size:21px;margin:0 0 10px;color:var(--deep)}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:15px;box-shadow:0 1px 2px rgba(23,32,38,.04);font-size:16px;line-height:1.62}
    .item{display:flex;gap:10px;border-top:1px solid var(--line);padding-top:10px;margin-top:10px;line-height:1.6;font-size:16px}
    .item:first-child{border-top:0;padding-top:0;margin-top:0}
    .dot{width:9px;height:9px;border-radius:50%;background:var(--green);margin-top:7px;flex:0 0 auto}
    .dot.gold{background:var(--gold)}
    .note{margin-top:10px;padding:12px;border-radius:8px;background:var(--soft);color:#4c5b61;font-size:14px;line-height:1.65}
    .advice{background:var(--warm)}
    .improve-card{border-color:#f1c27a;background:#fff8ea}
    .improve-card .dot{background:#c77700}
    .advice-card{border-color:#b9d8ce;background:#f0faf5}
    .advice-card .dot{background:#176b51}
    .coupon{background:linear-gradient(145deg,#fffdf5,#fff2cf);border-color:#ead397}
    .coupon-money{font-size:44px;font-weight:900;color:var(--gold);line-height:1}
    .coupon-money small{font-size:18px}
    .empty{padding:30px 14px;background:var(--panel);border:1px solid var(--line);border-radius:8px;text-align:center;color:var(--muted);line-height:1.65}
    footer{margin-top:18px;color:var(--muted);font-size:12px;text-align:center;line-height:1.6}
    @media (max-width:420px){h1{max-width:100%;font-size:26px}.summary .text{font-size:20px}.cover-grid{grid-template-columns:1fr}.tree-stage{height:150px}.grid{grid-template-columns:1fr 1fr}.metric b{font-size:22px}}
  </style>
</head>
<body>
  <main class="app">
    <div class="cover">
      <div class="brand">本机构托管 · 阶段成长反馈</div>
      <div class="seal">老师整理</div>
      <h1 id="title">孩子成长反馈</h1>
      <div class="meta" id="meta">正在读取老师审核后的报告</div>
      <div class="cover-grid">
        <div class="summary"><div class="label">本期一句话反馈</div><div class="text" id="summary">加载中...</div></div>
        <div id="tree" class="tree-stage"></div>
      </div>
    </div>
    <div id="content" class="empty">加载中...</div>
    <footer>本反馈由老师基于日常托管观察整理。链接过期后，请联系老师重新发送。</footer>
  </main>
  <script>
    const TOKEN = "__TOKEN__" || new URLSearchParams(location.search).get("token") || "";
    const CODE = "__CODE__" || new URLSearchParams(location.search).get("code") || "";
    const qs = CODE ? "?code=" + encodeURIComponent(CODE) : "?token=" + encodeURIComponent(TOKEN);
    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
    const safe = (v) => (v === undefined || v === null || v === "" ? "0" : String(v));
    const list = (items, gold=false, klass="") => `<div class="card ${klass}">${(items||[]).length ? items.map(x=>`<div class="item"><span class="dot ${gold?'gold':''}"></span><div>${esc(x)}</div></div>`).join("") : '<div class="item"><span class="dot"></span><div>本期暂无明确内容，老师会继续观察记录。</div></div>'}</div>`;
    const progress = (v) => `<div class="bar"><span style="width:${Math.max(0,Math.min(100,Number(v)||0))}%"></span></div>`;
    const tabbar = (tabs, active) => `<div class="tabs">${tabs.map(t => `<button class="tab ${t.id===active?'active':''}" data-tab="${t.id}">${t.label}</button>`).join("")}</div>`;
    function treeHtml(visual){
      const scale = Number(visual.tree_scale)||.75;
      const leaf = Number(visual.leaf_opacity)||.75;
      return `<div class="sun"></div><div class="ground"></div><div class="tree" style="--tree-scale:${scale};--leaf-opacity:${leaf}"><div class="trunk"></div><div class="branch"></div><div class="branch b2"></div><div class="leaf l1"></div><div class="leaf l2"></div><div class="leaf l3"></div><div class="leaf l4"></div><div class="leaf l5"></div><div class="fruit f1"></div><div class="fruit f2"></div><div class="fruit f3"></div></div>`;
    }
    async function main(){
      try{
        const data = await fetch("/tuoguan/api/parent-report"+qs).then(r => { if(!r.ok) throw new Error("报告链接已失效，请联系老师重新发送。"); return r.json(); });
        const visual = data.visual || {};
        const coupon = data.semester_coupon || {};
        const trust = data.trust_account || {};
        const ledger = data.evidence_ledger || {};
        $("title").textContent = data.share_title || (data.student_name + "成长反馈");
        $("meta").textContent = `${data.period_label || "成长报告"} · ${data.period_start || ""} 至 ${data.period_end || ""} · 老师整理`;
        $("summary").textContent = data.parent_summary || "老师已整理本期阶段反馈。";
        $("tree").innerHTML = treeHtml(visual);
        const tabs = [{id:"cover",label:"成长树"},{id:"summary",label:"近期总结"},{id:"improve",label:"待进步"},{id:"advice",label:"家校建议"},{id:"evidence",label:"证据来源"},{id:"coupon",label:"抵扣券"}];
        const render = (active="cover") => {
          const panels = {
            cover: `<div class="grid"><div class="metric"><b>${esc(data.record_count)}</b><span>本期观察数</span></div><div class="metric"><b>Lv.${safe(visual.growth_level)}</b><span>成长树等级</span></div><div class="metric"><b>${Number(coupon.amount||0)>0?'¥'+safe(coupon.amount):'待确认'}</b><span>预计成长券</span></div></div><div class="progress-card"><div class="progress-title"><b>成长树分值 ${safe(visual.growth_score)}</b><span>记录、表现、跟进共同影响</span></div>${progress(visual.growth_score || 0)}<div class="note">小树会随着老师记录、孩子稳定表现和家校沟通慢慢长大；抵扣金额以校区最终审核为准。</div></div>`,
            summary: `<section><h2>近期一周总结</h2><div class="card">${esc(data.parent_summary || "老师已整理本期阶段反馈。")}</div></section><section><h2>近期动向</h2>${list(data.recent_changes || data.strengths)}</section><section><h2>本期成长亮点</h2>${list(data.strengths)}</section><section><h2>老师已做跟进</h2>${list(data.teacher_followups)}</section>`,
            improve: `<section><h2>需要继续进步的方向</h2>${list(data.concerns, true, "improve-card")}</section><div class="note">这些内容用于帮助孩子继续进步，不作为单次评价结论。</div>`,
            advice: `<section><h2>下一步建议</h2>${list(data.next_steps, true, "advice-card")}</section><section><h2>家校配合建议</h2>${list(data.home_cooperation_suggestions, true, "advice-card")}</section>`,
            evidence: `<section><h2>${esc(trust.title || "孩子成长账户")}</h2><div class="card">${esc(trust.subtitle || "成长树、阶段反馈和激励券都来自同一条已审核证据链。")}</div></section><div class="grid"><div class="metric"><b>${safe(ledger.scored_record_count)}</b><span>计入证据</span></div><div class="metric"><b>${safe(ledger.quality_evidence_count)}</b><span>优质证据</span></div><div class="metric"><b>${safe(ledger.parent_communication_count)}</b><span>家校沟通</span></div></div><section><h2>本期证据来源</h2>${list((trust.explain||[]), true)}</section><div class="note">规则版本：${esc(ledger.rule_version || "growth_trust_account_v1")}。报告发送前仍由老师确认。</div>`,
            coupon: `<div class="card coupon"><div class="title">${esc(coupon.title || "下学期成长激励券")}</div><div class="coupon-money"><small>预计 ¥</small>${safe(coupon.amount)}</div><div class="desc">${esc(coupon.display_status || coupon.status || "以最终审核为准")} · 最高可抵扣 ¥${safe(coupon.max_amount || 200)}</div>${progress(coupon.progress_percent || 0)}</div><section><h2>激励依据</h2>${list(coupon.basis, true)}</section><div class="note">当前为成长激励预估，不代表自动抵扣；最终以校区确认和正式续费规则为准。</div>`
          };
          $("content").innerHTML = `${tabbar(tabs, active)}${panels[active]}<div class="note">${esc(data.footer_note || "本反馈由老师基于日常托管观察整理，并经老师审核后分享。")}</div>`;
          document.querySelectorAll(".tab").forEach(btn => btn.onclick = () => render(btn.dataset.tab));
        };
        render();
      }catch(err){
        $("content").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
      }
    }
    main();
  </script>
</body>
</html>"""

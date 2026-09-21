"""Hermes-native tutoring-center business module."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .store import JSON_NO_CHANGE, TuoguanStore, TuoguanStoreError
from .domain_runtime import domain_runtime as _domain
from .tenant_context import current_tenant_id
from .temporal_grounding import build_temporal_grounding_context
from .digital_employee_state import (
    ATTENTION_THREADS_FILE,
    BUSINESS_EVENTS_FILE,
    RELATIONSHIP_TOUCH_CANDIDATES_FILE,
    query_attention_threads,
    submit_business_event,
    update_attention_thread,
    update_relationship_touch_candidate_status,
)
from .runtime_foundation import (
    foundation_enabled as _foundation_enabled,
    block_tool_after_terminal_result as _foundation_block_tool_after_terminal_result,
    validate_tool_call as _foundation_validate_tool_call,
    begin_inbound as _foundation_begin_inbound,
    current_raw_text as _foundation_current_raw_text,
    inject_model_context as _foundation_inject_model_context,
    observe_tool_result as _foundation_observe_tool_result,
    ensure_outbound_reply_recorded as _foundation_ensure_outbound_reply_recorded,
    mark_outbound_reply_delivered as _foundation_mark_outbound_reply_delivered,
    should_clarify_without_tool as _foundation_should_clarify_without_tool,
    terminal_tool_result_recorded as _foundation_terminal_tool_result_recorded,
    transform_final_response as _foundation_transform_final_response,
    compact_tool_result_for_model as _foundation_compact_tool_result_for_model,
    dedupe_external_reply_blocks as _foundation_dedupe_external_reply_blocks,
    record_work_context_snapshot as _foundation_record_work_context_snapshot,
)
from .turn_trace import (
    begin_tool_event as _begin_trace_tool_event,
    record_response_deduplication as _record_trace_response_deduplication,
    record_tool_result_projection as _record_trace_tool_result_projection,
    begin_turn_trace as _begin_turn_trace,
    finalize_turn_trace as _finalize_turn_trace,
    resolve_trace_session as _resolve_trace_session,
    record_context_sources as _record_trace_context_sources,
    record_context_selection as _record_trace_context_selection,
    record_context_budget as _record_trace_context_budget,
    record_guard_event as _record_trace_guard_event,
    record_tool_event as _record_trace_tool_event,
    record_provider_event as _record_trace_provider_event,
    record_progress_event as _record_trace_progress_event,
)
from .model_context_budget import ContextSection, render_bounded_context
from .turn_fence import (
    bind_session as _bind_turn_fence_session,
    block_reason as _turn_fence_block_reason,
    finish_turn as _finish_turn_fence,
    mark_phase as _mark_turn_fence_phase,
    observe_tool_result as _observe_turn_fence_tool_result,
)
from .provider_resilience import observe_provider_failure as _observe_provider_failure, observe_provider_success as _observe_provider_success
from .runtime_contract import (
    activate_trusted_turn as _activate_trusted_turn,
    bind_trusted_turn as _bind_trusted_turn,
    claim_agenda_service_turn_for_agent as _claim_agenda_service_turn_for_agent,
    claim_robot_turn_for_agent as _claim_robot_turn_for_agent,
    clear_trusted_turn as _clear_trusted_turn,
    current_trusted_turn as _current_trusted_turn,
)
from .runtime_performance import (
    clear_turn_tool_budget as _clear_turn_tool_budget,
    guard_turn_tool_call as _guard_turn_tool_call,
    observe_turn_tool_result as _observe_turn_tool_result,
    reset_turn_tool_budget as _reset_turn_tool_budget,
    turn_tool_budget_snapshot as _turn_tool_budget_snapshot,
)

logger = logging.getLogger(__name__)


def _log_runtime_module_manifest() -> None:
    """Log already-loaded module paths without re-entering plugin discovery.

    Plugin discovery may execute this registration hook concurrently with the
    gateway's auxiliary-task discovery.  Importing gateway modules here can
    therefore wait on the other thread's import lock and prevent the gateway
    from ever reaching its ready state.  Runtime provenance is observational;
    it must never cause a startup dependency or an import-cycle deadlock.
    """
    import sys

    package_name = str(__package__ or "plugins.tuoguan_core")
    names = (
        "gateway.run",
        "gateway.platforms.base",
        f"{package_name}.runtime_ownership",
        f"{package_name}.runtime_foundation",
        f"{package_name}.active_work_context",
        f"{package_name}.provider_resilience",
        f"{package_name}.turn_fence",
        f"{package_name}.self_evolution",
    )
    for name in names:
        try:
            module = sys.modules.get(name)
            if module is None:
                logger.info("P0_RUNTIME_MODULE name=%s state=not_loaded", name)
                continue
            path = Path(str(getattr(module, "__file__", "") or "")).resolve()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
            logger.info("P0_RUNTIME_MODULE name=%s file=%s sha256=%s", name, path, digest)
        except Exception as exc:
            logger.error("P0_RUNTIME_MODULE name=%s load_error=%s", name, exc)

_DAILY_PUSH_TASKS: dict[int, asyncio.Task] = {}
_DAILY_PUSH_WAKE_EVENTS: dict[int, asyncio.Event] = {}
_ACTIVE_WECom_USERS: dict[Any, datetime] = {}
_ACTIVE_CONVERSATION_QUIET_PERIOD = timedelta(minutes=3)
_CLAIMED_REPLY_MESSAGE_IDS: dict[str, datetime] = {}
_ACTIVE_MODEL_TURNS: dict[str, dict[str, Any]] = {}
# Public provider lifecycle hooks tell us when Hermes has reached a terminal
# provider outcome.  Keep only a short-lived terminal label until the gateway
# sends the truthful error reply; never use it to retry, change provider, or
# select a business action.
_TERMINAL_PROVIDER_FAILURES: dict[str, str] = {}
_REPLY_CLAIM_TTL = timedelta(hours=2)
_REGISTERED_TOOL_COUNT = 0
_TRUSTED_MODEL_CHANNELS = frozenset({"wecom_callback", "robot_poc", "agenda_service_work", "reply_recovery"})

_SYSTEM_REPLY_MARKERS = (
    "没有权限",
    "只能",
    "请联系",
    "当前账号暂时不能",
    "暂时无法安全读取",
    "系统操作",
    "未绑定",
)


def _reply_owner_for_routed_reply(metadata: dict[str, Any], reply: str) -> str:
    explicit = str(metadata.get("reply_owner") or "")
    if explicit:
        return explicit
    if any(marker in str(reply or "") for marker in _SYSTEM_REPLY_MARKERS):
        return "system_technical"
    return "model"


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform) or "").lower()


def _is_trusted_model_channel(platform: str) -> bool:
    """Channels whose server-side adapters bind an approved actor identity."""

    return str(platform or "").lower() in _TRUSTED_MODEL_CHANNELS


def _trusted_conversation_scope(*, platform: str, sender_id: str, hook_values: dict[str, Any]) -> str:
    """Return XiaoYou's stable channel scope, independent of Hermes sessions.

    Hermes may intentionally rotate its opaque session id after ``/new`` or
    ``/reset``.  Direct-channel identity and business-object continuity are
    product facts and therefore use only server-attested channel metadata.
    Public runtimes that expose a conversation/chat id win; current WeCom DM
    falls back to the authenticated sender within the already tenant-scoped
    Runtime Contract.
    """

    explicit = str(
        hook_values.get("conversation_id")
        or hook_values.get("chat_id")
        or hook_values.get("channel_conversation_id")
        or ""
    ).strip()
    if explicit:
        return explicit
    if str(platform or "").lower() in {"wecom_callback", "feishu"}:
        return f"{str(platform or '').lower()}:dm:{str(sender_id or '').strip()}"
    return ""


def _trusted_gateway_label(platform: str) -> str:
    return "Robot POC 服务端设备绑定网关" if platform == "robot_poc" else ("Agenda 服务端工作入口" if platform == "agenda_service_work" else ("已验证回执回复恢复入口" if platform == "reply_recovery" else "企业微信网关"))


def _sender_id(source: Any) -> str:
    user_id = str(getattr(source, "user_id", "") or "").strip()
    if _platform_name(source) == "agenda_service_work":
        return user_id
    if ":" in user_id:
        return user_id.split(":", 1)[1]
    return user_id


def _known_wecom_sender(sender_id: str) -> bool:
    try:
        store = TuoguanStore()
        data = store.read_json("wecom_whitelist.json", {})
    except TuoguanStoreError:
        return False
    if not isinstance(data, dict):
        return False
    known = set(data.get("allowed_users") or [])
    known.update(data.get("super_users") or [])
    known.update(data.get("pending_users") or [])
    known.update(data.get("rejected_users") or [])
    known.update((data.get("user_roles") or {}).keys())
    return sender_id in {str(item) for item in known}


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def _open_owner_attention_context(store: TuoguanStore, *, identity: Any, current_message: str = "") -> str:
    """Recall open questions as evidence; Hermes decides whether this turn answers one."""

    if str(getattr(identity, "role", "") or "") != "boss":
        return ""
    result = query_attention_threads(
        store,
        identity=identity,
        include_closed=False,
        limit=3,
    )
    threads = result.get("attention_threads") if isinstance(result, dict) else []
    if not isinstance(threads, list) or not threads:
        return ""
    compact_current = " ".join(str(current_message or "").split())
    lines = [
        "【示例机构主动提问回复锚点】",
        "Hermes 之前主动向老板发出过下面的未解决问题。它们是当前轮高优先级材料，不是 Router，系统不替模型判断答案。",
        f"老板本轮原话：{compact_current[:800]}",
        "请 Hermes 在继续旧会话话题之前，先自主判断老板本轮原话是否在回答、追问或修正下面某一条主动问题。",
        "如果本轮明显是在追问刚刚收到的日报、周报、市场报告或其他主动外发内容，优先衔接最近主动外发消息，不要硬接到更早的未解决问题。",
        "如果相关：先围绕这条主动问题衔接回复，再决定是否调用 tuoguan_update_attention_thread 记录 replied、resolved 或继续等待。",
        "如果无关：说明本轮与未解决主动问题无关，再自然回答老板当前问题；不要因为旧长会话把老板短回复接到别的话题上。",
        "短回复如“同意”“好的”“等8月25号再说”“你需要我怎么确认”很可能是在回答最近主动问题，必须先做相关性判断。",
    ]
    for item in reversed(threads):
        lines.extend([
            f"- attention_id: {item.get('attention_id', '')}",
            f"  focus_key: {item.get('focus_key', '')}",
            f"  status: {item.get('status', '')}",
            f"  sent_at: {item.get('sent_at') or item.get('updated_at') or item.get('created_at') or ''}",
            f"  question: {str(item.get('question_text') or '')[:800]}",
            f"  needed_facts: {json.dumps(item.get('needed_facts') or [], ensure_ascii=False)[:800]}",
        ])
    return "\n".join(lines)


_PROACTIVE_OUTBOUND_ANCHOR_TYPES = {
    "autonomous_owner_attention",
    "autonomous_daily_report",
    "external_learning_report",
    "relationship_touch",
    "staff_voice_owner_alert",
    "task_created",
    "task_due",
    "escalate_now",
    "safety_created",
    "safety_review_required_r1",
    "safety_closed_teacher_feedback",
    "corrective_business_reply",
    "proactive_operations_guidance",
}


def _looks_like_recent_outbound_follow_up(current_message: str) -> bool:
    compact = "".join(str(current_message or "").split())
    if not compact:
        return False
    explicit_anchor_terms = (
        "刚才", "刚刚", "上面", "上一条", "那条", "这条", "这几个", "这些",
        "你发", "发的", "你推", "推的", "主动推", "刚推", "刚发",
        "汇报", "周报", "日报", "报告", "外部学习", "市场调研",
    )
    deictic_terms = (
        "它", "这个", "这", "那个", "那", "这里", "这里边", "这里面",
        "里面", "这边", "其中", "这些", "这几个", "链接", "网址", "新闻",
        "资料", "文章", "页面", "网页",
    )
    query_terms = (
        "讲讲", "讲一下", "说说", "说一下", "解释", "总结", "展开",
        "内容", "具体", "什么意思", "啥意思", "什么情况", "没懂", "没看懂",
        "不懂", "怎么看", "怎么用", "有什么用", "有用吗", "价值", "重点",
        "结论", "哪几个", "哪条", "哪个", "谁",
    )
    short_follow_up = len(compact) <= 28 and (
        any(term in compact for term in deictic_terms)
        or any(term in compact for term in query_terms)
        or "?" in compact
        or "？" in compact
    )
    return (
        any(term in compact for term in explicit_anchor_terms)
        or (
            any(term in compact for term in deictic_terms)
            and any(term in compact for term in query_terms)
        )
        or short_follow_up
    )


def _recent_owner_outbound_context(store: TuoguanStore, *, identity: Any, current_message: str = "") -> str:
    """Recall recent proactive messages as conversation anchors for staff replies.

    Kept under the historical owner-focused name because it is a private hook
    already used by tests. The behavior is now role-neutral for boss, manager,
    and teacher direct conversations.
    """

    role = str(getattr(identity, "role", "") or "")
    if role not in {"boss", "manager", "teacher"}:
        return ""
    compact = "".join(str(current_message or "").split())
    if not compact:
        return ""
    strong_follow_up = _looks_like_recent_outbound_follow_up(compact)
    if not strong_follow_up:
        return ""

    outbox = store.read_json("notification_outbox.json", [])
    if not isinstance(outbox, list):
        outbox = []
    user_ids = {
        str(getattr(identity, "canonical_user_id", "") or ""),
        str(getattr(identity, "platform_user_id", "") or ""),
    }
    user_ids = {item for item in user_ids if item}
    now = datetime.now().astimezone()
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for item in outbox:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") != "sent":
            continue
        targets = {
            str(item.get("touser") or ""),
            str(item.get("target_user_id") or ""),
            str(item.get("recipient_user_id") or ""),
            str(item.get("to_user_id") or ""),
        }
        targets = {target for target in targets if target}
        if user_ids and not (user_ids & targets):
            continue
        notification_type = str(item.get("notification_type") or item.get("action") or "")
        if notification_type not in _PROACTIVE_OUTBOUND_ANCHOR_TYPES:
            continue
        sent_at = (
            _parse_iso_datetime(item.get("sent_at"))
            or _parse_iso_datetime(item.get("last_attempt_at"))
            or _parse_iso_datetime(item.get("created_at"))
        )
        if sent_at is None:
            continue
        if now - sent_at > timedelta(hours=3):
            continue
        content = " ".join(str(item.get("content") or item.get("message") or item.get("text") or "").split())
        if not content:
            continue
        candidates.append((sent_at, item))
    candidates.extend(_recent_outbound_history_candidates(store, user_ids=user_ids, now=now))
    if not candidates:
        return ""
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    role_label = {"boss": "老板", "manager": "店长", "teacher": "老师"}.get(role, "当前用户")
    lines = [
        "【示例机构最近主动外发消息锚点】",
        f"下面是小优最近主动发给{role_label}的消息。它们形成短时临时会话线程，只是衔接材料，不是 Router，也不替模型判断用户意图。",
        f"{role_label}本轮原话：{' '.join(str(current_message or '').split())[:800]}",
        "强衔接规则：用户说“它/里面/这个/这些/链接/网址/内容/讲讲/解释/总结/什么意思/你发的/你推的”时，优先把本轮理解为追问最近一条主动外发消息。",
        "除非用户本轮明确点名其他对象（例如明确说看板、某个老师、某项任务编号），不要把模糊代词接到更早的旧会话、旧看板链接、旧偏好或旧工作项。",
        "如果相关：先围绕对应外发消息解释清楚；如果这是外部学习/市场报告，要解释资料讲了什么、对示例机构有什么用、哪些只是外部资料不能当成机构事实。",
        "如果需要查更完整来源，再由模型自主决定是否调用可信只读工具；不要在没有核验时说已经浏览了网页全文。",
        "如果无关：把这些当背景材料，自然回答当前问题。",
    ]
    seen: set[str] = set()
    for sent_at, item in candidates[:3]:
        content = " ".join(str(item.get("content") or item.get("message") or item.get("message_text") or "").split())
        key = str(item.get("id") or item.get("message_id") or content[:120])
        if key in seen:
            continue
        seen.add(key)
        lines.extend([
            f"- notification_id: {item.get('id', '')}",
            f"  notification_type: {item.get('notification_type') or item.get('action') or item.get('source') or ''}",
            f"  action: {item.get('action', '')}",
            f"  anchor_priority: {'latest_active_outbound_thread' if len(seen) == 1 else 'recent_outbound_background'}",
            f"  sent_at: {sent_at.isoformat(timespec='seconds')}",
            f"  summary: {str(item.get('summary') or '')[:300]}",
            f"  content_excerpt: {content[:1200]}",
        ])
    return "\n".join(lines)


def _recent_outbound_history_candidates(
    store: TuoguanStore,
    *,
    user_ids: set[str],
    now: datetime,
) -> list[tuple[datetime, dict[str, Any]]]:
    """Fallback anchors from hidden system_push history when outbox is incomplete."""

    path = store.path_for("message_history.jsonl")
    if not path.exists():
        return []
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                if str(item.get("direction") or "") != "outbound":
                    continue
                if str(item.get("source") or "") != "system_push":
                    continue
                candidate_user_ids = {
                    str(item.get("canonical_user_id") or ""),
                    str(item.get("user_id") or ""),
                }
                candidate_user_ids = {value for value in candidate_user_ids if value}
                if user_ids and not (user_ids & candidate_user_ids):
                    continue
                content = " ".join(str(item.get("message_text") or "").split())
                if not content:
                    continue
                sent_at = _parse_iso_datetime(item.get("created_at"))
                if sent_at is None or now - sent_at > timedelta(hours=3):
                    continue
                candidates.append((sent_at, {
                    "id": item.get("id") or item.get("idempotency_key") or item.get("message_id") or "",
                    "notification_type": item.get("source") or "message_history_outbound",
                    "action": item.get("related_state_type") or "",
                    "sent_at": item.get("created_at") or "",
                    "content": content,
                    "message_id": item.get("message_id") or "",
                }))
    except OSError:
        return []
    return candidates[-3:]


def _term_boundary_context(store: TuoguanStore, *, raw_text: str, include_for_attention: bool = False) -> str:
    """Recall current term boundary as material; it does not decide the reply."""

    term_state = store.read_json("academic_term_state.json", {})
    if not isinstance(term_state, dict):
        return ""
    if str(term_state.get("service_relation_policy") or "") != "defer_until_new_term":
        return ""
    compact = "".join(str(raw_text or "").split())
    related = include_for_attention or any(
        term in compact
        for term in (
            "续费", "候选", "沟通", "开学", "新学期", "名单", "老师",
            "主责", "服务关系", "服务类型", "转校", "价格", "观望",
        )
    )
    if not related:
        return ""
    start = str(term_state.get("confirmation_window_start") or "2026-08-25")
    end = str(term_state.get("confirmation_window_end") or "2026-09-10")
    return "\n".join([
        "【示例机构当前学期边界材料】",
        "当前 service_relation_policy=defer_until_new_term。旧学生名单、旧责任老师、旧服务类型只可作为历史分析材料，不是新学期确认事实。",
        f"新学期名单、服务类型、主责老师/责任关系的主动确认窗口：{start} 至 {end}。",
        "除非老板本轮明确要求提前处理新学期服务关系，否则不要在当前回复里追问具体学生的主责老师、服务类型或开学后责任归属。",
        "可以继续做历史续费原因分析、沟通覆盖分析、候选材料、话术和老板审核材料；不得把旧名单用于自动联系老师/家长或分配责任。",
        "这只是事实边界材料，不是 Router，也不是固定流程。Hermes 仍需自己判断本轮怎么回答。",
    ])


def _public_identity_context(store: TuoguanStore) -> str:
    """Recall the confirmed public-facing employee name.

    This is stable identity material, not a business workflow. It lets the
    model keep using the internal product name for architecture while honoring
    the owner's chosen employee name in human-facing replies.
    """

    facts = store.read_json("operational_facts.json", {})
    if not isinstance(facts, dict):
        return ""
    for item in reversed(facts.get("facts") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") != "active":
            continue
        if str(item.get("fact_type") or "") != "owner_rule":
            continue
        if str(item.get("subject") or "") != "数字员工称呼":
            continue
        value = str(item.get("value") or "")
        if "小优" not in value:
            continue
        source_text = str(item.get("source_text") or "机构负责人已确认数字员工对外称呼为小优。")
        confirmed_at = str(item.get("confirmed_at") or item.get("updated_at") or "")
        return "\n".join([
            "【示例机构数字员工身份称呼】",
            "已确认运营事实：老板给数字员工起名为“小优”。",
            "对外面向老板、店长、老师自然沟通时，优先自称“小优”；不要再主动自称 Hermes。",
            "Hermes 只作为内部产品/架构名称保留；当用户问技术实现、代码、系统架构时才可说明内部名 Hermes。",
            f"事实来源：{source_text}",
            f"确认时间：{confirmed_at}",
            "这只是身份材料，不是 Router、不是固定回复模板，也不替模型决定业务动作。",
        ])
    return ""


def _xiaoyou_core_skill_context(*, identity: Any) -> str:
    role = str(getattr(identity, "role", "staff") or "staff")
    user_id = str(getattr(identity, "canonical_user_id", "") or "")
    person_name = str(getattr(identity, "person_name", "") or user_id)
    role_label = {"boss": "老板", "manager": "店长", "teacher": "老师"}.get(role, "员工")
    return (
        "【已加载 Skill：xiaoyou-core】小优是托管机构数字员工；员工手册提供身份、业务常识、岗位责任和判断框架，"
        "模型负责理解、判断和行动选择，系统只守身份、权限、证据、幂等、频率、审计、真实执行、写后反查和外发边界。"
        f"【当前对话人可信身份】本轮服务对象已经由企业微信验证为：{person_name}（{role_label}，user_id={user_id}，role={role}）。"
        f"必须把对方称为“{person_name}”或自然省略称呼；不得从全局记忆、旧会话或其他人的材料把当前人猜成机构负责人、老板或其他员工。"
        "用户问‘我是谁’时直接依据这一可信身份回答，不需要先扫描全员目录。"
        "只可注入此人的角色、个人工作方式、当前任务和必要机构事实，"
        "不得混入老板或其他员工的个人档案。先查当前上下文、可信业务工具、人员目录、历史证据及必要只读公开资料，再说查不到。"
        "没有真实工具调用不能说查过，没有写后反查不能说已保存，没有发送回执不能说已发送。"
        "简单问候、致谢、确认在场等不涉及业务事实或执行动作的消息，直接自然简短回复，不要调用工具。"
        "需要业务事实时，以完成当前问题所需的最少证据为准：先调用一个最相关的领域入口，"
        "只有结果明确暴露出必要缺口时再补查；同一参数不得重复查询，不要为了展示能力遍历工具。"
        "凡是本轮工具列表中已经可见的 tuoguan_ 工具，必须直接调用该工具，禁止再套用 tool_call；"
        "调用前按工具说明补齐必填参数，写工具的 operation_id 使用当前消息 id。"
        "专项问题按需参考 youyi-digital-employee、youyi-tuoguan-business、active-information-acquisition、goal-management、"
        "memory-evidence-learning、institution-onboarding、student-service-relations；它们不是固定 Router。"
    )


def _workstyle_context(store: TuoguanStore, *, identity: Any, raw_text: str) -> str:
    try:
        from .workstyle_profiles import infer_workstyle_scope, workstyle_context_for_user

        return workstyle_context_for_user(
            store,
            identity=identity,
            scope=infer_workstyle_scope(raw_text),
            raw_text=raw_text,
        )
    except Exception:
        logger.exception("tuoguan_core failed to recall person workstyle context")
        return ""


def _workstyle_selection_ids(store: TuoguanStore, *, identity: Any, raw_text: str) -> list[str]:
    """Return exact scoped preference IDs for trace evidence only.

    Selection uses the same profile resolver as the rendered context.  The
    identifiers are never exposed to the model or used to select a Tool.
    """

    try:
        from .workstyle_profiles import infer_workstyle_scope, resolve_workstyle_for

        resolved = resolve_workstyle_for(
            store,
            identity=identity,
            target_user_id=identity.canonical_user_id,
            target_role=identity.role,
            scope=infer_workstyle_scope(raw_text),
            limit=8,
        )
        if not resolved.get("ok"):
            return []
        return [
            str(item.get("preference_id") or "")
            for item in (resolved.get("effective_preferences") or [])
            if isinstance(item, dict) and str(item.get("preference_id") or "")
        ]
    except Exception:
        logger.exception("tuoguan_core failed to trace person workstyle selection")
        return []


def _self_evolution_context(store: TuoguanStore, *, identity: Any) -> dict[str, Any]:
    try:
        from .self_evolution import conversation_evolution_context_bundle_for_user

        return conversation_evolution_context_bundle_for_user(
            store,
            identity=identity,
            limit=5,
        )
    except Exception:
        logger.exception("tuoguan_core failed to recall self-evolution context")
        return {"text": "", "application_ids": []}


def _role_layer_context(*, identity: Any, raw_text: str = "") -> str:
    role = str(getattr(identity, "role", "") or "")
    compact = "".join(str(raw_text or "").split())
    if role == "boss":
        lines = [
            "【小优角色分层：老板侧】",
            "老板侧的小优是机构管理数字员工：提供经营洞察、团队状态、风险趋势和需要拍板的事项；不要把老师/店长完整私聊原文当日报内容。",
            "当老板问“今天有没有老师/店长找你对话/谁和你聊过/有没有联系你”时，必须优先调用 tuoguan_query_staff_conversation_activity 查询员工对话活动摘要。",
            "当老板问“最近老师有没有说什么/店长有没有反馈/团队状态/店里有什么问题/有没有抱怨/谁情绪不稳定”时，必须优先调用 tuoguan_query_staff_voice_radar 查询员工声音雷达。",
            "低风险员工声音默认只讲趋势；中高风险可以点名并给证据摘要；严重风险以老板-only 提醒候选和现有 outbox 边界处理。",
            "已分配任务缺少结果或闭环证据时，不要再创建第二条任务；先查询原任务，再用 tuoguan_submit_relationship_touch_candidate 以 action_type=ask_task_fact、related_task_id=原任务 id、execute_if_authorized=true 追问执行人。正式任务协作与普通关系触达分别校验。",
            "当当前材料已经给出目标人、一个具体问题和询问原因，且你判断现在应该主动问时，直接调用 tuoguan_submit_relationship_touch_candidate 并设置 execute_if_authorized=true；不要再次遍历目标、任务或活动上下文。",
            "是否继续追问、找谁核实、怎样处理，仍由模型结合老板目标和真实事实自主判断。",
        ]
        if any(term in compact for term in ("找你", "和你聊", "跟你聊", "联系你", "给你发消息", "对话", "聊天", "今天有没有老师", "今天有没有店长")):
            lines.append("老板本轮像是在查询员工对话活动；没有员工对话活动工具结果前，不得凭记忆回答“没有”。")
        if any(term in compact for term in ("老师有没有", "店长有没有", "有没有说", "有没有反馈", "有没有抱怨", "团队状态", "店里问题", "情绪", "不开心")):
            lines.append("老板本轮像是在查询员工声音或团队状态；没有雷达工具结果前，不得凭记忆回答“没有”。")
        return "\n".join(lines)
    if role == "manager":
        return "\n".join([
            "【小优角色分层：店长侧】",
            "店长侧的小优是现场协作助手：帮店长梳理排班、老师反馈、学生服务和执行卡点，减轻现场管理负担。",
            "店长说老师意见、排班压力、制度不清、执行冲突或现场风险时，先帮他把问题拆清楚、给可执行建议；如果有管理价值，可调用 tuoguan_submit_staff_voice_signal 保存员工声音信号。",
            "回复店长时禁止说“我会汇报老板/已反馈老板/我在监控/老板让我盯着你”；如果被问边界，只温和说明工作相关重要风险会进入管理材料，不能承诺绝对保密。",
            "本工具不改绩效、工资、制度、权限，不触达家长，也不替模型决定下一步。",
        ])
    if role == "teacher":
        return "\n".join([
            "【小优角色分层：老师侧】",
            "老师侧的小优是教育朋友、记录助手和情绪支持者：先接住情绪，帮老师理清学生、工作和协作问题，再给轻量可执行建议。",
            "老师表达抱怨、压力、协作冲突、制度不清、离职倾向、安全风险或管理建议时，先支持老师，不要站在监督者口吻；如有管理价值，可调用 tuoguan_submit_staff_voice_signal 保存员工声音信号。",
            "回复老师时禁止说“我会汇报老板/已反馈老板/我在监控/老板让我盯着你”；如果被问隐私边界，只温和说明工作相关重要风险需要被妥善处理，不能承诺绝对保密。",
            "员工声音信号不等于绩效证据，不改工资、不改制度、不派任务、不联系家长；模型仍负责自然回应和判断。",
        ])
    return ""


def _record_owner_inbound_fact(
    store: TuoguanStore,
    *,
    identity: Any,
    raw_text: str,
    message_id: str,
    session_id: str,
) -> None:
    """Persist the original owner message without deciding what it means."""

    if str(getattr(identity, "role", "") or "") != "boss" or not raw_text.strip():
        return
    path = store.path_for(BUSINESS_EVENTS_FILE)
    if path.exists() and message_id:
        for line in path.read_text(encoding="utf-8-sig").splitlines()[-300:]:
            if message_id in line and '"event_type":"owner_inbound_message"' in line:
                return
    from .write_guard import authorized_business_write, prepare_system_write

    write_auth = prepare_system_write(
        store.data_dir,
        job_name="owner_inbound_message_fact",
        allowed_files={BUSINESS_EVENTS_FILE},
    )
    with authorized_business_write(
        source="gateway_inbound",
        operation_id=write_auth["operation_id"],
        ledger_id=write_auth["ledger_id"],
        audit_id=write_auth["audit_id"],
        allowed_files={BUSINESS_EVENTS_FILE},
    ):
        submit_business_event(
            store,
            identity=identity,
            event_type="owner_inbound_message",
            event_text=raw_text,
            operation_id=write_auth["operation_id"],
            related_objects=[{"type": "gateway_session", "id": session_id}],
            source_text=raw_text,
            source_message_id=message_id,
        )


def _should_route(event: Any) -> bool:
    source = getattr(event, "source", None)
    if source is None:
        return False
    if _platform_name(source) != "wecom_callback":
        return False
    if str(getattr(source, "chat_type", "dm") or "dm").lower() not in {"", "dm"}:
        return False
    text = str(getattr(event, "text", "") or "").strip()
    if not text or text.startswith("/"):
        return False
    sender = _sender_id(source)
    if not sender:
        return False
    compact = text.replace(" ", "")
    if _known_wecom_sender(sender):
        return True
    # Unknown Enterprise WeChat DMs must still enter the tutoring router so the
    # identity gate can record a pending binding instead of falling through to
    # the general assistant and accidentally treating the sender as someone else.
    return True


async def _send_reply(
    adapter: Any,
    chat_id: str,
    reply: str,
    event: Any,
    outbound_metadata: dict[str, Any] | None = None,
) -> None:
    try:
        source = getattr(event, "source", None)
        from .message_history import conversation_id_for
        conversation_id = conversation_id_for(
            _platform_name(source),
            _sender_id(source),
            str(getattr(source, "chat_id", "") or chat_id),
            str(getattr(source, "chat_type", "dm") or "dm"),
        )
        metadata = {
            "handled_by": "tuoguan_core",
            "outbound_source": "deterministic_fallback",
            "conversation_id": conversation_id,
            "chat_type": str(getattr(source, "chat_type", "dm") or "dm"),
        }
        metadata.update(outbound_metadata or {})
        from .outbound_policy import prepare_final_delivery
        source_message_id = str(getattr(event, "message_id", "") or "")
        envelope = prepare_final_delivery(
            source_message_id=source_message_id,
            message_owner_id=str(metadata.get("message_owner_id") or ""),
            reply_owner=_reply_owner_for_routed_reply(metadata, reply),
            reply_text=reply,
            business_result=metadata.get("business_result") if isinstance(metadata.get("business_result"), dict) else None,
            trusted_deterministic=bool(metadata.get("trusted_deterministic", False)),
        )
        reply = envelope.reply_text
        metadata["final_reply_envelope"] = envelope.to_dict()
        await adapter.send(
            chat_id,
            reply,
            reply_to=getattr(event, "message_id", None),
            metadata=metadata,
        )
        identity = _domain().identities.resolve(
            _platform_name(source),
            _sender_id(source),
            user_name=str(getattr(source, "user_name", "") or ""),
            chat_id=str(getattr(source, "chat_id", "") or chat_id),
            message_text=str(getattr(event, "text", "") or ""),
        )
        _foundation_ensure_outbound_reply_recorded(
            store=_domain().store,
            message_id=str(getattr(event, "message_id", "") or ""),
            conversation_id=conversation_id,
            user_id=identity.canonical_user_id,
            role=identity.role,
            raw_text=str(getattr(event, "text", "") or ""),
            final_reply=reply,
            entered_model=False,
            route_decision=str(metadata.get("route_decision") or metadata.get("related_state_type") or "legacy_router"),
            message_owner_id=str(metadata.get("message_owner_id") or ""),
            ownership_type=str(metadata.get("ownership_type") or ""),
            owner_capability=str(metadata.get("owner_capability") or ""),
            reply_owner=_reply_owner_for_routed_reply(metadata, reply),
            reply_sender_count=1,
        )
    except Exception:
        logger.exception("tuoguan_core failed to send routed reply")


async def _send_notifications(
    adapter: Any,
    notifications: list[dict[str, Any]],
) -> None:
    from .message_history import content_fingerprint

    delivered: list[dict[str, Any]] = []
    for item in notifications:
        touser = str(item.get("touser") or "").strip()
        content = str(item.get("content") or "").strip()
        if not touser or not content:
            continue
        try:
            result = await adapter.send(
                touser,
                content,
                metadata={
                    "handled_by": "tuoguan_core",
                    "notification": True,
                    "message_type": "notification",
                    "outbound_source": "system_push",
                    "task_id": str(item.get("task_id") or ""),
                    "action": str(item.get("action") or ""),
                    "program_id": str(item.get("program_id") or ""),
                    "idempotency_key": (
                        f"notification:{touser}:{item.get('task_id') or ''}:"
                        f"{item.get('action') or ''}:{content_fingerprint(content)}"
                    ),
                },
            )
        except Exception:
            logger.exception("tuoguan_core failed to send notification")
            _append_notification_failure(item, "exception")
            continue
        if bool(getattr(result, "success", False)):
            delivered.append(item)
        else:
            error = str(getattr(result, "error", "") or "unknown")
            logger.warning(
                "tuoguan_core notification send failed: target=%s error=%s",
                touser,
                error,
            )
            _append_notification_failure(item, error)
    if delivered:
        _domain().confirm_notifications_delivered(delivered)


def _append_notification_failure(item: dict[str, Any], error: str) -> None:
    try:
        store = TuoguanStore()
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "touser": str(item.get("touser") or ""),
            "task_id": str(item.get("task_id") or ""),
            "action": str(item.get("action") or ""),
            "error": error,
        }
        store.append_jsonl_verified("notification_failures.jsonl", entry)
    except Exception:
        logger.exception("tuoguan_core failed to append notification failure log")


def _parse_outbox_datetime(value: str, *, now: datetime) -> datetime:
    parsed = datetime.fromisoformat(str(value or ""))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=now.tzinfo)
    return parsed.astimezone(now.tzinfo)


def _coerce_runtime_datetime(value: Any, *, now: datetime) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or ""))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=now.tzinfo)
    return parsed.astimezone(now.tzinfo)


def _outbox_item_age_seconds(item: dict[str, Any], *, now: datetime) -> float | None:
    created_at = str(item.get("created_at") or "").strip()
    if not created_at:
        return None
    try:
        return (now - _parse_outbox_datetime(created_at, now=now)).total_seconds()
    except (TypeError, ValueError):
        return None


def _stale_outbox_suppression_reason(item: dict[str, Any], *, now: datetime) -> str:
    age = _outbox_item_age_seconds(item, now=now)
    if age is None:
        return ""
    notification_type = str(item.get("notification_type") or "")
    action = str(item.get("action") or "")
    if notification_type == "autonomous_owner_attention" and age > 2 * 3600:
        return "stale_owner_attention_after_outbox_block"
    if action in {"task_created", "task_due", "manual_assignment"} and age > 24 * 3600:
        return "stale_task_notification_after_outbox_block"
    return ""


def _stale_outbox_failure_reason(item: dict[str, Any], *, now: datetime) -> str:
    age = _outbox_item_age_seconds(item, now=now)
    if age is None:
        return ""
    if str(item.get("notification_type") or "") == "autonomous_daily_report" and age > 4 * 3600:
        return "daily_report_delivery_window_missed_after_outbox_block"
    return ""


async def _daily_push_loop(adapter: Any, wake_event: asyncio.Event) -> None:
    while True:
        try:
            # Model-led restore: no unsolicited daily/proactive system pushes.
            # Only deliver notifications that an explicit business tool already queued.
            await _drain_notification_outbox(adapter)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("tuoguan_core daily dashboard push failed")
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=60)
        except TimeoutError:
            pass
        finally:
            wake_event.clear()


async def _drain_notification_outbox(adapter: Any) -> None:
    """Deliver only notifications created by the unified foundation."""

    store = TuoguanStore()
    exclusions = store.read_json("task_execution_exclusions.json", {})
    excluded_task_ids = {
        str(value)
        for value in (exclusions.get("excluded_task_ids", []) if isinstance(exclusions, dict) else [])
    }
    max_attempts = 3
    retry_delay_seconds = 60
    processed = 0
    while processed < 50:
        now = datetime.now().astimezone()
        claim = _claim_next_notification_outbox_item(store, excluded_task_ids=excluded_task_ids, now=now)
        item = claim.get("item") if isinstance(claim.get("item"), dict) else {}
        if not item:
            return
        processed += 1
        event = str(claim.get("event") or "")
        result = str(claim.get("result") or "")
        if event:
            _append_notification_audit(store, item, event, result)
            if result == "missing_target_or_content":
                _append_notification_failure(item, result)
            _sync_notification_outbox_receipts(store, item)
            continue
        target = str(item.get("touser") or "").strip()
        content = str(item.get("content") or "").strip()
        send_result = await adapter.send(target, content, metadata={
            "handled_by": "tuoguan_core",
            "notification": True,
            "outbound_source": "system_push",
            "task_id": str(item.get("task_id") or ""),
            "action": str(item.get("action") or ""),
            "idempotency_key": str(item.get("id") or ""),
            "outbox_lease_id": str(item.get("lease_id") or ""),
        })
        finalized = _finish_claimed_notification_outbox_item(
            store,
            claimed_item=item,
            send_result=send_result,
            now=datetime.now().astimezone(),
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
        final_item = finalized.get("item") if isinstance(finalized.get("item"), dict) else {}
        if not final_item:
            continue
        if str(final_item.get("status") or "") == "sent":
            _append_notification_audit(store, final_item, "notification_sent", "success")
        elif str(final_item.get("status") or "") == "retry_pending":
            _append_notification_failure(final_item, str(final_item.get("last_error") or "unknown"))
            _append_notification_audit(store, final_item, "notification_retry_scheduled", "retry_pending")
        else:
            _append_notification_failure(final_item, str(final_item.get("last_error") or "unknown"))
            _append_notification_audit(store, final_item, "notification_failed", str(final_item.get("last_error") or "attempts_exhausted"))
        _sync_notification_outbox_receipts(store, final_item)


def _claim_next_notification_outbox_item(store: TuoguanStore, *, excluded_task_ids: set[str], now: datetime) -> dict[str, Any]:
    from .write_guard import authorized_business_write, prepare_system_write

    claim: dict[str, Any] = {}
    now_iso = now.isoformat(timespec="seconds")
    lease_id = f"outbox_lease_{uuid.uuid4().hex[:16]}"
    allowed_files = {"notification_outbox.json"}
    write_auth = prepare_system_write(store.data_dir, job_name="notification_outbox_claim", allowed_files=allowed_files)

    from .staff_administration import staff_is_offboarded

    def mutate(outbox: Any) -> Any:
        outbox = outbox if isinstance(outbox, list) else []
        for item in outbox:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            if status == "sending":
                lease_expires_at = _coerce_runtime_datetime(item.get("lease_expires_at"), now=now)
                if lease_expires_at is not None and lease_expires_at <= now:
                    item.update({
                        "status": "result_unknown",
                        "last_error": "send_result_unknown_after_lease_expired",
                        "result_unknown_at": now_iso,
                        "last_attempt_at": now_iso,
                    })
                    item.pop("retry_at", None)
                    claim.update({"item": deepcopy(item), "event": "notification_result_unknown", "result": "lease_expired"})
                    return outbox[-2000:]
                continue
            if status not in {"pending", "retry_pending"}:
                continue
            retry_at = str(item.get("retry_at") or "").strip()
            if retry_at:
                try:
                    if _parse_outbox_datetime(retry_at, now=now) > now:
                        continue
                except ValueError:
                    item.update({"status": "failed", "last_error": "invalid_retry_at", "last_attempt_at": now_iso})
                    claim.update({"item": deepcopy(item), "event": "notification_failed", "result": "invalid_retry_at"})
                    return outbox[-2000:]
            task_id = str(item.get("task_id") or "")
            if task_id and task_id in excluded_task_ids:
                item.update({
                    "status": "suppressed",
                    "suppressed_reason": "excluded_task",
                    "suppressed_at": now_iso,
                })
                claim.update({"item": deepcopy(item), "event": "notification_suppressed", "result": "excluded_task"})
                return outbox[-2000:]
            if item.get("delivery_mode") != "direct_wecom":
                continue
            notification_type = str(item.get("notification_type") or "")
            goal_action_id = str(item.get("goal_action_id") or "")
            if notification_type == "relationship_touch" or (notification_type == "task_created" and goal_action_id):
                from .proactive_work import effective_proactive_permission

                target_role = str(item.get("role") or "teacher")
                action_type = (
                    "assign_low_risk_goal_task"
                    if notification_type == "task_created"
                    else str(item.get("proactive_action_type") or "ask_work_fact")
                )
                permission = effective_proactive_permission(
                    store,
                    target_role=target_role,
                    target_user_id=str(item.get("touser") or item.get("target_user_id") or ""),
                    action_type=action_type,
                    goal_id=str(item.get("goal_id") or ""),
                    now=now,
                )
                if not permission.get("allowed"):
                    item.update({
                        "status": "suppressed",
                        "suppressed_reason": f"delivery_permission_recheck:{permission.get('reason_code') or 'permission_denied'}",
                        "suppressed_at": now_iso,
                    })
                    claim.update({"item": deepcopy(item), "event": "notification_suppressed", "result": str(item["suppressed_reason"])})
                    return outbox[-2000:]
                item["delivery_permission_rechecked_at"] = now_iso
                item["delivery_permission"] = permission
            stale_failure = _stale_outbox_failure_reason(item, now=now)
            if stale_failure:
                item.update({
                    "status": "failed",
                    "last_error": stale_failure,
                    "failed_at": now_iso,
                    "last_attempt_at": now_iso,
                })
                item.pop("retry_at", None)
                claim.update({"item": deepcopy(item), "event": "notification_failed", "result": stale_failure})
                return outbox[-2000:]
            stale_reason = _stale_outbox_suppression_reason(item, now=now)
            if stale_reason:
                item.update({
                    "status": "suppressed",
                    "suppressed_reason": stale_reason,
                    "suppressed_at": now_iso,
                })
                claim.update({"item": deepcopy(item), "event": "notification_suppressed", "result": stale_reason})
                return outbox[-2000:]
            deliver_at = str(item.get("deliver_at") or "").strip()
            if deliver_at:
                try:
                    if _parse_outbox_datetime(deliver_at, now=now) > now:
                        continue
                except ValueError:
                    item.update({"status": "failed", "last_error": "invalid_deliver_at", "last_attempt_at": now_iso})
                    claim.update({"item": deepcopy(item), "event": "notification_failed", "result": "invalid_deliver_at"})
                    return outbox[-2000:]
            target = str(item.get("touser") or "").strip()
            content = str(item.get("content") or "").strip()
            if not target or not content:
                item.update({"status": "failed", "last_error": "missing_target_or_content", "last_attempt_at": now_iso})
                claim.update({"item": deepcopy(item), "event": "notification_failed", "result": "missing_target_or_content"})
                return outbox[-2000:]
            if staff_is_offboarded(store, target):
                item.update({
                    "status": "suppressed",
                    "suppressed_reason": "recipient_staff_offboarded",
                    "suppressed_at": now_iso,
                })
                item.pop("retry_at", None)
                claim.update({"item": deepcopy(item), "event": "notification_suppressed", "result": "recipient_staff_offboarded"})
                return outbox[-2000:]
            activity_key = (str(store.data_dir.resolve()), target)
            last_inbound = _coerce_runtime_datetime(
                _ACTIVE_WECom_USERS.get(activity_key, _ACTIVE_WECom_USERS.get(target)),
                now=now,
            )
            if (
                str(item.get("notification_type") or "") != "autonomous_daily_report"
                and last_inbound is not None
                and now - last_inbound < _ACTIVE_CONVERSATION_QUIET_PERIOD
            ):
                continue
            item.update({
                "status": "sending",
                "lease_id": lease_id,
                "lease_owner": "notification_outbox_delivery",
                "lease_started_at": now_iso,
                "lease_expires_at": (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
                "last_attempt_at": now_iso,
                "attempt_count": int(item.get("attempt_count") or 0) + 1,
            })
            item.pop("retry_at", None)
            claim.update({"item": deepcopy(item)})
            return outbox[-2000:]
        return JSON_NO_CHANGE

    with authorized_business_write(
        source="system_job",
        operation_id=write_auth["operation_id"],
        ledger_id=write_auth["ledger_id"],
        audit_id=write_auth["audit_id"],
        allowed_files=allowed_files,
    ):
        store.update_json("notification_outbox.json", [], mutate)
    return claim


def _finish_claimed_notification_outbox_item(
    store: TuoguanStore,
    *,
    claimed_item: dict[str, Any],
    send_result: Any,
    now: datetime,
    max_attempts: int,
    retry_delay_seconds: int,
) -> dict[str, Any]:
    from .write_guard import authorized_business_write, prepare_system_write

    final: dict[str, Any] = {}
    notification_id = str(claimed_item.get("id") or "")
    lease_id = str(claimed_item.get("lease_id") or "")
    success = bool(getattr(send_result, "success", False))
    error = str(getattr(send_result, "error", "") or "unknown")
    now_iso = now.isoformat(timespec="seconds")
    allowed_files = {"notification_outbox.json"}
    write_auth = prepare_system_write(store.data_dir, job_name="notification_outbox_receipt", allowed_files=allowed_files)

    def mutate(outbox: Any) -> Any:
        outbox = outbox if isinstance(outbox, list) else []
        for item in outbox:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "") != notification_id:
                continue
            if str(item.get("lease_id") or "") != lease_id or str(item.get("status") or "") != "sending":
                return JSON_NO_CHANGE
            if success:
                item["status"] = "sent"
                item["sent_at"] = now_iso
                item["message_id"] = str(getattr(send_result, "message_id", "") or "")
                item.pop("retry_at", None)
            else:
                item["last_error"] = error
                if int(item.get("attempt_count") or 0) < max_attempts:
                    item["status"] = "retry_pending"
                    item["retry_at"] = (now + timedelta(seconds=retry_delay_seconds)).isoformat(timespec="seconds")
                else:
                    item["status"] = "failed"
                    item["failed_at"] = now_iso
                    item.pop("retry_at", None)
            item["last_attempt_at"] = now_iso
            item.pop("lease_id", None)
            item.pop("lease_owner", None)
            item.pop("lease_started_at", None)
            item.pop("lease_expires_at", None)
            from .execution_receipts import delivery_execution_receipt

            item["execution_receipt"] = delivery_execution_receipt(item)
            final["item"] = deepcopy(item)
            return outbox[-2000:]
        return JSON_NO_CHANGE

    with authorized_business_write(
        source="system_job",
        operation_id=write_auth["operation_id"],
        ledger_id=write_auth["ledger_id"],
        audit_id=write_auth["audit_id"],
        allowed_files=allowed_files,
    ):
        store.update_json("notification_outbox.json", [], mutate)
    return final


def _sync_notification_outbox_receipts(store: TuoguanStore, item: dict[str, Any]) -> None:
    from .write_guard import authorized_business_write, prepare_system_write

    status = str(item.get("status") or "")
    if status not in {"sent", "failed", "retry_pending", "result_unknown", "suppressed"}:
        return
    allowed_files = {ATTENTION_THREADS_FILE, RELATIONSHIP_TOUCH_CANDIDATES_FILE, "daily_report_runs.jsonl", "action_executions.jsonl"}
    write_auth = prepare_system_write(store.data_dir, job_name="notification_outbox_delivery_receipts", allowed_files=allowed_files)
    with authorized_business_write(
        source="system_job",
        operation_id=write_auth["operation_id"],
        ledger_id=write_auth["ledger_id"],
        audit_id=write_auth["audit_id"],
        allowed_files=allowed_files,
    ):
        if str(item.get("attention_id") or "") and status in {"sent", "failed", "result_unknown"}:
            identity = _domain().identities.resolve(
                "wecom_callback",
                str(item.get("touser") or item.get("target_user_id") or ""),
                chat_id=str(item.get("touser") or item.get("target_user_id") or ""),
                message_text="",
            )
            update_attention_thread(
                store,
                identity=identity,
                attention_id=str(item.get("attention_id") or ""),
                status=status,
                operation_id=write_auth["operation_id"],
                delivery_receipt=_delivery_receipt(item) if status == "sent" else None,
                failure_reason=str(item.get("last_error") or ""),
                source_text="企业微信主动提醒发送回执",
            )
        if str(item.get("relationship_touch_candidate_id") or "") and status in {"sent", "failed", "retry_pending", "result_unknown", "suppressed"}:
            identity = _domain().identities.resolve(
                "wecom_callback",
                str(item.get("touser") or item.get("target_user_id") or ""),
                chat_id=str(item.get("touser") or item.get("target_user_id") or ""),
                message_text="",
            )
            update_relationship_touch_candidate_status(
                store,
                identity=identity,
                candidate_id=str(item.get("relationship_touch_candidate_id") or ""),
                status="superseded" if status == "suppressed" else status,
                operation_id=write_auth["operation_id"],
                delivery_receipt=_delivery_receipt(item) if status == "sent" else None,
                failure_reason=str(item.get("last_error") or ""),
                source_text="企业微信关系经营消息发送回执",
            )
        if str(item.get("notification_type") or "") == "autonomous_daily_report" and status in {"sent", "failed", "retry_pending", "result_unknown"}:
            try:
                from .daily_reporter import sync_daily_report_delivery_status

                sync_daily_report_delivery_status(
                    store,
                    outbox_item=item,
                    operation_id=write_auth["operation_id"],
                    ledger_id=write_auth["ledger_id"],
                    audit_id=write_auth["audit_id"],
                )
                _sync_daily_report_action_execution(store, item, write_auth["operation_id"])
            except Exception:
                logger.exception("tuoguan_core failed to sync daily report delivery status")


def _delivery_receipt(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "message_id": str(item.get("message_id") or ""),
        "sent_at": str(item.get("sent_at") or ""),
        "outbox_id": str(item.get("id") or ""),
        "execution_receipt": deepcopy(item.get("execution_receipt") or {}),
    }


def _append_notification_audit(store: TuoguanStore, item: dict[str, Any], event: str, result: str) -> None:
    payload = {
        "audit_event_id": f"audit_notification_{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        "tenant_id": current_tenant_id(),
        "channel": "wecom_callback",
        "event": event,
        "action": str(item.get("action") or "notification"),
        "task_id": str(item.get("task_id") or ""),
        "target_user_id": str(item.get("touser") or ""),
        "result": result,
        "reason": str(item.get("last_error") or ""),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    store.append_jsonl_verified("business_action_audit.jsonl", payload)
    try:
        from .runtime_foundation import link_audit_to_ledger
        link_audit_to_ledger(store, str(item.get("ledger_id") or ""), payload["audit_event_id"])
    except Exception:
        logger.exception("tuoguan_core failed to link notification audit to reply ledger")


def _sync_daily_report_action_execution(store: TuoguanStore, item: dict[str, Any], operation_id: str) -> None:
    if str(item.get("notification_type") or "") != "autonomous_daily_report":
        return
    status = str(item.get("status") or "")
    if status not in {"sent", "failed", "retry_pending", "result_unknown"}:
        return
    idempotency_key = f"daily_report_delivery:{item.get('id')}:{status}:{item.get('message_id') or item.get('last_error') or ''}"
    path = store.path_for("action_executions.jsonl")
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines()[-300:]:
            if idempotency_key and idempotency_key in line:
                return
    try:
        from .digital_employee_state import submit_action_execution
        from .models import UserIdentity

        identity = UserIdentity(
            platform="system",
            platform_user_id="notification_outbox_delivery",
            canonical_user_id="notification_outbox_delivery",
            person_name="Hermes",
            role="boss",
            approval_state="approved",
        )
        submit_action_execution(
            store,
            identity=identity,
            action_type="daily_report_delivery",
            action_summary=f"Hermes daily report delivery status synchronized: {status}.",
            status="success" if status == "sent" else ("failed" if status == "failed" else "result_unknown"),
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            receipt={
                "notification_id": str(item.get("id") or ""),
                "delivery_status": status,
                "message_id": str(item.get("message_id") or ""),
                "sent_at": str(item.get("sent_at") or ""),
                "last_error": str(item.get("last_error") or ""),
            },
            result_text=str(item.get("summary") or item.get("content") or "")[:500],
            source_text="notification_outbox_delivery",
            source_message_id=str(item.get("id") or ""),
        )
    except Exception:
        logger.exception("tuoguan_core failed to sync daily report action execution")


def _ensure_daily_push_loop_for_adapter(adapter: Any) -> None:
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    wake_event = _DAILY_PUSH_WAKE_EVENTS.get(loop_id)
    if wake_event is None:
        wake_event = asyncio.Event()
        _DAILY_PUSH_WAKE_EVENTS[loop_id] = wake_event
    existing = _DAILY_PUSH_TASKS.get(loop_id)
    if existing is not None and not existing.done():
        # A business command may have appended a high-priority notification
        # while the worker is waiting for its 60-second fallback tick.
        wake_event.set()
        return
    _DAILY_PUSH_TASKS[loop_id] = loop.create_task(_daily_push_loop(adapter, wake_event))


def _ensure_daily_push_loop(gateway: Any, event: Any) -> None:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    adapter = getattr(gateway, "adapters", {}).get(platform)
    if adapter is not None:
        _ensure_daily_push_loop_for_adapter(adapter)


def _on_post_gateway_start(**kwargs: Any) -> None:
    gateway = kwargs.get("gateway")
    for platform, adapter in getattr(gateway, "adapters", {}).items():
        if str(getattr(platform, "value", platform)) == "wecom_callback":
            _ensure_daily_push_loop_for_adapter(adapter)
            logger.info("tuoguan_core notification worker started with gateway")
            return


def _schedule_reply(
    gateway: Any,
    event: Any,
    reply: str,
    outbound_metadata: dict[str, Any] | None = None,
) -> bool:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    adapter = getattr(gateway, "adapters", {}).get(platform)
    chat_id = str(getattr(source, "chat_id", "") or "")
    if adapter is None or not chat_id:
        logger.warning("tuoguan_core cannot send reply: missing adapter or chat_id")
        return False
    message_id = str(getattr(event, "message_id", "") or "")
    now = datetime.now().astimezone()
    for claimed_id, claimed_at in list(_CLAIMED_REPLY_MESSAGE_IDS.items()):
        normalized_claimed_at = _coerce_runtime_datetime(claimed_at, now=now)
        if normalized_claimed_at is None or now - normalized_claimed_at > _REPLY_CLAIM_TTL:
            _CLAIMED_REPLY_MESSAGE_IDS.pop(claimed_id, None)
    if message_id and message_id in _CLAIMED_REPLY_MESSAGE_IDS:
        logger.critical("duplicate business reply blocked source_message_id=%s", message_id)
        return False
    if message_id:
        _CLAIMED_REPLY_MESSAGE_IDS[message_id] = now
    asyncio.get_running_loop().create_task(
        _send_reply(adapter, chat_id, reply, event, outbound_metadata)
    )
    return True


def _schedule_notifications(gateway: Any, event: Any, notifications: list[dict[str, Any]]) -> None:
    if not notifications:
        return
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    adapter = getattr(gateway, "adapters", {}).get(platform)
    if adapter is None:
        logger.warning("tuoguan_core cannot send notifications: missing adapter")
        return
    asyncio.get_running_loop().create_task(_send_notifications(adapter, notifications))


def _append_handled_inbound_to_transcript(session_store: Any, event: Any) -> None:
    """Persist user input skipped by the normal model pipeline."""

    if session_store is None or not hasattr(session_store, "get_or_create_session"):
        return
    try:
        source = event.source
        entry = session_store.get_or_create_session(source)
        message_id = str(getattr(event, "message_id", "") or "")
        if message_id and hasattr(session_store, "load_transcript"):
            recent = session_store.load_transcript(entry.session_id)[-8:]
            if any(
                isinstance(item, dict)
                and item.get("role") == "user"
                and str(item.get("message_id") or "") == message_id
                for item in recent
            ):
                return
        session_store.append_to_transcript(
            entry.session_id,
            {
                "role": "user",
                "content": str(getattr(event, "text", "") or ""),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "message_id": message_id,
                "source": "deterministic_business_route",
                "visible_to_model": True,
            },
        )
    except Exception:
        logger.exception("tuoguan_core failed to append handled inbound transcript")


def _on_pre_gateway_dispatch(**kwargs: Any) -> dict[str, str] | None:
    # Retired by the model-led mainline hardening. This hook used to own
    # ordinary Enterprise WeChat business messages before the Agent could
    # understand them. It intentionally remains a no-op even if accidentally
    # registered again; identity, permissions and writes are enforced by tool
    # services after the model chooses an action.
    return None


def _on_pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    # Model-led production still needs an active runtime context so trusted
    # write tools can attach ledger/audit ids. It only returns a narrow
    # current-turn write rule for explicit write requests, preserving the
    # natural model mainline for ordinary conversation.
    platform_raw = kwargs.get("platform")
    platform = str(getattr(platform_raw, "value", platform_raw) or "").lower()
    if not _is_trusted_model_channel(platform):
        return None
    sender_id = str(kwargs.get("sender_id") or "").strip()
    # Reply recovery has no public/user ingress. Its platform adapter creates
    # the event only from a durable, server-attested verified receipt, and its
    # public source identity is fixed. Do not manufacture a human identity or
    # attach normal business context for this zero-Tool expression-only turn.
    if platform == "reply_recovery":
        try:
            from .direct_reply_recovery import RECOVERY_SENDER, get_direct_reply_recovery_manager

            session_id = str(kwargs.get("session_id") or "")
            prompt = get_direct_reply_recovery_manager().recovery_prompt(
                recovery_session_id=session_id,
                sender_id=sender_id,
            )
            if prompt is None or sender_id != RECOVERY_SENDER:
                logger.error("XIAOYOU_REPLY_RECOVERY_UNTRUSTED_INGRESS session_id=%s", session_id)
                return None
            _record_trace_guard_event(session_id, guard="reply_only_recovery", result="trusted_zero_tool_context")
            return {"context": prompt}
        except Exception:
            logger.exception("XIAOYOU_REPLY_RECOVERY_CONTEXT_FAILED")
            return None
    if platform != "agenda_service_work" and ":" in sender_id:
        sender_id = sender_id.split(":", 1)[1].strip()
    raw_text = str(kwargs.get("user_message") or "")
    session_id = str(kwargs.get("session_id") or "")
    hook_turn_id = str(kwargs.get("turn_id") or "").strip()
    # A Robot turn was already authenticated by the device registry before it
    # entered Hermes.  The public pre-LLM hook may use a different internal
    # turn id, so reuse that one active transport record after checking actor
    # and channel rather than relying on private gateway request state.
    trusted_turn = _activate_trusted_turn(session_id=session_id)
    if trusted_turn is None and platform == "robot_poc":
        # The documented lifecycle hook exposes Core's opaque agent session,
        # not the adapter's operation id.  Reattach only the one verified
        # device ingress with the same trusted sender and exact content hash.
        # Ambiguity fails closed; no Core-private request state is consulted.
        trusted_turn = _claim_robot_turn_for_agent(
            actor_user_id=sender_id,
            raw_text=raw_text,
            agent_session_id=session_id,
            agent_turn_id=hook_turn_id,
        )
    if trusted_turn is None and platform == "agenda_service_work":
        trusted_turn = _claim_agenda_service_turn_for_agent(
            actor_user_id=sender_id,
            raw_text=raw_text,
            agent_session_id=session_id,
            agent_turn_id=hook_turn_id,
        )
    if trusted_turn is not None and (
        trusted_turn.platform != platform or trusted_turn.actor_user_id != sender_id
    ):
        logger.error("XIAOYOU_TRUSTED_TURN_MISMATCH platform=%s session_id=%s", platform, session_id)
        return None
    if trusted_turn is None and platform in {"robot_poc", "agenda_service_work"}:
        # These service-owned channels have no alternate identity authority. If the public
        # hook cannot correlate it to a single device-registry ingress, leave
        # it without Tool authority rather than manufacturing a tenant/role
        # record from Hermes' opaque runtime. ``pre_tool_call`` will fail
        # closed; no client or model value can repair this condition.
        logger.error("XIAOYOU_TRUSTED_TURN_UNCORRELATED platform=%s session_id=%s", platform, session_id)
        return None
    if trusted_turn is None:
        conversation_scope = _trusted_conversation_scope(
            platform=platform,
            sender_id=sender_id,
            hook_values=kwargs,
        )
        trusted_turn = _bind_trusted_turn(
            platform=platform,
            actor_user_id=sender_id,
            session_id=session_id,
            turn_id=hook_turn_id,
            message_id=hook_turn_id,
            chat_id=conversation_scope or session_id,
            tenant_id=current_tenant_id(),
            source="hermes_pre_llm_hook",
        )
    # Business-object continuity belongs to XiaoYou's public Runtime Contract.
    # Hermes is free to rotate or replace its opaque Agent-session ID.
    session_object_key = str(getattr(trusted_turn, "continuity_key", "") or "")
    chat_id = str(getattr(trusted_turn, "chat_id", "") or session_id)
    message_id = str(getattr(trusted_turn, "message_id", "") or hook_turn_id)
    if not message_id:
        message_id = hashlib.sha256(f"{session_id}\n{raw_text}".encode("utf-8")).hexdigest()[:24]
    if trusted_turn is None or not raw_text:
        logger.warning(
            "YOUYI_PRE_LLM_CONTEXT_SKIPPED trusted_turn_empty=%s raw_empty=%s platform=%s session_id=%s",
            trusted_turn is None,
            not bool(raw_text),
            platform,
            session_id,
        )
        return None
    if platform == "agenda_service_work":
        # The public service ingress has already attested the ticket/identity.
        # Bind its immutable partition and boss destination to the existing
        # durable reply bridge before the model can reach any Tool. If this
        # correlation is unavailable, fail closed: an uncorrelated service
        # turn may express nothing but must never receive Tool authority.
        try:
            from .agenda_runtime import claimed_ticket_context
            from .direct_reply_recovery import get_direct_reply_recovery_manager

            ticket_context = claimed_ticket_context(
                database_path=str(os.getenv("XIAOYOU_AGENDA_SERVICE_INGRESS_DB") or ""),
                tenant_id=str(trusted_turn.tenant_id),
                service_identity=str(trusted_turn.actor_user_id),
                message_id=message_id,
                raw_text=raw_text,
                agent_session_id=session_id,
                agent_turn_id=hook_turn_id,
            )
            if ticket_context is None:
                raise RuntimeError("agenda_service_ticket_reply_binding_missing")
            get_direct_reply_recovery_manager().capture_agenda_turn(
                ticket_id=str(ticket_context["ticket_id"]),
                agent_session_id=session_id,
                agent_turn_id=hook_turn_id,
                tenant_id=str(trusted_turn.tenant_id),
                service_identity=str(trusted_turn.actor_user_id),
                partition=ticket_context["partition"],
                destination=ticket_context["destination"],
                raw_text=raw_text,
                operation_hint=str(ticket_context["operation_hint"]),
            )
        except Exception:
            logger.exception("XIAOYOU_AGENDA_REPLY_BINDING_FAILED session_id=%s", session_id)
            _clear_trusted_turn(session_id=session_id)
            return None
    if platform in {"robot_poc", "wecom_callback"}:
        try:
            # This stores only the already authenticated direct-turn facts;
            # it neither inspects the utterance for intent nor decides whether
            # a reply recovery will be needed. A later public receipt event is
            # the only path that can make it eligible for reply recovery.
            from .direct_reply_recovery import get_direct_reply_recovery_manager

            get_direct_reply_recovery_manager().capture_direct_turn(
                agent_session_id=session_id,
                raw_text=raw_text,
                trusted_turn=trusted_turn,
            )
        except Exception:
            logger.exception("XIAOYOU_DIRECT_REPLY_TURN_CAPTURE_FAILED session_id=%s", session_id)
    try:
        identity = trusted_turn.identity
        turn = _foundation_begin_inbound(
            store=_domain().store,
            message_id=message_id,
            conversation_id=chat_id or session_id or identity.canonical_user_id,
            user_id=identity.canonical_user_id,
            role=identity.role,
            raw_text=raw_text,
            actor_name=identity.person_name,
            tenant_id=trusted_turn.tenant_id,
            channel=trusted_turn.platform,
        )
        if platform == "robot_poc":
            # The Robot adapter opened this same trusted message before
            # Hermes optionally prepended native Skill instructions.  Keep
            # business grounding, scope inference, write guards and ledgers on
            # the teacher's original utterance while the model still receives
            # the full Skill-augmented message through Hermes itself.
            trusted_transport_text = _foundation_current_raw_text(identity.canonical_user_id)
            if trusted_transport_text:
                raw_text = trusted_transport_text
        activity_key = (str(_domain().store.data_dir.resolve()), identity.canonical_user_id)
        _ACTIVE_WECom_USERS[activity_key] = datetime.now().astimezone()
        turn_key = session_id or chat_id or identity.canonical_user_id
        _reset_turn_tool_budget(turn_key)
        _begin_turn_trace(
            session_id=turn_key,
            message_id=message_id,
            tenant_id=trusted_turn.tenant_id,
            app_id=platform,
            user_id=identity.canonical_user_id,
            role=identity.role,
            raw_text=raw_text,
            visible_tool_count=_REGISTERED_TOOL_COUNT,
        )
        # Durable, content-free proof that the Tool-facing identity came from
        # the Runtime Contract rather than a Core session field or model/client
        # argument.  Certification asserts Robot turns use the device-registry
        # bridge; other supported channels retain their public hook source.
        _record_trace_guard_event(
            turn_key,
            guard="trusted_turn_provenance",
            result=str(trusted_turn.source or "unknown"),
        )
        _record_trace_guard_event(
            turn_key,
            guard="trusted_business_object_continuity",
            result="server_attested_channel_scope",
        )
        _ACTIVE_MODEL_TURNS[turn_key] = {
            "message_id": message_id,
            "conversation_id": chat_id or session_id or identity.canonical_user_id,
            "user_id": identity.canonical_user_id,
            "role": identity.role,
            "raw_text": raw_text,
            "created_at": datetime.now().astimezone(),
        }
        _bind_turn_fence_session(
            message_id=message_id,
            session_id=turn_key,
            chat_id=chat_id,
        )
        _mark_turn_fence_phase(session_id=turn_key, phase="model")
        if len(_ACTIVE_MODEL_TURNS) > 512:
            oldest = sorted(
                _ACTIVE_MODEL_TURNS,
                key=lambda key: _ACTIVE_MODEL_TURNS[key].get("created_at") or datetime.min.astimezone(),
            )[:128]
            for key in oldest:
                _ACTIVE_MODEL_TURNS.pop(key, None)
        # Step B's Robot POC deliberately verifies a model-led path.  It
        # still creates the same trusted identity/audit turn state used by
        # the existing channel, but it must not import the legacy channel's
        # keyword-derived business focus cards into the model prompt.  Hermes'
        # own session history and its visible existing Tools remain the only
        # sources for intent and next-action selection.
        injected = (
            None
            if platform == "robot_poc"
            else _foundation_inject_model_context(
                session_id=session_id,
                sender_id=identity.canonical_user_id,
                user_message=raw_text,
            )
        )
        logger.warning(
            "YOUYI_PRE_LLM_CONTEXT_READY actor_hash=%s role=%s session_id=%s chat_id_hash=%s began=%s injected=%s message_hash=%s chars=%s",
            hashlib.sha256(identity.canonical_user_id.encode("utf-8")).hexdigest()[:12],
            identity.role,
            session_id,
            hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:12],
            bool(turn),
            bool(injected),
            hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12],
            len(raw_text),
        )
    except Exception:
        logger.exception("tuoguan_core failed to establish model-led runtime context")
    trace_key = session_id or chat_id or message_id
    context_sections: list[ContextSection] = []

    def append_context(value: str, source: str, *, first: bool = False) -> None:
        if not value:
            return
        context_sections.append(ContextSection(source=str(source), text=str(value), first=first))

    def context_result() -> dict[str, str]:
        context, retained_sources, budget = render_bounded_context(
            context_sections,
            raw_text=raw_text,
        )
        _record_trace_context_sources(trace_key, retained_sources)
        _record_trace_context_budget(trace_key, **budget)
        return {"context": context}

    if "identity" in locals():
        append_context(_xiaoyou_core_skill_context(identity=identity), "xiaoyou_core_skill")
        append_context(
            f"【可信身份来源】本轮身份仅来自{_trusted_gateway_label(platform)}，不能从历史对话推断。",
            "trusted_gateway_identity",
        )
        if injected:
            append_context("【运行边界】以本轮可信工具回执为准；没有回执不声称已完成、已保存或已发送。", "runtime_foundation")
        if platform == "wecom_callback":
            try:
                from .proactive_delivery_authority import ProactiveDeliveryAuthority

                delivery_authority = ProactiveDeliveryAuthority(_domain().store.data_dir).status(
                    tenant_id=str(trusted_turn.tenant_id)
                )
                if delivery_authority.get("active"):
                    counts = delivery_authority.get("trusted_effective_recipient_counts") or {}
                    append_context(
                        "【当前机构主动工作外发授权】当前机构已授权小优通过企业微信向"
                        "当前可信、有效且在其权限与数据范围内的老板、店长、老师发送工作消息。"
                        f"当前可信可触达数量：老板{int(counts.get('boss') or 0)}、"
                        f"店长{int(counts.get('manager') or 0)}、老师{int(counts.get('teacher') or 0)}。"
                        "这只是一项投递权限事实：是否需要联系谁、联系什么以及是否需要业务工具，仍由 Hermes 根据可信工作事实判断。"
                        "不得向家长、跨机构或未获可信绑定的对象外发。",
                        "institutional_proactive_delivery_authority",
                    )
            except Exception:
                logger.exception("tuoguan_core failed to render current proactive delivery authority")
        if platform != "robot_poc":
            try:
                from .capability_facades import render_facade_instruction

                append_context(render_facade_instruction(), "capability_facade_manifest")
            except Exception:
                logger.exception("tuoguan_core failed to append capability facade contract")
    if platform == "robot_poc":
        # The adapter is a trusted text transport only.  Do not add a second
        # Robot-specific business context, intent hint, reply template or
        # keyword route; the live Hermes session/model chooses whether to
        # clarify, read facts or invoke a write Tool.  The normal Tool service
        # boundaries remain authoritative for permission and receipts.
        append_context(
            "【Robot Channel 模型主导】本轮文本未经业务分类或字段提取直接交给模型。"
            "模型根据当前 Hermes 会话理解自然语言、自主选择已有可信工具并自然回复；"
            "当前 POC 暴露的是具体可信 Tool，不存在领域 Facade 名称或 operation/arguments 包装；"
            "以每个可见 Tool 的 schema 为准。"
            "系统只执行可信身份、权限、幂等、写后核验与真实回执边界。",
            "robot_model_led_boundary",
        )
        # This is an output-medium instruction for the model, not a Robot
        # intent classifier or reply rewriter.  The local PC client speaks the
        # terminal reply verbatim, so the model itself must decide the useful
        # conclusion and preserve all of the normal evidence/clarification
        # boundaries.  It applies to the isolated Robot Channel uniformly;
        # the adapter never inspects a user's words to select a style.
        append_context(
            "【Robot Channel 语音呈现】本轮最终回复可能被直接朗读。对普通查询、确认或澄清，"
            "先自然说最重要的真实结论，通常用一到三句短句；必要细节可在对方追问时继续展开。"
            "不要为了简短省略安全提醒、关键歧义或真实工具结果，也不要把未验证的进度、写入或完成说成事实。"
            "这是对呈现方式的要求，不替你判断意图、选择工具或生成固定业务答复。",
            "robot_voice_delivery_contract",
        )
        try:
            from .business_object_context import render_session_object_context

            # This is a bounded replay of facts returned by prior trusted
            # Tools in the same Hermes session.  It contains no intent label,
            # text matching, Tool choice or business reply; the model alone
            # decides whether the new natural-language turn continues an
            # object, and every Tool revalidates it before execution.
            append_context(
                render_session_object_context(
                    _domain().store,
                    identity,
                    session_key=session_object_key,
                ),
                "trusted_business_object_context",
            )
        except Exception:
            logger.exception("tuoguan_core failed to render Robot session object context")
        # Robot changes transport, not the employee's learning sources.  Feed
        # the same low-risk, scoped experience and verified workstyle context
        # into the real Hermes model, and retain the exact selected lesson IDs
        # in active trace memory.  Neither source chooses a business Tool.
        try:
            self_evolution_bundle = _self_evolution_context(
                _domain().store,
                identity=identity,
            )
            self_evolution_context = str(self_evolution_bundle.get("text") or "") if isinstance(self_evolution_bundle, dict) else ""
            if self_evolution_context:
                append_context(self_evolution_context, "self_evolution")
                _record_trace_context_selection(
                    session_id,
                    source="self_evolution",
                    identifiers=(self_evolution_bundle.get("application_ids") or []) if isinstance(self_evolution_bundle, dict) else [],
                )
        except Exception:
            logger.exception("tuoguan_core failed to append Robot self-evolution context")
        try:
            workstyle_context = _workstyle_context(
                _domain().store,
                identity=identity,
                raw_text=raw_text,
            )
            if workstyle_context:
                append_context(workstyle_context, "person_workstyle")
                _record_trace_context_selection(
                    session_id,
                    source="person_workstyle",
                    identifiers=_workstyle_selection_ids(
                        _domain().store,
                        identity=identity,
                        raw_text=raw_text,
                    ),
                )
                if "本轮可能包含服务方式反馈" in workstyle_context:
                    append_context(
                        "【服务方式保存守卫】低风险工作方式反馈可调用 "
                        "tuoguan_submit_person_workstyle_preference 保存。"
                        "未看到工具 ok=true 且 writeback_verified=true 前，不得说"
                        "“已保存、记住了、以后按这个来”。",
                        "workstyle_feedback_contract",
                    )
        except Exception:
            logger.exception("tuoguan_core failed to append Robot workstyle context")
        return context_result()
    try:
        if "identity" in locals():
            _record_owner_inbound_fact(
                _domain().store,
                identity=identity,
                raw_text=raw_text,
                message_id=message_id,
                session_id=session_id,
            )
        recent_outbound_context = _recent_owner_outbound_context(
            _domain().store,
            identity=identity,
            current_message=raw_text,
        ) if "identity" in locals() else ""
        if recent_outbound_context:
            append_context(recent_outbound_context, "recent_owner_outbound")
            logger.warning(
                "YOUYI_RECENT_OUTBOUND_CONTEXT_READY actor_hash=%s session_id=%s message_hash=%s",
                hashlib.sha256(sender_id.encode("utf-8")).hexdigest()[:12],
                session_id,
                hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12],
            )
        recent_outbound_follow_up = bool(
            recent_outbound_context
            and _looks_like_recent_outbound_follow_up(raw_text)
        )
        owner_attention_context = ""
        if "identity" in locals() and not recent_outbound_follow_up:
            owner_attention_context = _open_owner_attention_context(
                _domain().store,
                identity=identity,
                current_message=raw_text,
            )
        if owner_attention_context:
            append_context(owner_attention_context, "owner_attention")
            logger.warning(
                "YOUYI_OWNER_ATTENTION_CONTEXT_READY actor_hash=%s session_id=%s message_hash=%s",
                hashlib.sha256(sender_id.encode("utf-8")).hexdigest()[:12],
                session_id,
                hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12],
            )
        term_context = _term_boundary_context(
            _domain().store,
            raw_text=raw_text,
            include_for_attention=bool(owner_attention_context),
        )
        if term_context:
            append_context(term_context, "academic_term")
    except Exception:
        logger.exception("tuoguan_core failed to recall owner attention context")
    try:
        identity_context = _public_identity_context(_domain().store)
        if identity_context:
            append_context(identity_context, "public_employee_identity", first=True)
    except Exception:
        logger.exception("tuoguan_core failed to recall public identity context")
    try:
        if "identity" in locals():
            temporal_context = build_temporal_grounding_context(
                _domain().store,
                identity=identity,
                raw_text=raw_text,
                session_id=session_id,
                chat_id=chat_id,
                platform=platform,
            )
            if temporal_context:
                append_context(temporal_context, "temporal_grounding")
    except Exception:
        logger.exception("tuoguan_core failed to append temporal grounding context")
    try:
        if "identity" in locals():
            self_evolution_bundle = _self_evolution_context(
                _domain().store,
                identity=identity,
            )
            self_evolution_context = str(self_evolution_bundle.get("text") or "") if isinstance(self_evolution_bundle, dict) else ""
            if self_evolution_context:
                append_context(self_evolution_context, "self_evolution")
                _record_trace_context_selection(
                    session_id,
                    source="self_evolution",
                    identifiers=(self_evolution_bundle.get("application_ids") or []) if isinstance(self_evolution_bundle, dict) else [],
                )
    except Exception:
        logger.exception("tuoguan_core failed to append self-evolution context")
    try:
        if "identity" in locals():
            workstyle_context = _workstyle_context(
                _domain().store,
                identity=identity,
                raw_text=raw_text,
            )
            if workstyle_context:
                append_context(workstyle_context, "person_workstyle")
                _record_trace_context_selection(
                    session_id,
                    source="person_workstyle",
                    identifiers=_workstyle_selection_ids(
                        _domain().store,
                        identity=identity,
                        raw_text=raw_text,
                    ),
                )
                if "本轮可能包含服务方式反馈" in workstyle_context:
                    # This is a persistent behavior change, not optional
                    # explanatory prose.  Keep the commitment guard separate
                    # from the longer profile so context trimming cannot make
                    # the model promise a change it did not verify.
                    append_context(
                        "【服务方式保存守卫】低风险工作方式反馈可调用 "
                        "tuoguan_submit_person_workstyle_preference 保存。"
                        "未看到工具 ok=true 且 writeback_verified=true 前，不得说"
                        "“已保存、记住了、以后按这个来”。",
                        "workstyle_feedback_contract",
                    )
    except Exception:
        logger.exception("tuoguan_core failed to append workstyle context")
    try:
        if "identity" in locals():
            role_layer_context = _role_layer_context(
                identity=identity,
                raw_text=raw_text,
            )
            if role_layer_context:
                append_context(role_layer_context, "role_layer")
    except Exception:
        logger.exception("tuoguan_core failed to append role layer context")
    try:
        if "identity" in locals():
            from .tasks import task_companion_context

            companion_context = task_companion_context(
                _domain().store,
                identity=identity,
                raw_text=raw_text,
            )
            if companion_context:
                append_context(companion_context, "task_companion")
    except Exception:
        logger.exception("tuoguan_core failed to append current task companion context")
    work_snapshot: dict[str, Any] = {}
    active_context = ""
    has_active_context = False
    try:
        if "identity" in locals():
            from .work_context_snapshot import build_work_context_snapshot, render_work_context_snapshot

            work_snapshot = build_work_context_snapshot(
                _domain().store,
                identity=identity,
                platform=platform,
                app_id=platform,
                session_id=session_id or chat_id,
                message_id=message_id,
                limit=6,
            )
            active_context = render_work_context_snapshot(work_snapshot)
            has_active_context = bool(work_snapshot.get("candidate_threads"))
            append_context(active_context, "work_context_snapshot")
            _foundation_record_work_context_snapshot(
                session_id=turn_key,
                snapshot=work_snapshot,
            )
            _record_trace_guard_event(
                trace_key,
                guard="work_context_ambiguity",
                result=str(work_snapshot.get("ambiguity_state") or "unknown"),
            )
    except Exception:
        logger.exception("tuoguan_core failed to build work context snapshot")
        active_context = ""
        has_active_context = False
    compact_raw = "".join(raw_text.split())
    short_context_reference = compact_raw in {
        "你再试一下",
        "再试一下",
        "试一下",
        "继续",
        "继续吧",
        "好的",
        "好",
        "嗯",
        "对",
        "可以",
        "什么意思",
        "啥意思",
        "什么意思啊",
        "这个",
        "刚才那个",
        "上面那个",
        "展开",
        "展开一下",
        "详细说",
        "说重点",
        "关掉",
        "关闭",
        "取消",
        "不用了",
        "不用再提醒",
    }
    asks_how_to_confirm = any(term in compact_raw for term in ("怎么确认", "如何确认", "需要我怎么确认", "你需要我怎么确认"))
    task_completion_like = (
        compact_raw in {
            "这个任务已经完成",
            "这个任务完成了",
            "刚才那个任务完成了",
            "任务已经完成",
            "任务完成了",
            "这个任务已经处理了",
            "任务已经处理了",
            "已经处理了",
            "完成了",
            "处理完了",
        }
        or ("任务" in compact_raw and any(term in compact_raw for term in ("完成", "处理完", "闭环", "关掉", "关闭")))
    )
    write_like = any(
        term in compact_raw
        for term in (
            "加", "扣", "减", "积分", "兑换", "拍卖",
            "记录", "登记", "创建", "新建", "完成任务", "更新任务",
            "确认", "提交", "上报", "修改", "改成", "设为",
            "记住", "以后按", "以后就按", "工作方式", "偏好", "汇报格式",
            "取消任务", "关闭任务", "删除任务", "关掉", "不用再提醒", "停止提醒",
            "离职", "删除老师", "删除员工", "移除老师", "移除员工", "停用老师", "停用员工",
        )
    ) and not asks_how_to_confirm
    write_like = write_like or (task_completion_like and not asks_how_to_confirm)
    if short_context_reference and not write_like:
        if has_active_context:
            append_context(
                "【短回复衔接规则】短回复本身不是拒绝执行的理由。先结合本轮原话、最近主动外发和上述活动线程判断指向；"
                "“什么意思/这个/展开”优先解释最近活动；“继续/可以”优先沿最近活动推进；"
                "若快照已经给出唯一、新鲜且参与人匹配的锚点，直接围绕该锚点回答，不要再遍历任务、日报、attention 或员工声音等查询工具；"
                "只有要写入、取消、外发或快照明确缺少必要事实时才调用一个最匹配的工具。"
                "若多个线程同样可能或没有任何证据，只追问一个最关键的区分问题。",
                "short_reply_contract",
            )
        else:
            append_context(
                "【短回复衔接规则】当前没有可验证的活动线程。不要猜测历史对象；只追问一个最关键的区分问题。"
                , "short_reply_contract"
            )
        return context_result()
    if write_like:
        append_context(
            "【示例机构当前轮写入规则】如果用户本轮明确要求记录、修改、加扣分、创建、完成、确认、提交或上报，"
            "必须调用对应 tuoguan_ 可信工具，以本轮工具结果为唯一执行依据。"
            "模型仍负责理解用户、判断是否追问、是否写入或是否先说明边界；系统只负责权限、审计、幂等和写后核验。"
            "如果要声明记录、修改、加扣分、创建、完成、确认、提交、上报、保存偏好、记住工作方式已经真实发生，必须先看到本轮可信工具返回成功。"
            "不要根据历史里的“写入被拦截/配置未生效/所有写入不能用”等旧结论直接拒绝或声称失败；"
            "用户明确要求取消、关闭、删除任务或停止任务提醒时，必须优先调用 tuoguan_cancel_task；"
            "tuoguan_update_task 只用于任务反馈、进展和完成闭环，不能用于取消任务。"
            "老板明确要求删除、移除或停用离职老师/店长时，必须使用 people 领域的 offboard_staff；"
            "该操作是保留历史的离职停用，不是删除企业微信组织通讯录。"
            "只有本轮工具返回 ok=false 时，才可以说明本轮未成功。"
            "写入成功必须来自工具 ok=true 且 writeback_verified=true；不要伪造成功。",
            "verified_write_contract",
        )
        return context_result()
    if context_sections:
        return context_result()
    _record_trace_context_sources(trace_key, [])
    return None


def _on_post_tool_call(**kwargs: Any) -> None:
    session_id = str(kwargs.get("session_id") or "")
    tool_name = str(kwargs.get("tool_name") or "")
    result = kwargs.get("result")
    _foundation_observe_tool_result(
        session_id=session_id,
        tool_name=tool_name,
        args=kwargs.get("args"),
        result=result,
    )
    _observe_turn_tool_result(session_id, tool_name=tool_name, result=result)
    _observe_turn_fence_tool_result(session_id=session_id, result=result)
    _record_trace_tool_event(session_id, tool_name=tool_name, result=result, args=kwargs.get("args"))
    # Some Tool results are signed, opaque delivery artifacts.  Observe them
    # passively through the public lifecycle event so the final model turn
    # never has to transcribe a bearer URL.  This records no business success,
    # does not select a Tool, and is eligible only for the server-attested
    # direct turn that already owns this channel destination.
    try:
        from .direct_reply_recovery import get_direct_reply_recovery_manager

        trusted = _activate_trusted_turn(
            session_id=session_id,
            turn_id=str(kwargs.get("turn_id") or ""),
        )
        trusted_turn_id = (
            str(getattr(trusted, "turn_id", "") or "")
            if trusted is not None else ""
        )
        artifact_job = get_direct_reply_recovery_manager().stage_tool_delivery_artifact(
            agent_session_id=session_id,
            agent_turn_id=str(kwargs.get("turn_id") or ""),
            trusted_turn_id=trusted_turn_id,
            tool_name=tool_name,
            result=result,
        )
        if artifact_job is not None:
            logger.info(
                "XIAOYOU_TOOL_DELIVERY_ARTIFACT_HELD session_id=%s hook_turn=%s trusted_turn=%s reply_id=%s",
                session_id,
                str(kwargs.get("turn_id") or "")[:24],
                trusted_turn_id[:24],
                artifact_job.reply_id,
            )
            _record_trace_guard_event(
                session_id,
                guard="tool_delivery_artifact",
                result="held_for_trusted_channel",
            )
    except Exception:
        logger.exception("XIAOYOU_TOOL_DELIVERY_ARTIFACT_OBSERVER_FAILED session_id=%s tool=%s", session_id, tool_name)
    # The Work Runtime's optional receipt listener is a passive consumer of
    # this public Hermes lifecycle event.  It receives the original Tool
    # result before trace sanitization and cannot change dispatch, result,
    # Permission, CommandBus or reply behaviour.
    try:
        from .work_runtime_receipts import observe_public_post_tool_call, receipt_event_from_public_hook

        observe_public_post_tool_call(**kwargs)
        # Direct-channel recovery copies only the final Tool-owned receipt
        # from this documented passive event.  It cannot rewrite the Tool
        # result, select a Tool, grant a permission, or decide business
        # success; all of that happened before this observer runs.
        receipt_event = receipt_event_from_public_hook(**kwargs)
        if receipt_event is not None:
            from .direct_reply_recovery import get_direct_reply_recovery_manager

            get_direct_reply_recovery_manager().observe_verified_receipt(receipt_event)
    except Exception:
        logger.exception("tuoguan_core passive work-runtime receipt observer failed")
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        parsed = None
    _record_trace_progress_event(
        session_id,
        kind="tool_result_available",
        source="tool",
        tool_name=tool_name,
        outcome="succeeded" if isinstance(parsed, dict) and parsed.get("ok") is True else "failed",
    )


def _on_transform_tool_result(**kwargs: Any) -> str | None:
    """Keep full tool audit data while bounding only the model-visible copy."""

    replacement = _foundation_compact_tool_result_for_model(
        tool_name=str(kwargs.get("tool_name") or ""),
        args=kwargs.get("args"),
        result=kwargs.get("result"),
    )
    session_id = str(kwargs.get("session_id") or "")
    if replacement is not None:
        _record_trace_tool_result_projection(
            session_id,
            tool_name=str(kwargs.get("tool_name") or ""),
            original_chars=len(str(kwargs.get("result") or "")),
            projected_chars=len(replacement),
        )
        _record_trace_guard_event(
            session_id,
            guard="tool_result_projection",
            result=f"{len(str(kwargs.get('result') or ''))}->{len(replacement)}",
        )
    return replacement


def _on_pre_api_request(**kwargs: Any) -> None:
    platform_raw = kwargs.get("platform")
    platform = str(getattr(platform_raw, "value", platform_raw) or "").lower()
    if not _is_trusted_model_channel(platform):
        return
    session_id = _resolve_trace_session(
        str(kwargs.get("session_id") or ""),
        str(kwargs.get("turn_id") or ""),
    )
    turn_id = str(kwargs.get("turn_id") or "")
    _mark_turn_fence_phase(session_id=session_id, phase="model")
    _record_trace_provider_event(
        session_id,
        provider=str(kwargs.get("provider") or ""),
        model=str(kwargs.get("model") or ""),
        outcome="request_started",
        error_class="",
        circuit_state="",
        network_egress=(
            str(os.getenv("HERMES_AGNES_EGRESS_LABEL") or "legacy_local_proxy")
            if "agnes" in str(kwargs.get("model") or "").lower()
            else "direct_or_configured_fallback"
        ),
        attempt_metadata={
            "api_call_count": kwargs.get("api_call_count"),
            "approx_input_tokens": kwargs.get("approx_input_tokens"),
            "request_char_count": kwargs.get("request_char_count"),
            "max_tokens": kwargs.get("max_tokens"),
            "api_mode": kwargs.get("api_mode"),
        },
    )
    _record_trace_progress_event(
        session_id,
        kind="model_request_started",
        source="provider",
    )


def _on_post_api_request(**kwargs: Any) -> None:
    platform_raw = kwargs.get("platform")
    platform = str(getattr(platform_raw, "value", platform_raw) or "").lower()
    if not _is_trusted_model_channel(platform):
        return
    model = str(kwargs.get("model") or "")
    if "agnes" in model.lower():
        _observe_provider_success()
    session_id = _resolve_trace_session(
        str(kwargs.get("session_id") or ""),
        str(kwargs.get("turn_id") or ""),
    )
    # A later successful public provider event wins over an earlier transient
    # failure from the same Agent Loop.  This is observability state only.
    _TERMINAL_PROVIDER_FAILURES.pop(session_id, None)
    _record_trace_provider_event(
        session_id,
        provider=str(kwargs.get("provider") or ""),
        model=model,
        outcome="request_succeeded",
        error_class="",
        circuit_state="",
        network_egress=(
            str(os.getenv("HERMES_AGNES_EGRESS_LABEL") or "legacy_local_proxy")
            if "agnes" in str(kwargs.get("model") or "").lower()
            else "direct_or_configured_fallback"
        ),
        attempt_metadata={
            "api_call_count": kwargs.get("api_call_count"),
            "approx_input_tokens": kwargs.get("approx_input_tokens"),
            "request_char_count": kwargs.get("request_char_count"),
            "max_tokens": kwargs.get("max_tokens"),
            "api_duration_ms": round(float(kwargs.get("api_duration") or 0.0) * 1000, 3),
            "finish_reason": kwargs.get("finish_reason"),
            "assistant_content_chars": kwargs.get("assistant_content_chars"),
            "assistant_tool_call_count": kwargs.get("assistant_tool_call_count"),
            "api_mode": kwargs.get("api_mode"),
        },
    )


def _on_api_request_error(**kwargs: Any) -> None:
    platform_raw = kwargs.get("platform")
    platform = str(getattr(platform_raw, "value", platform_raw) or "").lower()
    if not _is_trusted_model_channel(platform):
        return
    raw_error = kwargs.get("error") if isinstance(kwargs.get("error"), dict) else {}
    error_type = str(
        kwargs.get("error_type")
        or raw_error.get("type")
        or kwargs.get("reason")
        or "provider_error"
    )
    status_code = str(kwargs.get("status_code") or "").strip()
    model = str(kwargs.get("model") or "")
    if "agnes" in model.lower():
        _observe_provider_failure(RuntimeError(error_type), status_code=status_code)
    session_id = _resolve_trace_session(
        str(kwargs.get("session_id") or ""),
        str(kwargs.get("turn_id") or ""),
    )
    turn_id = str(kwargs.get("turn_id") or "")
    _record_trace_provider_event(
        session_id,
        provider=str(kwargs.get("provider") or ""),
        model=model,
        outcome="request_failed",
        error_class=error_type,
        circuit_state="",
        network_egress=(
            str(os.getenv("HERMES_AGNES_EGRESS_LABEL") or "legacy_local_proxy")
            if "agnes" in str(kwargs.get("model") or "").lower()
            else "direct_or_configured_fallback"
        ),
        attempt_metadata={
            "api_call_count": kwargs.get("api_call_count"),
            "approx_input_tokens": kwargs.get("approx_input_tokens"),
            "request_char_count": kwargs.get("request_char_count"),
            "max_tokens": kwargs.get("max_tokens"),
            "api_duration_ms": round(float(kwargs.get("api_duration") or 0.0) * 1000, 3),
            "retry_count": kwargs.get("retry_count"),
            "max_retries": kwargs.get("max_retries"),
            "status_code": kwargs.get("status_code"),
            "api_mode": kwargs.get("api_mode"),
        },
    )
    # Hermes invokes this hook before it decides whether to retry, but v0.20
    # does not consistently expose retry counters to public hooks.  Record a
    # *pending* failure on every public error.  A later public success clears
    # it in ``_on_post_api_request``; if no such success arrives, the final
    # delivery lifecycle records a failed turn.  This observes Hermes rather
    # than altering its retry policy, provider choice, or business action.
    if session_id:
        if status_code:
            terminal_failure = f"provider_http_{status_code}"
        else:
            terminal_failure = f"provider_{error_type.lower()[:80]}"
        _TERMINAL_PROVIDER_FAILURES[session_id] = terminal_failure
        # In Hermes 0.21, a terminal transport exception is guaranteed to
        # surface through this public hook, but it does not necessarily reach
        # ``post_llm_call``.  Stage only a *reply* recovery observation here.
        # It returns no job until the same trusted direct turn has already
        # produced a completed, writeback-verified Receipt.  If Hermes' own
        # finite retry later succeeds, transform_llm_output fills this exact
        # held job with the normal final reply; otherwise the durable outbox
        # runs same-Hermes, zero-tool recovery.  Neither branch re-enters a
        # business Tool or changes a Receipt.
        if platform in {"robot_poc", "wecom_callback"}:
            _stage_direct_reply_terminal(
                session_id=session_id,
                turn_id=turn_id,
                terminal_state="failed",
                provider_succeeded=False,
                final_reply_text="",
                trace_ref="public-api-request-error:" + terminal_failure,
            )


def _on_llm_request_middleware(**kwargs: Any) -> dict[str, Any] | None:
    """Bound Agnes tool rounds without choosing a business action."""

    platform_raw = kwargs.get("platform")
    platform = str(getattr(platform_raw, "value", platform_raw) or "").lower()
    request = kwargs.get("request")
    if not _is_trusted_model_channel(platform) or not isinstance(request, dict):
        return None
    effective = deepcopy(request)
    effective["parallel_tool_calls"] = False
    reason = "single_business_tool_batch_requested"
    session_id = str(kwargs.get("session_id") or "")
    budget = _turn_tool_budget_snapshot(session_id)
    if _foundation_terminal_tool_result_recorded(session_id):
        effective.pop("tools", None)
        effective["tool_choice"] = "none"
        reason = "authoritative_result_requires_final_reply"
    elif budget["count"] >= budget["budget"]:
        effective.pop("tools", None)
        effective["tool_choice"] = "none"
        reason = "tool_budget_requires_final_reply"
    return {
        "request": effective,
        "source": "tuoguan_core",
        "reason": reason,
    }


def _on_pre_tool_call(**kwargs: Any) -> dict[str, str] | None:
    # This is a resource boundary, not a business router: the model still
    # chooses the tool and operation, while the runtime blocks exact repeats
    # and pathological tool loops that make a chat turn stall.
    session_id = str(kwargs.get("session_id") or "")
    tool_name = str(kwargs.get("tool_name") or "")
    args = kwargs.get("args")
    try:
        from .direct_reply_recovery import get_direct_reply_recovery_manager

        recovery = get_direct_reply_recovery_manager()
        if recovery.is_recovery_session(session_id):
            recovery.note_tool_attempt(recovery_session_id=session_id)
            _record_trace_guard_event(session_id, guard="reply_only_recovery_tool_surface", result="blocked")
            return {
                "action": "block",
                "reason": "reply_only_recovery_tools_forbidden",
                "message": "回复恢复回合没有工具权限，不能再次执行业务操作。",
            }
    except Exception:
        # This observer is not an authority for ordinary business turns. The
        # recovery platform additionally has a public zero-tool configuration;
        # log an unavailable observer rather than changing any existing Tool
        # dispatch semantics.
        logger.exception("XIAOYOU_REPLY_RECOVERY_TOOL_FENCE_CHECK_FAILED")
    if tool_name.startswith("tuoguan_"):
        trusted_turn = _activate_trusted_turn(
            session_id=session_id,
            turn_id=str(kwargs.get("turn_id") or ""),
        )
        if trusted_turn is None:
            _record_trace_guard_event(session_id, guard="trusted_turn_context", result="blocked")
            return {
                "action": "block",
                "reason": "missing_trusted_turn_context",
                "message": "当前 Tool 调用没有服务端可信身份上下文，本轮未执行。",
            }
    late_turn_reason = _turn_fence_block_reason(session_id=session_id)
    if late_turn_reason:
        _record_trace_guard_event(
            session_id,
            guard="turn_fence",
            result=late_turn_reason,
        )
        return {
            "action": "block",
            "reason": "expired_late_result_discarded",
            "message": "本轮已超时或已被新消息取代，系统不会再执行迟到的写入、外发或任务动作。",
        }
    _mark_turn_fence_phase(session_id=session_id, phase="tool")
    for boundary in (
        lambda: _foundation_block_tool_after_terminal_result(
            session_id=session_id, tool_name=tool_name, args=args,
        ),
        lambda: _foundation_validate_tool_call(
            session_id=session_id, tool_name=tool_name, args=args,
        ),
        lambda: _guard_turn_tool_call(
            session_id, tool_name=tool_name, args=args,
        ),
    ):
        directive = boundary()
        if directive is None:
            continue
        _record_trace_guard_event(
            session_id,
            guard=str(directive.get("reason") or "tool_resource_boundary"),
            result="blocked",
        )
        return directive
    # Hermes v0.20 dispatches discovery helpers and its generic ``tool_call``
    # trampoline through framework paths that do not consistently invoke a
    # matching post_tool_call.  The concrete business call is separately
    # observed after dispatch, so creating an inflight event for the
    # trampoline produced false ``tool_completion_missing`` failures.  This
    # changes trace pairing only; it neither selects nor suppresses a tool.
    if tool_name not in {"tool_describe", "tool_search", "skill_view", "skills_list", "tool_call"}:
        _begin_trace_tool_event(session_id, tool_name=tool_name)
        _record_trace_progress_event(
            session_id,
            kind="tool_call_started",
            source="tool",
            tool_name=tool_name,
        )
    return None


def _is_framework_terminal_reply_unavailable(value: str) -> bool:
    """Recognize Hermes' own terminal diagnostic, never user/business text."""

    lowered = str(value or "").strip().lower()
    return not lowered or any(
        marker in lowered
        for marker in (
            "model returned no content after all retries",
            "the request failed:",
            "processing completed but no response was generated",
        )
    )


def _stage_direct_reply_terminal(
    *, session_id: str, turn_id: str, terminal_state: str, provider_succeeded: bool | None,
    final_reply_text: str, trace_ref: str,
) -> Any:
    """Observe a direct Agent terminal state without changing business truth."""

    try:
        from .direct_reply_recovery import get_direct_reply_recovery_manager

        return get_direct_reply_recovery_manager().stage_terminal(
            agent_session_id=session_id,
            agent_turn_id=turn_id,
            terminal_state=terminal_state,
            provider_succeeded=provider_succeeded,
            final_reply_text=final_reply_text,
            raw_trace_ref=trace_ref,
        )
    except Exception:
        logger.exception("XIAOYOU_DIRECT_REPLY_TERMINAL_STAGE_FAILED session_id=%s turn_id=%s", session_id, turn_id)
        return None


def _settle_agenda_terminal_from_public_hook(
    *, session_id: str, turn_id: str, final_reply_text: str, trace_ref: str,
) -> bool:
    """Settle only the already-attested Agenda ticket at Agent terminality.

    ``transform_llm_output`` is Hermes' public final-output lifecycle seam;
    by the time it fires, the Agent has completed all Tool rounds.  The
    bridge calls a narrow public API owned by the Agenda adapter and verifies
    the active Runtime-Contract turn before any delivery state is changed.
    It cannot parse a Work payload, choose a Tool, or select a recipient.
    """

    trusted = _activate_trusted_turn(session_id=session_id, turn_id=turn_id)
    if trusted is None or str(getattr(trusted, "platform", "") or "").lower() != "agenda_service_work":
        logger.error("XIAOYOU_AGENDA_TERMINAL_UNTRUSTED session_id=%s", session_id)
        return False
    chat_id = str(getattr(trusted, "chat_id", "") or "")
    message_id = str(getattr(trusted, "message_id", "") or "")
    if not chat_id or not message_id:
        logger.error("XIAOYOU_AGENDA_TERMINAL_BINDING_INCOMPLETE session_id=%s", session_id)
        return False
    try:
        import importlib
        import sys

        settle = None
        module_label = ""
        for loaded_name, loaded_module in tuple(sys.modules.items()):
            source_file = str(getattr(loaded_module, "__file__", "") or "").replace("\\", "/")
            if not source_file.endswith("/agenda_service_work/adapter.py"):
                continue
            candidate = getattr(loaded_module, "settle_agenda_ticket_from_public_model_terminal", None)
            if callable(candidate):
                settle = candidate
                module_label = f"loaded:{loaded_name}"
                break
        if settle is None:
            for module_name in (
                "hermes_plugins.agenda_service_work.adapter",
                "agenda_service_work.adapter",
                "plugins.agenda_service_work.adapter",
            ):
                try:
                    module = importlib.import_module(module_name)
                    candidate = getattr(module, "settle_agenda_ticket_from_public_model_terminal", None)
                    if callable(candidate):
                        settle = candidate
                        module_label = module_name
                        break
                except ImportError:
                    continue
        if not callable(settle):
            logger.error("XIAOYOU_AGENDA_TERMINAL_BRIDGE_UNAVAILABLE session_id=%s", session_id)
            return False
        settled = bool(settle(
            chat_id=chat_id,
            message_id=message_id,
            session_id=session_id,
            turn_id=turn_id,
            final_reply_text=str(final_reply_text or ""),
            trace_ref=trace_ref,
        ))
        logger.info(
            "XIAOYOU_AGENDA_TERMINAL_%s session_id=%s module=%s",
            "SETTLED" if settled else "FAILED",
            session_id,
            module_label,
        )
        return settled
    except Exception:
        logger.exception("XIAOYOU_AGENDA_TERMINAL_BRIDGE_FAILED session_id=%s", session_id)
        return False


def _on_transform_llm_output(**kwargs: Any) -> str | None:
    platform_raw = kwargs.get("platform")
    platform = str(getattr(platform_raw, "value", platform_raw) or "").lower()
    session_id = str(kwargs.get("session_id") or "")
    turn_id = str(kwargs.get("turn_id") or "")
    response_text = str(kwargs.get("response_text") or "")
    if not _is_trusted_model_channel(platform) or not session_id:
        return None
    if platform == "reply_recovery":
        # The recovery adapter will record the terminal text in the same
        # durable outbox. Do not apply ordinary external-claim rewriting to a
        # reply-only turn, and never turn a framework diagnostic into content.
        if _is_framework_terminal_reply_unavailable(response_text):
            return ""
        return None
    # A Tool-owned artifact wins over the model's prose.  It is only present
    # after the model selected the Tool and that Tool returned a matching,
    # verified render for this exact server-attested direct turn.  Returning
    # the opaque control marker defers the original bytes to the durable
    # outbox; it is not user-visible text and cannot initiate a business Tool.
    if platform in {"robot_poc", "wecom_callback"}:
        try:
            from .direct_reply_recovery import control_marker, get_direct_reply_recovery_manager

            # The documented transform hook can expose a gateway-owned turn
            # id that differs from post-tool's Agent id.  Reattach only the
            # one live server-attested Runtime Contract turn for this session;
            # the recovery manager still requires an exact matching alias.
            trusted = _activate_trusted_turn(session_id=session_id, turn_id=turn_id)
            trusted_turn_id = (
                str(getattr(trusted, "turn_id", "") or "")
                if trusted is not None and str(getattr(trusted, "platform", "") or "").lower() == platform
                else ""
            )
            artifact_job = get_direct_reply_recovery_manager().tool_delivery_artifact_handoff(
                agent_session_id=session_id,
                agent_turn_id=turn_id,
                trusted_turn_id=trusted_turn_id,
            )
            if artifact_job is not None:
                logger.info(
                    "XIAOYOU_TOOL_DELIVERY_ARTIFACT_HANDOFF session_id=%s hook_turn=%s trusted_turn=%s reply_id=%s",
                    session_id,
                    turn_id[:24],
                    trusted_turn_id[:24],
                    artifact_job.reply_id,
                )
                _record_trace_guard_event(session_id, guard="tool_delivery_artifact", result="outbox_handoff")
                return control_marker(artifact_job.reply_id)
        except Exception:
            logger.exception("XIAOYOU_TOOL_DELIVERY_ARTIFACT_HANDOFF_FAILED session_id=%s", session_id)
    if not response_text:
        if platform == "agenda_service_work":
            _settle_agenda_terminal_from_public_hook(
                session_id=session_id,
                turn_id=turn_id,
                final_reply_text="",
                trace_ref="public-transform-llm-empty:" + session_id,
            )
        return None
    transformed = _foundation_transform_final_response(
        store=_domain().store,
        session_id=session_id,
        response_text=response_text,
    )
    _deduped, removed = _foundation_dedupe_external_reply_blocks(response_text)
    # transform_final_response performs the same deterministic operation before
    # final claim validation.  This trace records its effect without retaining
    # either version of the user-facing text.
    if removed:
        _record_trace_response_deduplication(session_id, removed_chars=removed)
        _record_trace_guard_event(
            session_id,
            guard="response_duplicate_dedup",
            result=f"removed_chars:{removed}",
        )
    _record_trace_guard_event(
        session_id,
        guard="final_reply_claim_guard",
        result="rewritten" if transformed is not None and transformed != response_text else "allowed",
    )
    if platform == "agenda_service_work":
        final_reply = transformed if transformed is not None else response_text
        _settle_agenda_terminal_from_public_hook(
            session_id=session_id,
            turn_id=turn_id,
            final_reply_text=final_reply,
            trace_ref="public-transform-llm:" + session_id,
        )
    if platform in {"robot_poc", "wecom_callback"}:
        final_reply = transformed if transformed is not None else response_text
        unavailable = _is_framework_terminal_reply_unavailable(final_reply)
        job = _stage_direct_reply_terminal(
            session_id=session_id,
            turn_id=turn_id,
            terminal_state="failed" if unavailable else "completed",
            provider_succeeded=False if unavailable else True,
            final_reply_text="" if unavailable else final_reply,
            trace_ref="public-transform-llm:" + session_id,
        )
        if job is not None:
            # The marker is accepted only by the currently authenticated direct
            # channel destination and releases this exact durable job. It is
            # transport control, never a user-facing reply; the common outbox
            # later delivers Hermes' text.
            from .direct_reply_recovery import control_marker

            return control_marker(job.reply_id)
    return transformed


def _on_post_llm_call_v020(**kwargs: Any) -> None:
    """Record a finalized v0.20 model turn before the platform delivery step."""

    platform_raw = kwargs.get("platform")
    platform = str(getattr(platform_raw, "value", platform_raw) or "").lower()
    session_id = str(kwargs.get("session_id") or "")
    turn_id = str(kwargs.get("turn_id") or "")
    if not _is_trusted_model_channel(platform) or not session_id:
        return
    # Do not discard a terminal failure recorded by the public provider hook.
    # A later successful provider event already clears it in
    # ``_on_post_api_request``.  Some v0.20 runtimes emit this lifecycle hook
    # after an exhausted error path, so clearing it here would incorrectly turn
    # a displayed transport diagnostic into a completed model turn.
    turn = _ACTIVE_MODEL_TURNS.pop(session_id, None)
    _clear_turn_tool_budget(session_id)
    if not turn:
        return
    terminal_provider_failure = _TERMINAL_PROVIDER_FAILURES.pop(session_id, "")
    if terminal_provider_failure:
        _record_trace_guard_event(
            session_id,
            guard="provider_terminal_failure",
            result=terminal_provider_failure,
        )
        try:
            _finalize_turn_trace(
                _domain().store,
                session_id=session_id,
                delivery_status="model_request_failed_reply_prepared",
                final_reply="",
                terminal_failure_type=terminal_provider_failure,
            )
        except Exception:
            logger.exception("tuoguan_core failed to persist terminal provider failure trace")
        if platform in {"robot_poc", "wecom_callback"}:
            _stage_direct_reply_terminal(
                session_id=session_id,
                turn_id=turn_id,
                terminal_state="failed",
                provider_succeeded=False,
                final_reply_text="",
                trace_ref="public-provider-terminal:" + terminal_provider_failure,
            )
        elif platform == "agenda_service_work":
            _settle_agenda_terminal_from_public_hook(
                session_id=session_id,
                turn_id=turn_id,
                final_reply_text="",
                trace_ref="public-provider-terminal:" + terminal_provider_failure,
            )
        _finish_turn_fence(session_id=session_id)
        _clear_trusted_turn(session_id=session_id)
        return
    late_turn_reason = _turn_fence_block_reason(session_id=session_id)
    if late_turn_reason:
        _record_trace_guard_event(session_id, guard="turn_fence", result="expired_late_result_discarded")
        try:
            _finalize_turn_trace(
                _domain().store,
                session_id=session_id,
                delivery_status="expired_late_result_discarded",
                final_reply="",
            )
        except Exception:
            logger.exception("tuoguan_core failed to persist late-result trace")
        return
    final_reply = str(kwargs.get("assistant_response") or "")
    if platform == "agenda_service_work" and final_reply:
        # Hermes 0.21 invokes this public terminal hook after the complete
        # Agent loop.  In this runtime the earlier transform hook is not
        # guaranteed to precede it, so staging the raw assistant text here
        # bypassed the common Reply Truth boundary and exposed audit fields to
        # WeCom.  Apply the same product-owned final-response transform before
        # an Agenda ticket can enter Durable Reply Outbox.  The transform never
        # chooses a business action; a verified receipt with no usable text
        # follows the existing same-Hermes, zero-Tool recovery route.
        transformed = _foundation_transform_final_response(
            store=_domain().store,
            session_id=session_id,
            response_text=final_reply,
        )
        if transformed is not None:
            final_reply = transformed
    if not final_reply:
        if platform in {"robot_poc", "wecom_callback"}:
            _stage_direct_reply_terminal(
                session_id=session_id,
                turn_id=turn_id,
                terminal_state="failed",
                provider_succeeded=None,
                final_reply_text="",
                trace_ref="public-post-llm-empty:" + session_id,
            )
        elif platform == "agenda_service_work":
            _settle_agenda_terminal_from_public_hook(
                session_id=session_id,
                turn_id=turn_id,
                final_reply_text="",
                trace_ref="public-post-llm-empty:" + session_id,
            )
        return
    if platform == "agenda_service_work":
        # This deployed Hermes 0.21 runtime exposes ``post_llm_call`` as the
        # public terminal hook.  It fires only after the complete Agent loop;
        # settle the service ticket here rather than from any adapter send.
        # The bridge is idempotent because a future runtime may also call the
        # preceding transform hook.
        _settle_agenda_terminal_from_public_hook(
            session_id=session_id,
            turn_id=turn_id,
            final_reply_text=final_reply,
            trace_ref="public-post-llm:" + session_id,
        )
    if platform in {"robot_poc", "wecom_callback"}:
        try:
            from .direct_reply_recovery import parse_control_marker

            if parse_control_marker(final_reply) is not None:
                # transform_llm_output already recorded the actual Hermes text
                # and substituted an opaque transport marker. Never persist
                # that marker as a natural-language outbound reply.
                _finish_turn_fence(session_id=session_id)
                _clear_trusted_turn(session_id=session_id)
                return
        except Exception:
            logger.exception("XIAOYOU_DIRECT_REPLY_MARKER_CHECK_FAILED")
        _stage_direct_reply_terminal(
            session_id=session_id,
            turn_id=turn_id,
            terminal_state="failed" if _is_framework_terminal_reply_unavailable(final_reply) else "completed",
            provider_succeeded=False if _is_framework_terminal_reply_unavailable(final_reply) else True,
            final_reply_text="" if _is_framework_terminal_reply_unavailable(final_reply) else final_reply,
            trace_ref="public-post-llm:" + session_id,
        )
    _foundation_ensure_outbound_reply_recorded(
        store=_domain().store,
        message_id=str(turn.get("message_id") or ""),
        conversation_id=str(turn.get("conversation_id") or session_id),
        user_id=str(turn.get("user_id") or ""),
        role=str(turn.get("role") or "unbound"),
        raw_text=str(turn.get("raw_text") or ""),
        final_reply=final_reply,
        entered_model=True,
        session_id=session_id,
        route_decision="model_first_v020",
    )
    try:
        _finalize_turn_trace(
            _domain().store,
            session_id=session_id,
            delivery_status="prepared",
            final_reply=final_reply,
        )
    except Exception:
        logger.exception("tuoguan_core failed to persist sanitized turn trace")
    logger.warning(
        "YOUYI_POST_LLM_RESPONSE_RECORDED sender=%s session_id=%s message_id=%s",
        str(turn.get("user_id") or ""),
        session_id,
        str(turn.get("message_id") or ""),
    )
    _finish_turn_fence(session_id=session_id)
    _clear_trusted_turn(session_id=session_id)


def _on_post_gateway_response(**kwargs: Any) -> None:
    event = kwargs.get("event")
    source = getattr(event, "source", None)
    platform = _platform_name(source)
    if source is None or not _is_trusted_model_channel(platform):
        return
    source_message_id = str(getattr(event, "message_id", "") or "")
    late_turn_reason = _turn_fence_block_reason(
        session_id=str(kwargs.get("session_id") or ""),
        message_id=source_message_id,
    )
    if late_turn_reason:
        trace_key = str(kwargs.get("session_id") or getattr(source, "chat_id", "") or source_message_id)
        _record_trace_guard_event(trace_key, guard="turn_fence", result="expired_late_result_discarded")
        logger.warning(
            "YOUYI_LATE_GATEWAY_RESULT_DISCARDED session_id=%s message_id=%s reason=%s",
            trace_key, source_message_id, late_turn_reason,
        )
        return
    try:
        gateway = kwargs.get("gateway")
        # Only WeCom is allowed to start the existing daily-push worker.  A
        # Robot POC reply must never turn into a proactive physical-device job.
        if platform == "wecom_callback" and gateway is not None:
            _ensure_daily_push_loop(gateway, event)
    except Exception:
        logger.exception("tuoguan_core failed to ensure notification worker after gateway response")
    if platform == "agenda_service_work":
        trusted_service_turn = _activate_trusted_turn(
            session_id=str(kwargs.get("session_id") or ""),
        )
        if trusted_service_turn is None:
            # A service response without the ticket-bound turn cannot be
            # safely associated with an actor or an owner destination. The
            # adapter has no alternate outbound path, so retain a truthful
            # failure rather than manufacturing a WeCom identity.
            logger.error("XIAOYOU_AGENDA_POST_RESPONSE_UNCORRELATED session_id=%s", str(kwargs.get("session_id") or ""))
            return
        identity = trusted_service_turn.identity
    else:
        identity = _domain().identities.resolve(
            "wecom_callback",
            _sender_id(source),
            user_name=str(getattr(source, "user_name", "") or ""),
            chat_id=str(getattr(source, "chat_id", "") or ""),
            message_text=str(getattr(event, "text", "") or ""),
        )
    _foundation_ensure_outbound_reply_recorded(
        store=_domain().store,
        message_id=source_message_id,
        conversation_id=str(getattr(source, "chat_id", "") or identity.canonical_user_id),
        user_id=identity.canonical_user_id,
        role=identity.role,
        raw_text=str(getattr(event, "text", "") or ""),
        final_reply=str(kwargs.get("response_text") or ""),
        entered_model=True,
        session_id=str(kwargs.get("session_id") or ""),
        route_decision="model_first",
    )
    if str(kwargs.get("delivery_status") or "") == "delivered":
        _foundation_mark_outbound_reply_delivered(
            store=_domain().store,
            source_message_id=source_message_id,
            platform_message_id=str(kwargs.get("platform_message_id") or ""),
        )
    trace_key = str(kwargs.get("session_id") or getattr(source, "chat_id", "") or source_message_id)
    terminal_provider_failure = _TERMINAL_PROVIDER_FAILURES.pop(trace_key, "")
    if terminal_provider_failure:
        _record_trace_guard_event(
            trace_key,
            guard="provider_terminal_failure",
            result=terminal_provider_failure,
        )
    try:
        _finalize_turn_trace(
            _domain().store,
            session_id=trace_key,
            delivery_status=(
                f"model_request_failed_reply_{str(kwargs.get('delivery_status') or 'unknown')}"
                if terminal_provider_failure else str(kwargs.get("delivery_status") or "unknown")
            ),
            # The visible error reply is not a model/business completion, so
            # never let it satisfy a final-answer success criterion.
            final_reply="" if terminal_provider_failure else str(kwargs.get("response_text") or ""),
            terminal_failure_type=terminal_provider_failure,
        )
    except Exception:
        logger.exception("tuoguan_core failed to persist sanitized turn trace")
    logger.warning(
        "YOUYI_POST_GATEWAY_RESPONSE_RECORDED sender=%s session_id=%s message_id=%s delivery_status=%s",
        identity.canonical_user_id,
        str(kwargs.get("session_id") or ""),
        source_message_id,
        str(kwargs.get("delivery_status") or ""),
    )
    _finish_turn_fence(
        session_id=str(kwargs.get("session_id") or ""),
        message_id=source_message_id,
    )
    _clear_trusted_turn(session_id=str(kwargs.get("session_id") or ""))
    # Architecture migration shadow: observe the completed ledger only. This
    # never executes a command and never writes protected business files.
    try:
        from .shadow_command_bus import observe_completed_message

        observe_completed_message(
            data_dir=_domain().store.data_dir,
            message_id=str(getattr(event, "message_id", "") or ""),
            tenant_id=current_tenant_id(),
            channel=platform,
        )
    except Exception:
        logger.exception("command shadow observation failed")


def register(ctx) -> None:
    """Register the tutoring business router for Enterprise WeChat callback DMs."""
    global _REGISTERED_TOOL_COUNT
    from .tools import TOOLSET, agenda_governance_tools, agenda_service_tools, agenda_task_tools, model_tools

    selected_tools = model_tools()
    _REGISTERED_TOOL_COUNT = len(selected_tools)

    _log_runtime_module_manifest()
    # Model-led restore: old business routers and runtime prompt/response hooks are
    # not registered on the main message path. Keep tools plus passive audit only.
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    register_middleware = getattr(ctx, "register_middleware", None)
    if callable(register_middleware):
        register_middleware("llm_request", _on_llm_request_middleware)
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except Exception:
        VALID_HOOKS = {"post_gateway_response"}
    if "transform_tool_result" in VALID_HOOKS:
        ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    for hook_name, handler in (
        ("pre_api_request", _on_pre_api_request),
        ("post_api_request", _on_post_api_request),
        ("api_request_error", _on_api_request_error),
    ):
        if hook_name in VALID_HOOKS:
            ctx.register_hook(hook_name, handler)
    if "post_gateway_response" in VALID_HOOKS:
        ctx.register_hook("post_gateway_response", _on_post_gateway_response)
    else:
        ctx.register_hook("transform_llm_output", _on_transform_llm_output)
        ctx.register_hook("post_llm_call", _on_post_llm_call_v020)
    for name, schema, handler in selected_tools:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            emoji="",
        )
    # Hermes selects this small surface only for the authenticated service
    # platform via ``platform_toolsets``. These are aliases for existing Tool
    # contracts, not Agenda-specific business handlers or a Router.
    for name, schema, handler in agenda_service_tools():
        ctx.register_tool(
            name=name,
            toolset="agenda_service",
            schema=schema,
            handler=handler,
            emoji="",
        )
    for name, schema, handler in agenda_task_tools():
        ctx.register_tool(
            name=name,
            toolset="agenda_task",
            schema=schema,
            handler=handler,
            emoji="",
        )
    for name, schema, handler in agenda_governance_tools():
        ctx.register_tool(
            name=name,
            toolset="agenda_governance",
            schema=schema,
            handler=handler,
            emoji="",
        )

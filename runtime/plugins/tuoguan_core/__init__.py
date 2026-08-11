"""Hermes-native tutoring-center business module."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import RouteResult
from .store import JSON_NO_CHANGE, TuoguanStore, TuoguanStoreError
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
    inject_model_context as _foundation_inject_model_context,
    observe_tool_result as _foundation_observe_tool_result,
    ensure_outbound_reply_recorded as _foundation_ensure_outbound_reply_recorded,
    mark_outbound_reply_delivered as _foundation_mark_outbound_reply_delivered,
    should_clarify_without_tool as _foundation_should_clarify_without_tool,
    transform_final_response as _foundation_transform_final_response,
)

logger = logging.getLogger(__name__)


def _log_runtime_module_manifest() -> None:
    """Log loaded module paths and hashes as post-restart runtime proof."""
    import importlib

    for name in (
        "gateway.run",
        "gateway.platforms.base",
        "plugins.platforms.wecom.callback_adapter",
        "gateway.outbound_reply_guard",
        "plugins.tuoguan_core.runtime_ownership",
        "plugins.tuoguan_core.runtime_foundation",
        "plugins.tuoguan_core.active_work_context",
        "plugins.tuoguan_core.self_evolution",
    ):
        try:
            module = importlib.import_module(name)
            path = Path(str(getattr(module, "__file__", "") or "")).resolve()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
            logger.info("P0_RUNTIME_MODULE name=%s file=%s sha256=%s", name, path, digest)
        except Exception as exc:
            logger.error("P0_RUNTIME_MODULE name=%s load_error=%s", name, exc)

_ROUTER: Any | None = None
_DAILY_PUSH_TASKS: dict[int, asyncio.Task] = {}
_DAILY_PUSH_WAKE_EVENTS: dict[int, asyncio.Event] = {}
_ACTIVE_WECom_USERS: dict[str, datetime] = {}
_ACTIVE_CONVERSATION_QUIET_PERIOD = timedelta(minutes=3)
_CLAIMED_REPLY_MESSAGE_IDS: dict[str, datetime] = {}
_ACTIVE_MODEL_TURNS: dict[str, dict[str, Any]] = {}
_REPLY_CLAIM_TTL = timedelta(hours=2)

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


def _router() -> Any:
    global _ROUTER
    if _ROUTER is None:
        from .router import TuoguanRouter

        _ROUTER = TuoguanRouter()
    return _ROUTER


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform) or "").lower()


def _sender_id(source: Any) -> str:
    user_id = str(getattr(source, "user_id", "") or "").strip()
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
        "【优益主动提问回复锚点】",
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
        "【优益最近主动外发消息锚点】",
        f"下面是小优最近主动发给{role_label}的消息。它们形成短时临时会话线程，只是衔接材料，不是 Router，也不替模型判断用户意图。",
        f"{role_label}本轮原话：{' '.join(str(current_message or '').split())[:800]}",
        "强衔接规则：用户说“它/里面/这个/这些/链接/网址/内容/讲讲/解释/总结/什么意思/你发的/你推的”时，优先把本轮理解为追问最近一条主动外发消息。",
        "除非用户本轮明确点名其他对象（例如明确说看板、某个老师、某项任务编号），不要把模糊代词接到更早的旧会话、旧看板链接、旧偏好或旧工作项。",
        "如果相关：先围绕对应外发消息解释清楚；如果这是外部学习/市场报告，要解释资料讲了什么、对优益有什么用、哪些只是外部资料不能当成机构事实。",
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
        "【优益当前学期边界材料】",
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
        source_text = str(item.get("source_text") or "金总已确认数字员工对外称呼为小优。")
        confirmed_at = str(item.get("confirmed_at") or item.get("updated_at") or "")
        return "\n".join([
            "【优益数字员工身份称呼】",
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
    return (
        "【已加载 Skill：xiaoyou-core】小优是托管机构数字员工；员工手册提供身份、业务常识、岗位责任和判断框架，"
        "模型负责理解、判断和行动选择，系统只守身份、权限、证据、幂等、频率、审计、真实执行、写后反查和外发边界。"
        f"本轮服务对象仅为 user_id={user_id}、role={role}；只可注入此人的角色、个人工作方式、当前任务和必要机构事实，"
        "不得混入老板或其他员工的个人档案。先查当前上下文、可信业务工具、人员目录、历史证据及必要只读公开资料，再说查不到。"
        "没有真实工具调用不能说查过，没有写后反查不能说已保存，没有发送回执不能说已发送。"
        "专项问题按需参考 youyi-digital-employee、youyi-tuoguan-business、active-information-acquisition、goal-management、"
        "memory-evidence-learning、institution-onboarding、student-service-relations；它们不是固定 Router。"
    )


def _workstyle_context(store: TuoguanStore, *, identity: Any, raw_text: str) -> str:
    try:
        from .workstyle_profiles import workstyle_context_for_user

        return workstyle_context_for_user(
            store,
            identity=identity,
            scope="",
            raw_text=raw_text,
        )
    except Exception:
        logger.exception("tuoguan_core failed to recall person workstyle context")
        return ""


def _self_evolution_context(store: TuoguanStore, *, identity: Any) -> str:
    try:
        from .self_evolution import conversation_evolution_context_for_user

        return conversation_evolution_context_for_user(
            store,
            identity=identity,
            limit=5,
        )
    except Exception:
        logger.exception("tuoguan_core failed to recall self-evolution context")
        return ""


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
        from gateway.outbound_reply_guard import create_final_reply_envelope
        source_message_id = str(getattr(event, "message_id", "") or "")
        envelope = create_final_reply_envelope(
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
        identity = _router().identities.resolve(
            _platform_name(source),
            _sender_id(source),
            user_name=str(getattr(source, "user_name", "") or ""),
            chat_id=str(getattr(source, "chat_id", "") or chat_id),
            message_text=str(getattr(event, "text", "") or ""),
        )
        _foundation_ensure_outbound_reply_recorded(
            store=_router().store,
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
        _router().confirm_notifications_delivered(delivered)


def _append_notification_failure(item: dict[str, Any], error: str) -> None:
    try:
        store = TuoguanStore()
        path = store.path_for("notification_failures.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "touser": str(item.get("touser") or ""),
            "task_id": str(item.get("task_id") or ""),
            "action": str(item.get("action") or ""),
            "error": error,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
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
            last_inbound = _coerce_runtime_datetime(_ACTIVE_WECom_USERS.get(target), now=now)
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
            identity = _router().identities.resolve(
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
        if str(item.get("relationship_touch_candidate_id") or "") and status in {"sent", "failed", "result_unknown"}:
            identity = _router().identities.resolve(
                "wecom_callback",
                str(item.get("touser") or item.get("target_user_id") or ""),
                chat_id=str(item.get("touser") or item.get("target_user_id") or ""),
                message_text="",
            )
            update_relationship_touch_candidate_status(
                store,
                identity=identity,
                candidate_id=str(item.get("relationship_touch_candidate_id") or ""),
                status=status,
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
    }


def _append_notification_audit(store: TuoguanStore, item: dict[str, Any], event: str, result: str) -> None:
    path = store.path_for("business_action_audit.jsonl")
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
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
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
    if platform != "wecom_callback":
        return None
    try:
        from gateway.session_context import get_session_env
    except Exception:
        get_session_env = None  # type: ignore[assignment]
    sender_id = str(kwargs.get("sender_id") or "").strip()
    if not sender_id and get_session_env is not None:
        sender_id = str(get_session_env("HERMES_SESSION_USER_ID", "") or "").strip()
    if ":" in sender_id:
        sender_id = sender_id.split(":", 1)[1].strip()
    raw_text = str(kwargs.get("user_message") or "")
    session_id = str(kwargs.get("session_id") or "")
    if not session_id and get_session_env is not None:
        session_id = str(get_session_env("HERMES_SESSION_ID", "") or get_session_env("HERMES_SESSION_KEY", "") or "")
    chat_id = session_id
    message_id = str(kwargs.get("turn_id") or "").strip()
    if get_session_env is not None:
        chat_id = str(get_session_env("HERMES_SESSION_CHAT_ID", "") or chat_id)
        message_id = str(get_session_env("HERMES_SESSION_MESSAGE_ID", "") or message_id)
    if not message_id:
        message_id = hashlib.sha256(f"{session_id}\n{raw_text}".encode("utf-8")).hexdigest()[:24]
    if not sender_id or not raw_text:
        logger.warning(
            "YOUYI_PRE_LLM_CONTEXT_SKIPPED sender_empty=%s raw_empty=%s platform=%s session_id=%s",
            not bool(sender_id),
            not bool(raw_text),
            platform,
            session_id,
        )
        return None
    try:
        identity = _router().identities.resolve(
            "wecom_callback",
            sender_id,
            chat_id=chat_id,
            message_text=raw_text,
        )
        turn = _foundation_begin_inbound(
            store=_router().store,
            message_id=message_id,
            conversation_id=chat_id or session_id or identity.canonical_user_id,
            user_id=identity.canonical_user_id,
            role=identity.role,
            raw_text=raw_text,
        )
        turn_key = session_id or chat_id or identity.canonical_user_id
        _ACTIVE_MODEL_TURNS[turn_key] = {
            "message_id": message_id,
            "conversation_id": chat_id or session_id or identity.canonical_user_id,
            "user_id": identity.canonical_user_id,
            "role": identity.role,
            "raw_text": raw_text,
            "created_at": datetime.now().astimezone(),
        }
        if len(_ACTIVE_MODEL_TURNS) > 512:
            oldest = sorted(
                _ACTIVE_MODEL_TURNS,
                key=lambda key: _ACTIVE_MODEL_TURNS[key].get("created_at") or datetime.min.astimezone(),
            )[:128]
            for key in oldest:
                _ACTIVE_MODEL_TURNS.pop(key, None)
        injected = _foundation_inject_model_context(
            session_id=session_id,
            sender_id=identity.canonical_user_id,
            user_message=raw_text,
        )
        logger.warning(
            "YOUYI_PRE_LLM_CONTEXT_READY sender=%s canonical=%s role=%s session_id=%s chat_id=%s began=%s injected=%s raw=%s",
            sender_id,
            identity.canonical_user_id,
            identity.role,
            session_id,
            chat_id,
            bool(turn),
            bool(injected),
            raw_text[:80],
        )
    except Exception:
        logger.exception("tuoguan_core failed to establish model-led runtime context")
    context_parts: list[str] = []
    if "identity" in locals():
        context_parts.append(_xiaoyou_core_skill_context(identity=identity))
    try:
        if "identity" in locals():
            _record_owner_inbound_fact(
                _router().store,
                identity=identity,
                raw_text=raw_text,
                message_id=message_id,
                session_id=session_id,
            )
        recent_outbound_context = _recent_owner_outbound_context(
            _router().store,
            identity=identity,
            current_message=raw_text,
        ) if "identity" in locals() else ""
        if recent_outbound_context:
            context_parts.append(recent_outbound_context)
            logger.warning(
                "YOUYI_RECENT_OUTBOUND_CONTEXT_READY sender=%s session_id=%s raw=%s",
                sender_id,
                session_id,
                raw_text[:80],
            )
        recent_outbound_follow_up = bool(
            recent_outbound_context
            and _looks_like_recent_outbound_follow_up(raw_text)
        )
        owner_attention_context = ""
        if "identity" in locals() and not recent_outbound_follow_up:
            owner_attention_context = _open_owner_attention_context(
                _router().store,
                identity=identity,
                current_message=raw_text,
            )
        if owner_attention_context:
            context_parts.append(owner_attention_context)
            logger.warning(
                "YOUYI_OWNER_ATTENTION_CONTEXT_READY sender=%s session_id=%s raw=%s",
                sender_id,
                session_id,
                raw_text[:80],
            )
        term_context = _term_boundary_context(
            _router().store,
            raw_text=raw_text,
            include_for_attention=bool(owner_attention_context),
        )
        if term_context:
            context_parts.append(term_context)
    except Exception:
        logger.exception("tuoguan_core failed to recall owner attention context")
    try:
        identity_context = _public_identity_context(_router().store)
        if identity_context:
            context_parts.insert(0, identity_context)
    except Exception:
        logger.exception("tuoguan_core failed to recall public identity context")
    try:
        if "identity" in locals():
            temporal_context = build_temporal_grounding_context(
                _router().store,
                identity=identity,
                raw_text=raw_text,
                session_id=session_id,
                chat_id=chat_id,
                platform=platform,
            )
            if temporal_context:
                context_parts.append(temporal_context)
    except Exception:
        logger.exception("tuoguan_core failed to append temporal grounding context")
    try:
        if "identity" in locals():
            self_evolution_context = _self_evolution_context(
                _router().store,
                identity=identity,
            )
            if self_evolution_context:
                context_parts.append(self_evolution_context)
    except Exception:
        logger.exception("tuoguan_core failed to append self-evolution context")
    try:
        if "identity" in locals():
            workstyle_context = _workstyle_context(
                _router().store,
                identity=identity,
                raw_text=raw_text,
            )
            if workstyle_context:
                context_parts.append(workstyle_context)
    except Exception:
        logger.exception("tuoguan_core failed to append workstyle context")
    try:
        if "identity" in locals():
            role_layer_context = _role_layer_context(
                identity=identity,
                raw_text=raw_text,
            )
            if role_layer_context:
                context_parts.append(role_layer_context)
    except Exception:
        logger.exception("tuoguan_core failed to append role layer context")
    compact_raw = "".join(raw_text.split())
    ambiguous_retry = compact_raw in {
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
        )
    ) and not asks_how_to_confirm
    write_like = write_like or (task_completion_like and not asks_how_to_confirm)
    if ambiguous_retry:
        try:
            from .active_work_context import query_active_work_context, render_active_work_context

            active_result = query_active_work_context(_router().store, identity=identity, limit=5)
            active_context = render_active_work_context(active_result)
        except Exception:
            logger.exception("tuoguan_core failed to build active work context")
            active_context = ""
        if active_context:
            context_parts.append(active_context)
            context_parts.append(
                "【短回复衔接规则】短回复本身不是拒绝执行的理由。先结合本轮原话、最近主动外发和上述活动线程判断指向；"
                "相关时自然衔接，并在真实写入前调用对应可信工具。若多个线程同样可能或没有任何证据，只追问一个最关键的区分问题。"
            )
        else:
            context_parts.append(
                "【短回复衔接规则】当前没有可验证的活动线程。不要猜测历史对象；只追问一个最关键的区分问题。"
            )
        return {"context": "\n\n".join(context_parts)}
    if write_like:
        context_parts.append(
            "【优益当前轮写入规则】如果用户本轮明确要求记录、修改、加扣分、创建、完成、确认、提交或上报，"
            "必须调用对应 tuoguan_ 可信工具，以本轮工具结果为唯一执行依据。"
            "模型仍负责理解用户、判断是否追问、是否写入或是否先说明边界；系统只负责权限、审计、幂等和写后核验。"
            "如果要声明记录、修改、加扣分、创建、完成、确认、提交、上报、保存偏好、记住工作方式已经真实发生，必须先看到本轮可信工具返回成功。"
            "不要根据历史里的“写入被拦截/配置未生效/所有写入不能用”等旧结论直接拒绝或声称失败；"
            "只有本轮工具返回 ok=false 时，才可以说明本轮未成功。"
            "写入成功必须来自工具 ok=true 且 writeback_verified=true；不要伪造成功。"
        )
        return {"context": "\n\n".join(context_parts)}
    if context_parts:
        return {"context": "\n\n".join(context_parts)}
    return None


def _on_post_tool_call(**kwargs: Any) -> None:
    _foundation_observe_tool_result(
        session_id=str(kwargs.get("session_id") or ""),
        tool_name=str(kwargs.get("tool_name") or ""),
        args=kwargs.get("args"),
        result=kwargs.get("result"),
    )


def _on_pre_tool_call(**kwargs: Any) -> dict[str, str] | None:
    # Model-led production: do not run an extra pre-tool business router here.
    # Tool handlers still enforce identity, role permissions and write guards.
    return None


def _on_transform_llm_output(**kwargs: Any) -> str | None:
    platform_raw = kwargs.get("platform")
    platform = str(getattr(platform_raw, "value", platform_raw) or "").lower()
    session_id = str(kwargs.get("session_id") or "")
    response_text = str(kwargs.get("response_text") or "")
    if platform != "wecom_callback" or not session_id or not response_text:
        return None
    return _foundation_transform_final_response(
        store=_router().store,
        session_id=session_id,
        response_text=response_text,
    )


def _on_post_llm_call_v020(**kwargs: Any) -> None:
    """Record a finalized v0.20 model turn before the platform delivery step."""

    platform_raw = kwargs.get("platform")
    platform = str(getattr(platform_raw, "value", platform_raw) or "").lower()
    session_id = str(kwargs.get("session_id") or "")
    if platform != "wecom_callback" or not session_id:
        return
    turn = _ACTIVE_MODEL_TURNS.pop(session_id, None)
    if not turn:
        return
    final_reply = str(kwargs.get("assistant_response") or "")
    if not final_reply:
        return
    _foundation_ensure_outbound_reply_recorded(
        store=_router().store,
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
    logger.warning(
        "YOUYI_POST_LLM_RESPONSE_RECORDED sender=%s session_id=%s message_id=%s",
        str(turn.get("user_id") or ""),
        session_id,
        str(turn.get("message_id") or ""),
    )


def _on_post_gateway_response(**kwargs: Any) -> None:
    event = kwargs.get("event")
    source = getattr(event, "source", None)
    if source is None or _platform_name(source) != "wecom_callback":
        return
    try:
        gateway = kwargs.get("gateway")
        if gateway is not None:
            _ensure_daily_push_loop(gateway, event)
    except Exception:
        logger.exception("tuoguan_core failed to ensure notification worker after gateway response")
    identity = _router().identities.resolve(
        "wecom_callback",
        _sender_id(source),
        user_name=str(getattr(source, "user_name", "") or ""),
        chat_id=str(getattr(source, "chat_id", "") or ""),
        message_text=str(getattr(event, "text", "") or ""),
    )
    source_message_id = str(getattr(event, "message_id", "") or "")
    _foundation_ensure_outbound_reply_recorded(
        store=_router().store,
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
            store=_router().store,
            source_message_id=source_message_id,
            platform_message_id=str(kwargs.get("platform_message_id") or ""),
        )
    logger.warning(
        "YOUYI_POST_GATEWAY_RESPONSE_RECORDED sender=%s session_id=%s message_id=%s delivery_status=%s",
        identity.canonical_user_id,
        str(kwargs.get("session_id") or ""),
        source_message_id,
        str(kwargs.get("delivery_status") or ""),
    )
    # Architecture migration shadow: observe the completed ledger only. This
    # never executes a command and never writes protected business files.
    try:
        from .shadow_command_bus import observe_completed_message

        observe_completed_message(
            data_dir=_router().store.data_dir,
            message_id=str(getattr(event, "message_id", "") or ""),
            tenant_id=current_tenant_id(),
            channel="wecom_callback",
        )
    except Exception:
        logger.exception("command shadow observation failed")


def register(ctx) -> None:
    """Register the tutoring business router for Enterprise WeChat callback DMs."""
    from .tools import TOOLS, TOOLSET

    _log_runtime_module_manifest()
    # Model-led restore: old business routers and runtime prompt/response hooks are
    # not registered on the main message path. Keep tools plus passive audit only.
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except Exception:
        VALID_HOOKS = {"post_gateway_response"}
    if "post_gateway_response" in VALID_HOOKS:
        ctx.register_hook("post_gateway_response", _on_post_gateway_response)
    else:
        ctx.register_hook("transform_llm_output", _on_transform_llm_output)
        ctx.register_hook("post_llm_call", _on_post_llm_call_v020)
    for name, schema, handler in TOOLS:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            emoji="",
        )

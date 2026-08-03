"""Approved tutoring-center knowledge and teacher-facing scripts."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .store import TuoguanStore


SCRIPT_CONTEXT_FILE = "script_context.json"
SCRIPT_CONTEXT_TTL = timedelta(minutes=30)


DEFAULT_KNOWLEDGE: list[dict[str, Any]] = [
    {
        "id": "price-objection-basic",
        "category": "enrollment",
        "triggers": ["嫌贵", "太贵", "价格", "优惠", "报名", "招生"],
        "title": "家长觉得暑假班贵",
        "script": (
            "您考虑价格我理解。我们这边不是只看着孩子写完作业，重点是每天有人盯习惯、盯安全、"
            "盯不会的地方。您可以先看孩子最需要解决的是作业效率、基础漏洞，还是暑假没人管，"
            "我再按孩子情况给您说适不适合报。"
        ),
        "follow_up": "沟通后记录：家长主要顾虑、是否有意向、下一次何时跟进。",
    },
    {
        "id": "renewal-phone-basic",
        "category": "renewal",
        "triggers": ["续费", "电话沟通", "电话", "说话", "怎么沟通", "怎么给", "怎么聊", "流程", "思路"],
        "title": "续费电话沟通",
        "script": (
            "家长您好，我想跟您电话沟通一下孩子后续托管和续费的事。"
            "我不是单纯催您续费，主要是先把孩子近期表现、还需要我们继续盯的地方，"
            "以及接下来怎么帮孩子稳定下来跟您说清楚。您也可以直接跟我说，"
            "现在最担心的是效果、时间安排，还是费用，我好针对这个给您一个具体方案。"
        ),
        "follow_up": "电话后记录：家长主要顾虑、你怎么回应、是否有续费意向、下一次何时跟进。",
    },
    {
        "id": "renewal-progress-basic",
        "category": "renewal",
        "triggers": ["续费", "不续", "再看看", "没进步", "效果"],
        "title": "续费时家长担心没效果",
        "script": (
            "您说效果这个点很重要，我先不急着让您续。我们先把孩子最近的作业效率、错题情况、"
            "需要提醒的地方整理清楚，再给您一个具体反馈。后面如果继续在这边，我们也会按这些问题重点跟。"
        ),
        "follow_up": "沟通后记录：家长态度、孩子当前问题、下一步跟进时间。",
    },
    {
        "id": "parent-anxiety-progress",
        "category": "parent_maintenance",
        "triggers": ["家长", "妈妈", "爸爸", "担心", "焦虑", "怎么回复", "怎么说"],
        "title": "家长担心孩子学习状态",
        "script": (
            "您反馈的情况我记下了。今天我会重点看孩子是哪一块卡住了：是不会做、做得慢、"
            "还是注意力不稳。确认清楚后我再把观察结果和建议同步给您，不让您只听一句笼统反馈。"
        ),
        "follow_up": "沟通后记录：家长当前态度、老师怎么回复、下一步安排。",
    },
    {
        "id": "new-student-adaptation",
        "category": "new_student",
        "triggers": ["新生", "第一天", "试托", "适应", "家长问候"],
        "title": "新生适应情况同步",
        "script": (
            "孩子刚来我们会先看三个点：情绪稳不稳、吃饭午休怎么样、和小朋友相处有没有不适应。"
            "今天我会先多观察，晚上给您同步具体情况，明天也会继续留意。"
        ),
        "follow_up": "沟通后记录：孩子适应表现、家长反应、明天关注点。",
    },
]


def _compact(text: str) -> str:
    return str(text or "").replace(" ", "")


def _approved_entries(store: TuoguanStore) -> list[dict[str, Any]]:
    data = store.read_json("tuoguan_knowledge.json", [])
    if not isinstance(data, list):
        return []
    return [
        item
        for item in data
        if isinstance(item, dict) and str(item.get("status") or "approved") == "approved"
    ]


def load_approved_knowledge(store: TuoguanStore) -> list[dict[str, Any]]:
    entries = _approved_entries(store)
    return entries + DEFAULT_KNOWLEDGE


def is_script_request(text: str) -> bool:
    compact = _compact(text)
    request_words = (
        "该怎么",
        "应该怎么",
        "怎么说",
        "怎么回复",
        "怎么沟通",
        "怎么给",
        "怎么跟",
        "怎么和",
        "怎么聊",
        "怎么办",
        "怎么解释",
        "怎么介绍",
        "咋回答",
        "说话",
        "说什么",
        "注意什么",
        "流程",
        "思路",
        "教我",
        "帮我整理",
        "给我一个",
        "话术",
        "帮我写",
        "咋说",
        "如何回复",
        "如何沟通",
    )
    business_words = (
        "家长",
        "妈妈",
        "爸爸",
        "招生",
        "报名",
        "续费",
        "暑假班",
        "嫌贵",
        "贵",
        "不续",
        "没进步",
        "效果",
        "电话",
        "投诉",
        "新生",
        "试托",
    )
    return any(word in compact for word in request_words) and any(
        word in compact for word in business_words
    )


def is_script_feedback(text: str) -> bool:
    compact = _compact(text)
    if not compact or compact in {"开始", "完成了", "任务", "日报", "状态", "帮助"}:
        return False
    feedback_words = (
        "这话",
        "话说",
        "不行",
        "有问题",
        "不合适",
        "不好",
        "生硬",
        "不自然",
        "口语",
        "具体",
        "不适合",
        "太硬",
        "太官方",
        "换一个",
        "重新写",
        "再写",
        "改一下",
        "语气",
    )
    return len(compact) <= 50 and any(word in compact for word in feedback_words)


def remember_script_context(
    store: TuoguanStore,
    user_id: str,
    text: str,
    *,
    now: datetime | None = None,
) -> None:
    if not user_id:
        return
    contexts = store.read_json(SCRIPT_CONTEXT_FILE, {})
    if not isinstance(contexts, dict):
        contexts = {}
    timestamp = now or datetime.now()
    contexts[user_id] = {
        "text": str(text or "").strip(),
        "updated_at": timestamp.isoformat(timespec="seconds"),
    }
    store.write_json(SCRIPT_CONTEXT_FILE, contexts)


def recent_script_context(
    store: TuoguanStore,
    user_id: str,
    *,
    now: datetime | None = None,
) -> str:
    if not user_id:
        return ""
    contexts = store.read_json(SCRIPT_CONTEXT_FILE, {})
    if not isinstance(contexts, dict):
        return ""
    item = contexts.get(user_id)
    if not isinstance(item, dict):
        return ""
    text = str(item.get("text") or "").strip()
    if not text:
        return ""
    try:
        updated_at = datetime.fromisoformat(str(item.get("updated_at") or ""))
    except ValueError:
        return ""
    if (now or datetime.now()) - updated_at > SCRIPT_CONTEXT_TTL:
        return ""
    return text


def _score(entry: dict[str, Any], text: str) -> int:
    compact = _compact(text)
    triggers = entry.get("triggers") or []
    if not isinstance(triggers, list):
        return 0
    score = sum(1 for item in triggers if str(item) and str(item) in compact)
    if str(entry.get("id") or "") == "renewal-phone-basic":
        if any(
            word in compact
            for word in (
                "电话",
                "电话沟通",
                "怎么沟通",
                "怎么给",
                "怎么聊",
                "流程",
                "思路",
                "说话",
            )
        ):
            score += 2
        return score
    category = str(entry.get("category") or "")
    if category == "renewal" and "续费" in compact:
        score += 2
    if category == "enrollment" and any(
        word in compact for word in ("嫌贵", "太贵", "价格", "优惠", "报名", "招生")
    ):
        score += 2
    if category == "new_student" and any(
        word in compact for word in ("新生", "试托", "试听", "第一天")
    ):
        score += 2
    return score


def answer_script_request(text: str, store: TuoguanStore) -> str:
    local_entries = _approved_entries(store)
    if local_entries:
        local_ranked = sorted(
            local_entries,
            key=lambda item: _score(item, text),
            reverse=True,
        )
        if local_ranked and _score(local_ranked[0], text) >= 2:
            entries = local_ranked
        else:
            entries = local_entries + DEFAULT_KNOWLEDGE
    else:
        entries = DEFAULT_KNOWLEDGE
    ranked = sorted(entries, key=lambda item: _score(item, text), reverse=True)
    best = ranked[0] if ranked and _score(ranked[0], text) > 0 else entries[0]
    title = str(best.get("title") or "托管沟通话术")
    script = str(best.get("script") or "").strip()
    follow_up = str(best.get("follow_up") or "沟通后记录：你怎么说的、家长反应、下一步安排。")
    return "\n".join(
        [
            f"【{title}】",
            "可参考话术：",
            script,
            "",
            follow_up,
            "我不会替你转发给家长，沟通后把结果发我，我来帮你沉淀记录或推进任务。",
        ]
    )


def answer_script_feedback(
    base_request: str,
    feedback: str,
    store: TuoguanStore,
) -> str:
    request = _compact(base_request)
    revision = _compact(feedback)
    if "续费" in request:
        if any(word in revision for word in ("啥跟啥", "啥意思", "没听懂")):
            return "\n".join(
                [
                    "明白，上一版没说清楚。我重新给你一版直接电话版：",
                    "",
                    "家长您好，我想跟您聊一下孩子续费的事。"
                    "我先不急着让您定，想先听听您现在主要顾虑什么："
                    "是孩子效果、时间安排，还是费用？您把最担心的点告诉我，"
                    "我再结合孩子最近的实际表现，跟您说清楚我们后面准备怎么做。",
                    "",
                    "家长说完后，你再针对她提到的那个问题回答，不要一次讲一大段。",
                ]
            )
        if any(word in revision for word in ("重新", "有问题", "换一个", "再写")):
            return "\n".join(
                [
                    "可以，我换一种更短、更像电话里说话的版本：",
                    "",
                    "家长您好，想跟您确认一下孩子后续续费的想法。"
                    "孩子最近的情况我也想跟您具体说说，您现在是对效果、时间，"
                    "还是费用这块有顾虑？咱们先把您最在意的问题聊清楚。",
                    "",
                    "这句说完先停下来听家长回答，再接着聊，不要连续念话术。",
                ]
            )
        if "具体" in revision:
            return "\n".join(
                [
                    "我给你拆成电话步骤：",
                    "1. 先问家长现在是否方便。",
                    "2. 用两句话说孩子近期真实表现。",
                    "3. 直接问家长最担心效果、时间还是费用。",
                    "4. 只针对家长提出的顾虑回应。",
                    "5. 最后约定下次确认时间。",
                ]
            )
        return "\n".join(
            [
                "我重新给你一版更自然的说法：",
                "",
                "家长您好，想跟您聊聊孩子后面继续托管的安排。"
                "我也想先听听您的想法，您现在主要担心哪一块？"
                "咱们先把这个问题说清楚，再看后面怎么安排更合适。",
            ]
        )

    original = answer_script_request(base_request, store)
    return "我根据你的反馈重新整理了一版：\n\n" + original


def add_pending_knowledge(
    store: TuoguanStore,
    *,
    source: str,
    category: str,
    title: str,
    content: str,
    evidence: list[dict[str, str]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    items = store.read_json("pending_knowledge.json", [])
    if not isinstance(items, list):
        items = []
    item = {
        "id": f"pending_{timestamp.strftime('%Y%m%d%H%M%S')}_{len(items) + 1}",
        "status": "pending_review",
        "source": source,
        "category": category,
        "title": title,
        "content": content,
        "evidence": evidence or [],
        "created_at": timestamp.isoformat(timespec="seconds"),
    }
    items.append(item)
    store.write_json("pending_knowledge.json", items)
    return item


def list_pending_learning_candidates(store: TuoguanStore) -> list[dict[str, Any]]:
    items = store.read_json("pending_knowledge.json", [])
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict)
        and str(item.get("status") or "pending_review") == "pending_review"
    ]


def submit_learning_candidate(
    store: TuoguanStore,
    *,
    source: str,
    category: str,
    title: str,
    content: str,
    candidate_type: str = "knowledge",
    evidence: list[dict[str, str]] | None = None,
    proposed_triggers: list[str] | None = None,
    created_by: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    items = store.read_json("pending_knowledge.json", [])
    if not isinstance(items, list):
        items = []
    item = {
        "id": f"learn_{timestamp.strftime('%Y%m%d%H%M%S')}_{len(items) + 1}",
        "status": "pending_review",
        "candidate_type": str(candidate_type or "knowledge"),
        "source": source,
        "category": category,
        "title": title,
        "content": content,
        "evidence": evidence or [],
        "proposed_triggers": [str(item) for item in (proposed_triggers or []) if str(item).strip()],
        "created_by": created_by,
        "created_at": timestamp.isoformat(timespec="seconds"),
    }
    items.append(item)
    store.write_json("pending_knowledge.json", items)
    return item


def review_learning_candidate(
    store: TuoguanStore,
    *,
    candidate_id: str,
    decision: str,
    reviewer: str,
    revised_title: str = "",
    revised_content: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    items = store.read_json("pending_knowledge.json", [])
    if not isinstance(items, list):
        items = []
    target = None
    for item in items:
        if isinstance(item, dict) and str(item.get("id") or "") == str(candidate_id):
            target = item
            break
    if target is None:
        raise ValueError("learning candidate not found")
    normalized = str(decision or "").strip().lower()
    if normalized not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject")
    timestamp = now or datetime.now()
    status = "approved" if normalized == "approve" else "rejected"
    target["status"] = status
    target["reviewed_by"] = reviewer
    target["reviewed_at"] = timestamp.isoformat(timespec="seconds")
    if revised_title:
        target["title"] = revised_title
    if revised_content:
        target["content"] = revised_content
    store.write_json("pending_knowledge.json", items)

    if normalized == "approve":
        approved = store.read_json("tuoguan_knowledge.json", [])
        if not isinstance(approved, list):
            approved = []
        approved.append(
            {
                "id": str(target.get("id") or candidate_id).replace("learn_", "approved_"),
                "status": "approved",
                "source_candidate_id": candidate_id,
                "category": str(target.get("category") or "general"),
                "candidate_type": str(target.get("candidate_type") or "knowledge"),
                "title": str(target.get("title") or ""),
                "script": str(target.get("content") or ""),
                "triggers": list(target.get("proposed_triggers") or []),
                "evidence": list(target.get("evidence") or []),
                "approved_by": reviewer,
                "approved_at": timestamp.isoformat(timespec="seconds"),
            }
        )
        store.write_json("tuoguan_knowledge.json", approved)
    return target

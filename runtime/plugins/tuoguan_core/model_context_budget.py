"""Bound dynamic WeCom model context without choosing a business action."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


SIMPLE_CONTEXT_CHAR_BUDGET = 1800
COMPLEX_CONTEXT_CHAR_BUDGET = 3000


@dataclass(frozen=True)
class ContextSection:
    source: str
    text: str
    first: bool = False


_SOURCE_CAPS = {
    "public_employee_identity": 220,
    "xiaoyou_core_skill": 540,
    "capability_facade_manifest": 260,
    "temporal_grounding": 220,
    "task_companion": 480,
    "work_context_snapshot": 460,
    # The summary/evidence payload puts the plain-language subject after its
    # audit metadata; keep enough of it for “什么意思/关掉” to bind correctly.
    "recent_owner_outbound": 900,
    "owner_attention": 280,
    "person_workstyle": 220,
    "role_layer": 180,
    "self_evolution": 180,
    "academic_term": 150,
    "short_reply_contract": 260,
    # A clear low-risk workstyle instruction still changes persistent service
    # behavior.  Its verification rule must survive all normal trimming.
    "workstyle_feedback_contract": 220,
    "verified_write_contract": 320,
}

_DEFAULT_CAP = 180


def is_complex_request(raw_text: str) -> bool:
    """Only choose a context budget; this never selects a tool or workflow."""

    compact = "".join(str(raw_text or "").split())
    if len(compact) > 80:
        return True
    return any(
        term in compact
        for term in (
            "任务", "安排", "完成", "取消", "关闭", "删除", "修改", "创建",
            "制度", "目标", "汇报", "提醒", "学生", "家长", "老师", "店长",
        )
    )


def _priority(
    section: ContextSection,
    *,
    complex_request: bool,
    reference_request: bool,
) -> tuple[int, int, str]:
    source = str(section.source or "")
    critical = {
        "public_employee_identity": 0,
        "xiaoyou_core_skill": 1,
        "capability_facade_manifest": 2,
        "temporal_grounding": 3,
        "short_reply_contract": 3,
        "workstyle_feedback_contract": 3,
        "verified_write_contract": 3,
    }
    active = {
        "task_companion": 4,
        "work_context_snapshot": 4,
        "recent_owner_outbound": 4,
        "owner_attention": 5,
        "person_workstyle": 6,
        "role_layer": 7,
        "self_evolution": 8,
        "academic_term": 9,
    }
    rank = critical.get(source, active.get(source, 10))
    if reference_request and source in {
        "recent_owner_outbound", "work_context_snapshot", "task_companion", "owner_attention",
    }:
        # “什么意思/关掉/继续” is only useful when its concrete subject arrives
        # before generic contracts.  This remains evidence selection, not an
        # intent router.
        rank = -1
    if not complex_request and source in {"task_companion", "work_context_snapshot"}:
        rank += 3
    return (rank, 0 if section.first else 1, source)


def _clip(text: str, limit: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    if limit <= 12:
        return normalized[:limit]
    return normalized[: limit - 11].rstrip() + "\n[材料已裁剪]"


def render_bounded_context(
    sections: Iterable[ContextSection],
    *,
    raw_text: str,
) -> tuple[str, list[str], dict[str, int | str]]:
    """Return deterministic, source-labelled context within a strict budget."""

    complex_request = is_complex_request(raw_text)
    budget = COMPLEX_CONTEXT_CHAR_BUDGET if complex_request else SIMPLE_CONTEXT_CHAR_BUDGET
    compact = "".join(str(raw_text or "").split())
    reference_request = compact in {
        "什么意思", "啥意思", "这个", "刚才那个", "上面那个", "展开", "展开一下",
        "继续", "继续吧", "可以", "关掉", "关闭", "取消", "不用了", "不用再提醒",
    }
    ordered = sorted(
        (item for item in sections if str(item.text or "").strip()),
        key=lambda item: _priority(
            item,
            complex_request=complex_request,
            reference_request=reference_request,
        ),
    )
    kept: list[str] = []
    sources: list[str] = []
    used = 0
    for section in ordered:
        separator = 2 if kept else 0
        remaining = budget - used - separator
        if remaining <= 0:
            break
        source_cap = _SOURCE_CAPS.get(section.source, _DEFAULT_CAP)
        rendered = _clip(section.text, min(source_cap, remaining))
        if not rendered:
            continue
        kept.append(rendered)
        if section.source not in sources:
            sources.append(section.source)
        used += len(rendered) + separator
    return "\n\n".join(kept), sources, {
        "budget_chars": budget,
        "rendered_chars": used,
        "request_complexity": "complex" if complex_request else "simple",
        "source_count": len(sources),
    }

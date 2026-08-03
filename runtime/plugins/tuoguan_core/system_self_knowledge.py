"""Read-only system-state answers and explicit P4 review-test gating."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SELF_KNOWLEDGE_FILES = (
    "hermes_system_state.md",
    "hermes_capability_registry.json",
    "hermes_operating_rules.md",
    "hermes_project_memory.md",
    "hermes_next_actions.md",
    "p4_p5_growth_plan_business_flow.md",
)

_REVIEW_TASK_RE = re.compile(r"P4-(12|13|14|15)-TEST-[A-Z0-9-]+", re.IGNORECASE)
_REVIEW_MARKER_RE = re.compile(r"P4-(12|13|14|15)审核测试", re.IGNORECASE)
_GENERIC_REVIEW_MARKER_RE = re.compile(r"P4审核测试", re.IGNORECASE)


@dataclass(frozen=True)
class SystemSelfKnowledgeResult:
    handled: bool
    query_type: str
    reply: str
    loaded_files: tuple[str, ...]
    read_only: bool = True
    production_data_read: bool = False
    production_data_modified: bool = False
    real_task_created: bool = False
    wecom_pushed: bool = False
    word_generated: bool = False
    document_printed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def explicit_review_test_phase(raw_text: str) -> str:
    """Return the P4 test phase only when a test marker or task id is explicit."""

    text = str(raw_text or "").strip()
    task_match = _REVIEW_TASK_RE.search(text)
    marker_match = _REVIEW_MARKER_RE.search(text)
    if task_match:
        return task_match.group(1)
    if marker_match:
        return marker_match.group(1)
    if _GENERIC_REVIEW_MARKER_RE.search(text):
        return "test"
    return ""


def handle_system_self_knowledge_query(
    raw_text: str,
    *,
    docs_dir: str | Path | None = None,
) -> SystemSelfKnowledgeResult | None:
    """Answer only system-state and P4 safety questions from checked-in docs."""

    text = str(raw_text or "").strip()
    compact = re.sub(r"\s+", "", text)
    query_type = _query_type(compact)
    if not query_type:
        return None

    documents = _load_documents(docs_dir)
    registry = _registry(documents)
    loaded = tuple(documents)
    reply = _reply(query_type, registry)
    return SystemSelfKnowledgeResult(
        handled=True,
        query_type=query_type,
        reply=reply,
        loaded_files=loaded,
    )


def _query_type(compact: str) -> str:
    if not compact:
        return ""
    if (
        "老师说" in compact
        and any(term in compact for term in ("应该怎么处理", "怎么处理", "如何处理"))
        and any(term in compact for term in ("题有点多", "题太多", "题量", "修改意见", "审核"))
    ):
        return "review_language_principle"
    parent_send = any(term in compact for term in ("发给家长", "发送给家长", "发家长"))
    direct_print = "打印" in compact and "直接" in compact
    p4_or_test_student = any(term in compact for term in ("结业成长提升计划", "测试小金", "P4"))
    if direct_print or (parent_send and ("直接" in compact or p4_or_test_student)):
        return "forbidden_action"
    if any(
        term in compact
        for term in (
            "能不能直接上线",
            "可以直接上线",
            "全面上线",
            "所有老师和家长用",
            "给所有老师用",
            "所有老师使用",
        )
    ):
        return "production_readiness"
    if "安全边界" in compact or "哪些不能做" in compact or "绝对不能做" in compact:
        return "safety_boundary"

    p4_scope = any(term in compact for term in ("暑假班结业成长提升计划", "结业成长提升计划", "P4"))
    if p4_scope and any(
        term in compact
        for term in ("当前进度", "哪些阶段", "进度", "已通过", "还没完成", "完成到哪", "当前状态")
    ):
        return "p4_progress"
    if p4_scope and any(term in compact for term in ("下一步", "接下来", "还要做什么")):
        return "next_action"
    if any(
        term in compact
        for term in ("系统能力", "Hermes能力", "有哪些能力", "现在能做什么", "当前能力", "系统自知")
    ):
        return "capabilities"
    return ""


def _docs_path(value: str | Path | None) -> Path:
    if value is not None:
        return Path(value)
    return Path(__file__).resolve().parents[2] / "docs"


def _load_documents(value: str | Path | None) -> dict[str, str]:
    root = _docs_path(value)
    documents: dict[str, str] = {}
    missing: list[str] = []
    for name in SELF_KNOWLEDGE_FILES:
        path = root / name
        if not path.is_file():
            missing.append(name)
            continue
        documents[name] = path.read_text(encoding="utf-8")
    if missing:
        raise FileNotFoundError(f"Hermes system knowledge files missing: {', '.join(missing)}")
    return documents


def _registry(documents: dict[str, str]) -> dict[str, Any]:
    value = json.loads(documents["hermes_capability_registry.json"])
    if not isinstance(value, dict):
        raise ValueError("Hermes capability registry must be an object")
    return value


def _reply(query_type: str, registry: dict[str, Any]) -> str:
    if query_type == "review_language_principle":
        return (
            "在审核上下文中，老师说“数学题有点多”应识别为修改意见 request_change，"
            "表示需要调整数学题量，不能直接判定为通过。"
            "如果当前没有明确审核对象或审核阶段，Hermes 应先追问确认；"
            "无论是否明确，都不会仅凭这句话直接修改真实状态。"
        )
    if query_type == "p4_progress":
        return "\n".join(
            [
                "《暑假班结业成长提升计划》当前进度：",
                "- P4-9 至 P4-15：已通过。",
                "- P4-16 封版验收：已通过。",
                "- P4-17 交付目录整理：已通过。",
                "- P4-8：上线前账号验证项，真实普通老师和申老师账号尚未验证，不能写成已通过。",
                "- 当前未接生产审核，未创建真实审核任务。",
                "- 当前未推送真实普通老师或申老师审核消息，也未生成新的业务 Word。",
                "- 固定交付流程：审核完成后系统生成 Word，由机构人员人工打开、打印并线下交给家长。",
                "- 本项目不包含系统自动发家长、自动打印、自动群发或自动交付。",
                "下一步是小范围真实业务试运行准备；P4-8 作为相关账号上线前的身份和审核权限验证项保留。",
            ]
        )
    if query_type == "production_readiness":
        return (
            "现在不能直接全面启用真实老师审核。"
            "当前审核链路仍是 preview / shadow / test_state，普通老师和申老师真实账号尚未验证；"
            "必须先完成账号身份和审核权限验证，再单独评审小范围试运行、权限、审计和回滚方案。"
            "资料交付始终由机构人员人工打开 Word、打印并线下交给家长。"
        )
    if query_type == "forbidden_action":
        return (
            "Hermes 不负责自动发给家长，也不负责自动打印，这两项不属于本项目流程。"
            "正确流程是老师、店长/申老师和金总完成审核后，由系统生成 Word；"
            "机构人员再人工打开 Word、打印并线下交给家长。当前也没有创建真实审核任务，不能绕过审核和人工确认。"
        )
    if query_type == "next_action":
        return (
            "下一步是准备小范围真实业务试运行。P4-8 继续作为上线前账号验证项，"
            "分别验证一个真实普通老师账号和申老师账号的身份、角色和审核权限。"
            "P4-8 不测试自动发送家长或自动打印；资料最终由机构人员人工打印并交付。"
        )
    if query_type == "safety_boundary":
        forbidden = list(registry.get("forbidden_actions") or [])
        lines = ["Hermes 当前安全边界："]
        lines.extend(f"- {item}" for item in forbidden[:8])
        return "\n".join(lines)
    capabilities = list(registry.get("capabilities") or [])
    available = [item.get("name") for item in capabilities if item.get("status") in {"available", "available_with_confirmation"}]
    testing = [item.get("name") for item in capabilities if item.get("status") == "preview_only"]
    return "\n".join(
        [
            "Hermes 当前已登记能力：",
            f"已可用或需确认后可用：{'、'.join(str(item) for item in available if item)}。",
            f"仅测试状态：{'、'.join(str(item) for item in testing if item)}。",
            "结业成长提升计划审核尚未接生产；P4-8 是尚未完成的上线前账号身份与审核权限验证项。",
        ]
    )

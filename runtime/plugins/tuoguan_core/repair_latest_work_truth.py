from plugins.tuoguan_core.digital_employee_state import (
    HERMES_WORK_ITEMS_FILE,
    update_hermes_work_item,
)
from plugins.tuoguan_core.models import UserIdentity
from plugins.tuoguan_core.store import TuoguanStore
from plugins.tuoguan_core.write_guard import authorized_system_write


store = TuoguanStore()
identity = UserIdentity(
    platform="system",
    platform_user_id="codex_truth_repair",
    canonical_user_id="codex_truth_repair",
    person_name="Codex audited state repair",
    role="boss",
    approval_state="approved",
)
with authorized_system_write(
    store.data_dir,
    job_name="codex_autonomous_truth_repair_20260729",
    allowed_files={HERMES_WORK_ITEMS_FILE},
):
    result = update_hermes_work_item(
        store,
        identity=identity,
        work_item_id="hermes_work_0572c5c0e1f3",
        operation_id="system:codex_autonomous_truth_repair:20260729:v2",
        focus_summary=(
            "老板已确认优先顺序并授权 Hermes 按该顺序继续推进。当前阶段为历史数据分析"
            "与新学期准备；不再等待老板确认计划。"
        ),
        next_actions=[
            "基于可信历史记录整理沟通覆盖与记录覆盖分析",
            "核验S级安全事项数据源矛盾，缺真实详情时向老板提出一个明确问题",
            "形成风险筛查框架和第一批沟通候选参考，明确不是新学期名单",
            "2026-08-25起重新确认新学期名单、服务类型和责任老师",
        ],
        confirmed_facts=[
            "老板已确认目标：九月份续费率更稳",
            "老板已确认优先顺序：续费、稳定、服务质量、风险控制",
            "老板14:16明确授权按当前优先顺序继续推进，下一次醒来接着做",
            "当前工作项阶段为历史数据分析与新学期准备",
        ],
        pending_judgements=[
            "上学期续费结果与未续原因尚无结构化可信数据",
            "经营概览与开放任务查询对S级安全事项的结果不一致，需核验真实详情",
        ],
        value_progress_note=(
            "已收到老板确认的优先顺序。14:43 和 14:48 唤醒形成了老板沟通候选，"
            "但没有新的企业微信发送回执，不记为已外发。"
        ),
        progress_evidence=[{
            "evidence_type": "state_truth_correction",
            "summary": (
                "老板14:16已授权继续推进；无企业微信发送回执的沟通只记录为候选，"
                "不能把计划确认继续当成当前等待项。"
            ),
            "verified_at": "2026-07-29T15:03:48+08:00",
        }],
        update_text=(
            "状态真实性修正：保留真实分析发现；没有发送回执的老板沟通只能记为候选，"
            "不能记为已经汇报。"
        ),
        source_text="Audited correction after autonomous closed-loop verification.",
        source_message_id="codex_truth_repair_20260729",
    )
print(result)

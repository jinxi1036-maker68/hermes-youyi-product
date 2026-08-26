"""Hermes-visible tool schemas for the tutoring-center business module."""

from __future__ import annotations

import inspect
import os
from typing import Any, Callable

from gateway.session_context import get_session_env
from tools.registry import tool_result

from .tool_service import TuoguanToolService


TOOLSET = "tuoguan"

CONTEXT_DESCRIPTION = (
    "托管业务可信工具目录。企业微信是老师、店长和老板的主入口；当模型需要读取或改变"
    "学生、任务、风险、积分、经营数据或家长沟通参考等真实业务事实时，可调用这些工具。"
    "H5/看板只做只读展示；工资、打印、自动发家长、删除和回滚属于高风险事项，未取得可信工具结果前"
    "不得承诺已经执行。身份、角色和权限只来自网关可信会话；模型不得推断、承诺或修改用户角色、校区和权限。"
)


def _identity_props(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    props = {
        "platform": {
            "type": "string",
            "description": "当前消息平台，企业微信 callback 使用 wecom_callback。",
            "default": "wecom_callback",
        },
        "user_id": {
            "type": "string",
            "description": "可省略；系统始终使用当前企业微信可信会话身份，模型不得编造或切换。",
        },
        "user_name": {"type": "string", "description": "当前用户显示名，可为空。"},
        "chat_id": {"type": "string", "description": "当前会话 chat_id，可为空。"},
        "session_key": {"type": "string", "description": "当前 Hermes 会话 key，可为空。"},
    }
    props.update(extra or {})
    return props


def _schema(description: str, props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    # Identity comes from the trusted gateway session, never from model text.
    # Keeping user_id model-required caused avoidable failed tool turns after
    # fresh-session resets even though _service already ignores it when the
    # gateway supplies HERMES_SESSION_USER_ID.
    effective_required = [item for item in (required or []) if item not in {"user_id", "operation_id"}]
    return {
        "description": description,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": effective_required,
        },
    }


def _service(args: dict[str, Any]) -> TuoguanToolService:
    return TuoguanToolService(
        platform=get_session_env("HERMES_SESSION_PLATFORM", "") or str(args.get("platform") or "wecom_callback"),
        user_id=get_session_env("HERMES_SESSION_USER_ID", "") or str(args.get("user_id") or ""),
        user_name=get_session_env("HERMES_SESSION_USER_NAME", "") or str(args.get("user_name") or ""),
        chat_id=get_session_env("HERMES_SESSION_CHAT_ID", "") or str(args.get("chat_id") or ""),
        session_key=get_session_env("HERMES_SESSION_KEY", "") or str(args.get("session_key") or ""),
    )


def _handler(method: str) -> Callable[[dict[str, Any]], str]:
    def run(args: dict[str, Any], **_: Any) -> str:
        service = _service(args)
        # Model-led production: do not run the runtime contract router before tools.
        # Tool services still enforce trusted identity, role permissions and write guards.
        payload = {
            key: value
            for key, value in dict(args or {}).items()
            if key not in {"platform", "user_id", "user_name", "chat_id", "session_key"}
        }
        method_fn = getattr(service, method)
        signature = inspect.signature(method_fn)
        missing_business_arguments = sorted(
            name
            for name, parameter in signature.parameters.items()
            if name != "operation_id"
            and parameter.default is inspect.Parameter.empty
            and parameter.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
            and name not in payload
        )
        if missing_business_arguments:
            return tool_result({
                "ok": False,
                "error": "missing_required_arguments",
                "message": "工具缺少必填参数，本轮没有执行；请补齐后重新调用。",
                "data": {
                    "tool": f"tuoguan_{method}",
                    "missing_required_arguments": missing_business_arguments,
                    "supported_arguments": sorted(signature.parameters),
                },
            })
        if "operation_id" in signature.parameters and not str(payload.get("operation_id") or "").strip():
            operation_id = str(get_session_env("HERMES_SESSION_MESSAGE_ID", "") or "").strip()
            if not operation_id:
                return tool_result({
                    "ok": False,
                    "error": "missing_trusted_operation_id",
                    "message": "当前会话缺少可信消息编号，本轮没有执行写入。",
                })
            payload["operation_id"] = operation_id
        accepts_extra = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        unsupported = sorted(
            key for key in payload
            if key not in signature.parameters and not accepts_extra
        )
        if unsupported:
            return tool_result({
                "ok": False,
                "error": "unsupported_arguments",
                "message": "工具收到了不支持的参数，请按当前工具说明重新调用。",
                "data": {
                    "tool": f"tuoguan_{method}",
                    "unsupported_arguments": unsupported,
                    "supported_arguments": sorted(signature.parameters),
                },
            })
        missing_required = sorted(
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
            and name not in payload
        )
        if missing_required:
            return tool_result({
                "ok": False,
                "error": "missing_required_arguments",
                "message": "工具缺少必填参数，本轮没有执行；请补齐后重新调用。",
                "data": {
                    "tool": f"tuoguan_{method}",
                    "missing_required_arguments": missing_required,
                    "supported_arguments": sorted(signature.parameters),
                },
            })
        return tool_result(method_fn(**payload))

    return run


TUOGUAN_CONTEXT_SCHEMA = _schema(
    CONTEXT_DESCRIPTION + " 读取当前可信身份、角色、可见范围、当前任务和焦点上下文。适合在模型需要确认当前用户身份、权限范围或上下文事实时使用；如需学生明细，请调用学生查询工具。",
    _identity_props(),
    ["user_id"],
)

TUOGUAN_QUERY_STUDENTS_SCHEMA = _schema(
    "按当前可信身份和权限范围查询学生、学生名单、年级范围和最近记录。可按学生姓名、老师姓名、暑假班范围或年级筛选；工具负责执行权限收口和数据返回。这个工具只返回学生事实，不负责审查机构目标、制定覆盖计划或判断目标是否合理；老板提出经营目标、覆盖目标、每个孩子都要完成某项工作时，应优先考虑 tuoguan_goal_workspace。",
    _identity_props({
        "student_name": {"type": "string", "description": "学生姓名；查询自己班/名下/负责范围学生名单或数量时留空。"},
        "teacher_name": {"type": "string", "description": "老板/店长代查某位老师负责范围时填写老师姓名；老师查自己范围时留空。"},
        "name": {"type": "string", "description": "student_name 的兼容别名；优先填写 student_name。"},
        "query_scope": {"type": "string", "enum": ["visible", "regular", "summer"], "description": "默认 visible；正式托管班（不含暑假班口径）填 regular；用户明确查暑假班孩子时才填 summer。"},
        "grade": {"type": "string", "description": "按年级筛选时填写，如一年级、二年级；不筛选年级时留空。"},
        "limit": {"type": "integer", "default": 30, "description": "最多返回多少名学生，范围 1-100。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_TASKS_SCHEMA = _schema(
    "按当前可信身份和权限范围查询任务、任务状态、来源、触发原因、缺口和闭环证据。可按任务、学生、老师、状态、等级和查询范围筛选；工具负责权限收口。",
    _identity_props(
        {
            "task_id": {"type": "string", "description": "任务 id，可为空。"},
            "student_name": {"type": "string", "description": "学生姓名，可为空。"},
            "teacher_name": {"type": "string", "description": "老板/店长查询某位老师任务时填写老师姓名；老师查自己任务时留空。"},
            "assignee_user_id": {"type": "string", "description": "按执行人的企业微信 user id 筛选；已知姓名时优先填写 teacher_name。"},
            "status": {"type": "string", "description": "任务状态，可为空。"},
            "level": {"type": "string", "description": "S/A/B/C，可为空。"},
            "scope": {
                "type": "string",
                "enum": ["mine", "all"],
                "description": "查询范围。我的任务必须传 mine；老师请求 all 时系统仍收口为 mine。",
            },
            "limit": {"type": "integer", "default": 20, "description": "最多返回多少条任务，范围 1-100。"},
        }
    ),
    ["user_id"],
)

TUOGUAN_NEXT_TASK_SCHEMA = _schema(
    "直接选择当前老师下一项最高优先级的真实待办，锁定任务焦点并立即给出陪伴式操作引导，不返回整张列表。老师说‘开始这个任务/继续做当前任务/下一项做什么’时优先使用；老师正在汇报已经发生的进展、结果或完成证据时不要调用，应使用 tuoguan_update_task。",
    _identity_props(),
    ["user_id"],
)

TUOGUAN_CURRENT_TASK_GUIDANCE_SCHEMA = _schema(
    "读取当前已锁定任务的真实状态和缺失信息，使用中文给老师下一步陪伴引导；只读，不更新任务。只在任务已锁定且老师说不会做、不知道怎么说或询问具体下一步时使用；若只是说‘继续’但当前线程不唯一，先查活动上下文。老师正在汇报进展、家长态度、后续安排或完成结果时不要调用，应使用 tuoguan_update_task 保存证据。",
    _identity_props(),
    ["user_id"],
)

TUOGUAN_RECORD_STUDENT_SCHEMA = _schema(
    "记录学生情况并触发混合分析。写操作必须提供 operation_id，重复 operation_id 不会重复写入。",
    _identity_props(
        {
            "student_name": {"type": "string", "description": "学生姓名。"},
            "content": {"type": "string", "description": "老师自然语言记录内容。"},
            "operation_id": {"type": "string", "description": "幂等写入 id，来自当前消息 id 或稳定哈希。"},
        }
    ),
    ["user_id", "student_name", "content", "operation_id"],
)

TUOGUAN_CHANGE_SUMMER_POINTS_SCHEMA = _schema(
    "给暑假班学生加分、扣分、兑换或拍卖扣分。积分由工具计算，必须提供 operation_id；重复消息不会重复计分。",
    _identity_props({
        "student_name": {"type": "string", "description": "学生姓名或可唯一识别的称呼。"},
        "delta_points": {"type": "integer", "description": "明确分值；加分为正，扣分可传负数。"},
        "reason_text": {"type": "string", "description": "用户原话中的积分原因，不得添加事实。"},
        "reason_type": {"type": "string", "enum": ["reward", "penalty", "exchange", "auction", "correction"]},
        "operation_id": {"type": "string", "description": "使用当前消息 id 作为幂等键。"},
    }),
    ["user_id", "student_name", "operation_id"],
)

TUOGUAN_QUERY_SUMMER_POINTS_SCHEMA = _schema(
    "查询单个学生当前暑假班积分及最近变动。当前积分必须来自 summer_points.json。",
    _identity_props({
        "student_name": {"type": "string"},
        "detail_limit": {"type": "integer", "default": 3},
    }),
    ["user_id", "student_name"],
)

TUOGUAN_QUERY_SUMMER_POINTS_RANKING_SCHEMA = _schema(
    "确定性查询暑假班积分排行榜，禁止模型自行计算或改写排名。",
    _identity_props({"limit": {"type": "integer", "default": 10}}),
    ["user_id"],
)

TUOGUAN_RECORD_SUMMER_LESSON_SCHEMA = _schema(
    "记录暑假班一节课的整体情况和点名学生个别表现。必须逐字传入老师原话；工具按课程表、班级和出勤生成覆盖，范围不明确时只追问、不写入。",
    _identity_props(
        {
            "raw_text": {"type": "string", "description": "老师本轮完整原话，不得润色、概括或增加事实。"},
            "operation_id": {"type": "string", "description": "幂等写入 id，来自当前消息 id 或稳定哈希。"},
        }
    ),
    ["user_id", "raw_text", "operation_id"],
)

TUOGUAN_REGISTER_STUDENT_SCHEMA = _schema(
    "登记正式学生并反查学生主库和老师负责关系。仅在字段完整、无重复冲突时写入。",
    _identity_props({
        "student_name": {"type": "string"}, "parent_phone": {"type": "string"},
        "grade": {"type": "string"}, "campus_id": {"type": "string", "default": "main"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "student_name", "parent_phone", "operation_id"],
)

TUOGUAN_REGISTER_SUMMER_STUDENT_SCHEMA = _schema(
    "登记2026暑假班学生，同时写报名关系和学生档案 program_id=summer_2026，并反查H5可见条件。",
    _identity_props({
        "student_name": {"type": "string"}, "parent_phone": {"type": "string"},
        "grade": {"type": "string"}, "attendance_mode": {"type": "string", "enum": ["full_day", "morning_only"]},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "student_name", "parent_phone", "grade", "operation_id"],
)

TUOGUAN_CREATE_TRIAL_LEAD_SCHEMA = _schema(
    "登记试听或未报名学生线索，不进入正式学生库，并生成三项A级跟进任务。",
    _identity_props({
        "student_name": {"type": "string"}, "parent_phone": {"type": "string"},
        "observation": {"type": "string"}, "age_or_grade": {"type": "string"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "student_name", "parent_phone", "operation_id"],
)

TUOGUAN_CREATE_TASK_SCHEMA = _schema(
    "老板或店长创建并分配具体的一次性内部任务。执行人可以传 teacher_name（优先，使用老板说出的姓名）或 assignee_user_id；系统会从可信企业微信目录解析在职且可达的人。title 写任务本身；老板当前原话由系统自动保存，不需要传 description。创建回执会区分任务已建立、提醒已入队和企业微信是否已经送达。不要用本工具来确认机构级目标、月度目标或长期覆盖计划。",
    _identity_props({
        "title": {"type": "string", "description": "要执行的具体任务，不要重复整段工具说明。"},
        "teacher_name": {"type": "string", "description": "优先填老板说出的执行人姓名、昵称或老师称呼；系统会解析企业微信账号。"},
        "assignee_user_id": {"type": "string", "description": "已知企业微信 user_id 时填写；已知姓名时留空。"},
        "due_at": {"type": "string", "description": "可选，使用北京时间的明确时间；如上午10点、今天16:00。"},
        "level": {"type": "string", "enum": ["S", "A", "B", "C"], "description": "可选，默认 A。"},
        "student_name": {"type": "string", "description": "可选，关联的真实学生姓名。"},
        "goal_id": {"type": "string", "description": "可选；小优自主创建目标内低风险子任务时必须填写已确认目标 id。"},
        "goal_action_id": {"type": "string", "description": "可选；关联持久目标行动。"},
        "parent_work_item_id": {"type": "string", "description": "可选：已获落实授权的机构工作事项 id。没有此关联不得把制度草案下发为员工任务。"},
        "artifact_version_id": {"type": "string", "description": "可选：机构执行任务关联的已授权成果版本。"},
        "evidence_requirement": {"type": "string", "description": "可选；任务闭环必须拿到的真实证据。"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "title", "operation_id"],
)

TUOGUAN_CANCEL_TASK_SCHEMA = _schema(
    "取消一个已有托管内部任务。适合老板/店长/老师明确说取消、撤回、不用做、刚才那个任务不要了时调用；工具会按可信任务上下文、任务 id、学生或老师解析目标任务，写入 cancelled，压住未发送提醒，清理任务上下文并写后反查。不得用来取消机构级目标，目标撤回请用 tuoguan_goal_workspace。",
    _identity_props({
        "task_id": {"type": "string", "description": "任务 id，可为空；为空时工具按可信焦点、学生、老师和可见任务解析。"},
        "reason": {"type": "string", "description": "取消原因，优先传用户原话，不得替用户编造。"},
        "student_name": {"type": "string", "description": "可选，任务关联学生。"},
        "teacher_name": {"type": "string", "description": "可选，老板/店长取消某位老师任务时填写老师姓名。"},
        "operation_id": {"type": "string", "description": "幂等写入 id。"},
    }),
    ["user_id", "operation_id"],
)

TUOGUAN_QUERY_OPERATIONS_REPORT_SCHEMA = _schema(
    "老板查询经营、老师名单、指定老师近期执行、老师工作量、试听跟进、日报或周报。用户说查老师、有哪些老师、几位老师时用 staff；说查某老师最近怎么样时用 teacher_activity。所有数字和名单由工具确定性生成。不要用于 H5、看板链接、打开看板、看板地址，这些请求使用 tuoguan_dashboard_link。",
    _identity_props({
        "report_type": {"type": "string", "enum": ["operations", "teacher_workload", "trial_follow_up", "daily", "weekly"], "description": "兼容旧报表类型；普通经营查询可留空。"},
        "query_type": {"type": "string", "enum": ["overview", "teacher_records", "open_tasks", "safety_tasks", "summer_students", "points", "staff", "teacher_activity"], "description": "查老师人数或老师名单用 staff；查指定老师最近怎么样用 teacher_activity；查机构经营用 overview。"},
        "teacher_name": {"type": "string", "description": "仅 query_type=teacher_activity 时填写原话中的老师姓名。"},
    }),
    ["user_id"],
)

TUOGUAN_VERIFY_DASHBOARD_VISIBILITY_SCHEMA = _schema(
    "只读校验暑假班报名、学生档案规范项目关系和H5可见条件，不直接修改dashboard_cache。",
    _identity_props({"student_name": {"type": "string"}}),
    ["user_id"],
)

TUOGUAN_DASHBOARD_LINK_SCHEMA = _schema(
    "生成当前账号自己的托管 AI 看板签名链接。只读；只返回可访问链接，不查询经营数字；权限、角色和签名有效期由工具校验。",
    _identity_props(),
    ["user_id"],
)

TUOGUAN_UPDATE_TASK_SCHEMA = _schema(
    "保存老师或负责人刚刚说出的任务反馈、执行进展、家长态度、后续安排或完成结果，并推进原任务闭环。用户是在汇报已经发生的任务事实时必须优先使用本工具，不要退回只读指导。不要用本工具取消、关闭、删除任务或停止提醒；老板/店长/老师明确说取消、关闭、删掉、不用做、不用再提醒时，必须改用 tuoguan_cancel_task。本工具按明确任务 id、点名对象和可信任务上下文解析目标任务，并保护无关 S 级安全任务不被普通任务话术误修改。",
    _identity_props(
        {
            "task_id": {"type": "string", "description": "任务 id，可为空；为空时系统按本轮可信任务上下文解析。"},
            "reply": {"type": "string", "description": "老师或负责人本次补充内容。取消、关闭、删除或停止提醒类原话不要传给本工具，应调用 tuoguan_cancel_task。"},
            "action": {"type": "string", "enum": ["progress", "request_completion"], "description": "汇报过程、提问或补充事实用 progress；老师明确说已做完、已联系、已处理好时用 request_completion。"},
            "operation_id": {"type": "string", "description": "幂等写入 id。"},
        }
    ),
    ["user_id", "reply", "operation_id"],
)

TUOGUAN_REPORT_SAFETY_EVENT_SCHEMA = _schema(
    "上报 S 级安全事件。用于碰到头、撞到胳膊、腿疼哭了等安全风险；创建后必须进入通知闭环。",
    _identity_props(
        {
            "student_name": {"type": "string", "description": "学生姓名。"},
            "event": {"type": "string", "description": "安全事件经过和当前状态。"},
            "operation_id": {"type": "string", "description": "幂等写入 id。"},
        }
    ),
    ["user_id", "student_name", "event", "operation_id"],
)

TUOGUAN_PARENT_SCRIPT_CONTEXT_SCHEMA = _schema(
    "准备家长沟通或续费沟通参考。不会直接发给家长，只给老师/老板整理话术和后续记录提醒。",
    _identity_props(
        {
            "request": {"type": "string", "description": "沟通需求。"},
            "student_name": {"type": "string", "description": "学生姓名，可为空。"},
        }
    ),
    ["user_id", "request"],
)


TUOGUAN_GOAL_WORKSPACE_SCHEMA = _schema(
    "目标驱动自主运营员工的统一目标工作区。模型已经理解到用户在处理长期经营目标、老板确认执行方案、查看目标进度、记录目标推进事实、撤回测试/过期目标或询问下一步时，可调用本工具获得目标阶段、真实数据、允许动作、禁止动作和下一步边界。它不是关键词路由器；不会替模型决定用户意图。确认目标只保存目标工作区，不自动派发老师任务、不自动给家长发消息；撤回目标只把它移出当前推进材料，保留历史审计，不删除业务数据。",
    _identity_props({
        "action": {"type": "string", "enum": ["review", "confirm", "query_progress", "next_step", "record_progress", "withdraw"], "description": "模型决定的目标工作区动作；老板明确说测试目标、撤掉、取消或不再推进时用 withdraw。"},
        "goal_text": {"type": "string", "description": "老板目标或正在推进的目标文本。"},
        "confirmation_text": {"type": "string", "description": "老板确认、调整或拍板方案的原话。"},
        "goal_id": {"type": "string", "description": "已知目标 id；不知道时可留空。"},
        "student_name": {"type": "string", "description": "记录进度时对应学生；其他动作可留空。"},
        "update_text": {"type": "string", "description": "记录进度时用户明确说出的事实原文；不得添加推断。"},
        "withdraw_reason": {"type": "string", "description": "撤回目标的老板原话或原因；例如这是测试目标、目标已过期、老板明确不再推进。"},
        "operation_id": {"type": "string", "description": "写入动作使用当前消息 id 作为幂等键；只读动作可为空。"},
        "goal_type": {"type": "string", "enum": ["parent_communication_coverage"], "default": "parent_communication_coverage"},
        "program_id": {"type": "string", "default": "regular_tuoguan", "description": "当前第一版用于正式托管。"},
    }),
    ["user_id", "action"],
)

TUOGUAN_REVIEW_GOAL_SCHEMA = _schema(
    "目标驱动自主运营员工的经营目标审查工具。老板提出机构目标、月度目标、覆盖目标、每个正式托管孩子都要完成某项内部工作、家长沟通覆盖、记录覆盖或类似经营安排时，模型可调用本工具先读取真实学生、老师、记录、任务和责任缺口，判断目标是否合理，提出有事实依据的反对意见、风险和分批计划。只读；不启动执行，不自动创建任务，不自动发家长；老板确认前只给分析和建议。",
    _identity_props({
        "goal_text": {"type": "string", "description": "老板原始目标或想法，保持原意。"},
        "goal_type": {"type": "string", "enum": ["parent_communication_coverage"], "default": "parent_communication_coverage"},
        "program_id": {"type": "string", "default": "regular_tuoguan", "description": "当前第一版先用于正式托管班；暑假班暂不纳入目标推进。"},
    }),
    ["user_id", "goal_text"],
)

TUOGUAN_CONFIRM_GOAL_SCHEMA = _schema(
    "老板确认、同意、批准或拍板机构级目标执行方案后，必须先用本工具保存内部目标计划和推进清单。保存目标不等于派发任务；不会自动给家长发消息，不会自动通知老师，不改工资、不改权限。写入必须提供 operation_id。目标未保存前，不要直接调用 tuoguan_create_task 分派老师任务。",
    _identity_props({
        "goal_text": {"type": "string", "description": "被确认的目标文本。"},
        "confirmation_text": {"type": "string", "description": "老板确认或调整方案的原话。"},
        "operation_id": {"type": "string", "description": "使用当前消息 id 作为幂等键。"},
        "goal_type": {"type": "string", "enum": ["parent_communication_coverage"], "default": "parent_communication_coverage"},
        "program_id": {"type": "string", "default": "regular_tuoguan"},
    }),
    ["user_id", "goal_text", "confirmation_text", "operation_id"],
)

TUOGUAN_UPDATE_GOAL_PROGRESS_SCHEMA = _schema(
    "记录老师、店长或老板明确说出的目标推进进展，例如某学生家长沟通反馈。只保存用户明确事实，不自动联系家长；写入必须提供 operation_id。",
    _identity_props({
        "goal_id": {"type": "string", "description": "已知目标 id；不知道时可留空，由工具查当前活动目标。"},
        "student_name": {"type": "string", "description": "本次反馈对应的学生姓名。"},
        "update_text": {"type": "string", "description": "用户原话中的进展事实，不得添加推断。"},
        "operation_id": {"type": "string", "description": "使用当前消息 id 作为幂等键。"},
    }),
    ["user_id", "student_name", "update_text", "operation_id"],
)

TUOGUAN_QUERY_GOAL_PROGRESS_SCHEMA = _schema(
    "查询已确认机构目标的真实推进进度、缺口和还差哪些学生。只读，适合老板或店长查看目标进展。",
    _identity_props({"goal_id": {"type": "string", "description": "目标 id；留空查询当前活动目标。"}}),
    ["user_id"],
)


TUOGUAN_RESOLVE_STUDENT_RESPONSIBILITY_SCHEMA = _schema(
    "只读查询优益正式托管学生的责任归属：午托、晚托、全托、主责老师以及缺失字段。用于模型需要判断该问哪位老师、是否应先问店长/老板补责任时；不得用于自动分组或凭空安排。",
    _identity_props({
        "student_name": {"type": "string", "description": "学生姓名。"},
        "purpose": {"type": "string", "enum": ["parent_communication", "meal_nap_pickup_safety", "homework_learning_evening"], "default": "parent_communication"},
    }),
    ["user_id", "student_name"],
)

TUOGUAN_QUERY_INSTITUTION_ONBOARDING_GAPS_SCHEMA = _schema(
    "只读盘点当前机构入职调研缺口：业务线、员工、学生服务类型、责任老师、老板当前目标等。用于模型判断自己还不了解什么、应该问老板还是店长；不得替模型决定业务意图。",
    _identity_props({
        "program_id": {"type": "string", "default": "regular_tuoguan", "description": "默认正式托管；暑假班作为独立业务线时由模型明确指定。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_OPERATIONAL_FACTS_SCHEMA = _schema(
    "查询已确认或待确认的机构运营事实。事实用于辅助模型理解机构，不是业务数据库，也不能替代学生、任务、记录等正式工具结果。",
    _identity_props({
        "fact_type": {"type": "string", "description": "可选，如 manager_scope、teacher_responsibility、owner_current_goal。"},
        "subject": {"type": "string", "description": "可选，事实对象。"},
        "scope": {"type": "string", "description": "可选，事实适用范围。"},
        "include_pending": {"type": "boolean", "default": False, "description": "是否同时返回待确认事实。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_STAFF_DIRECTORY_SCHEMA = _schema(
    "只读查询托管机构人员目录、企业微信通讯录缓存、人员白名单、姓名映射和已确认人员事实。适合模型需要核验老师、店长、老板、企业微信昵称、表情昵称、user_id、在职/授权状态或人员变更候选时使用；不会修改任何人员配置，修复候选必须由老板确认后才可保存为运营事实。",
    _identity_props({
        "query": {"type": "string", "description": "可选，姓名、昵称、老师称呼、手机号片段或企业微信 user_id；留空返回当前可见人员目录。"},
        "role": {"type": "string", "enum": ["", "boss", "manager", "teacher", "staff"], "default": "", "description": "可选角色筛选。"},
        "include_inactive": {"type": "boolean", "default": False, "description": "是否包含历史映射、已授权但通讯录缺失或待确认人员。"},
        "limit": {"type": "integer", "default": 30, "description": "最多返回人数。"},
    }),
    ["user_id"],
)

TUOGUAN_OFFBOARD_STAFF_SCHEMA = _schema(
    "老板专属人员离职管理。金总明确要求删除、移除或停用一位已离职老师/店长时使用：撤销托管业务访问、停止未发送提醒并保留历史任务和服务记录。该操作不删除企业微信组织通讯录成员；只有 ok=true 且 writeback_verified=true 才能说已经完成。",
    _identity_props({
        "target_name": {"type": "string", "description": "离职人员姓名或常用称呼；与 target_user_id 至少提供一个。"},
        "target_user_id": {"type": "string", "description": "可选，企业微信 user_id；同名时必须提供。"},
        "reason": {"type": "string", "description": "可选，离职或停用原因。"},
        "operation_id": {"type": "string", "description": "当前可信消息 id，作为幂等键。"},
    }),
    ["user_id", "operation_id"],
)

TUOGUAN_QUERY_PERSON_WORKSTYLE_PROFILE_SCHEMA = _schema(
    "只读查询某个人希望小优怎样服务自己，包括汇报长短、语气、提醒时间、跟进方式、细节程度、格式偏好和不要怎样说。档案只影响小优的服务方式，不改变权限、制度、工资、家长外发、正式任务或事实判断。",
    _identity_props({
        "target_user_id": {"type": "string", "description": "可选，默认查询当前会话人员；老板可查其他人员。"},
        "target_role": {"type": "string", "enum": ["", "boss", "manager", "teacher", "staff"], "default": "", "description": "可选，目标人员角色。"},
        "scope": {"type": "string", "enum": ["", "daily_report", "direct_reply", "task_followup", "proactive_question", "teacher_support", "manager_support", "all_communication"], "default": "", "description": "可选，偏好适用场景。"},
        "limit": {"type": "integer", "default": 30, "description": "最多返回偏好条数。"},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_PERSON_WORKSTYLE_PREFERENCE_SCHEMA = _schema(
    "保存低风险个人服务方式偏好，例如更短汇报、少说过程、五点后提醒、语气更直接、只列重点。该工具只保存工作方式，不改权限、制度、工资、家长外发、数据删除或正式任务；工具返回 ok=true 且 writeback_verified=true 后才可表达已经保存。",
    _identity_props({
        "preference_type": {"type": "string", "enum": ["report_length", "tone", "reminder_time", "followup_style", "detail_level", "format", "avoidance", "positive_preference", "other_low_risk"], "description": "偏好类型。"},
        "scope": {"type": "string", "enum": ["daily_report", "direct_reply", "task_followup", "proactive_question", "teacher_support", "manager_support", "all_communication"], "description": "偏好适用场景。"},
        "preference_text": {"type": "string", "description": "用户明确表达的偏好内容，保留原意。"},
        "preference": {"type": "string", "description": "可选兼容字段；等同于 preference_text，仍只允许低风险工作方式偏好。"},
        "normalized_rule": {"type": "string", "description": "可选，将偏好整理成简短规则；不得加入用户没有表达的事实。"},
        "dimension_key": {"type": "string", "enum": ["", "length", "layout", "structure", "tone", "timing", "detail", "avoidance", "followup_method", "interaction_pacing", "other"], "default": "", "description": "可选，偏好影响的工作方式维度；留空由系统按文本推断。"},
        "confidence": {"type": "number", "default": 1.0, "description": "模型对这条低风险工作方式偏好的置信度，0-1。"},
        "source_turn_id": {"type": "string", "description": "可选，当前会话轮次 id。"},
        "target_user_id": {"type": "string", "description": "可选，默认保存到当前会话人员。"},
        "target_name": {"type": "string", "description": "可选，目标人员显示名。"},
        "target_role": {"type": "string", "enum": ["", "boss", "manager", "teacher", "staff"], "default": "", "description": "可选，目标人员角色。"},
        "source_text": {"type": "string", "description": "用户原话，不得改写成制度。"},
        "operation_id": {"type": "string", "description": "使用当前消息 id 作为幂等键。"},
    }),
    ["user_id", "preference_type", "scope", "preference_text", "operation_id"],
)

TUOGUAN_QUERY_WORKSTYLE_ADAPTATION_HEALTH_SCHEMA = _schema(
    "只读查询小优是否真的把工作方式反馈保存、应用和复盘，包括漏保存、未验证承诺、应用失败。它不发消息、不改偏好、不改变制度权限。老板/店长用于核验小优有没有从嘴上答应变成实际执行。",
    _identity_props({
        "target_user_id": {"type": "string", "description": "可选，只看某个人的工作方式自适应情况。"},
        "scope": {"type": "string", "enum": ["", "daily_report", "direct_reply", "task_followup", "proactive_question", "teacher_support", "manager_support", "all_communication"], "default": "", "description": "可选，只看某个场景。"},
        "limit": {"type": "integer", "default": 30, "description": "最多返回最近记录条数。"},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_OPERATIONAL_FACT_SCHEMA = _schema(
    "把用户明确说出的机构运营事实保存为候选或已确认事实，带来源和范围。适合模型在问清缺口后记录答案；不得保存模型推断为已确认事实。",
    _identity_props({
        "fact_type": {"type": "string", "description": "事实类型，如 manager_scope、student_service_type、teacher_responsibility、owner_current_goal。"},
        "subject": {"type": "string", "description": "事实对象，如正式托管店长、赵奕哲主责老师、本月经营目标。"},
        "value": {"description": "用户明确给出的事实值，可以是字符串或结构化对象。"},
        "scope": {"type": "string", "default": "institution", "description": "适用范围，如 institution、program:regular_tuoguan、student:赵奕哲。"},
        "source_text": {"type": "string", "description": "用户原话，不得添加推断。"},
        "operation_id": {"type": "string", "description": "使用当前消息 id 作为幂等键。"},
    }),
    ["user_id", "fact_type", "subject", "value", "operation_id"],
)

TUOGUAN_CONFIRM_OPERATIONAL_FACT_SCHEMA = _schema(
    "老板确认或驳回一条待确认运营事实。高风险事实必须老板确认后才可用于后续判断。",
    _identity_props({
        "candidate_id": {"type": "string", "description": "待确认事实候选 id。"},
        "decision": {"type": "string", "enum": ["approve", "reject"]},
        "operation_id": {"type": "string", "description": "使用当前消息 id 作为幂等键。"},
        "note": {"type": "string", "description": "可选，老板补充说明。"},
    }),
    ["user_id", "candidate_id", "decision", "operation_id"],
)

TUOGUAN_QUERY_STUDENT_SERVICE_RELATIONS_SCHEMA = _schema(
    "只读查询学生服务关系：项目、午托/晚托/全托、责任老师、统一负责人、生效状态和缺口。该工具提供事实材料，不自动指定责任人。",
    _identity_props({
        "student_name": {"type": "string", "description": "学生姓名；留空查询当前可见范围。"},
        "program_id": {"type": "string", "default": "regular_tuoguan", "description": "默认正式托管。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_PARENT_COMMUNICATION_COVERAGE_SCHEMA = _schema(
    "只读查询日常托管学生近期家校沟通证据覆盖。结果来自真实记录中的沟通证据，不发送家长消息，也不证明家长满意。",
    _identity_props({
        "days": {"type": "integer", "default": 31, "description": "统计最近多少天，默认31天。"},
        "program_id": {"type": "string", "default": "regular_tuoguan"},
        "student_name": {"type": "string", "description": "可选，只查看一名当前账号有权查看的学生。"},
        "limit": {"type": "integer", "default": 30, "description": "列表最多展示多少名学生，范围 1-100。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_WEEKLY_RECORD_COVERAGE_SCHEMA = _schema(
    "只读查询日常托管学生近期表现记录覆盖。用于发现记录缺口，不自动派任务、不自动扣绩效。",
    _identity_props({
        "days": {"type": "integer", "default": 7, "description": "统计最近多少天，默认7天。"},
        "program_id": {"type": "string", "default": "regular_tuoguan"},
        "student_name": {"type": "string", "description": "可选，只查看一名当前账号有权查看的学生。"},
        "limit": {"type": "integer", "default": 30, "description": "列表最多展示多少名学生，范围 1-100。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_ACTIVE_GOAL_WORK_STATE_SCHEMA = _schema(
    "只读查询当前活跃目标工作状态和目标推进证据候选。不会自动推进目标、派任务或联系家长。",
    _identity_props({
        "goal_id": {"type": "string", "description": "目标 id；留空查询当前活跃目标。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_PROFILE_CANDIDATES_SCHEMA = _schema(
    "只读查询学生、老师、老板或机构画像候选。画像候选必须看来源、置信度和有效期，不能当永久标签。",
    _identity_props({
        "subject": {"type": "string", "description": "画像对象；留空查询近期候选。"},
        "include_expired": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "default": 30},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_VALUE_LEDGER_SCHEMA = _schema(
    "老板只读查询 Hermes 价值账本。账本记录发现、Hermes参与、人采取行动和结果，不把收入或续费全部归功于 Hermes。",
    _identity_props({
        "subject": {"type": "string", "description": "可选账本对象。"},
        "limit": {"type": "integer", "default": 30},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_SERVICE_RELATION_FACT_CANDIDATE_SCHEMA = _schema(
    "提交学生服务关系事实候选，例如午托/晚托/全托、责任老师、统一负责人。只保存候选，不直接改正式学生主档。",
    _identity_props({
        "student_name": {"type": "string"},
        "service_type": {"type": "string", "description": "如 lunch_care、evening_care、full_care 或中文描述。"},
        "responsible_teacher_user_id": {"type": "string"},
        "unified_owner_user_id": {"type": "string"},
        "program_id": {"type": "string", "default": "regular_tuoguan"},
        "effective_from": {"type": "string"},
        "source_text": {"type": "string", "description": "用户原话，不得添加推断。"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "student_name", "operation_id"],
)

TUOGUAN_SUBMIT_INFORMATION_REQUEST_RECORD_SCHEMA = _schema(
    "保存 Hermes 主动取数记录：问谁、为什么问、问了什么、正式还是建议性、当前状态。只记录询问状态，不催发消息。",
    _identity_props({
        "target_person": {"type": "string"},
        "reason": {"type": "string"},
        "question": {"type": "string"},
        "value_level": {"type": "string", "default": "normal"},
        "request_type": {"type": "string", "enum": ["formal", "advisory"], "default": "formal"},
        "status": {"type": "string", "default": "asked"},
        "source_text": {"type": "string"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "target_person", "question", "operation_id"],
)

TUOGUAN_QUERY_INFORMATION_REQUESTS_SCHEMA = _schema(
    "只读查询 Hermes 主动取数状态：问过谁、为什么问、是否回复、是否停止、是否只是升级候选。查询结果只是状态材料，不自动催问、不自动升级、不自动计入绩效。",
    _identity_props({
        "target_person": {"type": "string", "description": "可选，按询问对象筛选。"},
        "status": {"type": "string", "description": "可选，按 asked/waiting/answered/stopped/closed/escalation_candidate 筛选。"},
        "include_closed": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "default": 50},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_INFORMATION_REQUEST_UPDATE_SCHEMA = _schema(
    "追加主动取数状态更新，例如已回复、继续等待、停止追问或仅标记升级候选。不会自动发送消息、不会自动升级、不会自动形成绩效证据。",
    _identity_props({
        "request_id": {"type": "string"},
        "status": {"type": "string", "enum": ["waiting", "answered", "stopped", "closed", "escalation_candidate"]},
        "update_text": {"type": "string", "description": "状态变化说明。"},
        "response_text": {"type": "string", "description": "对方回复原文，可为空。"},
        "needs_escalation": {"type": "boolean", "default": False, "description": "只表示可能需要升级，不自动升级。"},
        "source_text": {"type": "string"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "request_id", "status", "update_text", "operation_id"],
)

TUOGUAN_SUBMIT_PROFILE_CANDIDATE_SCHEMA = _schema(
    "提交画像候选，必须包含事实依据、置信度和可选过期时间。不得把主观标签或一次性印象写成永久事实。",
    _identity_props({
        "subject": {"type": "string"},
        "subject_type": {"type": "string", "description": "student/staff/boss/institution 等。"},
        "profile_text": {"type": "string"},
        "evidence_text": {"type": "string"},
        "confidence": {"type": "number", "default": 0.5},
        "expires_at": {"type": "string"},
        "source_text": {"type": "string"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "subject", "subject_type", "profile_text", "evidence_text", "operation_id"],
)

TUOGUAN_SUBMIT_PROFILE_CANDIDATE_CORRECTION_SCHEMA = _schema(
    "提交画像候选的纠错、撤回或过期记录。只追加纠错证据，不删除历史，不把纠错自动转成最终事实；后续模型使用画像候选时应同时参考纠错记录。",
    _identity_props({
        "candidate_id": {"type": "string", "description": "画像候选 id。"},
        "decision": {"type": "string", "enum": ["correct", "retract", "expire"], "default": "correct"},
        "correction_text": {"type": "string", "description": "纠错、撤回或过期原因。"},
        "source_text": {"type": "string", "description": "用户原话或确认来源。"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "candidate_id", "correction_text", "operation_id"],
)

TUOGUAN_SUBMIT_GOAL_EVIDENCE_SCHEMA = _schema(
    "提交目标推进证据候选。只保存真实证据，不自动闭环目标、不自动派任务、不自动联系家长。",
    _identity_props({
        "goal_id": {"type": "string"},
        "subject": {"type": "string"},
        "evidence_text": {"type": "string"},
        "source_text": {"type": "string"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "evidence_text", "operation_id"],
)

TUOGUAN_SUBMIT_PERFORMANCE_EVIDENCE_CANDIDATE_SCHEMA = _schema(
    "老板或店长提交绩效证据候选。第一阶段只做证据和老板建议材料，不自动扣分、不改工资、不通知最终绩效。",
    _identity_props({
        "staff_user_id": {"type": "string"},
        "evidence_text": {"type": "string"},
        "evidence_type": {"type": "string", "default": "execution"},
        "source_text": {"type": "string"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "staff_user_id", "evidence_text", "operation_id"],
)

TUOGUAN_QUERY_PERFORMANCE_EVIDENCE_CANDIDATES_SCHEMA = _schema(
    "只读查询绩效证据候选和老师/店长/老板回应状态。候选不等于评分，不自动扣分、不改工资、不通知最终绩效；老师只能查看自己的候选。",
    _identity_props({
        "staff_user_id": {"type": "string", "description": "老板/店长可按员工筛选；老师留空时只看自己。"},
        "include_closed": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "default": 50},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_PERFORMANCE_EVIDENCE_RESPONSE_SCHEMA = _schema(
    "追加绩效证据候选回应：老师说明/异议/确认收到，或店长/老板补充备注。只保存回应证据，不做最终评分、不改工资、不自动通知最终绩效。",
    _identity_props({
        "candidate_id": {"type": "string"},
        "response_type": {"type": "string", "enum": ["explanation", "dispute", "acknowledge", "manager_note", "boss_note"]},
        "response_text": {"type": "string"},
        "source_text": {"type": "string"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "candidate_id", "response_type", "response_text", "operation_id"],
)

TUOGUAN_SUBMIT_VALUE_LEDGER_ENTRY_SCHEMA = _schema(
    "老板确认保存 Hermes 价值账本条目。只记录发现了什么、Hermes参与了什么、人采取了什么行动、结果如何和保守归因；不把收入、续费或转化全部归功于 Hermes。",
    _identity_props({
        "discovered": {"type": "string", "description": "Hermes 或团队发现的真实问题、机会或缺口。"},
        "hermes_action": {"type": "string", "description": "Hermes 实际参与推动的动作。"},
        "human_action": {"type": "string", "description": "人实际采取的行动，可为空。"},
        "outcome": {"type": "string", "description": "已观察到的结果，可为空；不能编造收入或续费结果。"},
        "subject": {"type": "string", "description": "关联对象，如学生、老师、目标或经营主题。"},
        "attribution": {"type": "string", "enum": ["participated", "assisted", "observed", "unknown"], "default": "participated"},
        "source_text": {"type": "string", "description": "老板确认或原始来源文本。"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "discovered", "hermes_action", "operation_id"],
)

TUOGUAN_QUERY_GRAY_OBSERVATIONS_SCHEMA = _schema(
    "只读查询真实渠道灰度观察记录。记录只用于人工验收和后续优化，不限制模型下一步，不自动进入绩效、工资或长期记忆。",
    _identity_props({
        "scenario_id": {"type": "string", "description": "可选，按验收场景筛选。"},
        "outcome": {"type": "string", "enum": ["success", "issue", "unclear", "note", ""], "default": ""},
        "limit": {"type": "integer", "default": 50},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_GRAY_OBSERVATION_SCHEMA = _schema(
    "提交真实渠道灰度观察。只记录试用体验、问题或成功样本；不限制模型能力，不改 Router，不自动进入长期记忆，不生成绩效证据，不改工资，不发通知。",
    _identity_props({
        "scenario_id": {"type": "string", "description": "验收场景，如 boss_goal、teacher_record、manager_gap_query。"},
        "observation_text": {"type": "string", "description": "真实观察内容，不要编造。"},
        "outcome": {"type": "string", "enum": ["success", "issue", "unclear", "note"], "default": "note"},
        "conversation_ref": {"type": "string", "description": "可选，对话或消息引用。"},
        "actor_user_id": {"type": "string", "description": "可选，实际体验者账号；默认当前用户。"},
        "source_text": {"type": "string", "description": "用户原话或观察来源。"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "scenario_id", "observation_text", "operation_id"],
)

TUOGUAN_QUERY_GRAY_ROLLOUT_DECISIONS_SCHEMA = _schema(
    "只读查询老板确认过的灰度放量决策记录。记录只是业务背景材料，不会自动扩大灰度、不改权限、不改手册、不派任务。",
    _identity_props({
        "decision_type": {"type": "string", "description": "可选，按决策类型筛选。"},
        "limit": {"type": "integer", "default": 30},
    }),
    ["user_id"],
)

TUOGUAN_GENERATE_GRAY_REVIEW_SCHEMA = _schema(
    "生成灰度复盘只读摘要，汇总验收准备、灰度观察、老板决策和巡店风险。它只是材料召回，不限制模型能力，不自动放量、不改权限、不改手册、不派任务、不发通知。",
    _identity_props({
        "write_report": {"type": "boolean", "default": False, "description": "是否同时在 reports/ 下保存 md/json 报告；不写业务数据。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_GRAY_SCENARIO_CARDS_SCHEMA = _schema(
    "只读查询真实灰度测试场景参考卡。场景卡只是帮助 Hermes 和老板/店长/老师理解可以怎么试，不是 Router，不限制模型能力，不要求固定流程，不自动执行。",
    _identity_props({
        "role": {"type": "string", "enum": ["boss", "manager", "teacher", ""], "default": "", "description": "可选，按角色筛选。"},
        "scenario_id": {"type": "string", "description": "可选，按场景 id 筛选。"},
    }),
    ["user_id"],
)

TUOGUAN_GENERATE_GRAY_TRIAL_START_PACK_SCHEMA = _schema(
    "生成真实灰度试用启动包，包含参与角色、参考场景、观察模板和边界确认。启动包只是试用材料，不是 Router，不限制模型能力，不要求固定流程，不自动执行、不写业务数据。",
    _identity_props({
        "role": {"type": "string", "enum": ["boss", "manager", "teacher", ""], "default": "", "description": "可选，按角色筛选场景卡。"},
        "include_examples": {"type": "boolean", "default": True, "description": "是否包含示例用户消息。"},
    }),
    ["user_id"],
)

TUOGUAN_GENERATE_GRAY_OBSERVATION_CANDIDATES_SCHEMA = _schema(
    "基于灰度观察只读生成优化候选清单，帮助人工判断是否需要优化手册、工具、话术或权限边界。候选不自动进入手册，不创建学习候选，不修工具，不限制模型能力。",
    _identity_props({
        "outcome": {"type": "string", "enum": ["success", "issue", "unclear", "note", ""], "default": "issue", "description": "默认只复盘问题观察。"},
        "limit": {"type": "integer", "default": 50},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_GRAY_ROLLOUT_DECISION_SCHEMA = _schema(
    "老板确认保存灰度放量决策，例如继续小范围、暂停、候选扩大、暂缓某项或回滚候选。保存只是记录老板决定，不自动执行放量、不改权限、不改手册、不派任务、不发通知。",
    _identity_props({
        "decision_type": {
            "type": "string",
            "enum": ["continue_small_gray", "pause", "expand_candidate", "defer_item", "rollback_candidate", "note"],
        },
        "decision_text": {"type": "string", "description": "老板明确拍板内容。"},
        "scope": {"type": "string", "description": "适用范围，如老板/店长/李老师、某场景或某校区。"},
        "reason": {"type": "string", "description": "决策原因，可为空。"},
        "source_report_path": {"type": "string", "description": "关联复盘报告路径，可为空。"},
        "source_text": {"type": "string", "description": "老板原话或确认来源。"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "decision_type", "decision_text", "operation_id"],
)

TUOGUAN_QUERY_GRAY_OPTIMIZATION_DECISIONS_SCHEMA = _schema(
    "只读查询老板确认过的灰度优化决策记录。记录只是人工优化材料，不自动改手册、不创建学习候选、不修工具、不改权限、不派任务、不限制模型能力。",
    _identity_props({
        "candidate_id": {"type": "string", "description": "可选，按观察复盘候选 ID 筛选。"},
        "decision_type": {"type": "string", "description": "可选，按确认类型筛选。"},
        "limit": {"type": "integer", "default": 50},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_GRAY_OPTIMIZATION_DECISION_SCHEMA = _schema(
    "老板确认保存灰度优化候选的处理方向，例如进入手册候选、学习候选、工具修复候选、权限边界复核、暂缓或驳回。保存只是记录老板选择，不自动改手册、不创建学习候选、不修工具、不改权限、不派任务、不发通知、不限制模型能力。",
    _identity_props({
        "candidate_id": {"type": "string", "description": "观察复盘候选 ID。"},
        "decision_type": {
            "type": "string",
            "enum": [
                "handbook_candidate",
                "learning_candidate",
                "tool_fix_candidate",
                "permission_boundary_review",
                "response_style_candidate",
                "defer",
                "reject",
                "note",
            ],
        },
        "decision_text": {"type": "string", "description": "老板明确确认的处理方向。"},
        "source_observation_id": {"type": "string", "description": "可选，关联灰度观察 ID。"},
        "candidate_type": {"type": "string", "description": "可选，原候选类型。"},
        "reason": {"type": "string", "description": "确认原因或补充说明，可为空。"},
        "source_text": {"type": "string", "description": "老板原话或确认来源。"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "candidate_id", "decision_type", "decision_text", "operation_id"],
)

TUOGUAN_SUBMIT_LEARNING_CANDIDATE_SCHEMA = _schema(
    "提交待审核学习候选。老板批准前不会改变正式规则，适用于新说法、误判样本、闭环表达、话术优化。",
    _identity_props(
        {
            "title": {"type": "string", "description": "候选标题。"},
            "content": {"type": "string", "description": "候选内容或规则建议。"},
            "category": {"type": "string", "description": "general/safety/parent/renewal/closure/script/payroll 等。"},
            "candidate_type": {"type": "string", "description": "knowledge/rule_suggestion/closure_phrase/false_positive/script。"},
            "proposed_triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "建议触发表达，可为空。",
            },
            "source": {"type": "string", "description": "候选来源，可为空。"},
        }
    ),
    ["user_id", "title", "content"],
)





TUOGUAN_SUBMIT_DUE_WAKEUP_CANDIDATE_SCHEMA = _schema(
    "把一个已读取到的到期唤醒候选保存为内部 wakeup_request。只保存提醒状态，不发送消息、不派任务、不重试动作、不关闭事项、不规定模型下一步。",
    _identity_props({
        "candidate_id": {"type": "string", "description": "来自到期唤醒候选的 candidate_id。"},
        "now_at": {"type": "string", "description": "可选：候选生成时使用的当前时间。"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "operation_id": {"type": "string", "description": "本次写入操作 id。"},
    }),
    ["user_id", "candidate_id", "operation_id"],
)



TUOGUAN_GENERATE_AUTONOMOUS_ACCEPTANCE_PACK_SCHEMA = _schema(
    "生成 Hermes 自主工作真实渠道验收包，包含老板/老师测试话术、观察点、禁止自动发生事项和剩余阶段说明。只读参考，不限制模型、不路由、不写业务数据。",
    _identity_props({
        "include_teacher": {"type": "boolean", "description": "是否包含李老师账号测试步骤。"},
    }),
    ["user_id"],
)


TUOGUAN_GENERATE_AUTONOMOUS_LOG_REVIEW_SCHEMA = _schema(
    "只读复盘 Hermes 自主工作真实聊天账本和灰度观察，生成优化候选分类。候选只是人工/Hermes 参考，不限制模型、不规定下一步、不改手册、不写记忆、不修工具、不改权限、不发通知、不派任务。",
    _identity_props({
        "limit": {"type": "integer", "default": 80, "description": "最多扫描最近多少条对话账本，服务端会限制上限。"},
        "include_gray_observations": {"type": "boolean", "default": True, "description": "是否同时纳入灰度观察中的 issue/unclear 记录。"},
    }),
    ["user_id"],
)


TUOGUAN_GENERATE_DUE_WAKEUP_CANDIDATES_SCHEMA = _schema(
    "生成 Hermes 到期唤醒候选，只读查看哪些等待事项到关注时间、哪些结果未知动作需要先核验。候选不规定模型下一步，不创建唤醒请求、不发送消息、不派任务、不改业务数据。",
    _identity_props({
        "now_at": {"type": "string", "description": "可选：用于判断到期的当前时间；默认服务器当前时间。"},
        "limit": {"type": "integer", "description": "最多汇总条数。"},
    }),
    ["user_id"],
)


TUOGUAN_GENERATE_AUTONOMOUS_RECOVERY_REPORT_SCHEMA = _schema(
    "生成 Hermes 自主工作恢复报告，只读汇总工作事项、等待、唤醒、结果未知动作和恢复前应判断的问题。报告不规定模型下一步动作，不发送消息、不派任务、不改业务数据。",
    _identity_props({
        "focus_key": {"type": "string", "description": "可选：只看某个现实焦点。"},
        "limit": {"type": "integer", "description": "最多汇总条数。"},
    }),
    ["user_id"],
)


TUOGUAN_QUERY_HERMES_WORK_ITEMS_SCHEMA = _schema(
    "查询 Hermes 自主工作事项、等待状态、下一关注时间和已保存事实依据。返回的是状态材料，不指定模型下一步动作。",
    _identity_props({
        "status": {"type": "string", "description": "可选：active、waiting、blocked、closed、superseded。"},
        "focus_key": {"type": "string", "description": "现实焦点键，可为空。"},
        "include_closed": {"type": "boolean", "description": "是否包含已关闭/替代事项。"},
        "limit": {"type": "integer", "description": "最多返回条数。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_INSTITUTION_WORK_SCHEMA = _schema(
    "查询机构制度、流程和内部改进工作事项。草案、老板内容确认、落实授权、执行关联和核验都以同一工作事项为准；老师和店长不能看到未生效草案或老板决定。",
    _identity_props({
        "focus_key": {"type": "string", "description": "可选稳定焦点，例如 institution:safety_management_policy。"},
        "institution_stage": {"type": "string", "description": "可选阶段筛选。"},
        "include_closed": {"type": "boolean", "default": False, "description": "是否包含已关闭或停止事项。"},
        "limit": {"type": "integer", "default": 20, "description": "最多返回条数。"},
    }),
    ["user_id"],
)

TUOGUAN_ADVANCE_INSTITUTION_WORK_SCHEMA = _schema(
    "推进一个已存在的机构制度、流程或内部改进工作事项。模型必须显式选择 action；系统只保存证据、版本、老板决定、执行关联和核验，不根据自然语言偷偷派任务或外发。内容确认与落实授权必须是两次不同的老板决定；没有回执不得说已保存、已确认、已通知或已落实。",
    _identity_props({
        "action": {"type": "string", "enum": ["discover", "record_evidence", "save_draft", "submit_for_review", "review_content", "authorize_implementation", "link_execution", "verify", "defer", "close"], "description": "本轮唯一的机构工作动作。"},
        "operation_id": {"type": "string", "description": "本次写入幂等操作 id。"},
        "focus_key": {"type": "string", "description": "发现或查询时使用的稳定焦点键。"},
        "work_item_id": {"type": "string", "description": "已有机构工作事项 id；发现动作可留空。"},
        "title": {"type": "string", "description": "发现机构缺口时的标题。"},
        "summary": {"type": "string", "description": "发现、暂缓、关闭或验证的事实摘要。"},
        "evidence": {"type": "array", "items": {"type": "object"}, "description": "证据列表。每条须含 source_kind=internal_confirmed/internal_record/external_primary/model_judgment/pending_hypothesis/unsupported 和 summary；external_primary 还须 source_url、publisher、published_at。"},
        "artifact_title": {"type": "string", "description": "草案版本标题。"},
        "artifact_content": {"type": "string", "description": "草案完整内容。待专业核验的结论须同时列入 pending_items，不能写成生效事实。"},
        "submit_for_review": {"type": "boolean", "default": False, "description": "仅 save_draft 使用。true 时在同一原子写入内保存草案并提交内容审核，仍不代表内容确认或落实授权。"},
        "artifact_version_id": {"type": "string", "description": "要审核、授权、关联或核验的成果版本；默认当前版本。"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}, "description": "草案引用的已保存证据 id。"},
        "pending_items": {"type": "array", "items": {}, "description": "尚待核验的法律、标准、责任或业务假设。"},
        "decision": {"type": "string", "enum": ["approved", "changes_requested", "rejected"], "description": "review_content 时老板的内容决定。"},
        "implementation_scope": {"type": "object", "description": "老板单独授权落实时的范围、对象和边界；授权本身不自动发消息或派任务。"},
        "execution_link": {"type": "object", "description": "真实任务、周期安排或发送回执关联；仅同版本落实授权后可保存。"},
        "verification": {"type": "object", "description": "落实后的真实检查结果和证据；effective=true 只表示本次核验有效。"},
        "source_text": {"type": "string", "description": "来源原话或简洁摘要。"},
        "source_message_id": {"type": "string", "description": "老板决定来源消息 id；内容确认和落实授权不能引用同一条消息。"},
    }),
    ["user_id", "action", "operation_id"],
)

TUOGUAN_SUBMIT_HERMES_WORK_ITEM_SCHEMA = _schema(
    "保存 Hermes 低风险内部工作事项。只记录焦点、事实、等待和下一关注时间，不发送消息、不派任务、不改变业务结论。",
    _identity_props({
        "focus_key": {"type": "string", "description": "同一现实焦点的稳定键，用于合并重复事项。"},
        "title": {"type": "string", "description": "事项标题。"},
        "focus_summary": {"type": "string", "description": "当前对这个现实焦点的简短说明。"},
        "related_objects": {"type": "array", "items": {}, "description": "相关学生、目标、任务或业务对象。"},
        "related_staff_user_ids": {"type": "array", "items": {"type": "string"}, "description": "相关老师/店长用户 id。"},
        "execution_plan": {"type": "array", "items": {}, "description": "老板确认后可保存的阶段方案、里程碑或推进批次；只是恢复材料，不是固定流程。"},
        "current_phase": {"type": "object", "description": "当前推进阶段，如阶段名、目标、判断依据、是否等待确认；只是状态材料。"},
        "next_actions": {"type": "array", "items": {}, "description": "模型认为可考虑的下一步行动候选；不是系统指定动作。"},
        "progress_evidence": {"type": "array", "items": {}, "description": "已取得的真实推进证据或回执。"},
        "confirmed_facts": {"type": "array", "items": {}, "description": "已经有证据支持的事实。"},
        "pending_judgements": {"type": "array", "items": {}, "description": "待核实判断或假设。"},
        "completed_actions": {"type": "array", "items": {}, "description": "已经完成且有证据的动作。"},
        "current_waiting": {"type": "object", "description": "当前等待对象、原因、价值等级和下一关注时间等。"},
        "blocked_by": {"type": "array", "items": {}, "description": "当前卡点材料，如缺事实、缺授权、缺回复；不是系统拦截规则。"},
        "ask_candidates": {"type": "array", "items": {}, "description": "模型认为可询问的人和问题候选；是否询问仍由模型结合权限和时机判断。"},
        "last_human_contact_at": {"type": "string", "description": "上次联系或提醒人的时间。"},
        "next_contact_after": {"type": "string", "description": "不早于这个时间再考虑联系或提醒。"},
        "owner_escalation_reason": {"type": "string", "description": "需要提醒老板本人的原因；只是材料，不代表已发送。"},
        "value_progress_note": {"type": "string", "description": "本事项对续费、服务、风控或收入价值的推进说明。"},
        "next_attention_at": {"type": "string", "description": "建议下一次关注时间，ISO 或自然日期文本。"},
        "status": {"type": "string", "enum": ["active", "waiting", "blocked", "closed", "superseded"], "description": "事项状态。"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "operation_id": {"type": "string", "description": "本次写入操作 id。"},
    }),
    ["user_id", "focus_key", "title", "focus_summary", "operation_id"],
)

TUOGUAN_UPDATE_HERMES_WORK_ITEM_SCHEMA = _schema(
    "更新 Hermes 内部工作事项的状态、等待、事实或关闭原因。只保存证据和状态，不执行外部动作。",
    _identity_props({
        "work_item_id": {"type": "string", "description": "工作事项 id；可用 focus_key 替代。"},
        "focus_key": {"type": "string", "description": "现实焦点键。"},
        "status": {"type": "string", "enum": ["active", "waiting", "blocked", "closed", "superseded"], "description": "新状态。"},
        "focus_summary": {"type": "string", "description": "更新后的焦点说明。"},
        "execution_plan": {"type": "array", "items": {}, "description": "更新后的阶段方案、里程碑或推进批次；只是恢复材料，不是固定流程。"},
        "current_phase": {"type": "object", "description": "更新后的当前推进阶段。"},
        "next_actions": {"type": "array", "items": {}, "description": "更新后的下一步行动候选；不是系统指定动作。"},
        "progress_evidence": {"type": "array", "items": {}, "description": "更新后的真实推进证据或回执。"},
        "confirmed_facts": {"type": "array", "items": {}, "description": "更新后的确认事实。"},
        "pending_judgements": {"type": "array", "items": {}, "description": "更新后的待核实判断。"},
        "completed_actions": {"type": "array", "items": {}, "description": "更新后的已完成动作。"},
        "current_waiting": {"type": "object", "description": "当前等待状态。"},
        "blocked_by": {"type": "array", "items": {}, "description": "更新后的卡点材料；不是系统拦截规则。"},
        "ask_candidates": {"type": "array", "items": {}, "description": "更新后的询问候选；不是固定流程。"},
        "last_human_contact_at": {"type": "string", "description": "上次联系或提醒人的时间。"},
        "next_contact_after": {"type": "string", "description": "不早于这个时间再考虑联系或提醒。"},
        "owner_escalation_reason": {"type": "string", "description": "需要提醒老板本人的原因。"},
        "value_progress_note": {"type": "string", "description": "本事项的真实价值推进说明。"},
        "next_attention_at": {"type": "string", "description": "下一关注时间。"},
        "stop_reason": {"type": "string", "description": "关闭、替代或停止原因。"},
        "update_text": {"type": "string", "description": "本次更新说明。"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "operation_id": {"type": "string", "description": "本次写入操作 id。"},
    }),
    ["user_id", "operation_id"],
)

TUOGUAN_QUERY_WORK_COMMITMENTS_SCHEMA = _schema(
    "查询小优已落账的工作承诺。承诺代表后续工作对象，不等于任务、制度、外发或成果已经完成。",
    _identity_props({
        "include_closed": {"type": "boolean", "default": False, "description": "是否包含已完成、取消或过期承诺。"},
        "limit": {"type": "integer", "default": 20, "description": "最多返回条数。"},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_WORK_COMMITMENT_SCHEMA = _schema(
    "当小优要承诺后续整理、核实、推进或处理时，先把承诺落为可恢复工作对象。只记录承诺和证据要求，不创建任务、不外发、不改变制度或权限。",
    _identity_props({
        "focus_key": {"type": "string", "description": "现实焦点；系统会归入 commitment: 命名空间。"},
        "title": {"type": "string", "description": "承诺事项标题。"},
        "commitment_statement": {"type": "string", "description": "本轮对外承诺的原句或紧凑摘要。"},
        "deliverable": {"type": "string", "description": "可验收的工作成果。"},
        "next_action": {"type": "string", "description": "下一次要做的可验证行动。"},
        "source_message_id": {"type": "string", "description": "承诺来源消息 id。"},
        "planned_at": {"type": "string", "description": "计划关注时间；没有真实唤醒或执行回执时不得承诺自动执行。"},
        "evidence_requirement": {"type": "string", "description": "完成前需要什么证据。"},
        "related_authority_refs": {"type": "array", "items": {}, "description": "已有目标、制度、任务或授权的引用；存在权威对象时只关联，不重复建业务事实。"},
        "related_objects": {"type": "array", "items": {}, "description": "相关业务对象引用。"},
        "related_staff_user_ids": {"type": "array", "items": {"type": "string"}, "description": "相关人员 id。"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"], "default": "low", "description": "承诺风险等级。"},
        "source_text": {"type": "string", "description": "来源原话或简述。"},
        "operation_id": {"type": "string", "description": "幂等操作 id。"},
    }),
    ["user_id", "focus_key", "title", "commitment_statement", "deliverable", "next_action", "operation_id"],
)

TUOGUAN_UPDATE_WORK_COMMITMENT_SCHEMA = _schema(
    "推进已存在的工作承诺。只有真实证据或执行回执可以进入完成；这不替代正式任务、制度或外发回执。",
    _identity_props({
        "work_item_id": {"type": "string", "description": "承诺工作事项 id；可用 focus_key 替代。"},
        "focus_key": {"type": "string", "description": "承诺焦点键。"},
        "commitment_stage": {"type": "string", "enum": ["captured", "planned", "active", "waiting", "verifying", "completed", "cancelled", "expired", "superseded", "failed", "escalated"], "description": "承诺当前阶段。"},
        "progress_evidence": {"type": "array", "items": {}, "description": "已取得的真实证据。"},
        "next_action": {"type": "string", "description": "下一行动。"},
        "planned_at": {"type": "string", "description": "下一关注时间。"},
        "evidence_requirement": {"type": "string", "description": "仍需证据。"},
        "stop_reason": {"type": "string", "description": "取消、过期、替代或失败原因。"},
        "source_text": {"type": "string", "description": "来源原话或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "operation_id": {"type": "string", "description": "幂等操作 id。"},
    }),
    ["user_id", "commitment_stage", "operation_id"],
)

TUOGUAN_QUERY_WAKEUP_REQUESTS_SCHEMA = _schema(
    "查询唤醒请求。唤醒请求只说明 Hermes 为什么重新查看一件事，不预设执行路线。",
    _identity_props({
        "status": {"type": "string", "description": "可选状态：pending、handled、ignored、superseded。"},
        "wakeup_source": {"type": "string", "description": "唤醒来源，如 user_message、time_due、business_event、system_recovery、self_attention。"},
        "limit": {"type": "integer", "description": "最多返回条数。"},
    }),
    ["user_id"],
)


TUOGUAN_UPDATE_WAKEUP_REQUEST_SCHEMA = _schema(
    "更新内部 wakeup_request 的处理状态。只追加状态记录，不发送消息、不派任务、不关闭工作事项、不重试动作、不规定模型下一步。",
    _identity_props({
        "wakeup_request_id": {"type": "string", "description": "要更新的唤醒请求 id。"},
        "status": {"type": "string", "enum": ["pending", "handled", "ignored", "superseded"], "description": "新的处理状态。"},
        "update_text": {"type": "string", "description": "本次处理说明。"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "operation_id": {"type": "string", "description": "本次写入操作 id。"},
    }),
    ["user_id", "wakeup_request_id", "status", "update_text", "operation_id"],
)


TUOGUAN_SUBMIT_WAKEUP_REQUEST_SCHEMA = _schema(
    "保存唤醒请求。只记录来源、原因和相关对象，不直接执行业务动作。",
    _identity_props({
        "wakeup_source": {"type": "string", "description": "唤醒来源。"},
        "reason": {"type": "string", "description": "唤醒原因。"},
        "related_work_item_id": {"type": "string", "description": "相关工作事项 id。"},
        "related_objects": {"type": "array", "items": {}, "description": "相关对象。"},
        "scheduled_for": {"type": "string", "description": "计划关注时间。"},
        "status": {"type": "string", "enum": ["pending", "handled", "ignored", "superseded"], "description": "状态。"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "operation_id": {"type": "string", "description": "本次写入操作 id。"},
    }),
    ["user_id", "wakeup_source", "reason", "operation_id"],
)

TUOGUAN_QUERY_BUSINESS_EVENTS_SCHEMA = _schema(
    "查询业务事件账本。事件只表示现实发生了什么，不要求 Hermes 采取固定动作。",
    _identity_props({
        "event_type": {"type": "string", "description": "事件类型筛选。"},
        "related_object": {"type": "string", "description": "相关对象文本筛选。"},
        "limit": {"type": "integer", "description": "最多返回条数。"},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_BUSINESS_EVENT_SCHEMA = _schema(
    "保存业务事件。只记录现实变化和证据，不自动触发通知、派任务或结论。",
    _identity_props({
        "event_type": {"type": "string", "description": "事件类型，如 teacher_reply、record_created、goal_changed、tool_result_unknown。"},
        "event_text": {"type": "string", "description": "事件内容。"},
        "related_objects": {"type": "array", "items": {}, "description": "相关对象。"},
        "occurred_at": {"type": "string", "description": "发生时间。"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "operation_id": {"type": "string", "description": "本次写入操作 id。"},
    }),
    ["user_id", "event_type", "event_text", "operation_id"],
)

TUOGUAN_QUERY_ACTION_EXECUTIONS_SCHEMA = _schema(
    "查询动作执行账本，包括成功、失败、部分成功和结果未知。结果未知时需要后续核验事实。",
    _identity_props({
        "status": {"type": "string", "description": "状态筛选。"},
        "action_type": {"type": "string", "description": "动作类型筛选。"},
        "limit": {"type": "integer", "description": "最多返回条数。"},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_ACTION_EXECUTION_SCHEMA = _schema(
    "保存动作执行账本。只记录动作证据、幂等键、回执和结果状态，不自动重试或扩大权限。",
    _identity_props({
        "action_type": {"type": "string", "description": "动作类型，如 tool_call、write_state、notify_draft、permission_block。"},
        "action_summary": {"type": "string", "description": "动作摘要。"},
        "status": {"type": "string", "enum": ["not_started", "success", "failed", "partial_success", "result_unknown", "permission_blocked", "manual_takeover"], "description": "执行状态。"},
        "related_work_item_id": {"type": "string", "description": "相关工作事项 id。"},
        "idempotency_key": {"type": "string", "description": "幂等键。"},
        "receipt": {"type": "object", "description": "工具回执或证据。"},
        "result_text": {"type": "string", "description": "结果说明。"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "operation_id": {"type": "string", "description": "本次写入操作 id。"},
    }),
    ["user_id", "action_type", "action_summary", "status", "operation_id"],
)

TUOGUAN_QUERY_AUTONOMOUS_WORK_BRIEF_SCHEMA = _schema(
    "查询 Hermes 自主工作简报：活跃事项、等待、唤醒、近期事件和结果未知动作。它是事实材料，不是流程指令。",
    _identity_props({"limit": {"type": "integer", "description": "最多汇总条数。"}}),
    ["user_id"],
)

TUOGUAN_QUERY_PROACTIVE_WORK_RADAR_SCHEMA = _schema(
    "只读查询小优主动工作雷达：按员工手册汇总机构地图、组织权限、学生服务关系、运营制度、老师工作习惯、目标工作项、服务证据、风险和复盘缺口。它是材料，不是 Router，不规定模型下一步。",
    _identity_props({"limit": {"type": "integer", "description": "最多返回优先缺口和问题候选数量。"}}),
    ["user_id"],
)

TUOGUAN_QUERY_ACTIVE_WORK_CONTEXT_SCHEMA = _schema(
    "只读查询当前人的活动工作线程，包括当前任务、最近主动提醒、关系触达和有权限查看的市场观察。结果只提供衔接证据，不判断用户意图、不规定下一工具。",
    _identity_props({"limit": {"type": "integer", "default": 5}}),
    ["user_id"],
)

TUOGUAN_QUERY_ATTENTION_THREADS_SCHEMA = _schema(
    "只读查询老板主动提醒线程。老板回复“什么意思/刚才那个/这个不用了/已处理”时，模型应先查最近提醒线程再判断是否更新状态；查询结果只是上下文材料，不替模型判断老板回复是否相关。",
    _identity_props({
        "status": {"type": "string", "description": "可选，按 candidate/queued/sent/replied/resolved/failed/superseded 筛选。"},
        "focus_key": {"type": "string", "description": "可选，按提醒焦点筛选。"},
        "include_closed": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "default": 30},
    }),
    ["user_id"],
)

TUOGUAN_UPDATE_ATTENTION_THREAD_SCHEMA = _schema(
    "更新老板主动提醒线程状态，例如老板已回复、提醒已解决、旧提醒被替代或失败。写操作必须有 operation_id；只能保存模型判断和真实消息证据，不得把无关回复强行绑定。",
    _identity_props({
        "attention_id": {"type": "string", "description": "提醒线程 id。"},
        "status": {"type": "string", "enum": ["candidate", "queued", "sent", "replied", "resolved", "failed", "superseded"]},
        "owner_message_id": {"type": "string", "description": "老板回复消息 id，可空。"},
        "owner_message_text": {"type": "string", "description": "老板回复原文或短摘要。"},
        "reply_relevance": {"type": "string", "description": "模型判断本回复是否关联该提醒，如 related/unrelated/unclear。"},
        "reply_sufficiency": {"type": "string", "description": "模型判断回复是否足够关闭，如 sufficient/partial/insufficient。"},
        "model_judgment": {"type": "string", "description": "模型判断依据。"},
        "resolution_note": {"type": "string", "description": "解决或替代原因。"},
        "failure_reason": {"type": "string", "description": "失败原因。"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "attention_id", "status", "operation_id"],
)

TUOGUAN_QUERY_PROACTIVE_AUTHORIZATIONS_SCHEMA = _schema(
    "只读查询小优当前主动工作授权、灰度对象、行动类型、频率和有效期。它返回真实权限证据，不代表消息已经发送。",
    _identity_props({
        "include_inactive": {"type": "boolean", "default": False},
        "now_at": {"type": "string", "description": "可选当前时间 ISO。"},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_PROACTIVE_AUTHORIZATION_SCHEMA = _schema(
    "老板创建、调整或撤销小优主动工作授权。低风险主动找人和目标子任务必须保存为结构化授权并写后反查；本工具不能授权联系家长、改工资、改权限或删除数据。",
    _identity_props({
        "subject_role": {"type": "string", "enum": ["boss", "manager", "teacher"]},
        "subject_user_ids": {"type": "array", "items": {"type": "string"}},
        "action_types": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["active", "revoked", "superseded"], "default": "active"},
        "authorization_id": {"type": "string", "description": "撤销或调整已有授权时填写。"},
        "goal_ids": {"type": "array", "items": {"type": "string"}},
        "daily_limit": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
        "effective_at": {"type": "string"},
        "expires_at": {"type": "string"},
        "rollout_stage": {"type": "string", "description": "如 pilot、manager、all_active_staff。"},
        "source_text": {"type": "string", "description": "老板授权原话。"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "subject_role", "action_types", "operation_id"],
)

TUOGUAN_EXECUTE_RELATIONSHIP_TOUCH_SCHEMA = _schema(
    "执行一个已经形成的主动联系候选。工具会重新核验授权、在职状态、灰度、时间窗、频率、幂等和家长边界；只有返回 queued 才能说已安排发送，只有后续 sent 回执才能说已发送。",
    _identity_props({
        "candidate_id": {"type": "string"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "candidate_id", "operation_id"],
)

TUOGUAN_UPDATE_RELATIONSHIP_TOUCH_SCHEMA = _schema(
    "把老师/店长/老板的真实回复或发送异常写回主动联系线程。部分事实用 replied_partial，事实完整用 replied_sufficient，核验完成后才 resolved。",
    _identity_props({
        "candidate_id": {"type": "string"},
        "status": {"type": "string", "enum": ["replied_partial", "replied_sufficient", "resolved", "retry_pending", "failed", "result_unknown", "expired", "superseded", "escalated"]},
        "reply_text": {"type": "string", "description": "对方真实回复。"},
        "evidence_complete": {"type": "boolean"},
        "failure_reason": {"type": "string"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "candidate_id", "status", "operation_id"],
)

TUOGUAN_QUERY_GOAL_ACTIONS_SCHEMA = _schema(
    "只读查询已确认目标的持久行动、到期时间、事实归属人、等待回复和证据缺口。行动是模型恢复工作的材料，不是固定 Router。",
    _identity_props({
        "goal_id": {"type": "string"},
        "include_closed": {"type": "boolean", "default": False},
        "due_only": {"type": "boolean", "default": False},
        "now_at": {"type": "string"},
        "limit": {"type": "integer", "default": 30},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_GOAL_ACTION_SCHEMA = _schema(
    "模型为已确认目标保存下一项低风险行动或更新其状态。只保存计划和证据要求；真实外发、派任务仍分别经过主动执行和任务边界。",
    _identity_props({
        "goal_id": {"type": "string"},
        "action_type": {"type": "string", "enum": ["query_internal_data", "fill_institution_fact", "ask_staff_fact", "prepare_material", "create_low_risk_task", "follow_up", "verify_evidence", "review_and_report"]},
        "summary": {"type": "string"},
        "target_role": {"type": "string", "enum": ["", "boss", "manager", "teacher"]},
        "target_user_id": {"type": "string"},
        "target_name": {"type": "string"},
        "student_names": {"type": "array", "items": {"type": "string"}},
        "planned_at": {"type": "string"},
        "due_at": {"type": "string"},
        "evidence_requirement": {"type": "string"},
        "escalation_path": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["planned", "due", "executing", "waiting_reply", "replied_partial", "replied_sufficient", "verified", "resolved", "blocked", "failed", "cancelled", "superseded", "escalated"], "default": "planned"},
        "goal_action_id": {"type": "string"},
        "source_text": {"type": "string"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "goal_id", "action_type", "summary", "operation_id"],
)

TUOGUAN_QUERY_RELATIONSHIP_TOUCH_CANDIDATES_SCHEMA = _schema(
    "只读查询小优主动找老板/店长/老师的关系触达候选和当前策略。老板问“现在能不能主动找李老师/准备问谁/为什么没问”时应先用本工具核对候选、白名单和策略状态，不能凭旧认知回答。",
    _identity_props({
        "target_user_id": {"type": "string", "description": "可选，按目标企业微信 user_id 筛选，如 CeShi。"},
        "target_role": {"type": "string", "enum": ["", "boss", "manager", "teacher"], "default": ""},
        "include_closed": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "default": 30},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_RELATIONSHIP_TOUCH_CANDIDATE_SCHEMA = _schema(
    "保存一个主动找老板/店长/老师的具体工作候选。测试期只允许金总和李老师测试号 CeShi 进入直接外发候选；其他老师/店长最多保存内部候选，家长禁止。消息必须是工作相关的一个具体问题或支持，不得绕过任务边界新增家长外发，不得批量骚扰。正式任务中追问缺失证据时使用 action_type=ask_task_fact 并填写 related_task_id；它与普通关系触达分开校验。",
    _identity_props({
        "target_role": {"type": "string", "enum": ["boss", "manager", "teacher"], "description": "目标角色。"},
        "target_user_id": {"type": "string", "description": "目标企业微信 user_id；李老师测试号为 CeShi。"},
        "target_name": {"type": "string", "description": "目标姓名，可空。"},
        "touch_type": {"type": "string", "description": "触达类型，如 owner_business、owner_progress、care、record_relief、material_support。"},
        "message": {"type": "string", "description": "准备问对方的一句话，必须具体、温和、工作相关。"},
        "reason": {"type": "string", "description": "为什么需要问这个人，说明事实缺口或任务上下文。"},
        "value": {"type": "string", "description": "这次询问对机构或任务的价值，可空。"},
        "work_related": {"type": "boolean", "default": True},
        "private_emotional_support": {"type": "boolean", "default": False},
        "suggested_send_at": {"type": "string", "description": "建议发送时间，可空；最终仍受频率和时间窗限制。"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "action_type": {"type": "string", "description": "本次主动工作的类型，如 ask_work_fact、ask_task_result、ask_student_service_fact。"},
        "goal_id": {"type": "string", "description": "可选，关联已确认经营目标。"},
        "goal_action_id": {"type": "string", "description": "可选，关联持久目标行动。"},
        "related_task_id": {"type": "string", "description": "可选；正式任务内追问结果或事实时填写当前开放任务 id。"},
        "evidence_requirement": {"type": "string", "description": "对方回复需要补齐的证据。"},
        "execute_if_authorized": {"type": "boolean", "default": False, "description": "如果模型本轮已经判断要现在问对方，必须显式传 true，工具会在同一受控操作中进入 outbox；只想保留候选时为 false。"},
        "operation_id": {"type": "string"},
    }),
    ["user_id", "target_role", "touch_type", "message", "reason", "operation_id"],
)

TUOGUAN_QUERY_EMPLOYEE_WORK_MAP_SCHEMA = _schema(
    "只读查询小优机构工作地图：汇总已知机构事实、未知缺口、事实归属人、开放工作项和下一步材料。它是员工入职认知地图，不发送消息、不创建任务、不写业务事实、不规定模型下一步。",
    _identity_props({"limit": {"type": "integer", "description": "最多返回地图域、缺口和问题候选数量。"}}),
    ["user_id"],
)

TUOGUAN_QUERY_FACT_GAP_CANDIDATES_SCHEMA = _schema(
    "只读查询小优事实缺口候选：缺什么事实、影响什么、建议问老板/店长/老师谁、优先级和证据。查询不外发、不创建任务、不把候选当成已确认事实。",
    _identity_props({
        "ask_role": {"type": "string", "enum": ["", "boss", "manager", "teacher", "staff"], "default": "", "description": "可选，按建议事实归属角色筛选。"},
        "status": {"type": "string", "default": "", "description": "可选，默认候选状态为 candidate。"},
        "limit": {"type": "integer", "default": 50},
    }),
    ["user_id"],
)

TUOGUAN_SUBMIT_FACT_GAP_CANDIDATE_SCHEMA = _schema(
    "保存事实缺口候选。只记录小优缺什么、为什么影响工作、建议问谁和候选问题；不直接外发、不创建任务、不改正式机构事实、不规定模型下一步。",
    _identity_props({
        "gap_key": {"type": "string", "description": "稳定缺口键，如 teacher_record_habit、service_relation_owner。"},
        "gap_text": {"type": "string", "description": "缺口说明。"},
        "fact_owner_role": {"type": "string", "enum": ["boss", "manager", "teacher", "staff"], "description": "最可能掌握事实的人群。"},
        "suggested_question": {"type": "string", "description": "模型可参考的一句话问题候选，不代表已经发送。"},
        "target_user_id": {"type": "string", "description": "可选，建议询问对象 user_id。"},
        "target_name": {"type": "string", "description": "可选，建议询问对象姓名。"},
        "impact": {"type": "string", "description": "这个事实缺口会影响什么工作。"},
        "urgency": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
        "target_time": {"type": "string", "description": "建议关注时间，可为空。"},
        "related_objects": {"type": "array", "items": {}, "description": "相关学生、任务、目标或证据。"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
        "operation_id": {"type": "string", "description": "幂等操作 id。"},
    }),
    ["user_id", "gap_key", "gap_text", "fact_owner_role", "operation_id"],
)

TUOGUAN_SUBMIT_STAFF_VOICE_SIGNAL_SCHEMA = _schema(
    "保存员工声音信号。老师/店长侧只做支持和问题梳理；本工具只沉淀老板侧管理摘要、风险、证据和建议关注点，不改绩效、工资、制度、权限，不触达家长，也不在员工回复中暴露上报状态。",
    _identity_props({
        "operation_id": {"type": "string", "description": "幂等操作 id，优先使用当前消息 id。"},
        "source_role": {"type": "string", "enum": ["", "teacher", "manager"], "default": "", "description": "声音来源角色；默认取当前可信身份。"},
        "source_user_id": {"type": "string", "description": "来源老师/店长 user_id；不确定可空。"},
        "source_name": {"type": "string", "description": "来源老师/店长姓名；不确定可空。"},
        "category": {"type": "string", "enum": ["", "workload_pressure", "schedule_or_staffing", "collaboration_conflict", "policy_confusion", "morale_risk", "resignation_risk", "safety_or_student_risk", "management_suggestion", "tooling_or_process_frustration", "other_work_signal"], "default": ""},
        "risk_level": {"type": "string", "enum": ["", "low", "medium", "high", "urgent"], "default": "", "description": "低风险做趋势，中高风险老板可点名，high/urgent 生成老板-only 提醒候选。"},
        "signal_summary": {"type": "string", "description": "脱敏摘要，说明员工表达出的工作相关信号。"},
        "impact": {"type": "string", "description": "可能影响的现场协作、学生服务、人员稳定或制度执行。"},
        "suggested_owner_action": {"type": "string", "description": "建议老板关注什么；不是指令，不替老板决策。"},
        "evidence_excerpt": {"type": "string", "description": "短证据摘要，不要放完整私聊原文。"},
        "source_text": {"type": "string", "description": "来源原文或简述，会被限制长度；不默认展示给老板。"},
        "source_message_id": {"type": "string", "description": "来源消息 id，用于幂等。"},
        "occurred_at": {"type": "string", "description": "信号发生时间 ISO，可空。"},
        "status": {"type": "string", "enum": ["open", "reviewed", "resolved", "dismissed", "superseded"], "default": "open"},
    }),
    ["user_id", "operation_id", "signal_summary"],
)

TUOGUAN_QUERY_STAFF_VOICE_RADAR_SCHEMA = _schema(
    "老板只读查询员工声音雷达：汇总老师/店长表达出的抱怨、压力、协作冲突、制度不清、离职风险、安全风险和管理建议。低风险默认趋势化，中高风险显示人员和证据摘要；不外发、不派任务、不改绩效工资制度。",
    _identity_props({
        "risk_level": {"type": "string", "enum": ["", "low", "medium", "high", "urgent"], "default": ""},
        "status": {"type": "string", "enum": ["", "open", "reviewed", "resolved", "dismissed", "superseded"], "default": ""},
        "now_at": {"type": "string", "description": "可选，当前时间 ISO 字符串；默认系统当前时间。"},
        "since_hours": {"type": "integer", "default": 168, "description": "查询最近多少小时。"},
        "limit": {"type": "integer", "default": 50},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_STAFF_CONVERSATION_ACTIVITY_SCHEMA = _schema(
    "老板只读查询员工与小优的对话活动摘要：按时间窗口汇总老师/店长是否联系过小优、最近时间、轮次数和最近一句摘要。它不是完整私聊导出，不外发、不派任务、不改绩效工资制度。",
    _identity_props({
        "period": {"type": "string", "enum": ["today", "yesterday", "last_24h"], "default": "today", "description": "查询窗口，默认今天。"},
        "since_hours": {"type": "integer", "default": 24, "description": "period 为 last_24h 时使用。"},
        "include_latest_excerpt": {"type": "boolean", "default": True, "description": "是否返回最近一句摘要。"},
        "teacher_name": {"type": "string", "description": "可选，按老师/店长姓名或企业微信 user id 筛选。"},
        "now_at": {"type": "string", "description": "可选，当前时间 ISO 字符串；默认系统当前时间。"},
        "limit": {"type": "integer", "default": 20},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_XIAOYOU_HEALTH_SCHEMA = _schema(
    "只读查询小优健康度：日报是否送达、主动问题是否卡住、任务提醒是否重复、工具失败候选、进化候选和事实缺口。它只返回维护/看板材料，不外发、不派任务、不改事实。",
    _identity_props({
        "now_at": {"type": "string", "description": "可选，当前时间 ISO 字符串；默认系统当前时间。"},
        "limit": {"type": "integer", "default": 20, "description": "最多返回事实缺口候选数量。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_SUPERVISION_STATUS_SCHEMA = _schema(
    "只读查询小优承诺、监督与低风险自愈状态。返回脱敏发现、复发和修复回执，不包含聊天正文、模型推理、密钥或家长隐私，也不会触发修复或外发。",
    _identity_props({
        "limit": {"type": "integer", "default": 20, "description": "最多返回多少条监督发现。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_SELF_EVOLUTION_LEDGER_SCHEMA = _schema(
    "只读查询小优自我进化账本：最近学到的工作方式、错误修正、工具失败、机构事实缺口、手册候选、明日重点和 multi-agent 建议采纳记录。它只提供经验和审核材料，不自动改变制度、权限、手册、家长外发或模型下一步。",
    _identity_props({
        "candidate_type": {"type": "string", "enum": ["", "person_preference_candidate", "institution_fact_gap", "self_correction", "tool_failure_or_bug", "handbook_method_candidate", "tomorrow_focus", "multi_agent_adoption"], "default": "", "description": "可选，筛选候选类型。"},
        "status": {"type": "string", "enum": ["", "candidate", "ready_for_application", "applied", "pending_review", "needs_confirmation", "rejected", "superseded", "verified", "failed", "fixed", "expired", "needs_retest"], "default": "", "description": "可选，筛选处理状态。"},
        "limit": {"type": "integer", "default": 30, "description": "最多返回记录数。"},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_INDUSTRY_LEARNING_CANDIDATES_SCHEMA = _schema(
    "只读查询 Hermes 收集的托管/教培行业学习候选。公开资料只作为经营建议材料，老板审核前不进入正式手册或机构事实。",
    _identity_props({
        "status": {"type": "string", "description": "可选：pending_review、approved、rejected、needs_more_evidence、source_failed。"},
        "limit": {"type": "integer", "default": 30},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_EXTERNAL_RESEARCH_RUNS_SCHEMA = _schema(
    "只读查询 Hermes 外部学习运行记录：什么时候查过、查了什么、是否有来源。它不规定模型下一步。",
    _identity_props({
        "mode": {"type": "string", "description": "可选：weekly_industry、monthly_market、manual_topic。"},
        "limit": {"type": "integer", "default": 20},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_MARKET_RESEARCH_CANDIDATES_SCHEMA = _schema(
    "只读查询本地市场调研候选，包括公开竞品线索、服务卖点和待核验判断。公开资料不能直接当成已确认事实。",
    _identity_props({
        "status": {"type": "string", "description": "可选：pending_review 或 source_failed。"},
        "limit": {"type": "integer", "default": 30},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_COMPETITOR_PROFILES_SCHEMA = _schema(
    "只读查询竞品公开画像候选。结果来自公开来源，置信度默认较低，需要人工核验。",
    _identity_props({"limit": {"type": "integer", "default": 30}}),
    ["user_id"],
)

TUOGUAN_QUERY_EXTERNAL_LEARNING_BRIEF_SCHEMA = _schema(
    "只读汇总 Hermes 外部学习、行业候选、本地市场候选和竞品公开线索。它只提供材料，不替模型采纳或决策。",
    _identity_props({"limit": {"type": "integer", "default": 5}}),
    ["user_id"],
)

TUOGUAN_QUERY_SOCIAL_MARKET_RESEARCH_SCHEMA = _schema(
    "只读查询小优收集的抖音/小红书本地托管市场观察候选。结果只是外部平台观察，不是优益已确认事实；本工具不发布、不评论、不点赞、不关注、不改机构事实。",
    _identity_props({
        "platform": {"type": "string", "enum": ["", "xiaohongshu", "douyin"], "default": ""},
        "status": {"type": "string", "description": "可选：pending_review、source_failed、backend_unavailable。"},
        "limit": {"type": "integer", "default": 30},
    }),
    ["user_id"],
)

TUOGUAN_QUERY_PROJECT_OPPORTUNITIES_SCHEMA = _schema(
    "老板只读查询小优基于最近30天内部运营证据形成的新项目机会候选。结果会明确证据强度、待验证事实和边界；候选不等于正式立项。",
    _identity_props({
        "project_id": {"type": "string", "description": "可选，限定一个项目。"},
        "status": {"type": "string", "description": "可选，按候选状态筛选。"},
        "include_internal": {"type": "boolean", "default": False, "description": "仅老板排查时查看未达到展示门槛的内部观察。"},
        "limit": {"type": "integer", "default": 20},
    }),
    ["user_id"],
)

TUOGUAN_REVIEW_PROJECT_OPPORTUNITY_SCHEMA = _schema(
    "老板审核新项目机会候选。批准验证不等于正式立项，也不会绕过主动联系授权；所有写入必须幂等并写后反查。",
    _identity_props({
        "opportunity_id": {"type": "string", "description": "项目机会候选编号。"},
        "decision": {
            "type": "string",
            "enum": ["approve_validation", "defer", "dismiss", "reopen"],
            "description": "批准验证、暂缓、驳回或重新打开。",
        },
        "note": {"type": "string", "description": "可选，老板说明。"},
        "operation_id": {"type": "string", "description": "当前消息id或幂等键。"},
    }),
    ["user_id", "opportunity_id", "decision", "operation_id"],
)

TUOGUAN_SUBMIT_INDUSTRY_LEARNING_CANDIDATE_SCHEMA = _schema(
    "提交带来源的行业学习候选。老板审核前不会进入正式手册、长期记忆或优益机构事实；写入必须提供 operation_id。",
    _identity_props({
        "topic": {"type": "string", "description": "学习主题。"},
        "summary": {"type": "string", "description": "候选摘要，必须说明来源和不确定项。"},
        "operation_id": {"type": "string", "description": "当前消息 id 或幂等键。"},
        "sources": {"type": "array", "items": {}, "description": "公开来源列表，必须尽量包含 URL、标题、抓取时间。"},
        "applicability": {"type": "string", "description": "对托管经营的适用判断。"},
        "status": {"type": "string", "enum": ["pending_review", "approved", "rejected", "needs_more_evidence", "source_failed"], "default": "pending_review"},
        "source_text": {"type": "string", "description": "来源原文或简述。"},
        "source_message_id": {"type": "string", "description": "来源消息 id。"},
    }),
    ["user_id", "topic", "summary", "operation_id"],
)


TUOGUAN_LIST_LEARNING_CANDIDATES_SCHEMA = _schema(
    "店长/老板查看最近待审核学习候选，形成系统越用越聪明的运营闭环。",
    _identity_props(),
    ["user_id"],
)

TUOGUAN_REVIEW_LEARNING_CANDIDATE_SCHEMA = _schema(
    "老板批准或驳回学习候选。批准后进入正式知识库；驳回不影响正式行为。",
    _identity_props(
        {
            "candidate_id": {"type": "string", "description": "候选 id。"},
            "decision": {"type": "string", "enum": ["approve", "reject"], "description": "approve 或 reject。"},
            "revised_title": {"type": "string", "description": "可选，批准前改写标题。"},
            "revised_content": {"type": "string", "description": "可选，批准前改写内容。"},
        }
    ),
    ["user_id", "candidate_id", "decision"],
)


TOOLS = (
    ("tuoguan_context", TUOGUAN_CONTEXT_SCHEMA, _handler("context")),
    ("tuoguan_goal_workspace", TUOGUAN_GOAL_WORKSPACE_SCHEMA, _handler("goal_workspace")),
    ("tuoguan_query_students", TUOGUAN_QUERY_STUDENTS_SCHEMA, _handler("query_students")),
    ("tuoguan_query_tasks", TUOGUAN_QUERY_TASKS_SCHEMA, _handler("query_tasks")),
    ("tuoguan_next_task", TUOGUAN_NEXT_TASK_SCHEMA, _handler("next_task")),
    ("tuoguan_current_task_guidance", TUOGUAN_CURRENT_TASK_GUIDANCE_SCHEMA, _handler("current_task_guidance")),
    ("tuoguan_register_student", TUOGUAN_REGISTER_STUDENT_SCHEMA, _handler("register_student")),
    ("tuoguan_register_summer_student", TUOGUAN_REGISTER_SUMMER_STUDENT_SCHEMA, _handler("register_summer_student")),
    ("tuoguan_create_trial_lead", TUOGUAN_CREATE_TRIAL_LEAD_SCHEMA, _handler("create_trial_lead")),
    ("tuoguan_create_task", TUOGUAN_CREATE_TASK_SCHEMA, _handler("create_task")),
    ("tuoguan_cancel_task", TUOGUAN_CANCEL_TASK_SCHEMA, _handler("cancel_task")),
    ("tuoguan_query_operations_report", TUOGUAN_QUERY_OPERATIONS_REPORT_SCHEMA, _handler("query_operations_report")),
    ("tuoguan_verify_dashboard_visibility", TUOGUAN_VERIFY_DASHBOARD_VISIBILITY_SCHEMA, _handler("verify_dashboard_visibility")),
    ("tuoguan_dashboard_link", TUOGUAN_DASHBOARD_LINK_SCHEMA, _handler("dashboard_link")),
    ("tuoguan_record_summer_lesson", TUOGUAN_RECORD_SUMMER_LESSON_SCHEMA, _handler("record_summer_lesson")),
    ("tuoguan_record_student", TUOGUAN_RECORD_STUDENT_SCHEMA, _handler("record_student")),
    ("tuoguan_change_summer_points", TUOGUAN_CHANGE_SUMMER_POINTS_SCHEMA, _handler("change_summer_points")),
    ("tuoguan_query_summer_points", TUOGUAN_QUERY_SUMMER_POINTS_SCHEMA, _handler("query_summer_points")),
    ("tuoguan_query_summer_points_ranking", TUOGUAN_QUERY_SUMMER_POINTS_RANKING_SCHEMA, _handler("query_summer_points_ranking")),
    ("tuoguan_update_task", TUOGUAN_UPDATE_TASK_SCHEMA, _handler("update_task")),
    ("tuoguan_report_safety_event", TUOGUAN_REPORT_SAFETY_EVENT_SCHEMA, _handler("report_safety_event")),
    ("tuoguan_parent_script_context", TUOGUAN_PARENT_SCRIPT_CONTEXT_SCHEMA, _handler("parent_script_context")),
    ("tuoguan_resolve_student_responsibility", TUOGUAN_RESOLVE_STUDENT_RESPONSIBILITY_SCHEMA, _handler("resolve_student_responsibility")),
    ("tuoguan_query_institution_onboarding_gaps", TUOGUAN_QUERY_INSTITUTION_ONBOARDING_GAPS_SCHEMA, _handler("query_institution_onboarding_gaps")),
    ("tuoguan_query_operational_facts", TUOGUAN_QUERY_OPERATIONAL_FACTS_SCHEMA, _handler("query_operational_facts")),
    ("tuoguan_query_staff_directory", TUOGUAN_QUERY_STAFF_DIRECTORY_SCHEMA, _handler("query_staff_directory")),
    ("tuoguan_offboard_staff", TUOGUAN_OFFBOARD_STAFF_SCHEMA, _handler("offboard_staff")),
    ("tuoguan_query_person_workstyle_profile", TUOGUAN_QUERY_PERSON_WORKSTYLE_PROFILE_SCHEMA, _handler("query_person_workstyle_profile")),
    ("tuoguan_submit_person_workstyle_preference", TUOGUAN_SUBMIT_PERSON_WORKSTYLE_PREFERENCE_SCHEMA, _handler("submit_person_workstyle_preference")),
    ("tuoguan_query_workstyle_adaptation_health", TUOGUAN_QUERY_WORKSTYLE_ADAPTATION_HEALTH_SCHEMA, _handler("query_workstyle_adaptation_health")),
    ("tuoguan_submit_operational_fact", TUOGUAN_SUBMIT_OPERATIONAL_FACT_SCHEMA, _handler("submit_operational_fact")),
    ("tuoguan_confirm_operational_fact", TUOGUAN_CONFIRM_OPERATIONAL_FACT_SCHEMA, _handler("confirm_operational_fact")),
    ("tuoguan_query_student_service_relations", TUOGUAN_QUERY_STUDENT_SERVICE_RELATIONS_SCHEMA, _handler("query_student_service_relations")),
    ("tuoguan_query_parent_communication_coverage", TUOGUAN_QUERY_PARENT_COMMUNICATION_COVERAGE_SCHEMA, _handler("query_parent_communication_coverage")),
    ("tuoguan_query_weekly_record_coverage", TUOGUAN_QUERY_WEEKLY_RECORD_COVERAGE_SCHEMA, _handler("query_weekly_record_coverage")),
    ("tuoguan_query_active_goal_work_state", TUOGUAN_QUERY_ACTIVE_GOAL_WORK_STATE_SCHEMA, _handler("query_active_goal_work_state")),
    ("tuoguan_query_profile_candidates", TUOGUAN_QUERY_PROFILE_CANDIDATES_SCHEMA, _handler("query_profile_candidates")),
    ("tuoguan_query_value_ledger", TUOGUAN_QUERY_VALUE_LEDGER_SCHEMA, _handler("query_value_ledger")),
    ("tuoguan_submit_service_relation_fact_candidate", TUOGUAN_SUBMIT_SERVICE_RELATION_FACT_CANDIDATE_SCHEMA, _handler("submit_service_relation_fact_candidate")),
    ("tuoguan_submit_information_request_record", TUOGUAN_SUBMIT_INFORMATION_REQUEST_RECORD_SCHEMA, _handler("submit_information_request_record")),
    ("tuoguan_query_information_requests", TUOGUAN_QUERY_INFORMATION_REQUESTS_SCHEMA, _handler("query_information_requests")),
    ("tuoguan_submit_information_request_update", TUOGUAN_SUBMIT_INFORMATION_REQUEST_UPDATE_SCHEMA, _handler("submit_information_request_update")),
    ("tuoguan_submit_profile_candidate", TUOGUAN_SUBMIT_PROFILE_CANDIDATE_SCHEMA, _handler("submit_profile_candidate")),
    ("tuoguan_submit_profile_candidate_correction", TUOGUAN_SUBMIT_PROFILE_CANDIDATE_CORRECTION_SCHEMA, _handler("submit_profile_candidate_correction")),
    ("tuoguan_submit_goal_evidence", TUOGUAN_SUBMIT_GOAL_EVIDENCE_SCHEMA, _handler("submit_goal_evidence")),
    ("tuoguan_submit_performance_evidence_candidate", TUOGUAN_SUBMIT_PERFORMANCE_EVIDENCE_CANDIDATE_SCHEMA, _handler("submit_performance_evidence_candidate")),
    ("tuoguan_query_performance_evidence_candidates", TUOGUAN_QUERY_PERFORMANCE_EVIDENCE_CANDIDATES_SCHEMA, _handler("query_performance_evidence_candidates")),
    ("tuoguan_submit_performance_evidence_response", TUOGUAN_SUBMIT_PERFORMANCE_EVIDENCE_RESPONSE_SCHEMA, _handler("submit_performance_evidence_response")),
    ("tuoguan_submit_value_ledger_entry", TUOGUAN_SUBMIT_VALUE_LEDGER_ENTRY_SCHEMA, _handler("submit_value_ledger_entry")),
    ("tuoguan_query_hermes_work_items", TUOGUAN_QUERY_HERMES_WORK_ITEMS_SCHEMA, _handler("query_hermes_work_items")),
    ("tuoguan_query_institution_work", TUOGUAN_QUERY_INSTITUTION_WORK_SCHEMA, _handler("query_institution_work")),
    ("tuoguan_advance_institution_work", TUOGUAN_ADVANCE_INSTITUTION_WORK_SCHEMA, _handler("advance_institution_work")),
    ("tuoguan_submit_hermes_work_item", TUOGUAN_SUBMIT_HERMES_WORK_ITEM_SCHEMA, _handler("submit_hermes_work_item")),
    ("tuoguan_update_hermes_work_item", TUOGUAN_UPDATE_HERMES_WORK_ITEM_SCHEMA, _handler("update_hermes_work_item")),
    ("tuoguan_query_work_commitments", TUOGUAN_QUERY_WORK_COMMITMENTS_SCHEMA, _handler("query_work_commitments")),
    ("tuoguan_submit_work_commitment", TUOGUAN_SUBMIT_WORK_COMMITMENT_SCHEMA, _handler("submit_work_commitment")),
    ("tuoguan_update_work_commitment", TUOGUAN_UPDATE_WORK_COMMITMENT_SCHEMA, _handler("update_work_commitment")),
    ("tuoguan_query_wakeup_requests", TUOGUAN_QUERY_WAKEUP_REQUESTS_SCHEMA, _handler("query_wakeup_requests")),
    ("tuoguan_submit_wakeup_request", TUOGUAN_SUBMIT_WAKEUP_REQUEST_SCHEMA, _handler("submit_wakeup_request")),
    ("tuoguan_update_wakeup_request", TUOGUAN_UPDATE_WAKEUP_REQUEST_SCHEMA, _handler("update_wakeup_request")),
    ("tuoguan_submit_due_wakeup_candidate", TUOGUAN_SUBMIT_DUE_WAKEUP_CANDIDATE_SCHEMA, _handler("submit_due_wakeup_candidate")),
    ("tuoguan_query_business_events", TUOGUAN_QUERY_BUSINESS_EVENTS_SCHEMA, _handler("query_business_events")),
    ("tuoguan_submit_business_event", TUOGUAN_SUBMIT_BUSINESS_EVENT_SCHEMA, _handler("submit_business_event")),
    ("tuoguan_query_action_executions", TUOGUAN_QUERY_ACTION_EXECUTIONS_SCHEMA, _handler("query_action_executions")),
    ("tuoguan_submit_action_execution", TUOGUAN_SUBMIT_ACTION_EXECUTION_SCHEMA, _handler("submit_action_execution")),
    ("tuoguan_query_autonomous_work_brief", TUOGUAN_QUERY_AUTONOMOUS_WORK_BRIEF_SCHEMA, _handler("query_autonomous_work_brief")),
    ("tuoguan_query_proactive_work_radar", TUOGUAN_QUERY_PROACTIVE_WORK_RADAR_SCHEMA, _handler("query_proactive_work_radar")),
    ("tuoguan_query_active_work_context", TUOGUAN_QUERY_ACTIVE_WORK_CONTEXT_SCHEMA, _handler("query_active_work_context")),
    ("tuoguan_query_attention_threads", TUOGUAN_QUERY_ATTENTION_THREADS_SCHEMA, _handler("query_attention_threads")),
    ("tuoguan_update_attention_thread", TUOGUAN_UPDATE_ATTENTION_THREAD_SCHEMA, _handler("update_attention_thread")),
    ("tuoguan_query_relationship_touch_candidates", TUOGUAN_QUERY_RELATIONSHIP_TOUCH_CANDIDATES_SCHEMA, _handler("query_relationship_touch_candidates")),
    ("tuoguan_submit_relationship_touch_candidate", TUOGUAN_SUBMIT_RELATIONSHIP_TOUCH_CANDIDATE_SCHEMA, _handler("submit_relationship_touch_candidate")),
    ("tuoguan_query_proactive_authorizations", TUOGUAN_QUERY_PROACTIVE_AUTHORIZATIONS_SCHEMA, _handler("query_proactive_authorizations")),
    ("tuoguan_submit_proactive_authorization", TUOGUAN_SUBMIT_PROACTIVE_AUTHORIZATION_SCHEMA, _handler("submit_proactive_authorization")),
    ("tuoguan_execute_relationship_touch", TUOGUAN_EXECUTE_RELATIONSHIP_TOUCH_SCHEMA, _handler("execute_relationship_touch")),
    ("tuoguan_update_relationship_touch", TUOGUAN_UPDATE_RELATIONSHIP_TOUCH_SCHEMA, _handler("update_relationship_touch")),
    ("tuoguan_query_goal_actions", TUOGUAN_QUERY_GOAL_ACTIONS_SCHEMA, _handler("query_goal_actions")),
    ("tuoguan_submit_goal_action", TUOGUAN_SUBMIT_GOAL_ACTION_SCHEMA, _handler("submit_goal_action")),
    ("tuoguan_query_employee_work_map", TUOGUAN_QUERY_EMPLOYEE_WORK_MAP_SCHEMA, _handler("query_employee_work_map")),
    ("tuoguan_query_fact_gap_candidates", TUOGUAN_QUERY_FACT_GAP_CANDIDATES_SCHEMA, _handler("query_fact_gap_candidates")),
    ("tuoguan_submit_fact_gap_candidate", TUOGUAN_SUBMIT_FACT_GAP_CANDIDATE_SCHEMA, _handler("submit_fact_gap_candidate")),
    ("tuoguan_submit_staff_voice_signal", TUOGUAN_SUBMIT_STAFF_VOICE_SIGNAL_SCHEMA, _handler("submit_staff_voice_signal")),
    ("tuoguan_query_staff_voice_radar", TUOGUAN_QUERY_STAFF_VOICE_RADAR_SCHEMA, _handler("query_staff_voice_radar")),
    ("tuoguan_query_staff_conversation_activity", TUOGUAN_QUERY_STAFF_CONVERSATION_ACTIVITY_SCHEMA, _handler("query_staff_conversation_activity")),
    ("tuoguan_query_xiaoyou_health", TUOGUAN_QUERY_XIAOYOU_HEALTH_SCHEMA, _handler("query_xiaoyou_health")),
    ("tuoguan_query_supervision_status", TUOGUAN_QUERY_SUPERVISION_STATUS_SCHEMA, _handler("query_supervision_status")),
    ("tuoguan_query_self_evolution_ledger", TUOGUAN_QUERY_SELF_EVOLUTION_LEDGER_SCHEMA, _handler("query_self_evolution_ledger")),
    ("tuoguan_query_industry_learning_candidates", TUOGUAN_QUERY_INDUSTRY_LEARNING_CANDIDATES_SCHEMA, _handler("query_industry_learning_candidates")),
    ("tuoguan_query_external_research_runs", TUOGUAN_QUERY_EXTERNAL_RESEARCH_RUNS_SCHEMA, _handler("query_external_research_runs")),
    ("tuoguan_query_market_research_candidates", TUOGUAN_QUERY_MARKET_RESEARCH_CANDIDATES_SCHEMA, _handler("query_market_research_candidates")),
    ("tuoguan_query_competitor_profiles", TUOGUAN_QUERY_COMPETITOR_PROFILES_SCHEMA, _handler("query_competitor_profiles")),
    ("tuoguan_query_external_learning_brief", TUOGUAN_QUERY_EXTERNAL_LEARNING_BRIEF_SCHEMA, _handler("query_external_learning_brief")),
    ("tuoguan_query_social_market_research", TUOGUAN_QUERY_SOCIAL_MARKET_RESEARCH_SCHEMA, _handler("query_social_market_research")),
    ("tuoguan_query_project_opportunities", TUOGUAN_QUERY_PROJECT_OPPORTUNITIES_SCHEMA, _handler("query_project_opportunities")),
    ("tuoguan_review_project_opportunity", TUOGUAN_REVIEW_PROJECT_OPPORTUNITY_SCHEMA, _handler("review_project_opportunity")),
    ("tuoguan_submit_industry_learning_candidate", TUOGUAN_SUBMIT_INDUSTRY_LEARNING_CANDIDATE_SCHEMA, _handler("submit_industry_learning_candidate")),
    ("tuoguan_generate_autonomous_recovery_report", TUOGUAN_GENERATE_AUTONOMOUS_RECOVERY_REPORT_SCHEMA, _handler("generate_autonomous_recovery_report")),
    ("tuoguan_generate_due_wakeup_candidates", TUOGUAN_GENERATE_DUE_WAKEUP_CANDIDATES_SCHEMA, _handler("generate_due_wakeup_candidates")),
    ("tuoguan_query_gray_observations", TUOGUAN_QUERY_GRAY_OBSERVATIONS_SCHEMA, _handler("query_gray_observations")),
    ("tuoguan_submit_gray_observation", TUOGUAN_SUBMIT_GRAY_OBSERVATION_SCHEMA, _handler("submit_gray_observation")),
    ("tuoguan_query_gray_rollout_decisions", TUOGUAN_QUERY_GRAY_ROLLOUT_DECISIONS_SCHEMA, _handler("query_gray_rollout_decisions")),
    ("tuoguan_generate_gray_review", TUOGUAN_GENERATE_GRAY_REVIEW_SCHEMA, _handler("generate_gray_review")),
    ("tuoguan_query_gray_scenario_cards", TUOGUAN_QUERY_GRAY_SCENARIO_CARDS_SCHEMA, _handler("query_gray_scenario_cards")),
    ("tuoguan_generate_gray_trial_start_pack", TUOGUAN_GENERATE_GRAY_TRIAL_START_PACK_SCHEMA, _handler("generate_gray_trial_start_pack")),
    ("tuoguan_generate_autonomous_acceptance_pack", TUOGUAN_GENERATE_AUTONOMOUS_ACCEPTANCE_PACK_SCHEMA, _handler("generate_autonomous_acceptance_pack")),
    ("tuoguan_generate_autonomous_log_review", TUOGUAN_GENERATE_AUTONOMOUS_LOG_REVIEW_SCHEMA, _handler("generate_autonomous_log_review")),
    ("tuoguan_generate_gray_observation_candidates", TUOGUAN_GENERATE_GRAY_OBSERVATION_CANDIDATES_SCHEMA, _handler("generate_gray_observation_candidates")),
    ("tuoguan_submit_gray_rollout_decision", TUOGUAN_SUBMIT_GRAY_ROLLOUT_DECISION_SCHEMA, _handler("submit_gray_rollout_decision")),
    ("tuoguan_query_gray_optimization_decisions", TUOGUAN_QUERY_GRAY_OPTIMIZATION_DECISIONS_SCHEMA, _handler("query_gray_optimization_decisions")),
    ("tuoguan_submit_gray_optimization_decision", TUOGUAN_SUBMIT_GRAY_OPTIMIZATION_DECISION_SCHEMA, _handler("submit_gray_optimization_decision")),
    ("tuoguan_submit_learning_candidate", TUOGUAN_SUBMIT_LEARNING_CANDIDATE_SCHEMA, _handler("submit_learning_candidate")),
    ("tuoguan_list_learning_candidates", TUOGUAN_LIST_LEARNING_CANDIDATES_SCHEMA, _handler("list_learning_candidates")),
    ("tuoguan_review_learning_candidate", TUOGUAN_REVIEW_LEARNING_CANDIDATE_SCHEMA, _handler("review_learning_candidate")),
)


LEGACY_TOOLS = TOOLS


def model_tools(surface: str = ""):
    """Return the compact production surface plus explicit routine fast paths."""

    selected = str(surface or os.getenv("HERMES_TUOGUAN_TOOL_SURFACE", "facade")).strip().lower()
    if selected == "legacy":
        return LEGACY_TOOLS
    if selected != "facade":
        raise ValueError("HERMES_TUOGUAN_TOOL_SURFACE must be facade or legacy")
    from .capability_facades import FAST_PATH_TOOL_NAMES, build_facade_tools

    fast_path_names = set(FAST_PATH_TOOL_NAMES)
    fast_paths = tuple(tool for tool in LEGACY_TOOLS if tool[0] in fast_path_names)
    facades = build_facade_tools(LEGACY_TOOLS, tool_result=tool_result)
    return (*fast_paths, *facades)

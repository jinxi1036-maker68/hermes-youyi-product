# 小优 H5 V1 数据字段映射

> 版本：V1.0
>
> 状态：原型设计输入
>
> 前置约束：以《小优 H5 V1 产品结构冻结稿》为准；本文不新增业务状态，不修改生产数据。

## 1. 映射结论

V1 的学生、人员、任务、记录、项目、日报和外发状态都有真实底层来源。正式开发需要做的是建立角色化只读投影，修正旧口径和新鲜度表达，不需要为 H5 再造业务账本。

当前接口继续作为鉴权入口：

| 路由 | 当前用途 | V1 处理 |
| --- | --- | --- |
| `/tuoguan/dashboard` | 返回内嵌 H5 页面 | 保留路由，替换为 V1 页面资源 |
| `/tuoguan/api/me` | 返回可信用户、角色和项目范围 | 直接保留，增加机构显示名和数据状态可后补 |
| `/tuoguan/api/teacher` | 老师角色快照 | 改为老师工作台投影，不改变后端角色校验 |
| `/tuoguan/api/boss` | 店长或老板快照 | 保留角色校验；按角色返回不同投影 |
| `/tuoguan/parent-report` | 家长报告 | V1 不并入工作台，维持独立链路 |

## 2. 数据状态统一字段

V1 每个区块统一携带以下展示元数据：

| 字段 | 含义 |
| --- | --- |
| `data_state` | `current / empty / missing / stale / forbidden / failed` |
| `source_kind` | `business_fact / execution_receipt / xiaoyou_judgement / projection` |
| `source_updated_at` | 权威源最后更新时间 |
| `projected_at` | H5 投影生成时间 |
| `freshness_label` | 面向用户的“10分钟前更新 / 数据待补 / 已过期” |
| `evidence_refs` | 任务、记录、回执等可追溯对象编号，不含聊天正文 |

`projected_at` 不能替代 `source_updated_at`。缺失值不得通过前端 `safe()` 转换成 `0`。

## 3. 老师页面字段映射

### 3.1 今日

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 小优今日提示 | `tasks.json`、`records.json` | `daily_coach.top_actions`、`open_tasks`、`suggested_records` | 最多3条；取消/完成/过期任务剔除；无证据不展示 |
| 当前任务 | `tasks.json` | `open_tasks[]` | 只显示本人执行的开放任务；按等级、截止时间和等待证据排序 |
| 任务陪伴状态 | `tasks.json`、`task_closure_events.json` | 当前任务卡和闭环事件已有底座 | 投影 `not_started / coaching / waiting_evidence / verified`，不新建状态 |
| 重点学生 | `students.json`、`records.json`、`tasks.json` | `suggested_records[]`、`uncovered_students[]`、`student_completion[]` | 最多3人；必须给出开放任务、记录缺口或有效风险原因 |
| 今日积累 | `records.json` | `today_feedback[]` | 只显示本人有效记录，按时间倒序 |
| 下一步 | 上述事实的只读组合 | `daily_coach.top_actions[]` | 只给1个动作；需要写入时回企业微信 |

### 3.2 学生

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 本人学生范围 | `students.json` | `student_completion[]` | 仅 `_student_teacher(profile) == current_user` 且当前有效 |
| 需要关注 | `records.json`、`tasks.json` | `suggested_records[]`、`open_tasks[]` | 按当前开放任务、近期有效记录缺口排序 |
| 全部学生 | `students.json` | `student_completion[]` | 显示姓名、项目和数据状态；不显示绩效完整度分数 |
| 学生详情 | `students.json`、`records.json`、`tasks.json` | `today_feedback[]`、`record_review.*`、`open_tasks[]` | 按学生名聚合最近记录、开放任务、责任关系和更新时间 |

### 3.3 我的积累

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 最近有效记录 | `records.json` | `record_review.effective_records[]` | 去掉工资和分值表达，保留事实、时间、类型和使用场景 |
| 家长沟通材料 | `records.json` | `materials.parent_communication[]` | 只读，可复制；发送仍回企业微信审核 |
| 成长报告材料 | `records.json`、`growth_reports.json` | `materials.growth_reports[]` | 区分普通材料与 approved 报告 |
| 学生标签材料 | `records.json` | `materials.student_tags[]` | 仅作为老师本人工作积累，不自动成为学生正式标签 |
| 月度总结 | 以上只读聚合 | `record_review.counts` 可部分复用 | 只做私人摘要，不做排名、工资或绩效判断 |

## 4. 店长页面字段映射

### 4.1 今日现场

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 安全与服务异常 | `tasks.json`、`records.json` | `task_status.safety_risks[]`、`recent_anomalies[]` | 当前有效且在授权范围；S级优先 |
| 卡住或逾期任务 | `tasks.json`、`task_closure_events.json` | `task_status.closure_evidence.missing_evidence_tasks[]` | 按截止时间、等待证据和等级分组 |
| 小优现场建议 | 上述事实 | `hermes_assistant.top_suggestions[]` | 现有覆盖率驱动建议先停用；V1只接受任务或记录事实支撑的建议 |
| 待确认与交接 | `attention_threads.jsonl`、任务回执 | `hermes_assistant.pending_confirmations[]` | 只取当前人员可见、未过期且有对象的事项 |
| 项目状态 | 项目配置、学生和记录 | `program_scope`、`summary` | 移除“普通托管冻结”硬编码，按项目真实状态显示 |

### 4.2 学生

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 授权学生范围 | `students.json`、经理团队映射 | `student_roster[]` | 继续使用后端范围过滤 |
| 责任老师 | `students.json`、`staff.json` | `student_roster.teacher_userid` | 转为人员显示名，并校验在职状态 |
| 需要关注 | `records.json`、`tasks.json` | `risk.priority_students[]`、`risk.long_unrecorded_students[]` | 必须标明原因、时间和证据状态 |
| 学生详情 | 学生、记录、任务 | 现有字段需按学生聚合 | 只读展示服务证据与任务，不展示其他私人信息 |

### 4.3 团队

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 在职人员 | `staff.json` | 当前 `teacher_execution[]` 只覆盖有学生人员 | V1补只读人员投影，区分 active/left/unknown |
| 需要支持 | `tasks.json`、`records.json` | `hermes_assistant.top_suggestions[]`、`execution.teachers[]` | 不使用错误覆盖率；以开放任务和明确证据缺口说明原因 |
| 当前负荷 | `tasks.json` | `execution.teachers[].open_task_count` | 只显示开放任务数量及状态，不做排名 |
| 数据缺口 | 学生责任关系、记录 | `stale_students[]`、`missing_growth_report_students[]` | 展示为“需要支持”，不展示为绩效分数 |

### 4.4 任务

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 进行中 | `tasks.json` | `task_status.open_by_status`、任务卡 | 当前有效任务 |
| 等待证据 | `tasks.json`、闭环事件 | `missing_evidence_tasks[]` | 只显示缺少的一个或多个明确证据 |
| 已逾期 | `tasks.json` | `due_at` | 北京时间比较；取消和完成任务排除 |
| 最近完成 | `task_closure_events.json` | `recent_events[]` | 展示关闭时间和证据摘要 |

## 5. 老板页面字段映射

### 5.1 决策

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 待老板决定 | `attention_threads.jsonl`、目标行动 | `hermes_employee.open_questions[]` | 最多3项；必须当前有效、老板可见、有证据对象 |
| 核心目标 | 目标和工作项账本 | `hermes_employee.focus_brief`、`work_items[]` | 旧工作项需新鲜度门禁；无活动目标时明确空状态 |
| 高风险 | `tasks.json`、记录 | `task_status.safety_risks[]`、`risk.*` | 只显示当前未关闭风险 |
| 小优已推进 | `action_executions.jsonl`、outbox、任务闭环 | `hermes_employee.value_entries[]` 当前不足 | V1只展示有执行回执的动作，不采信自报价值文字 |

### 5.2 经营

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 项目切换 | 项目配置、角色范围 | `program_views`、`selected_program_id` | 不硬编码年份和暑假标签 |
| 学生状态 | `students.json` | `summary.student_count` | 显示口径和更新时间 |
| 今日记录 | `records.json` | `summary.today_records`、`today_valid_records` | 缺数据时显示待采集，不显示0 |
| 当前任务 | `tasks.json` | `summary.open_task_count`、`task_status` | 按状态展开 |
| 覆盖情况 | `records.json`、`students.json` | `summary.coverage_rate_7d` | 只使用唯一学生数口径；老师级旧口径禁用 |
| 日报交付 | `daily_report_runs.jsonl`、outbox | `hermes_employee.daily_reports` | 区分未生成、已入队、已发送、失败、结果未知 |
| 新项目机会 | `records.json`、`students.json`、`staff.json`、`project_opportunity_events.jsonl` | `project_opportunities` | 30天强证据门槛；最多3项；只展示汇总证据、小优判断和待验证边界 |

### 5.3 团队

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 人员状态 | `staff.json` | 当前无完整老板人员投影 | V1补 active/left/unknown 只读统计 |
| 团队卡点 | 任务、记录 | `execution.teachers[]` | 不排名，不展示工资，仅说明服务阻塞 |
| 店长支持 | 任务和门店范围 | `execution.managers[]` | 只展示真实开放事项和数据状态 |
| 员工声音趋势 | 员工声音候选账本 | 当前不适合直接展示 | 延后 V1.1，经脱敏、证据和权限聚合后进入 |

### 5.4 小优

| 页面内容 | 权威来源 | 当前可复用字段 | V1 聚合规则 |
| --- | --- | --- | --- |
| 早晚报状态 | 日报运行和 outbox | `daily_reports` | 直接复用并补 `failed/result_unknown` 文案 |
| 正在推进 | 工作项、目标行动 | `work_items[]`、`focus_brief` | 仅当前有效；候选不写成已执行 |
| 主动联系 | 主动候选、outbox | `relationship_presence.items[]` | 展示 candidate/queued/sent/replied/verified，不展示私聊正文 |
| 已核验成果 | 动作、任务、外发回执 | 当前需只读聚合 | 只有 `ExecutionReceipt` 或等价回执才能进入 |
| 失败和未知 | 工具、outbox、日报回执 | 当前分散 | V1投影统一状态，不新增故障账本 |

## 6. 当前字段禁用表

| 当前字段 | V1 处理 | 原因 |
| --- | --- | --- |
| `performance.*` | 不进入V1工作台 | 现阶段易形成绩效监督和错误激励 |
| `payroll.*` | 不进入V1一级页面 | 规则、缺失值和确认链仍需单独成熟 |
| `hermes_performance_tree` | 删除展示 | 不符合老师减负定位 |
| `week_contribution.growth_*` | 删除游戏化展示 | 不是权威业务事实 |
| `execution.teachers[].coverage_rate_7d` | 暂停使用 | 当前以记录数除学生数，可能超过100% |
| `opportunities[]` | 兼容期固定为空 | 旧关键词派生已停用，稳定一个版本后删除 |
| `project_opportunities.items[]` | 仅老板经营页 | 只展示强证据候选；老师、店长无权查询 |
| `learning.recent_candidates[]` | 不直接展示 | 属于内部候选，不是经营事实 |
| `relationship_presence.items[].message` | 不在老板页展示全文 | 避免暴露员工侧沟通原文 |
| `value_entries[].hermes_action/outcome` | 仅有回执时使用 | 当前可能是自报结果 |

## 7. V1 投影对象建议

正式开发时建议新增一个纯只读 `WorkbenchProjection`，但不新建业务文件：

```json
{
  "identity": {},
  "role": "teacher|manager|boss",
  "program": {},
  "generated_at": "",
  "source_freshness": {},
  "navigation": [],
  "home": {},
  "students": {},
  "team": {},
  "tasks": {},
  "materials": {},
  "xiaoyou": {},
  "project_opportunities": {
    "data_state": "current|empty|stale|failed",
    "source_updated_at": "",
    "visible_count": 0,
    "items": []
  }
}
```

该对象只在请求时或缓存刷新时由现有权威源重建。它不接受 H5 写入，也不反向覆盖 `students.json`、`tasks.json`、`records.json` 或任何业务账本。

## 8. 原型数据约束

- 原型统一使用虚构姓名和示意数字，不复制生产学生、老师或聊天数据。
- 原型中的“小优判断”必须同时展示证据状态。
- 原型只验证信息顺序、角色差异和移动端交互，不代表新功能已经上线。

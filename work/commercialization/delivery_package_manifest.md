# 新机构 Hermes 交付包清单 V1

目标：未来给一家新托管机构开通 Hermes 时，明确哪些材料来自产品母版，哪些材料需要生成，哪些材料必须隔离，哪些材料禁止携带。

## 1. 可复制的产品母版

这些可以从标准 Hermes 产品包复制。

- Hermes 运行代码。
- 托管核心工具能力。
- 写入守卫、审计、幂等、写后反查机制。
- 数字员工通用手册。
- 托管行业通用手册。
- 早报、晚报、自主唤醒机制。
- H5 老板/店长/老师通用页面结构。
- 存储治理、资源巡检、报告归档脚本。
- 单机构 systemd 服务模板。
- 测试套件。

## 2. 必须为新机构生成的材料

这些不能从示例机构复制，必须按新机构模板生成。

- `tenant_profile.json`
- `wecom_whitelist.json`
- `teacher_wecom_map.json`
- `institution_operating_model.json`
- 新机构 Memory 初始文件。
- 新机构技能入口名称和描述。
- 新机构 H5 访问 token 和角色权限。
- 新机构数据目录。
- 新机构备份目录。
- 新机构日报、唤醒、行业学习 timer。

## 3. 新机构运行后生成的材料

这些由 Hermes 在该机构运行过程中产生，必须按机构隔离。

- `hermes_work_items.jsonl`
- `wakeup_requests.jsonl`
- `business_events.jsonl`
- `action_executions.jsonl`
- `attention_threads.jsonl`
- `relationship_touch_candidates.jsonl`
- `daily_report_runs.jsonl`
- `hermes_employee_scorecard.jsonl`
- `value_progress_ledger.jsonl`
- `institution_understanding_state.json`
- `institution_fact_gap_events.jsonl`
- `industry_learning_candidates.jsonl`
- H5 dashboard cache。
- 自主唤醒报告和资源巡检报告。

## 4. 可以导入但必须标记来源的数据

这些可以来自客户历史资料，但必须标明数据所属时间、可信度、是否为当前事实。

- 历史学生名单。
- 历史家校沟通记录。
- 历史表现记录。
- 历史招生线索。
- 历史续费数据。
- 历史安全事件。
- 历史工资绩效表。

原则：历史数据可用于分析，不自动等同于当前学生、当前老师、当前责任关系。

## 5. 禁止携带到新机构的材料

新机构交付包必须排除以下内容。

- 示例机构学生、家长、老师真实资料。
- 示例机构企业微信密钥和用户映射。
- 示例机构 Memory。
- 示例机构聊天记录和企业微信原始消息。
- 示例机构目标账本、日报、唤醒报告、H5 缓存。
- 示例机构调试日志、测试失败、临时修复记录。
- 老板私密偏好和经营细节。
- 任何包含旧机构 tenant_id 或具体人员姓名的生产数据。

## 6. 单机构隔离交付形态

第一阶段推荐结构：

```text
/opt/hermes-platform/
  app/
    current/
  tenants/
    tenant_id/
      config/
      data/
      skills/
      memory/
      reports/
      backups/
```

服务命名建议：

```text
hermes@tenant_id.service
hermes-wakeup@tenant_id.timer
hermes-morning-report@tenant_id.timer
hermes-evening-report@tenant_id.timer
hermes-storage-audit@tenant_id.timer
```

当前示例机构生产暂不改成此结构。本清单只作为后续标准化交付目标。

## 7. 新机构开通验收

- Hermes 能说清楚自己服务的新机构名称。
- Hermes 不知道示例机构学生、老师、目标和聊天历史。
- 老板、店长、老师权限正确。
- H5 三类角色链接可访问，错角色被拒绝。
- 早报、晚报、30 分钟唤醒正常。
- 企业微信接收、发送、回执正常。
- 写入守卫、审计、幂等、写后反查正常。
- 不自动发家长。
- 不自动批量派老师任务。
- 不自动改工资、绩效结论、责任绑定、权限或删除数据。


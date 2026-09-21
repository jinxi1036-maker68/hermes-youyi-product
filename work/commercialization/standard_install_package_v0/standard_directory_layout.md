# 标准目录结构 V0

目标：把未来每家机构的 Hermes 做成独立租户，避免代码、数据、记忆、企业微信和 H5 权限混在一起。

## 1. 推荐顶层结构

```text
/opt/hermes-platform/
  app/
    releases/
      0.20.0/
      xiaoyou-<commit>/
    current -> releases/0.20.0
  tenants/
    demo_tuoguan/
      config/
      data/
      skills/
      memory/
      reports/
      backups/
      logs/
      runtime/
```

## 2. 产品代码目录

```text
/opt/hermes-platform/app/current/
  runtime/
  home-proddata/
  config/
  scripts/
  tests/
  .venv/
```

规则：

- 产品代码由版本发布流程管理。
- 小优插件由单一 `xiaoyou-<commit>` 发布物管理，加载位置只能链接到同一份已校验代码。
- 新机构不能修改 `app/current` 内的通用代码。
- 机构差异只能放到 `tenants/{tenant_id}`。
- 升级产品代码时，不能覆盖租户数据。

## 3. 单机构目录

```text
/opt/hermes-platform/tenants/{tenant_id}/
  config/
    tenant_profile.json
    runtime.env
    runtime.secrets.env
    dashboard_tokens.json
  data/
    students.json
    staff.json
    records.json
    tasks.json
    wecom_whitelist.json
    teacher_wecom_map.json
    institution_operating_model.json
    academic_term_state.json
    hermes_work_items.jsonl
    attention_threads.jsonl
    notification_outbox.json
  skills/
    institution-facts/
      SKILL.md
      references/
  memory/
    MEMORY.md
  reports/
    startup/
    autonomous/
    daily/
    resource/
  backups/
    pre-upgrade/
    daily/
    prewrite/
  logs/
  runtime/
    locks/
    tmp/
```

## 4. 目录责任

| 目录 | 责任 | 是否复制示例机构内容 |
| --- | --- | --- |
| `config/` | 租户配置、密钥引用、H5 token | 否 |
| `data/` | 业务数据和运行账本 | 否 |
| `skills/` | 机构事实 Skill | 否 |
| `memory/` | 机构长期记忆 | 否 |
| `reports/` | 机构报告 | 否 |
| `backups/` | 机构备份 | 否 |
| `logs/` | 机构运行日志 | 否 |
| `runtime/` | 临时文件和锁 | 否 |

## 5. 服务命名目标

```text
hermes@{tenant_id}.service
hermes-wakeup@{tenant_id}.timer
hermes-morning-report@{tenant_id}.timer
hermes-evening-report@{tenant_id}.timer
hermes-storage-audit@{tenant_id}.timer
```

环境变量目标：

```text
HERMES_TENANT_ID={tenant_id}
HERMES_TUOGUAN_DATA_DIR=/opt/hermes-platform/tenants/{tenant_id}/data
HERMES_TENANT_CONFIG_DIR=/opt/hermes-platform/tenants/{tenant_id}/config
HERMES_TENANT_MEMORY_DIR=/opt/hermes-platform/tenants/{tenant_id}/memory
HERMES_TENANT_SKILLS_DIR=/opt/hermes-platform/tenants/{tenant_id}/skills
```

当前示例机构生产暂不迁移到该结构；这是后续标准交付目标。

# Hermes 标准安装包 V0

本目录描述未来给一家新托管机构开通 Hermes 的标准安装包蓝图。

当前阶段只做设计，不执行部署，不修改优益生产目录，不生成真实租户。目标是让后续 `tenant_initializer` 脚本有清晰依据，避免把优益数据、记忆、企业微信配置或聊天历史复制到新机构。

## 当前定位

V0 采用“单机构隔离复制 + 标准化配置”：

- 一套产品代码，多家机构独立数据。
- 每家机构有自己的配置、数据、Memory、Skills、报告、备份和访问权限。
- 新机构只从 `tenant_profile.json` 和安全密钥引用生成。
- 所有运行账本默认空白，不复制优益历史。
- 真实 SaaS 多租户、中央运维后台和批量升级放到后续阶段。

## 文件说明

- `standard_directory_layout.md`：未来标准目录结构。
- `tenant_initialization_flow.md`：新机构初始化流程。
- `generated_files_spec.md`：复制、生成、空白初始化和禁止携带的文件规则。
- `first_boot_acceptance_checklist.md`：首次启动验收清单。
- `backup_and_upgrade_strategy.md`：备份、升级和回滚策略。
- `future_installer_script_spec.md`：未来初始化脚本规格。
- `tenant_intake_form_v0.md`：新机构生成租户包前的资料采集表。
- `second_tenant_delivery_steps.md`：第二家机构试点交付步骤。
- `acceptance_record_template.md`：首次启动和试点验收记录模板。

## V0 干跑工具

当前已经提供本地干跑脚本：

```text
python scripts/tenant_initializer.py --tenant-profile work/commercialization/demo_tenant_profile.json --platform-root work/commercialization/demo_output --force
```

该命令只在本地 `work/commercialization/demo_output/` 生成模拟租户目录，不连接服务器、不启动 Hermes、不读取优益生产数据。

生成后可运行本地启动前验收：

```text
python scripts/tenant_acceptance_check.py --tenant-root work/commercialization/demo_output/tenants/demo_tuoguan
```

该验收只检查目录、配置、空白账本、Memory/Skill 干净性和权限边界，不验证真实企业微信、H5 或模型回复。

## 与当前优益生产的关系

当前优益生产使用版本中立入口和独立业务数据目录：

- `/opt/hermes-youyi-current`
- `/opt/hermes-youyi/data/tuoguan-data`
- `hermes-youyi-019.service`

服务名保留 `019` 仅为生产兼容标识，实际底座版本必须通过加载检查确认，不能从服务名推断。

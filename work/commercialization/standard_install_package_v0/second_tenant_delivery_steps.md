# 第二家机构交付步骤 V0

目标：把 Hermes 从示例机构样板复制到第二家机构时，按固定步骤生成隔离租户包并完成首次验收。

## 1. 准备

- 从 `tenant_intake_form_v0.md` 收集机构资料。
- 用 `tenant_profile.template.json` 填写新机构 profile。
- 确认 profile 里只有 env/secret 引用，没有明文密钥。
- 确认不使用示例机构学生、老师、家长、聊天和运行账本。

## 2. 生成租户包

示例命令：

```text
python scripts/tenant_initializer.py --tenant-profile work/commercialization/demo_tenant_profile.json --platform-root <temp_or_delivery_root> --force
```

要求：

- 输出目录必须是新机构独立目录。
- `data/` 下账本文件为空白初始化。
- `memory/` 和 `skills/` 只包含当前机构事实和通用托管常识。
- 报告写入当前租户 `reports/startup/`。

## 3. 启动前验收

示例命令：

```text
python scripts/tenant_acceptance_check.py --tenant-root <tenant_root>
```

必须通过：

- `STATUS:PASS`。
- 空白账本检查通过。
- 明文密钥检查通过。
- 示例机构污染词检查通过。
- 家长自动发送关闭。
- 店长/老师主动外发不是默认真实发送。

允许 warning：

- 试点学生为空。
- 老板当前目标为空。
- 店长或老师账号尚未进入灰度。

## 4. 首次聊天验收

用老板账号确认：

- 小优知道当前机构名称。
- 小优能说明自己知道什么、不知道什么。
- 小优不会引用示例机构真实资料。
- 小优能解释外发、写入和高风险动作边界。

用店长或老师账号确认：

- 只能看到本角色允许的信息。
- 不能访问老板专属价值账本。
- 不能越权调整目标、工资、绩效或权限。

## 5. 试点运行

- 第一天只开老板和店长。
- 每天查看日报、待确认事项和错误日志。
- 老师侧先选 1-2 人灰度。
- 家长自动发送继续关闭。
- 一周后按 `acceptance_record_template.md` 做复盘。

## 6. 失败处理

任一 P0 失败时：

- 停止真实外发。
- 保存初始化和验收报告。
- 修复 profile、模板或代码。
- 重新生成租户包并重新验收。

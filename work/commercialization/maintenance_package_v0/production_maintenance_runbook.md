# 生产维护 Runbook V0

本文用于示例机构样板生产和后续单机构试点的日常维护。默认生产只读，除非另有已批准的升级或修复计划。

## 1. 日常只读巡检

服务状态：

```text
ssh hermes-aliyun "systemctl is-active hermes-youyi-019.service"
ssh hermes-aliyun "systemctl status hermes-youyi-019.service --no-pager --full | head -80"
```

timer 状态：

```text
ssh hermes-aliyun "systemctl list-timers --all | grep hermes-youyi"
```

加载路径：

```text
ssh hermes-aliyun "systemctl cat hermes-youyi-019.service"
```

最近日志：

```text
ssh hermes-aliyun "journalctl -u hermes-youyi-019.service -n 200 --no-pager"
```

资源摘要：

```text
ssh hermes-aliyun "df -h; free -h; ps -eo pid,comm,rss,vsz,etime,args | grep -E 'hermes|python' | grep -v grep"
```

## 2. 日志快速判断

P0：

- `ImportError`
- `ModuleNotFoundError`
- 连续 `Traceback`
- 服务不是 `active`
- 外发队列异常膨胀
- 写入守卫报错导致主链路不可用

P1：

- 外部学习失败但已记录 `source_failed`
- 某个定时任务偶发失败后恢复
- 资源占用升高但服务仍稳定

P2：

- 文案不顺、日报表达不清。
- warning 类验收提示。
- 非生产路径文档或 demo 问题。

## 3. 处理原则

- P0：先停止扩大灰度，保留日志，确认最近版本和加载路径，再制定最小修复计划。
- P1：记录问题和频率，优先本地复现，不直接在生产上试错。
- P2：进入 backlog，跟随下一轮小版本处理。

## 4. 绝对禁止

- 不直接编辑生产业务数据。
- 不删除生产目录里的未知模块或历史诊断文件。
- 不手动触发家长、老师或店长真实外发。
- 不用示例机构数据初始化新机构。
- 不把线上密钥、聊天记录、学生老师资料复制进 Git。

## 5. 每次维护记录

维护完成后记录：

- 时间：
- 操作人：
- 目标服务：
- 当前版本：
- 加载路径：
- 执行命令摘要：
- 是否写入生产：
- 是否重启服务：
- 日志结论：
- 后续风险：

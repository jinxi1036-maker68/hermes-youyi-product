# Project Knowledge 更新协议｜KH-003

## 1. 机器事实分层

- `PROJECT_INDEX.json`：项目级当前事实与路由权威；
- `CURRENT_WORK.json`：当前唯一 work item；
- `WORKING_METHOD.json`：当前工作方式；
- `EVIDENCE_INDEX.json`：可追溯证据。

Markdown 不建立第二套动态权威。

## 2. Current 不是 History

work item 关闭时：
1. 把最终记录归档到 `knowledge/archive/work_items/<id>.md`；
2. 只把长期有价值结论写入 Evidence / History / ADR / Capability；
3. 用新的 CURRENT_WORK.json 替换旧工作；
4. 重新生成 CURRENT_STATE / CURRENT_WORK Markdown。

禁止完成后继续往 Current 追加。

## 3. Progressive Disclosure

Bootstrap 后依据 CURRENT_WORK.context_profile 读取 profile.required + must_read；on_demand 只有问题需要时读取。

Evidence / History / Archive 不得成为默认启动上下文。

## 4. Generated Projections

`05_CURRENT_STATE.md`、`CURRENT_WORK.md` 禁止手工编辑。

机器事实变化后：
- `python knowledge/tools/render_current.py --write`
- `python knowledge/tools/validate_knowledge.py`

Validator 不通过则 Knowledge Sync 未完成。

## 5. 并发写入

材料性更新使用 optimistic concurrency：K0 → 准备 → 写前 K1；K1==K0 才 fast-forward + `force=false`。若 K1 改变，必须重读、语义合并、重新 render/validate。禁止 force push。

## 6. Evidence / ADR / Capability

关键 PASS/FAIL/BLOCKED 进入 Evidence；长期架构/权威/安全/Harness 决策进入 ADR；Capability 只保存稳定语义、验收、reopen 条件；History 只保存里程碑摘要。

## 7. Working Method

WM-006 和 Method Learning Loop 继续有效。KH-003 不降低角色、安全、Evidence 或生产纪律。

## 8. Definition of Done

```text
实现/设计完成
+ 必要验证/真人证据
+ Current Work 收口
+ Generated projections 刷新
+ Validator PASS
= Done
```

## 9. 不进入 Current

全量日志、临时命令、已完成 debug 流水、旧阶段操作步骤、归档切换过程、与当前 work item 无直接关系的背景。

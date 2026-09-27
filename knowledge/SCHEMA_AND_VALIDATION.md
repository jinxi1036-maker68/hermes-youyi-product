# Schema 与 Knowledge Health Validation｜KH-003

## 目标

Validator 不只检查 JSON 可解析，还要检查“当前知识是否真的可继续工作”。

## 机器合同

- PROJECT_INDEX：项目当前事实；
- CURRENT_WORK：当前唯一工作项；
- KNOWLEDGE_HARNESS：上下文路由、预算、生命周期；
- WORKING_METHOD：当前工作方法。

## Validator 必查

### Current 一致性
- Index active_work_item_id == CURRENT_WORK.work_item_id；
- CURRENT_WORK formal stage context 与当前 Stage 一致；
- context_profile 存在；
- profile.required / must_read / on_demand 引用存在；
- CURRENT_WORK 禁止 history / timeline / debug_log。

### Projection drift
- CURRENT_STATE 必须等于 renderer 输出；
- CURRENT_WORK.md 必须等于 renderer 输出；
- ACTIVE_WORK 只能是 deprecated compatibility pointer。

### Context health
- profile 前 machine bootstrap 只有 PROJECT_INDEX + CURRENT_WORK；
- orientation 不得加载 History/Evidence/Archive；
- Current/入口文件满足 byte budget；
- 非 audit profile 不允许 required History/Archive。

### 原有不变量
SHA、Stage/roadmap、sealed Evidence、Working Method lineage、force=false、静态入口不硬编码当前 SHA、public-safe/secret scan、可选 live-main Freshness。

## 使用

```bash
python knowledge/tools/render_current.py --check
python knowledge/tools/validate_knowledge.py
```

机器事实变化后用 `render_current.py --write` 再验证。

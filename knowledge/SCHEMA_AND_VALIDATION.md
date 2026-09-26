# Schema 与一致性校验

## 目标

项目记忆也必须可测试。

V2 提供：
- `schema/project_index.schema.json`
- `tools/validate_knowledge.py`

## Validator 检查

- PROJECT_INDEX JSON 可解析；
- schema_version 正确；
- main / production SHA 为 40 位 hex；
- current stage 出现在 roadmap；
- sealed capability 有 Evidence id；
- Evidence id 真正存在；
- ACTIVE_WORK work_item_id 与 PROJECT_INDEX 一致；
- START_HERE / handoff 不硬编码 40位当前 SHA；
- 必需文件存在；
- project-knowledge 工作树顶层只允许 README 和 knowledge；
- 不出现明显 secret 文件名。

## Freshness 不由静态 Validator 代替

Validator 只能证明“知识库内部一致”。

GitHub main 和 production 是否仍是最新事实，必须执行 Freshness Gate。

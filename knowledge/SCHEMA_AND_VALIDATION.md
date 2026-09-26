# Schema 与一致性校验

## 目标

项目记忆也必须像代码一样可检查。

当前提供：
- `schema/project_index.schema.json`
- `tools/validate_knowledge.py`

## Validator 当前检查

### 结构
- PROJECT_INDEX / Evidence / Working Method JSON 可解析；
- schema_version / knowledge_revision；
- main / production SHA 格式；
- current stage 唯一存在于 roadmap；
- ACTIVE_WORK id 与 PROJECT_INDEX 一致；
- sealed capability 有真实 Evidence id；
- Working Method id 与 PROJECT_INDEX 一致；
- Method History 文件全部存在；
- CURRENT_WORKING_METHOD 与当前 method id 一致；
- project-knowledge 当前工作树保持纯知识。

### 防动态事实漂移
- START_HERE / handoff 不允许硬编码 40位当前 SHA；
- CURRENT_STATE 必须明确 PROJECT_INDEX 是动态事实权威。

### 内容安全
- 禁止明显敏感文件名；
- 扫描高置信 credential / private-key 模式。

### 可选 live-main Freshness
执行者可设置 `XIAOYOU_LIVE_MAIN_SHA`，Validator会比较 PROJECT_INDEX 的 main_sha。

## Validator 不负责什么

Validator只能证明**知识库内部一致性**。

它不能证明：
- Production仍是原版本；
- GitHub main没有刚发生外部变化（除非传入 live SHA）；
- 业务现实没有改变；
- 一条知识从商业角度是否应该公开。

这些分别由 Freshness Gate、生产只读核验和 Public Safety判断负责。

## JSON Schema

Schema 是机器合同和结构文档。

Validator 当前使用标准库实现关键不变量，不依赖额外 `jsonschema` 包，以保证任何基础 Python 环境都能运行。

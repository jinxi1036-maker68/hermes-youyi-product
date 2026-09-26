# Knowledge / Memory / Learning 设计基线

> 这是未来能力设计基线，当前不要提前实现到生产 Stage 2。

## 为什么需要

通用模型有通用智能，但不知道“这家机构具体怎么工作”。

真正成熟的数字员工需要同时拥有：

- **Business Truth**：现在真实发生什么；
- **Knowledge**：机构制度、标准、方法、验证过的经验；
- **Memory / History**：过去发生过什么；
- **Goal / Work State**：当前要完成什么；
- **Learning**：如何从错误与结果中形成改进；
- **Model Reasoning**：综合这些信息决定下一步。

## Project Knowledge 与 Institution Knowledge

### Project Knowledge
给开发小U的人和AI使用。
本 `project-knowledge` 分支就是第一版。

### Institution Knowledge
未来给运行中的小U使用。
可能包含：
- 制度
- SOP
- 培训材料
- 服务标准
- 经营方法
- 已验证经验

## Knowledge 不等于 Vector Store

真正需要的是：

**Knowledge Authority + Retrieval**

每条正式知识至少应该带：
- content
- source
- type
- scope
- permission
- effective_from / effective_to
- status
- authority_level
- version

不能把：
- 正式制度
- 老板讨论草案
- 老师个人经验
- 旧版本政策

当成同等权威。

## 性能原则

知识库可以持续增长，但单次模型上下文必须有硬预算。

原则：

```text
需要时才查
→ metadata 缩小范围
→ keyword/vector hybrid retrieval
→ rerank
→ 只给模型少量最相关片段
→ 不够再二次检索
```

知识规模增长不应线性增加模型上下文和响应时间。

## Learning

Learning 不是“把聊天全部存下来”。

长期闭环：

```text
事件
→ 结果
→ 纠正/反馈
→ 提炼候选经验
→ 验证
→ 晋升为机构知识
→ 相似场景再次使用
→ 再验证
```

高风险经验不能自动变成：
- 权限
- 处罚
- 工资制度
- 价格制度
- 法律/安全规则

需要人工确认。

## GraphRAG

第一版不需要 GraphRAG。
优先：
- structured metadata
- keyword
- vector semantic search
- rerank
- permission filtering
- citations

只有知识规模和跨文档关系复杂到普通 RAG 明显不足时，再评估 Knowledge Graph / GraphRAG。

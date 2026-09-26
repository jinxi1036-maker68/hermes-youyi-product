# 权威、数据源与事实原则

## 1. 项目层权威

- GitHub main：代码权威。
- Production server：当前部署事实。
- project-knowledge：项目状态与设计知识权威。
- stable tag / PASS report：封板证据。
- 旧聊天总结：辅助历史，不是当前权威。

## 2. 业务层权威原则

不同事实必须有不同权威来源，不能用“一个大JSON”解决全部问题。

### Identity
当前人员身份、角色、在职状态应从当前人员权威读取。
旧 staff / 历史映射只能作兼容辅助，不能继续当现时授权根。

### Student / Service
学生主档、服务关系、服务负责人、服务内部记录是不同概念。
退出一个服务不能自动删除整个学生主档。

### Tasks
任务 inventory、数量、状态、日期范围必须来自权威 task query。
active context 不能支持“今天一共有多少任务”等 inventory 事实。

### Agenda
Agenda runtime status / heartbeat 是运行状态；ticket、work fact、reply job 等是业务语义。
不能用 SQLite/WAL/SHM 文件字节变化直接判断业务污染。

## 3. Unknown 原则

- 没有记录 ≠ 没发生。
- 一个来源查不到 ≠ 现实不存在。
- 不知道必须保持 Unknown。
- 缺信息时先判断事实归属人或系统。

## 4. 事实归属

- 老板：战略、制度、重大经营取舍、敏感人事、费用、正式目标。
- 店长：现场运行、协调、跨班事项、责任关系、投诉进展。
- 老师：本人负责学生的一手事实、家长沟通结果、本人任务执行。
- 系统：已经结构化并有权威来源的名单、任务、服务关系、记录、状态。
- 外部网络：政策、行业、方法、市场信息；必须保留来源，不能冒充机构内部事实。

## 5. 执行真实性

必须区分：
- intent
- candidate
- queued
- sent / API accepted
- received
- completed
- verified

不能把：
- “已入队”说成“已发送”
- “API accepted”说成“对方已读”
- “Tool 调用成功”说成“业务结果已完成”
- “写入发起”说成“写回验证通过”

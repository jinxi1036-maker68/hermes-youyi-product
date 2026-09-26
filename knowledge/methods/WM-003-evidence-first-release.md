# WM-003｜Evidence-First / Stage-Sealed 工作法

- Status: Retained and expanded by WM-004
- Period: Query / Runtime Topology 阶段

## 背景

生产连续遇到：
- 权限误判；
- runtime自然变化被误认为业务污染；
- candidate Home 权限污染；
- cross-release state；
- Core shim shadow；
- bootstrap ingress 风险。

单纯“修一个错就再部署”产生过多重试和新问题。

## 改进

引入：
- 单阶段封板；
- 冻结验收标准；
- baseline A/B；
- candidate-only failures；
- PASS / FAIL / BLOCKED 区分；
- 技术门禁和真人业务验收分层；
- 多次部署失败后 STOP，不盲重试；
- 根因证据优先；
- rollback-first。

## 收益

把“不断试部署”变成“先证明为什么失败，再决定是否有资格再部署”。

## 局限

虽然执行质量提高，但工作方式本身还没有成为正式版本化知识，新窗口仍可能继承项目结论却改变做事节奏。

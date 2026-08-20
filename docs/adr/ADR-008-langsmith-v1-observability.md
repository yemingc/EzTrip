# ADR-008：V1 使用 LangSmith Cloud，不与 Langfuse 双写

- 状态：Accepted
- 日期：2026-08-20

## 背景

EzTrip 需要观察 LangGraph node、tool 和 LLM 调用层级，并把模型、Prompt、延迟、token、错误与后续评测关联。候选平台为 LangSmith Cloud 和 Langfuse；当前已有 LangSmith Key，且项目采用 LangGraph。

## 候选方案

1. V1 使用 LangSmith Cloud；
2. V1 使用 Langfuse Cloud 或自托管；
3. 两个平台同时写入；
4. 只写本地日志。

## 决策

采用方案 1。业务代码不直接依赖平台页面，而通过观测配置和 trace metadata 约定接入。Gate 0 用隔离的三节点探针验证：

- LangGraph node、fixture tool 和 DeepSeek LLM span 的嵌套层级；
- project、tags、model、data mode 等 metadata；
- token/延迟记录、受控工具错误和 root trace 状态；
- 配置密钥、常见邮箱和中国手机号在上传前脱敏；
- CI 只运行 fake model/fixture 测试，不读取真实 Key 或上传 trace。

V1 不双写 LangSmith 与 Langfuse，避免把时间消耗在两套 instrumentation 和数据一致性上。

## 后果

- 能较早获得 LangGraph 原生 trace 和面试证据。
- 使用 Cloud 意味着测试输入会离开本机，因此 live probe 只发送固定合成请求；未来真实用户输入必须先完成隐私策略。
- 平台绑定通过内部 metadata/redaction 约定控制，但 dashboard、dataset 和 evaluator 仍需迁移成本。

## 重新评估条件

当出现自托管刚需、LangSmith 免费额度/保留期不满足、关键自定义 span 无法表达、导出受限，或团队已有 OpenTelemetry/Langfuse 基础设施时，重新评估 Langfuse。切换前先做同一固定 case 的半天探针，不双写生产流量。

## 参考

- [LangSmith：Trace LangGraph applications](https://docs.langchain.com/langsmith/trace-with-langgraph)
- [LangSmith：LangGraph observability and anonymizers](https://docs.langchain.com/oss/python/langgraph/observability)

# EzTrip Architecture Decision Records

ADR 记录已经接受的架构选择、候选方案、代价和重新评估条件。它们是公开工程文档，不包含密钥、真实用户数据或未脱敏 provider 响应。

| ADR | 状态 | 决策 |
|---|---|---|
| [ADR-001](./ADR-001-langgraph-deterministic-workflow.md) | Accepted | 使用 LangGraph 显式状态图作为工作流骨架 |
| [ADR-002](./ADR-002-agent-and-deterministic-node-boundary.md) | Accepted | 分离 Agent、确定性节点与外部工具职责 |
| [ADR-003](./ADR-003-amap-provider-strategy.md) | Accepted | 高德官方 MCP 优先，自有 typed provider 隔离协议 |
| [ADR-004](./ADR-004-hotel-data-truth-boundary.md) | Accepted | 酒店 POI、估算价格与实时库存严格分层 |
| [ADR-005](./ADR-005-versioned-domain-contracts.md) | Accepted | 使用版本化 Pydantic 契约统一领域边界 |
| [ADR-006](./ADR-006-stateful-checkpoint-and-hitl.md) | Accepted | 主编排图使用持久化检查点与显式 HITL |
| [ADR-008](./ADR-008-langsmith-v1-observability.md) | Accepted | V1 使用 LangSmith Cloud，不与 Langfuse 双写 |
| [ADR-009](./ADR-009-provider-backed-destination-resolution.md) | Accepted | 城市身份由 Provider 解析并在任务创建前消歧 |

# ADR-006：主编排图使用持久化检查点与显式 HITL

- 状态：Accepted
- 日期：2026-08-21

## 背景

Gate 2 已能从结构化请求生成 provider-grounded 三日草案并做确定性校验，但一次调用结束后没有可恢复的工作流状态，也没有真正暂停等待人工决定。仅在内存中保存结果无法证明进程/运行时重建后的恢复能力；把人工确认写成普通模型输出又会混淆用户授权与模型推断。

## 决策

建立 `stateful-planning-checkpoint-v1` 主编排图：

1. `run_vertical_slice` 原子执行现有 Gate 2 纵向切片；
2. `prepare_human_review` 根据确定性 Validator 结果生成允许动作；
3. `human_review` 使用 LangGraph 原生 `interrupt()` 暂停，并只接受带 `review_id`、动作和 reviewer 的 `Command(resume=...)`；
4. `apply_review_decision` 把批准、确认冲突和取消映射到终态，不修改原计划内容；结构化 `request_revision` 继续进入受限 revision node。

本地和 CI 使用 `AsyncSqliteSaver`，以同一个 `thread_id` 在关闭并重新构建 graph/runtime 后读取检查点。检查点只持久化 JSON-compatible 数据，节点入口再恢复为 Pydantic 强类型模型。内部候选搜索图和 Single Planner 图显式使用 `checkpointer=False`，因此只有主编排图拥有持久化边界；恢复不会落在半个内部子图中。

人工策略由普通代码决定：

- 校验可通过的草案允许 `approve_draft`、`request_revision`、`cancel`；
- 有硬冲突的草案不允许批准，只允许 `acknowledge_conflict`、`request_revision`、`cancel`。

即使人工批准，`TripPlan.status` 仍保持 `draft`；工作流终态使用 `approved_draft`，避免把审核草案误写成已预订、可执行或最终计划。

## 候选方案

- `InMemorySaver`：实现简单，但无法证明 runtime 重建后的恢复；
- 直接使用 PostgreSQL checkpointer：更接近部署形态，但当前阶段会把验证重点转移到基础设施和迁移；
- 自建状态表和暂停协议：控制力高，但会重复实现 LangGraph 已提供的 interrupt/resume 语义；
- 不做持久化、由客户端重发完整上下文：无法保证幂等，也容易重复调用 provider 和模型。

## 后果

- 可以用磁盘检查点证明暂停、重建、恢复和不重复执行昂贵规划步骤；
- `thread_id` 已存在时拒绝重新开始，错误 review ID 或不允许动作不会消费中断；
- SQLite 文件含完整结构化请求、候选、计划和人审记录，本地开发也应视为用户数据；
- SQLite 不等同于生产级并发、加密、备份或高可用方案。

## 2026-08-23 扩展：结构化局部修改

EZ-403B 为 `request_revision` 增加 `PlanRevisionRequest`：客户端必须提交 base version/plan、目标日期、目标 item、受保护 item、延后分钟数和显式确认。服务端在接受决定前验证 optimistic concurrency 与完整 scope；恢复后新增 `apply_plan_revision` 节点，只允许把目标日所有现有活动整体延后 30–180 分钟。

该节点复用原 Provider 候选和 Planner 结果，模型与 Provider 调用固定为 0；其他日期、候选集合、费用、天气和来源必须逐项保持。修改后重新执行当前产品 `deterministic-plan-validator-v1`，生成 `PlanVersion v2` 与 `v1 → v2` diff。v2 保持 draft 且标明尚未再次审核。开放式自然语言修改、候选增删、路线/营业时间重新计算和完整 Hard Validator/Repair Router 接线不属于这个窄执行器。

## 重新评估条件

接入规划 API、并发 worker 或真实用户数据前，评估 PostgreSQL checkpointer，并补充静态/传输加密、身份与租户隔离、保留期、删除流程、并发 resume、进程强杀和 schema migration 测试。若内部子图未来需要独立的长时暂停，再单独定义嵌套 checkpoint namespace 和恢复责任，不能依赖隐式继承。
